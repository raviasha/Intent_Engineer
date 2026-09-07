"""Thin, fail-closed steps used by the repository's GitHub workflows."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import anyio
from pydantic import ConfigDict, Field

from intent_engineering.cli.runtime import CheckRuntimeAdapter, load_runtime
from intent_engineering.core.models._base import StrictModel
from intent_engineering.intent_workflow.check import (
    SharedStateRestoreStatus,
    TestResultArtifact,
    validate_test_result_artifact,
)
from intent_engineering.intent_workflow.dev_observer import DevObserver, TestRunStatus
from intent_engineering.storage.jsonl.strict import loads_strict_object
from intent_engineering.storage.secure import SecureDirectory
from intent_engineering.team_state.restore import EnvironmentTrustProvider, GitSharedStateRestorer

_STAGED_RESULT = Path(".intent-ci/reviewed-tests.json")
_RESULT = Path(".intent-ci/test-results.json")


class _StagedResult(StrictModel):
    model_config = ConfigDict(frozen=True, strict=True)
    snapshot: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    result: TestResultArtifact


def _clear_results(root: Path) -> None:
    directory = SecureDirectory.open(root)
    try:
        for path in (_STAGED_RESULT, _RESULT):
            target = directory.file(path, create_parents=True)
            try:
                target.unlink(missing_ok=True)
            finally:
                target.close()
    finally:
        directory.close()


def _observer(root: Path, adapter: CheckRuntimeAdapter) -> DevObserver:
    runtime = load_runtime(root)
    try:
        return DevObserver(
            root, runtime.config, repository_id=adapter.repository_id, principals=adapter.principals
        )
    finally:
        runtime.close()


def _require_snapshot(observer: DevObserver, expected: str) -> None:
    if observer.clean_commit_snapshot() != expected:
        raise ValueError("clean commit snapshot changed")


def restore(root: Path) -> None:
    """Verify the fetched protected ref before restoring any approved local baseline."""
    result = GitSharedStateRestorer(
        EnvironmentTrustProvider()
    ).verify_and_restore_approved_baseline(root)
    if result.status is not SharedStateRestoreStatus.VERIFIED:
        raise ValueError("shared state unavailable")


def _write(root: Path, path: Path, content: bytes) -> None:
    directory = SecureDirectory.open(root)
    try:
        target = directory.file(path, create_parents=True)
        try:
            target.atomic_write(content, reject_target_races=True)
        finally:
            target.close()
    finally:
        directory.close()


async def run_tests(root: Path, at: datetime) -> None:
    """Run every restored, reviewed argv; stage evidence only if every command passed."""
    adapter = CheckRuntimeAdapter(root)
    observer: DevObserver | None = None
    completed_write = False
    try:
        adapter.restore(require_shared=False)
        _clear_results(root)
        observer = _observer(root, adapter)
        if not observer.command_ids:
            raise ValueError("reviewed tests failed")
        observer.prepare_clean_commit_execution()
        revision = adapter.current_revision()
        snapshot = observer.clean_commit_snapshot()
        completed: list[str] = []
        author = ""
        for identifier in observer.command_ids:
            _require_snapshot(observer, snapshot)
            result = await observer.run_reviewed_tests(identifier, at=at)
            _require_snapshot(observer, snapshot)
            if (
                result.status is not TestRunStatus.PASSED
                or result.artifact is None
                or result.artifact.commit_sha != revision
            ):
                raise ValueError("reviewed tests failed")
            completed.append(identifier)
            author = result.artifact.author
        artifact = TestResultArtifact(
            repository_id=adapter.repository_id,
            commit_sha=revision,
            observed_at=at,
            status="passed",
            test_ids=tuple(completed),
            author=author,
            acl=tuple(sorted(adapter.principals)),
        )
        _require_snapshot(observer, snapshot)
        staged = _StagedResult(snapshot=snapshot, result=artifact)
        _write(root, _STAGED_RESULT, staged.model_dump_json().encode("utf-8"))
        _require_snapshot(observer, snapshot)
        completed_write = True
    finally:
        if observer is not None:
            observer.close()
        adapter.close()
        if not completed_write:
            _clear_results(root)


def write_results(root: Path, at: datetime) -> None:
    """Revalidate repository, HEAD, time and ACL before writing canonical CI evidence."""
    adapter = CheckRuntimeAdapter(root)
    observer: DevObserver | None = None
    completed_write = False
    try:
        adapter.restore(require_shared=False)
        raw = adapter.read_test_results(_STAGED_RESULT)
        loads_strict_object(raw.decode("utf-8"))
        staged = _StagedResult.model_validate_json(raw)
        artifact = validate_test_result_artifact(
            staged.result.canonical_bytes(),
            repository_id=adapter.repository_id,
            commit_sha=adapter.current_revision(),
            at=at,
            principals=adapter.principals,
        )
        observer = _observer(root, adapter)
        _require_snapshot(observer, staged.snapshot)
        _write(root, _RESULT, artifact.canonical_bytes())
        _require_snapshot(observer, staged.snapshot)
        completed_write = True
    finally:
        if observer is not None:
            observer.close()
        adapter.close()
        if not completed_write:
            _clear_results(root)


def main() -> int:
    """Keep all subprocess and trust failures fixed and secret-free on workflow stdout."""
    try:
        root = Path.cwd()
        now = datetime.now(UTC)
        if sys.argv[1:] == ["restore"]:
            restore(root)
        elif sys.argv[1:] == ["test"]:
            anyio.run(run_tests, root, now)
        elif sys.argv[1:] == ["results"]:
            write_results(root, now)
        else:
            raise ValueError("invalid workflow step")
    except Exception:  # noqa: BLE001 - fixed workflow failure boundary
        print("Intent workflow step failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
