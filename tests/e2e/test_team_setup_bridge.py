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


def test_confirmed_cli_transfers_exact_nonsecret_setup_into_production_service(tmp_path) -> None:
    """Catches the production command returning a route without a runnable setup request."""
    harness = _harness(tmp_path, aliases=("github:alice",))
    git(harness.project, "init", "--initial-branch=main")
    git(harness.project, "add", "docs/prd.md")
    git(harness.project, "commit", "-m", "Code")
    preview = build_github_enable_preview(harness.project, "acme/project")

    async def confirm():
        return await run_confirmed_github_enablement(
            harness.project,
            "acme/project",
            preview_confirmation=preview.preview_digest,
            protection_confirmation=None,
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
        assert "token" not in request_file.read_text().lower()
        assert result.control_plane_path == "/"
        service = ControlPlaneService(harness.runtime, origin=ORIGIN, clock=lambda: NOW)
        try:
            assert service.github_setup_status()["repository_id"] == "github.com/acme/project"
        finally:
            service.close()
    finally:
        harness.service.close()
        harness.runtime.close()


def test_stale_cli_setup_confirmation_never_persists_handoff(tmp_path) -> None:
    harness = _harness(tmp_path)

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

    harness = _harness(tmp_path)
    launches = []
    monkeypatch.setattr(
        "intent_engineering.cli.dev.dev_command", lambda **kwargs: launches.append(kwargs)
    )
    try:
        preview = build_github_enable_preview(harness.project, "acme/project")
        result = CliRunner().invoke(
            app,
            [
                "team",
                "enable",
                "github",
                "--project",
                str(harness.project),
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
    harness = _harness(tmp_path)
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

    harness = _harness(tmp_path)
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
        self.lose_pr_response = False

    async def request_json_object(
        self, method, path, *, payload=None, params=None, allowed_statuses=frozenset({200})
    ):
        from intent_engineering.team_state.github import GitHubTeamStateClient

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
            }
        elif path.endswith("/branches/main"):
            data = {"name": "main", "commit": {"sha": "a" * 40}}
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
            data = GitHubTeamStateClient._protection_payload() if self.protected else {}
            if self.protected:
                data["enforce_admins"] = {"enabled": True}
        elif path.endswith("/contents/.github/CODEOWNERS"):
            code, data = 404, {}
        elif path.endswith("/git/blobs"):
            import hashlib

            self.blobs.append(payload)
            code, data = 201, {"sha": hashlib.sha1(payload["content"].encode()).hexdigest()}
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
            else:
                code, data = 201, {"sha": "b" * 40}
                if self.after_tree:
                    self.after_tree()
        elif "/git/trees/" in path:
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
            data = {"sha": "c" * 40, "tree": {"sha": "b" * 40}, "parents": []}
        elif path.endswith("/git/refs"):
            if payload["ref"] == "refs/heads/intent-state":
                self.branch = payload["sha"]
            else:
                if self.publication_ref is not None:
                    return GitHubJsonResponse(422, {}, {"X-OAuth-Scopes": "repo"})
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
        elif path.endswith("/pulls"):
            self.pr = {
                "number": 1,
                "html_url": "https://github.com/acme/project/pull/1",
                "head": {
                    "ref": self.publication_ref["ref"].removeprefix("refs/heads/"),
                    "repo": {"full_name": "acme/project"},
                },
                "base": {"ref": "intent-state", "repo": {"full_name": "acme/project"}},
            }
            code, data = 201, self.pr
            if self.lose_pr_response:
                self.lose_pr_response = False
                raise RuntimeError("GitHub response lost")
        else:
            raise AssertionError((method, path))
        assert code in allowed_statuses
        return GitHubJsonResponse(code, data, {"X-OAuth-Scopes": "repo"})

    async def aclose(self):
        self.closed += 1

    async def get_pages(self, path, params, etag=None):
        from intent_engineering.capture.github.models import PageResult

        return PageResult(items=() if self.pr is None else (self.pr,), etag=None)


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
    def verify_authentication(self, response, request):
        assert response == b"signed-assertion"
        assert request == self.authentication_requests[-1]
        return VerifiedAuthentication(
            credential_id=b"team-control-plane-credential", new_sign_count=0, user_verified=True
        )


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["file-conflict", "anchor-race"])
async def test_protection_rejects_conflicts_and_branch_changes_at_write_boundaries(
    tmp_path, monkeypatch, failure
):
    import keyring

    from intent_engineering.team_state import setup

    harness = _harness(tmp_path, aliases=("github:alice",))
    git(harness.project, "init", "--initial-branch=main")
    git(harness.project, "add", "docs/prd.md")
    git(harness.project, "commit", "-m", "Code")
    setup.save_setup_request(
        harness.runtime, build_github_enable_preview(harness.project, "acme/project")
    )
    transport, backend, verifier = _GitHubTransport(), _Keyring(), _SetupVerifier()
    monkeypatch.setattr(setup, "github_api", lambda: transport)
    monkeypatch.setattr(keyring, "get_password", backend.get_password)
    monkeypatch.setattr(keyring, "set_password", backend.set_password)
    service = ControlPlaneService(
        harness.runtime, origin=ORIGIN, clock=lambda: NOW, webauthn_verifier=verifier
    )
    try:
        await service.github_setup_action("enroll")
        service.complete_team_enrollment(_registration_response(verifier.registration_requests[-1]))
        if failure == "file-conflict":
            (harness.project / ".github").mkdir()
            (harness.project / ".github/CODEOWNERS").write_text("* @someone-else\n")
            with pytest.raises(ValueError):
                await service.github_setup_action("protection-preview")
            assert transport.writes == []
        else:
            preview = await service.github_setup_action("protection-preview")
            payload = HumanDecisionPayload.model_validate_json(json.dumps(preview["payload"]))
            await service.github_setup_action("options", payload=payload)
            transport.after_tree = lambda: setattr(transport, "branch", "9" * 40)
            with pytest.raises(ValueError):
                await service.github_setup_action(
                    "verify", payload=payload, response=b"signed-assertion"
                )
            assert transport.branch == "9" * 40
            assert not any(path.endswith("/git/commits") for _, path, _ in transport.writes)
    finally:
        service.close()
        harness.service.close()
        harness.runtime.close()


@pytest.mark.anyio
@pytest.mark.parametrize("lost_pr_response", [False, True])
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
    git(harness.project, "add", "docs/prd.md")
    git(harness.project, "commit", "-m", "Code")
    preview = build_github_enable_preview(harness.project, "acme/project")
    await run_confirmed_github_enablement(
        harness.project,
        "acme/project",
        preview_confirmation=preview.preview_digest,
        protection_confirmation=None,
    )
    transport = _GitHubTransport()
    backend = _Keyring()

    class OfflineHttpTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            assert request.headers["Authorization"] == "Bearer offline-github-credential"
            if request.method == "GET" and request.url.path.endswith("/pulls"):
                page = await transport.get_pages(request.url.path, dict(request.url.params))
                return httpx.Response(
                    200,
                    json=page.model_dump(mode="json")["items"],
                    headers={"X-OAuth-Scopes": "repo"},
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
        assert result["state"] == "protection_configured"
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
        publication_payload = HumanDecisionPayload.model_validate_json(
            json.dumps(publication["payload"])
        )
        assert publication_payload.action.value == "publish_state"
        assert publication_payload.parent_bundle_digest == "sha256:" + "0" * 64
        assert transport.publication_ref is None
        service.close()
        service = ControlPlaneService(
            harness.runtime, origin=ORIGIN, clock=lambda: NOW, webauthn_verifier=verifier
        )
        recovered = await service.github_setup_action("publication-preview")
        assert recovered["preview"]["bundle_digest"] == publication["preview"]["bundle_digest"]
        with pytest.raises(ValueError):
            await service.github_setup_action("options", payload=publication_payload)
        recovered = await service.github_setup_action("publication-preview")
        publication_payload = HumanDecisionPayload.model_validate_json(
            json.dumps(recovered["payload"])
        )
        await service.github_setup_action("options", payload=publication_payload)
        if lost_pr_response:
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
        assert not (harness.project / ".intent/team-publication.json").exists()
        assert not (harness.project / ".intent/team-setup.json").exists()
        assert service._github_setup_bridge is None
        assert transport.branch == "c" * 40
        assert transport.commit["parents"] == ["c" * 40]
        assert transport.publication_ref["ref"].startswith("refs/heads/intent-publication/")
        assert len(transport.blobs) == (6 if lost_pr_response else 3)
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
        assert "Keep exports local" not in str(transport.writes)
    finally:
        service.close()
        harness.service.close()
        harness.runtime.close()
