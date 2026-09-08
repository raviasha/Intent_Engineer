"""Production readiness rejects incomplete, changing, or unsafe canonical state."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import structlog
from typer.testing import CliRunner

import intent_engineering.cli.dev as dev_cli
import intent_engineering.cli.runtime as runtime_module
from intent_engineering.cli.app import app
from intent_engineering.cli.runtime import load_readiness_runtime
from intent_engineering.intent_workflow.readiness import EnsureRequest, ReadinessService
from intent_engineering.storage import secure
from intent_engineering.validation import validate_project
from tests.helpers.shared_state import ready_project

INVALID = {
    "version": "1",
    "schema_version": 1,
    "status": "shared_state_invalid",
    "attention_route": "team_state",
    "graph_version": 0,
    "pending_proposal_ids": [],
    "open_case_ids": [],
}
INPUT_PATHS = (
    "config.yaml",
    "graph.yaml",
    "evidence/evidence.jsonl",
    "history/changesets.jsonl",
    "reconciliation/cases.jsonl",
    "approvals/receipts.jsonl",
    "cache/checkpoints.yaml",
    "history/intent-proposals.jsonl",
    "history/.local-transaction.json",
)


@pytest.fixture(autouse=True)
def _reset_cli_logging(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not retain a CLI capture stream in later library tests' logging configuration."""
    monkeypatch.setattr(
        dev_cli,
        "_shared_restore",
        lambda _root: dev_cli.SharedStateRestoreResult(
            status=dev_cli.SharedStateRestoreStatus.NOT_REQUIRED
        ),
    )
    monkeypatch.setattr(
        dev_cli,
        "_start_or_reuse_background_service",
        lambda _root, _status: True,
    )
    structlog.reset_defaults()
    request.addfinalizer(structlog.reset_defaults)


def _ensure(project: Path) -> dict[str, object]:
    result = CliRunner().invoke(app, ["ensure", "--project", str(project), "--format", "json"])
    assert result.exit_code == 0
    assert result.stderr == ""
    return json.loads(result.stdout)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    ready_project(root)
    return root


@pytest.mark.parametrize(
    "path,content",
    (
        ("evidence/evidence.jsonl", b'{"PRIVATE-EVIDENCE-8197":'),
        ("history/changesets.jsonl", b'{"PRIVATE-HISTORY-8197":'),
        ("approvals/receipts.jsonl", b'{"PRIVATE-RECEIPT-8197":'),
        ("cache/checkpoints.yaml", b"PRIVATE-CHECKPOINT-8197: ["),
        ("history/intent-proposals.jsonl", b'{"PRIVATE-DECISION-8197":'),
    ),
)
def test_ensure_validates_every_canonical_ledger_before_reporting_ready(
    project: Path, path: str, content: bytes
) -> None:
    """Catches readiness omitting corrupt canonical evidence, history or decision inputs."""
    (project / ".intent" / path).write_bytes(content)

    assert _ensure(project) == INVALID
    assert (project / ".intent" / path).read_bytes() == content


def test_ensure_reuses_canonical_cross_file_validation(project: Path) -> None:
    """Catches parse-only readiness accepting a graph whose evidence was removed."""
    (project / ".intent/evidence/evidence.jsonl").write_bytes(b"")
    report = validate_project(project)
    assert not report.valid
    assert "graph.evidence_ref_missing" in {item.code for item in report.diagnostics}

    assert _ensure(project) == INVALID


@pytest.mark.parametrize("path", INPUT_PATHS)
def test_every_readiness_input_rejects_a_fifo_promptly(project: Path, path: str) -> None:
    """Catches a FIFO blocking the actual ensure command before file-kind authentication."""
    target = project / ".intent" / path
    target.unlink(missing_ok=True)
    os.mkfifo(target)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from intent_engineering.cli.app import app; app()",
            "ensure",
            "--project",
            str(project),
            "--format",
            "json",
        ],
        capture_output=True,
        timeout=2,
        check=False,
    )

    assert result.returncode == 0
    assert result.stderr == b""
    assert json.loads(result.stdout) == INVALID


@pytest.mark.parametrize("path", INPUT_PATHS)
@pytest.mark.parametrize("kind", ("symlink", "hardlink", "directory", "oversized"))
def test_every_readiness_input_rejects_unsafe_or_oversized_files(
    project: Path, path: str, kind: str
) -> None:
    """Catches unchecked optional files, link traversal and unbounded regular-file reads."""
    target = project / ".intent" / path
    original = target.read_bytes() if target.exists() else b""
    target.unlink(missing_ok=True)
    outside = project.parent / "PRIVATE-READINESS-8197"
    outside.write_bytes(original)
    if kind == "symlink":
        target.symlink_to(outside)
    elif kind == "hardlink":
        target.hardlink_to(outside)
    elif kind == "directory":
        target.mkdir()
    else:
        with target.open("wb") as stream:
            stream.truncate(8 * 1024 * 1024 + 1)

    assert _ensure(project) == INVALID
    assert outside.read_bytes() == original


def test_readiness_enforces_an_aggregate_input_bound(project: Path) -> None:
    """Catches individually bounded files collectively exceeding the readiness budget."""
    for name in ("config.yaml", "graph.yaml"):
        target = project / ".intent" / name
        target.write_bytes(target.read_bytes() + b"\n#" + b"x" * (6 * 1024 * 1024))
    (project / ".intent/approvals/receipts.jsonl").write_bytes(b" " * (6 * 1024 * 1024))

    assert _ensure(project) == INVALID


def test_readiness_rejects_change_to_an_earlier_file_during_the_final_read(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches identical byte scans accepting an earlier input rewritten during each scan."""
    target = project / ".intent/config.yaml"
    content = target.read_bytes()
    metadata = target.stat()
    trigger = project / ".intent/history/intent-proposals.jsonl"
    trigger.write_bytes(b"")
    trigger_inode = trigger.stat().st_ino
    original_read = os.read

    def changing_read(descriptor: int, size: int) -> bytes:
        result = original_read(descriptor, size)
        if os.fstat(descriptor).st_ino == trigger_inode:
            target.write_bytes(content)
            os.utime(target, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
        return result

    monkeypatch.setattr(secure.os, "read", changing_read)

    assert _ensure(project) == INVALID
    assert target.read_bytes() == content


def test_readiness_is_detached_replayable_and_concurrent_without_writes(project: Path) -> None:
    """Catches rereading live stores after validating a different canonical snapshot."""
    before = {
        path: path.read_bytes() for path in (project / ".intent").rglob("*") if path.is_file()
    }

    def inspect() -> str:
        runtime = load_readiness_runtime(project)
        try:
            return ReadinessService(runtime).ensure(EnsureRequest()).model_dump_json()
        finally:
            runtime.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = tuple(pool.map(lambda _: inspect(), range(8)))
    assert len(set(results)) == 1
    assert json.loads(results[0])["status"] == "ready"
    assert {
        path: path.read_bytes() for path in (project / ".intent").rglob("*") if path.is_file()
    } == before
    runtime = load_readiness_runtime(project)
    try:
        (project / ".intent/history/changesets.jsonl").write_bytes(b"PRIVATE-CORRUPT-8197")
        assert ReadinessService(runtime).ensure(EnsureRequest()).status.value == "ready"
    finally:
        runtime.close()


def test_readiness_uses_the_immutable_canonical_validator(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches readiness introducing a separate validator or reopening files for validation."""
    from intent_engineering.validation import validate_canonical_snapshot

    observed: list[dict[str, bytes | None]] = []

    def validate(content):
        observed.append(dict(content))
        return validate_canonical_snapshot(content)

    monkeypatch.setattr(runtime_module, "validate_canonical_snapshot", validate, raising=False)

    assert _ensure(project)["status"] == "ready"
    assert len(observed) == 1
    assert set(observed[0]) == {
        "config",
        "graph",
        "evidence",
        "history",
        "cases",
        "receipts",
        "checkpoints",
    }
    assert observed[0]["evidence"] == (project / ".intent/evidence/evidence.jsonl").read_bytes()


def test_readiness_cancellation_preserves_identity_and_releases_content_and_descriptors(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches cancellation retaining plaintext snapshot frames or open canonical descriptors."""

    class Cancelled(BaseException):
        pass

    cancellation = Cancelled("cancelled")
    marker = "PRIVATE-CANCEL-SNAPSHOT-8197"
    config = project / ".intent/config.yaml"
    config.write_bytes(config.read_bytes() + f"\n# {marker}\n".encode())
    trigger = project / ".intent/history/intent-proposals.jsonl"
    trigger.write_bytes(b"")
    inode = trigger.stat().st_ino
    opened: list[int] = []
    original_open = os.open
    original_read = os.read

    def record_open(*args, **kwargs):
        descriptor = original_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    def cancel_read(descriptor: int, size: int) -> bytes:
        if os.fstat(descriptor).st_ino == inode:
            raise cancellation
        return original_read(descriptor, size)

    monkeypatch.setattr(secure.os, "open", record_open)
    monkeypatch.setattr(secure.os, "read", cancel_read)

    with pytest.raises(Cancelled) as caught:
        load_readiness_runtime(project)

    assert caught.value is cancellation
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None
    traceback = caught.value.__traceback__
    while traceback is not None:
        frame = traceback.tb_frame
        if "/src/intent_engineering/" in frame.f_code.co_filename:
            assert marker not in repr(frame.f_locals)
        traceback = traceback.tb_next
    for descriptor in opened:
        with pytest.raises(OSError):
            os.fstat(descriptor)


@pytest.mark.parametrize("failure", ("cancel", "ordinary"))
@pytest.mark.parametrize(
    "operation,target,occurrence",
    (
        ("open", "project", 1),
        ("open", ".intent", 1),
        ("open", "evidence", 1),
        ("open", "project", 2),
        ("fstat", "project", 1),
        ("fstat", ".intent", 1),
        ("fstat", "project", 2),
        ("fstat", ".intent", 2),
        ("fstat", "evidence", 1),
        ("fstat", "evidence", 2),
        ("fstat", "project", 3),
    ),
)
def test_directory_readiness_failure_closes_every_descriptor_and_preserves_cancellation(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    target: str,
    occurrence: int,
    failure: str,
) -> None:
    """Catches untransferred directory FDs or cancellation hidden by readiness errors."""

    class Cancelled(BaseException):
        pass

    marker = "PRIVATE-DIRECTORY-SNAPSHOT-8197"
    config = project / ".intent/config.yaml"
    config.write_bytes(config.read_bytes() + f"\n# {marker}\n".encode())
    target_path = {
        "project": project,
        ".intent": project / ".intent",
        "evidence": project / ".intent/evidence",
    }[target]
    target_inode = target_path.stat().st_ino
    signal = Cancelled("cancelled") if failure == "cancel" else OSError(marker)
    observed: BaseException | None = None
    original_open, original_dup, original_close, original_fstat = (
        os.open,
        os.dup,
        os.close,
        os.fstat,
    )
    original_duplicate = secure.SecureDirectory.duplicate
    live: set[int] = set()
    # Keep real wrappers alive: deterministic cleanup must not depend on garbage collection.
    retained: list[secure.SecureDirectory] = []
    hits = 0

    def maybe_fail() -> None:
        nonlocal hits
        hits += 1
        if hits == occurrence:
            raise signal

    def record_open(path, *args, **kwargs):
        if operation == "open" and path == target:
            maybe_fail()
        descriptor = original_open(path, *args, **kwargs)
        live.add(descriptor)
        return descriptor

    def record_dup(descriptor: int) -> int:
        duplicated = original_dup(descriptor)
        live.add(duplicated)
        return duplicated

    def record_close(descriptor: int) -> None:
        original_close(descriptor)
        live.discard(descriptor)

    def authenticate(descriptor: int) -> os.stat_result:
        metadata = original_fstat(descriptor)
        if operation == "fstat" and metadata.st_ino == target_inode:
            maybe_fail()
        return metadata

    def retain_duplicate(directory: secure.SecureDirectory) -> secure.SecureDirectory:
        duplicated = original_duplicate(directory)
        retained.append(duplicated)
        return duplicated

    monkeypatch.setattr(secure.os, "open", record_open)
    monkeypatch.setattr(secure.os, "dup", record_dup)
    monkeypatch.setattr(secure.os, "close", record_close)
    monkeypatch.setattr(secure.os, "fstat", authenticate)
    monkeypatch.setattr(secure.SecureDirectory, "duplicate", retain_duplicate)
    try:
        if failure == "ordinary":
            assert _ensure(project) == INVALID
        else:
            try:
                load_readiness_runtime(project)
            except BaseException as caught:  # noqa: BLE001 - inspect cancellation identity
                observed = caught
        leaked = set(live)
    finally:
        for directory in retained:
            directory.close()
        for descriptor in tuple(live):
            record_close(descriptor)

    assert hits == occurrence
    assert leaked == set(), f"leaked {len(leaked)} directory descriptors"
    if failure == "cancel":
        assert observed is signal
        assert observed.__context__ is None and observed.__cause__ is None
        traceback = observed.__traceback__
        while traceback is not None:
            frame = traceback.tb_frame
            if "/src/intent_engineering/" in frame.f_code.co_filename:
                assert marker not in repr(frame.f_locals)
            traceback = traceback.tb_next


def test_dangling_workspace_symlink_is_invalid_not_onboarding(tmp_path: Path) -> None:
    """Catches unsafe existing workspace entries being mistaken for absent state."""
    (tmp_path / ".intent").symlink_to(tmp_path / "PRIVATE-MISSING-8197")

    assert _ensure(tmp_path) == INVALID


def test_aliasing_canonical_graph_path_does_not_leak_a_replaced_descriptor(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a configured graph alias replacing an already-owned canonical descriptor."""
    config = project / ".intent/config.yaml"
    config.write_bytes(config.read_bytes().replace(b".intent/graph.yaml", b".intent/config.yaml"))
    opened: list[int] = []
    original_open = os.open

    def record_open(*args, **kwargs):
        descriptor = original_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    monkeypatch.setattr(secure.os, "open", record_open)

    assert _ensure(project) == INVALID
    for descriptor in opened:
        with pytest.raises(OSError):
            os.fstat(descriptor)


@pytest.mark.parametrize("parent", (".intent", ".intent/evidence"))
def test_readiness_rejects_a_replaced_canonical_ancestor(
    project: Path, parent: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a content-identical replacement changing the bound canonical directory."""
    import shutil

    trigger = project / ".intent/history/intent-proposals.jsonl"
    trigger.write_bytes(b"")
    trigger_inode = trigger.stat().st_ino
    directory = project / parent
    replacement = project.parent / "replacement"
    shutil.copytree(directory, replacement)
    original_read = os.read
    replaced = False

    def replace_ancestor(descriptor: int, size: int) -> bytes:
        nonlocal replaced
        result = original_read(descriptor, size)
        if not replaced and os.fstat(descriptor).st_ino == trigger_inode:
            directory.rename(project.parent / "displaced")
            replacement.rename(directory)
            replaced = True
        return result

    monkeypatch.setattr(secure.os, "read", replace_ancestor)

    assert _ensure(project) == INVALID
    assert replaced
