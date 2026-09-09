"""Canonical, bounded persistence for progressive graph-enrichment sessions."""

from __future__ import annotations

import json
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from intent_engineering.intent_workflow import enrichment_store as enrichment_store_module
from intent_engineering.intent_workflow.enrichment_models import (
    EnrichmentEvent,
    EnrichmentSession,
)
from intent_engineering.intent_workflow.enrichment_store import (
    EnrichmentSessionStore,
    EnrichmentStoreError,
)

NOW = datetime(2026, 9, 9, tzinfo=UTC)
SNAPSHOT = "sha256:" + "1" * 64


def _session(**updates: object) -> EnrichmentSession:
    values: dict[str, object] = {
        "id": "refine:1",
        "status": "open",
        "focus_id": "requirement:workspace-owners",
        "budget_minutes": 5,
        "remaining_budget_seconds": 300,
        "snapshot_digest": SNAPSHOT,
        "current_gap_id": "gap:red-critical",
        "answered_gap_ids": (),
        "skipped_gap_ids": (),
        "answer_evidence_refs": (),
        "started_at": NOW,
        "updated_at": NOW,
    }
    values.update(updates)
    return EnrichmentSession.model_validate(values)


def _event(
    sequence: int,
    event_type: str,
    session: EnrichmentSession,
    *,
    predecessor_event_digest: str | None = None,
    gap_id: str | None = None,
    answer_evidence_ref: str | None = None,
) -> EnrichmentEvent:
    return EnrichmentEvent.model_validate(
        {
            "sequence": sequence,
            "predecessor_event_digest": predecessor_event_digest,
            "event_type": event_type,
            "session": session,
            "gap_id": gap_id,
            "answer_evidence_ref": answer_evidence_ref,
            "at": session.updated_at,
        }
    )


def test_session_requires_exactly_one_focus_or_budget_and_utc_time() -> None:
    with pytest.raises(ValidationError):
        _session(focus_id=None, budget_minutes=None)
    with pytest.raises(ValidationError):
        _session(updated_at=NOW.replace(tzinfo=None))
    with pytest.raises(ValidationError):
        _session(budget_minutes=10)
    with pytest.raises(ValidationError):
        _session(remaining_budget_seconds=301)
    with pytest.raises(ValidationError):
        _session(answered_gap_ids=("gap:one",), answer_evidence_refs=("WorkspaceOwners",))


def test_store_contains_references_not_answer_plaintext(tmp_path: Path) -> None:
    store = EnrichmentSessionStore(tmp_path / "enrichment-sessions.jsonl")
    opened = _event(0, "opened", _session())
    assert store.append(opened) is True

    answered_session = _session(
        answered_gap_ids=("gap:red-critical",),
        answer_evidence_refs=("evidence:answer:1",),
        current_gap_id=None,
        remaining_budget_seconds=280,
        updated_at=NOW + timedelta(seconds=20),
    )
    answered = _event(
        1,
        "answered",
        answered_session,
        predecessor_event_digest=opened.digest,
        gap_id="gap:red-critical",
        answer_evidence_ref="evidence:answer:1",
    )
    assert store.append(answered) is True

    assert b"PRIVATE ANSWER" not in store.bytes()
    assert store.latest("refine:1") == answered_session
    assert store.events("refine:1") == (opened, answered)
    store.close()


def test_store_is_idempotent_but_rejects_conflicting_or_noncontiguous_frames(
    tmp_path: Path,
) -> None:
    store = EnrichmentSessionStore(tmp_path / "enrichment-sessions.jsonl")
    opened = _event(0, "opened", _session())
    assert store.append(opened) is True
    assert store.append(opened) is False

    wrong_predecessor = _event(
        1,
        "paused",
        _session(status="paused", updated_at=NOW + timedelta(seconds=1)),
        predecessor_event_digest="sha256:" + "2" * 64,
    )
    with pytest.raises(EnrichmentStoreError, match="enrichment session ledger unavailable"):
        store.append(wrong_predecessor)

    skipped_sequence = _event(
        2,
        "paused",
        _session(status="paused", updated_at=NOW + timedelta(seconds=1)),
        predecessor_event_digest=opened.digest,
    )
    with pytest.raises(EnrichmentStoreError, match="enrichment session ledger unavailable"):
        store.append(skipped_sequence)


def test_store_rejects_invalid_lifecycle_and_cumulative_state(tmp_path: Path) -> None:
    store = EnrichmentSessionStore(tmp_path / "enrichment-sessions.jsonl")
    opened = _event(0, "opened", _session())
    store.append(opened)

    illegal_resume = _event(
        1,
        "resumed",
        _session(updated_at=NOW + timedelta(seconds=1)),
        predecessor_event_digest=opened.digest,
    )
    with pytest.raises(EnrichmentStoreError):
        store.append(illegal_resume)

    fabricated_answer = _event(
        1,
        "answered",
        _session(
            answered_gap_ids=("gap:red-critical",),
            answer_evidence_refs=("evidence:answer:other",),
            current_gap_id=None,
            updated_at=NOW + timedelta(seconds=1),
        ),
        predecessor_event_digest=opened.digest,
        gap_id="gap:red-critical",
        answer_evidence_ref="evidence:answer:1",
    )
    with pytest.raises(EnrichmentStoreError):
        store.append(fabricated_answer)


def test_lifecycle_only_events_cannot_rewrite_audited_session_facts(tmp_path: Path) -> None:
    store = EnrichmentSessionStore(tmp_path / "enrichment-sessions.jsonl")
    opened = _event(0, "opened", _session())
    store.append(opened)
    forged = _event(
        1,
        "paused",
        _session(
            status="paused",
            snapshot_digest="sha256:" + "2" * 64,
            current_gap_id=None,
            answered_gap_ids=("gap:red-critical",),
            answer_evidence_refs=("evidence:answer:forged",),
            remaining_budget_seconds=0,
            updated_at=NOW + timedelta(seconds=1),
        ),
        predecessor_event_digest=opened.digest,
    )

    with pytest.raises(EnrichmentStoreError):
        store.append(forged)

    assert store.latest("refine:1") == opened.session

    paused = _event(
        1,
        "paused",
        _session(
            status="paused", remaining_budget_seconds=299, updated_at=NOW + timedelta(seconds=1)
        ),
        predecessor_event_digest=opened.digest,
    )
    store.append(paused)
    forged_resume = _event(
        2,
        "resumed",
        _session(
            snapshot_digest="sha256:" + "3" * 64,
            current_gap_id="gap:attacker-selected",
            remaining_budget_seconds=299,
            updated_at=NOW + timedelta(minutes=5),
        ),
        predecessor_event_digest=paused.digest,
    )
    with pytest.raises(EnrichmentStoreError):
        store.append(forged_resume)


def test_budget_debits_only_whole_active_seconds_and_excludes_paused_time(tmp_path: Path) -> None:
    with pytest.raises(EnrichmentStoreError):
        EnrichmentSessionStore(tmp_path / "bad-opening.jsonl").append(
            _event(0, "opened", _session(remaining_budget_seconds=299))
        )

    store = EnrichmentSessionStore(tmp_path / "enrichment-sessions.jsonl")
    opened = _event(0, "opened", _session())
    store.append(opened)
    paused_session = _session(
        status="paused", remaining_budget_seconds=280, updated_at=NOW + timedelta(seconds=20)
    )
    paused = _event(1, "paused", paused_session, predecessor_event_digest=opened.digest)
    store.append(paused)
    resumed = _event(
        2,
        "resumed",
        _session(
            remaining_budget_seconds=280,
            updated_at=NOW + timedelta(minutes=10, seconds=20),
        ),
        predecessor_event_digest=paused.digest,
    )
    assert store.append(resumed) is True

    charged_pause = _event(
        3,
        "paused",
        _session(
            status="paused",
            remaining_budget_seconds=0,
            updated_at=NOW + timedelta(minutes=10, seconds=21),
        ),
        predecessor_event_digest=resumed.digest,
    )
    with pytest.raises(EnrichmentStoreError):
        store.append(charged_pause)


def test_store_rejects_noncanonical_duplicate_key_and_torn_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "enrichment-sessions.jsonl"
    opened = _event(0, "opened", _session())
    canonical = opened.model_dump(mode="json")
    path.write_text(json.dumps(canonical, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(EnrichmentStoreError):
        EnrichmentSessionStore(path)

    path.write_text('{"schema_version":1,"schema_version":1}\n', encoding="utf-8")
    with pytest.raises(EnrichmentStoreError):
        EnrichmentSessionStore(path)

    path.write_bytes(b'{"schema_version":1}')
    with pytest.raises(EnrichmentStoreError):
        EnrichmentSessionStore(path)


def test_events_are_bounded_and_cross_session_predecessors_are_rejected(tmp_path: Path) -> None:
    store = EnrichmentSessionStore(tmp_path / "enrichment-sessions.jsonl")
    first = _event(0, "opened", _session())
    store.append(first)
    other = _event(0, "opened", _session(id="refine:2", focus_id="requirement:other"))
    other = other.model_copy(update={"predecessor_event_digest": first.digest})
    with pytest.raises(EnrichmentStoreError):
        store.append(other)

    assert store.events() == (first,)
    with pytest.raises(EnrichmentStoreError):
        store.latest("refine:missing")


def test_append_rejects_a_frame_that_would_exceed_the_ledger_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = EnrichmentSessionStore(tmp_path / "enrichment-sessions.jsonl")
    opened = _event(0, "opened", _session())
    store.append(opened)
    before = store.bytes()
    monkeypatch.setattr(enrichment_store_module, "_MAX_LEDGER_BYTES", len(before))
    paused = _event(
        1,
        "paused",
        _session(status="paused", updated_at=NOW + timedelta(seconds=1)),
        predecessor_event_digest=opened.digest,
    )

    with pytest.raises(EnrichmentStoreError):
        store.append(paused)

    assert store.bytes() == before


def test_ledger_size_is_bounded_before_reading_file_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: list[int | None] = []
    original = enrichment_store_module.SecureFile.read_bytes_nonblocking

    def read(file: object, *, max_bytes: int | None = None) -> bytes:
        observed.append(max_bytes)
        return original(file, max_bytes=max_bytes)  # type: ignore[arg-type]

    monkeypatch.setattr(enrichment_store_module.SecureFile, "read_bytes_nonblocking", read)
    store = EnrichmentSessionStore(tmp_path / "enrichment-sessions.jsonl")
    store.bytes()

    assert observed
    assert all(value == enrichment_store_module._MAX_LEDGER_BYTES for value in observed)


def test_cancellation_identity_is_preserved_without_ledger_material(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = "PRIVATE_ANSWER_9271"
    cancellation = BaseException(marker)
    cancellation.secret = marker  # type: ignore[attr-defined]
    store = EnrichmentSessionStore(tmp_path / "enrichment-sessions.jsonl")
    event = _event(0, "opened", _session())

    def cancel(_file: object, _encoded: bytes) -> None:
        raise cancellation

    monkeypatch.setattr(enrichment_store_module, "append_durable_line", cancel)
    with pytest.raises(BaseException) as raised:
        store.append(event)

    assert raised.value is cancellation
    assert cancellation.args == ()
    assert cancellation.__dict__ == {}
    assert cancellation.__cause__ is None
    assert cancellation.__context__ is None
    traceback = cancellation.__traceback__
    while traceback is not None:
        if "/src/intent_engineering/intent_workflow/enrichment_store.py" in (
            traceback.tb_frame.f_code.co_filename
        ):
            assert marker not in repr(traceback.tb_frame.f_locals)
        traceback = traceback.tb_next


@pytest.mark.parametrize("method_name", ["latest", "events", "bytes"])
def test_read_cancellation_is_detached_from_parsed_ledger_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method_name: str
) -> None:
    marker = "PRIVATE_LEDGER_6142"
    cancellation = BaseException(marker)
    cancellation.secret = marker  # type: ignore[attr-defined]
    store = EnrichmentSessionStore(tmp_path / "enrichment-sessions.jsonl")

    def cancel() -> object:
        raise cancellation

    monkeypatch.setattr(store, "_decode_unlocked", cancel)
    method = getattr(store, method_name)
    with pytest.raises(BaseException) as raised:
        method("refine:1") if method_name != "bytes" else method()

    assert raised.value is cancellation
    assert cancellation.args == ()
    assert cancellation.__dict__ == {}
    assert cancellation.__cause__ is None
    assert cancellation.__context__ is None


def test_read_preserves_primary_cancellation_when_lock_cleanup_also_cancels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    primary_marker = "PRIVATE_READ_PRIMARY_7231"
    cleanup_marker = "PRIVATE_LOCK_CLEANUP_7232"
    primary = BaseException(primary_marker)
    primary.secret = primary_marker  # type: ignore[attr-defined]
    cleanup = BaseException(cleanup_marker)
    cleanup.secret = cleanup_marker  # type: ignore[attr-defined]
    retained: list[object] = []
    store = EnrichmentSessionStore(tmp_path / "enrichment-sessions.jsonl")

    def cancel_read() -> object:
        try:
            raise primary
        except BaseException as caught:
            retained.append(caught.__traceback__)
            raise

    def lock_with_failing_cleanup() -> object:
        @enrichment_store_module.contextmanager
        def manager() -> object:
            try:
                yield
            finally:
                try:
                    raise cleanup
                except BaseException as caught:
                    retained.append(caught.__traceback__)
                    raise

        return manager()

    monkeypatch.setattr(store, "_decode_unlocked", cancel_read)
    monkeypatch.setattr(store, "_raw_locked", lock_with_failing_cleanup, raising=False)

    with pytest.raises(BaseException) as raised:
        store.bytes()

    assert raised.value is primary
    for signal in (primary, cleanup):
        assert signal.args == ()
        assert signal.__dict__ == {}
        assert signal.__cause__ is None
        assert signal.__context__ is None
    for old_traceback in retained:
        current = old_traceback
        while current is not None:
            filename = current.tb_frame.f_code.co_filename  # type: ignore[union-attr]
            if "/src/intent_engineering/" in filename:
                values = repr(current.tb_frame.f_locals)  # type: ignore[union-attr]
                assert primary_marker not in values
                assert cleanup_marker not in values
            current = current.tb_next  # type: ignore[union-attr]


def test_lock_cleanup_cancellation_wins_over_an_ordinary_body_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ordinary = ValueError("PRIVATE_ORDINARY_1901")
    cleanup = BaseException("PRIVATE_CLEANUP_1902")
    cleanup.secret = "PRIVATE_CLEANUP_1902"  # type: ignore[attr-defined]
    store = EnrichmentSessionStore(tmp_path / "enrichment-sessions.jsonl")

    def fail_read() -> object:
        raise ordinary

    def lock_with_failing_cleanup() -> object:
        @enrichment_store_module.contextmanager
        def manager() -> object:
            try:
                yield
            finally:
                raise cleanup

        return manager()

    monkeypatch.setattr(store, "_decode_unlocked", fail_read)
    monkeypatch.setattr(store, "_raw_locked", lock_with_failing_cleanup)

    with pytest.raises(BaseException) as raised:
        store.bytes()

    assert raised.value is cleanup
    assert ordinary.args == ()
    assert cleanup.args == ()
    assert cleanup.__dict__ == {}
    assert cleanup.__cause__ is None
    assert cleanup.__context__ is None


def test_constructor_cancellation_is_detached_and_closes_its_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = "PRIVATE_CONSTRUCTOR_LEDGER_8812"
    cancellation = BaseException(marker)
    cancellation.secret = marker  # type: ignore[attr-defined]
    closed: list[bool] = []
    original_close = enrichment_store_module.SecureFile.close

    def cancel(_store: EnrichmentSessionStore) -> object:
        raise cancellation

    def close(file: object) -> None:
        closed.append(True)
        original_close(file)  # type: ignore[arg-type]

    monkeypatch.setattr(EnrichmentSessionStore, "_decode_unlocked", cancel)
    monkeypatch.setattr(enrichment_store_module.SecureFile, "close", close)

    with pytest.raises(BaseException) as raised:
        EnrichmentSessionStore(tmp_path / "enrichment-sessions.jsonl")

    assert raised.value is cancellation
    assert cancellation.args == ()
    assert cancellation.__dict__ == {}
    assert cancellation.__cause__ is None
    assert cancellation.__context__ is None
    assert closed


def test_constructor_scrubs_cancellation_from_secure_file_acquisition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = "PRIVATE_COERCE_7392"
    cancellation = BaseException(marker)
    cancellation.secret = marker  # type: ignore[attr-defined]

    def cancel(_path: object) -> object:
        raise cancellation

    monkeypatch.setattr(enrichment_store_module, "coerce_secure_file", cancel)
    with pytest.raises(BaseException) as raised:
        EnrichmentSessionStore(tmp_path / "enrichment-sessions.jsonl")

    assert raised.value is cancellation
    assert cancellation.args == ()
    assert cancellation.__dict__ == {}
    assert cancellation.__traceback__ is not None
    assert cancellation.__cause__ is None
    assert cancellation.__context__ is None


def test_constructor_preserves_primary_cancellation_and_scrubs_close_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    primary_marker = "PRIVATE_PRIMARY_4862"
    close_marker = "PRIVATE_CLOSE_4863"
    primary = BaseException(primary_marker)
    primary.secret = primary_marker  # type: ignore[attr-defined]
    close_signal = BaseException(close_marker)
    close_signal.secret = close_marker  # type: ignore[attr-defined]
    retained: list[object] = []
    original_close = enrichment_store_module.SecureFile.close

    def cancel(_store: EnrichmentSessionStore) -> object:
        try:
            raise primary
        except BaseException as caught:
            retained.append(caught.__traceback__)
            raise

    def close(file: object) -> None:
        if getattr(file, "_parent_fd", -1) < 0:
            return
        original_close(file)  # type: ignore[arg-type]
        try:
            raise close_signal
        except BaseException as caught:
            retained.append(caught.__traceback__)
            raise

    monkeypatch.setattr(EnrichmentSessionStore, "_decode_unlocked", cancel)
    monkeypatch.setattr(EnrichmentSessionStore, "_locked", lambda _store: nullcontext())
    monkeypatch.setattr(enrichment_store_module.SecureFile, "close", close)

    with pytest.raises(BaseException) as raised:
        EnrichmentSessionStore(tmp_path / "enrichment-sessions.jsonl")

    assert raised.value is primary
    for signal in (primary, close_signal):
        assert signal.args == ()
        assert signal.__dict__ == {}
        assert signal.__cause__ is None
        assert signal.__context__ is None
    for old_traceback in retained:
        current = old_traceback
        while current is not None:
            filename = current.tb_frame.f_code.co_filename  # type: ignore[union-attr]
            if "/src/intent_engineering/" in filename:
                values = repr(current.tb_frame.f_locals)  # type: ignore[union-attr]
                assert primary_marker not in values
                assert close_marker not in values
            current = current.tb_next  # type: ignore[union-attr]
