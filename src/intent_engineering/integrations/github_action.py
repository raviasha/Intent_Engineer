"""Thin, fail-closed steps used by the repository's GitHub workflows."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import anyio

from intent_engineering.cli.runtime import CheckRuntimeAdapter, load_runtime
from intent_engineering.intent_workflow.check import (
    SharedStateRestoreStatus,
    TestResultArtifact,
    validate_test_result_artifact,
)
from intent_engineering.intent_workflow.dev_observer import DevObserver, TestRunStatus
from intent_engineering.storage.secure import SecureDirectory
from intent_engineering.team_state.restore import EnvironmentTrustProvider, GitSharedStateRestorer

_STAGED_RESULT = Path(".intent-ci/reviewed-tests.json")
_RESULT = Path(".intent-ci/test-results.json")


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
    try:
        adapter.restore(require_shared=False)
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
        # The workflow restores the signed configuration in the preceding step.
        readiness = load_runtime(root)
        try:
            config = readiness.config
        finally:
            readiness.close()
        observer = DevObserver(
            root, config, repository_id=adapter.repository_id, principals=adapter.principals
        )
        if not observer.command_ids:
            raise ValueError("reviewed tests failed")
        revision = adapter.current_revision()
        completed: list[str] = []
        for identifier in observer.command_ids:
            result = await observer.run_reviewed_tests(identifier, at=at)
            if (
                result.status is not TestRunStatus.PASSED
                or result.artifact is None
                or result.artifact.commit_sha != revision
            ):
                raise ValueError("reviewed tests failed")
            completed.append(identifier)
        artifact = TestResultArtifact(
            repository_id=adapter.repository_id,
            commit_sha=revision,
            observed_at=at,
            status="passed",
            test_ids=tuple(completed),
            author=config.local_actor,
            acl=tuple(sorted(adapter.principals)),
        )
        _write(root, _STAGED_RESULT, artifact.canonical_bytes())
    finally:
        if observer is not None:
            observer.close()
        adapter.close()


def write_results(root: Path, at: datetime) -> None:
    """Revalidate repository, HEAD, time and ACL before writing canonical CI evidence."""
    adapter = CheckRuntimeAdapter(root)
    try:
        adapter.restore(require_shared=False)
        artifact = validate_test_result_artifact(
            adapter.read_test_results(_STAGED_RESULT),
            repository_id=adapter.repository_id,
            commit_sha=adapter.current_revision(),
            at=at,
            principals=adapter.principals,
        )
        _write(root, _RESULT, artifact.canonical_bytes())
    finally:
        adapter.close()


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
