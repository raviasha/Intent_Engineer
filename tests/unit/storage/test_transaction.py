"""Crash-consistency and untrusted-journal tests for local canonical transactions."""

from __future__ import annotations

import base64
import json
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path

import pytest

from intent_engineering.storage import transaction as transaction_module
from intent_engineering.storage.secure import SecureDirectory, SecureFile, UnsafePathError
from intent_engineering.storage.transaction import (
    LocalTransaction,
    LocalTransactionCoordinator,
    LocalTransactionExtraReadPolicy,
    TransactionRecoveryError,
)


def test_only_live_issued_no_recovery_read_handle_is_authenticated(tmp_path: Path) -> None:
    """Catches forged, stale, recovering, write, or cross-coordinator handles gaining read trust."""
    coordinator, _paths, _journal = _coordinator(tmp_path)
    other_root = tmp_path / "other"
    other_root.mkdir()
    other, _other_paths, _other_journal = _coordinator(other_root)
    forged = LocalTransaction(coordinator, read_only=True)
    held: LocalTransaction | None = None
    try:
        assert coordinator.owns_active_no_recovery_read_transaction(forged) is False
        with coordinator.read_transaction() as recovering:
            assert coordinator.owns_active_no_recovery_read_transaction(recovering) is False
        with coordinator.transaction() as writing:
            assert coordinator.owns_active_no_recovery_read_transaction(writing) is False
        with coordinator.read_transaction_without_recovery() as issued:
            held = issued
            assert coordinator.owns_active_no_recovery_read_transaction(issued) is True
            assert other.owns_active_no_recovery_read_transaction(issued) is False
            with pytest.raises(ValueError, match="no-recovery read"):
                coordinator.recover()
            with pytest.raises(ValueError, match="no-recovery read"):
                coordinator.snapshot()
            with pytest.raises(ValueError, match="read-only snapshot unavailable"):
                coordinator.snapshot_without_recovery()
            with (
                pytest.raises(ValueError, match="no-recovery read"),
                coordinator.read_transaction(),
            ):
                pass
            with pytest.raises(ValueError, match="no-recovery read"), coordinator.transaction():
                pass
            with pytest.raises(ValueError, match="no-recovery read"), coordinator.coordinated():
                pass
        assert held is not None
        assert coordinator.owns_active_no_recovery_read_transaction(held) is False
    finally:
        forged._finish()
        other.close()
        coordinator.close()


def test_no_recovery_read_cancellation_retires_the_exact_handle(tmp_path: Path) -> None:
    """Catches cancellation leaving a formerly trusted read handle active or registered."""

    class Cancellation(BaseException):
        pass

    coordinator, _paths, _journal = _coordinator(tmp_path)
    signal = Cancellation()
    held: LocalTransaction | None = None
    try:
        with (
            pytest.raises(Cancellation) as caught,
            coordinator.read_transaction_without_recovery() as issued,
        ):
            held = issued
            assert coordinator.owns_active_no_recovery_read_transaction(issued) is True
            raise signal
        assert caught.value is signal
        assert held is not None
        assert coordinator.owns_active_no_recovery_read_transaction(held) is False
    finally:
        coordinator.close()


def test_no_recovery_read_preserves_primary_cancellation_across_lock_cleanup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches lock cleanup replacing cancellation or skipping later lock releases."""

    class Cancellation(BaseException):
        pass

    primary_marker = "PRIVATE-NO-RECOVERY-PRIMARY"
    cleanup_marker = "PRIVATE-NO-RECOVERY-CLEANUP"
    primary = Cancellation(primary_marker)
    primary.secret = primary_marker  # type: ignore[attr-defined]
    cleanup = Cancellation(cleanup_marker)
    cleanup.secret = cleanup_marker  # type: ignore[attr-defined]
    retained: list[object] = []
    entered: dict[str, int] = {}
    exited: dict[str, int] = {}
    original_lock = transaction_module.same_path_lock
    coordinator, _paths, _journal = _coordinator(tmp_path)
    held: LocalTransaction | None = None

    @contextmanager
    def hostile_lock(path: Path | SecureFile) -> Iterator[None]:
        name = path.name
        entered[name] = entered.get(name, 0) + 1
        try:
            with original_lock(path):
                yield
        finally:
            exited[name] = exited.get(name, 0) + 1
            if name == "history.jsonl":
                private_cleanup_material = cleanup_marker
                try:
                    if private_cleanup_material:
                        raise cleanup
                except BaseException as caught:
                    retained.append(caught.__traceback__)
                    raise

    monkeypatch.setattr(transaction_module, "same_path_lock", hostile_lock)
    try:
        with (
            pytest.raises(Cancellation) as caught,
            coordinator.read_transaction_without_recovery() as transaction,
        ):
            held = transaction
            private_primary_material = primary_marker
            try:
                if private_primary_material:
                    raise primary
            except BaseException as raised:
                retained.append(raised.__traceback__)
                raise

        assert caught.value is primary
        assert held is not None
        assert coordinator.owns_active_no_recovery_read_transaction(held) is False
        assert entered == {
            ".local-transaction.json": 1,
            "cases.jsonl": 1,
            "graph.yaml": 1,
            "history.jsonl": 1,
        }
        assert exited == entered
        for signal in (primary, cleanup):
            assert signal.args == ()
            assert signal.__dict__ == {}
            assert signal.__cause__ is None
            assert signal.__context__ is None
        assert retained
        assert primary_marker not in "\n".join(
            repr(frame.f_locals)
            for old_traceback in retained
            if old_traceback is not None
            for frame, _line in traceback.walk_tb(old_traceback)  # type: ignore[arg-type]
            if "/src/intent_engineering/" in frame.f_code.co_filename
        )
        assert cleanup_marker not in "\n".join(
            repr(frame.f_locals)
            for old_traceback in retained
            if old_traceback is not None
            for frame, _line in traceback.walk_tb(old_traceback)  # type: ignore[arg-type]
            if "/src/intent_engineering/" in frame.f_code.co_filename
        )
    finally:
        monkeypatch.setattr(transaction_module, "same_path_lock", original_lock)
        coordinator.close()


@pytest.mark.parametrize("cleanup_cancels", (False, True))
def test_no_recovery_read_selects_and_scrubs_ordinary_body_and_cleanup_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cleanup_cancels: bool,
) -> None:
    """Catches an ordinary primary being replaced or retained by hostile cleanup."""

    class Cancellation(BaseException):
        pass

    primary = RuntimeError("PRIVATE-NO-RECOVERY-ORDINARY-PRIMARY")
    primary.secret = "PRIVATE-NO-RECOVERY-ORDINARY-PRIMARY"  # type: ignore[attr-defined]
    cleanup: BaseException = (
        Cancellation("PRIVATE-NO-RECOVERY-CANCELLING-CLEANUP")
        if cleanup_cancels
        else RuntimeError("PRIVATE-NO-RECOVERY-ORDINARY-CLEANUP")
    )
    cleanup.secret = "PRIVATE-NO-RECOVERY-CLEANUP"  # type: ignore[attr-defined]
    retained: list[object] = []
    original_lock = transaction_module.same_path_lock
    coordinator, _paths, _journal = _coordinator(tmp_path)
    held: LocalTransaction | None = None

    @contextmanager
    def hostile_lock(path: Path | SecureFile) -> Iterator[None]:
        try:
            with original_lock(path):
                yield
        finally:
            if path.name == "history.jsonl":
                private_cleanup_material = cleanup.secret  # type: ignore[attr-defined]
                try:
                    if private_cleanup_material:
                        raise cleanup
                except BaseException as caught:
                    retained.append(caught.__traceback__)
                    raise

    monkeypatch.setattr(transaction_module, "same_path_lock", hostile_lock)
    expected = cleanup if cleanup_cancels else primary
    try:
        with (
            pytest.raises(BaseException) as caught,
            coordinator.read_transaction_without_recovery() as transaction,
        ):
            held = transaction
            private_primary_material = primary.secret  # type: ignore[attr-defined]
            try:
                if private_primary_material:
                    raise primary
            except BaseException as raised:
                retained.append(raised.__traceback__)
                raise

        assert caught.value is expected
        assert held is not None
        assert coordinator.owns_active_no_recovery_read_transaction(held) is False
        for signal in (primary, cleanup):
            assert signal.args == ()
            assert signal.__dict__ == {}
            assert signal.__cause__ is None
            assert signal.__context__ is None
        assert retained
        assert "PRIVATE-NO-RECOVERY" not in "\n".join(
            repr(frame.f_locals)
            for old_traceback in retained
            if old_traceback is not None
            for frame, _line in traceback.walk_tb(old_traceback)  # type: ignore[arg-type]
            if "/src/intent_engineering/" in frame.f_code.co_filename
        )
    finally:
        monkeypatch.setattr(transaction_module, "same_path_lock", original_lock)
        coordinator.close()


def test_no_recovery_read_scrubs_acquisition_cancellation_and_releases_prior_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches partial lock acquisition leaking cancellation or an earlier held lock."""

    class Cancellation(BaseException):
        pass

    marker = "PRIVATE-NO-RECOVERY-ACQUISITION"
    signal = Cancellation(marker)
    signal.secret = marker  # type: ignore[attr-defined]
    entered: list[str] = []
    released: list[str] = []
    retained: list[object] = []
    original_lock = transaction_module.same_path_lock
    coordinator, _paths, _journal = _coordinator(tmp_path)

    @contextmanager
    def hostile_lock(path: Path | SecureFile) -> Iterator[None]:
        entered.append(path.name)
        if len(entered) == 2:
            private_acquisition_material = marker
            try:
                if private_acquisition_material:
                    raise signal
            except BaseException as caught:
                retained.append(caught.__traceback__)
                raise
        with original_lock(path):
            try:
                yield
            finally:
                released.append(path.name)

    monkeypatch.setattr(transaction_module, "same_path_lock", hostile_lock)
    try:
        with (
            pytest.raises(Cancellation) as caught,
            coordinator.read_transaction_without_recovery(),
        ):
            raise AssertionError("lock acquisition cancellation reached the body")
        assert caught.value is signal
        assert len(entered) == 2
        assert released == entered[:1]
        assert signal.args == ()
        assert signal.__dict__ == {}
        assert signal.__cause__ is None
        assert signal.__context__ is None
        assert retained
        assert marker not in "\n".join(
            repr(frame.f_locals)
            for old_traceback in retained
            if old_traceback is not None
            for frame, _line in traceback.walk_tb(old_traceback)  # type: ignore[arg-type]
            if "/src/intent_engineering/" in frame.f_code.co_filename
        )
    finally:
        monkeypatch.setattr(transaction_module, "same_path_lock", original_lock)
        with coordinator.read_transaction_without_recovery():
            pass
        coordinator.close()


def _coordinator(
    tmp_path: Path,
    *,
    fault_hook: object | None = None,
) -> tuple[LocalTransactionCoordinator, dict[str, Path], Path]:
    root = SecureDirectory.open(tmp_path)
    paths = {
        "graph": tmp_path / "graph.yaml",
        "history": tmp_path / "history.jsonl",
        "cases": tmp_path / "cases.jsonl",
    }
    coordinator = LocalTransactionCoordinator(
        root.file(".local-transaction.json"),
        {name: root.file(path.name) for name, path in paths.items()},
        fault_hook=fault_hook,  # type: ignore[arg-type]
    )
    return coordinator, paths, tmp_path / ".local-transaction.json"


def _seed(paths: dict[str, Path]) -> dict[str, bytes | None]:
    paths["graph"].write_bytes(b"graph-before\n")
    paths["history"].write_bytes(b"history-before\n")
    return {name: path.read_bytes() if path.exists() else None for name, path in paths.items()}


def _mutate(coordinator: LocalTransactionCoordinator) -> None:
    with coordinator.transaction() as transaction:
        transaction.write("graph", b"graph-after\n")
        transaction.append("history", b"history-after\n")
        transaction.write("cases", b"cases-after\n")


def test_ordinary_exception_restores_exact_bytes_and_existence(tmp_path: Path) -> None:
    coordinator, paths, journal = _coordinator(tmp_path)
    before = _seed(paths)

    with (
        pytest.raises(
            RuntimeError,
            match="fixture failure",
        ),
        coordinator.transaction() as transaction,
    ):
        transaction.write("graph", b"changed\n")
        transaction.write("cases", b"created\n")
        raise RuntimeError("fixture failure")

    assert {
        name: path.read_bytes() if path.exists() else None for name, path in paths.items()
    } == before
    assert not journal.exists()


def test_nested_transaction_commits_through_one_outer_journal(tmp_path: Path) -> None:
    stages: list[str] = []
    coordinator, paths, journal = _coordinator(tmp_path, fault_hook=stages.append)
    _seed(paths)

    with coordinator.transaction() as outer:
        outer.write("graph", b"graph-after\n")
        with coordinator.transaction() as nested:
            nested.append("history", b"nested-after\n")

    assert stages.count("journal_prepared") == 1
    assert stages.count("journal_committed") == 1
    assert paths["graph"].read_bytes() == b"graph-after\n"
    assert paths["history"].read_bytes() == b"history-before\nnested-after\n"
    assert not journal.exists()


def test_caught_nested_failure_poisons_and_rolls_back_outer_transaction(
    tmp_path: Path,
) -> None:
    coordinator, paths, journal = _coordinator(tmp_path)
    before = _seed(paths)

    with (
        pytest.raises(ValueError, match="nested local transaction failed"),
        coordinator.transaction() as outer,
    ):
        outer.write("graph", b"graph-after\n")
        try:
            with coordinator.transaction() as nested:
                nested.append("history", b"nested-after\n")
                raise RuntimeError("caught nested failure")
        except RuntimeError:
            pass

    assert {
        name: path.read_bytes() if path.exists() else None for name, path in paths.items()
    } == before
    assert not journal.exists()


def test_nested_cancellation_uses_outer_rollback_policy_and_preserves_identity(
    tmp_path: Path,
) -> None:
    class Cancellation(BaseException):
        pass

    coordinator, paths, journal = _coordinator(tmp_path)
    before = _seed(paths)
    signal = Cancellation()

    with (
        pytest.raises(Cancellation) as caught,
        coordinator.transaction(rollback_base_exceptions=True) as outer,
    ):
        outer.write("graph", b"graph-after\n")
        with coordinator.transaction() as nested:
            nested.append("history", b"nested-after\n")
            raise signal

    assert caught.value is signal
    assert {
        name: path.read_bytes() if path.exists() else None for name, path in paths.items()
    } == before
    assert not journal.exists()


@pytest.mark.parametrize(
    "stage",
    ["journal_prepared", "target:graph", "target:history", "target:cases"],
)
def test_crash_after_each_precommit_durable_stage_recovers_exact_preimages(
    tmp_path: Path,
    stage: str,
) -> None:
    def crash(current: str) -> None:
        if current == stage:
            raise SystemExit()

    coordinator, paths, journal = _coordinator(tmp_path, fault_hook=crash)
    before = _seed(paths)

    with pytest.raises(SystemExit):
        _mutate(coordinator)

    assert journal.exists()
    recovery, _, _ = _coordinator(tmp_path)
    recovery.recover()

    assert {
        name: path.read_bytes() if path.exists() else None for name, path in paths.items()
    } == before
    assert not journal.exists()
    recovery.recover()


def test_stale_committed_journal_completes_without_replaying_preimages(tmp_path: Path) -> None:
    def crash(stage: str) -> None:
        if stage == "journal_committed":
            raise SystemExit()

    coordinator, paths, journal = _coordinator(tmp_path, fault_hook=crash)
    _seed(paths)

    with pytest.raises(SystemExit):
        _mutate(coordinator)

    after = {name: path.read_bytes() if path.exists() else None for name, path in paths.items()}
    assert after == {
        "graph": b"graph-after\n",
        "history": b"history-before\nhistory-after\n",
        "cases": b"cases-after\n",
    }
    recovery, _, _ = _coordinator(tmp_path)
    recovery.recover()

    assert {
        name: path.read_bytes() if path.exists() else None for name, path in paths.items()
    } == after
    assert not journal.exists()


def test_snapshot_recovers_before_returning_one_locked_cross_store_view(tmp_path: Path) -> None:
    """Validation must never parse torn targets before prepared-journal recovery."""
    extra = tmp_path / "evidence.jsonl"
    extra.write_bytes(b"evidence-before\n")

    def crash(stage: str) -> None:
        if stage == "target:history":
            raise SystemExit()

    coordinator, paths, journal = _coordinator(tmp_path, fault_hook=crash)
    before = _seed(paths)
    with pytest.raises(SystemExit), coordinator.transaction() as transaction:
        transaction.write("graph", b"torn: [")
        transaction.write("history", b'{"torn":')

    recovery, _, _ = _coordinator(tmp_path)
    root = SecureDirectory.open(tmp_path)
    snapshot = recovery.snapshot({"evidence": root.file(extra.name)})

    assert snapshot.recovered is True
    assert dict(snapshot.content) == {**before, "evidence": b"evidence-before\n"}
    assert not journal.exists()


def _bounded_authority_policy(
    *,
    max_bytes: int = 4,
    max_aggregate_bytes: int = 8,
) -> LocalTransactionExtraReadPolicy:
    return LocalTransactionExtraReadPolicy(
        max_bytes=max_bytes,
        nonblocking_regular=True,
        aggregate_group="authority",
        max_aggregate_bytes=max_aggregate_bytes,
    )


@pytest.mark.parametrize("stage", ["snapshot", "pre", "post"])
def test_bounded_nonblocking_extra_policy_applies_to_every_authority_read_stage(
    tmp_path: Path,
    stage: str,
) -> None:
    coordinator, paths, journal = _coordinator(tmp_path)
    before = _seed(paths)
    authority_path = tmp_path / "authority.yaml"
    authority_path.write_bytes(b"safe")
    root = SecureDirectory.open(tmp_path)
    authority = root.file(authority_path.name)
    extras = {"authority": authority}
    policies = {"authority": _bounded_authority_policy()}

    if stage == "snapshot":
        authority_path.write_bytes(b"large")
        with pytest.raises(UnsafePathError):
            coordinator.snapshot(extras, extra_read_policies=policies)
    else:
        snapshot = coordinator.snapshot(extras, extra_read_policies=policies)
        assert snapshot.content["authority"] == b"safe"
        if stage == "pre":
            authority_path.write_bytes(b"large")
        with (
            pytest.raises(UnsafePathError),
            coordinator.transaction(
                rollback_base_exceptions=True,
                extras=extras,
                extra_read_policies=policies,
            ) as transaction,
        ):
            if stage == "post":
                assert transaction.read_optional("authority") == b"safe"
            transaction.write("graph", b"changed\n")
            if stage == "post":
                authority_path.write_bytes(b"large")
            transaction.read_optional("authority")

    assert {
        name: path.read_bytes() if path.exists() else None for name, path in paths.items()
    } == before
    assert not journal.exists()


def test_bounded_extra_policy_enforces_one_aggregate_before_returning_snapshot(
    tmp_path: Path,
) -> None:
    coordinator, _paths, _journal = _coordinator(tmp_path)
    root = SecureDirectory.open(tmp_path)
    extras = {}
    policies = {}
    for name, content in (("authority_a", b"aaaa"), ("authority_b", b"bbbb")):
        (tmp_path / f"{name}.yaml").write_bytes(content)
        extras[name] = root.file(f"{name}.yaml")
        policies[name] = _bounded_authority_policy()

    exact = coordinator.snapshot(extras, extra_read_policies=policies)
    assert exact.content["authority_a"] == b"aaaa"
    assert exact.content["authority_b"] == b"bbbb"
    (tmp_path / "authority_c.yaml").write_bytes(b"c")
    extras["authority_c"] = root.file("authority_c.yaml")
    policies["authority_c"] = _bounded_authority_policy()

    with pytest.raises(UnsafePathError):
        coordinator.snapshot(extras, extra_read_policies=policies)


def test_bounded_extra_policy_enforces_aggregate_again_during_transaction(
    tmp_path: Path,
) -> None:
    coordinator, paths, journal = _coordinator(tmp_path)
    before = _seed(paths)
    root = SecureDirectory.open(tmp_path)
    extras = {}
    policies = {}
    for name in ("authority_a", "authority_b"):
        (tmp_path / f"{name}.yaml").write_bytes(b"safe")
        extras[name] = root.file(f"{name}.yaml")
        policies[name] = _bounded_authority_policy()
    extras["authority_c"] = root.file("authority_c.yaml")
    policies["authority_c"] = _bounded_authority_policy()
    snapshot = coordinator.snapshot(extras, extra_read_policies=policies)
    assert snapshot.content["authority_c"] is None

    with (
        pytest.raises(UnsafePathError),
        coordinator.transaction(
            rollback_base_exceptions=True,
            extras=extras,
            extra_read_policies=policies,
        ) as transaction,
    ):
        assert transaction.read_optional("authority_a") == b"safe"
        assert transaction.read_optional("authority_b") == b"safe"
        assert transaction.read_optional("authority_c") is None
        transaction.write("graph", b"changed\n")
        (tmp_path / "authority_c.yaml").write_bytes(b"x")
        transaction.read_optional("authority_c")

    assert {
        name: path.read_bytes() if path.exists() else None for name, path in paths.items()
    } == before
    assert not journal.exists()


def test_nested_extra_read_inherits_outer_bounded_policy(tmp_path: Path) -> None:
    coordinator, paths, journal = _coordinator(tmp_path)
    before = _seed(paths)
    authority_path = tmp_path / "authority.yaml"
    authority_path.write_bytes(b"safe")
    root = SecureDirectory.open(tmp_path)
    authority = root.file(authority_path.name)
    extras = {"authority": authority}

    with (
        pytest.raises(UnsafePathError),
        coordinator.transaction(
            rollback_base_exceptions=True,
            extras=extras,
            extra_read_policies={"authority": _bounded_authority_policy()},
        ) as transaction,
    ):
        transaction.write("graph", b"changed\n")
        authority_path.write_bytes(b"large")
        with coordinator.read_transaction(extras) as nested:
            nested.read_optional("authority")

    assert {
        name: path.read_bytes() if path.exists() else None for name, path in paths.items()
    } == before
    assert not journal.exists()


@pytest.mark.parametrize("race", ["create", "delete"])
def test_bounded_extra_policy_observes_missing_create_and_delete_races(
    tmp_path: Path,
    race: str,
) -> None:
    coordinator, paths, journal = _coordinator(tmp_path)
    before = _seed(paths)
    authority_path = tmp_path / "authority.yaml"
    if race == "delete":
        authority_path.write_bytes(b"safe")
    root = SecureDirectory.open(tmp_path)
    authority = root.file(authority_path.name)
    extras = {"authority": authority}
    policies = {"authority": _bounded_authority_policy()}
    expected = None if race == "create" else b"safe"
    snapshot = coordinator.snapshot(extras, extra_read_policies=policies)
    assert snapshot.content["authority"] == expected

    with (
        pytest.raises(ValueError, match="authority changed"),
        coordinator.transaction(
            rollback_base_exceptions=True,
            extras=extras,
            extra_read_policies=policies,
        ) as transaction,
    ):
        assert transaction.read_optional("authority") == expected
        transaction.write("graph", b"changed\n")
        if race == "create":
            authority_path.write_bytes(b"safe")
        else:
            authority_path.unlink()
        if transaction.read_optional("authority") != expected:
            raise ValueError("authority changed")

    assert {
        name: path.read_bytes() if path.exists() else None for name, path in paths.items()
    } == before
    assert authority_path.exists() is (race == "create")
    assert not journal.exists()


def test_bounded_extra_policy_preserves_cancellation_identity_and_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Cancellation(BaseException):
        pass

    coordinator, paths, journal = _coordinator(tmp_path)
    before = _seed(paths)
    authority_path = tmp_path / "authority.yaml"
    authority_path.write_bytes(b"safe")
    root = SecureDirectory.open(tmp_path)
    authority = root.file(authority_path.name)
    signal = Cancellation()
    original_read = SecureFile.read_optional_nonblocking

    def cancel_authority(
        self: SecureFile,
        *,
        max_bytes: int | None = None,
    ) -> bytes | None:
        if self.name == authority_path.name:
            raise signal
        return original_read(self, max_bytes=max_bytes)

    monkeypatch.setattr(SecureFile, "read_optional_nonblocking", cancel_authority)

    with (
        pytest.raises(Cancellation) as caught,
        coordinator.transaction(
            rollback_base_exceptions=True,
            extras={"authority": authority},
            extra_read_policies={"authority": _bounded_authority_policy()},
        ) as transaction,
    ):
        transaction.write("graph", b"changed\n")
        transaction.read_optional("authority")

    assert caught.value is signal
    assert {
        name: path.read_bytes() if path.exists() else None for name, path in paths.items()
    } == before
    assert not journal.exists()


def test_legacy_extra_reads_remain_blocking_and_unbounded_by_default(tmp_path: Path) -> None:
    coordinator, _paths, _journal = _coordinator(tmp_path)
    content = b"x" * 2_097_152
    extra_path = tmp_path / "legacy-extra.bin"
    extra_path.write_bytes(content)
    root = SecureDirectory.open(tmp_path)
    extra = root.file(extra_path.name)

    snapshot = coordinator.snapshot({"legacy_extra": extra})
    assert snapshot.content["legacy_extra"] == content
    with coordinator.read_transaction({"legacy_extra": extra}) as transaction:
        assert transaction.read_optional("legacy_extra") == content


def _preimage(target: str, content: bytes | None) -> dict[str, object]:
    return {
        "target": target,
        "existed": content is not None,
        "content": None if content is None else base64.b64encode(content).decode("ascii"),
        "digest": f"sha256:{sha256(content or b'').hexdigest()}",
    }


def _valid_journal(paths: dict[str, Path]) -> dict[str, object]:
    return {
        "schema": "intent.local_transaction",
        "version": 1,
        "state": "prepared",
        "transaction_id": "a" * 64,
        "preimages": [
            _preimage(name, path.read_bytes() if path.exists() else None)
            for name, path in paths.items()
        ],
    }


@pytest.mark.parametrize(
    "corrupt",
    [
        lambda payload: payload.update({"unknown": True}),
        lambda payload: payload["preimages"].append(payload["preimages"][0]),
        lambda payload: payload["preimages"][0].update({"target": "../graph"}),
        lambda payload: payload["preimages"][0].update({"content": "%%%"}),
        lambda payload: payload["preimages"][0].update({"digest": "sha256:" + "0" * 64}),
    ],
)
def test_corrupt_or_untrusted_journal_is_rejected_without_target_mutation(
    tmp_path: Path,
    corrupt: object,
) -> None:
    coordinator, paths, journal = _coordinator(tmp_path)
    before = _seed(paths)
    payload = _valid_journal(paths)
    corrupt(payload)  # type: ignore[operator]
    journal.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

    with pytest.raises(TransactionRecoveryError, match="local transaction recovery failed"):
        coordinator.recover()

    assert {
        name: path.read_bytes() if path.exists() else None for name, path in paths.items()
    } == before
    assert journal.exists()


@pytest.mark.parametrize("operation", ("snapshot", "read_transaction"))
def test_no_recovery_reads_reject_journal_presence_without_reading_its_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    """Catches no-recovery paths parsing or byte-reading an unrecovered journal."""
    coordinator, paths, journal = _coordinator(tmp_path)
    _seed(paths)
    journal.write_bytes(b"PRIVATE malformed recovery material")
    original = SecureFile.read_optional_nonblocking

    def reject_journal_read(
        self: SecureFile,
        *,
        max_bytes: int | None = None,
    ) -> bytes | None:
        if self.name == journal.name:
            raise AssertionError("journal payload was read")
        return original(self, max_bytes=max_bytes)

    monkeypatch.setattr(SecureFile, "read_optional_nonblocking", reject_journal_read)

    with pytest.raises(TransactionRecoveryError, match="^local transaction recovery failed$"):
        if operation == "snapshot":
            coordinator.snapshot_without_recovery()
        else:
            with coordinator.read_transaction_without_recovery():
                raise AssertionError("unrecovered state became visible")

    assert journal.read_bytes() == b"PRIVATE malformed recovery material"


def test_no_recovery_snapshot_maps_unsafe_journal_entries_to_recovery_error(
    tmp_path: Path,
) -> None:
    """Catches journal metadata failures escaping instead of one fail-closed signal."""
    coordinator, paths, journal = _coordinator(tmp_path)
    _seed(paths)
    journal.symlink_to(paths["graph"])

    with pytest.raises(TransactionRecoveryError, match="^local transaction recovery failed$"):
        coordinator.snapshot_without_recovery()
