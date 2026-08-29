"""Descriptor-safe CLI onboarding and reviewed intent proposal commands."""

# ruff: noqa: B008

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from fnmatch import fnmatchcase
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol, cast
from urllib.parse import unquote, urlsplit, urlunsplit

import typer
import yaml  # type: ignore[import-untyped]
from pydantic import ConfigDict

from intent_engineering.capture.base import RawSourceObject
from intent_engineering.capture.github.connector import GitHubCheckpoint
from intent_engineering.capture.markdown.connector import MarkdownConnector
from intent_engineering.capture.mcp.profile_loader import load_strict_yaml_mapping_bytes
from intent_engineering.cli.connectors import configured_actor_principals, connector_catalog
from intent_engineering.cli.output import OutputFormat, emit
from intent_engineering.cli.runtime import Runtime, github_repository_scope, load_runtime
from intent_engineering.cli.writes import MutationPolicy, policy_actor_aliases
from intent_engineering.core.models import (
    JsonValue,
    ProjectConfig,
    SourceRole,
    SourceRoleAssignment,
)
from intent_engineering.core.models._base import StrictModel
from intent_engineering.core.policy import ProjectNotInitialized, initialize_project
from intent_engineering.intent_workflow.bootstrap import BootstrapService
from intent_engineering.intent_workflow.onboarding import (
    OnboardingRuntime,
    OnboardingState,
    OnboardingStatus,
    inspect_onboarding,
)
from intent_engineering.storage._atomic import same_path_lock
from intent_engineering.storage.executor import LocalChangeSetExecutor
from intent_engineering.storage.secure import SecureRead

sources_app = typer.Typer(help="Configure explicit source roles for intent interpretation.")
proposals_app = typer.Typer(help="Inspect and confirm reviewed intent proposals.")

_MAX_PRD_BYTES = 1_048_576
_MAX_PATH_CHARS = 2048
_MAX_PROPOSALS = 256
_PROPOSAL_ID = re.compile(r"proposal:sha256:[0-9a-f]{64}\Z")
_URI_SCHEME = re.compile(r"[a-z][a-z0-9+.-]{0,31}\Z")


class OnboardState(StrEnum):
    """Stable public states emitted by the guided onboarding command."""

    CONFIRMATION_REQUIRED = "confirmation_required"
    PROPOSAL_REQUIRED = "proposal_required"
    REVIEW_REQUIRED = "review_required"
    READY = "ready"


class OnboardingCommandResult(StrictModel):
    """Frozen, capability-free projection of one guided onboarding decision."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    state: OnboardState
    graph_version: int
    active_node_count: int
    pending_proposal_ids: tuple[str, ...]
    source_role: SourceRoleAssignment | None = None
    proposal: dict[str, JsonValue] | None = None
    next_action: str | None = None
    message: str | None = None
    authorization_issued: Literal[False] = False


class ProposalTerminal(Protocol):
    """Minimal injectable terminal boundary for exact proposal confirmation."""

    def is_interactive(self) -> bool: ...

    def display_preview(self, preview: dict[str, object]) -> None: ...

    def read_confirmation(self, proposal_digest: str) -> str: ...


@dataclass(frozen=True)
class ConsoleProposalTerminal:
    """Production terminal that refuses piped or unattended confirmation."""

    def is_interactive(self) -> bool:
        return sys.stdin.isatty()

    def display_preview(self, preview: dict[str, object]) -> None:
        typer.echo(
            json.dumps(preview, ensure_ascii=False, indent=2, sort_keys=True),
            err=True,
        )

    def read_confirmation(self, proposal_digest: str) -> str:
        return cast(str, typer.prompt(f"Type 'confirm {proposal_digest}' to confirm"))


def terminal() -> ProposalTerminal:
    return ConsoleProposalTerminal()


def _fixed_error(exit_code: int = 1) -> None:
    typer.echo("intent error: intent onboarding failed", err=True)
    raise typer.Exit(exit_code)


def _canonical_scope(value: str, *, markdown_file: bool = False) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > _MAX_PATH_CHARS
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("invalid source scope")
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or relative.as_posix() != value
        or value == "."
        or any(part in {"", ".", ".."} for part in relative.parts)
        or (markdown_file and relative.suffix.casefold() != ".md")
    ):
        raise ValueError("invalid source scope")
    return value


def _canonical_provider_scope(value: str) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > _MAX_PATH_CHARS
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("invalid source scope")
    parsed = urlsplit(value)
    if (
        _URI_SCHEME.fullmatch(parsed.scheme) is None
        or parsed.scheme != parsed.scheme.lower()
        or parsed.query
        or urlunsplit(parsed) != value
    ):
        raise ValueError("invalid source scope")
    decoded_parts = tuple(part for part in unquote(parsed.path).split("/") if part)
    if any(part in {".", ".."} for part in decoded_parts):
        raise ValueError("invalid source scope")
    if parsed.netloc:
        if (
            parsed.scheme != "https"
            or parsed.hostname is None
            or parsed.hostname != parsed.hostname.lower()
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or parsed.netloc != parsed.hostname
            or not parsed.path.startswith("/")
        ):
            raise ValueError("invalid source scope")
    elif not parsed.path:
        raise ValueError("invalid source scope")
    return value


def _source_identity_and_scope(
    runtime: Runtime,
    connector_id: str,
    scope: str,
) -> tuple[str, str]:
    if connector_id == "markdown":
        return connector_id, _canonical_scope(scope)
    if connector_id == "git":
        return connector_id, (
            _canonical_provider_scope(scope)
            if _URI_SCHEME.match(scope)
            else _canonical_scope(scope)
        )
    if connector_id.startswith("github:"):
        repository = connector_id.removeprefix("github:")
        GitHubCheckpoint(repository=repository)
        if github_repository_scope(os.environ) != repository:
            raise ValueError("GitHub connector is not configured")
        canonical = _canonical_provider_scope(scope)
        parsed = urlsplit(canonical)
        repository_path = f"/{repository}"
        if parsed.hostname != "github.com" or not (
            parsed.path == repository_path or parsed.path.startswith(f"{repository_path}/")
        ):
            raise ValueError("GitHub source scope mismatch")
        return connector_id, canonical
    canonical_id = connector_catalog(runtime).source_role_connector_id(connector_id)
    return canonical_id, _canonical_provider_scope(scope)


def _snapshot_config(runtime: Runtime) -> tuple[ProjectConfig, bytes]:
    config_file = runtime.workspace_directory.file("config.yaml")
    try:
        content = config_file.read_bytes_nonblocking()
        loaded = load_strict_yaml_mapping_bytes(content)
        config = ProjectConfig.model_validate_json(
            json.dumps(loaded, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        )
    finally:
        config_file.close()
    if config != runtime.config:
        raise ValueError("project configuration changed")
    return config, content


def _principals(runtime: Runtime, config: ProjectConfig) -> frozenset[str]:
    return frozenset(
        {
            config.local_actor,
            *configured_actor_principals(runtime, actor=config.local_actor),
            *policy_actor_aliases(runtime, actor=config.local_actor),
        }
    )


def _bootstrap_service(runtime: Runtime, config: ProjectConfig) -> BootstrapService:
    return BootstrapService(
        graph_store=runtime.graph_store,
        evidence_store=runtime.evidence_store,
        proposal_store=runtime.intent_proposals,
        changeset_executor=LocalChangeSetExecutor(
            runtime.graph_store,
            runtime.case_store,
            runtime.transactions,
        ),
        transactions=runtime.transactions,
        config=config,
    )


def _excluded(config: ProjectConfig, relative: PurePosixPath) -> bool:
    path = relative.as_posix()
    return any(
        relative.match(pattern) or fnmatchcase(path, pattern)
        for pattern in config.source_exclusions
    )


def _capture_prd(runtime: Runtime, config: ProjectConfig, relative_path: str) -> str:
    relative = PurePosixPath(_canonical_scope(relative_path, markdown_file=True))
    if _excluded(config, relative):
        raise ValueError("excluded source")
    snapshot = runtime.project_directory.read_relative(
        relative,
        nonblocking=True,
        max_bytes=_MAX_PRD_BYTES,
    )
    content = snapshot.content
    reauthenticated: SecureRead | None = None
    try:
        text = content.decode("utf-8")
        digest = f"sha256:{sha256(content).hexdigest()}"
        raw = RawSourceObject(
            connector_type="markdown",
            external_object_id=f"path:{relative_path}",
            external_version=digest,
            author=config.local_actor,
            observed_at=datetime.fromtimestamp(
                snapshot.modified_ns / 1_000_000_000,
                tz=UTC,
            ),
            source_locator=relative_path,
            content_hash=digest,
            payload={"path": relative_path, "content": text},
        )
        record = MarkdownConnector.normalize(cast(MarkdownConnector, None), raw)
        try:
            durable = runtime.evidence_store.get(record.id)
        except KeyError:
            durable = None
        if durable is not None:
            if durable.model_copy(update={"observed_at": record.observed_at}) != record:
                raise ValueError("evidence identity conflict")
            record = durable
        reauthenticated = runtime.project_directory.read_relative(
            relative,
            expected_identities=snapshot.identities,
            nonblocking=True,
            max_bytes=_MAX_PRD_BYTES,
        )
        if (
            reauthenticated.content != content
            or reauthenticated.modified_ns != snapshot.modified_ns
        ):
            raise ValueError("source changed")
        runtime.evidence_store.associate("markdown", record)
        return record.id
    finally:
        content = b""
        snapshot = cast(SecureRead, None)
        reauthenticated = None


def _bootstrap_result(
    project: Path,
    prd: str,
) -> tuple[bool, dict[str, object] | None, BaseException | None]:
    runtime: Runtime | None = None
    payload: dict[str, object] | None = None
    try:
        runtime = load_runtime(project)
        config, _ = _snapshot_config(runtime)
        graph_version = runtime.graph_store.load().version
        proposal_bytes = runtime.intent_proposals.bytes()
        evidence_ref = _capture_prd(runtime, config, prd)
        if (
            runtime.graph_store.load().version != graph_version
            or runtime.intent_proposals.bytes() != proposal_bytes
            or _snapshot_config(runtime)[0] != config
        ):
            raise ValueError("project state changed")
        payload = {
            "status": "agent_submission_required",
            "graph_version": graph_version,
            "evidence_refs": [evidence_ref],
            "context_packet": {
                "repository_id": config.project_id,
                "connector_id": "markdown",
                "scope": prd,
                "source_role": SourceRole.DECLARED_INTENT.value,
            },
        }
        return True, payload, None
    except Exception:  # noqa: BLE001 - public CLI receives one fixed failure
        return False, None, None
    except BaseException as error:  # noqa: BLE001 - preserve detached control flow
        error.__traceback__ = None
        error.__cause__ = None
        error.__context__ = None
        return False, None, error
    finally:
        project = Path()
        prd = ""
        runtime = None


def bootstrap_command(
    prd: str = typer.Option(..., "--prd"),
    project: Path = typer.Option(Path("."), "--project"),
    output_format: OutputFormat = typer.Option(OutputFormat.TEXT, "--format"),
) -> None:
    """Capture one existing Markdown PRD and request a typed agent submission."""
    ok, payload, abort = _bootstrap_result(project, prd)
    del project, prd
    if abort is not None:
        raise abort
    if not ok or payload is None:
        _fixed_error()
    emit(payload, output_format)
    raise typer.Exit(4)


def _source_role_result(
    project: Path,
    connector_id: str,
    scope: str,
    role: SourceRole,
    inherited: bool,
) -> tuple[bool, dict[str, object] | None, BaseException | None]:
    runtime: Runtime | None = None
    config_file = None
    try:
        runtime = load_runtime(project)
        config, preimage = _snapshot_config(runtime)
        canonical_connector_id, canonical_scope = _source_identity_and_scope(
            runtime,
            connector_id,
            scope,
        )
        assignment = SourceRoleAssignment(
            connector_id=canonical_connector_id,
            scope=canonical_scope,
            role=role,
            inherited=inherited,
        )
        retained = tuple(
            item
            for item in config.source_roles
            if (item.connector_id, item.scope) != (canonical_connector_id, canonical_scope)
        )
        updated = config.model_copy(update={"source_roles": (*retained, assignment)})
        updated = ProjectConfig.model_validate_json(updated.model_dump_json())
        config_file = runtime.workspace_directory.file("config.yaml")
        with same_path_lock(config_file):
            if config_file.read_bytes_nonblocking() != preimage:
                raise ValueError("project configuration changed")
            if updated == config:
                return (
                    True,
                    {
                        "status": "unchanged",
                        "source_role": assignment.model_dump(mode="json"),
                    },
                    None,
                )
            encoded = yaml.safe_dump(
                updated.model_dump(mode="json"),
                allow_unicode=True,
                sort_keys=True,
            ).encode("utf-8")
            config_file.atomic_write(encoded, reject_target_races=True)
        return (
            True,
            {
                "status": "configured",
                "source_role": assignment.model_dump(mode="json"),
            },
            None,
        )
    except Exception:  # noqa: BLE001 - config/path details remain private
        return False, None, None
    except BaseException as error:  # noqa: BLE001 - preserve detached control flow
        error.__traceback__ = None
        error.__cause__ = None
        error.__context__ = None
        return False, None, error
    finally:
        if config_file is not None:
            config_file.close()
        project = Path()
        connector_id = scope = ""
        runtime = None


@sources_app.command("add")
def sources_add_command(
    connector_id: str,
    scope: str,
    role: SourceRole = typer.Option(..., "--role"),
    inherited: bool = typer.Option(False, "--inherited"),
    project: Path = typer.Option(Path("."), "--project"),
    output_format: OutputFormat = typer.Option(OutputFormat.TEXT, "--format"),
) -> None:
    """Atomically add or replace one exact connector/scope source role."""
    ok, payload, abort = _source_role_result(project, connector_id, scope, role, inherited)
    del project, connector_id, scope, role
    if abort is not None:
        raise abort
    if not ok or payload is None:
        _fixed_error()
    emit(payload, output_format)


def _onboarding_result(
    status: OnboardingStatus,
    *,
    state: OnboardState,
    source_role: SourceRoleAssignment | None = None,
    proposal: dict[str, JsonValue] | None = None,
    next_action: str | None = None,
    message: str | None = None,
) -> OnboardingCommandResult:
    """Project detached inspection data into the public guided-command envelope."""
    return OnboardingCommandResult(
        state=state,
        graph_version=status.graph_version,
        active_node_count=status.active_node_count,
        pending_proposal_ids=status.pending_proposal_ids,
        source_role=source_role,
        proposal=proposal,
        next_action=next_action,
        message=message,
    )


def _existing_onboarding_result(
    runtime: Runtime,
    status: OnboardingStatus,
) -> OnboardingCommandResult:
    """Return a byte-noop review or ready projection from fresh inspected state."""
    if status.state is OnboardingState.READY:
        return _onboarding_result(status, state=OnboardState.READY)
    if status.state is not OnboardingState.REVIEW_REQUIRED or not status.pending_proposal_ids:
        raise ValueError("onboarding state changed")
    config, _ = _snapshot_config(runtime)
    preview = proposal_payload(runtime, config, status.pending_proposal_ids[0])
    return _onboarding_result(
        status,
        state=OnboardState.REVIEW_REQUIRED,
        proposal=cast(dict[str, JsonValue], preview),
        next_action="intent_proposal_confirm",
    )


def _inspect_onboarding(runtime: Runtime) -> OnboardingStatus:
    """Adapt the CLI's immutable runtime to the read-only onboarding protocol."""
    return inspect_onboarding(cast(OnboardingRuntime, runtime))


def _uninitialized_onboarding_status() -> OnboardingStatus:
    """Represent a missing workspace as a consent-gated, pre-baseline repository."""
    return OnboardingStatus(
        state=OnboardingState.REQUIRED,
        graph_version=0,
        active_node_count=0,
        pending_proposal_ids=(),
    )


def _post_capture_onboarding_result(
    runtime: Runtime,
    status: OnboardingStatus,
    source_role: SourceRoleAssignment,
) -> OnboardingCommandResult:
    """Project the exact state observed after capture/configuration without overwriting it."""
    if status.state is not OnboardingState.REQUIRED:
        return _existing_onboarding_result(runtime, status)
    return _onboarding_result(
        status,
        state=OnboardState.PROPOSAL_REQUIRED,
        source_role=source_role,
        next_action="intent_bootstrap_propose",
    )


def _provision_local_clarification_policy(runtime: Runtime, config: ProjectConfig) -> None:
    """Create only the empty-workspace local policy needed for low-risk clarification."""
    policy_file = runtime.workspace_directory.file("approvals/policy.yaml")
    try:
        with same_path_lock(policy_file):
            preimage = policy_file.read_optional_nonblocking(max_bytes=_MAX_PRD_BYTES)
            if preimage:
                return
            policy = MutationPolicy.model_validate(
                {
                    "schema_version": 1,
                    "contributors": [config.local_actor],
                    "approvers": [config.local_actor],
                    "executors": [config.local_actor],
                    "identities": {config.local_actor: [config.local_actor]},
                }
            )
            encoded = yaml.safe_dump(
                policy.model_dump(mode="json"),
                allow_unicode=True,
                sort_keys=True,
            ).encode("utf-8")
            policy_file.atomic_write(encoded, reject_target_races=True)
    finally:
        policy_file.close()


def _onboard_result(
    project: Path,
    prd: str,
    yes: bool,
) -> tuple[bool, OnboardingCommandResult | None, BaseException | None]:
    """Compose existing capture and source-role paths around read-only onboarding state."""
    runtime: Runtime | None = None
    payload: OnboardingCommandResult | None = None
    try:
        try:
            runtime = load_runtime(project)
        except ProjectNotInitialized:
            status = _uninitialized_onboarding_status()
            if not yes:
                return (
                    True,
                    _onboarding_result(
                        status,
                        state=OnboardState.CONFIRMATION_REQUIRED,
                        next_action="intent_onboard_confirm",
                        message="Start guided onboarding now?",
                    ),
                    None,
                )
            initialize_project(project)
            runtime = load_runtime(project)
        status = _inspect_onboarding(runtime)
        if status.state is not OnboardingState.REQUIRED:
            return True, _existing_onboarding_result(runtime, status), None
        if not yes:
            return (
                True,
                _onboarding_result(
                    status,
                    state=OnboardState.CONFIRMATION_REQUIRED,
                    next_action="intent_onboard_confirm",
                    message="Start guided onboarding now?",
                ),
                None,
            )

        # Inspect immediately before each existing operation that can write durable state.
        status = _inspect_onboarding(runtime)
        if status.state is not OnboardingState.REQUIRED:
            return True, _existing_onboarding_result(runtime, status), None
        captured, bootstrap_payload, abort = _bootstrap_result(project, prd)
        if abort is not None:
            return False, None, abort
        if not captured or bootstrap_payload is None:
            return False, None, None

        status = _inspect_onboarding(runtime)
        if status.state is not OnboardingState.REQUIRED:
            return True, _existing_onboarding_result(runtime, status), None
        configured, source_payload, abort = _source_role_result(
            project,
            "markdown",
            prd,
            SourceRole.DECLARED_INTENT,
            False,
        )
        if abort is not None:
            return False, None, abort
        if not configured or source_payload is None:
            return False, None, None
        role_payload = source_payload.get("source_role")
        if type(role_payload) is not dict:
            return False, None, None
        source_role = SourceRoleAssignment.model_validate_json(
            json.dumps(role_payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        )
        evidence_refs = bootstrap_payload.get("evidence_refs")
        if type(evidence_refs) is not list or not all(type(item) is str for item in evidence_refs):
            return False, None, None
        runtime = load_runtime(project)
        refreshed = _inspect_onboarding(runtime)
        if refreshed.state is not OnboardingState.REQUIRED:
            return True, _existing_onboarding_result(runtime, refreshed), None
        config, _config_bytes = _snapshot_config(runtime)
        _provision_local_clarification_policy(runtime, config)
        payload = _post_capture_onboarding_result(
            runtime,
            _inspect_onboarding(runtime),
            source_role,
        )
        return True, payload, None
    except Exception:  # noqa: BLE001 - public CLI receives one fixed failure
        return False, None, None
    except BaseException as error:  # noqa: BLE001 - preserve detached control flow
        error.__traceback__ = None
        error.__cause__ = None
        error.__context__ = None
        return False, None, error
    finally:
        project = Path()
        prd = ""
        runtime = None


def onboard_command(
    prd: str = typer.Option(..., "--prd"),
    project: Path = typer.Option(Path("."), "--project"),
    output_format: OutputFormat = typer.Option(OutputFormat.TEXT, "--format"),
    yes: bool = typer.Option(False, "--yes"),
) -> None:
    """Guide source capture and proposal review without approving a baseline."""
    ok, payload, abort = _onboard_result(project, prd, yes)
    del project, prd, yes
    if abort is not None:
        try:
            raise abort
        finally:
            abort = None
    if not ok or payload is None:
        _fixed_error()
    assert payload is not None
    emit(payload, output_format)
    if payload.state is OnboardState.CONFIRMATION_REQUIRED:
        raise typer.Exit(1)


def proposal_payload(
    runtime: Runtime,
    config: ProjectConfig,
    proposal_id: str,
) -> dict[str, object]:
    """Build one detached ACL-projected proposal preview without raw evidence bodies."""
    if _PROPOSAL_ID.fullmatch(proposal_id) is None:
        raise ValueError("proposal unavailable")
    if runtime.intent_proposals.decision_for(proposal_id) is not None:
        raise ValueError("proposal unavailable")
    proposal = runtime.intent_proposals.get(proposal_id)
    review = _bootstrap_service(runtime, config).review(
        proposal_id,
        _principals(runtime, config),
    )
    if proposal.digest != review.proposal_digest:
        raise ValueError("proposal unavailable")
    return {
        "proposal_id": review.proposal_id,
        "proposal_digest": review.proposal_digest,
        "baseline_graph_version": review.baseline_graph_version,
        "current_graph_version": runtime.graph_store.load().version,
        "candidate_changeset": review.candidate_changeset.model_dump(mode="json"),
        "nodes": [node.model_dump(mode="json") for node in review.all_nodes],
        "edges": [edge.model_dump(mode="json") for edge in review.candidate_edges],
        "core_node_ids": [node.id for node in review.core_nodes],
        "provisional_node_ids": [node.id for node in review.provisional_nodes],
        "assumptions": list(review.assumptions),
        "unanswered_questions": list(review.unanswered_questions),
        "conflicting_authors": list(proposal.conflicting_authors),
        "destructive": proposal.destructive,
        "evidence_refs": list(proposal.evidence_refs),
        "source_roles": [item.model_dump(mode="json") for item in proposal.source_roles],
    }


def _proposal_result(
    project: Path,
    proposal_id: str | None,
) -> tuple[bool, object | None, BaseException | None]:
    runtime: Runtime | None = None
    try:
        runtime = load_runtime(project)
        config, _ = _snapshot_config(runtime)
        if proposal_id is not None:
            return True, proposal_payload(runtime, config, proposal_id), None
        summaries: list[dict[str, object]] = []
        for proposal in runtime.intent_proposals.list()[:_MAX_PROPOSALS]:
            try:
                payload = proposal_payload(runtime, config, proposal.id)
            except Exception:  # noqa: BLE001, S112 - hidden/missing are indistinguishable
                continue
            summaries.append(
                {
                    "proposal_id": payload["proposal_id"],
                    "proposal_digest": payload["proposal_digest"],
                    "baseline_graph_version": payload["baseline_graph_version"],
                    "core_node_ids": payload["core_node_ids"],
                    "provisional_node_ids": payload["provisional_node_ids"],
                }
            )
        return True, summaries, None
    except Exception:  # noqa: BLE001 - hidden and unavailable use one public result
        return False, None, None
    except BaseException as error:  # noqa: BLE001 - preserve detached control flow
        error.__traceback__ = None
        error.__cause__ = None
        error.__context__ = None
        return False, None, error
    finally:
        project = Path()
        proposal_id = ""
        runtime = None


@proposals_app.command("list")
def proposals_list_command(
    project: Path = typer.Option(Path("."), "--project"),
    output_format: OutputFormat = typer.Option(OutputFormat.TEXT, "--format"),
) -> None:
    """List bounded authorized proposal summaries in durable order."""
    ok, payload, abort = _proposal_result(project, None)
    del project
    if abort is not None:
        raise abort
    if not ok or payload is None:
        _fixed_error()
    emit({"proposals": payload}, output_format)


@proposals_app.command("show")
def proposals_show_command(
    proposal_id: str,
    project: Path = typer.Option(Path("."), "--project"),
    output_format: OutputFormat = typer.Option(OutputFormat.TEXT, "--format"),
) -> None:
    """Show one complete authorized proposal preview."""
    ok, payload, abort = _proposal_result(project, proposal_id)
    del project, proposal_id
    if abort is not None:
        raise abort
    if not ok or payload is None:
        _fixed_error()
    emit({"proposal": payload}, output_format)


def _confirmation_result(
    project: Path,
    proposal_id: str,
    proposal_terminal: ProposalTerminal,
) -> tuple[bool, object | None, BaseException | None]:
    runtime: Runtime | None = None
    preview: dict[str, object] | None = None
    try:
        if not proposal_terminal.is_interactive():
            return False, None, None
        runtime = load_runtime(project)
        config, config_bytes = _snapshot_config(runtime)
        preview = proposal_payload(runtime, config, proposal_id)
        proposal_terminal.display_preview(dict(preview))
        digest = cast(str, preview["proposal_digest"])
        confirmation = proposal_terminal.read_confirmation(digest)
        if confirmation != f"confirm {digest}":
            return False, None, None
        if _snapshot_config(runtime) != (config, config_bytes):
            return False, None, None
        current = proposal_payload(runtime, config, proposal_id)
        if current != preview:
            return False, None, None
        graph = _bootstrap_service(runtime, config).activate(
            proposal_id,
            confirmed_node_ids=tuple(cast(list[str], preview["core_node_ids"])),
            actor=config.local_actor,
            at=datetime.now(UTC),
        )
        return (
            True,
            {
                "status": "activated",
                "proposal_id": proposal_id,
                "graph_version": graph.version,
            },
            None,
        )
    except Exception:  # noqa: BLE001 - proposal/config details remain private
        return False, None, None
    except BaseException as error:  # noqa: BLE001 - preserve detached control flow
        error.__traceback__ = None
        error.__cause__ = None
        error.__context__ = None
        return False, None, error
    finally:
        project = Path()
        proposal_id = ""
        preview = None
        runtime = None


@proposals_app.command("confirm")
def proposals_confirm_command(
    proposal_id: str,
    project: Path = typer.Option(Path("."), "--project"),
    output_format: OutputFormat = typer.Option(OutputFormat.TEXT, "--format"),
) -> None:
    """Activate the reviewed core after complete preview and exact TTY confirmation."""
    proposal_terminal = terminal()
    ok, payload, abort = _confirmation_result(project, proposal_id, proposal_terminal)
    del project, proposal_id, proposal_terminal
    if abort is not None:
        raise abort
    if not ok or payload is None:
        _fixed_error(4)
    emit(payload, output_format)


__all__ = [
    "ConsoleProposalTerminal",
    "OnboardState",
    "OnboardingCommandResult",
    "ProposalTerminal",
    "bootstrap_command",
    "onboard_command",
    "proposal_payload",
    "proposals_app",
    "sources_app",
    "terminal",
]
