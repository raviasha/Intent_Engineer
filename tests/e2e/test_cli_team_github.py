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
    GitHubProtectionPolicy,
    GitHubProtectionPreview,
    GitHubRequiredStatusCheck,
    GitHubTeamStateStatus,
    PublicationPullRequest,
)
from intent_engineering.team_state.models import PreparedPublication, RecipientRecord
from intent_engineering.team_state.publication import PublicationPreview
from tests.helpers.shared_state import git


def _project(tmp_path: Path, *, github_aliases: tuple[str, ...] = ("github:alice",)) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    initialize_project(root)
    aliases = ["local", *github_aliases]
    (root / ".intent/approvals/policy.yaml").write_text(
        "schema_version: 1\n"
        "contributors: [local]\n"
        "approvers: [local]\n"
        "executors: [local]\n"
        "identities:\n"
        f"  local: {json.dumps(aliases)}\n",
        encoding="utf-8",
    )
    return root


@pytest.mark.parametrize(
    "remote",
    [
        "git@github.com:acme/project.git",
        "https://github.com/acme/project.git",
        "ssh://git@github.com/acme/project.git",
    ],
)
def test_cli_discovers_origin_before_environment(tmp_path, remote):
    project = _project(tmp_path)
    git(project, "init", "--initial-branch=main")
    git(project, "add", ".intent/config.yaml", "-f")
    git(project, "commit", "-m", "Code")
    git(project, "remote", "add", "origin", remote)
    result = CliRunner().invoke(
        app,
        ["team", "enable", "github", "--project", str(project), "--format", "json"],
        env={"GITHUB_REPOSITORY": "wrong/project"},
    )
    assert result.exit_code == 4, result.exception
    assert json.loads(result.stdout)["repository_id"] == "github.com/acme/project"


def test_cli_explicit_repository_and_fixed_configuration_failure(tmp_path):
    project = _project(tmp_path)
    result = CliRunner().invoke(
        app,
        [
            "team",
            "enable",
            "github",
            "--project",
            str(project),
            "--repository",
            "acme/project",
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 4, result.exception
    assert json.loads(result.stdout)["repository_id"] == "github.com/acme/project"
    failed = CliRunner().invoke(
        app,
        [
            "team",
            "enable",
            "github",
            "--project",
            str(project),
            "--repository",
            "https://secret-token@evil.test/acme/project",
        ],
    )
    assert failed.exit_code == 1
    assert "configuration unavailable" in failed.output
    assert "secret-token" not in failed.output
    assert isinstance(failed.exception, SystemExit)


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
    assert payload["code_owner"] == "@alice"
    assert payload["codeowners_path"] == ".github/CODEOWNERS"
    assert payload["workflow_path"] == ".github/workflows/intent-state.yml"
    assert payload["codeowners_suggestion"] == (
        "/.intent/ @alice\n"
        "/.github/workflows/ @alice\n"
        "/ci/launch.py @alice\n"
        "/src/intent_engineering/ @alice\n"
    )
    import yaml

    workflow = yaml.safe_load(payload["workflow_suggestion"])
    assert workflow["on"]["pull_request_target"]["branches"] == ["intent-state"]
    state_job = workflow["jobs"]["state"]
    assert state_job["runs-on"] == {
        "group": "intent-state",
        "labels": ["self-hosted", "intent-state"],
    }
    assert "INTENT_CI_SHARED_STATE_TRUST" not in state_job["steps"][-1]["env"]
    assert state_job["name"] == "Intent Engineering / state"
    assert state_job["steps"][0]["with"]["ref"] == "${{ github.workflow_sha }}"
    assert state_job["steps"][0]["with"]["persist-credentials"] is False
    assert state_job["steps"][-1]["run"] == "python -I .intent-trusted/ci/launch.py validate-state"
    assert payload["preview_digest"].startswith("sha256:")
    assert called is False


def test_setup_imports_public_ci_descriptor_and_refuses_missing_ci(tmp_path: Path) -> None:
    """Production setup cannot publish a bundle that the required state check cannot read."""
    from intent_engineering.team_state.models import CiRecipientRecord

    project = _project(tmp_path)
    runner = CliRunner()
    args = [
        "team",
        "enable",
        "github",
        "--project",
        str(project),
        "--repository",
        "acme/project",
        "--format",
        "json",
    ]
    preview = json.loads(runner.invoke(app, args).stdout)
    missing = runner.invoke(app, [*args, "--confirm-preview", preview["preview_digest"]])
    assert missing.exit_code == 1
    assert "--ci-recipient" in missing.output
    assert not (project / ".intent/team-setup.json").exists()
    recipient = CiRecipientRecord(
        project_id="project",
        repository_id="github.com/acme/project",
        runner_id="release-01",
        public_key=base64.urlsafe_b64encode(
            X25519PrivateKey.generate().public_key().public_bytes_raw()
        )
        .rstrip(b"=")
        .decode(),
    )
    descriptor = tmp_path / "ci.json"
    descriptor.write_text(recipient.model_dump_json())
    imported = runner.invoke(app, [*args, "--ci-recipient", str(descriptor)])
    assert imported.exit_code == 4, imported.output
    reviewed = json.loads(imported.stdout)
    assert reviewed["ci_recipient"]["key_id"] == recipient.key_id
    assert reviewed["preview_digest"] != preview["preview_digest"]
    changed = recipient.model_copy(update={"runner_id": "release-02", "key_id": ""})
    descriptor.write_text(changed.model_dump_json())
    stale = runner.invoke(
        app,
        [*args, "--ci-recipient", str(descriptor), "--confirm-preview", reviewed["preview_digest"]],
    )
    assert stale.exit_code == 1
    assert not (project / ".intent/team-setup.json").exists()


@pytest.mark.parametrize(
    "aliases",
    [(), ("github:alice", "github:bob"), ("github:acme/team",)],
)
def test_team_enable_requires_one_exact_github_user_code_owner(
    tmp_path: Path, aliases: tuple[str, ...]
) -> None:
    """Catches a repository owner, ambiguous alias, or team-shaped guess entering CODEOWNERS."""
    project = _project(tmp_path, github_aliases=aliases)

    result = CliRunner().invoke(
        app,
        ["team", "enable", "github", "--project", str(project)],
        env={"GITHUB_REPOSITORY": "acme/project"},
    )

    assert result.exit_code == 1
    assert "GitHub code owner unavailable" in result.output
    assert "github:<login> alias" in result.output
    assert "@acme" not in result.output


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


def test_confirmed_cli_routes_to_control_plane_for_platform_webauthn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches the default CLI replacing required platform WebAuthn with a fixed failure."""
    monkeypatch.setattr("intent_engineering.cli.dev.dev_command", lambda **_: None)
    project = _project(tmp_path)
    from intent_engineering.team_state.ci import CiKeyStore
    from tests.unit.team_state.test_ci_recipient import Backend

    ci_recipient = CiKeyStore(
        "project",
        "github.com/acme/project",
        "release-01",
        backend=Backend(),
        lock_root=tmp_path / "locks",
    ).provision()
    descriptor = tmp_path / "ci.json"
    descriptor.write_text(ci_recipient.model_dump_json())
    runner = CliRunner()
    env = {"GITHUB_REPOSITORY": "acme/project"}
    preview = runner.invoke(
        app,
        [
            "team",
            "enable",
            "github",
            "--project",
            str(project),
            "--ci-recipient",
            str(descriptor),
            "--format",
            "json",
        ],
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
            "--ci-recipient",
            str(descriptor),
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
    assert payload["control_plane_path"] == "/"


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
    configured_policy = GitHubProtectionPolicy(
        snapshot_digest="sha256:" + "3" * 64,
        enforce_admins=True,
        allow_deletions=False,
        allow_force_pushes=False,
        required_linear_history=True,
        dismiss_stale_reviews=True,
        require_code_owner_reviews=False,
        required_approving_review_count=1,
        bypass_pull_request_allowances_empty=True,
        required_status_checks_strict=True,
        required_status_check_contexts=("Intent Engineering / state",),
        required_status_checks=(
            GitHubRequiredStatusCheck(context="Intent Engineering / state", app_id=15368),
        ),
        restrictions_digest=None,
    )

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
                default_branch_commit="b" * 40,
                branch_commit=None,
                branch_present=False,
                protection_compatible=False,
                codeowners_present=True,
            )

        def protection_preview(self) -> GitHubProtectionPreview:
            events.append("protection_preview")
            return GitHubProtectionPreview(
                repository_id="github.com/acme/project",
                branch_creation_required=True,
                requires_change=True,
                before_policy=None,
                after_policy=configured_policy,
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
                protection_policy=configured_policy,
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
