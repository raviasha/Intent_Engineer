"""Unit contracts for process-local intent preflight capabilities."""

from __future__ import annotations

import json
import threading
import traceback
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

import intent_engineering.intent_workflow.authorization as authorization_module
from intent_engineering.core.models import Graph
from intent_engineering.intent_workflow.authorization import (
    AuthorizationError,
    AuthorizationGrant,
    AuthorizationIssuer,
    AuthorizationVerification,
)
from intent_engineering.intent_workflow.models import (
    PreflightResult,
    TaskClassification,
    TaskEnvelope,
)
from intent_engineering.storage.yaml.graph_store import serialize_graph

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


def _envelope(
    *,
    request: str = "Add CSV export",
    scope: tuple[str, ...] = ("src/export.py",),
    created_at: datetime = NOW,
) -> TaskEnvelope:
    return TaskEnvelope(
        repository_id="demo",
        actor="local:asha",
        conversation_ref="codex:thread-1",
        request=request,
        request_evidence_ref="evidence:conversation:" + "1" * 64,
        graph_version=3,
        created_at=created_at,
        requested_scope=scope,
    )


def _result(
    envelope: TaskEnvelope,
    *,
    classification: TaskClassification = TaskClassification.ALIGNED,
    authorized: bool = True,
    task_id: str | None = None,
    graph_version: int | None = None,
    permitted_scope: tuple[str, ...] | None = None,
    relevant_node_ids: tuple[str, ...] | None = None,
) -> PreflightResult:
    relevant = (
        ("requirement:csv-export",)
        if relevant_node_ids is None and classification is TaskClassification.ALIGNED
        else (() if relevant_node_ids is None else relevant_node_ids)
    )
    return PreflightResult(
        task_id=envelope.id if task_id is None else task_id,
        graph_version=envelope.graph_version if graph_version is None else graph_version,
        classification=classification,
        authorized=authorized,
        basis="Deterministically validated preflight",
        relevant_node_ids=relevant,
        evidence_refs=("evidence:conversation:" + "1" * 64,),
        permitted_scope=(
            tuple(sorted(envelope.requested_scope))
            if permitted_scope is None
            else permitted_scope
        ),
        context={} if classification is TaskClassification.NO_SEMANTIC_IMPACT else {"task": "csv"},
    )


def _verify(
    issuer: AuthorizationIssuer,
    token: str,
    envelope: TaskEnvelope,
    *,
    actor: str = "local:asha",
    repository_id: str = "demo",
    task_id: str | None = None,
    graph_version: int = 3,
    requested_paths: tuple[str, ...] = ("src/export.py",),
    graph_content: bytes | None = None,
    now: datetime = NOW,
) -> AuthorizationVerification:
    return issuer.verify(
        token,
        actor=actor,
        repository_id=repository_id,
        task_id=envelope.id if task_id is None else task_id,
        graph_version=graph_version,
        graph_content=_graph_content() if graph_content is None else graph_content,
        requested_paths=requested_paths,
        now=now,
    )


def _graph_content(*, purpose: str = "Original intent") -> bytes:
    return serialize_graph(
        Graph(
            id="graph:demo",
            version=3,
            purpose=purpose,
            nodes=(),
            edges=(),
        )
    )


def _issue(
    issuer: AuthorizationIssuer,
    envelope: TaskEnvelope,
    result: PreflightResult,
    *,
    graph_content: bytes | None = None,
    now: datetime = NOW,
) -> str:
    return issuer.issue(
        envelope,
        result,
        graph_content=_graph_content() if graph_content is None else graph_content,
        now=now,
    )


@pytest.mark.parametrize(
    ("classification", "scope", "relevant"),
    [
        (TaskClassification.NO_SEMANTIC_IMPACT, ("README.md",), ()),
        (
            TaskClassification.ALIGNED,
            ("src/export.py", "tests/test_export.py"),
            ("requirement:csv-export",),
        ),
    ],
)
def test_capability_binds_exact_preflight_and_authorizes_subsets(
    classification: TaskClassification,
    scope: tuple[str, ...],
    relevant: tuple[str, ...],
) -> None:
    """Fails if an authorized validated result cannot mint one exactly scoped grant."""
    issuer = AuthorizationIssuer()
    envelope = _envelope(scope=scope)
    result = _result(
        envelope,
        classification=classification,
        relevant_node_ids=relevant,
    )

    token = _issue(issuer, envelope, result)
    verified = _verify(
        issuer,
        token,
        envelope,
        requested_paths=(scope[0],),
    )

    assert verified == AuthorizationVerification(
        authorized=True,
        classification=classification,
        relevant_node_ids=relevant,
        expires_at=NOW + timedelta(minutes=5),
        reason="authorized",
    )
    assert verified.model_dump(mode="json") == {
        "schema_version": 1,
        "authorized": True,
        "classification": classification.value,
        "relevant_node_ids": list(relevant),
        "expires_at": "2026-08-26T12:05:00Z",
        "reason": "authorized",
    }


@pytest.mark.parametrize(
    ("updates", "reason"),
    [
        ({"actor": "local:other"}, "actor_mismatch"),
        ({"repository_id": "other"}, "repository_mismatch"),
        ({"task_id": "task:sha256:" + "9" * 64}, "task_mismatch"),
        ({"graph_version": 4}, "graph_mismatch"),
        ({"requested_paths": ("src/unrelated.py",)}, "scope_mismatch"),
    ],
)
def test_verification_fails_closed_for_every_binding_mismatch(
    updates: dict[str, object],
    reason: str,
) -> None:
    """Fails if any actor, repository, task, graph, or scope binding is advisory."""
    issuer = AuthorizationIssuer()
    envelope = _envelope()
    token = _issue(issuer, envelope, _result(envelope))

    denied = _verify(issuer, token, envelope, **updates)  # type: ignore[arg-type]

    assert denied.authorized is False
    assert denied.reason == reason
    assert denied.classification is None
    assert denied.relevant_node_ids == ()
    assert denied.expires_at is None


@pytest.mark.parametrize(
    "result_update",
    [
        {"authorized": False},
        {"classification": TaskClassification.NEW_OR_AMBIGUOUS},
        {"classification": TaskClassification.CONFLICTING},
        {"task_id": "task:sha256:" + "9" * 64},
        {"graph_version": 4},
        {"permitted_scope": ("src/unrelated.py",)},
        {"relevant_node_ids": ()},
        {"relevant_node_ids": ("requirement:z", "requirement:a")},
    ],
)
def test_issue_rejects_untrusted_or_mismatched_preflight_results(
    result_update: dict[str, object],
) -> None:
    """Fails if a caller can mint from a non-authorizing, stale, or incomplete result."""
    issuer = AuthorizationIssuer()
    envelope = _envelope()

    with pytest.raises(AuthorizationError, match="intent authorization unavailable"):
        _issue(issuer, envelope, _result(envelope, **result_update))  # type: ignore[arg-type]


def test_issue_requires_exact_detached_models_and_fresh_utc_chronology() -> None:
    """Fails if subclassed, malformed, non-UTC, future, or stale inputs can mint."""
    issuer = AuthorizationIssuer()
    envelope = _envelope()

    class EnvelopeSubclass(TaskEnvelope):
        pass

    class ResultSubclass(PreflightResult):
        pass

    hostile_envelope = EnvelopeSubclass.model_validate(envelope.model_dump())
    hostile_result = ResultSubclass.model_validate(_result(envelope).model_dump())
    for candidate_envelope, candidate_result, at in (
        (hostile_envelope, _result(envelope), NOW),
        (envelope, hostile_result, NOW),
        (envelope, _result(envelope), NOW.replace(tzinfo=None)),
        (envelope, _result(envelope), NOW.astimezone(timezone(timedelta(hours=5, minutes=30)))),
        (_envelope(created_at=NOW + timedelta(seconds=1)), _result(_envelope(created_at=NOW + timedelta(seconds=1))), NOW),
        (_envelope(created_at=NOW - timedelta(minutes=5, microseconds=1)), _result(_envelope(created_at=NOW - timedelta(minutes=5, microseconds=1))), NOW),
    ):
        with pytest.raises(AuthorizationError, match="intent authorization unavailable"):
            _issue(issuer, candidate_envelope, candidate_result, now=at)


@pytest.mark.parametrize(
    "scope",
    [
        ("",),
        ("/src/export.py",),
        ("../src/export.py",),
        ("src/../export.py",),
        ("src\\export.py",),
        ("src//export.py",),
        ("src/export.py", "src/export.py"),
        ("C:/src/export.py",),
        ("C:\\src\\export.py",),
        ("C:src/export.py",),
        ("//server/share/export.py",),
        ("\\\\server\\share\\export.py",),
        ("//?/C:/src/export.py",),
        ("\\\\?\\C:\\src\\export.py",),
        ("//./PIPE/name",),
        ("src/export:alternate.py",),
        ("NUL",),
        ("src/NUL.txt",),
        ("src/con/config.py",),
        ("src/AuX.log",),
        ("src/prn.",),
        ("src/CLOCK$",),
        ("src/clock$.txt",),
        ("src/COM1",),
        ("src/com9.log",),
        ("src/LPT1",),
        ("src/lpt9.txt",),
        ("src/NUL ",),
    ],
)
def test_issue_rejects_noncanonical_path_scope(scope: tuple[str, ...]) -> None:
    """Fails if ambiguous or escaping file scope reaches an authorization grant."""
    issuer = AuthorizationIssuer()
    envelope = _envelope(scope=scope)

    with pytest.raises(AuthorizationError, match="intent authorization unavailable"):
        _issue(issuer, envelope, _result(envelope))


@pytest.mark.parametrize(
    "path",
    ["src/com10.py", "src/lpt10.txt", "src/auxiliary.py", "src/null-device.py"],
)
def test_issue_preserves_valid_posix_names_near_windows_devices(path: str) -> None:
    """Fails if reserved-device rejection overmatches ordinary relative POSIX names."""
    issuer = AuthorizationIssuer()
    envelope = _envelope(scope=(path,))

    token = _issue(issuer, envelope, _result(envelope))

    assert _verify(
        issuer,
        token,
        envelope,
        requested_paths=(path,),
    ).authorized


def test_same_version_semantic_graph_replacement_invalidates_capability() -> None:
    """Fails if version equality substitutes for the authenticated graph identity."""
    issuer = AuthorizationIssuer()
    envelope = _envelope()
    original = _graph_content()
    replacement = _graph_content(purpose="Semantically replaced intent")

    token = issuer.issue(
        envelope,
        _result(envelope),
        graph_content=original,
        now=NOW,
    )
    denied = issuer.verify(
        token,
        actor=envelope.actor,
        repository_id=envelope.repository_id,
        task_id=envelope.id,
        graph_version=envelope.graph_version,
        graph_content=replacement,
        requested_paths=envelope.requested_scope,
        now=NOW,
    )

    assert denied.authorized is False
    assert denied.reason == "graph_mismatch"
    grant = next(iter(issuer.__dict__["_grants"].values()))
    assert grant.graph_digest == "sha256:c544d3202b83dda4df66182d14b5b96bca8dadbd28265d267a704afcddd3ae51"


def test_issue_rejects_oversized_or_control_bearing_identity_material() -> None:
    """Fails if model construction can bypass authorization byte and control bounds."""
    issuer = AuthorizationIssuer()
    valid = _envelope()
    for updates in (
        {"repository_id": "x" * 2049},
        {"actor": "local:\x00asha"},
        {"request_evidence_ref": "evidence:\x7fprivate"},
        {"requested_scope": ("src/" + "é" * 1023 + ".py",)},
    ):
        hostile = TaskEnvelope.model_construct(**{**valid.model_dump(), **updates})
        with pytest.raises(AuthorizationError, match="intent authorization unavailable"):
            _issue(issuer, hostile, _result(valid))


def test_zero_scope_allows_only_an_empty_requested_operation() -> None:
    """Fails if an unscoped task can be expanded into file mutation authority."""
    issuer = AuthorizationIssuer()
    envelope = _envelope(scope=())
    token = _issue(
        issuer,
        envelope,
        _result(
            envelope,
            classification=TaskClassification.NO_SEMANTIC_IMPACT,
            relevant_node_ids=(),
        ),
    )

    assert _verify(issuer, token, envelope, requested_paths=()).authorized
    assert not _verify(
        issuer,
        token,
        envelope,
        requested_paths=("README.md",),
    ).authorized


def test_expiry_restart_and_revoke_invalidate_without_extending_ttl() -> None:
    """Fails if a grant survives its issuer, expiry boundary, or explicit revocation."""
    issuer = AuthorizationIssuer()
    envelope = _envelope()
    token = _issue(issuer, envelope, _result(envelope))

    assert _verify(
        issuer,
        token,
        envelope,
        now=NOW + timedelta(minutes=4, seconds=59),
    ).expires_at == NOW + timedelta(minutes=5)
    expired = _verify(
        issuer,
        token,
        envelope,
        now=NOW + timedelta(minutes=5),
    )
    assert (expired.authorized, expired.reason) == (False, "expired")
    assert not _verify(AuthorizationIssuer(), token, envelope).authorized

    token = _issue(
        issuer,
        _envelope(request="Format README"),
        _result(_envelope(request="Format README")),
    )
    issuer.revoke_all()
    assert not _verify(
        issuer,
        token,
        _envelope(request="Format README"),
    ).authorized


def test_grants_are_strict_frozen_detached_and_registry_holds_digest_only() -> None:
    """Fails if token material or mutable caller state reaches the server-side registry."""
    issuer = AuthorizationIssuer()
    scope = ["src/export.py"]
    envelope = _envelope(scope=tuple(scope))
    result = _result(envelope)
    token = _issue(issuer, envelope, result)
    scope[0] = "src/unrelated.py"

    registry = issuer.__dict__["_grants"]
    assert len(registry) == 1
    digest, grant = next(iter(registry.items()))
    assert digest.startswith("sha256:") and len(digest) == 71
    assert grant.digest == digest
    assert grant.permitted_paths == ("src/export.py",)
    assert token not in repr(vars(issuer))
    assert token not in grant.model_dump_json()
    with pytest.raises(ValidationError):
        AuthorizationGrant.model_validate({**grant.model_dump(), "unknown": True})
    with pytest.raises(ValidationError):
        grant.actor = "local:other"  # type: ignore[misc]

    verified = _verify(issuer, token, envelope)
    with pytest.raises(ValidationError):
        verified.authorized = False  # type: ignore[misc]
    assert _verify(issuer, token, envelope).authorized
    assert token not in verified.model_dump_json()
    assert digest not in verified.model_dump_json()


def test_tokens_are_full_length_unique_and_never_persisted(tmp_path: Path) -> None:
    """Fails if opaque token bytes are shortened, reused, or written to project files."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "graph.yaml").write_text("graph: demo\n", encoding="utf-8")
    issuer = AuthorizationIssuer(max_live_grants=64)
    tokens = tuple(
        _issue(
            issuer,
            envelope := _envelope(request=f"Task {index}"),
            _result(envelope),
        )
        for index in range(64)
    )

    assert len(tokens) == len(set(tokens)) == 64
    assert all(len(token) == 43 for token in tokens)
    project_text = "".join(
        path.read_text(encoding="utf-8")
        for path in project.rglob("*")
        if path.is_file() and not path.is_symlink()
    )
    assert all(token not in project_text for token in tokens)


def test_capacity_never_evicts_live_grants_and_evicts_expired_before_rejecting() -> None:
    """Fails if capacity silently discards a live grant or counts expired state as live."""
    issuer = AuthorizationIssuer(max_live_grants=2)
    first = _envelope(request="Task 1")
    second = _envelope(request="Task 2")
    first_token = _issue(issuer, first, _result(first))
    second_token = _issue(issuer, second, _result(second))
    third = _envelope(request="Task 3")

    with pytest.raises(AuthorizationError, match="intent authorization unavailable"):
        _issue(issuer, third, _result(third))
    assert _verify(issuer, first_token, first).authorized
    assert _verify(issuer, second_token, second).authorized

    later = NOW + timedelta(minutes=5)
    fresh = _envelope(request="Fresh task", created_at=later)
    fresh_token = _issue(issuer, fresh, _result(fresh), now=later)
    assert _verify(issuer, fresh_token, fresh, now=later).authorized
    assert not _verify(issuer, first_token, first, now=later).authorized


def test_concurrent_issue_capacity_and_revoke_never_resurrect_state() -> None:
    """Fails if registry races exceed capacity or verify a token after revoke completes."""
    issuer = AuthorizationIssuer(max_live_grants=16)
    barrier = threading.Barrier(32)

    def issue(index: int) -> str | None:
        envelope = _envelope(request=f"Concurrent task {index}")
        barrier.wait(timeout=5)
        try:
            return _issue(issuer, envelope, _result(envelope))
        except AuthorizationError:
            return None

    with ThreadPoolExecutor(max_workers=32) as executor:
        tokens = tuple(executor.map(issue, range(32)))
    issued = tuple(token for token in tokens if token is not None)
    assert len(issued) == 16
    assert len(set(issued)) == 16

    envelope = _envelope(request="Race target")
    issuer.revoke_all()
    token = _issue(issuer, envelope, _result(envelope))
    race = threading.Barrier(2)

    def verify() -> bool:
        race.wait(timeout=5)
        return _verify(issuer, token, envelope).authorized

    def revoke() -> None:
        race.wait(timeout=5)
        issuer.revoke_all()

    with ThreadPoolExecutor(max_workers=2) as executor:
        checked = executor.submit(verify)
        removed = executor.submit(revoke)
        checked.result(timeout=5)
        removed.result(timeout=5)
    assert not _verify(issuer, token, envelope).authorized


@pytest.mark.parametrize("token", ["", "short", "x" * 4097, "token\x00private"])
def test_malformed_and_unknown_tokens_have_one_bounded_denial(token: str) -> None:
    """Fails if attacker-controlled token input escapes in an error or result."""
    denied = _verify(AuthorizationIssuer(), token, _envelope())

    assert denied == AuthorizationVerification(authorized=False, reason="unknown")
    assert token not in denied.model_dump_json() if token else True


class _CancellationSignal(BaseException):
    pass


def _repository_traceback_values(error: BaseException) -> str:
    values: list[str] = []
    for frame, _ in traceback.walk_tb(error.__traceback__):
        if "/src/intent_engineering/" in frame.f_code.co_filename:
            values.extend(repr(value) for value in frame.f_locals.values())
    return " ".join(values)


def test_issue_and_verify_cancellation_preserve_identity_without_secret_locals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fails if cancellation is replaced or repository traceback frames retain raw tokens."""
    sentinel = "PRIVATE-CAPABILITY-" + "x" * 24
    signal = _CancellationSignal("cancel")
    issuer = AuthorizationIssuer()
    envelope = _envelope()
    monkeypatch.setattr(authorization_module.secrets, "token_urlsafe", lambda size: sentinel)
    monkeypatch.setattr(authorization_module, "_token_digest", lambda token: (_ for _ in ()).throw(signal))

    with pytest.raises(_CancellationSignal) as caught:
        _issue(issuer, envelope, _result(envelope))
    assert caught.value is signal
    assert sentinel not in _repository_traceback_values(caught.value)
    assert issuer.__dict__["_grants"] == {}

    monkeypatch.undo()
    token = _issue(issuer, envelope, _result(envelope))
    verify_signal = _CancellationSignal("verify cancel")
    monkeypatch.setattr(
        authorization_module.hmac,
        "compare_digest",
        lambda left, right: (_ for _ in ()).throw(verify_signal),
    )
    with pytest.raises(_CancellationSignal) as verify_caught:
        _verify(issuer, token, envelope)
    assert verify_caught.value is verify_signal
    assert token not in _repository_traceback_values(verify_caught.value)
    assert token not in json.dumps(verify_caught.value.args)


def test_cancellation_after_registry_insert_rolls_back_the_unreturned_capability() -> None:
    """Fails if interruption after insertion leaves an accidentally live hidden capability."""
    signal = _CancellationSignal("post-insert cancel")

    class CancelAfterInsert(OrderedDict):
        def __setitem__(self, key: object, value: object) -> None:
            super().__setitem__(key, value)
            raise signal

    issuer = AuthorizationIssuer()
    issuer.__dict__["_grants"] = CancelAfterInsert()
    envelope = _envelope()

    with pytest.raises(_CancellationSignal) as caught:
        _issue(issuer, envelope, _result(envelope))

    assert caught.value is signal
    assert issuer.__dict__["_grants"] == {}


def test_targeted_revoke_cancellation_does_not_retain_token_in_digest_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fails if post-issue rollback interruption retains the capability in traceback locals."""
    signal = _CancellationSignal("cancel targeted revoke")
    sentinel = "PRIVATE_CAPABILITY_" + "x" * 24

    def cancel_digest(_content: bytes) -> object:
        raise signal

    monkeypatch.setattr(authorization_module.hashlib, "sha256", cancel_digest)
    with pytest.raises(_CancellationSignal) as caught:
        AuthorizationIssuer().revoke(sentinel)

    assert caught.value is signal
    assert sentinel not in _repository_traceback_values(caught.value)
