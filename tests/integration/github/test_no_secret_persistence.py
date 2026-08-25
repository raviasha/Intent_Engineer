"""Offline application-path audit for GitHub credential persistence."""

from __future__ import annotations

import base64
import json
import os
import re
import stat
from collections.abc import Callable
from pathlib import Path

import anyio
import httpx
import pytest
from structlog.testing import capture_logs
from typer.testing import CliRunner

from intent_engineering.capture.base import ConnectorError
from intent_engineering.capture.github.auth import GitHubCredentials
from intent_engineering.capture.github.client import GitHubClient, RetryPolicy
from intent_engineering.capture.github.connector import GitHubCheckpoint, GitHubConnector
from intent_engineering.capture.github.errors import GitHubProtocolError, GitHubRateLimitError
from intent_engineering.cli.app import app
from intent_engineering.cli.github import check_github
from intent_engineering.cli.runtime import load_runtime, run_selected_sync
from intent_engineering.core.policy.project import initialize_project
from intent_engineering.sync.models import SyncRunStatus
from intent_engineering.validation import validate_project
from tests.helpers.cli import init_git_repo
from tests.integration.github.conftest import FakeGitHubApi

VALID_FINE_GRAINED_PAT = (
    "github_pat_" + "A1b2C3d4E5f6G7h8J9k0L1m2N3p4Q5r6S7t8U9v0"
)


def _independent_hex_reflections(token: str) -> tuple[bytes, bytes, bytes]:
    """Derive lower, upper, and deterministic mixed-case hexadecimal probes."""
    lowercase = token.encode("utf-8").hex()
    uppercase = lowercase.upper()
    letter_index = 0
    mixed_characters: list[str] = []
    for character in lowercase:
        if character in "abcdef":
            mixed_characters.append(character.upper() if letter_index % 2 == 0 else character)
            letter_index += 1
        else:
            mixed_characters.append(character)
    mixed = "".join(mixed_characters)
    return tuple(value.encode("ascii") for value in (lowercase, uppercase, mixed))


def _audit_needles(token: str) -> tuple[bytes, ...]:
    """Derive useful credential reflections without writing a token fixture to disk."""
    token_bytes = token.encode("utf-8")
    sanitized_bytes = re.sub(r"[^A-Za-z0-9:_-]", "_", token.strip()).encode("utf-8")
    encoded = (
        base64.b64encode(token_bytes),
        *_independent_hex_reflections(token),
    )
    variants = (token_bytes, sanitized_bytes, *encoded)
    meaningful_fragments = tuple(
        variant[index : index + 12]
        for variant in variants
        for index in range(max(0, len(variant) - 11))
    )
    return tuple(dict.fromkeys((
        token_bytes,
        token_bytes[:20],
        token_bytes[-12:],
        sanitized_bytes,
        sanitized_bytes[:20],
        sanitized_bytes[-12:],
        *encoded,
        *meaningful_fragments,
        b"Bearer " + token_bytes,
    )))


def _assert_secret_free(value: object, token: str) -> None:
    rendered = repr(value).encode("utf-8", errors="backslashreplace")
    assert all(needle not in rendered for needle in _audit_needles(token))


def _repository_traceback(error: BaseException) -> bytes:
    """Render only repository frames, matching the public traceback-risk boundary."""
    frames: list[str] = []
    traceback = error.__traceback__
    while traceback is not None:
        if "/src/intent_engineering/" in traceback.tb_frame.f_code.co_filename:
            frames.append(repr(traceback.tb_frame.f_locals))
        traceback = traceback.tb_next
    return "".join(frames).encode("utf-8", errors="backslashreplace")


def _scan_project(
    project: Path,
    token: str,
    expected: set[str],
    *,
    before_file_open: Callable[[str], None] | None = None,
) -> dict[str, bytes]:
    """Scan authenticated regular descriptors below one held no-follow project root."""
    files: dict[str, bytes] = {}
    directory_flags = (
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)

    def same_identity(left: os.stat_result, right: os.stat_result) -> bool:
        return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)

    def read_descriptor(descriptor: int) -> bytes:
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 65_536):
            chunks.append(chunk)
        return b"".join(chunks)

    def walk(directory_fd: int, relative: Path = Path()) -> None:
        with os.scandir(directory_fd) as entries:
            for entry in entries:
                entry_relative = relative / entry.name
                if relative == Path() and entry.name == ".git":
                    continue
                enumerated = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(enumerated.st_mode):
                    raise AssertionError(f"symlinked audit entry: {entry_relative}")
                if stat.S_ISDIR(enumerated.st_mode):
                    try:
                        child_fd = os.open(entry.name, directory_flags, dir_fd=directory_fd)
                    except OSError as error:
                        raise AssertionError(f"changed audit entry: {entry_relative}") from error
                    try:
                        opened = os.fstat(child_fd)
                        if not stat.S_ISDIR(opened.st_mode) or not same_identity(
                            enumerated, opened
                        ):
                            raise AssertionError(f"changed audit entry: {entry_relative}")
                        walk(child_fd, entry_relative)
                    finally:
                        os.close(child_fd)
                    continue
                if not stat.S_ISREG(enumerated.st_mode) or enumerated.st_nlink != 1:
                    raise AssertionError(f"unsafe audit entry: {entry_relative}")
                relative_name = entry_relative.as_posix()
                if before_file_open is not None:
                    before_file_open(relative_name)
                try:
                    descriptor = os.open(entry.name, file_flags, dir_fd=directory_fd)
                except OSError as error:
                    raise AssertionError(f"changed audit entry: {entry_relative}") from error
                try:
                    opened = os.fstat(descriptor)
                    if (
                        not stat.S_ISREG(opened.st_mode)
                        or opened.st_nlink != 1
                        or not same_identity(enumerated, opened)
                    ):
                        raise AssertionError(f"changed audit entry: {entry_relative}")
                    files[relative_name] = read_descriptor(descriptor)
                finally:
                    os.close(descriptor)

    root_fd = os.open(project, directory_flags)
    try:
        if not stat.S_ISDIR(os.fstat(root_fd).st_mode):
            raise AssertionError("audit root is not a directory")
        walk(root_fd)
    finally:
        os.close(root_fd)
    assert files, "the audit must scan at least one regular project file"
    assert expected <= set(files), f"missing canonical audit files: {expected - set(files)}"
    needles = _audit_needles(token)
    leaked = {
        name: needle
        for name, content in files.items()
        for needle in needles
        if needle in content
    }
    assert not leaked
    return files


@pytest.mark.anyio
async def test_real_github_sync_does_not_persist_a_unique_credential(
    github_sync_harness,
) -> None:  # type: ignore[no-untyped-def]
    """Persisting an Authorization value through any real store must fail this audit."""
    sentinel = "gh" + "p_task5_unique_persistence_sentinel"

    result = await github_sync_harness.run(token=sentinel)

    assert result.evidence_added == 5
    await github_sync_harness.close()


@pytest.mark.anyio
async def test_valid_fine_grained_pat_allows_normal_doctor(tmp_path: Path) -> None:
    """Recognizing the public token format alone must not reject safe doctor fields."""
    project = tmp_path / "doctor-project"
    project.mkdir()
    initialize_project(project)
    runtime = load_runtime(project)
    api = FakeGitHubApi()
    clients: list[GitHubClient] = []

    def client_factory(credentials: GitHubCredentials) -> GitHubClient:
        client = GitHubClient(
            credentials,
            transport=httpx.MockTransport(api.handler),
            retry_policy=RetryPolicy(max_attempts=1),
        )
        clients.append(client)
        return client

    result = await check_github(
        runtime,
        env={"GH_TOKEN": VALID_FINE_GRAINED_PAT, "GITHUB_REPOSITORY": "acme/demo"},
        token_runner=lambda _: "unused",
        client_factory=client_factory,
    )

    assert result.healthy is True
    assert result.access == "accessible"
    assert result.repository == "acme/demo"
    assert clients and all(client.is_closed for client in clients)


@pytest.mark.anyio
async def test_valid_fine_grained_pat_allows_normal_unpaginated_sync(
    github_sync_harness,
) -> None:  # type: ignore[no-untyped-def]
    """Locally constructed GitHub metadata must not collide with a valid token prefix."""
    result = await github_sync_harness.run(
        "valid-fine-grained-unpaginated",
        token=VALID_FINE_GRAINED_PAT,
    )
    await github_sync_harness.close()

    assert result.status is SyncRunStatus.SUCCESS
    assert result.evidence_added == 5
    assert len(
        github_sync_harness.evidence_store.ledger(
            "github:acme/demo",
            connector_type="github",
        )
    ) == 5


@pytest.mark.anyio
async def test_valid_fine_grained_pat_allows_normal_paginated_link_sync(
    github_sync_harness,
) -> None:  # type: ignore[no-untyped-def]
    """A normal GitHub Link URL must not collide with a valid token's public prefix."""
    api = github_sync_harness.api
    first_issue = api.payloads["issues"][0]
    second_issue = {
        **first_issue,
        "id": 1043,
        "number": 43,
        "title": "Issue 43",
        "html_url": "https://github.com/acme/demo/issues/43",
    }

    def paginated_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/acme/demo/issues":
            if request.url.params.get("page") == "2":
                return httpx.Response(200, json=[second_issue], headers={"ETag": '"issues-1"'})
            return httpx.Response(
                200,
                json=[first_issue],
                headers={
                    "ETag": '"issues-1"',
                    "Link": (
                        '<https://api.github.com/repos/acme/demo/issues?per_page=100&page=2>; '
                        'rel="next"'
                    ),
                },
            )
        return api.handler(request)

    await github_sync_harness.client.aclose()
    credentials = GitHubCredentials.resolve(
        {"GH_TOKEN": VALID_FINE_GRAINED_PAT},
        lambda _: "unused",
    )
    client = GitHubClient(
        credentials,
        transport=httpx.MockTransport(paginated_handler),
        retry_policy=RetryPolicy(max_attempts=1),
    )
    github_sync_harness.client = client
    github_sync_harness.connector = GitHubConnector(client, owner="acme", repository="demo")

    result = await github_sync_harness.orchestrator.run(
        "valid-fine-grained-paginated",
        (github_sync_harness.connector,),
    )
    await github_sync_harness.close()

    assert result.status is SyncRunStatus.SUCCESS
    assert result.evidence_added == 6
    assert len(
        github_sync_harness.evidence_store.ledger(
            "github:acme/demo",
            connector_type="github",
        )
    ) == 6


def test_audit_scanner_rejects_a_file_swapped_after_enumeration(tmp_path: Path) -> None:
    """A path swap between enumeration and open must not let different bytes be audited."""
    project = tmp_path / "project"
    project.mkdir()
    target = project / "state.jsonl"
    target.write_bytes(b"enumerated")
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"replacement")

    def swap(relative: str) -> None:
        if relative == "state.jsonl":
            os.replace(replacement, target)

    with pytest.raises(AssertionError, match="changed audit entry"):
        _scan_project(project, "not-present", {"state.jsonl"}, before_file_open=swap)


def test_audit_scanner_rejects_a_final_symlink_swap(tmp_path: Path) -> None:
    """A final symlink substitution must be rejected by the descriptor-relative open."""
    project = tmp_path / "project"
    project.mkdir()
    target = project / "state.jsonl"
    target.write_bytes(b"enumerated")
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"replacement")

    def swap(relative: str) -> None:
        if relative == "state.jsonl":
            target.unlink()
            target.symlink_to(replacement)

    with pytest.raises(AssertionError, match="changed audit entry"):
        _scan_project(project, "not-present", {"state.jsonl"}, before_file_open=swap)


def test_audit_scanner_rejects_a_final_hardlink_swap(tmp_path: Path) -> None:
    """A final multiply-linked substitution must be rejected before bytes are read."""
    project = tmp_path / "project"
    project.mkdir()
    target = project / "state.jsonl"
    target.write_bytes(b"enumerated")
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"replacement")

    def swap(relative: str) -> None:
        if relative == "state.jsonl":
            target.unlink()
            os.link(replacement, target)

    with pytest.raises(AssertionError, match="changed audit entry"):
        _scan_project(project, "not-present", {"state.jsonl"}, before_file_open=swap)


@pytest.mark.parametrize(
    "representation",
    (
        "full",
        "prefix",
        "suffix",
        "sanitized",
        "base64",
        "hex",
        "hex_upper",
        "hex_mixed",
        "hex_upper_prefix",
        "hex_upper_suffix",
        "hex_mixed_prefix",
        "hex_mixed_suffix",
        "full_key",
        "base64_key",
        "hex_upper_key",
        "hex_mixed_key",
        "hex_upper_prefix_key",
        "hex_upper_suffix_key",
        "hex_mixed_prefix_key",
        "hex_mixed_suffix_key",
    ),
)
@pytest.mark.anyio
async def test_reflected_github_source_payload_is_rejected_before_evidence_persistence(
    tmp_path: Path,
    representation: str,
) -> None:
    """A provider-reflected credential must fail closed before EvidenceRecord creation."""
    token = "gh" + "p_JKLMNO.KLMNO+JKLMNO"
    hex_lower, hex_upper, hex_mixed = (
        value.decode("ascii") for value in _independent_hex_reflections(token)
    )
    assert len({hex_lower, hex_upper, hex_mixed}) == 3
    reflections = {
        "full": token,
        "prefix": token[:20],
        "suffix": token[-12:],
        "sanitized": token.replace(".", "_").replace("+", "_")[4:20],
        "base64": base64.b64encode(token.encode("utf-8")).decode("ascii"),
        "hex": hex_lower,
        "hex_upper": hex_upper,
        "hex_mixed": hex_mixed,
        "hex_upper_prefix": f"A{hex_upper}",
        "hex_upper_suffix": f"{hex_upper}A",
        "hex_mixed_prefix": f"A{hex_mixed}",
        "hex_mixed_suffix": f"{hex_mixed}A",
    }
    project = tmp_path / f"payload-project-{representation}"
    project.mkdir()
    initialize_project(project)
    runtime = load_runtime(project)
    api = FakeGitHubApi()
    if representation.endswith("_key"):
        reflection_name = representation.removesuffix("_key")
        api.payloads["issues"][0][reflections[reflection_name]] = "unknown provider field"
    else:
        api.payloads["issues"][0]["title"] = reflections[representation]
    clients: list[GitHubClient] = []

    def client_factory(credentials: GitHubCredentials) -> GitHubClient:
        client = GitHubClient(
            credentials,
            transport=httpx.MockTransport(api.handler),
            retry_policy=RetryPolicy(max_attempts=1),
        )
        clients.append(client)
        return client

    with capture_logs() as logs:
        result = await run_selected_sync(
            runtime,
            "github",
            "reflected-source-payload",
            env={"GH_TOKEN": token, "GITHUB_REPOSITORY": "acme/demo"},
            token_runner=lambda _: "unused",
            client_factory=client_factory,
        )

    assert result.status is SyncRunStatus.FAILED
    assert runtime.evidence() == ()
    assert runtime.checkpoint_store.get("github:acme/demo") is None
    _assert_secret_free(result.model_dump(mode="json"), token)
    _assert_secret_free(logs, token)
    _scan_project(
        project,
        token,
        {".intent/config.yaml", ".intent/graph.yaml"},
    )
    assert all(client.is_closed for client in clients)

    probe_client = GitHubClient(
        GitHubCredentials.resolve({"GH_TOKEN": token}, lambda _: "unused"),
        transport=httpx.MockTransport(api.handler),
        retry_policy=RetryPolicy(max_attempts=1),
    )
    probe_connector = GitHubConnector(probe_client, owner="acme", repository="demo")
    discovered = await probe_connector.discover(None)
    assert discovered == ()
    with pytest.raises(ConnectorError) as caught:
        probe_connector.finalize_checkpoint(discovered, ())
    _assert_secret_free(
        (caught.value, caught.value.args, caught.value.__cause__, caught.value.__context__), token
    )
    assert all(needle not in _repository_traceback(caught.value) for needle in _audit_needles(token))
    await probe_client.aclose()
    assert probe_client.is_closed is True


@pytest.mark.anyio
async def test_reflected_pagination_link_is_rejected_without_following_or_retaining_token() -> None:
    """A credential-bearing Link target must never become the next endpoint or traceback state."""
    token = "gh" + "p_Q8mV2xR7kN4sT9cL6wZ3"
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=[],
            headers={
                "ETag": '"page-1"',
                "Link": (
                    f'<https://api.github.com/repos/acme/demo/{token}>; rel="next"'
                ),
            },
        )

    credentials = GitHubCredentials.resolve({"GH_TOKEN": token}, lambda _: "unused")
    client = GitHubClient(
        credentials,
        transport=httpx.MockTransport(handler),
        retry_policy=RetryPolicy(max_attempts=1),
    )
    with pytest.raises(GitHubProtocolError) as caught:
        await client.get_pages("/repos/acme/demo/issues", {"per_page": "100"})

    assert len(requests) == 1
    _assert_secret_free(
        (caught.value, caught.value.args, caught.value.__cause__, caught.value.__context__), token
    )
    assert all(needle not in _repository_traceback(caught.value) for needle in _audit_needles(token))
    await client.aclose()
    assert client.is_closed is True


@pytest.mark.parametrize("hex_case", ("upper", "mixed"))
@pytest.mark.parametrize("hex_affix", ("none", "prefix", "suffix"))
@pytest.mark.parametrize("header_name", ("ETag", "Link"))
@pytest.mark.anyio
async def test_case_insensitive_hex_reflection_in_response_headers_is_rejected(
    header_name: str,
    hex_affix: str,
    hex_case: str,
) -> None:
    """Changing hexadecimal letter case must not bypass the raw HTTP boundary."""
    token = "ghp_JKLMNOJKLMNOJKLMNO"
    lowercase, uppercase, mixed = _independent_hex_reflections(token)
    assert len({lowercase, uppercase, mixed}) == 3
    reflection = {"upper": uppercase, "mixed": mixed}[hex_case].decode("ascii")
    reflection = {
        "none": reflection,
        "prefix": f"A{reflection}",
        "suffix": f"{reflection}A",
    }[hex_affix]
    if hex_affix != "none":
        assert len(reflection) % 2 == 1
        assert re.fullmatch(r"[0-9A-Fa-f]+", reflection) is not None
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        headers = (
            {"ETag": f'"{reflection}"'}
            if header_name == "ETag"
            else {
                "ETag": '"page-1"',
                "Link": (
                    f'<https://api.github.com/repos/acme/demo/{reflection}>; rel="next"'
                ),
            }
        )
        return httpx.Response(200, json=[], headers=headers)

    credentials = GitHubCredentials.resolve({"GH_TOKEN": token}, lambda _: "unused")
    client = GitHubClient(
        credentials,
        transport=httpx.MockTransport(handler),
        retry_policy=RetryPolicy(max_attempts=1),
    )
    with pytest.raises(GitHubProtocolError) as caught:
        await client.get_pages("/repos/acme/demo/issues", {"per_page": "100"})

    assert len(requests) == 1
    _assert_secret_free(
        (caught.value, caught.value.args, caught.value.__cause__, caught.value.__context__), token
    )
    assert all(needle not in _repository_traceback(caught.value) for needle in _audit_needles(token))
    await client.aclose()
    assert client.is_closed is True


@pytest.mark.parametrize(
    ("token", "reflected_header", "safe_headers"),
    (
        (
            "Q8mV2xR7kN4sT9cL6wZ3",
            ("X-RateLimit-Resource", "Q8mV2xR7kN4sT9cL6wZ3"),
            {},
        ),
        ("314159265", ("X-RateLimit-Limit", "314159265"), {}),
        (
            "271828182",
            ("X-RateLimit-Remaining", "271828182"),
            {"X-RateLimit-Limit": "400000000"},
        ),
        (
            "161803398",
            ("X-RateLimit-Used", "161803398"),
            {"X-RateLimit-Limit": "400000000"},
        ),
        ("1787659200", ("X-RateLimit-Reset", "1787659200"), {}),
    ),
)
@pytest.mark.anyio
async def test_reflected_rate_limit_scalars_are_rejected_without_retaining_token(
    token: str,
    reflected_header: tuple[str, str],
    safe_headers: dict[str, str],
) -> None:
    """Credential overlap in resource or bounded numeric diagnostics must fail closed."""
    headers = {
        "X-RateLimit-Limit": "5000",
        "X-RateLimit-Remaining": "4999",
        "X-RateLimit-Used": "1",
        "X-RateLimit-Reset": "1787659200",
        "X-RateLimit-Resource": "core",
        **safe_headers,
        reflected_header[0]: reflected_header[1],
    }

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"full_name": "acme/demo"}, headers=headers)

    credentials = GitHubCredentials.resolve({"GH_TOKEN": token}, lambda _: "unused")
    client = GitHubClient(
        credentials,
        transport=httpx.MockTransport(handler),
        retry_policy=RetryPolicy(max_attempts=1),
    )
    with pytest.raises(GitHubProtocolError) as caught:
        await client.get_repository_status("acme/demo")

    _assert_secret_free(
        (caught.value, caught.value.args, caught.value.__cause__, caught.value.__context__), token
    )
    assert all(needle not in _repository_traceback(caught.value) for needle in _audit_needles(token))
    await client.aclose()
    assert client.is_closed is True


def test_offline_cli_distinguishes_failed_github_from_mixed_partial_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GitHub-only failure exits 1 while surviving local connectors make exit 3 partial."""
    token = "gh" + "p_C9vR4mK7xT2nL8sQ5wZ1"
    project = init_git_repo(tmp_path)
    initialize_project(project)
    api = FakeGitHubApi()
    api.fail_endpoint = "issues"
    clients: list[GitHubClient] = []
    environment = {"GH_TOKEN": token, "GITHUB_REPOSITORY": "acme/demo"}

    def client_factory(credentials: GitHubCredentials) -> GitHubClient:
        client = GitHubClient(
            credentials,
            transport=httpx.MockTransport(api.handler),
            retry_policy=RetryPolicy(max_attempts=1),
        )
        clients.append(client)
        return client

    def offline_invoke(runtime, sources: str):  # type: ignore[no-untyped-def]
        async def run():  # type: ignore[no-untyped-def]
            return await run_selected_sync(
                runtime,
                sources,
                f"cli-exit-{len(clients)}",
                env=environment,
                token_runner=lambda _: "unused",
                client_factory=client_factory,
            )

        return anyio.run(run)

    monkeypatch.setattr("intent_engineering.cli.app._configure_logging", lambda: None)
    monkeypatch.setattr("intent_engineering.cli.app._invoke_sync", offline_invoke)
    runner = CliRunner()
    with capture_logs() as logs:
        github_only = runner.invoke(
            app,
            ["sync", "--project", str(project), "--sources", "github", "--format", "json"],
            env=environment,
        )
        mixed = runner.invoke(
            app,
            [
                "sync",
                "--project",
                str(project),
                "--sources",
                "markdown,git,github",
                "--format",
                "json",
            ],
            env=environment,
        )

    assert github_only.exit_code == 1
    assert json.loads(github_only.stdout)["status"] == "failed"
    assert mixed.exit_code == 3
    assert json.loads(mixed.stdout)["status"] == "partial"
    _assert_secret_free(
        (
            logs,
            github_only.stdout,
            github_only.stderr,
            github_only.exception,
            mixed.stdout,
            mixed.stderr,
            mixed.exception,
        ),
        token,
    )
    assert len(clients) == 2 and all(client.is_closed for client in clients)


@pytest.mark.anyio
async def test_offline_cli_application_path_keeps_credentials_out_of_state_reports_and_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A token reflected by a provider error must never cross an application boundary."""
    token = "gh" + "p_task5_unique_persistence_sentinel_7d4c"
    project = tmp_path / "audit-project"
    project.mkdir()
    initialize_project(project)
    runtime = load_runtime(project)
    api = FakeGitHubApi()
    clients: list[GitHubClient] = []
    environment = {"GH_TOKEN": token, "GITHUB_REPOSITORY": "acme/demo"}

    def client_factory(credentials: GitHubCredentials) -> GitHubClient:
        client = GitHubClient(
            credentials,
            transport=httpx.MockTransport(api.handler),
            retry_policy=RetryPolicy(max_attempts=1),
        )
        clients.append(client)
        return client

    async def sync(run_id: str):  # type: ignore[no-untyped-def]
        return await run_selected_sync(
            runtime,
            "github",
            run_id,
            env=environment,
            token_runner=lambda _: "unused",
            client_factory=client_factory,
        )

    with capture_logs() as logs:
        first = await sync("audit-first")
    assert first.status is SyncRunStatus.SUCCESS
    assert first.evidence_added == 5
    evidence = runtime.evidence()
    assert len(evidence) == 5
    assert {record.connector_type for record in evidence} == {"github"}
    checkpoint = runtime.checkpoint_store.get("github:acme/demo")
    assert checkpoint is not None and checkpoint.cursor is not None
    assert GitHubCheckpoint.decode(checkpoint.cursor, expected_repository="acme/demo").repository == "acme/demo"
    checkpoint_path = project / ".intent/cache/checkpoints.yaml"
    checkpoint_before = checkpoint_path.read_bytes()
    expected = {
        ".intent/config.yaml",
        ".intent/graph.yaml",
        ".intent/evidence/evidence.jsonl",
        ".intent/cache/checkpoints.yaml",
    }
    first_state = _scan_project(project, token, expected)
    expected = set(first_state)
    _assert_secret_free(first.model_dump(mode="json"), token)
    _assert_secret_free(logs, token)

    second = await sync("audit-noop")
    assert second.status is SyncRunStatus.SUCCESS
    assert second.evidence_added == second.changes_applied == second.cases_created == 0
    assert _scan_project(project, token, expected) == first_state

    doctor = await check_github(
        runtime,
        env=environment,
        token_runner=lambda _: "unused",
        client_factory=client_factory,
    )
    assert doctor.healthy is True and doctor.access == "accessible"
    _assert_secret_free(doctor.model_dump(mode="json"), token)

    monkeypatch.setattr("intent_engineering.cli.app._configure_logging", lambda: None)

    async def offline_doctor(command_runtime: object, *, env: object, **_: object) -> object:
        return await check_github(
            command_runtime,  # type: ignore[arg-type]
            env=env,  # type: ignore[arg-type]
            token_runner=lambda _: "",
            client_factory=client_factory,
        )

    monkeypatch.setattr("intent_engineering.cli.app.check_github", offline_doctor)
    report = CliRunner().invoke(
        app,
        [
            "drift",
            "--project",
            str(project),
            "--format",
            "markdown",
            "--output",
            "intent-drift.md",
        ],
    )
    assert report.exit_code == 0
    assert (project / "intent-drift.md").read_text(encoding="utf-8") == report.stdout
    expected.add("intent-drift.md")
    _assert_secret_free((report.stdout, report.stderr, report.exception), token)
    repeated_report = CliRunner().invoke(
        app,
        [
            "drift",
            "--project",
            str(project),
            "--format",
            "markdown",
            "--output",
            "intent-drift.md",
        ],
    )
    assert repeated_report.exit_code == 0 and repeated_report.stdout == report.stdout
    report_state = _scan_project(project, token, expected)
    tombstones = {
        name: content for name, content in report_state.items() if ".rollback" in Path(name).name
    }
    assert tombstones and all(content == b"" for content in tombstones.values())
    expected = set(report_state)

    for status, body, headers, code in (
        (
            403,
            b"provider-error:" + token.encode(),
            {"X-GitHub-Request-Id": token},
            "github.permission",
        ),
        (
            429,
            b"provider-error:" + token.encode(),
            {"Retry-After": "60", "X-GitHub-Request-Id": token},
            "github.rate_limit",
        ),
        (200, b"not-json:" + token.encode(), {}, "github.protocol"),
    ):
        api.doctor_response = (status, body, headers)
        with capture_logs() as failure_logs:
            failed_doctor = await check_github(
                runtime,
                env=environment,
                token_runner=lambda _: "unused",
                client_factory=client_factory,
            )
        assert failed_doctor.error is not None and failed_doctor.error.code == code
        _assert_secret_free(failed_doctor.model_dump(mode="json"), token)
        _assert_secret_free(failure_logs, token)
        cli_failure = CliRunner().invoke(
            app,
            ["doctor", "github", "--project", str(project), "--format", "json"],
            env=environment,
        )
        assert cli_failure.exit_code == 1
        _assert_secret_free((cli_failure.stdout, cli_failure.stderr, cli_failure.exception), token)
    authentication_failure = await check_github(
        runtime,
        env={"GITHUB_REPOSITORY": "acme/demo"},
        token_runner=lambda _: "",
        client_factory=client_factory,
    )
    assert authentication_failure.error is not None
    assert authentication_failure.error.code == "github.authentication"
    _assert_secret_free(authentication_failure.model_dump(mode="json"), token)
    cli_authentication = CliRunner().invoke(
        app,
        ["doctor", "github", "--project", str(project), "--format", "json"],
        env={"GITHUB_REPOSITORY": "acme/demo"},
    )
    assert cli_authentication.exit_code == 1
    _assert_secret_free(
        (cli_authentication.stdout, cli_authentication.stderr, cli_authentication.exception), token
    )
    api.doctor_response = None

    reflected_client = client_factory(GitHubCredentials.resolve(environment, lambda _: "unused"))
    api.doctor_response = (
        429,
        b"provider-error:" + token.encode(),
        {"Retry-After": "60", "X-GitHub-Request-Id": token},
    )
    with pytest.raises(GitHubRateLimitError) as caught:
        await reflected_client.get_repository_status("acme/demo")
    _assert_secret_free(
        (caught.value, caught.value.args, caught.value.__cause__, caught.value.__context__), token
    )
    assert all(needle not in _repository_traceback(caught.value) for needle in _audit_needles(token))
    await reflected_client.aclose()

    api.doctor_response = None
    api.payloads["issues"] = [
        {
            **api.payloads["issues"][0],
            "title": "Changed before reflected rate limit",
            "updated_at": "2026-08-26T10:00:00Z",
        }
    ]
    api.etags["issues"] = '"issues-2"'
    api.fail_endpoint = "pulls"
    api.reflected_secret = token
    with capture_logs() as failure_logs:
        failed = await sync("audit-rate-failure")
    assert failed.status is SyncRunStatus.FAILED
    assert failed.evidence_added == 1
    assert checkpoint_path.read_bytes() == checkpoint_before
    _assert_secret_free(failed.model_dump(mode="json"), token)
    _assert_secret_free(failure_logs, token)
    _scan_project(project, token, expected)

    api.fail_endpoint = None
    api.reflected_secret = None
    recovered = await sync("audit-rate-retry")
    assert recovered.status is SyncRunStatus.SUCCESS
    assert recovered.evidence_added == 0
    assert recovered.connectors["github:acme/demo"].checkpoint_advanced is True
    assert checkpoint_path.read_bytes() != checkpoint_before
    _scan_project(project, token, expected)
    assert clients and all(client.is_closed for client in clients)


@pytest.mark.anyio
async def test_fresh_initialized_repository_validates_and_replays_one_combined_offline_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replacing the shared orchestrator with per-source runs breaks this release proof."""
    token = "gh" + "p_Z7qK9vX2mN4cR8tL6sW3_audit"
    project = init_git_repo(tmp_path)
    fixture = project / "release-intent.md"
    fixture.write_text(
        """---
intent_engineering:
  intent_assertion:
    id: assertion:release-proof
    subject_id: module:release-proof
    change_kind: initialize
    node_type: MODULE
    label: Release proof module
    source_mode: explicit
    evidence_refs: [$self]
    confidence: 0.9
  detection_input:
    schema_version: 1
    subject_ref: module:release-proof
    affected_refs: [module:release-proof]
    implementation:
      label: Release proof implementation
      claim: A material module exists without mapped semantics
      evidence_refs: [$self]
      confidence: 0.9
      source_mode: explicit
      current: true
    compatibility: unknown
    requirement_active: true
    has_mapped_semantics: false
    material_code_change: true
---
# Release proof
""",
        encoding="utf-8",
    )
    initialize_project(project)
    workspace = project / ".intent"
    initialized_directories = {
        "evidence",
        "reconciliation",
        "history",
        "approvals",
        "cache",
    }
    assert {
        entry.name for entry in workspace.iterdir() if entry.is_dir() and not entry.is_symlink()
    } == initialized_directories
    assert all(not any((workspace / name).iterdir()) for name in initialized_directories)
    assert not (workspace / "history/.local-transaction.json").exists()
    assert (workspace / "config.yaml").stat().st_size > 0
    assert (workspace / "graph.yaml").stat().st_size > 0
    assert validate_project(project).valid is True
    runtime = load_runtime(project)
    api = FakeGitHubApi()
    clients: list[GitHubClient] = []
    environment = {"GH_TOKEN": token, "GITHUB_REPOSITORY": "acme/demo"}

    def client_factory(credentials: GitHubCredentials) -> GitHubClient:
        client = GitHubClient(
            credentials,
            transport=httpx.MockTransport(api.handler),
            retry_policy=RetryPolicy(max_attempts=1),
        )
        clients.append(client)
        return client

    doctor = await check_github(
        runtime,
        env=environment,
        token_runner=lambda _: "unused",
        client_factory=client_factory,
    )
    assert doctor.healthy is True
    first = await run_selected_sync(
        runtime,
        "markdown,git,github",
        "combined-release-first",
        env=environment,
        token_runner=lambda _: "unused",
        client_factory=client_factory,
    )
    assert first.status is SyncRunStatus.SUCCESS
    assert set(first.connectors) == {"markdown", "git", "github:acme/demo"}
    assert first.evidence_added == 8
    assert first.changes_applied >= 2
    assert first.cases_created >= 1
    assert runtime.graph_store.load().version >= 2
    assert len(runtime.cases()) >= 1
    assert len(runtime.graph_store.history("module:release-proof")) >= 1
    assert not (workspace / "history/.local-transaction.json").exists()
    assert not any((workspace / "approvals").iterdir())
    assert {entry.name for entry in (workspace / "cache").iterdir()} == {
        ".checkpoints.yaml.lock",
        "checkpoints.yaml",
    }
    assert (workspace / "cache/.checkpoints.yaml.lock").read_bytes() == b""
    assert (workspace / "history/..local-transaction.json.lock").read_bytes() == b""
    for canonical in (
        workspace / "evidence/evidence.jsonl",
        workspace / "reconciliation/cases.jsonl",
        workspace / "history/changesets.jsonl",
        workspace / "cache/checkpoints.yaml",
    ):
        assert canonical.stat().st_size > 0
    assert validate_project(project).valid is True
    expected = {
        ".intent/config.yaml",
        ".intent/graph.yaml",
        ".intent/evidence/evidence.jsonl",
        ".intent/reconciliation/cases.jsonl",
        ".intent/history/changesets.jsonl",
        ".intent/cache/checkpoints.yaml",
    }
    first_state = _scan_project(project, token, expected)

    second = await run_selected_sync(
        runtime,
        "markdown,git,github",
        "combined-release-noop",
        env=environment,
        token_runner=lambda _: "unused",
        client_factory=client_factory,
    )
    assert second.status is SyncRunStatus.SUCCESS
    assert second.evidence_added == second.changes_applied == second.cases_created == 0
    assert _scan_project(project, token, set(first_state)) == first_state

    monkeypatch.setattr("intent_engineering.cli.app._configure_logging", lambda: None)
    report = CliRunner().invoke(
        app,
        [
            "drift",
            "--project",
            str(project),
            "--format",
            "markdown",
            "--output",
            "intent-drift.md",
        ],
    )
    assert report.exit_code == 0
    assert (project / "intent-drift.md").read_bytes() == report.stdout.encode("utf-8")
    _assert_secret_free((report.stdout, report.stderr, report.exception), token)
    assert clients and all(client.is_closed for client in clients)
