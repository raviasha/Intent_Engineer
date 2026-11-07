"""Version-dispatched candidate validation contracts."""

from __future__ import annotations

import traceback
from pathlib import Path

import pytest

from intent_engineering.team_state import candidate as candidate_module
from intent_engineering.team_state.authority import authority_digest
from intent_engineering.team_state.candidate import validate_candidate
from intent_engineering.team_state.models import TeamStateManifestV2
from intent_engineering.team_state.restore import StaticTrustProvider
from tests.helpers.shared_state import NOW, REPOSITORY_ID, keys
from tests.unit.team_state.test_authority import (
    _real_certificate,
    _RootStore,
    _verified_authority,
)


def test_candidate_dispatches_v2_without_changing_existing_ci_trust_shape(
    tmp_path: Path, monkeypatch
) -> None:
    """Catches routing v2 candidates through the schema-1 signer-pin parser."""
    _recipient, _signer, trust = keys()
    root_store = _RootStore(b"r" * 32)
    certificate, _private = _real_certificate(root_store)
    registry = _verified_authority(root_store, (certificate,), roles=("sponsor",))
    manifest = TeamStateManifestV2(
        project_id="project",
        repository_id=REPOSITORY_ID,
        graph_version=2,
        parent_bundle_digest="sha256:" + "1" * 64,
        bundle_digest="sha256:" + "2" * 64,
        bundle_size=100,
        recipient_key_ids=registry.active_recipient_key_ids(),
        authority_digest=authority_digest(registry),
        authority_epoch=registry.authority_epoch,
        root_key_id=registry.root.root_key_id,
        created_at=NOW,
    )
    base = "a" * 40
    head = "b" * 40
    calls: list[tuple[str, str]] = []
    close_failure: BaseException | None = None

    class Reader:
        _root = tmp_path

        def commit(self) -> str:
            return base

        def parents(self, commit: str) -> tuple[str, ...]:
            assert commit == head
            return (base,)

        def blob(self, commit: str, path: str, maximum: int) -> bytes:
            assert (
                commit == head
                and path == "manifest.json"
                and maximum >= len(manifest.canonical_bytes())
            )
            return manifest.canonical_bytes()

        def close(self) -> None:
            if close_failure is not None:
                raise close_failure

    monkeypatch.setattr(candidate_module, "_GitRefReader", lambda _root: Reader())
    monkeypatch.setattr(candidate_module, "_origin_repository", lambda _root: REPOSITORY_ID)

    def validate_v2(reader, loaded_trust, *, root: Path, base: str, head: str, at) -> None:
        assert reader.__class__ is Reader
        assert loaded_trust is trust
        assert root == tmp_path
        calls.append((base, head))

    monkeypatch.setattr(candidate_module, "_validate_v2_candidate", validate_v2)

    validate_candidate(
        tmp_path,
        StaticTrustProvider(trust),
        base=base,
        head=head,
        at=NOW,
    )

    assert calls == [(base, head)]

    close_failure = RuntimeError("PRIVATE-V2-CLOSE-ORDINARY-6103")
    with pytest.raises(ValueError, match="state candidate unavailable"):
        validate_candidate(
            tmp_path,
            StaticTrustProvider(trust),
            base=base,
            head=head,
            at=NOW,
        )

    class CloseCancelled(BaseException):
        pass

    close_marker = "PRIVATE-V2-CLOSE-CANCEL-6104"
    close_cancellation = CloseCancelled(close_marker)
    close_cancellation.private = close_marker
    close_cancellation.__cause__ = RuntimeError(close_marker)
    close_failure = close_cancellation
    with pytest.raises(CloseCancelled) as close_caught:
        validate_candidate(
            tmp_path,
            StaticTrustProvider(trust),
            base=base,
            head=head,
            at=NOW,
        )
    assert close_caught.value is close_cancellation
    assert close_caught.value.args == ()
    assert close_caught.value.__dict__ == {}
    assert close_caught.value.__cause__ is None
    assert close_caught.value.__context__ is None
    close_failure = None

    class Cancelled(BaseException):
        pass

    marker = "PRIVATE-V2-CANDIDATE-CANCEL-6104"
    cancellation = Cancelled(marker)
    cancellation.private = marker
    cancellation.__cause__ = RuntimeError(marker)

    def cancel(*_args, **_kwargs) -> None:
        raise cancellation

    monkeypatch.setattr(candidate_module, "_validate_v2_candidate", cancel)
    with pytest.raises(Cancelled) as caught:
        validate_candidate(
            tmp_path,
            StaticTrustProvider(trust),
            base=base,
            head=head,
            at=NOW,
        )
    assert caught.value is cancellation
    assert type(caught.value) is Cancelled
    assert caught.value.args == ()
    assert caught.value.__dict__ == {}
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    for frame, _line in traceback.walk_tb(caught.value.__traceback__):
        if frame.f_globals.get("__name__") != "intent_engineering.team_state.candidate":
            continue
        assert frame.f_locals.get("trust") is None
        assert frame.f_locals.get("files") == {}
        assert marker not in repr(frame.f_locals)


def test_candidate_scrubs_constructor_and_dual_cancellation_tracebacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches reader construction escaping the boundary or cleanup replacing a primary."""
    _recipient, _signer, trust = keys()
    base = "a" * 40
    head = "b" * 40

    class Cancelled(BaseException):
        pass

    constructor_marker = "PRIVATE-V2-READER-CONSTRUCTOR-8217"
    constructor_cancellation = Cancelled(constructor_marker)
    constructor_cancellation.private = constructor_marker
    constructor_cancellation.__cause__ = RuntimeError(constructor_marker)
    constructor_tracebacks = []

    def cancel_constructor(root: Path):
        _secret_root = (root, constructor_marker)
        try:
            raise constructor_cancellation
        except BaseException as caught:
            constructor_tracebacks.append(caught.__traceback__)
            raise

    monkeypatch.setattr(candidate_module, "_GitRefReader", cancel_constructor)
    with pytest.raises(Cancelled) as caught:
        validate_candidate(
            tmp_path,
            StaticTrustProvider(trust),
            base=base,
            head=head,
            at=NOW,
        )
    assert caught.value is constructor_cancellation
    assert caught.value.args == ()
    assert caught.value.__dict__ == {}
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert len(constructor_tracebacks) == 1
    for frame, _line in traceback.walk_tb(constructor_tracebacks[0]):
        assert constructor_marker not in repr(frame.f_locals)

    primary_marker = "PRIVATE-V2-CANDIDATE-PRIMARY-8218"
    cleanup_marker = "PRIVATE-V2-CANDIDATE-CLEANUP-8219"
    primary = Cancelled(primary_marker)
    primary.private = primary_marker
    primary.__cause__ = RuntimeError(primary_marker)
    cleanup = Cancelled(cleanup_marker)
    cleanup.private = cleanup_marker
    cleanup.__cause__ = RuntimeError(cleanup_marker)
    retained_tracebacks = []

    class Reader:
        def commit(self) -> str:
            return base

        def parents(self, commit: str) -> tuple[str, ...]:
            assert commit == head
            return (base,)

        def blob(self, _commit: str, _path: str, _maximum: int) -> bytes:
            return manifest.canonical_bytes()

        def close(self) -> None:
            _secret = cleanup_marker
            try:
                raise cleanup
            except BaseException as caught:
                retained_tracebacks.append(caught.__traceback__)
                raise

    def cancel_operation(*_args, **_kwargs) -> None:
        _secret = primary_marker
        try:
            raise primary
        except BaseException as caught:
            retained_tracebacks.append(caught.__traceback__)
            raise

    root_store = _RootStore(b"r" * 32)
    certificate, _private = _real_certificate(root_store)
    registry = _verified_authority(root_store, (certificate,), roles=("sponsor",))
    manifest = TeamStateManifestV2(
        project_id="project",
        repository_id=REPOSITORY_ID,
        graph_version=2,
        parent_bundle_digest="sha256:" + "1" * 64,
        bundle_digest="sha256:" + "2" * 64,
        bundle_size=100,
        recipient_key_ids=registry.active_recipient_key_ids(),
        authority_digest=authority_digest(registry),
        authority_epoch=registry.authority_epoch,
        root_key_id=registry.root.root_key_id,
        created_at=NOW,
    )
    monkeypatch.setattr(candidate_module, "_GitRefReader", lambda _root: Reader())
    monkeypatch.setattr(candidate_module, "_origin_repository", lambda _root: REPOSITORY_ID)
    monkeypatch.setattr(candidate_module, "_validate_v2_candidate", cancel_operation)
    with pytest.raises(Cancelled) as caught:
        validate_candidate(
            tmp_path,
            StaticTrustProvider(trust),
            base=base,
            head=head,
            at=NOW,
        )
    assert caught.value is primary
    for signal in (primary, cleanup):
        assert signal.args == ()
        assert signal.__dict__ == {}
        assert signal.__cause__ is None
        assert signal.__context__ is None
    assert len(retained_tracebacks) == 2
    for retained in retained_tracebacks:
        for frame, _line in traceback.walk_tb(retained):
            assert primary_marker not in repr(frame.f_locals)
            assert cleanup_marker not in repr(frame.f_locals)
