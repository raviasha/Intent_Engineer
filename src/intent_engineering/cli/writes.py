"""Local exact-preview, independent-approval, and guarded-execution commands."""

# ruff: noqa: B008

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Protocol, cast

import typer
from pydantic import ConfigDict, field_serializer, field_validator, model_validator

from intent_engineering.capture.mcp.connector import McpConnector, _resource_uri
from intent_engineering.capture.mcp.errors import McpError, McpSchemaError
from intent_engineering.capture.mcp.profile_loader import load_strict_yaml_mapping_bytes
from intent_engineering.capture.mcp.profile_models import ReadOperation
from intent_engineering.capture.mcp.runtime import McpRuntime
from intent_engineering.capture.mcp.selectors import bind_arguments, select_value
from intent_engineering.capture.mcp.session import thaw_json
from intent_engineering.cli.connectors import (
    ConfiguredConnector,
    ConnectorCatalog,
    load_connector_catalog,
)
from intent_engineering.cli.output import OutputFormat, emit, normalize
from intent_engineering.cli.runtime import Runtime
from intent_engineering.core.models import JsonValue, ResolutionAction
from intent_engineering.core.models._base import StrictModel
from intent_engineering.mutations.approval import approve_plan
from intent_engineering.mutations.committer import LocalWriteCommitter
from intent_engineering.mutations.executor import ExternalMutationGateway, WriteExecutor
from intent_engineering.mutations.models import (
    ApprovalRecord,
    RemoteObject,
    WritePlan,
    WriteResult,
    provider_binding_hash,
)
from intent_engineering.mutations.planner import build_write_plan
from intent_engineering.storage.jsonl.approval_store import (
    JsonlApprovalStore,
    JsonlWritePlanStore,
)
from intent_engineering.storage.jsonl.receipt_store import JsonlReceiptStore
from intent_engineering.storage.jsonl.strict import loads_strict_object

write_app = typer.Typer(help="Preview, approve, and execute guarded external writes.")


class MutationPolicy(StrictModel):
    """Local role and person-alias registry for independent write authorization."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    contributors: frozenset[str]
    approvers: frozenset[str]
    executors: frozenset[str]
    identities: dict[str, frozenset[str]]

    @field_validator("contributors", "approvers", "executors", mode="before")
    @classmethod
    def require_role_lists(cls, values: object) -> frozenset[str]:
        if type(values) is not list or any(type(value) is not str for value in values):
            raise ValueError("invalid mutation policy role")
        roles = cast(list[str], values)
        if len(roles) != len(set(roles)):
            raise ValueError("invalid mutation policy role")
        return frozenset(roles)

    @field_validator("contributors", "approvers", "executors")
    @classmethod
    def validate_roles(cls, values: frozenset[str]) -> frozenset[str]:
        if not values or any(type(value) is not str or not value.strip() for value in values):
            raise ValueError("invalid mutation policy role")
        return frozenset(values)

    @field_validator("identities", mode="before")
    @classmethod
    def require_identity_lists(cls, values: object) -> dict[str, frozenset[str]]:
        if type(values) is not dict:
            raise ValueError("invalid mutation identity registry")
        converted: dict[str, frozenset[str]] = {}
        for actor, aliases in cast(dict[object, object], values).items():
            if (
                type(actor) is not str
                or type(aliases) is not list
                or any(type(alias) is not str for alias in aliases)
            ):
                raise ValueError("invalid mutation identity registry")
            identity_values = cast(list[str], aliases)
            if len(identity_values) != len(set(identity_values)):
                raise ValueError("invalid mutation identity registry")
            converted[actor] = frozenset(identity_values)
        return converted

    @field_validator("identities")
    @classmethod
    def freeze_identities(cls, values: dict[str, frozenset[str]]) -> dict[str, frozenset[str]]:
        if not values:
            raise ValueError("invalid mutation identity registry")
        copied: dict[str, frozenset[str]] = {}
        for actor, aliases in values.items():
            if (
                type(actor) is not str
                or not actor.strip()
                or not aliases
                or actor not in aliases
                or any(type(alias) is not str or not alias.strip() for alias in aliases)
            ):
                raise ValueError("invalid mutation identity registry")
            copied[actor] = frozenset(aliases)
        return cast(dict[str, frozenset[str]], MappingProxyType(copied))

    @field_serializer("identities")
    def serialize_identities(self, values: dict[str, frozenset[str]]) -> dict[str, list[str]]:
        return {actor: sorted(aliases) for actor, aliases in values.items()}

    @model_validator(mode="after")
    def validate_known_actors(self) -> MutationPolicy:
        known = set(self.identities)
        if not (set(self.contributors) | set(self.approvers) | set(self.executors)).issubset(known):
            raise ValueError("mutation policy references unknown actor")
        return self


class Terminal(Protocol):
    """Minimal injectable terminal boundary for exact local confirmation."""

    def is_interactive(self) -> bool: ...

    def display_preview(self, preview: dict[str, object]) -> None: ...

    def read_confirmation(self, plan_id: str) -> str: ...


def _preview_payload(plan: WritePlan) -> dict[str, object]:
    payload = cast(dict[str, object], normalize(plan))
    return {**payload, "plan_hash": plan.canonical_hash}


@dataclass(frozen=True)
class ConsoleTerminal:
    """Production terminal that refuses piped or unattended approval."""

    def is_interactive(self) -> bool:
        return sys.stdin.isatty()

    def display_preview(self, preview: dict[str, object]) -> None:
        typer.echo(
            json.dumps(preview, ensure_ascii=False, indent=2, sort_keys=True),
            err=True,
        )

    def read_confirmation(self, plan_id: str) -> str:
        return cast(str, typer.prompt(f"Type 'approve {plan_id}' to approve"))


def terminal() -> Terminal:
    return ConsoleTerminal()


def _required_text(value: object) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError("invalid provider write object")
    return value


class McpMutationGateway(ExternalMutationGateway):
    """Profile-driven provider gateway used by preview and the Task 6 executor."""

    def __init__(
        self,
        runtime: McpRuntime,
        configured: ConfiguredConnector,
        *,
        local_actor: str,
        object_type: str,
        semantic_operation: str,
        connector_id: str | None = None,
    ) -> None:
        if object_type not in configured.profile.objects:
            raise ValueError("external write object is unavailable")
        if (
            semantic_operation not in configured.profile.writes
            or configured.profile.writes[semantic_operation].target_object != object_type
        ):
            raise ValueError("external write operation is unavailable")
        self._runtime = runtime
        self._configured = configured
        self._local_actor = local_actor
        self._object_type = object_type
        self._semantic_operation = semantic_operation
        self._connector_id = connector_id

    async def _read(self, operation: ReadOperation, context: dict[str, JsonValue]) -> JsonValue:
        config = self._configured.config
        arguments = bind_arguments(operation.arguments, context)
        if operation.kind == "tool":
            return await self._runtime.call(
                config.server,
                config.binding.tools[operation.semantic_name],
                arguments,
            )
        uri = _resource_uri(config.binding.resources[operation.semantic_name], arguments)
        return await self._runtime.read_resource(config.server, uri)

    async def _fetch_result(
        self, object_id: str, version: str | None
    ) -> tuple[RemoteObject | None, BaseException | None]:
        try:
            profile = self._configured.profile
            object_profile = profile.objects[self._object_type]
            operation = profile.operations[object_profile.fetch_operation]
            response = await self._read(
                operation,
                {
                    "object_id": object_id,
                    "object_version": version,
                    "scope": {
                        key: thaw_json(value)
                        for key, value in self._configured.config.scope.items()
                    },
                    "fields": {},
                },
            )
            payload = select_value(response, operation.item_selector)
            selected_id = _required_text(select_value(payload, object_profile.external_id))
            selected_version = _required_text(
                select_value(payload, object_profile.external_version)
            )
            write_profile = profile.writes[self._semantic_operation]
            guarded_id = _required_text(select_value(payload, write_profile.target_id))
            guarded_version = _required_text(select_value(payload, write_profile.before_version))
            if (
                selected_id != object_id
                or guarded_id != selected_id
                or guarded_version != selected_version
            ):
                return None, None
            content: dict[str, JsonValue] = {
                field: select_value(payload, selector)
                for field, selector in object_profile.content.items()
            }
            connector_id = (
                self._connector_id
                or McpConnector(
                    self._runtime,
                    config=self._configured.config,
                    profile=profile,
                    object_name=self._object_type,
                    local_actor=self._local_actor,
                ).connector_id
            )
            return (
                RemoteObject(
                    connector_id=connector_id,
                    profile_id=profile.id,
                    profile_version=profile.version,
                    object_type=self._object_type,
                    id=selected_id,
                    version=selected_version,
                    content=content,
                ),
                None,
            )
        except McpError as error:
            error.__traceback__ = None
            error.__cause__ = None
            error.__context__ = None
            return None, error
        except Exception:  # noqa: BLE001 - provider values never cross this result boundary
            return None, None
        except BaseException as error:  # noqa: BLE001 - preserve detached control flow
            error.__traceback__ = None
            error.__cause__ = None
            error.__context__ = None
            return None, error

    async def fetch_object(self, object_id: str, version: str | None = None) -> RemoteObject:
        """Fetch and normalize the exact writable object through declared selectors."""
        result, error = await self._fetch_result(object_id, version)
        del object_id, version
        if error is not None:
            raise error
        if result is None:
            raise McpSchemaError() from None
        return result

    async def fetch_target(self, plan: WritePlan) -> RemoteObject:
        """Refetch the approved target for exact Task 6 pre/post-write verification."""
        if (
            plan.profile_id != self._configured.profile.id
            or plan.profile_version != self._configured.profile.version
            or plan.object_type != self._object_type
        ):
            raise ValueError("external write target unavailable")
        return await self.fetch_object(plan.target_id)

    async def _execute_result(
        self,
        operation: str,
        arguments: dict[str, JsonValue],
    ) -> tuple[WriteResult | None, BaseException | None]:
        try:
            profile = self._configured.profile
            expected_operation = self._configured.config.binding.tools[self._semantic_operation]
            if operation != expected_operation:
                return None, None
            result = await self._runtime.call(
                self._configured.config.server,
                operation,
                arguments,
            )
            resulting_version = _required_text(
                select_value(
                    result,
                    profile.writes[self._semantic_operation].result_version,
                )
            )
            return (
                WriteResult(
                    resulting_version=resulting_version,
                    redacted_result={"status": "verified"},
                ),
                None,
            )
        except McpError as error:
            error.__traceback__ = None
            error.__cause__ = None
            error.__context__ = None
            return None, error
        except Exception:  # noqa: BLE001 - provider values never cross this result boundary
            return None, None
        except BaseException as error:  # noqa: BLE001 - preserve detached control flow
            error.__traceback__ = None
            error.__cause__ = None
            error.__context__ = None
            return None, error

    async def execute(
        self,
        operation: str,
        arguments: dict[str, JsonValue],
    ) -> WriteResult:
        """Call one exactly bound write capability and retain only its selected version."""
        result, error = await self._execute_result(operation, arguments)
        arguments = {}
        del operation, arguments
        if error is not None:
            raise error
        if result is None:
            raise McpSchemaError() from None
        return result


class WriteWorkflow:
    """CLI composition over the reviewed immutable plan and approval services."""

    def __init__(
        self,
        catalog: ConnectorCatalog,
        plans: JsonlWritePlanStore,
        approvals: JsonlApprovalStore,
        policy: MutationPolicy,
    ) -> None:
        self.catalog = catalog
        self.plans = plans
        self.approvals = approvals
        self.policy = policy

    def preview(self, plan_id: str, **_kwargs: object) -> WritePlan:
        """Strictly reload the complete hash-bound preview."""
        return WritePlan.model_validate_json(self.plans.get(plan_id).model_dump_json())

    def _configuration_for(self, plan: WritePlan) -> ConfiguredConnector:
        matches = tuple(
            item
            for item in self.catalog.configured
            if item.profile.id == plan.profile_id
            and item.profile.version == plan.profile_version
            and provider_binding_hash(item.config.binding) == plan.binding_hash
            and plan.object_type in item.profile.objects
            and McpConnector(
                self.catalog.mcp_runtime,
                config=item.config,
                profile=item.profile,
                object_name=plan.object_type,
                local_actor=plan.created_by,
            ).connector_id
            == plan.connector_id
        )
        if len(matches) != 1:
            raise ValueError("external write configuration unavailable")
        return matches[0]

    def _identity_aliases(self) -> dict[str, frozenset[str]]:
        return {actor: frozenset(values) for actor, values in self.policy.identities.items()}

    def approve(
        self,
        plan_id: str,
        approval_terminal: Terminal,
        *,
        now: datetime | None = None,
    ) -> ApprovalRecord:
        """Create one independently authenticated exact interactive approval."""
        plan = self.preview(plan_id)
        selected = self._configuration_for(plan)
        if not approval_terminal.is_interactive():
            raise ValueError("external write approval rejected")
        confirmation = approval_terminal.read_confirmation(plan.id)
        approval = approve_plan(
            plan,
            selected.config.binding,
            actor=self.catalog.runtime.config.local_actor,
            now=datetime.now(UTC) if now is None else now,
            expires_in=timedelta(minutes=15),
            confirmation=confirmation,
            interactive=True,
            authorized_approvers=self.policy.approvers,
            identity_aliases=self._identity_aliases(),
        )
        self.approvals.put(approval)
        return approval

    def _case_is_authorized(self, evidence_refs: tuple[str, ...], actor: str) -> bool:
        aliases = self.policy.identities.get(actor, frozenset())
        records = {record.id: record for record in self.catalog.runtime.evidence()}
        return bool(aliases) and all(
            reference in records
            and (not records[reference].acl or not aliases.isdisjoint(records[reference].acl))
            for reference in evidence_refs
        )

    async def create_preview(
        self,
        case_id: str,
        *,
        connector_id: str,
        operation: str,
        requested_fields: dict[str, JsonValue],
        resolution_action: ResolutionAction,
        now: datetime | None = None,
    ) -> WritePlan:
        """Fetch current provider state, build Task 5's exact plan, and persist it."""
        selected = self.catalog._selected(connector_id)
        if operation not in selected.profile.writes:
            raise ValueError("external write operation unavailable")
        actor = self.catalog.runtime.config.local_actor
        case = self.catalog.runtime.case_store.get(case_id)
        if not self._case_is_authorized(case.all_evidence_refs, actor):
            raise ValueError("external write case unavailable")
        operation_profile = selected.profile.writes[operation]
        prefix = f"{selected.profile.id}:"
        references = (case.subject_ref, *case.affected_refs)
        targets = tuple(
            reference.removeprefix(prefix)
            for reference in references
            if reference.startswith(prefix)
        )
        if len(set(targets)) != 1:
            raise ValueError("external write target unavailable")
        gateway = McpMutationGateway(
            self.catalog.mcp_runtime,
            selected,
            local_actor=actor,
            object_type=operation_profile.target_object,
            semantic_operation=operation,
        )
        current = await gateway.fetch_object(targets[0])
        plan = build_write_plan(
            case,
            selected.profile,
            selected.config.binding,
            operation,
            current,
            requested_fields,
            actor=actor,
            authorized_contributors=self.policy.contributors,
            identity_aliases=self._identity_aliases(),
            resolution_action=resolution_action,
            now=datetime.now(UTC) if now is None else now,
        )
        self.plans.put(plan)
        return plan

    async def execute(
        self,
        plan_id: str,
        approval_id: str,
        *,
        now: datetime | None = None,
    ) -> object:
        """Assemble and invoke the reviewed Task 6 executor without implying approval."""
        plan = self.preview(plan_id)
        selected = self._configuration_for(plan)
        runtime = self.catalog.runtime
        receipts_file = runtime.transactions.target_file("receipts")
        try:
            receipts = JsonlReceiptStore(receipts_file, transactions=runtime.transactions)
            gateway = McpMutationGateway(
                self.catalog.mcp_runtime,
                selected,
                local_actor=runtime.config.local_actor,
                object_type=plan.object_type,
                semantic_operation=plan.operation,
                connector_id=plan.connector_id,
            )
            committer = LocalWriteCommitter(
                runtime.transactions,
                evidence_acl=tuple(sorted(self.policy.identities)),
                profile=selected.profile,
                binding=selected.config.binding,
                authorized_contributors=self.policy.contributors,
                authorized_approvers=self.policy.approvers,
                authorized_executors=self.policy.executors,
                identity_aliases=self._identity_aliases(),
            )
            executor = WriteExecutor(
                plans=self.plans,
                approvals=self.approvals,
                receipts=receipts,
                profile=selected.profile,
                binding=selected.config.binding,
                gateway=gateway,
                success_committer=committer,
                authorized_contributors=self.policy.contributors,
                authorized_approvers=self.policy.approvers,
                authorized_executors=self.policy.executors,
                identity_aliases=self._identity_aliases(),
            )
            return await executor.execute(
                plan.id,
                approval_id,
                actor=runtime.config.local_actor,
                now=datetime.now(UTC) if now is None else now,
            )
        finally:
            receipts_file.close()


def _policy_result(policy_file: object) -> MutationPolicy | None:
    """Parse the local policy without exposing its aliases on public tracebacks."""
    try:
        return MutationPolicy.model_validate(
            load_strict_yaml_mapping_bytes(policy_file.read_bytes_nonblocking())  # type: ignore[attr-defined]
        )
    except Exception:  # noqa: BLE001 - caller receives only an inert invalid result
        return None


def policy_actor_aliases(runtime: Runtime) -> frozenset[str]:
    """Return the local policy aliases authenticated for the selected runtime actor."""
    directory = None
    try:
        directory = runtime.workspace_directory.subdirectory("approvals")
        policy_file = directory.file("policy.yaml")
        try:
            policy = _policy_result(policy_file)
        finally:
            policy_file.close()
    except Exception:  # noqa: BLE001 - malformed or absent policy grants no aliases
        policy = None
    finally:
        if directory is not None:
            directory.close()
    if policy is None:
        return frozenset()
    return policy.identities.get(runtime.config.local_actor, frozenset())


def load_write_workflow(_project: Path) -> WriteWorkflow:
    catalog = load_connector_catalog(_project)
    approvals_directory = catalog.runtime.workspace_directory.subdirectory("approvals")
    try:
        plans = JsonlWritePlanStore(approvals_directory.file("plans.jsonl"))
        approvals = JsonlApprovalStore(approvals_directory.file("approvals.jsonl"))
        policy_file = approvals_directory.file("policy.yaml")
        try:
            policy = _policy_result(policy_file)
        finally:
            policy_file.close()
    finally:
        approvals_directory.close()
    if policy is None:
        raise ValueError("external write policy unavailable") from None
    return WriteWorkflow(catalog, plans, approvals, policy)


def _write_error(exit_code: int = 1) -> None:
    typer.echo("intent error: external write operation failed", err=True)
    raise typer.Exit(exit_code)


def _preview_result(
    project: Path,
    reference: str,
    connector_id: str | None,
    operation: str | None,
    fields: str | None,
    action: ResolutionAction,
) -> tuple[bool, object, BaseException | None]:
    """Build or reload a preview without putting its private fields on a public traceback."""
    try:
        workflow = load_write_workflow(project)
        if reference.startswith("write-plan:"):
            return True, _preview_payload(workflow.preview(reference)), None
        if connector_id is None or operation is None or fields is None:
            return False, None, None
        requested = loads_strict_object(fields)

        async def run() -> WritePlan:
            return await workflow.create_preview(
                reference,
                connector_id=connector_id,
                operation=operation,
                requested_fields=cast(dict[str, JsonValue], requested),
                resolution_action=action,
            )

        import anyio

        return True, _preview_payload(anyio.run(run)), None
    except Exception:  # noqa: BLE001 - caller receives only an inert failure bit
        return False, None, None
    except BaseException as error:  # noqa: BLE001 - preserve detached cancellation/interrupt
        error.__traceback__ = None
        error.__cause__ = None
        error.__context__ = None
        return False, None, error


def _approval_result(
    project: Path,
    plan_id: str,
    approval_terminal: Terminal,
) -> tuple[bool, object, BaseException | None]:
    """Prompt and approve behind a context-free result boundary."""
    try:
        workflow = load_write_workflow(project)
        preview = workflow.preview(plan_id)
        approval_terminal.display_preview(_preview_payload(preview))
        approval = workflow.approve(plan_id, approval_terminal)
        payload = cast(dict[str, object], normalize(approval))
        return True, {**payload, "preview": _preview_payload(preview)}, None
    except Exception:  # noqa: BLE001 - caller receives only an inert failure bit
        return False, None, None
    except BaseException as error:  # noqa: BLE001 - preserve detached cancellation/interrupt
        error.__traceback__ = None
        error.__cause__ = None
        error.__context__ = None
        return False, None, error


def _execution_result(
    project: Path, plan_id: str, approval_id: str
) -> tuple[bool, object, BaseException | None]:
    """Execute behind a non-raising boundary while preserving cancellation and interrupts."""
    try:
        workflow = load_write_workflow(project)

        async def run() -> object:
            return await workflow.execute(plan_id, approval_id)

        import anyio

        return True, anyio.run(run), None
    except Exception:  # noqa: BLE001 - caller receives only an inert failure bit
        return False, None, None
    except BaseException as error:  # noqa: BLE001 - preserve detached cancellation/interrupt
        error.__traceback__ = None
        error.__cause__ = None
        error.__context__ = None
        return False, None, error


@write_app.command("preview")
def preview_write(
    reference: str,
    connector_id: str | None = typer.Option(None, "--connector-id"),
    operation: str | None = typer.Option(None, "--operation"),
    fields: str | None = typer.Option(None, "--fields"),
    action: ResolutionAction = typer.Option(ResolutionAction.UPDATE_REQUIREMENT, "--action"),
    project: Path = typer.Option(Path("."), "--project"),
    output_format: OutputFormat = typer.Option(OutputFormat.TEXT, "--format"),
) -> None:
    """Render a stored plan or create one exact preview for a review case."""
    ok, result, abort = _preview_result(project, reference, connector_id, operation, fields, action)
    del project, reference, connector_id, operation, fields, action
    if abort is not None:
        raise abort
    if not ok:
        del result
        _write_error()
    emit(result, output_format)


@write_app.command("approve")
def approve_write(
    plan_id: str,
    project: Path = typer.Option(Path("."), "--project"),
    output_format: OutputFormat = typer.Option(OutputFormat.TEXT, "--format"),
) -> None:
    """Create one independent approval after exact interactive confirmation."""
    approval_terminal = terminal()
    if not approval_terminal.is_interactive():
        _write_error(4)
    ok, payload, abort = _approval_result(project, plan_id, approval_terminal)
    del project, plan_id, approval_terminal
    if abort is not None:
        raise abort
    if not ok:
        del payload
        _write_error(4)
    emit(payload, output_format)


@write_app.command("execute")
def execute_write(
    plan_id: str,
    approval_id: str = typer.Option(..., "--approval-id"),
    project: Path = typer.Option(Path("."), "--project"),
    output_format: OutputFormat = typer.Option(OutputFormat.TEXT, "--format"),
) -> None:
    """Execute only a separately persisted, still-valid approval."""
    ok, payload, abort = _execution_result(project, plan_id, approval_id)
    del project, plan_id, approval_id
    if abort is not None:
        raise abort
    if not ok:
        del payload
        _write_error()
    emit(payload, output_format)


__all__ = ["ConsoleTerminal", "Terminal", "WriteWorkflow", "load_write_workflow", "write_app"]
