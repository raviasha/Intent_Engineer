"""Descriptor-safe append-only persistence for graph-enrichment sessions."""

from __future__ import annotations

import hashlib
import json
import traceback
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import NoReturn, cast

from pydantic import ValidationError

from intent_engineering.intent_workflow.enrichment_models import (
    EnrichmentEvent,
    EnrichmentProposalBinding,
    EnrichmentSession,
)
from intent_engineering.storage._atomic import append_durable_line, same_path_lock
from intent_engineering.storage.jsonl.strict import loads_strict_object
from intent_engineering.storage.secure import SecureFile, UnsafePathError, coerce_secure_file
from intent_engineering.storage.transaction import LocalTransaction, LocalTransactionCoordinator

_MAX_LEDGER_BYTES = 8 * 1024 * 1024
_MAX_EVENTS = 100_000


def _preimage_digest(content: bytes | None) -> str:
    framed = b"absent" if content is None else b"present\x00" + content
    return f"sha256:{hashlib.sha256(framed).hexdigest()}"


class EnrichmentStoreError(ValueError):
    """Fixed public failure for invalid or unavailable session state."""

    def __init__(self) -> None:
        super().__init__("enrichment session ledger unavailable")


@dataclass(frozen=True)
class _LedgerState:
    content: bytes
    events: tuple[EnrichmentEvent, ...]
    by_session: dict[str, tuple[EnrichmentEvent, ...]]


def _serialize(event: EnrichmentEvent) -> bytes:
    material = event.model_dump(mode="json")
    if event.reassessment_predecessor_digest is None:
        material.pop("reassessment_predecessor_digest")
    if event.operation_id is None:
        material.pop("operation_id")
    return (
        json.dumps(
            material,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )


def _valid_transition(previous: EnrichmentEvent | None, event: EnrichmentEvent) -> bool:
    session = event.session
    if previous is None:
        return (
            event.event_type == "opened"
            and session.status == "open"
            and not session.answered_gap_ids
            and not session.skipped_gap_ids
            and not session.answer_evidence_refs
            and session.started_at == session.updated_at
            and session.remaining_budget_seconds
            == (0 if session.budget_minutes is None else session.budget_minutes * 60)
        )
    before = previous.session
    immutable = {
        "status",
        "remaining_budget_seconds",
        "snapshot_digest",
        "current_gap_id",
        "answered_gap_ids",
        "skipped_gap_ids",
        "answer_evidence_refs",
        "updated_at",
    }
    elapsed = (event.at - previous.at).total_seconds()
    if elapsed < 0 or not elapsed.is_integer():
        return False
    expected_budget = (
        before.remaining_budget_seconds
        if before.status == "paused"
        else max(0, before.remaining_budget_seconds - int(elapsed))
    )
    if (
        event.sequence != previous.sequence + 1
        or event.predecessor_event_digest != previous.digest
        or before.model_dump(mode="json", exclude=immutable)
        != session.model_dump(mode="json", exclude=immutable)
        or session.remaining_budget_seconds != expected_budget
    ):
        return False
    if event.event_type == "answered":
        return (
            before.status == session.status == "open"
            and event.gap_id == before.current_gap_id
            and session.answered_gap_ids == (*before.answered_gap_ids, event.gap_id)
            and session.skipped_gap_ids == before.skipped_gap_ids
            and session.answer_evidence_refs
            == (*before.answer_evidence_refs, event.answer_evidence_ref)
            and session.snapshot_digest == before.snapshot_digest
        )
    if event.event_type == "skipped":
        return (
            before.status == session.status == "open"
            and event.gap_id == before.current_gap_id
            and session.skipped_gap_ids == (*before.skipped_gap_ids, event.gap_id)
            and session.answered_gap_ids == before.answered_gap_ids
            and session.answer_evidence_refs == before.answer_evidence_refs
            and session.snapshot_digest == before.snapshot_digest
        )
    if event.event_type == "paused":
        return (
            before.status == "open"
            and session.status == "paused"
            and session.snapshot_digest == before.snapshot_digest
            and session.current_gap_id == before.current_gap_id
            and session.answered_gap_ids == before.answered_gap_ids
            and session.skipped_gap_ids == before.skipped_gap_ids
            and session.answer_evidence_refs == before.answer_evidence_refs
        )
    if event.event_type == "resumed":
        return (
            before.status == "paused"
            and session.status == "open"
            and session.snapshot_digest == before.snapshot_digest
            and session.current_gap_id == before.current_gap_id
            and session.answered_gap_ids == before.answered_gap_ids
            and session.skipped_gap_ids == before.skipped_gap_ids
            and session.answer_evidence_refs == before.answer_evidence_refs
        )
    if event.event_type == "reassessed":
        return (
            before.status == session.status
            and before.status in {"open", "paused"}
            and event.reassessment_predecessor_digest == before.snapshot_digest
            and session.answered_gap_ids == before.answered_gap_ids
            and session.skipped_gap_ids == before.skipped_gap_ids
            and session.answer_evidence_refs == before.answer_evidence_refs
        )
    if event.event_type == "completed":
        return (
            before.status == "open"
            and session.status == "complete"
            and session.snapshot_digest == before.snapshot_digest
            and session.answered_gap_ids == before.answered_gap_ids
            and session.skipped_gap_ids == before.skipped_gap_ids
            and session.answer_evidence_refs == before.answer_evidence_refs
        )
    if event.event_type == "cancelled":
        return (
            before.status in {"open", "paused"}
            and session.status == "cancelled"
            and session.snapshot_digest == before.snapshot_digest
            and session.answered_gap_ids == before.answered_gap_ids
            and session.skipped_gap_ids == before.skipped_gap_ids
            and session.answer_evidence_refs == before.answer_evidence_refs
        )
    return False


def _parse(content: bytes) -> _LedgerState | None:
    if len(content) > _MAX_LEDGER_BYTES or (content and not content.endswith(b"\n")):
        return None
    events: list[EnrichmentEvent] = []
    sessions: dict[str, list[EnrichmentEvent]] = {}
    completed_operations: dict[str, set[str]] = {}
    current_operation: dict[str, str | None] = {}
    try:
        lines = content.splitlines(keepends=True)
        if len(lines) > _MAX_EVENTS:
            return None
        for encoded in lines:
            if not encoded.endswith(b"\n") or encoded == b"\n":
                return None
            line = encoded[:-1]
            loads_strict_object(line.decode("utf-8"))
            event = EnrichmentEvent.model_validate_json(line)
            if _serialize(event) != encoded:
                return None
            ledger = sessions.setdefault(event.session.id, [])
            operation_id = event.operation_id
            prior_operation = current_operation.get(event.session.id)
            if operation_id != prior_operation:
                if prior_operation is not None:
                    completed_operations.setdefault(event.session.id, set()).add(prior_operation)
                if operation_id is not None and operation_id in completed_operations.get(
                    event.session.id, set()
                ):
                    return None
                current_operation[event.session.id] = operation_id
            if not _valid_transition(ledger[-1] if ledger else None, event):
                return None
            ledger.append(event)
            events.append(event)
        return _LedgerState(
            content=content,
            events=tuple(events),
            by_session={key: tuple(value) for key, value in sessions.items()},
        )
    except (UnicodeError, TypeError, ValidationError, ValueError):
        return None


def _scrub_signal(error: BaseException) -> BaseException:
    old_traceback = error.__traceback__
    error.args = ()
    error.__dict__.clear()
    error.__traceback__ = None
    error.__cause__ = None
    error.__context__ = None
    if old_traceback is not None:
        traceback.clear_frames(old_traceback)
    old_traceback = None
    return error


def _raise_signal(error: BaseException) -> NoReturn:
    raise error.with_traceback(None) from None


class EnrichmentSessionStore:
    """Persist canonical session events without answer plaintext."""

    def __init__(
        self,
        path: Path | SecureFile,
        *,
        transactions: LocalTransactionCoordinator | None = None,
    ) -> None:
        owned: SecureFile | None = None
        failed = False
        target_mismatch = False
        signal: BaseException | None = None
        cleanup_signal: BaseException | None = None
        try:
            owned = coerce_secure_file(path)
            if transactions is not None and not transactions.target_matches(
                "enrichment_sessions", owned
            ):
                target_mismatch = True
            else:
                self._file = owned
                self._transactions = transactions
                self.path = owned.path
                with self._locked():
                    if self._decode_unlocked() is None:
                        failed = True
        except Exception:  # noqa: BLE001 - expose one fixed public integrity failure
            failed = True
        except BaseException as caught:  # noqa: BLE001 - preserve cancellation identity
            signal = _scrub_signal(caught)
        finally:
            if (failed or target_mismatch or signal is not None) and owned is not None:
                try:
                    owned.close()
                except Exception:  # noqa: BLE001 - preserve the fixed primary outcome
                    failed = True
                except BaseException as caught:  # noqa: BLE001 - scrub cleanup cancellation
                    cleanup_signal = _scrub_signal(caught)
            path = cast(Path | SecureFile, None)
            transactions = None
            owned = None
        if signal is not None:
            detached = signal
            signal = None
            cleanup_signal = None
            _raise_signal(detached)
        if cleanup_signal is not None:
            detached = cleanup_signal
            cleanup_signal = None
            _raise_signal(detached)
        if target_mismatch:
            raise ValueError("enrichment session transaction target is unavailable") from None
        if failed:
            raise EnrichmentStoreError() from None

    @contextmanager
    def _raw_locked(self) -> Iterator[None]:
        if self._transactions is None:
            with same_path_lock(self._file):
                yield
            return
        with self._transactions.coordinated(), same_path_lock(self._file):
            yield

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Preserve a body cancellation even when lock cleanup also fails."""
        manager = self._raw_locked()
        primary: BaseException | None = None
        primary_traceback: TracebackType | None = None
        cleanup: BaseException | None = None
        manager.__enter__()
        try:
            try:
                yield
            except BaseException as caught:  # noqa: BLE001 - defer through lock cleanup
                primary = caught
                primary_traceback = caught.__traceback__
        finally:
            try:
                manager.__exit__(
                    None if primary is None else type(primary),
                    primary,
                    primary_traceback,
                )
            except BaseException as caught:  # noqa: BLE001 - preserve primary cancellation
                cleanup = caught
        if primary is not None:
            if (
                cleanup is not None
                and isinstance(primary, Exception)
                and not isinstance(cleanup, Exception)
            ):
                _scrub_signal(primary)
                primary = None
                primary_traceback = None
                selected = _scrub_signal(cleanup)
                cleanup = None
                _raise_signal(selected)
            selected = _scrub_signal(primary)
            primary = None
            primary_traceback = None
            if cleanup is not None:
                _scrub_signal(cleanup)
                cleanup = None
            _raise_signal(selected)
        if cleanup is not None:
            selected = _scrub_signal(cleanup)
            cleanup = None
            _raise_signal(selected)

    def _content_unlocked(self) -> bytes | None:
        try:
            return self._file.read_bytes_nonblocking(max_bytes=_MAX_LEDGER_BYTES)
        except UnsafePathError:
            try:
                return b"" if not self._file.exists() else None
            except UnsafePathError:
                return None

    def _decode_unlocked(self) -> _LedgerState | None:
        content = self._content_unlocked()
        return None if content is None else _parse(content)

    def _append(self, event: EnrichmentEvent) -> bool:
        validated: EnrichmentEvent | None = None
        state: _LedgerState | None = None
        existing: tuple[EnrichmentEvent, ...] = ()
        encoded: bytes | None = None
        with self._locked():
            validated = EnrichmentEvent.model_validate_json(event.model_dump_json())
            state = self._decode_unlocked()
            if state is None:
                raise EnrichmentStoreError()
            existing = state.by_session.get(validated.session.id, ())
            if validated in existing:
                return False
            if not _valid_transition(existing[-1] if existing else None, validated):
                raise EnrichmentStoreError()
            encoded = _serialize(validated)
            if (
                len(state.events) >= _MAX_EVENTS
                or len(state.content) + len(encoded) > _MAX_LEDGER_BYTES
            ):
                raise EnrichmentStoreError()
            append_durable_line(self._file, encoded)
            return True

    def append(self, event: EnrichmentEvent) -> bool:
        """Append one exact event through a fixed, cancellation-safe boundary."""
        result = False
        failed = False
        signal: BaseException | None = None
        try:
            result = self._append(event)
        except Exception as caught:  # noqa: BLE001 - expose one fixed public integrity failure
            _scrub_signal(caught)
            failed = True
        except BaseException as caught:  # noqa: BLE001 - preserve cancellation identity
            signal = _scrub_signal(caught)
        finally:
            event = cast(EnrichmentEvent, None)
        if signal is not None:
            detached = signal
            signal = None
            _raise_signal(detached)
        if failed:
            raise EnrichmentStoreError() from None
        return result

    def latest(self, session_id: str) -> EnrichmentSession:
        result: EnrichmentSession | None = None
        failed = False
        signal: BaseException | None = None
        state: _LedgerState | None = None
        events: tuple[EnrichmentEvent, ...] | None = None
        try:
            with self._locked():
                state = self._decode_unlocked()
                events = None if state is None else state.by_session.get(session_id)
                if not events:
                    failed = True
                else:
                    result = events[-1].session
        except Exception:  # noqa: BLE001 - expose one fixed public integrity failure
            failed = True
        except BaseException as caught:  # noqa: BLE001 - preserve cancellation identity
            signal = _scrub_signal(caught)
        finally:
            session_id = ""
            state = None
            events = None
        if signal is not None:
            detached = signal
            signal = None
            _raise_signal(detached)
        if failed or result is None:
            raise EnrichmentStoreError() from None
        return result

    def events(self, session_id: str | None = None) -> tuple[EnrichmentEvent, ...]:
        result: tuple[EnrichmentEvent, ...] | None = None
        failed = False
        signal: BaseException | None = None
        state: _LedgerState | None = None
        try:
            with self._locked():
                state = self._decode_unlocked()
                if state is None:
                    failed = True
                elif session_id is None:
                    result = state.events
                else:
                    result = state.by_session.get(session_id, ())
        except Exception:  # noqa: BLE001 - expose one fixed public integrity failure
            failed = True
        except BaseException as caught:  # noqa: BLE001 - preserve cancellation identity
            signal = _scrub_signal(caught)
        finally:
            session_id = None
            state = None
        if signal is not None:
            detached = signal
            signal = None
            _raise_signal(detached)
        if failed or result is None:
            raise EnrichmentStoreError() from None
        return result

    def validate_proposal_binding(
        self,
        binding: EnrichmentProposalBinding,
        transaction: LocalTransaction,
        *,
        preimage_names: Mapping[str, str] | None = None,
    ) -> EnrichmentSession:
        """Require an exact latest session state inside the caller's held write scope."""
        aliases = dict(preimage_names or {})
        if (
            type(binding) is not EnrichmentProposalBinding
            or self._transactions is None
            or not self._transactions.owns_active_write_transaction(transaction)
            or not self._transactions.target_matches("enrichment_sessions", self._file)
            or set(aliases) - {"acl_policy", "config"}
            or any(type(value) is not str or not value for value in aliases.values())
        ):
            raise EnrichmentStoreError() from None
        try:
            validated = EnrichmentProposalBinding.model_validate_json(binding.model_dump_json())
            if validated != binding:
                raise ValueError("invalid enrichment proposal binding")
            content = (
                transaction.read_optional_bounded(
                    "enrichment_sessions", max_bytes=_MAX_LEDGER_BYTES
                )
                or b""
            )
            state = _parse(content)
            if state is None:
                raise ValueError("invalid enrichment proposal ledger")
            events = state.by_session.get(binding.session_id, ())
            latest = events[-1] if events else None
            session = None if latest is None else latest.session
            if (
                latest is None
                or session is None
                or latest.digest != binding.latest_event_digest
                or session.snapshot_digest != binding.snapshot_digest
                or session.answered_gap_ids != binding.answered_gap_ids
                or session.answer_evidence_refs != binding.answer_evidence_refs
                or tuple(
                    (
                        name,
                        _preimage_digest(transaction.read_optional(aliases.get(name, name))),
                    )
                    for name, _digest in binding.assessment_preimage_digests
                )
                != binding.assessment_preimage_digests
            ):
                raise ValueError("stale enrichment proposal binding")
            return session
        except Exception as caught:  # noqa: BLE001 - fixed internal trust boundary
            _scrub_signal(caught)
            raise EnrichmentStoreError() from None

    def bytes(self) -> bytes:
        result: bytes | None = None
        failed = False
        signal: BaseException | None = None
        state: _LedgerState | None = None
        try:
            with self._locked():
                state = self._decode_unlocked()
                if state is None:
                    failed = True
                else:
                    result = state.content
        except Exception:  # noqa: BLE001 - expose one fixed public integrity failure
            failed = True
        except BaseException as caught:  # noqa: BLE001 - preserve cancellation identity
            signal = _scrub_signal(caught)
        finally:
            state = None
        if signal is not None:
            detached = signal
            signal = None
            _raise_signal(detached)
        if failed or result is None:
            raise EnrichmentStoreError() from None
        return result

    def close(self) -> None:
        self._file.close()


__all__ = ["EnrichmentSessionStore", "EnrichmentStoreError"]
