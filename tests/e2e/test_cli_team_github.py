"""Black-box contracts for ``intent team enable github``."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from typer.testing import CliRunner

from intent_engineering.cli.app import app
from intent_engineering.cli.team import (
    GitHubEnablementServices,
    GitHubEnablementWorkflow,
    GitHubEnableResult,
    VerifiedProtectionAuthorization,
    build_github_enable_preview,
    run_confirmed_github_enablement,
)
from intent_engineering.control_plane.models import HumanDecisionPayload
from intent_engineering.control_plane.webauthn_service import VerifiedHumanDecision
from intent_engineering.core.policy import initialize_project
from intent_engineering.team_state.github import (
    GitHubProtectionPreview,
    GitHubTeamStateStatus,
    PublicationPullRequest,
)
from intent_engineering.team_state.models import PreparedPublication, RecipientRecord
from intent_engineering.team_state.publication import PublicationPreview


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    initialize_project(root)
    return root


def test_team_enable_github_is_a_no_network_preview_before_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches credentials, network, enrollment, or publication running before review."""
    project = _project(tmp_path)
    called = False

    async def forbidden(*_: object, **__: object) -> GitHubEnableResult:
        nonlocal called
        called = True
        raise AssertionError("network must not run during preview")

    monkeypatch.setattr("intent_engineering.cli.team.run_confirmed_github_enablement", forbidden)
    result = CliRunner().invoke(
        app,
        ["team", "enable", "github", "--project", str(project), "--format", "json"],
        env={"GITHUB_REPOSITORY": "acme/project"},
    )

    assert result.exit_code == 4, repr(result.exception)
    payload = json.loads(result.stdout)
    assert payload["state"] == "confirmation_required"
    assert payload["repository_id"] == "github.com/acme/project"
    assert payload["codeowners_path"] == ".github/CODEOWNERS"
    assert payload["workflow_path"] == ".github/workflows/intent-state.yml"
    assert payload["codeowners_suggestion"] == (
        "/.intent/ @acme\n/.github/workflows/intent-state.yml @acme\n"
    )
    assert "intent check --shared-state" in payload["workflow_suggestion"]
    assert payload["preview_digest"].startswith("sha256:")
    assert called is False


def test_team_enable_github_requires_exact_preview_and_second_protection_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches stale preview acceptance or branch protection changing after one consent."""
    project = _project(tmp_path)
    runner = CliRunner()
    env = {"GITHUB_REPOSITORY": "acme/project"}
    preview = runner.invoke(
        app,
        ["team", "enable", "github", "--project", str(project), "--format", "json"],
        env=env,
    )
    preview_digest = json.loads(preview.stdout)["preview_digest"]
    calls: list[tuple[str, str | None]] = []

    async def enable(
        *_: object,
        preview_confirmation: str,
        protection_confirmation: str | None,
        **__: object,
    ) -> GitHubEnableResult:
        calls.append((preview_confirmation, protection_confirmation))
        if protection_confirmation is None:
            return GitHubEnableResult(
                state="protection_confirmation_required",
                repository_id="github.com/acme/project",
                preview_digest=preview_digest,
                protection_digest="sha256:" + "2" * 64,
            )
        return GitHubEnableResult(
            state="published",
            repository_id="github.com/acme/project",
            preview_digest=preview_digest,
            protection_digest=protection_confirmation,
            pull_request_url="https://github.com/acme/project/pull/7",
        )

    monkeypatch.setattr("intent_engineering.cli.team.run_confirmed_github_enablement", enable)
    protection = runner.invoke(
        app,
        [
            "team",
            "enable",
            "github",
            "--project",
            str(project),
            "--confirm-preview",
            preview_digest,
            "--format",
            "json",
        ],
        env=env,
    )

    assert protection.exit_code == 4, repr(protection.exception)
    protection_payload = json.loads(protection.stdout)
    assert protection_payload["state"] == "protection_confirmation_required"
    protection_digest = protection_payload["protection_digest"]

    published = runner.invoke(
        app,
        [
            "team",
            "enable",
            "github",
            "--project",
            str(project),
            "--confirm-preview",
            preview_digest,
            "--confirm-protection",
            protection_digest,
            "--format",
            "json",
        ],
        env=env,
    )

    assert published.exit_code == 0, repr(published.exception)
    assert json.loads(published.stdout)["state"] == "published"
    assert calls == [(preview_digest, None), (preview_digest, protection_digest)]


def test_confirmed_cli_routes_to_control_plane_for_platform_webauthn(tmp_path: Path) -> None:
    """Catches the default CLI replacing required platform WebAuthn with a fixed failure."""
    project = _project(tmp_path)
    runner = CliRunner()
    env = {"GITHUB_REPOSITORY": "acme/project"}
    preview = runner.invoke(
        app,
        ["team", "enable", "github", "--project", str(project), "--format", "json"],
        env=env,
    )
    preview_digest = json.loads(preview.stdout)["preview_digest"]

    result = runner.invoke(
        app,
        [
            "team",
            "enable",
            "github",
            "--project",
            str(project),
            "--confirm-preview",
            preview_digest,
            "--format",
            "json",
        ],
        env=env,
    )

    assert result.exit_code == 4, repr(result.exception)
    payload = json.loads(result.stdout)
    assert payload["state"] == "webauthn_confirmation_required"
    assert payload["control_plane_path"] == "/team-state"


@pytest.mark.anyio
async def test_confirmed_enablement_runs_real_reviewed_orchestration_without_side_effects_early(
    tmp_path: Path,
) -> None:
    """Catches enrollment/publication before protection consent or bypass of prepared output."""
    project = _project(tmp_path)
    preview = build_github_enable_preview(project, "acme/project")
    publication_decision = cast(VerifiedHumanDecision, object())
    publication_payload = cast(HumanDecisionPayload, object())
    publication_preview = cast(PublicationPreview, SimpleNamespace(payload=publication_payload))
    prepared = cast(PreparedPublication, object())
    events: list[str] = []
    protection_digest = "sha256:" + "2" * 64
    verified_protection_digest = "sha256:" + "0" * 64

    class GitHub:
        async def inspect(self, repository: str) -> GitHubTeamStateStatus:
            assert repository == "acme/project"
            events.append("inspect")
            return GitHubTeamStateStatus(
                repository_id="github.com/acme/project",
                repository_node_id="77",
                account_id="123",
                login="alice",
                scopes=("repo",),
                private=True,
                default_branch="main",
                branch_commit="a" * 40,
                branch_present=True,
                protection_compatible=False,
                codeowners_present=True,
            )

        def protection_preview(self) -> GitHubProtectionPreview:
            events.append("protection_preview")
            return GitHubProtectionPreview(
                repository_id="github.com/acme/project",
                branch_creation_required=False,
                requires_change=True,
                digest=protection_digest,
            )

        async def configure_protection(self, confirmation: str) -> GitHubTeamStateStatus:
            assert confirmation == protection_digest
            events.append("configure_protection")
            return GitHubTeamStateStatus(
                repository_id="github.com/acme/project",
                repository_node_id="77",
                account_id="123",
                login="alice",
                scopes=("repo",),
                private=True,
                default_branch="main",
                branch_commit="a" * 40,
                branch_present=True,
                protection_compatible=True,
                codeowners_present=True,
            )

        async def open_publication_pr(
            self, publication: PreparedPublication
        ) -> PublicationPullRequest:
            assert publication is prepared
            events.append("open_pr")
            return PublicationPullRequest(
                repository_id="github.com/acme/project",
                number=7,
                url="https://github.com/acme/project/pull/7",
                created=True,
            )

        async def aclose(self) -> None:
            events.append("close")

    class Publication:
        def bind_publication_base_commit(self, commit: str) -> None:
            assert commit == "a" * 40
            events.append("bind_publication_base")

        def preview(self, *, now: datetime) -> PublicationPreview:
            assert now == datetime(2026, 9, 8, 12, tzinfo=UTC)
            events.append("publication_preview")
            return publication_preview

        def prepare(self, verified: VerifiedHumanDecision, *, now: datetime) -> PreparedPublication:
            assert verified is publication_decision
            events.append("publication_prepare_push")
            return prepared

    async def verify_protection(_: object) -> VerifiedProtectionAuthorization:
        events.append("protection_webauthn_verify")
        return VerifiedProtectionAuthorization(
            project_id=preview.project_id,
            repository_id=preview.repository_id,
            actor=preview.actor,
            preview_digest=verified_protection_digest,
            result_digest=verified_protection_digest,
            credential_id="credential:platform",
            verified_at=datetime(2026, 9, 8, 12, tzinfo=UTC),
            expires_at=datetime(2026, 9, 8, 12, 5, tzinfo=UTC),
        )

    async def enroll(reviewed: object, status: GitHubTeamStateStatus) -> RecipientRecord:
        assert reviewed == preview
        assert status.account_id == "123"
        assert status.login == "alice"
        events.append("enroll")
        public_key = X25519PrivateKey.generate().public_key().public_bytes_raw()
        encode = lambda value: base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")
        return RecipientRecord(
            key_id="recipient:alice",
            project_id=preview.project_id,
            repository_id=preview.repository_id,
            actor=preview.actor,
            github_account_id="123",
            github_login="alice",
            public_key=encode(public_key),
            webauthn_credential_id=encode(b"credential"),
            webauthn_credential_public_key=encode(b"public-credential-key"),
            enrolled_at=datetime(2026, 9, 8, 12, tzinfo=UTC),
        )

    async def verify_publication(payload: HumanDecisionPayload) -> VerifiedHumanDecision:
        assert payload is publication_payload
        events.append("publication_webauthn_verify")
        return publication_decision

    def stage_code_suggestions(reviewed: object) -> None:
        assert reviewed == preview
        events.append("stage_code_suggestions")

    workflow = GitHubEnablementWorkflow(
        preview,
        GitHubEnablementServices(
            github=GitHub(),  # type: ignore[arg-type]
            publication=Publication(),  # type: ignore[arg-type]
            verify_protection=verify_protection,
            enroll=enroll,
            stage_code_suggestions=stage_code_suggestions,
            verify_publication=verify_publication,
            clock=lambda: datetime(2026, 9, 8, 12, tzinfo=UTC),
        ),
    )
    pending = await run_confirmed_github_enablement(
        project,
        "acme/project",
        preview_confirmation=preview.preview_digest,
        protection_confirmation=None,
        workflow=workflow,
    )

    assert pending.state == "protection_confirmation_required"
    assert events == ["inspect", "protection_preview", "close"]

    with pytest.raises(ValueError, match="branch-protection authority changed"):
        await run_confirmed_github_enablement(
            project,
            "acme/project",
            preview_confirmation=preview.preview_digest,
            protection_confirmation=protection_digest,
            workflow=workflow,
        )
    assert "configure_protection" not in events
    verified_protection_digest = protection_digest

    published = await run_confirmed_github_enablement(
        project,
        "acme/project",
        preview_confirmation=preview.preview_digest,
        protection_confirmation=protection_digest,
        workflow=workflow,
    )

    assert published.state == "published"
    assert published.pull_request_url == "https://github.com/acme/project/pull/7"
    assert events == [
        "inspect",
        "protection_preview",
        "close",
        "inspect",
        "protection_preview",
        "protection_webauthn_verify",
        "close",
        "inspect",
        "protection_preview",
        "protection_webauthn_verify",
        "configure_protection",
        "bind_publication_base",
        "enroll",
        "stage_code_suggestions",
        "publication_preview",
        "publication_webauthn_verify",
        "publication_prepare_push",
        "open_pr",
        "close",
    ]
