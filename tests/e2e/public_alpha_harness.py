"""Offline composition harness for the public-alpha release contract."""

from __future__ import annotations

import asyncio
import copy
import json
import shutil
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path

import httpx
import yaml  # type: ignore[import-untyped]
from structlog.testing import capture_logs

from intent_engineering.capture.github.auth import GitHubCredentials
from intent_engineering.capture.github.client import GitHubClient, RetryPolicy
from intent_engineering.capture.mcp.runtime import create_production_session
from intent_engineering.capture.mcp.session import McpServerConfig
from intent_engineering.cli.runtime import load_runtime, run_selected_sync
from intent_engineering.core.models import ResolutionAction
from intent_engineering.integrations.mcp_server.server import build_server
from intent_engineering.integrations.mcp_server.tools import McpReadServices
from intent_engineering.validation.service import validate_project
from tests.e2e.test_cli_write_approval import _approval_project, _FakeTerminal, _WriteRuntime
from tests.e2e.test_mcp_write_guard import _live_fake_workflow, _mutation_services
from tests.helpers.cli import run_intent
from tests.helpers.fixtures import materialize_fixture_repository
from tests.integration.github.conftest import FakeGitHubApi
from tests.integration.mcp.test_read_sync import McpSyncHarness
from tests.unit.capture.mcp.test_runtime import _CapturedSdk

RELEASE_NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
GITHUB_SENTINEL = "gh" + "p_PUBLIC_ALPHA_SENTINEL_82d9"
SLACK_SENTINEL = "slack-public-alpha-sentinel-7719"
JIRA_SENTINEL = "jira-public-alpha-sentinel-7719"


class PublicAlphaHarness:
    """Compose one production state domain with deterministic external boundaries."""

    def __init__(self, root: Path) -> None:
        descriptor_root = root / "descriptors"
        descriptor_root.mkdir()
        descriptor = descriptor_root / "public-alpha-source"
        descriptor.mkdir()
        (descriptor / "fixture.yaml").write_text(
            "kind: cross_author_conflict\nsubject: jira:jira-issue-1001\n", encoding="utf-8"
        )
        self.root = root
        self.project = materialize_fixture_repository(descriptor, root)
        self._configure_writes(root)
        self.runtime = load_runtime(self.project)
        self.github_api = FakeGitHubApi()
        self.github_clients: list[GitHubClient] = []
        self.slack = McpSyncHarness(root / "slack-fake")
        self.slack.runtime.credential_sentinel = SLACK_SENTINEL
        self.provider = _WriteRuntime()
        self.provider.credential_sentinel = JIRA_SENTINEL
        self.proposer = _live_fake_workflow(self.runtime, self.provider)
        self.reviewer = None
        self._changed_plan = None
        self._changed_approval = None
        self._logs: list[object] = []
        self._outputs: list[object] = []
        self._exercise_mcp_credentials()

    def _exercise_mcp_credentials(self) -> None:
        async def run() -> None:
            slack_sdk = _CapturedSdk()
            slack_session = create_production_session(
                McpServerConfig(
                    id="release-slack",
                    transport="stdio",
                    command="slack-mcp",
                    args=["--readonly"],
                    environment_refs={"SLACK_TOKEN": "env:SLACK_TOKEN"},
                ),
                sdk_loader=lambda: slack_sdk,
                environ={"SLACK_TOKEN": SLACK_SENTINEL},
            )
            await slack_session.start()
            assert slack_sdk.launched_environment == {"SLACK_TOKEN": SLACK_SENTINEL}
            await slack_session.close()
            assert SLACK_SENTINEL not in repr(vars(slack_session))

            jira_sdk = _CapturedSdk()
            jira_session = create_production_session(
                McpServerConfig(
                    id="release-jira",
                    transport="streamable_http",
                    url="https://jira.example.test/mcp",
                    headers={"Authorization": "env:JIRA_TOKEN"},
                ),
                sdk_loader=lambda: jira_sdk,
                environ={"JIRA_TOKEN": JIRA_SENTINEL},
            )
            await jira_session.start()
            assert jira_sdk.http_client is not None
            assert jira_sdk.http_client.kwargs["headers"] == {"Authorization": JIRA_SENTINEL}
            await jira_session.close()
            assert JIRA_SENTINEL not in repr(vars(jira_session))

        stdout = StringIO()
        stderr = StringIO()
        with capture_logs() as logs, redirect_stdout(stdout), redirect_stderr(stderr):
            asyncio.run(run())
        self._logs.append(logs)
        self._outputs.extend((stdout.getvalue(), stderr.getvalue()))

    def _configure_writes(self, root: Path) -> None:
        template_parent = root / "write-template"
        template_parent.mkdir()
        template = _approval_project(template_parent)
        shutil.copytree(template / "profiles", self.project / "profiles", dirs_exist_ok=True)
        shutil.copy2(
            template / ".intent/connectors/jira.yaml",
            self.project / ".intent/connectors/jira.yaml",
        )
        shutil.copy2(
            template / ".intent/approvals/policy.yaml",
            self.project / ".intent/approvals/policy.yaml",
        )
        self._set_actor("local:proposer")

    def _set_actor(self, actor: str) -> None:
        config_path = self.project / ".intent/config.yaml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        config["local_actor"] = actor
        config_path.write_text(yaml.safe_dump(config, sort_keys=True), encoding="utf-8")

    def _github_client(self, credentials: GitHubCredentials) -> GitHubClient:
        client = GitHubClient(
            credentials,
            transport=httpx.MockTransport(self.github_api.handler),
            retry_policy=RetryPolicy(max_attempts=1),
        )
        self.github_clients.append(client)
        return client

    def init(self):
        return run_intent(self.project, "init")

    def validate(self) -> dict[str, object]:
        result = validate_project(self.project)
        return {"valid": result.valid, "diagnostics": [item.code for item in result.diagnostics]}

    def _sync(self, sources: str, run_id: str) -> object:
        async def run() -> object:
            with capture_logs() as logs:
                result = await run_selected_sync(
                    self.runtime,
                    sources,
                    run_id,
                    env={
                        "GH_TOKEN": GITHUB_SENTINEL,
                        "GITHUB_REPOSITORY": "acme/demo",
                    },
                    token_runner=lambda _: "unused",
                    client_factory=self._github_client,
                    mcp_connectors=(self.slack.connector(),),
                )
            self._logs.append(logs)
            self._outputs.append(result.model_dump(mode="json"))
            return result

        return asyncio.run(run())

    def sync_twice(self) -> tuple[dict[str, int], dict[str, int]]:
        first = self._sync("markdown,git,github,mcp", "public-alpha-combined-1")
        second = self._sync("markdown,git,github,mcp", "public-alpha-combined-2")
        return (
            {
                "evidence_added": first.evidence_added,  # type: ignore[attr-defined]
                "changes_applied": first.changes_applied,  # type: ignore[attr-defined]
                "cases_created": first.cases_created,  # type: ignore[attr-defined]
            },
            {
                "evidence_added": second.evidence_added,  # type: ignore[attr-defined]
                "changes_applied": second.changes_applied,  # type: ignore[attr-defined]
                "cases_created": second.cases_created,  # type: ignore[attr-defined]
            },
        )

    def sync_conversation_revision(self) -> dict[str, int]:
        assert self.slack.runtime.raw is not None
        revised = copy.deepcopy(self.slack.runtime.raw)
        revised["updated"] = "1700000000.000300"
        revised["updated_at"] = "2026-08-20T10:20:30Z"
        revised["text"] = "A second teammate proposes centralized export."
        revised["last_modified_by"] = {"id": "U456"}
        revised["allowed_principals"] = ["U123", "U456", "slack-group:ENG"]
        self.slack.runtime.raw = revised
        result = self._sync("mcp", "public-alpha-conversation-revision")
        return {
            "evidence_added": result.evidence_added,  # type: ignore[attr-defined]
            "changes_applied": result.changes_applied,  # type: ignore[attr-defined]
        }

    def conversation_versions(self) -> tuple[tuple[str, str | None], ...]:
        versions = tuple(
            record for record in self.runtime.evidence() if record.connector_type == "mcp"
        )
        ingestions = {item.evidence.id: item for item in self.runtime.evidence_store.ingestions()}
        return tuple((record.author, ingestions[record.id].predecessor_id) for record in versions)

    def authors(self) -> set[str]:
        return {record.author for record in self.runtime.evidence()}

    def case_types(self) -> set[str]:
        return {case.case_type.value for case in self.runtime.cases()}

    def review_case_id(self) -> str:
        return next(
            case.id
            for case in self.runtime.cases()
            if case.case_type.value == "CONFLICTING_SOURCES"
        )

    def mcp_context(self, task: str) -> dict[str, object]:
        server = build_server(McpReadServices(self.runtime))
        result = asyncio.run(server.call_tool("intent_context", {"task": task, "format": "json"}))
        assert result.structured_content is not None
        self._outputs.append(result.structured_content)
        return result.structured_content

    def drift(self) -> str:
        result = run_intent(self.project, "drift", "--format", "markdown")
        self._outputs.extend((result.stdout, result.stderr))
        assert result.returncode == 0
        return result.stdout

    def external_write_preview(self, case_id: str) -> dict[str, object]:
        review = run_intent(
            self.project,
            "reconcile",
            "resolve",
            case_id,
            "--format",
            "json",
        )
        self._outputs.extend((review.stdout, review.stderr))
        assert review.returncode == 4
        assert review.json()["case"]["status"] == "needs_human"
        self.proposer = _live_fake_workflow(load_runtime(self.project), self.provider)
        preview = asyncio.run(
            self.proposer.create_preview(
                case_id,
                connector_id="jira-local",
                operation="update_issue",
                requested_fields={
                    "summary": "Keep exports local by default",
                    "description": "Independent review remains mandatory.",
                    "status": "Approved",
                },
                resolution_action=ResolutionAction.UPDATE_REQUIREMENT,
                now=RELEASE_NOW,
            )
        )
        self._changed_plan = asyncio.run(
            self.proposer.create_preview(
                case_id,
                connector_id="jira-local",
                operation="update_issue",
                requested_fields={
                    "summary": "A later reviewed proposal",
                    "description": "Target changes still invalidate approval.",
                    "status": "Approved",
                },
                resolution_action=ResolutionAction.UPDATE_REQUIREMENT,
                now=RELEASE_NOW,
            )
        )
        return {
            **preview.model_dump(mode="json"),
            "plan_hash": preview.canonical_hash,
            "plan_id": preview.id,
        }

    def execute_without_approval(self, plan_id: str) -> dict[str, object]:
        server = build_server(
            McpReadServices(self.runtime), mutation_services=_mutation_services(self.proposer)
        )
        before = len(self.provider.calls)
        result = asyncio.run(
            server.call_tool(
                "intent_write_execute",
                {
                    "plan_id": plan_id,
                    "approval_id": "approval:sha256:" + "f" * 64,
                },
            )
        )
        assert result.structured_content is not None
        self._outputs.append(result.structured_content)
        return {**result.structured_content, "provider_calls": len(self.provider.calls) - before}

    def interactive_approve(self, plan_id: str) -> dict[str, object]:
        self._set_actor("local:reviewer")
        reviewer_runtime = load_runtime(self.project)
        self.reviewer = _live_fake_workflow(reviewer_runtime, self.provider)
        approval = self.reviewer.approve(
            plan_id,
            _FakeTerminal(True, f"approve {plan_id}"),
            now=RELEASE_NOW.replace(minute=1),
        )
        assert self._changed_plan is not None
        self._changed_approval = self.reviewer.approve(
            self._changed_plan.id,
            _FakeTerminal(True, f"approve {self._changed_plan.id}"),
            now=RELEASE_NOW.replace(minute=1),
        )
        return approval.model_dump(mode="json")

    def execute(self, plan_id: str, approval_id: str) -> dict[str, object]:
        assert self.reviewer is not None
        before = sum(name == "update_issue" for name, _ in self.provider.calls)
        receipt = asyncio.run(
            self.reviewer.execute(plan_id, approval_id, now=RELEASE_NOW.replace(minute=2))
        )
        evidence = self.reviewer.catalog.runtime.evidence_store.get(receipt.evidence_ref)
        return {
            **receipt.model_dump(mode="json"),
            "provider_mutations": (
                sum(name == "update_issue" for name, _ in self.provider.calls) - before
            ),
            "evidence_author": evidence.author,
            "evidence_plan_id": evidence.payload["plan"]["id"],  # type: ignore[index]
        }

    def changed_target_rejection(self) -> dict[str, object]:
        assert self.reviewer is not None
        assert self._changed_plan is not None
        assert self._changed_approval is not None
        before = sum(name == "update_issue" for name, _ in self.provider.calls)
        self.provider.issue["updated"] = "2026-08-20T13:30:00Z"
        receipt = asyncio.run(
            self.reviewer.execute(
                self._changed_plan.id,
                self._changed_approval.id,
                now=RELEASE_NOW.replace(minute=5),
            )
        )
        after = sum(name == "update_issue" for name, _ in self.provider.calls)
        return {"status": receipt.status, "provider_mutations": after - before}

    def persisted_sentinels(self) -> tuple[str, ...]:
        sentinels = (GITHUB_SENTINEL, SLACK_SENTINEL, JIRA_SENTINEL)
        found: list[str] = []
        for path in self.project.rglob("*"):
            if not path.is_file() or path.is_symlink() or ".git" in path.parts:
                continue
            content = path.read_bytes()
            found.extend(value for value in sentinels if value.encode() in content)
        rendered = json.dumps((self._logs, self._outputs), default=str, sort_keys=True)
        found.extend(value for value in sentinels if value in rendered)
        return tuple(sorted(set(found)))

    def close(self) -> None:
        assert all(client.is_closed for client in self.github_clients)
