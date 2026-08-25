"""Behavioral contract for the repository-scoped GitHub connector."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

import pytest

from intent_engineering.capture.base import ConnectorError, normalize_raw_source
from intent_engineering.capture.github.connector import GitHubCheckpoint, GitHubConnector
from intent_engineering.capture.github.models import PageResult


class StaticGitHubClient:
    """Deterministic provider boundary with complete endpoint-shaped payloads."""

    def __init__(self, pages: Mapping[str, PageResult]) -> None:
        self.pages = dict(pages)
        self.calls: list[tuple[str, dict[str, str], str | None]] = []

    async def get_pages(
        self,
        path: str,
        params: Mapping[str, str],
        etag: str | None = None,
    ) -> PageResult:
        self.calls.append((path, dict(params), etag))
        return self.pages[path]


def _user(login: str = "octocat") -> dict[str, object]:
    return {
        "id": 1,
        "login": login,
        "html_url": f"https://github.com/{login}",
        "avatar_url": "https://avatars.githubusercontent.com/u/1",
    }


def _issue(
    *, title: str = "Preserve provenance", labels: list[dict[str, object]] | None = None
) -> dict[str, object]:
    return {
        "id": 1042,
        "number": 42,
        "title": title,
        "body": "Evidence must remain immutable.",
        "state": "open",
        "user": _user(),
        "labels": labels if labels is not None else [{"name": "intent", "color": "112233"}],
        "milestone": {"title": "alpha", "number": 1},
        "updated_at": "2026-08-25T10:00:00Z",
        "html_url": "https://github.com/acme/demo/issues/42",
        "repository_url": "https://api.github.com/repos/acme/demo",
    }


def _pull_request() -> dict[str, object]:
    return {
        "id": 2007,
        "number": 7,
        "title": "Add deterministic sync",
        "body": None,
        "state": "closed",
        "user": None,
        "labels": [{"name": "sync"}],
        "milestone": None,
        "updated_at": "2026-08-25T10:05:00Z",
        "html_url": "https://github.com/acme/demo/pull/7",
        "base": {"ref": "main", "sha": "a" * 40, "repo": {"id": 9}},
        "head": {"ref": "feature", "sha": "b" * 40, "repo": {"id": 9}},
        "merge_commit_sha": "c" * 40,
        "draft": False,
    }


def _commit() -> dict[str, object]:
    return {
        "sha": "d" * 40,
        "html_url": f"https://github.com/acme/demo/commit/{'d' * 40}",
        "author": None,
        "committer": None,
        "commit": {
            "message": "Keep evidence durable\n\nAcross retries.",
            "author": {
                "name": "Ghost Author",
                "date": "2026-08-25T09:00:00Z",
                "email": "ghost@example.test",
            },
            "committer": {"name": "CI", "date": "2026-08-25T09:01:00Z", "email": "ci@example.test"},
            "tree": {"sha": "e" * 40},
        },
        "parents": [{"sha": "f" * 40}],
    }


def _issue_comment() -> dict[str, object]:
    return {
        "id": 3001,
        "body": "Confirmed.",
        "user": _user("reviewer"),
        "updated_at": "2026-08-25T10:10:00Z",
        "html_url": "https://github.com/acme/demo/issues/42#issuecomment-3001",
        "issue_url": "https://api.github.com/repos/acme/demo/issues/42",
        "reactions": {"total_count": 1},
    }


def _review_comment() -> dict[str, object]:
    return {
        "id": 4001,
        "body": "Please keep this deterministic.",
        "user": None,
        "updated_at": "2026-08-25T10:11:00Z",
        "html_url": "https://github.com/acme/demo/pull/7#discussion_r4001",
        "pull_request_url": "https://api.github.com/repos/acme/demo/pulls/7",
        "path": "src/sync.py",
        "line": 17,
        "diff_hunk": "@@ -1 +1 @@",
    }


def _pages(*, issue: dict[str, object] | None = None) -> dict[str, PageResult]:
    return {
        "/repos/acme/demo/issues": PageResult(items=(issue or _issue(),), etag='"issues-1"'),
        "/repos/acme/demo/pulls": PageResult(items=(_pull_request(),), etag='"pulls-1"'),
        "/repos/acme/demo/commits": PageResult(items=(_commit(),), etag='"commits-1"'),
        "/repos/acme/demo/issues/comments": PageResult(
            items=(_issue_comment(),), etag='"issue-comments-1"'
        ),
        "/repos/acme/demo/pulls/comments": PageResult(
            items=(_review_comment(),), etag='"review-comments-1"'
        ),
    }


@pytest.mark.anyio
async def test_connector_contract_has_repository_instance_identity_stable_order_and_exact_fetch() -> (
    None
):
    client = StaticGitHubClient(_pages())
    connector = GitHubConnector(client, owner="Acme", repository="Demo")

    discovered = await connector.discover(None)

    assert connector.connector_type == "github"
    assert connector.connector_id == "github:acme/demo"
    assert [item.external_object_id for item in discovered] == [
        f"github:acme/demo:commit:{'d' * 40}",
        "github:acme/demo:issue:42",
        "github:acme/demo:issue_comment:3001",
        "github:acme/demo:pull_request:7",
        "github:acme/demo:review_comment:4001",
    ]
    source = discovered[1]
    raw = await connector.fetch(source.external_object_id, source.external_version)
    assert connector.normalize(raw) == normalize_raw_source(raw)
    with pytest.raises(ConnectorError, match="GitHub fetch failed"):
        await connector.fetch(source.external_object_id, "2026-08-25T10:00:01Z")
    with pytest.raises(ConnectorError, match="GitHub fetch failed"):
        await connector.fetch("github:acme/demo:issue:999", source.external_version)


@pytest.mark.anyio
async def test_all_five_provider_kinds_normalize_to_exact_provider_neutral_evidence() -> None:
    connector = GitHubConnector(StaticGitHubClient(_pages()), owner="acme", repository="demo")
    discovered = await connector.discover(None)
    records = {
        item.external_object_id: connector.normalize(
            await connector.fetch(item.external_object_id, item.external_version)
        )
        for item in discovered
    }

    issue = records["github:acme/demo:issue:42"]
    assert issue.external_version == "2026-08-25T10:00:00Z"
    assert issue.author == "octocat"
    assert issue.observed_at == datetime(2026, 8, 25, 10, tzinfo=UTC)
    assert issue.source_locator == "https://github.com/acme/demo/issues/42"
    assert issue.model_dump(mode="json")["payload"] == {
        "kind": "issue",
        "repository": "acme/demo",
        "provider_id": 1042,
        "number": 42,
        "title": "Preserve provenance",
        "body": "Evidence must remain immutable.",
        "state": "open",
        "labels": ["intent"],
        "milestone": "alpha",
        "updated_at": "2026-08-25T10:00:00Z",
    }
    assert issue.content_hash.startswith("sha256:")

    pull = records["github:acme/demo:pull_request:7"]
    assert pull.author is None
    assert pull.model_dump(mode="json")["payload"] == {
        "kind": "pull_request",
        "repository": "acme/demo",
        "provider_id": 2007,
        "number": 7,
        "title": "Add deterministic sync",
        "body": None,
        "state": "closed",
        "labels": ["sync"],
        "milestone": None,
        "base_ref": "main",
        "base_sha": "a" * 40,
        "head_ref": "feature",
        "head_sha": "b" * 40,
        "merge_commit_sha": "c" * 40,
        "updated_at": "2026-08-25T10:05:00Z",
    }

    commit = records[f"github:acme/demo:commit:{'d' * 40}"]
    assert commit.external_version == "d" * 40
    assert commit.author == "Ghost Author"
    assert commit.observed_at == datetime(2026, 8, 25, 9, 1, tzinfo=UTC)
    assert commit.model_dump(mode="json")["payload"] == {
        "kind": "commit",
        "repository": "acme/demo",
        "sha": "d" * 40,
        "message": "Keep evidence durable\n\nAcross retries.",
        "committed_at": "2026-08-25T09:01:00Z",
    }

    issue_comment = records["github:acme/demo:issue_comment:3001"]
    assert issue_comment.parent_ref is None
    assert issue_comment.model_dump(mode="json")["payload"] == {
        "kind": "issue_comment",
        "repository": "acme/demo",
        "provider_id": 3001,
        "body": "Confirmed.",
        "issue_number": 42,
        "updated_at": "2026-08-25T10:10:00Z",
    }

    review = records["github:acme/demo:review_comment:4001"]
    assert review.parent_ref is None
    assert review.model_dump(mode="json")["payload"] == {
        "kind": "review_comment",
        "repository": "acme/demo",
        "provider_id": 4001,
        "body": "Please keep this deterministic.",
        "pull_request_number": 7,
        "path": "src/sync.py",
        "line": 17,
        "updated_at": "2026-08-25T10:11:00Z",
    }


@pytest.mark.anyio
async def test_pull_request_rows_from_issues_are_filtered_and_not_duplicated() -> None:
    pr_shaped_issue = {
        **_issue(),
        "id": 2007,
        "number": 7,
        "pull_request": {"url": "https://api.github.com/repos/acme/demo/pulls/7"},
    }
    pages = _pages()
    pages["/repos/acme/demo/issues"] = PageResult(items=(pr_shaped_issue,), etag='"issues-1"')
    connector = GitHubConnector(StaticGitHubClient(pages), owner="acme", repository="demo")

    discovered = await connector.discover(None)
    ids = [item.external_object_id for item in discovered]

    assert ids.count("github:acme/demo:pull_request:7") == 1
    assert "github:acme/demo:issue:7" not in ids


@pytest.mark.anyio
async def test_nested_unknown_fields_and_label_order_do_not_affect_semantic_hash() -> None:
    first_issue = _issue(
        labels=[{"name": "zeta", "color": "000000"}, {"name": "alpha", "color": "ffffff"}]
    )
    second_issue = dict(reversed(tuple(first_issue.items())))
    second_issue["labels"] = [
        {"color": "changed-extra", "name": "alpha"},
        {"name": "zeta", "description": "unknown"},
    ]
    second_issue["unknown_nested"] = {"secretish": [1, {"flag": True}]}

    first = GitHubConnector(
        StaticGitHubClient(_pages(issue=first_issue)), owner="acme", repository="demo"
    )
    second = GitHubConnector(
        StaticGitHubClient(_pages(issue=second_issue)), owner="acme", repository="demo"
    )
    first_source = next(
        item for item in await first.discover(None) if ":issue:" in item.external_object_id
    )
    second_source = next(
        item for item in await second.discover(None) if ":issue:" in item.external_object_id
    )
    first_record = first.normalize(
        await first.fetch(first_source.external_object_id, first_source.external_version)
    )
    second_record = second.normalize(
        await second.fetch(second_source.external_object_id, second_source.external_version)
    )

    assert first_record.content_hash == second_record.content_hash
    assert first_record.model_dump(mode="json")["payload"]["labels"] == ["alpha", "zeta"]

    changed = _issue(title="Changed semantic title", labels=first_issue["labels"])  # type: ignore[arg-type]
    third = GitHubConnector(
        StaticGitHubClient(_pages(issue=changed)), owner="acme", repository="demo"
    )
    third_source = next(
        item for item in await third.discover(None) if ":issue:" in item.external_object_id
    )
    third_record = third.normalize(
        await third.fetch(third_source.external_object_id, third_source.external_version)
    )
    assert third_record.content_hash != first_record.content_hash


@pytest.mark.anyio
async def test_checkpoint_etags_round_trip_and_not_modified_retains_exact_state() -> None:
    first_client = StaticGitHubClient(_pages())
    first = GitHubConnector(first_client, owner="acme", repository="demo")
    discovered = await first.discover(None)
    cursor = first.next_checkpoint(discovered)
    assert cursor is not None
    checkpoint = GitHubCheckpoint.decode(cursor, expected_repository="acme/demo")
    assert checkpoint.encode() == cursor

    not_modified_pages = {
        path: PageResult(items=(), etag=result.etag, not_modified=True)
        for path, result in _pages().items()
    }
    second_client = StaticGitHubClient(not_modified_pages)
    second = GitHubConnector(second_client, owner="acme", repository="demo")
    assert await second.discover(cursor) == ()
    assert second.next_checkpoint(()) == cursor
    assert [call[2] for call in second_client.calls] == [
        checkpoint.etags["issues"],
        checkpoint.etags["pull_requests"],
        checkpoint.etags["commits"],
        checkpoint.etags["issue_comments"],
        checkpoint.etags["review_comments"],
    ]


@pytest.mark.parametrize(
    "owner,repository",
    [("bad/owner", "demo"), ("-acme", "demo"), ("acme", "../demo"), ("acme", "demo name")],
)
def test_repository_components_are_inert(owner: str, repository: str) -> None:
    with pytest.raises(ValueError, match="invalid GitHub repository"):
        GitHubConnector(StaticGitHubClient({}), owner=owner, repository=repository)


@pytest.mark.parametrize(
    "cursor",
    [
        '{"cursor_schema_version":1,"cursor_schema_version":1,"etags":{},"newest_commit_sha":null,"newest_updated_at":null,"repository":"acme/demo"}',
        '{"cursor_schema_version":true,"etags":{},"newest_commit_sha":null,"newest_updated_at":null,"repository":"acme/demo"}',
        '{"cursor_schema_version":1,"etags":{},"newest_commit_sha":null,"newest_updated_at":NaN,"repository":"acme/demo"}',
        '{"cursor_schema_version":1,"etags":{"unknown":"etag"},"newest_commit_sha":null,"newest_updated_at":null,"repository":"acme/demo"}',
        '{"cursor_schema_version":1,"etags":{},"extra":1,"newest_commit_sha":null,"newest_updated_at":null,"repository":"acme/demo"}',
        "x" * 65_537,
    ],
)
def test_cursor_rejects_duplicate_nonfinite_wrong_type_extra_unknown_and_oversized_values(
    cursor: str,
) -> None:
    with pytest.raises(ValueError, match="invalid GitHub checkpoint") as caught:
        GitHubCheckpoint.decode(cursor, expected_repository="acme/demo")
    assert cursor[:80] not in str(caught.value)


def test_cursor_rejects_foreign_repository_and_noncanonical_encoding() -> None:
    checkpoint = GitHubCheckpoint(repository="acme/demo")
    cursor = checkpoint.encode()
    with pytest.raises(ValueError, match="invalid GitHub checkpoint"):
        GitHubCheckpoint.decode(cursor, expected_repository="acme/other")
    with pytest.raises(ValueError, match="invalid GitHub checkpoint"):
        GitHubCheckpoint.decode(cursor.replace(":", ": ", 1), expected_repository="acme/demo")


@pytest.mark.anyio
async def test_malformed_cursor_fails_before_http_and_checkpoint_call_is_single_use() -> None:
    client = StaticGitHubClient(_pages())
    connector = GitHubConnector(client, owner="acme", repository="demo")

    with pytest.raises(ConnectorError, match="GitHub discovery failed"):
        await connector.discover("not-json")
    assert client.calls == []

    discovered = await connector.discover(None)
    connector.next_checkpoint(discovered)
    with pytest.raises(ConnectorError, match="GitHub checkpoint failed"):
        connector.next_checkpoint(discovered)


@pytest.mark.anyio
async def test_newest_commit_sha_uses_provider_newest_first_order() -> None:
    newer = _commit()
    older = _commit()
    older["sha"] = "e" * 40
    older["html_url"] = f"https://github.com/acme/demo/commit/{'e' * 40}"
    older["commit"] = {
        "message": "Older commit",
        "author": {"name": "Old", "date": "2026-08-24T09:00:00Z"},
        "committer": {"name": "Old", "date": "2026-08-24T09:01:00Z"},
    }
    pages = _pages()
    pages["/repos/acme/demo/commits"] = PageResult(items=(newer, older), etag='"commits-2"')
    connector = GitHubConnector(StaticGitHubClient(pages), owner="acme", repository="demo")

    discovered = await connector.discover(None)
    cursor = connector.next_checkpoint(discovered)

    assert cursor is not None
    assert (
        GitHubCheckpoint.decode(cursor, expected_repository="acme/demo").newest_commit_sha
        == "d" * 40
    )


@pytest.mark.anyio
async def test_foreign_linked_comment_repository_fails_closed() -> None:
    foreign = _issue_comment()
    foreign["issue_url"] = "https://api.github.com/repos/acme/other/issues/42"
    pages = _pages()
    pages["/repos/acme/demo/issues/comments"] = PageResult(items=(foreign,), etag='"foreign"')
    connector = GitHubConnector(StaticGitHubClient(pages), owner="acme", repository="demo")

    discovered = await connector.discover(None)

    assert {item.external_object_id for item in discovered} == {
        f"github:acme/demo:commit:{'d' * 40}",
        "github:acme/demo:issue:42",
        "github:acme/demo:pull_request:7",
    }
    with pytest.raises(ConnectorError, match="GitHub checkpoint failed"):
        connector.next_checkpoint(discovered)


@pytest.mark.anyio
async def test_empty_successful_200_responses_retain_prior_cursor_and_etags_exactly() -> None:
    initial = GitHubConnector(StaticGitHubClient(_pages()), owner="acme", repository="demo")
    first_discovered = await initial.discover(None)
    prior_cursor = initial.next_checkpoint(first_discovered)
    assert prior_cursor is not None
    prior = GitHubCheckpoint.decode(prior_cursor, expected_repository="acme/demo")
    empty_pages = {
        path: PageResult(items=(), etag=f'"changed-{index}"')
        for index, path in enumerate(_pages(), start=1)
    }
    connector = GitHubConnector(StaticGitHubClient(empty_pages), owner="acme", repository="demo")

    discovered = await connector.discover(prior_cursor)

    assert discovered == ()
    assert connector.next_checkpoint(discovered) == prior_cursor
    assert (
        GitHubCheckpoint.decode(prior_cursor, expected_repository="acme/demo").etags == prior.etags
    )


@pytest.mark.anyio
async def test_normalize_rejects_raw_objects_outside_current_repository_cache() -> None:
    connector = GitHubConnector(StaticGitHubClient(_pages()), owner="acme", repository="demo")
    source = next(
        item for item in await connector.discover(None) if ":issue:" in item.external_object_id
    )
    raw = await connector.fetch(source.external_object_id, source.external_version)
    foreign = raw.model_copy(update={"external_object_id": "github:acme/other:issue:42"})

    with pytest.raises(ConnectorError, match="GitHub normalization failed"):
        connector.normalize(foreign)


@pytest.mark.anyio
async def test_unexpected_html_locator_fragment_fails_closed() -> None:
    unsafe = _issue()
    unsafe["html_url"] = "https://github.com/acme/demo/issues/42#provider-controlled"
    connector = GitHubConnector(
        StaticGitHubClient(_pages(issue=unsafe)), owner="acme", repository="demo"
    )

    discovered = await connector.discover(None)

    assert discovered == ()
    with pytest.raises(ConnectorError, match="GitHub checkpoint failed"):
        connector.next_checkpoint(discovered)


@pytest.mark.anyio
async def test_duplicate_endpoint_identity_fails_before_caching_that_endpoint() -> None:
    pages = _pages()
    pages["/repos/acme/demo/issues"] = PageResult(items=(_issue(), _issue()), etag='"duplicate"')
    connector = GitHubConnector(StaticGitHubClient(pages), owner="acme", repository="demo")

    discovered = await connector.discover(None)

    assert discovered == ()
    with pytest.raises(ConnectorError, match="GitHub fetch failed"):
        await connector.fetch("github:acme/demo:issue:42", "2026-08-25T10:00:00Z")
    with pytest.raises(ConnectorError, match="GitHub checkpoint failed"):
        connector.next_checkpoint(discovered)


@pytest.mark.anyio
async def test_provider_repository_url_casing_normalizes_to_canonical_scope() -> None:
    comment = _issue_comment()
    comment["issue_url"] = "https://api.github.com/repos/Acme/Demo/issues/42"
    comment["html_url"] = "https://github.com/Acme/Demo/issues/42#issuecomment-3001"
    pages = _pages()
    pages["/repos/acme/demo/issues/comments"] = PageResult(items=(comment,), etag='"case"')
    connector = GitHubConnector(StaticGitHubClient(pages), owner="Acme", repository="Demo")

    discovered = await connector.discover(None)
    source = next(item for item in discovered if ":issue_comment:" in item.external_object_id)
    record = connector.normalize(
        await connector.fetch(source.external_object_id, source.external_version)
    )

    assert record.external_object_id == "github:acme/demo:issue_comment:3001"
    assert record.source_locator == ("https://github.com/acme/demo/issues/42#issuecomment-3001")
