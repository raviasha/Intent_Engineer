"""Production setup factories exercised with offline external transports only."""

from __future__ import annotations

import json

import anyio
import pytest

from intent_engineering.cli.team import build_github_enable_preview, run_confirmed_github_enablement
from intent_engineering.control_plane.models import HumanDecisionPayload
from intent_engineering.control_plane.service import ControlPlaneService
from intent_engineering.control_plane.webauthn_service import VerifiedAuthentication
from intent_engineering.team_state.github import GitHubJsonResponse
from tests.e2e.test_team_enrollment import _TeamVerifier
from tests.helpers.shared_state import git
from tests.integration.control_plane.test_service import (
    NOW,
    ORIGIN,
    _harness,
    _registration_response,
)


def test_confirmed_cli_transfers_exact_nonsecret_setup_into_production_service(
    tmp_path, monkeypatch
) -> None:
    """Catches the production command returning a route without a runnable setup request."""
    harness = _harness(tmp_path, aliases=("github:alice",))
    monkeypatch.setenv("GH_TOKEN", "never-persist-this-credential")
    git(harness.project, "init", "--initial-branch=main")
    git(harness.project, "add", "docs/prd.md")
    git(harness.project, "commit", "-m", "Code")
    from intent_engineering.team_state.ci import CiKeyStore

    backend = _Keyring()
    ci_recipient = CiKeyStore(
        harness.runtime.config.project_id,
        "github.com/acme/project",
        "release-01",
        backend=backend,
        lock_root=tmp_path / "ci-locks",
    ).provision()
    preview = build_github_enable_preview(
        harness.project, "acme/project", ci_recipient=ci_recipient
    )

    async def confirm():
        return await run_confirmed_github_enablement(
            harness.project,
            "acme/project",
            preview_confirmation=preview.preview_digest,
            protection_confirmation=None,
            ci_recipient=ci_recipient,
        )

    try:
        result = anyio.run(confirm)
        request_file = harness.project / ".intent/team-setup.json"
        assert request_file.is_file(), (
            "confirmed setup must reach the repository-bound UI lifecycle"
        )
        request = json.loads(request_file.read_bytes())
        assert request["preview"]["preview_digest"] == preview.preview_digest
        assert request["preview"]["repository_id"] == "github.com/acme/project"
        assert "never-persist-this-credential" not in request_file.read_text()
        assert result.control_plane_path == "/"
        service = ControlPlaneService(harness.runtime, origin=ORIGIN, clock=lambda: NOW)
        try:
            assert service.github_setup_status()["repository_id"] == "github.com/acme/project"
            anyio.run(service.github_setup_action, "cancel")
            assert service.github_setup_status()["state"] == "unconfigured"
            assert not request_file.exists()
        finally:
            service.close()
    finally:
        harness.service.close()
        harness.runtime.close()


def test_stale_cli_setup_confirmation_never_persists_handoff(tmp_path) -> None:
    harness = _harness(tmp_path, aliases=("github:alice",))

    async def confirm():
        await run_confirmed_github_enablement(
            harness.project,
            "acme/project",
            preview_confirmation="sha256:" + "0" * 64,
            protection_confirmation=None,
        )

    try:
        with pytest.raises(ValueError, match="preview changed"):
            anyio.run(confirm)
        assert not (harness.project / ".intent/team-setup.json").exists()
    finally:
        harness.service.close()
        harness.runtime.close()


def test_default_cli_confirmation_launches_the_trusted_local_ui(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from intent_engineering.cli.app import app

    harness = _harness(tmp_path, aliases=("github:alice",))
    launches = []
    monkeypatch.setattr(
        "intent_engineering.cli.dev.dev_command", lambda **kwargs: launches.append(kwargs)
    )
    try:
        from intent_engineering.team_state.ci import CiKeyStore

        ci_recipient = CiKeyStore(
            "project",
            "github.com/acme/project",
            "release-01",
            backend=_Keyring(),
            lock_root=tmp_path / "ci-locks",
        ).provision()
        descriptor = tmp_path / "ci.json"
        descriptor.write_text(ci_recipient.model_dump_json())
        preview = build_github_enable_preview(
            harness.project, "acme/project", ci_recipient=ci_recipient
        )
        result = CliRunner().invoke(
            app,
            [
                "team",
                "enable",
                "github",
                "--project",
                str(harness.project),
                "--ci-recipient",
                str(descriptor),
                "--confirm-preview",
                preview.preview_digest,
                "--format",
                "json",
            ],
            env={"GITHUB_REPOSITORY": "acme/project"},
        )
        assert result.exit_code == 4, result.exception
        assert launches and launches[0]["project"] == harness.project
        assert launches[0]["no_open"] is False
    finally:
        harness.service.close()
        harness.runtime.close()


def test_cli_preview_binds_code_file_preimages_before_confirmation(tmp_path):
    harness = _harness(tmp_path, aliases=("github:alice",))
    git(harness.project, "init", "--initial-branch=main")
    git(harness.project, "add", "docs/prd.md")
    git(harness.project, "commit", "-m", "Code")
    try:
        first = build_github_enable_preview(harness.project, "acme/project")
        (harness.project / ".github").mkdir()
        (harness.project / ".github/CODEOWNERS").write_text("* @somebody-else\n")
        changed = build_github_enable_preview(harness.project, "acme/project")
        assert changed.preview_digest != first.preview_digest
    finally:
        harness.service.close()
        harness.runtime.close()


def test_setup_api_routes_validate_exact_requests_and_use_production_factory(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    from intent_engineering.control_plane.web import build_control_plane_app
    from intent_engineering.team_state import setup
    from tests.e2e.test_team_enrollment import CSRF, _headers

    harness = _harness(tmp_path, aliases=("github:alice",))
    preview = build_github_enable_preview(harness.project, "acme/project")
    setup.save_setup_request(harness.runtime, preview)
    transport = _GitHubTransport()
    monkeypatch.setattr(setup, "github_api", lambda: transport)
    try:
        with TestClient(
            build_control_plane_app(harness.service, origin=ORIGIN, csrf_secret=CSRF),
            base_url=ORIGIN,
        ) as browser:
            status = browser.get("/api/v1/team/setup")
            assert status.status_code == 200
            assert status.json()["repository_id"] == "github.com/acme/project"
            inspected = browser.post("/api/v1/team/setup/inspect", headers=_headers(), json={})
            assert inspected.status_code == 200
            assert inspected.json()["github_account_id"] == "123"
            assert (
                browser.post(
                    "/api/v1/team/setup/inspect", headers=_headers(), json={"approve": True}
                ).status_code
                == 400
            )
            assert (
                browser.post(
                    "/api/v1/team/setup/verify",
                    headers=_headers(),
                    json={"payload": {}, "response": {}},
                ).status_code
                == 400
            )
            assert browser.post("/api/v1/team/setup/verify", json={}).status_code == 403
            assert transport.writes == []
    finally:
        harness.service.close()
        harness.runtime.close()


@pytest.mark.anyio
@pytest.mark.parametrize("cancel", [False, True])
async def test_setup_failure_closes_provider_and_scrubs_exception_frames(
    tmp_path, monkeypatch, cancel
):
    from intent_engineering.team_state import setup

    harness = _harness(tmp_path, aliases=("github:alice",))
    setup.save_setup_request(
        harness.runtime, build_github_enable_preview(harness.project, "acme/project")
    )

    class Stop(BaseException):
        pass

    class FailedTransport(_GitHubTransport):
        async def request_json_object(self, *args, **kwargs):
            private_marker = "PRIVATE-GITHUB-TOKEN"
            if cancel:
                raise Stop()
            raise RuntimeError(private_marker)

    transport = FailedTransport()
    monkeypatch.setattr(setup, "github_api", lambda: transport)
    try:
        with pytest.raises(Stop if cancel else ValueError) as caught:
            await harness.service.github_setup_action("inspect")
        assert "PRIVATE-GITHUB-TOKEN" not in str(caught.value)
        frames = []
        trace = caught.value.__traceback__
        while trace:
            frames.append(trace.tb_frame.f_code.co_name)
            trace = trace.tb_next
        assert "request_json_object" not in frames
        assert transport.closed == 1
    finally:
        harness.service.close()
        harness.runtime.close()


class _GitHubTransport:
    def __init__(self):
        self.branch = None
        self.protected = False
        self.closed = 0
        self.writes = []
        self.reads = []
        self.blobs = []
        self.tree = []
        self.commit = None
        self.publication_ref = None
        self.pr = None
        self.after_tree = None
        self.after_publication_tree = None
        self.lose_pr_response = False
        self.tooling = None
        self.lose_bootstrap_response = False
        self.default_protected = True
        self.default_commit = "a" * 40
        self.merged_commit = "8" * 40
        self.merged_parent = "c" * 40

    def promote_tooling(self, preview):
        self.tooling = {
            "4" * 40: preview.codeowners_suggestion.encode(),
            "5" * 40: preview.workflow_suggestion.encode(),
            "6" * 40: preview.check_workflow_suggestion.encode(),
        }

    def workflow_parameters(self, *, state=True):
        return {
            "do_not_enforce_on_create": state,
            "workflows": [
                {
                    "path": ".github/workflows/intent-state.yml"
                    if state
                    else ".github/workflows/intent-check.yml",
                    "ref": "refs/heads/main",
                    "repository_id": 77,
                    "sha": self.default_commit,
                }
            ],
        }

    async def request_json_object(
        self, method, path, *, payload=None, params=None, allowed_statuses=frozenset({200})
    ):
        code = 200
        if method != "GET":
            self.writes.append((method, path, payload))
        else:
            self.reads.append(path)
        if path == "/user":
            data = {"id": 123, "login": "alice"}
        elif path == "/repos/acme/project":
            data = {
                "id": 77,
                "full_name": "acme/project",
                "private": True,
                "default_branch": "main",
                "permissions": {
                    "admin": True,
                    "maintain": True,
                    "pull": True,
                    "push": True,
                    "triage": True,
                },
            }
        elif path.endswith("/branches/main"):
            data = {
                "name": "main",
                "protected": self.default_protected,
                "commit": {"sha": self.default_commit},
            }
        elif path.endswith("/branches/main/protection"):
            data = {
                "required_status_checks": {
                    "strict": True,
                    "contexts": ["Intent Engineering / check"],
                    "checks": [{"context": "Intent Engineering / check", "app_id": 15368}],
                },
                "enforce_admins": {"enabled": True},
                "allow_deletions": {"enabled": False},
                "allow_force_pushes": {"enabled": False},
                "required_pull_request_reviews": {
                    "required_approving_review_count": 1,
                    "require_code_owner_reviews": True,
                    "dismiss_stale_reviews": True,
                    "bypass_pull_request_allowances": {"users": [], "teams": [], "apps": []},
                },
            }
        elif path == "/orgs/acme/actions/runner-groups":
            assert params == {
                "page": "1",
                "per_page": "100",
                "visible_to_repository": "acme/project",
            }
            data = {
                "total_count": 1,
                "runner_groups": [
                    {
                        "id": 42,
                        "name": "intent-state",
                        "visibility": "selected",
                        "default": False,
                        "restricted_to_workflows": True,
                        "selected_workflows": [
                            "acme/project/.github/workflows/intent-check.yml@refs/heads/main",
                            "acme/project/.github/workflows/intent-state.yml@refs/heads/main",
                        ],
                    }
                ],
            }
        elif path in {"/repos/acme/project/rulesets/91", "/repos/acme/project/rulesets/92"}:
            state = path.endswith("91")
            data = {
                "id": 91 if state else 92,
                "target": "branch",
                "enforcement": "active",
                "bypass_actors": [],
                "conditions": {
                    "ref_name": {
                        "include": ["refs/heads/intent-state" if state else "refs/heads/main"],
                        "exclude": [],
                    }
                },
                "rules": [
                    {"type": "workflows", "parameters": self.workflow_parameters(state=state)}
                ],
            }
        elif path.endswith("/branches/intent-state"):
            code = 404 if self.branch is None else 200
            data = (
                {}
                if self.branch is None
                else {
                    "name": "intent-state",
                    "commit": {"sha": self.branch},
                    "protected": self.protected,
                }
            )
        elif path.endswith("/protection"):
            if method == "PUT":
                self.protected = True
            code = 200 if self.protected else 404
            data = (
                {
                    "enforce_admins": {"enabled": True},
                    "required_status_checks": {
                        "strict": True,
                        "contexts": ["Intent Engineering / state"],
                        "checks": [{"context": "Intent Engineering / state", "app_id": 15368}],
                    },
                    "required_pull_request_reviews": {
                        "dismiss_stale_reviews": True,
                        "require_code_owner_reviews": False,
                        "required_approving_review_count": 1,
                    },
                    "restrictions": None,
                    "allow_force_pushes": {"enabled": False},
                    "allow_deletions": {"enabled": False},
                    "required_linear_history": {"enabled": True},
                }
                if self.protected
                else {}
            )
        elif path.endswith("/contents/.github/CODEOWNERS"):
            code, data = 404, {}
        elif path.endswith("/git/blobs"):
            import hashlib

            self.blobs.append(payload)
            code, data = 201, {"sha": hashlib.sha1(payload["content"].encode()).hexdigest()}
        elif "/git/blobs/" in path:
            import base64
            import hashlib

            sha = path.rsplit("/", 1)[-1]
            blob = next(
                item
                for item in self.blobs
                if hashlib.sha1(item["content"].encode()).hexdigest() == sha
            )
            data = {
                "sha": sha,
                "encoding": "base64",
                "size": len(base64.b64decode(blob["content"])),
                "content": blob["content"],
            }
        elif path.endswith("/git/trees"):
            if payload["tree"]:
                self.tree = payload["tree"]
                code, data = (
                    201,
                    {
                        "sha": "e" * 40,
                        "truncated": False,
                        "tree": [
                            self.tree[0],
                            {"path": "bundles", "mode": "040000", "type": "tree", "sha": "6" * 40},
                            {
                                "path": "signatures",
                                "mode": "040000",
                                "type": "tree",
                                "sha": "7" * 40,
                            },
                        ],
                    },
                )
                if self.after_publication_tree:
                    self.after_publication_tree()
            else:
                code, data = 201, {"sha": "b" * 40}
                if self.after_tree:
                    self.after_tree()
        elif "/git/trees/" in path:
            if path.endswith("1" * 40):
                return GitHubJsonResponse(
                    200,
                    {
                        "sha": "1" * 40,
                        "truncated": False,
                        "tree": []
                        if self.tooling is None
                        else [
                            {"path": ".github", "mode": "040000", "type": "tree", "sha": "2" * 40}
                        ],
                    },
                    {"X-OAuth-Scopes": "repo, admin:org"},
                )
            if path.endswith("2" * 40):
                return GitHubJsonResponse(
                    200,
                    {
                        "sha": "2" * 40,
                        "truncated": False,
                        "tree": [
                            {
                                "path": "CODEOWNERS",
                                "mode": "100644",
                                "type": "blob",
                                "sha": "4" * 40,
                            },
                            {
                                "path": "workflows",
                                "mode": "040000",
                                "type": "tree",
                                "sha": "3" * 40,
                            },
                        ],
                    },
                    {"X-OAuth-Scopes": "repo, admin:org"},
                )
            if path.endswith("3" * 40):
                return GitHubJsonResponse(
                    200,
                    {
                        "sha": "3" * 40,
                        "truncated": False,
                        "tree": [
                            {
                                "path": "intent-state.yml",
                                "mode": "100644",
                                "type": "blob",
                                "sha": "5" * 40,
                            },
                            {
                                "path": "intent-check.yml",
                                "mode": "100644",
                                "type": "blob",
                                "sha": "6" * 40,
                            },
                        ],
                    },
                    {"X-OAuth-Scopes": "repo, admin:org"},
                )
            data = (
                {
                    "sha": "e" * 40,
                    "tree": [
                        *self.tree,
                        {"path": "bundles", "mode": "040000", "type": "tree", "sha": "6" * 40},
                        {"path": "signatures", "mode": "040000", "type": "tree", "sha": "7" * 40},
                    ],
                    "truncated": False,
                }
                if path.endswith("e" * 40)
                else {"sha": "b" * 40, "tree": [], "truncated": False}
            )
        elif path.endswith("/git/commits"):
            if payload["parents"]:
                self.commit = payload
                code, data = (
                    201,
                    {
                        "sha": "f" * 40,
                        "tree": {"sha": payload["tree"]},
                        "parents": [{"sha": item} for item in payload["parents"]],
                    },
                )
            else:
                code, data = 201, {"sha": "c" * 40, "tree": {"sha": "b" * 40}, "parents": []}
        elif "/git/commits/" in path:
            if path.endswith(self.default_commit):
                return GitHubJsonResponse(
                    200,
                    {"sha": self.default_commit, "tree": {"sha": "1" * 40}, "parents": []},
                    {"X-OAuth-Scopes": "repo, admin:org"},
                )
            data = (
                {
                    "sha": self.merged_commit,
                    "tree": {"sha": "e" * 40},
                    "parents": [{"sha": self.merged_parent}],
                }
                if path.endswith(self.merged_commit)
                else {"sha": "c" * 40, "tree": {"sha": "b" * 40}, "parents": []}
            )
        elif path.endswith("/git/refs"):
            if payload["ref"] == "refs/heads/intent-state":
                self.branch = payload["sha"]
                if self.lose_bootstrap_response:
                    self.lose_bootstrap_response = False
                    raise RuntimeError("bootstrap response lost")
            else:
                if self.publication_ref is not None:
                    return GitHubJsonResponse(422, {}, {"X-OAuth-Scopes": "repo, admin:org"})
                self.publication_ref = payload
            code, data = (
                201,
                {"ref": payload["ref"], "object": {"sha": payload["sha"], "type": "commit"}},
            )
        elif "/git/ref/heads/intent-publication/" in path:
            data = {
                "ref": self.publication_ref["ref"],
                "object": {"sha": self.publication_ref["sha"], "type": "commit"},
            }
        elif path.endswith("/pulls/1"):
            data = {
                **self.pr,
                "merged": self.branch == self.merged_commit,
                "state": "closed" if self.branch == self.merged_commit else self.pr["state"],
                "merge_commit_sha": self.branch,
            }
        elif path.endswith("/pulls"):
            self.pr = {
                "number": 1,
                "state": "open",
                "merged": False,
                "html_url": "https://github.com/acme/project/pull/1",
                "head": {
                    "ref": self.publication_ref["ref"].removeprefix("refs/heads/"),
                    "sha": self.publication_ref["sha"],
                    "repo": {"full_name": "acme/project"},
                },
                "base": {
                    "ref": "intent-state",
                    "sha": self.branch,
                    "repo": {"full_name": "acme/project"},
                },
            }
            code, data = 201, self.pr
            if self.lose_pr_response:
                self.lose_pr_response = False
                raise RuntimeError("GitHub response lost")
        else:
            raise AssertionError((method, path))
        assert code in allowed_statuses
        return GitHubJsonResponse(code, data, {"X-OAuth-Scopes": "repo, admin:org"})

    async def aclose(self):
        self.closed += 1

    async def get_pages(self, path, params, etag=None):
        from intent_engineering.capture.github.models import PageResult

        if path == "/orgs/acme/actions/runner-groups/42/runners":
            return PageResult(
                items=(
                    {
                        "id": 91,
                        "name": "release-01",
                        "labels": [{"name": "self-hosted"}, {"name": "intent-state"}],
                    },
                ),
                etag=None,
            )
        if path in {
            "/repos/acme/project/rules/branches/intent-state",
            "/repos/acme/project/rules/branches/main",
        }:
            state = path.endswith("intent-state")
            return PageResult(
                items=(
                    {
                        "type": "workflows",
                        "ruleset_source_type": "Repository",
                        "ruleset_source": "acme/project",
                        "ruleset_id": 91 if state else 92,
                        "parameters": self.workflow_parameters(state=state),
                    },
                ),
                etag=None,
            )
        assert path == "/repos/acme/project/pulls"
        return PageResult(items=() if self.pr is None else (self.pr,), etag=None)

    async def request_bytes(self, method, path, *, max_bytes, accept):
        assert method == "GET" and accept == "application/vnd.github.raw+json"
        assert self.tooling is not None
        import base64
        import hashlib

        sha = path.rsplit("/", 1)[-1]
        content = self.tooling.get(sha)
        if content is None:
            blob = next(
                item
                for item in self.blobs
                if hashlib.sha1(item["content"].encode()).hexdigest() == sha
            )
            content = base64.b64decode(blob["content"])
        assert len(content) <= max_bytes
        return content


class _Keyring:
    def __init__(self):
        self.values = {}

    def get_password(self, service, account):
        return self.values.get((service, account))

    def set_password(self, service, account, value):
        self.values[service, account] = value

    def delete_password(self, service, account):
        self.values.pop((service, account), None)


class _SetupVerifier(_TeamVerifier):
    next_sign_count = 0

    def verify_authentication(self, response, request):
        assert response == b"signed-assertion"
        assert request == self.authentication_requests[-1]
        return VerifiedAuthentication(
            credential_id=b"team-control-plane-credential",
            new_sign_count=self.next_sign_count,
            user_verified=True,
        )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "failure",
    [
        "default-unprotected",
        "file-conflict",
        "check-workflow-conflict",
        "anchor-race",
        "before-suggestions",
        "before-signing",
    ],
)
async def test_protection_rejects_conflicts_and_branch_changes_at_write_boundaries(
    tmp_path, monkeypatch, failure
):
    import keyring

    from intent_engineering.team_state import setup

    harness = _harness(tmp_path, aliases=("github:alice",))
    git(harness.project, "init", "--initial-branch=main")
    git(harness.project, "add", "docs/prd.md")
    git(harness.project, "commit", "-m", "Code")
    transport, backend, verifier = _GitHubTransport(), _Keyring(), _SetupVerifier()
    from intent_engineering.team_state.ci import CiKeyStore

    ci_recipient = CiKeyStore(
        "project",
        "github.com/acme/project",
        "release-01",
        backend=backend,
        lock_root=tmp_path / "ci-locks",
    ).provision()
    setup_preview = build_github_enable_preview(
        harness.project, "acme/project", ci_recipient=ci_recipient
    )
    setup.save_setup_request(harness.runtime, setup_preview)
    monkeypatch.setattr(setup, "github_api", lambda: transport)
    monkeypatch.setattr(keyring, "get_password", backend.get_password)
    monkeypatch.setattr(keyring, "set_password", backend.set_password)
    service = ControlPlaneService(
        harness.runtime, origin=ORIGIN, clock=lambda: NOW, webauthn_verifier=verifier
    )
    try:
        await service.github_setup_action("enroll")
        service.complete_team_enrollment(_registration_response(verifier.registration_requests[-1]))
        if failure == "default-unprotected":
            transport.default_protected = False
            prerequisite = await service.github_setup_action("protection-preview")
            assert prerequisite["state"] == "default_branch_prerequisite"
            assert "guidance" in prerequisite and "payload" not in prerequisite
            assert transport.writes == []
            assert not (harness.project / ".github").exists()
            assert not any(
                name.startswith("intent-engineering-signing/") for name, _ in backend.values
            )
        elif failure in {"file-conflict", "check-workflow-conflict"}:
            target = harness.project / (
                ".github/CODEOWNERS"
                if failure == "file-conflict"
                else ".github/workflows/intent-check.yml"
            )
            target.parent.mkdir(parents=True)
            target.write_text("* @someone-else\n")
            with pytest.raises(ValueError):
                await service.github_setup_action("protection-preview")
            assert transport.writes == []
        else:
            preview = await service.github_setup_action("protection-preview")
            assert preview["preview"]["phase"] == "code_changes"
            if failure != "before-suggestions":
                staging_payload = HumanDecisionPayload.model_validate_json(
                    json.dumps(preview["payload"])
                )
                await service.github_setup_action("options", payload=staging_payload)
                staged = await service.github_setup_action(
                    "verify", payload=staging_payload, response=b"signed-assertion"
                )
                assert staged["state"] == "code_changes_staged"
                transport.promote_tooling(setup_preview)
                preview = await service.github_setup_action("protection-preview")
                assert preview["preview"]["phase"] == "protection"
            payload = HumanDecisionPayload.model_validate_json(json.dumps(preview["payload"]))
            await service.github_setup_action("options", payload=payload)
            if failure == "anchor-race":
                transport.after_tree = lambda: setattr(transport, "branch", "9" * 40)
            else:
                bridge = service._github_setup_bridge
                original = bridge._anchor

                async def changed_anchor(api, status):
                    await original(api, status)
                    if failure == "before-suggestions" or transport.protected:
                        path = harness.project / ".intent/config.yaml"
                        path.write_bytes(path.read_bytes() + b"\n# authority changed\n")

                monkeypatch.setattr(bridge, "_anchor", changed_anchor)
            with pytest.raises(ValueError):
                await service.github_setup_action(
                    "verify", payload=payload, response=b"signed-assertion"
                )
            if failure == "anchor-race":
                assert transport.branch == "9" * 40
                assert not any(path.endswith("/git/commits") for _, path, _ in transport.writes)
            else:
                assert not any(
                    name.startswith("intent-engineering-signing/") for name, _ in backend.values
                )
                if failure == "before-suggestions":
                    assert not (harness.project / ".github/CODEOWNERS").exists()
    finally:
        service.close()
        harness.service.close()
        harness.runtime.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "lost_pr_response", [False, True, "cancel", "bootstrap", "legacy-draft", "legacy-receipt"]
)
async def test_production_ui_requires_exact_enrolled_protection_decision(
    tmp_path, monkeypatch, lost_pr_response
):
    """Catches the production factory omitting identity/enrollment or accepting stale/replayed writes."""
    import httpx
    import keyring

    from intent_engineering.cli.runtime import load_runtime
    from intent_engineering.core.models import ChangeSet
    from tests.helpers.shared_state import ready_project

    harness = _harness(tmp_path, aliases=("github:alice",))
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    ready_project(baseline)
    baseline_runtime = load_runtime(baseline)
    try:
        for evidence in baseline_runtime.evidence():
            harness.runtime.evidence_store.associate("markdown", evidence)
        changeset = ChangeSet.model_validate_json(
            (baseline / ".intent/history/changesets.jsonl").read_bytes()
        )
        harness.runtime.graph_store.apply(changeset)
    finally:
        baseline_runtime.close()
    git(harness.project, "init", "--initial-branch=main")
    git(harness.project, "remote", "add", "origin", "https://github.com/acme/project.git")
    git(harness.project, "add", "docs/prd.md")
    git(harness.project, "commit", "-m", "Code")
    from intent_engineering.team_state.ci import CiKeyStore

    backend = _Keyring()
    ci_recipient = CiKeyStore(
        harness.runtime.config.project_id,
        "github.com/acme/project",
        "release-01",
        backend=backend,
        lock_root=tmp_path / "ci-locks",
    ).provision()
    preview = build_github_enable_preview(
        harness.project, "acme/project", ci_recipient=ci_recipient
    )
    await run_confirmed_github_enablement(
        harness.project,
        "acme/project",
        preview_confirmation=preview.preview_digest,
        protection_confirmation=None,
        ci_recipient=ci_recipient,
    )
    transport = _GitHubTransport()

    class OfflineHttpTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            assert request.headers["Authorization"] == "Bearer offline-github-credential"
            if (
                request.method == "GET"
                and request.headers.get("accept") == "application/vnd.github.raw+json"
            ):
                content = await transport.request_bytes(
                    "GET",
                    request.url.path,
                    max_bytes=32 * 1024 * 1024,
                    accept=request.headers["accept"],
                )
                return httpx.Response(200, content=content)
            if request.method == "GET" and (
                request.url.path.endswith("/pulls")
                or request.url.path.endswith("/runners")
                or "/rules/branches/" in request.url.path
            ):
                page = await transport.get_pages(request.url.path, dict(request.url.params))
                return httpx.Response(
                    200,
                    json=page.model_dump(mode="json")["items"],
                    headers={"X-OAuth-Scopes": "repo, admin:org"},
                )
            result = await transport.request_json_object(
                request.method,
                request.url.path,
                params=dict(request.url.params),
                payload=json.loads(request.content) if request.content else None,
                allowed_statuses=frozenset({200, 201, 404, 422}),
            )
            return httpx.Response(
                result.status_code, json=dict(result.payload), headers=dict(result.headers)
            )

        async def aclose(self):
            await transport.aclose()

    original_client = httpx.AsyncClient
    monkeypatch.setenv("GH_TOKEN", "offline-github-credential")
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original_client(**{**kwargs, "transport": OfflineHttpTransport()}),
    )
    monkeypatch.setattr(keyring, "get_password", backend.get_password)
    monkeypatch.setattr(keyring, "set_password", backend.set_password)
    monkeypatch.setattr(keyring, "delete_password", backend.delete_password)
    verifier = _SetupVerifier()
    service = ControlPlaneService(
        harness.runtime, origin=ORIGIN, clock=lambda: NOW, webauthn_verifier=verifier
    )
    try:
        status = await service.github_setup_action("inspect")
        assert status["github_account_id"] == "123"
        assert status["github_login"] == "alice"
        assert transport.writes == []
        await service.github_setup_action("enroll")
        service.complete_team_enrollment(_registration_response(verifier.registration_requests[-1]))
        protection = await service.github_setup_action("protection-preview")
        payload = HumanDecisionPayload.model_validate_json(json.dumps(protection["payload"]))
        assert payload.action.value == "approve_external_write"
        assert payload.graph_version == 1
        wrong = payload.model_copy(update={"repository_id": "repo:sha256:" + "0" * 64})
        with pytest.raises(ValueError):
            await service.github_setup_action("options", payload=wrong)
        assert transport.writes == []
        await service.github_setup_action("options", payload=payload)
        result = await service.github_setup_action(
            "verify", payload=payload, response=b"signed-assertion"
        )
        assert result["state"] == "code_changes_staged"
        assert transport.writes == []
        assert not any(name.startswith("intent-engineering-signing/") for name, _ in backend.values)
        awaiting_merge = await service.github_setup_action("protection-preview")
        assert awaiting_merge["state"] == "code_changes_staged" and "payload" not in awaiting_merge
        transport.promote_tooling(preview)
        service.close()
        service = ControlPlaneService(
            harness.runtime, origin=ORIGIN, clock=lambda: NOW, webauthn_verifier=verifier
        )
        assert service.github_setup_status()["state"] == "code_changes_staged"
        protection = await service.github_setup_action("protection-preview")
        assert protection["preview"]["phase"] == "protection"
        assert protection["preview"]["tooling"]["commit"] == "a" * 40
        payload = HumanDecisionPayload.model_validate_json(json.dumps(protection["payload"]))
        await service.github_setup_action("options", payload=payload)
        if lost_pr_response == "bootstrap":
            transport.lose_bootstrap_response = True
            with pytest.raises(ValueError):
                await service.github_setup_action(
                    "verify", payload=payload, response=b"signed-assertion"
                )
            assert (harness.project / ".intent/team-bootstrap.json").exists()
            assert transport.branch == "c" * 40 and not transport.protected
            with pytest.raises(ValueError):
                await service.github_setup_action("cancel")
            assert (harness.project / ".intent/team-bootstrap.json").exists()
            service.close()
            service = ControlPlaneService(
                harness.runtime, origin=ORIGIN, clock=lambda: NOW, webauthn_verifier=verifier
            )
            recovery = await service.github_setup_action("protection-preview")
            payload = HumanDecisionPayload.model_validate_json(json.dumps(recovery["payload"]))
            await service.github_setup_action("options", payload=payload)
        result = await service.github_setup_action(
            "verify", payload=payload, response=b"signed-assertion"
        )
        assert result["state"] == "protection_configured"
        assert not (harness.project / ".intent/team-bootstrap.json").exists()
        assert (
            len(
                [
                    item
                    for item in transport.writes
                    if item[0] == "POST"
                    and item[1].endswith("/git/refs")
                    and item[2]["ref"] == "refs/heads/intent-state"
                ]
            )
            == 1
        )
        assert transport.branch == "c" * 40
        assert transport.protected
        assert any(name.startswith("intent-engineering-signing/") for name, _ in backend.values)
        assert (harness.project / ".github/CODEOWNERS").is_file()
        with pytest.raises(ValueError):
            await service.github_setup_action(
                "verify", payload=payload, response=b"signed-assertion"
            )
        assert transport.closed >= 4
        publication = await service.github_setup_action("publication-preview")
        assert ci_recipient.key_id in publication["preview"]["recipient_key_ids"]
        from intent_engineering.team_state.ci import CiTrustConfig, CiTrustProvider

        public_ci_trust = CiTrustConfig.model_validate_json(
            json.dumps(publication["preview"]["ci_trust"])
        )
        protected = tmp_path / "protected-ci"
        protected.mkdir(mode=0o700)
        public_ci_path = protected / "trust.json"
        public_ci_path.write_bytes(public_ci_trust.canonical_bytes())
        public_ci_path.chmod(0o600)
        ci_trust = CiTrustProvider(
            public_ci_path, backend=backend, lock_root=tmp_path / "ci-locks"
        ).load()
        publication_payload = HumanDecisionPayload.model_validate_json(
            json.dumps(publication["payload"])
        )
        assert publication_payload.action.value == "publish_state"
        assert publication_payload.parent_bundle_digest == "sha256:" + "0" * 64
        assert transport.publication_ref is None
        if lost_pr_response == "cancel":
            result = await service.github_setup_action("cancel")
            assert result["state"] == "cancelled"
            assert not (harness.project / ".intent/team-publication.json").exists()
            assert not (harness.project / ".intent/team-setup.json").exists()
            service.close()
            service = ControlPlaneService(harness.runtime, origin=ORIGIN, clock=lambda: NOW)
            assert service.github_setup_status()["state"] == "unconfigured"
            assert transport.publication_ref is None
            return
        service.close()
        service = ControlPlaneService(
            harness.runtime, origin=ORIGIN, clock=lambda: NOW, webauthn_verifier=verifier
        )
        if lost_pr_response == "legacy-draft":
            draft_path = harness.project / ".intent/team-publication.json"
            old = json.loads(draft_path.read_bytes())
            old.pop("external_write_attempted")
            draft_path.write_text(json.dumps(old, separators=(",", ":")))
            assert transport.publication_ref is None
            assert service.github_setup_status()["state"] == "publication_recovery_required"
            assert json.loads(draft_path.read_bytes())["external_write_attempted"] is True
            with pytest.raises(ValueError):
                await service.github_setup_action("cancel")
            assert draft_path.exists()
        else:
            assert service.github_setup_status()["state"] == "publication_draft"
        recovered = await service.github_setup_action("publication-preview")
        assert recovered["preview"]["bundle_digest"] == publication["preview"]["bundle_digest"]
        with pytest.raises(ValueError):
            await service.github_setup_action("options", payload=publication_payload)
        recovered = await service.github_setup_action("publication-preview")
        publication_payload = HumanDecisionPayload.model_validate_json(
            json.dumps(recovered["payload"])
        )
        await service.github_setup_action("options", payload=publication_payload)
        if lost_pr_response is True:
            transport.lose_pr_response = True
            with pytest.raises(ValueError):
                await service.github_setup_action(
                    "verify", payload=publication_payload, response=b"signed-assertion"
                )
            assert transport.publication_ref is not None
            service.close()
            service = ControlPlaneService(
                harness.runtime, origin=ORIGIN, clock=lambda: NOW, webauthn_verifier=verifier
            )
            retry = await service.github_setup_action("publication-preview")
            assert retry["preview"]["bundle_digest"] == publication["preview"]["bundle_digest"]
            publication_payload = HumanDecisionPayload.model_validate_json(
                json.dumps(retry["payload"])
            )
            await service.github_setup_action("options", payload=publication_payload)
        published = await service.github_setup_action(
            "verify", payload=publication_payload, response=b"signed-assertion"
        )
        assert published["pull_request_url"] == "https://github.com/acme/project/pull/1"
        assert published["state"] == "publication_pending"
        if lost_pr_response == "legacy-receipt":
            draft_path = harness.project / ".intent/team-publication.json"
            old = json.loads(draft_path.read_bytes())
            old.pop("external_write_attempted")
            draft_path.write_text(json.dumps(old, separators=(",", ":")))
            service.close()
            service = ControlPlaneService(
                harness.runtime, origin=ORIGIN, clock=lambda: NOW, webauthn_verifier=verifier
            )
            assert service.github_setup_status()["state"] == "publication_pending"
            assert json.loads(draft_path.read_bytes())["external_write_attempted"] is True
        with pytest.raises(ValueError):
            await service.github_setup_action("cancel")
        assert (harness.project / ".intent/team-publication.json").exists()
        assert (harness.project / ".intent/team-setup.json").exists()
        assert not (harness.project / ".intent/team-trust.json").exists()
        pending = await service.github_setup_action("inspect")
        assert pending["state"] == "publication_pending"
        assert pending["pull_request_url"] == "https://github.com/acme/project/pull/1"
        assert not (harness.project / ".intent/team-trust.json").exists()
        transport.branch = "8" * 40
        finalized = await service.github_setup_action("inspect")
        assert finalized["state"] == "published"
        assert not (harness.project / ".intent/team-publication.json").exists()
        assert not (harness.project / ".intent/team-setup.json").exists()
        assert service._github_setup_bridge is None
        trust_path = harness.project / ".intent/team-trust.json"
        assert trust_path.exists()
        assert "private" not in trust_path.read_text()
        from intent_engineering.team_state.local_trust import LocalTrustProvider

        trust = LocalTrustProvider(harness.project).load()
        assert trust is not None and trust.repository_id == "github.com/acme/project"
        restarted = ControlPlaneService(harness.runtime, origin=ORIGIN, clock=lambda: NOW)
        try:
            assert restarted.team_enrollment_status()["status"] == "enrolled"
        finally:
            restarted.close()
        assert transport.branch == "8" * 40
        assert transport.commit["parents"] == ["c" * 40]
        assert transport.publication_ref["ref"].startswith("refs/heads/intent-publication/")
        assert len(transport.blobs) == (6 if lost_pr_response is True else 3)
        assert (
            len(
                [
                    path
                    for method, path, _ in transport.writes
                    if method == "POST" and path.endswith("/pulls")
                ]
            )
            == 1
        )
        import base64

        manifest = json.loads(base64.b64decode(transport.blobs[0]["content"]))
        assert manifest["parent_bundle_digest"] is None
        assert manifest["repository_id"] == "github.com/acme/project"
        from intent_engineering.team_state.crypto import EncryptedBundle, decrypt_bundle
        from intent_engineering.team_state.models import TeamStateManifest
        from intent_engineering.team_state.restore import _manifest_aad

        typed_manifest = TeamStateManifest.model_validate_json(
            base64.b64decode(transport.blobs[0]["content"])
        )
        aad = _manifest_aad(
            project_id=typed_manifest.project_id,
            repository_id=typed_manifest.repository_id,
            graph_version=typed_manifest.graph_version,
            parent_bundle_digest=None,
            created_at=typed_manifest.created_at,
            recipient_key_ids=typed_manifest.recipient_key_ids,
            required_signature_ids=typed_manifest.required_signature_ids,
        )
        envelope = EncryptedBundle.model_validate_json(
            base64.b64decode(transport.blobs[1]["content"])
        )
        plaintext = decrypt_bundle(envelope, ci_trust.recipient_private_key, aad)
        from intent_engineering.team_state.archive import validate_archive

        snapshot = validate_archive(plaintext)
        assert snapshot.project_id == harness.runtime.config.project_id
        assert snapshot.graph_version == 1
        assert (
            next(item.content for item in snapshot.files if item.path == "graph.yaml")
            == (harness.project / ".intent/graph.yaml").read_bytes()
        )
        assert all(value not in public_ci_path.read_text() for value in backend.values.values())
        assert "Keep exports local" not in str(transport.writes)
        if lost_pr_response is False:
            from intent_engineering.control_plane import team_enrollment as journey
            from intent_engineering.team_state.candidate import validate_candidate
            from intent_engineering.team_state.keys import GitHubIdentity
            from intent_engineering.team_state.publication import PreparedV1Migration
            from intent_engineering.team_state.restore import (
                SharedStateArtifacts,
                StaticTrustProvider,
                _GitRefReader,
            )
            from intent_engineering.team_state.setup import _draft
            from tests.helpers.shared_state import install_state_ref

            v1_artifacts = SharedStateArtifacts(
                manifest=base64.b64decode(transport.blobs[0]["content"]),
                bundle=base64.b64decode(transport.blobs[1]["content"]),
                signatures=base64.b64decode(transport.blobs[2]["content"]),
                bundle_path=(
                    f"bundles/{typed_manifest.graph_version}-"
                    f"{typed_manifest.bundle_digest.removeprefix('sha256:')}.intent"
                ),
                signature_path=(
                    f"signatures/{typed_manifest.graph_version}-"
                    f"{typed_manifest.bundle_digest.removeprefix('sha256:')}.json"
                ),
            )
            v1_commit = install_state_ref(harness.project, v1_artifacts)
            marker = harness.project / ".intent/cache/shared-state.json"
            marker.parent.mkdir(exist_ok=True)
            marker.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "bundle_digest": typed_manifest.bundle_digest,
                        "graph_version": typed_manifest.graph_version,
                        "ref_commit": v1_commit,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            marker.chmod(0o600)
            transport.branch = v1_commit
            transport.blobs = []
            transport.tree = []
            transport.commit = None
            transport.publication_ref = None
            transport.pr = None
            translated_commits: dict[str, str] = {}

            class RewrittenStateReader:
                def __init__(self, root):
                    self.reader = _GitRefReader(root)

                def translated(self, commit):
                    return translated_commits.get(commit, commit)

                def commit(self):
                    if transport.branch == transport.merged_commit:
                        return transport.merged_commit
                    return self.reader.commit()

                def blob(self, commit, path, maximum):
                    return self.reader.blob(self.translated(commit), path, maximum)

                def parents(self, commit):
                    return self.reader.parents(self.translated(commit))

                def require_release_tree(self, commit, name):
                    return self.reader.require_release_tree(self.translated(commit), name)

                def __getattr__(self, name):
                    return getattr(self.reader, name)

                def close(self):
                    self.reader.close()

            from intent_engineering.team_state import restore

            monkeypatch.setattr(
                restore,
                "_refresh_state_ref",
                lambda _repository_id: RewrittenStateReader(harness.project),
            )
            monkeypatch.setenv("GITHUB_REPOSITORY", "acme/project")
            invite_path = tmp_path / "alice-to-bob.intent-invite.json"
            request = journey.save_enrollment_request(
                harness.runtime,
                action="invite",
                project_id=harness.runtime.config.project_id,
                repository_id="github.com/acme/project",
                identity=GitHubIdentity(account_id="200", login="bob"),
                output=str(invite_path),
            )
            legacy_trust = LocalTrustProvider(harness.project).load_versioned()
            assert (
                f"intent-engineering/{harness.runtime.config.project_id}",
                legacy_trust.recipient_key_id,
            ) in backend.values
            assert journey.enrollment_status(harness.runtime)["state"] == "migration_required"
            membership = journey.MembershipSession(service)
            try:
                migration_context = await membership._migration()
                assert migration_context.commit == v1_commit
                migration = await membership.action("migration-preview", request.session_id)
                assert migration["state"] == "migration_preview"
                assert migration["preview"]["legacy_parent_bundle_digest"] == (
                    typed_manifest.bundle_digest
                )
                await membership.action("options", request.session_id)
                verifier.next_sign_count = 1
                original_default_commit = transport.default_commit

                def drift_default_tooling():
                    transport.after_publication_tree = None
                    transport.default_commit = "9" * 40

                transport.after_publication_tree = drift_default_tooling
                with pytest.raises(ValueError):
                    await membership.action(
                        "verify", request.session_id, response=b"signed-assertion"
                    )
                assert transport.tree
                assert journey.enrollment_status(harness.runtime)["state"] == (
                    "publication_recovery_required"
                )
            finally:
                membership.close()
            membership = journey.MembershipSession(service)
            try:
                with pytest.raises(ValueError):
                    await membership.action("reconcile", request.session_id)
            finally:
                membership.close()
            transport.default_commit = original_default_commit
            transport.lose_pr_response = True
            membership = journey.MembershipSession(service)
            try:
                with pytest.raises(ValueError):
                    await membership.action("reconcile", request.session_id)
            finally:
                membership.close()
            assert journey.enrollment_status(harness.runtime)["state"] == (
                "publication_recovery_required"
            )
            membership = journey.MembershipSession(service)
            try:
                pending = await membership.action("reconcile", request.session_id)
            finally:
                membership.close()
            assert pending["state"] == "publication_pending"
            assert pending["action"] == "invite"
            assert pending["session_id"] == request.session_id
            migration_draft = _draft(harness.runtime)
            assert migration_draft is not None
            prepared_migration = migration_draft.publication()
            assert type(prepared_migration) is PreparedV1Migration
            migration_commit = install_state_ref(
                harness.project,
                SharedStateArtifacts(
                    manifest=prepared_migration.manifest_bytes,
                    bundle=prepared_migration.bundle,
                    signatures=prepared_migration.signatures,
                    bundle_path=prepared_migration.bundle_path,
                    signature_path=prepared_migration.signature_path,
                ),
                parent=v1_commit,
            )
            git(harness.project, "update-ref", "refs/remotes/origin/intent-state", v1_commit)
            validate_candidate(
                harness.project,
                StaticTrustProvider(ci_trust),
                base=v1_commit,
                head=migration_commit,
                at=NOW,
            )
            translated_commits[transport.merged_commit] = migration_commit
            transport.merged_parent = v1_commit
            transport.branch = transport.merged_commit
            membership = journey.MembershipSession(service)
            try:
                await membership._sponsor()
            finally:
                membership.close()
            assert LocalTrustProvider(harness.project).load_versioned().schema_version == 2
            assert _draft(harness.runtime) is not None
            assert journey.enrollment_status(harness.runtime)["state"] == (
                "publication_recovery_required"
            )
            membership = journey.MembershipSession(service)
            try:
                reconciled = await membership.action("reconcile", request.session_id)
                assert reconciled["state"] == "review_required"
                assert reconciled["action"] == "invite"
                assert reconciled["session_id"] == request.session_id
            finally:
                membership.close()
            assert _draft(harness.runtime) is None
            membership = journey.MembershipSession(service)
            try:
                invitation = await membership.action("create-invite", request.session_id)
                assert invitation["state"] == "invitation-ready"
            finally:
                membership.close()
            assert invite_path.is_file()
    finally:
        service.close()
        harness.service.close()
        harness.runtime.close()
