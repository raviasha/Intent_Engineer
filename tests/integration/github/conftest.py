"""Real-store GitHub sync harness backed by a deterministic fake HTTP API."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from intent_engineering.capture.github.auth import GitHubCredentials
from intent_engineering.capture.github.client import GitHubClient, RetryPolicy
from intent_engineering.capture.github.connector import GitHubConnector
from intent_engineering.core.models import CandidateAssertion, EvidenceDelta, Graph
from intent_engineering.extract.base import SemanticReasoner
from intent_engineering.storage.executor import LocalChangeSetExecutor
from intent_engineering.storage.jsonl.case_store import JsonlCaseStore
from intent_engineering.storage.jsonl.evidence_store import JsonlEvidenceStore
from intent_engineering.storage.secure import SecureDirectory
from intent_engineering.storage.transaction import LocalTransactionCoordinator
from intent_engineering.storage.yaml.checkpoint_store import YamlCheckpointStore
from intent_engineering.storage.yaml.graph_store import YamlGraphStore
from intent_engineering.sync.models import SyncRunResult
from intent_engineering.sync.orchestrator import SyncOrchestrator

NOW = datetime(2026, 8, 25, 12, tzinfo=UTC)
TOKEN = "gh" + "p_fake-only-never-persist"


def user(login: str = "octocat") -> dict[str, object]:
    return {"id": 1, "login": login, "html_url": f"https://github.com/{login}"}


def issue(number: int = 42, *, updated_at: str = "2026-08-25T10:00:00Z") -> dict[str, object]:
    return {
        "id": 1000 + number,
        "number": number,
        "title": f"Issue {number}",
        "body": "Body",
        "state": "open",
        "user": user(),
        "labels": [{"name": "intent"}],
        "milestone": None,
        "updated_at": updated_at,
        "html_url": f"https://github.com/acme/demo/issues/{number}",
    }


def pull() -> dict[str, object]:
    return {
        "id": 2007,
        "number": 7,
        "title": "PR 7",
        "body": "Body",
        "state": "open",
        "user": user("pr-author"),
        "labels": [],
        "milestone": None,
        "updated_at": "2026-08-25T10:05:00Z",
        "html_url": "https://github.com/acme/demo/pull/7",
        "base": {"ref": "main", "sha": "a" * 40},
        "head": {"ref": "feature", "sha": "b" * 40},
        "merge_commit_sha": None,
    }


def commit() -> dict[str, object]:
    return {
        "sha": "d" * 40,
        "html_url": f"https://github.com/acme/demo/commit/{'d' * 40}",
        "author": None,
        "committer": None,
        "commit": {
            "message": "Commit",
            "author": None,
            "committer": None,
        },
    }


def issue_comment() -> dict[str, object]:
    return {
        "id": 3001,
        "body": "Issue comment",
        "user": user("commenter"),
        "updated_at": "2026-08-25T10:10:00Z",
        "html_url": "https://github.com/acme/demo/issues/42#issuecomment-3001",
        "issue_url": "https://api.github.com/repos/acme/demo/issues/42",
    }


def review_comment() -> dict[str, object]:
    return {
        "id": 4001,
        "body": "Review comment",
        "user": user("reviewer"),
        "updated_at": "2026-08-25T10:11:00Z",
        "html_url": "https://github.com/acme/demo/pull/7#discussion_r4001",
        "pull_request_url": "https://api.github.com/repos/acme/demo/pulls/7",
        "path": "src/example.py",
        "line": None,
    }


class RecordingNoopReasoner(SemanticReasoner):
    """Record real durable deltas while returning non-semantic proposals."""

    def __init__(self) -> None:
        self.deltas: list[EvidenceDelta] = []

    def extract_assertions(self, delta: EvidenceDelta) -> Sequence[CandidateAssertion]:
        self.deltas.append(delta)
        return ()

    def map_to_graph(self, assertions: Sequence[CandidateAssertion], graph: Graph):  # type: ignore[no-untyped-def]
        from intent_engineering.core.models import ChangeSet

        del assertions
        return ChangeSet(
            id="changeset:noop",
            actor="fixture",
            timestamp=NOW,
            baseline_graph_version=graph.version,
            evidence_refs=(),
            nodes_added=(),
            nodes_updated=(),
            nodes_superseded=(),
            edges_added=(),
            edges_updated=(),
            edges_superseded=(),
            confidence_changes=(),
            implementation_status_changes=(),
            reconciliation_cases_created=(),
            reconciliation_cases_resolved=(),
            validation_status="validated",
        )

    def propose_reconciliation(self, case: object) -> None:
        del case


class FakeGitHubApi:
    """Mutable fake server with ETag support and one injectable endpoint failure."""

    order = ("issues", "pulls", "commits", "issues/comments", "pulls/comments")

    def __init__(self) -> None:
        self.payloads: dict[str, list[dict[str, object]]] = {
            "issues": [issue()],
            "pulls": [pull()],
            "commits": [commit()],
            "issues/comments": [issue_comment()],
            "pulls/comments": [review_comment()],
        }
        self.etags = {name: f'"{name.replace("/", "-")}-1"' for name in self.order}
        self.fail_endpoint: str | None = None
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        prefix = "/repos/acme/demo/"
        assert request.url.path.startswith(prefix)
        endpoint = request.url.path.removeprefix(prefix)
        if endpoint == self.fail_endpoint:
            return httpx.Response(
                429,
                content=b"PRIVATE_PROVIDER_BODY",
                headers={"Retry-After": "60", "X-GitHub-Request-Id": "SAFE_TEST"},
            )
        etag = self.etags[endpoint]
        if request.headers.get("If-None-Match") == etag:
            return httpx.Response(304, headers={"ETag": etag})
        return httpx.Response(200, json=self.payloads[endpoint], headers={"ETag": etag})


class GitHubSyncHarness:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.api = FakeGitHubApi()
        self.reasoner = RecordingNoopReasoner()
        directory = SecureDirectory.open(root, create=True)
        self.graph_path = root / "graph.yaml"
        self.evidence_path = root / "evidence.jsonl"
        self.checkpoint_path = root / "checkpoints.yaml"
        case_store = JsonlCaseStore(directory.file("cases.jsonl"))
        transactions = LocalTransactionCoordinator(
            directory.file(".local-transaction.json"),
            {
                "graph": directory.file("graph.yaml"),
                "history": directory.file("history.jsonl"),
                "cases": case_store._file,
            },
        )
        self.graph_store = YamlGraphStore(
            directory.file("graph.yaml"),
            history_path=directory.file("history.jsonl"),
            transactions=transactions,
        )
        self.graph_store.initialize(Graph(id="github-fixture", version=0, nodes=(), edges=()))
        self.evidence_store = JsonlEvidenceStore(directory.file("evidence.jsonl"))
        self.checkpoint_store = YamlCheckpointStore(directory.file("checkpoints.yaml"))
        credentials = GitHubCredentials.resolve({"GH_TOKEN": TOKEN}, lambda _: "unused")
        self.client = GitHubClient(
            credentials,
            transport=httpx.MockTransport(self.api.handler),
            retry_policy=RetryPolicy(max_attempts=1),
        )
        self.connector = GitHubConnector(self.client, owner="acme", repository="demo")
        executor = LocalChangeSetExecutor(self.graph_store, case_store, transactions)
        self.orchestrator = SyncOrchestrator(
            graph_store=self.graph_store,
            evidence_store=self.evidence_store,
            checkpoint_store=self.checkpoint_store,
            case_store=case_store,
            reasoner=self.reasoner,
            changeset_executor=executor,
            clock=lambda: NOW,
        )

    async def run(self, run_id: str = "github-run") -> SyncRunResult:
        return await self.orchestrator.run(run_id, (self.connector,))

    async def close(self) -> None:
        await self.client.aclose()

    def build_repository_connector(
        self,
        owner: str,
        repository: str,
    ) -> tuple[GitHubClient, GitHubConnector]:
        """Build another complete fake-API connector against the same durable stores."""
        api = FakeGitHubApi()
        source_repository = "acme/demo"
        target_repository = f"{owner}/{repository}"

        def replace_repository(value: object) -> object:
            if isinstance(value, str):
                return value.replace(source_repository, target_repository)
            if isinstance(value, list):
                return [replace_repository(item) for item in value]
            if isinstance(value, dict):
                return {key: replace_repository(item) for key, item in value.items()}
            return value

        def handler(request: httpx.Request) -> httpx.Response:
            prefix = f"/repos/{target_repository}/"
            assert request.url.path.startswith(prefix)
            endpoint = request.url.path.removeprefix(prefix)
            etag = api.etags[endpoint]
            if request.headers.get("If-None-Match") == etag:
                return httpx.Response(304, headers={"ETag": etag})
            payload = replace_repository(api.payloads[endpoint])
            return httpx.Response(200, json=payload, headers={"ETag": etag})

        credentials = GitHubCredentials.resolve({"GH_TOKEN": TOKEN}, lambda _: "unused")
        client = GitHubClient(
            credentials,
            transport=httpx.MockTransport(handler),
            retry_policy=RetryPolicy(max_attempts=1),
        )
        return client, GitHubConnector(client, owner=owner, repository=repository)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def github_sync_harness(tmp_path: Path) -> GitHubSyncHarness:
    return GitHubSyncHarness(tmp_path)
