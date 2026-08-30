"""Durable, descriptor-safe WebAuthn authority-state coverage."""

from __future__ import annotations

import json
import multiprocessing
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from intent_engineering.control_plane.models import ChallengeRecord, CredentialRecord
from intent_engineering.control_plane.webauthn_store import (
    WebAuthnChallengeStore,
    WebAuthnCredentialStore,
)
from intent_engineering.storage.secure import SecureFile


def credential_record() -> CredentialRecord:
    return CredentialRecord(
        id="credential:primary",
        project_id="project",
        repository_id="repo:sha256:" + "a" * 64,
        actor="local",
        credential_id="credential_material",
        public_key="public_key_material",
        sign_count=0,
        created_at=datetime(2026, 8, 30, tzinfo=UTC),
    )


def challenge_record() -> ChallengeRecord:
    issued_at = datetime(2026, 8, 30, tzinfo=UTC)
    return ChallengeRecord(
        id="challenge:" + "b" * 64,
        project_id="project",
        repository_id="repo:sha256:" + "a" * 64,
        actor="local",
        ceremony="authentication",
        challenge="challenge_material",
        payload_digest="sha256:" + "c" * 64,
        issued_at=issued_at,
        expires_at=issued_at + timedelta(minutes=5),
    )


def credential_store(tmp_path: Path) -> WebAuthnCredentialStore:
    return WebAuthnCredentialStore(SecureFile.from_path(tmp_path / "credentials.jsonl"))


def challenge_store(tmp_path: Path) -> WebAuthnChallengeStore:
    return WebAuthnChallengeStore(SecureFile.from_path(tmp_path / "challenges.jsonl"))


def _consume_once(path: str, sending: Any) -> None:
    record = challenge_record()
    try:
        result = WebAuthnChallengeStore(SecureFile.from_path(Path(path))).consume(
            record.id, record.issued_at
        )
    except ValueError:
        sending.send("unavailable")
    else:
        sending.send("consumed" if result == record else "unexpected")
    finally:
        sending.close()


def _list_fifo(path: str, sending: Any) -> None:
    try:
        WebAuthnCredentialStore(SecureFile.from_path(Path(path))).list()
    except ValueError as error:
        sending.send((str(error), error.__context__ is None))
    else:
        sending.send(("accepted", False))
    finally:
        sending.close()


def test_credential_store_persists_canonical_record_once(tmp_path: Path) -> None:
    store = credential_store(tmp_path)
    record = credential_record()

    assert store.put(record)
    assert not store.put(record)
    assert store.list() == (record,)
    assert (tmp_path / "credentials.jsonl").read_bytes() == record.canonical_bytes()


def test_credential_store_rejects_noncanonical_or_conflicting_ledger(tmp_path: Path) -> None:
    record = credential_record()
    ledger = tmp_path / "credentials.jsonl"
    ledger.write_bytes(record.canonical_bytes().replace(b'"actor":"local"', b'"actor": "local"'))

    with pytest.raises(ValueError, match="credential ledger unavailable"):
        credential_store(tmp_path).list()


def test_challenge_is_consumed_exactly_once(tmp_path: Path) -> None:
    store = challenge_store(tmp_path)
    record = challenge_record()
    assert store.issue(record)
    assert store.consume(record.id, record.issued_at) == record
    with pytest.raises(ValueError, match="challenge unavailable"):
        store.consume(record.id, record.issued_at)


def test_challenge_store_replays_issue_and_consume_frames(tmp_path: Path) -> None:
    first = challenge_store(tmp_path)
    record = challenge_record()
    assert first.issue(record)

    second = challenge_store(tmp_path)
    assert second.consume(record.id, record.issued_at) == record
    assert not second.issue(record)


def test_challenge_store_rejects_expired_or_corrupt_ledger(tmp_path: Path) -> None:
    store = challenge_store(tmp_path)
    record = challenge_record()
    assert store.issue(record)

    with pytest.raises(ValueError, match="challenge unavailable"):
        store.consume(record.id, record.expires_at + timedelta(microseconds=1))

    (tmp_path / "challenges.jsonl").write_bytes(b'{"kind":"issue"}\n')
    with pytest.raises(ValueError, match="challenge unavailable"):
        challenge_store(tmp_path).issue(record)


def test_challenge_store_rejects_a_consume_frame_that_rebinds_the_issued_record(
    tmp_path: Path,
) -> None:
    store = challenge_store(tmp_path)
    record = challenge_record()
    assert store.issue(record)
    forged = json.loads(record.canonical_bytes())
    forged.update(
        kind="consume",
        actor="other",
        consumed_at="2026-08-30T00:00:00Z",
    )
    with (tmp_path / "challenges.jsonl").open("ab") as ledger:
        ledger.write(
            json.dumps(forged, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
                "utf-8"
            )
            + b"\n"
        )

    with pytest.raises(ValueError, match="challenge unavailable"):
        store.issue(record)


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_credential_store_rejects_linked_ledger_entries(tmp_path: Path, kind: str) -> None:
    ledger = tmp_path / "credentials.jsonl"
    outside = tmp_path / "outside.jsonl"
    outside.write_bytes(credential_record().canonical_bytes())
    if kind == "symlink":
        ledger.symlink_to(outside)
    else:
        os.link(outside, ledger)

    with pytest.raises(ValueError, match="credential ledger unavailable"):
        credential_store(tmp_path).list()


def test_credential_store_rejects_fifo_without_blocking_or_traceback_details(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "credentials.jsonl"
    os.mkfifo(ledger)
    before = os.lstat(ledger)
    context = multiprocessing.get_context("spawn")
    receiving, sending = context.Pipe(duplex=False)
    process = context.Process(target=_list_fifo, args=(str(ledger), sending))
    process.start()
    sending.close()
    process.join(2.0)
    if process.is_alive():
        process.terminate()
        process.join()
        pytest.fail("credential ledger FIFO read blocked")
    result = receiving.recv()
    receiving.close()

    after = os.lstat(ledger)
    assert result == ("credential ledger unavailable", True)
    assert stat.S_ISFIFO(after.st_mode)
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)


def test_challenge_store_allows_exactly_one_concurrent_consumer(tmp_path: Path) -> None:
    ledger = tmp_path / "challenges.jsonl"
    store = WebAuthnChallengeStore(SecureFile.from_path(ledger))
    assert store.issue(challenge_record())
    context = multiprocessing.get_context("spawn")
    receiving: list[Any] = []
    processes: list[multiprocessing.Process] = []
    for _ in range(2):
        receive, send = context.Pipe(duplex=False)
        process = context.Process(target=_consume_once, args=(str(ledger), send))
        process.start()
        send.close()
        receiving.append(receive)
        processes.append(process)
    for process in processes:
        process.join(5.0)
        assert process.exitcode == 0
    outcomes = sorted(connection.recv() for connection in receiving)
    for connection in receiving:
        connection.close()

    assert outcomes == ["consumed", "unavailable"]


def test_store_preserves_cancellation_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Cancelled(BaseException):
        pass

    cancellation = Cancelled()

    def cancel(*_args: object) -> None:
        raise cancellation

    monkeypatch.setattr(
        "intent_engineering.control_plane.webauthn_store.append_durable_line", cancel
    )
    with pytest.raises(Cancelled) as raised:
        credential_store(tmp_path).put(credential_record())

    assert raised.value is cancellation
