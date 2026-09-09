"""Crash-consistent local transactions over descriptor-rooted canonical files."""

from __future__ import annotations

import base64
import json
import re
import secrets
import traceback
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from threading import local
from types import MappingProxyType
from typing import Literal, NoReturn

from pydantic import ConfigDict, Field, ValidationError, model_validator

from intent_engineering.core.models._base import StrictModel
from intent_engineering.storage._atomic import same_path_lock
from intent_engineering.storage.secure import SecureFile, UnsafePathError

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


@dataclass(frozen=True)
class LocalTransactionExtraReadPolicy:
    """Opt-in limits for one descriptor-held read-only transaction extra."""

    max_bytes: int
    nonblocking_regular: bool
    aggregate_group: str | None = None
    max_aggregate_bytes: int | None = None

    def __post_init__(self) -> None:
        if (
            type(self.max_bytes) is not int
            or self.max_bytes < 0
            or self.nonblocking_regular is not True
            or (self.aggregate_group is None) != (self.max_aggregate_bytes is None)
            or (
                self.aggregate_group is not None
                and not _TARGET_PATTERN.fullmatch(self.aggregate_group)
            )
            or (
                self.max_aggregate_bytes is not None
                and (type(self.max_aggregate_bytes) is not int or self.max_aggregate_bytes < 0)
            )
        ):
            raise ValueError("invalid local transaction extra read policy")


class _ExtraReadBudget:
    """Track the latest retained size for each bounded extra within one lock scope."""

    def __init__(self, policies: Mapping[str, LocalTransactionExtraReadPolicy]) -> None:
        self._policies = dict(policies)
        self._sizes: dict[str, int] = {}
        self._totals: dict[str, int] = {}

    def read_optional(self, name: str, target: SecureFile) -> bytes | None:
        policy = self._policies.get(name)
        if policy is None:
            return target.read_optional()
        max_bytes = policy.max_bytes
        previous = self._sizes.get(name, 0)
        group = policy.aggregate_group
        if group is not None:
            aggregate_limit = policy.max_aggregate_bytes
            if aggregate_limit is None:  # pragma: no cover - constructor invariant
                raise ValueError("invalid local transaction extra read policy")
            retained_without_current = self._totals.get(group, 0) - previous
            max_bytes = min(max_bytes, aggregate_limit - retained_without_current)
            if max_bytes < 0:
                raise UnsafePathError()
        content = target.read_optional_nonblocking(max_bytes=max_bytes)
        current = 0 if content is None else len(content)
        if group is not None:
            aggregate_limit = policy.max_aggregate_bytes
            if aggregate_limit is None:  # pragma: no cover - constructor invariant
                raise ValueError("invalid local transaction extra read policy")
            next_total = self._totals.get(group, 0) - previous + current
            if next_total > aggregate_limit:
                content = None
                raise UnsafePathError()
            self._totals[group] = next_total
        self._sizes[name] = current
        return content


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
    scope: str | None = None
    postimages: tuple[_Preimage, ...] = ()


def _digest(content: bytes) -> str:
    return f"sha256:{sha256(content).hexdigest()}"


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


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


def _signal_chain(error: BaseException) -> tuple[BaseException, ...]:
    pending = [error]
    seen: set[int] = set()
    result: list[BaseException] = []
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        result.append(current)
        cause = current.__cause__
        context = current.__context__
        if cause is not None:
            pending.append(cause)
        if context is not None:
            pending.append(context)
    pending.clear()
    seen.clear()
    return tuple(result)


def _scrub_signal_chain(error: BaseException) -> None:
    for current in _signal_chain(error):
        _scrub_signal(current)


def _raise_signal(error: BaseException) -> NoReturn:
    try:
        raise error.with_traceback(None) from None
    except BaseException as caught:  # noqa: BLE001 - detach the suspended primary context
        caught.__traceback__ = None
        caught.__cause__ = None
        caught.__context__ = None
        error = BaseException()
        try:
            raise caught.with_traceback(None) from None
        finally:
            caught = BaseException()


class LocalTransaction:
    """The mutation handle yielded while a coordinator owns every target lock."""

    def __init__(
        self,
        owner: LocalTransactionCoordinator,
        extras: Mapping[str, SecureFile] | None = None,
        *,
        read_only: bool = False,
        extra_read_policies: Mapping[str, LocalTransactionExtraReadPolicy] | None = None,
        extra_read_budget: _ExtraReadBudget | None = None,
        _issued_by: object | None = None,
    ) -> None:
        self._owner = owner
        self._extras = dict(extras or {})
        self._read_only = read_only
        self._extra_read_policies = dict(extra_read_policies or {})
        self._extra_read_budget = extra_read_budget or _ExtraReadBudget(self._extra_read_policies)
        self._active = True
        self._issued_by = _issued_by

    def _target(self, name: str) -> SecureFile:
        if not self._active:
            raise RuntimeError("local transaction is no longer active")
        if self._read_only:
            raise ValueError("read-only local transaction")
        try:
            return self._owner._targets[name]
        except KeyError as error:
            raise ValueError("unknown local transaction target") from error

    def _read_target(self, name: str) -> SecureFile:
        if not self._active:
            raise RuntimeError("local transaction is no longer active")
        target = self._owner._targets.get(name)
        if target is not None:
            return target
        try:
            return self._extras[name]
        except KeyError as error:
            raise ValueError("unknown local transaction target") from error

    def read_optional(self, name: str) -> bytes | None:
        """Read one target while the transaction's complete lock set is held."""
        target = self._read_target(name)
        if name in self._extras:
            return self._extra_read_budget.read_optional(name, target)
        if self._read_only:
            return target.read_optional_nonblocking()
        return target.read_optional()

    def read(self, name: str) -> bytes:
        """Read one existing target while the transaction is active."""
        if name in self._extras:
            content = self.read_optional(name)
            if content is None:
                raise UnsafePathError()
            return content
        return self._read_target(name).read_bytes()

    def read_optional_bounded(self, name: str, *, max_bytes: int) -> bytes | None:
        """Read one held regular target only up to an exact caller-supplied bound."""
        if type(max_bytes) is not int or max_bytes < 0:
            raise ValueError("invalid local transaction read bound")
        if name in self._extras:
            raise ValueError("bounded canonical read cannot target an extra")
        return self._read_target(name).read_optional_nonblocking(max_bytes=max_bytes)

    def write(self, name: str, content: bytes) -> None:
        """Durably replace one target and expose its deterministic crash stage."""
        self._target(name)
        self._owner._record_recovery_write(name, content)
        self._owner._write_target(name, content)
        self._owner._fault(f"target:{name}")

    def append(self, name: str, content: bytes) -> None:
        """Durably append to one target and expose its deterministic crash stage."""
        target = self._target(name)
        target.append(content)
        self._owner._fault(f"target:{name}")

    def _finish(self) -> None:
        self._active = False
        self._read_only = True
        self._extras.clear()
        self._extra_read_policies.clear()


class _TransactionThreadState(local):
    """Track same-coordinator nesting without creating a second lock domain."""

    def __init__(self) -> None:
        self.active = False
        self.extras: dict[str, SecureFile] = {}
        self.extra_read_policies: dict[str, LocalTransactionExtraReadPolicy] = {}
        self.extra_read_budget: _ExtraReadBudget | None = None
        self.poisoned = False
        self.issued_write_transactions: list[LocalTransaction] = []
        self.issued_no_recovery_read_transactions: list[LocalTransaction] = []
        self.no_recovery_read_active = False


class LocalTransactionCoordinator:
    """Coordinate exact preimage recovery across a fixed local target set."""

    def __init__(
        self,
        journal: SecureFile,
        targets: Mapping[str, SecureFile],
        *,
        fault_hook: Callable[[str], None] | None = None,
        legacy_target_sets: Sequence[frozenset[str]] = (),
        recovery_scope: str | None = None,
        max_recovery_bytes: int | None = None,
        recovery_merges: Mapping[
            str, Callable[[bytes | None, bytes | None, bytes | None], bytes | None]
        ]
        | None = None,
        target_writers: Mapping[str, Callable[[SecureFile, bytes], None]] | None = None,
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
        self._thread_state = _TransactionThreadState()
        self.__transaction_authority = object()
        self.__no_recovery_read_authority = object()
        self._recovery_scope = recovery_scope
        self._max_recovery_bytes = max_recovery_bytes
        self._recovery_merges = dict(recovery_merges or {})
        self._target_writers = dict(target_writers or {})
        if set(self._recovery_merges) - set(targets):
            raise ValueError("invalid recovery merge target")
        self._prepared_journal: _Journal | None = None

    def close(self) -> None:
        """Release journal and target descriptors after all transactions quiesce."""
        self._journal.close()
        for target in self._targets.values():
            target.close()

    def _require_nested_extras(self, extras: Mapping[str, SecureFile]) -> None:
        if not self._thread_state.active:
            return
        held = self._thread_state.extras
        if any(
            name not in held or held[name].lock_key != file.lock_key
            for name, file in extras.items()
        ):
            raise ValueError("nested local transaction cannot expand targets")

    @staticmethod
    def _extra_read_policies(
        extras: Mapping[str, SecureFile],
        policies: Mapping[str, LocalTransactionExtraReadPolicy] | None,
    ) -> dict[str, LocalTransactionExtraReadPolicy]:
        result = dict(policies or {})
        if set(result) - set(extras) or any(
            type(policy) is not LocalTransactionExtraReadPolicy for policy in result.values()
        ):
            raise ValueError("invalid local transaction extra read policies")
        aggregate_limits: dict[str, int] = {}
        for policy in result.values():
            group = policy.aggregate_group
            limit = policy.max_aggregate_bytes
            if group is None or limit is None:
                continue
            existing = aggregate_limits.setdefault(group, limit)
            if existing != limit:
                raise ValueError("inconsistent local transaction aggregate read policy")
        return result

    def _nested_extra_read_state(
        self,
        extras: Mapping[str, SecureFile],
        requested: Mapping[str, LocalTransactionExtraReadPolicy],
    ) -> tuple[dict[str, LocalTransactionExtraReadPolicy], _ExtraReadBudget]:
        held = self._thread_state.extra_read_policies
        if any(name not in held or held[name] != policy for name, policy in requested.items()):
            raise ValueError("nested local transaction cannot change extra read policies")
        policies = {name: held[name] for name in extras if name in held}
        budget = self._thread_state.extra_read_budget
        if budget is None:  # pragma: no cover - active-state invariant
            raise ValueError("invalid nested local transaction read state")
        return policies, budget

    @staticmethod
    def _read_files(
        files: Mapping[str, SecureFile],
        policies: Mapping[str, LocalTransactionExtraReadPolicy],
        *,
        budget: _ExtraReadBudget | None = None,
    ) -> dict[str, bytes | None]:
        reads = budget or _ExtraReadBudget(policies)
        content: dict[str, bytes | None] = {}
        succeeded = False
        try:
            for name in sorted(files):
                if name in policies:
                    value = reads.read_optional(name, files[name])
                else:
                    value = files[name].read_optional_nonblocking()
                content[name] = value
                value = None
            succeeded = True
            return content
        finally:
            if not succeeded:
                content.clear()

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

    def owns_active_write_transaction(self, transaction: object) -> bool:
        """Authenticate one exact mutation handle issued for the current held lock scope."""
        return bool(
            type(transaction) is LocalTransaction
            and transaction._owner is self
            and transaction._issued_by is self.__transaction_authority
            and transaction._active
            and not transaction._read_only
            and self._thread_state.active
            and any(
                transaction is issued for issued in self._thread_state.issued_write_transactions
            )
        )

    def owns_active_no_recovery_read_transaction(self, transaction: object) -> bool:
        """Authenticate one exact read handle issued without recovery on this thread."""
        return bool(
            type(transaction) is LocalTransaction
            and transaction._owner is self
            and transaction._issued_by is self.__no_recovery_read_authority
            and transaction._active
            and transaction._read_only
            and self._thread_state.no_recovery_read_active
            and any(
                transaction is issued
                for issued in self._thread_state.issued_no_recovery_read_transactions
            )
        )

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
        if self._max_recovery_bytes is None:
            return {name: self._targets[name].read_optional() for name in sorted(self._targets)}
        result: dict[str, bytes | None] = {}
        remaining = self._max_recovery_bytes
        for name in sorted(self._targets):
            result[name] = self._targets[name].read_optional_nonblocking(max_bytes=remaining)
            if remaining is not None:
                remaining -= len(result[name] or b"")
        return result

    def _require_no_recovery_journal(self) -> None:
        """Reject any journal entry by held-directory metadata without reading its payload."""
        try:
            present = self._journal.exists()
        except UnsafePathError:
            raise TransactionRecoveryError() from None
        if present:
            raise TransactionRecoveryError() from None

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
            scope=self._recovery_scope,
        )

    @staticmethod
    def _journal_bytes(journal: _Journal) -> bytes:
        document = journal.model_dump(mode="json", by_alias=True)
        if journal.scope is None:
            document.pop("scope")
        if not journal.postimages:
            document.pop("postimages")
        return json.dumps(
            document,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    def _write_journal(self, journal: _Journal) -> None:
        if self._max_recovery_bytes is not None:
            retained = sum(
                len(record.content or "") * 3 // 4 - (record.content or "").count("=")
                for record in (*journal.preimages, *journal.postimages)
            )
            if retained > self._max_recovery_bytes:
                raise TransactionRecoveryError()
        payload = self._journal_bytes(journal)
        self._journal.atomic_write(payload)

    def _record_recovery_write(self, name: str, content: bytes) -> None:
        if name not in self._recovery_merges:
            return
        journal = self._prepared_journal
        if journal is None:
            raise TransactionRecoveryError()
        post = self._journal_for({name: content}, "prepared", journal.transaction_id).preimages[0]
        records = {p.target: p for p in journal.postimages}
        records[name] = post
        updated = journal.model_copy(
            update={"postimages": tuple(records[n] for n in sorted(records))}
        )
        self._write_journal(updated)
        self._prepared_journal = updated

    def _write_target(self, name: str, content: bytes) -> None:
        writer = self._target_writers.get(name)
        if writer is None:
            self._targets[name].atomic_write(content)
        else:
            writer(self._targets[name], content)

    def _read_journal(self) -> tuple[_Journal, dict[str, bytes | None]] | None:
        limit = self._max_recovery_bytes
        content = self._journal.read_optional_nonblocking(
            max_bytes=None if limit is None else (limit * 4 // 3) + 65536
        )
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
            if journal.scope != self._recovery_scope or (
                self._recovery_scope is not None and content != self._journal_bytes(journal)
            ):
                raise ValueError("journal recovery scope changed")
            names = tuple(record.target for record in journal.preimages)
            name_set = frozenset(names)
            if len(names) != len(name_set) or (
                name_set != frozenset(self._targets) and name_set not in self._legacy_target_sets
            ):
                raise ValueError("journal target set mismatch")
            preimages: dict[str, bytes | None] = {}
            total = 0
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
                total += len(decoded or b"")
                if limit is not None and total > limit:
                    raise ValueError("journal recovery size exceeded")
            post_names = tuple(record.target for record in journal.postimages)
            if len(set(post_names)) != len(post_names) or set(post_names) - set(
                self._recovery_merges
            ):
                raise ValueError("journal recovery target changed")
            for record in journal.postimages:
                decoded = base64.b64decode(record.content or "", validate=True)
                if (
                    _digest(decoded) != record.digest
                    or base64.b64encode(decoded).decode() != record.content
                ):
                    raise ValueError("journal recovery postimage changed")
                total += len(decoded)
                if limit is not None and total > limit:
                    raise ValueError("journal recovery size exceeded")
            return journal, preimages
        except (UnicodeError, json.JSONDecodeError, ValidationError, ValueError) as error:
            raise TransactionRecoveryError() from error

    def _restore(
        self, preimages: Mapping[str, bytes | None], journal: _Journal | None = None
    ) -> None:
        restored = dict(preimages)
        current = (
            self._snapshot()
            if self._max_recovery_bytes is not None or self._recovery_merges
            else {}
        )
        if journal is not None:
            touched = {record.target for record in journal.postimages}
            for name in self._recovery_merges.keys() - touched:
                # No write-ahead postimage means this transaction never touched
                # the shared target; an intervening writer owns its current bytes.
                restored.pop(name, None)
            for record in journal.postimages:
                post = base64.b64decode(record.content or "", validate=True)
                restored[record.target] = self._recovery_merges[record.target](
                    preimages[record.target], post, current[record.target]
                )
        for name in sorted(restored):
            content = restored[name]
            if content is None:
                self._targets[name].unlink(missing_ok=True)
            else:
                self._write_target(name, content)

    def _recover_unlocked(self) -> bool:
        loaded = self._read_journal()
        if loaded is None:
            return False
        journal, preimages = loaded
        try:
            if journal.state == "prepared":
                self._restore(preimages, journal)
            self._journal.unlink()
        except Exception as error:
            raise TransactionRecoveryError() from error
        return True

    def recover(self) -> None:
        """Recover a prepared transaction or finish a committed stale journal."""
        if self._thread_state.no_recovery_read_active:
            raise ValueError("recovery unavailable during no-recovery read")
        with self._locks():
            self._recover_unlocked()

    @contextmanager
    def coordinated(self) -> Iterator[None]:
        """Recover and serialize one non-transactional target operation."""
        if self._thread_state.no_recovery_read_active:
            raise ValueError("coordinated operation unavailable during no-recovery read")
        if self._thread_state.active:
            yield
            return
        with self._locks():
            self._recover_unlocked()
            yield

    def snapshot(
        self,
        extras: Mapping[str, SecureFile] | None = None,
        *,
        extra_read_policies: Mapping[str, LocalTransactionExtraReadPolicy] | None = None,
        target_names: Collection[str] | None = None,
    ) -> LocalTransactionSnapshot:
        """Recover, then read selected canonical targets under one deterministic lock set."""
        extra_files = dict(extras or {})
        policies = self._extra_read_policies(extra_files, extra_read_policies)
        selected_names = (
            frozenset(self._targets) if target_names is None else frozenset(target_names)
        )
        if any(type(name) is not str for name in selected_names) or not selected_names.issubset(
            self._targets
        ):
            raise ValueError("invalid local snapshot target selection")
        if any(not _TARGET_PATTERN.fullmatch(name) for name in extra_files):
            raise ValueError("invalid local snapshot targets")
        if set(extra_files) & set(self._targets):
            raise ValueError("duplicate local snapshot target")
        all_files = {**self._targets, **extra_files}
        selected_files = {
            **{name: self._targets[name] for name in selected_names},
            **extra_files,
        }
        lock_keys = [self._journal.lock_key, *(item.lock_key for item in all_files.values())]
        if len(lock_keys) != len(set(lock_keys)):
            raise ValueError("duplicate local snapshot target")
        if self._thread_state.no_recovery_read_active:
            raise ValueError("recovering snapshot unavailable during no-recovery read")
        if self._thread_state.active:
            self._require_nested_extras(extra_files)
            policies, budget = self._nested_extra_read_state(extra_files, policies)
            content = self._read_files(selected_files, policies, budget=budget)
            return LocalTransactionSnapshot(MappingProxyType(content), False)
        with self._locks(extra_files):
            recovered = self._recover_unlocked()
            content = self._read_files(selected_files, policies)
        return LocalTransactionSnapshot(MappingProxyType(content), recovered)

    def snapshot_without_recovery(
        self,
        extras: Mapping[str, SecureFile] | None = None,
        *,
        extra_read_policies: Mapping[str, LocalTransactionExtraReadPolicy] | None = None,
        target_names: Collection[str] | None = None,
    ) -> LocalTransactionSnapshot:
        """Read an exact snapshot only when no transaction requires recovery."""
        extra_files = dict(extras or {})
        policies = self._extra_read_policies(extra_files, extra_read_policies)
        selected_names = (
            frozenset(self._targets) if target_names is None else frozenset(target_names)
        )
        if any(type(name) is not str for name in selected_names) or not selected_names.issubset(
            self._targets
        ):
            raise ValueError("invalid local snapshot target selection")
        if any(not _TARGET_PATTERN.fullmatch(name) for name in extra_files):
            raise ValueError("invalid local snapshot targets")
        if (
            set(extra_files) & set(self._targets)
            or self._thread_state.active
            or self._thread_state.no_recovery_read_active
        ):
            raise ValueError("read-only snapshot unavailable")
        all_files = {**self._targets, **extra_files}
        selected_files = {
            **{name: self._targets[name] for name in selected_names},
            **extra_files,
        }
        lock_keys = [self._journal.lock_key, *(item.lock_key for item in all_files.values())]
        if len(lock_keys) != len(set(lock_keys)):
            raise ValueError("duplicate local snapshot target")
        with self._locks(extra_files):
            self._require_no_recovery_journal()
            content = self._read_files(selected_files, policies)
        return LocalTransactionSnapshot(MappingProxyType(content), False)

    @contextmanager
    def read_transaction(
        self,
        extras: Mapping[str, SecureFile] | None = None,
        *,
        extra_read_policies: Mapping[str, LocalTransactionExtraReadPolicy] | None = None,
    ) -> Iterator[LocalTransaction]:
        """Yield a read-only view while the complete canonical lock set remains held."""
        extra_files = dict(extras or {})
        policies = self._extra_read_policies(extra_files, extra_read_policies)
        if any(not _TARGET_PATTERN.fullmatch(name) for name in extra_files):
            raise ValueError("invalid local transaction extras")
        if set(extra_files) & set(self._targets):
            raise ValueError("duplicate local transaction target")
        all_files = {**self._targets, **extra_files}
        lock_keys = [self._journal.lock_key, *(item.lock_key for item in all_files.values())]
        if len(lock_keys) != len(set(lock_keys)):
            raise ValueError("duplicate local transaction target")
        if self._thread_state.no_recovery_read_active:
            raise ValueError("recovering read unavailable during no-recovery read")
        if self._thread_state.active:
            self._require_nested_extras(extra_files)
            policies, budget = self._nested_extra_read_state(extra_files, policies)
            transaction = LocalTransaction(
                self,
                extra_files,
                read_only=True,
                extra_read_policies=policies,
                extra_read_budget=budget,
            )
            try:
                yield transaction
            finally:
                transaction._finish()
            return
        with self._locks(extra_files):
            self._recover_unlocked()
            transaction = LocalTransaction(
                self,
                extra_files,
                read_only=True,
                extra_read_policies=policies,
            )
            try:
                yield transaction
            finally:
                transaction._finish()

    @contextmanager
    def read_transaction_without_recovery(
        self,
        extras: Mapping[str, SecureFile] | None = None,
        *,
        extra_read_policies: Mapping[str, LocalTransactionExtraReadPolicy] | None = None,
    ) -> Iterator[LocalTransaction]:
        """Hold a read-only view only when no transaction journal exists."""
        extra_files = dict(extras or {})
        policies = self._extra_read_policies(extra_files, extra_read_policies)
        if any(not _TARGET_PATTERN.fullmatch(name) for name in extra_files):
            raise ValueError("invalid local transaction extras")
        if (
            set(extra_files) & set(self._targets)
            or self._thread_state.active
            or self._thread_state.no_recovery_read_active
        ):
            raise ValueError("read-only transaction unavailable")
        all_files = {**self._targets, **extra_files}
        lock_keys = [self._journal.lock_key, *(item.lock_key for item in all_files.values())]
        if len(lock_keys) != len(set(lock_keys)):
            raise ValueError("duplicate local transaction target")
        transaction: LocalTransaction | None = None
        primary_signal: BaseException | None = None
        cleanup_signal: BaseException | None = None
        try:
            try:
                with self._locks(extra_files):
                    self._require_no_recovery_journal()
                    transaction = LocalTransaction(
                        self,
                        extra_files,
                        read_only=True,
                        extra_read_policies=policies,
                        _issued_by=self.__no_recovery_read_authority,
                    )
                    self._thread_state.issued_no_recovery_read_transactions.append(transaction)
                    self._thread_state.no_recovery_read_active = True
                    try:
                        yield transaction
                    except BaseException as caught:  # noqa: BLE001 - defer signal selection
                        primary_signal = caught
                    finally:
                        self._thread_state.no_recovery_read_active = False
                        try:
                            self._thread_state.issued_no_recovery_read_transactions.remove(
                                transaction
                            )
                        finally:
                            transaction._finish()
            except BaseException as caught:  # noqa: BLE001 - select after every lock cleanup
                cleanup_signal = caught
        finally:
            transaction = None
            extra_files.clear()
            policies.clear()
            all_files.clear()
        cleanup_cancellation = (
            next(
                (
                    signal
                    for signal in _signal_chain(cleanup_signal)
                    if not isinstance(signal, Exception)
                ),
                None,
            )
            if cleanup_signal is not None
            else None
        )
        selected: BaseException | None
        scrub = False
        if primary_signal is not None and not isinstance(primary_signal, Exception):
            selected = primary_signal
            scrub = True
        elif cleanup_cancellation is not None:
            selected = cleanup_cancellation
            scrub = True
        elif primary_signal is not None:
            selected = primary_signal
            scrub = cleanup_signal is not None
        else:
            selected = cleanup_signal
            scrub = cleanup_signal is not None and not isinstance(cleanup_signal, Exception)
        if scrub:
            if cleanup_signal is not None:
                _scrub_signal_chain(cleanup_signal)
            if primary_signal is not None:
                _scrub_signal_chain(primary_signal)
        primary_signal = None
        cleanup_signal = None
        cleanup_cancellation = None
        scrubbed = scrub
        scrub = False
        if selected is not None:
            detached = selected
            selected = None
            if scrubbed:
                _raise_signal(detached)
            raise detached

    @contextmanager
    def transaction(
        self,
        *,
        rollback_base_exceptions: bool = False,
        extras: Mapping[str, SecureFile] | None = None,
        extra_read_policies: Mapping[str, LocalTransactionExtraReadPolicy] | None = None,
    ) -> Iterator[LocalTransaction]:
        """Yield a mutation scope with durable targets and locked read-only extras."""
        extra_files = dict(extras or {})
        policies = self._extra_read_policies(extra_files, extra_read_policies)
        if any(not _TARGET_PATTERN.fullmatch(name) for name in extra_files):
            raise ValueError("invalid local transaction extras")
        if set(extra_files) & set(self._targets):
            raise ValueError("duplicate local transaction target")
        all_files = {**self._targets, **extra_files}
        lock_keys = [self._journal.lock_key, *(item.lock_key for item in all_files.values())]
        if len(lock_keys) != len(set(lock_keys)):
            raise ValueError("duplicate local transaction target")
        if self._thread_state.no_recovery_read_active:
            raise ValueError("write transaction unavailable during no-recovery read")
        if self._thread_state.active:
            self._require_nested_extras(extra_files)
            policies, budget = self._nested_extra_read_state(extra_files, policies)
            transaction = LocalTransaction(
                self,
                extra_files,
                extra_read_policies=policies,
                extra_read_budget=budget,
                _issued_by=self.__transaction_authority,
            )
            self._thread_state.issued_write_transactions.append(transaction)
            try:
                yield transaction
            except BaseException:
                self._thread_state.poisoned = True
                raise
            finally:
                self._thread_state.issued_write_transactions.remove(transaction)
                transaction._finish()
            return
        with self._locks(extra_files):
            self._recover_unlocked()
            preimages = self._snapshot()
            transaction_id = secrets.token_hex(32)
            prepared = self._journal_for(preimages, "prepared", transaction_id)
            self._write_journal(prepared)
            self._prepared_journal = prepared
            budget = _ExtraReadBudget(policies)
            transaction = LocalTransaction(
                self,
                extra_files,
                extra_read_policies=policies,
                extra_read_budget=budget,
                _issued_by=self.__transaction_authority,
            )
            self._thread_state.active = True
            self._thread_state.extras = extra_files
            self._thread_state.extra_read_policies = policies
            self._thread_state.extra_read_budget = budget
            self._thread_state.poisoned = False
            self._thread_state.issued_write_transactions.append(transaction)
            try:
                self._fault("journal_prepared")
                yield transaction
                if self._thread_state.poisoned:
                    raise ValueError("nested local transaction failed")
                committed = (self._prepared_journal or prepared).model_copy(
                    update={"state": "committed"}
                )
                self._write_journal(committed)
                self._fault("journal_committed")
                self._journal.unlink()
                self._fault("journal_cleaned")
            except Exception:
                try:
                    self._restore(preimages, self._prepared_journal)
                    self._journal.unlink(missing_ok=True)
                except Exception as recovery_error:
                    raise TransactionRecoveryError() from recovery_error
                raise
            except BaseException:
                if rollback_base_exceptions:
                    try:
                        self._restore(preimages, self._prepared_journal)
                        self._journal.unlink(missing_ok=True)
                    except Exception as recovery_error:
                        raise TransactionRecoveryError() from recovery_error
                raise
            finally:
                self._thread_state.issued_write_transactions.remove(transaction)
                transaction._finish()
                self._thread_state.active = False
                self._thread_state.extras = {}
                self._thread_state.extra_read_policies = {}
                self._thread_state.extra_read_budget = None
                self._thread_state.poisoned = False
                self._prepared_journal = None
