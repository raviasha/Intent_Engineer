"""Crash-consistent local transactions over descriptor-rooted canonical files."""

from __future__ import annotations

import base64
import json
import re
import secrets
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from pydantic import ConfigDict, Field, ValidationError, model_validator

from intent_engineering.core.models._base import StrictModel
from intent_engineering.storage._atomic import same_path_lock
from intent_engineering.storage.secure import SecureFile

_TARGET_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


class TransactionRecoveryError(ValueError):
    """A deliberately redacted failure to validate or restore a local journal."""

    def __init__(self) -> None:
        super().__init__("local transaction recovery failed")


@dataclass(frozen=True)
class LocalTransactionSnapshot:
    """An immutable cross-store byte view captured after raw transaction recovery."""

    content: Mapping[str, bytes | None]
    recovered: bool


class _Preimage(StrictModel):
    model_config = ConfigDict(frozen=True)

    target: str
    existed: bool
    content: str | None
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_content_shape(self) -> _Preimage:
        if not _TARGET_PATTERN.fullmatch(self.target):
            raise ValueError("invalid target")
        if self.existed is (self.content is None):
            raise ValueError("preimage existence and content disagree")
        return self


class _Journal(StrictModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)

    schema_name: Literal["intent.local_transaction"] = Field(alias="schema")
    version: Literal[1]
    state: Literal["prepared", "committed"]
    transaction_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    preimages: tuple[_Preimage, ...]


def _digest(content: bytes) -> str:
    return f"sha256:{sha256(content).hexdigest()}"


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


class LocalTransaction:
    """The mutation handle yielded while a coordinator owns every target lock."""

    def __init__(self, owner: LocalTransactionCoordinator) -> None:
        self._owner = owner
        self._active = True

    def _target(self, name: str) -> SecureFile:
        if not self._active:
            raise RuntimeError("local transaction is no longer active")
        try:
            return self._owner._targets[name]
        except KeyError as error:
            raise ValueError("unknown local transaction target") from error

    def read_optional(self, name: str) -> bytes | None:
        """Read one target while the transaction's complete lock set is held."""
        return self._target(name).read_optional()

    def read(self, name: str) -> bytes:
        """Read one existing target while the transaction is active."""
        return self._target(name).read_bytes()

    def write(self, name: str, content: bytes) -> None:
        """Durably replace one target and expose its deterministic crash stage."""
        target = self._target(name)
        target.atomic_write(content)
        self._owner._fault(f"target:{name}")

    def append(self, name: str, content: bytes) -> None:
        """Durably append to one target and expose its deterministic crash stage."""
        target = self._target(name)
        target.append(content)
        self._owner._fault(f"target:{name}")

    def _finish(self) -> None:
        self._active = False


class LocalTransactionCoordinator:
    """Coordinate exact preimage recovery across a fixed local target set."""

    def __init__(
        self,
        journal: SecureFile,
        targets: Mapping[str, SecureFile],
        *,
        fault_hook: Callable[[str], None] | None = None,
        legacy_target_sets: Sequence[frozenset[str]] = (),
    ) -> None:
        if not targets or any(not _TARGET_PATTERN.fullmatch(name) for name in targets):
            raise ValueError("invalid local transaction targets")
        if len({target.lock_key for target in targets.values()}) != len(targets):
            raise ValueError("duplicate local transaction target")
        if journal.lock_key in {target.lock_key for target in targets.values()}:
            raise ValueError("journal cannot also be a transaction target")
        current_names = frozenset(targets)
        legacy_sets = tuple(frozenset(names) for names in legacy_target_sets)
        if len(legacy_sets) != len(set(legacy_sets)) or any(
            not names
            or not names < current_names
            or any(not _TARGET_PATTERN.fullmatch(name) for name in names)
            for names in legacy_sets
        ):
            raise ValueError("invalid legacy transaction targets")
        self._journal = journal.duplicate()
        self._targets = {name: target.duplicate() for name, target in targets.items()}
        self._fault_hook = fault_hook
        self._legacy_target_sets = frozenset(legacy_sets)

    @property
    def target_names(self) -> frozenset[str]:
        """Expose only symbolic names so callers cannot escape to path-based I/O."""
        return frozenset(self._targets)

    @property
    def journal_path(self) -> Path:
        """Expose the diagnostic path label without using it for canonical I/O."""
        return self._journal.path

    def target_matches(self, name: str, target: SecureFile) -> bool:
        """Authenticate one store file against this coordinator's held target identity."""
        expected = self._targets.get(name)
        return expected is not None and expected.lock_key == target.lock_key

    def target_file(self, name: str) -> SecureFile:
        """Return a duplicate of one descriptor-held canonical transaction target."""
        try:
            return self._targets[name].duplicate()
        except KeyError as error:
            raise ValueError("unknown local transaction target") from error

    def _fault(self, stage: str) -> None:
        if self._fault_hook is not None:
            self._fault_hook(stage)

    @contextmanager
    def _locks(self, extras: Mapping[str, SecureFile] | None = None) -> Iterator[None]:
        entries = [("journal", self._journal), *self._targets.items()]
        if extras is not None:
            entries.extend(extras.items())
        entries.sort(key=lambda item: (*item[1].lock_key, item[0]))
        with ExitStack() as stack:
            for _, secure_file in entries:
                stack.enter_context(same_path_lock(secure_file))
            yield

    def _snapshot(self) -> dict[str, bytes | None]:
        return {name: self._targets[name].read_optional() for name in sorted(self._targets)}

    def _journal_for(
        self,
        preimages: Mapping[str, bytes | None],
        state: Literal["prepared", "committed"],
        transaction_id: str,
    ) -> _Journal:
        records: list[_Preimage] = []
        for name in sorted(preimages):
            content = preimages[name]
            records.append(
                _Preimage(
                    target=name,
                    existed=content is not None,
                    content=(
                        None if content is None else base64.b64encode(content).decode("ascii")
                    ),
                    digest=_digest(content or b""),
                )
            )
        return _Journal(
            schema="intent.local_transaction",
            version=1,
            state=state,
            transaction_id=transaction_id,
            preimages=tuple(records),
        )

    def _write_journal(self, journal: _Journal) -> None:
        payload = json.dumps(
            journal.model_dump(mode="json", by_alias=True),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        self._journal.atomic_write(payload)

    def _read_journal(self) -> tuple[_Journal, dict[str, bytes | None]] | None:
        content = self._journal.read_optional()
        if content is None:
            return None
        try:
            loaded = json.loads(
                content.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=lambda _value: (_ for _ in ()).throw(
                    ValueError("invalid JSON constant")
                ),
            )
            journal = _Journal.model_validate(loaded)
            names = tuple(record.target for record in journal.preimages)
            name_set = frozenset(names)
            if len(names) != len(name_set) or (
                name_set != frozenset(self._targets) and name_set not in self._legacy_target_sets
            ):
                raise ValueError("journal target set mismatch")
            preimages: dict[str, bytes | None] = {}
            for record in journal.preimages:
                if record.content is None:
                    decoded = None
                    digest_content = b""
                else:
                    decoded = base64.b64decode(record.content, validate=True)
                    if base64.b64encode(decoded).decode("ascii") != record.content:
                        raise ValueError("noncanonical base64")
                    digest_content = decoded
                if _digest(digest_content) != record.digest:
                    raise ValueError("preimage digest mismatch")
                preimages[record.target] = decoded
            return journal, preimages
        except (UnicodeError, json.JSONDecodeError, ValidationError, ValueError) as error:
            raise TransactionRecoveryError() from error

    def _restore(self, preimages: Mapping[str, bytes | None]) -> None:
        for name in sorted(preimages):
            content = preimages[name]
            if content is None:
                self._targets[name].unlink(missing_ok=True)
            else:
                self._targets[name].atomic_write(content)

    def _recover_unlocked(self) -> bool:
        loaded = self._read_journal()
        if loaded is None:
            return False
        journal, preimages = loaded
        try:
            if journal.state == "prepared":
                self._restore(preimages)
            self._journal.unlink()
        except Exception as error:
            raise TransactionRecoveryError() from error
        return True

    def recover(self) -> None:
        """Recover a prepared transaction or finish a committed stale journal."""
        with self._locks():
            self._recover_unlocked()

    @contextmanager
    def coordinated(self) -> Iterator[None]:
        """Recover and serialize one non-transactional target operation."""
        with self._locks():
            self._recover_unlocked()
            yield

    def snapshot(
        self,
        extras: Mapping[str, SecureFile] | None = None,
    ) -> LocalTransactionSnapshot:
        """Recover, then read every canonical target under one deterministic lock set."""
        extra_files = dict(extras or {})
        if any(not _TARGET_PATTERN.fullmatch(name) for name in extra_files):
            raise ValueError("invalid local snapshot targets")
        if set(extra_files) & set(self._targets):
            raise ValueError("duplicate local snapshot target")
        all_files = {**self._targets, **extra_files}
        lock_keys = [self._journal.lock_key, *(item.lock_key for item in all_files.values())]
        if len(lock_keys) != len(set(lock_keys)):
            raise ValueError("duplicate local snapshot target")
        with self._locks(extra_files):
            recovered = self._recover_unlocked()
            content = {name: all_files[name].read_optional() for name in sorted(all_files)}
        return LocalTransactionSnapshot(MappingProxyType(content), recovered)

    @contextmanager
    def transaction(
        self,
        *,
        rollback_base_exceptions: bool = False,
    ) -> Iterator[LocalTransaction]:
        """Yield a locked mutation scope with durable preimages and crash recovery."""
        with self._locks():
            self._recover_unlocked()
            preimages = self._snapshot()
            transaction_id = secrets.token_hex(32)
            prepared = self._journal_for(preimages, "prepared", transaction_id)
            self._write_journal(prepared)
            transaction = LocalTransaction(self)
            try:
                self._fault("journal_prepared")
                yield transaction
                committed = prepared.model_copy(update={"state": "committed"})
                self._write_journal(committed)
                self._fault("journal_committed")
                self._journal.unlink()
            except Exception:
                try:
                    self._restore(preimages)
                    self._journal.unlink(missing_ok=True)
                except Exception as recovery_error:
                    raise TransactionRecoveryError() from recovery_error
                raise
            except BaseException:
                if rollback_base_exceptions:
                    try:
                        self._restore(preimages)
                        self._journal.unlink(missing_ok=True)
                    except Exception as recovery_error:
                        raise TransactionRecoveryError() from recovery_error
                raise
            finally:
                transaction._finish()
