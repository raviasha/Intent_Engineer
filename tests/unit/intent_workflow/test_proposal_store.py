"""Descriptor-safe, canonical persistence for intent workflow proposals."""

from __future__ import annotations

import json
import multiprocessing
import os
import stat
import traceback
from contextlib import contextmanager
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

from intent_engineering.core.models import ChangeSet
from intent_engineering.intent_workflow.models import (
    IntentProposal,
    ProposalDecision,
    ProposalKind,
)
from intent_engineering.intent_workflow.proposal_store import (
    IntentProposalStore,
    IntentProposalStoreError,
)
from intent_engineering.storage import secure as secure_storage
from intent_engineering.storage.secure import SecureDirectory
from intent_engineering.storage.transaction import LocalTransactionCoordinator

NOW = datetime(2026, 8, 26, tzinfo=UTC)


def _identity(prefix: str, material: dict[str, object]) -> str:
    encoded = json.dumps(
        material,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"{prefix}:sha256:{sha256(encoded).hexdigest()}"


def _changeset() -> ChangeSet:
    return ChangeSet(
        id="changeset:proposal-store",
        actor="local:asha",
        timestamp=NOW,
        baseline_graph_version=3,
        evidence_refs=("evidence:prd:v1",),
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


def _proposal(*, assumption: str = "CSV supports UTF-8") -> IntentProposal:
    material: dict[str, object] = {
        "schema_version": 1,
        "kind": "requirement",
        "proposed_by": "local:asha",
        "proposed_at": "2026-08-26T00:00:00Z",
        "baseline_graph_version": 3,
        "evidence_refs": ["evidence:prd:v1"],
        "source_roles": [],
        "changeset": _changeset().model_dump(mode="json"),
        "core_node_ids": [],
        "provisional_node_ids": [],
        "assumptions": [assumption],
        "unanswered_questions": [],
        "conflicting_authors": [],
        "destructive": False,
    }
    return IntentProposal(
        id=_identity("proposal", material),
        kind=ProposalKind.REQUIREMENT,
        proposed_by="local:asha",
        proposed_at=NOW,
        baseline_graph_version=3,
        evidence_refs=("evidence:prd:v1",),
        source_roles=(),
        changeset=_changeset(),
        assumptions=(assumption,),
    )


def _decision(proposal: IntentProposal, **updates: object) -> ProposalDecision:
    material: dict[str, object] = {
        "schema_version": 1,
        "proposal_id": proposal.id,
        "proposal_digest": proposal.digest,
        "actor": "local:ben",
        "actor_aliases": ["local:ben", "github:ben"],
        "decided_at": "2026-08-26T00:00:00Z",
        "action": "confirm",
        "baseline_graph_version": 3,
    }
    material.update(updates)
    return ProposalDecision.model_validate_json(
        json.dumps(
            {"id": _identity("proposal-decision", material), **material},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )


def _canonical_record(
    sequence: int,
    *,
    proposal: IntentProposal | None = None,
    decision: ProposalDecision | None = None,
) -> bytes:
    return (
        json.dumps(
            {
                "decision": None if decision is None else decision.model_dump(mode="json"),
                "proposal": None if proposal is None else proposal.model_dump(mode="json"),
                "schema_version": 1,
                "sequence": sequence,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )


@pytest.fixture
def proposal() -> IntentProposal:
    return _proposal()


@pytest.fixture
def proposal_store(tmp_path: Path) -> IntentProposalStore:
    path = tmp_path / "intent-proposals.jsonl"
    path.write_bytes(b"")
    return IntentProposalStore(path)


def test_proposal_store_is_canonical_idempotent_and_append_only(
    proposal_store: IntentProposalStore,
    proposal: IntentProposal,
) -> None:
    """Catches duplicate appends, rewritten history, and noncanonical envelope bytes."""
    assert proposal_store.put(proposal) is True
    before = proposal_store.bytes()
    assert before == _canonical_record(0, proposal=proposal)
    assert proposal_store.put(proposal) is False
    assert proposal_store.bytes() == before
    assert proposal_store.get(proposal.id) == proposal
    assert proposal_store.list() == (proposal,)


def test_decision_binds_exact_proposal_actor_and_graph_version(
    proposal_store: IntentProposalStore,
    proposal: IntentProposal,
) -> None:
    """Catches decisions detached from the exact proposal digest or baseline version."""
    contributor_decision = _decision(proposal)
    proposal_store.put(proposal)

    assert proposal_store.decide(contributor_decision) is True
    before = proposal_store.bytes()
    assert before == _canonical_record(0, proposal=proposal) + _canonical_record(
        1, decision=contributor_decision
    )
    assert proposal_store.decide(contributor_decision) is False
    assert proposal_store.bytes() == before
    assert proposal_store.decision_for(proposal.id) == contributor_decision


@pytest.mark.parametrize(
    "updates",
    [
        {"proposal_id": "proposal:sha256:" + "0" * 64},
        {"proposal_digest": "sha256:" + "1" * 64},
        {"baseline_graph_version": 4},
    ],
)
def test_unbound_decision_is_fixed_failure_and_never_rewrites(
    proposal_store: IntentProposalStore,
    proposal: IntentProposal,
    updates: dict[str, object],
) -> None:
    """Catches accepting a validly hashed decision for the wrong proposal snapshot."""
    proposal_store.put(proposal)
    before = proposal_store.bytes()

    with pytest.raises(IntentProposalStoreError) as caught:
        proposal_store.decide(_decision(proposal, **updates))

    assert caught.value.args == ("intent proposal ledger unavailable",)
    assert caught.value.__context__ is None
    assert proposal_store.bytes() == before


def test_second_distinct_decision_for_one_proposal_is_rejected_without_rewrite(
    proposal_store: IntentProposalStore,
    proposal: IntentProposal,
) -> None:
    """Catches silently replacing or appending a second terminal human decision."""
    first = _decision(proposal)
    second = _decision(proposal, action="reject")
    proposal_store.put(proposal)
    proposal_store.decide(first)
    before = proposal_store.bytes()

    with pytest.raises(IntentProposalStoreError):
        proposal_store.decide(second)

    assert proposal_store.bytes() == before


def _watchdog_probe(path: str, connection: Any) -> None:
    try:
        IntentProposalStore(Path(path)).list()
    except IntentProposalStoreError as error:
        connection.send(
            "fixed-unavailable"
            if error.args == ("intent proposal ledger unavailable",) and error.__context__ is None
            else "unsafe-error"
        )
    except Exception:  # noqa: BLE001 - the child reports every unexpected public failure
        connection.send("unexpected-error")
    else:
        connection.send("accepted")
    finally:
        connection.close()


def _run_with_watchdog(path: Path, seconds: float) -> str:
    context = multiprocessing.get_context("fork")
    receiving, sending = context.Pipe(duplex=False)
    process = context.Process(target=_watchdog_probe, args=(str(path), sending))
    process.start()
    sending.close()
    process.join(seconds)
    if process.is_alive():
        process.terminate()
        process.join()
        receiving.close()
        return "blocked"
    result = receiving.recv() if receiving.poll() else "no-result"
    receiving.close()
    return str(result)


@pytest.mark.parametrize(
    "attack",
    [
        "fifo",
        "symlink",
        "hardlink",
        "duplicate-key",
        "unterminated",
        "noncanonical",
        "sequence-gap",
        "duplicate-proposal",
    ],
)
def test_fifo_symlink_hardlink_and_malformed_ledgers_fail_without_blocking_or_rewrite(
    tmp_path: Path,
    proposal: IntentProposal,
    attack: str,
) -> None:
    """Catches blocking special-file reads and normalization of hostile durable bytes."""
    ledger = tmp_path / "intent-proposals.jsonl"
    outside = tmp_path / "outside.jsonl"
    raw: bytes | None = None
    if attack == "fifo":
        os.mkfifo(ledger)
    elif attack == "symlink":
        outside.write_bytes(b"PRIVATE-SYMLINK-PAYLOAD")
        ledger.symlink_to(outside)
    elif attack == "hardlink":
        outside.write_bytes(b"PRIVATE-HARDLINK-PAYLOAD")
        os.link(outside, ledger)
    elif attack == "duplicate-key":
        raw = b'{"schema_version":1,"sequence":0,"sequence":1,"proposal":null,"decision":null}\n'
        ledger.write_bytes(raw)
    elif attack == "unterminated":
        raw = b'{"private":"PRIVATE-UNTERMINATED-PAYLOAD"}'
        ledger.write_bytes(raw)
    elif attack == "noncanonical":
        raw = _canonical_record(0, proposal=proposal).replace(b'"sequence":0', b'"sequence": 0')
        ledger.write_bytes(raw)
    elif attack == "sequence-gap":
        raw = _canonical_record(1, proposal=proposal)
        ledger.write_bytes(raw)
    else:
        raw = _canonical_record(0, proposal=proposal) + _canonical_record(1, proposal=proposal)
        ledger.write_bytes(raw)

    before = os.lstat(ledger)
    outside_before = outside.read_bytes() if outside.exists() else None

    assert _run_with_watchdog(ledger, seconds=1.0) == "fixed-unavailable"
    after = os.lstat(ledger)
    assert (after.st_dev, after.st_ino, stat.S_IFMT(after.st_mode)) == (
        before.st_dev,
        before.st_ino,
        stat.S_IFMT(before.st_mode),
    )
    if raw is not None:
        assert ledger.read_bytes() == raw
    if outside_before is not None:
        assert outside.read_bytes() == outside_before


@pytest.mark.parametrize(
    "replacement",
    [
        (b',"schema_version":1,"sequence":0}', b',"schema_version":true,"sequence":0}'),
        (b',"schema_version":1,"sequence":0}', b',"schema_version":1.0,"sequence":0}'),
        (b'"proposed_at":"2026-08-26T00:00:00Z"', b'"proposed_at":"2026-08-26T00:00:00+00:00"'),
    ],
)
def test_typed_noncanonical_representations_are_rejected_without_rewrite(
    tmp_path: Path,
    proposal: IntentProposal,
    replacement: tuple[bytes, bytes],
) -> None:
    """Catches raw-canonical JSON that strict models coerce or normalize on replay."""
    path = tmp_path / "intent-proposals.jsonl"
    raw = _canonical_record(0, proposal=proposal).replace(*replacement)
    assert raw != _canonical_record(0, proposal=proposal)
    path.write_bytes(raw)

    with pytest.raises(IntentProposalStoreError) as caught:
        IntentProposalStore(path).list()

    assert caught.value.args == ("intent proposal ledger unavailable",)
    assert caught.value.__context__ is None
    assert path.read_bytes() == raw


def _repository_traceback_locals(error: BaseException) -> str:
    frames: list[str] = []
    current = error.__traceback__
    while current is not None:
        if "/src/intent_engineering/" in current.tb_frame.f_code.co_filename:
            frames.append(repr(current.tb_frame.f_locals))
        current = current.tb_next
    return "".join(frames)


def test_append_interruption_clears_raw_proposal_from_repository_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches cancellation retaining proposal bodies in repository frame locals."""
    secret = "PRIVATE-INTENT-PROPOSAL-8197"
    proposal = _proposal(assumption=secret)
    store = IntentProposalStore(tmp_path / "intent-proposals.jsonl")
    interruption = KeyboardInterrupt("append interrupted")

    def interrupt_append(_source: object, _line: bytes) -> None:
        raise interruption

    monkeypatch.setattr(
        "intent_engineering.intent_workflow.proposal_store.append_durable_line",
        interrupt_append,
    )

    with pytest.raises(KeyboardInterrupt) as caught:
        store.put(proposal)

    assert caught.value is interruption
    assert secret not in _repository_traceback_locals(caught.value)


def test_read_interruption_clears_decoded_proposal_from_repository_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches lock-exit cancellation retaining a decoded proposal in public read frames."""
    secret = "PRIVATE-INTENT-READ-8197"
    store = IntentProposalStore(tmp_path / "intent-proposals.jsonl")
    store.put(_proposal(assumption=secret))
    original_locked = store._locked
    interruption = KeyboardInterrupt("read interrupted")

    @contextmanager
    def interrupt_after_read() -> Any:
        with original_locked():
            yield
        raise interruption

    monkeypatch.setattr(store, "_locked", interrupt_after_read)

    with pytest.raises(KeyboardInterrupt) as caught:
        store.list()

    assert caught.value is interruption
    assert secret not in _repository_traceback_locals(caught.value)


def test_low_level_append_interruption_clears_payload_from_shared_storage_frames(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches the actual write loop retaining proposal bytes after interruption."""
    secret = "PRIVATE-LOW-LEVEL-APPEND-8197"
    path = tmp_path / "intent-proposals.jsonl"
    store = IntentProposalStore(path)
    before = store.bytes()
    interruption = KeyboardInterrupt("low-level append interrupted")
    original_write = secure_storage.os.write

    def interrupt_private_write(descriptor: int, content: object) -> int:
        if secret.encode() in bytes(content):
            raise interruption
        return original_write(descriptor, content)  # type: ignore[arg-type]

    monkeypatch.setattr(secure_storage.os, "write", interrupt_private_write)

    with pytest.raises(KeyboardInterrupt) as caught:
        store.put(_proposal(assumption=secret))

    assert caught.value is interruption
    assert caught.value.__context__ is None
    assert secret not in _repository_traceback_locals(caught.value)
    assert path.read_bytes() == before


def test_low_level_nonblocking_read_interruption_clears_accumulated_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches the actual read loop retaining completed private chunks after interruption."""
    secret = "PRIVATE-LOW-LEVEL-READ-8197"
    path = tmp_path / "intent-proposals.jsonl"
    store = IntentProposalStore(path)
    store.put(_proposal(assumption=secret))
    before = path.read_bytes()
    interruption = KeyboardInterrupt("low-level read interrupted")
    original_read = secure_storage.os.read
    observed_private = False

    def interrupt_after_private_read(descriptor: int, size: int) -> bytes:
        nonlocal observed_private
        if observed_private:
            raise interruption
        content = original_read(descriptor, size)
        if secret.encode() in content:
            observed_private = True
        return content

    monkeypatch.setattr(secure_storage.os, "read", interrupt_after_private_read)

    with pytest.raises(KeyboardInterrupt) as caught:
        store.list()

    assert caught.value is interruption
    assert caught.value.__context__ is None
    assert secret not in _repository_traceback_locals(caught.value)
    assert path.read_bytes() == before


def _fifo_swap_before_append_probe(path: str, proposal_json: str, connection: Any) -> None:
    store = IntentProposalStore(Path(path))
    original_decode = store._decode_unlocked

    def decode_then_swap() -> Any:
        state = original_decode()
        os.unlink(path)
        os.mkfifo(path)
        metadata = os.lstat(path)
        connection.send(("swapped", metadata.st_dev, metadata.st_ino))
        return state

    store._decode_unlocked = decode_then_swap  # type: ignore[method-assign]
    try:
        store.put(IntentProposal.model_validate_json(proposal_json))
    except IntentProposalStoreError as error:
        result = (
            "fixed-unavailable"
            if error.args == ("intent proposal ledger unavailable",)
            and error.__context__ is None
            else "unsafe-error"
        )
    except Exception:  # noqa: BLE001 - child reports any unexpected public failure
        result = "unexpected-error"
    else:
        result = "accepted"
    connection.send(("result", result))
    connection.close()


def test_fifo_replacement_after_decode_before_append_fails_fast_without_mutation(
    tmp_path: Path,
) -> None:
    """Catches an authenticated regular ledger being swapped to a blocking FIFO before append."""
    path = tmp_path / "intent-proposals.jsonl"
    path.write_bytes(b"")
    proposal = _proposal()
    context = multiprocessing.get_context("fork")
    receiving, sending = context.Pipe(duplex=False)
    process = context.Process(
        target=_fifo_swap_before_append_probe,
        args=(str(path), proposal.model_dump_json(), sending),
    )
    process.start()
    sending.close()
    assert receiving.poll(1.0)
    swapped = receiving.recv()
    assert swapped[0] == "swapped"
    process.join(1.0)
    result = "blocked"
    if process.is_alive():
        process.terminate()
        process.join()
    elif receiving.poll():
        result = receiving.recv()[1]
    receiving.close()

    after = os.lstat(path)
    assert result == "fixed-unavailable"
    assert stat.S_ISFIFO(after.st_mode)
    assert (after.st_dev, after.st_ino) == swapped[1:]


def test_corrupt_secret_bearing_ledger_raises_fixed_error_without_context_or_rewrite(
    tmp_path: Path,
) -> None:
    """Catches raw durable payload escaping through error args, context, or traceback locals."""
    secret = "PRIVATE-INTENT-LEDGER-8197"
    path = tmp_path / "intent-proposals.jsonl"
    raw = json.dumps({"secret": secret}, separators=(",", ":")).encode() + b"\n"
    path.write_bytes(raw)
    store = IntentProposalStore(path)

    with pytest.raises(IntentProposalStoreError) as caught:
        store.list()

    assert caught.value.args == ("intent proposal ledger unavailable",)
    assert caught.value.__context__ is None
    assert secret not in _repository_traceback_locals(caught.value)
    assert path.read_bytes() == raw


def test_store_rejects_transaction_coordinator_for_a_different_held_file(
    tmp_path: Path,
) -> None:
    """Catches a proposal store escaping the shared transaction recovery target."""
    directory = SecureDirectory.open(tmp_path)
    intended = directory.file("intent-proposals.jsonl")
    wrong = directory.file("other.jsonl")
    journal = directory.file("journal.json")
    intended.atomic_write(b"")
    wrong.atomic_write(b"")
    transactions = LocalTransactionCoordinator(journal, {"intent_proposals": intended})
    try:
        with pytest.raises(ValueError, match="transaction target is unavailable"):
            IntentProposalStore(wrong, transactions=transactions)
    finally:
        intended.close()
        wrong.close()
        journal.close()
        directory.close()


def test_store_accepts_the_exact_transaction_target_and_recovers_before_read(
    tmp_path: Path,
) -> None:
    """Catches reads that bypass prepared-journal recovery in the shared target domain."""
    directory = SecureDirectory.open(tmp_path)
    target = directory.file("intent-proposals.jsonl")
    journal = directory.file("journal.json")
    proposal = _proposal()
    target.atomic_write(_canonical_record(0, proposal=proposal))

    def crash(stage: str) -> None:
        if stage == "target:intent_proposals":
            raise SystemExit()

    crashing = LocalTransactionCoordinator(
        journal,
        {"intent_proposals": target},
        fault_hook=crash,
    )
    with pytest.raises(SystemExit), crashing.transaction() as transaction:
        transaction.write("intent_proposals", b'{"torn":')
    recovered = LocalTransactionCoordinator(journal, {"intent_proposals": target})
    store_file = recovered.target_file("intent_proposals")
    try:
        store = IntentProposalStore(store_file, transactions=recovered)
        assert store.get(proposal.id) == proposal
        assert store.bytes() == _canonical_record(0, proposal=proposal)
    finally:
        store_file.close()
        target.close()
        journal.close()
        directory.close()


def test_public_failure_traceback_rendering_does_not_recover_private_payload(
    tmp_path: Path,
) -> None:
    """Catches exception rendering that recovers parser locals through chained state."""
    secret = "PRIVATE-INTENT-TRACEBACK-8197"
    path = tmp_path / "intent-proposals.jsonl"
    path.write_bytes(f'{{"secret":"{secret}"}}\n'.encode())

    with pytest.raises(IntentProposalStoreError) as caught:
        IntentProposalStore(path).list()

    rendered = "".join(traceback.format_exception(caught.value))
    assert secret not in rendered
