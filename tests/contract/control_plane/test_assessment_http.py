"""Hostile-boundary contracts for read-only assessment HTTP projections."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, cast

import pytest
from pydantic import ValidationError
from starlette.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send

from intent_engineering.control_plane import build_control_plane_app, http_models

ORIGIN = "http://localhost:43127"
CSRF = "csrf-assessment-43127"
FIXED_ERROR = {"schema_version": 1, "status": "rejected", "reason": "request_unavailable"}
DIGEST = "sha256:" + "a" * 64


def _empty_report() -> dict[str, object]:
    return {
        "schema_version": 1,
        "project_id": "project",
        "graph_id": "graph:project",
        "graph_version": 1,
        "graph_digest": DIGEST,
        "evidence_digest": DIGEST,
        "ingestion_digest": DIGEST,
        "case_digest": DIGEST,
        "clarification_digest": DIGEST,
        "history_digest": DIGEST,
        "policy_digest": DIGEST,
        "snapshot_digest": DIGEST,
        "principal_projection_digest": DIGEST,
        "generated_at": "2026-09-02T12:00:00Z",
        "project": {
            "project_id": "project",
            "robustness": None,
            "confidence": None,
            "health": "unassessed",
            "branch_ids": [],
            "contributing_node_ids": [],
            "contribution_weights": {},
        },
        "branches": [],
        "nodes": [],
        "gaps": [],
        "warnings": [],
        "assessment_complete": True,
    }


class _Service:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.failure: BaseException | None = None

    def assessment(
        self, focus: str | None = None, *, page_cursor: str | None = None
    ) -> dict[str, object]:
        self.calls.append(("assessment", (focus, page_cursor)))
        if self.failure is not None:
            raise self.failure
        return {
            "schema_version": 1,
            "assessment": _empty_report(),
            "focus": None,
            "page": {"rows": [], "next_cursor": None},
        }

    def assessment_node(self, node_id: str) -> dict[str, object]:
        self.calls.append(("assessment_node", node_id))
        if self.failure is not None:
            raise self.failure
        raise LookupError("assessment node unavailable")


def _app(service: _Service):
    return build_control_plane_app(cast(Any, service), origin=ORIGIN, csrf_secret=CSRF)


def test_assessment_get_is_exact_loopback_read_and_returns_shared_report() -> None:
    service = _Service()

    response = TestClient(_app(service), base_url=ORIGIN).get("/api/v1/assessment")

    assert response.status_code == 200
    assert response.json()["assessment"]["snapshot_digest"] == DIGEST
    assert service.calls == [("assessment", (None, None))]


def test_hidden_node_lookup_is_indistinguishable_from_unknown() -> None:
    service = _Service()
    client = TestClient(_app(service), base_url=ORIGIN)

    hidden = client.get("/api/v1/assessment/nodes/req%3Ahidden")
    unknown = client.get("/api/v1/assessment/nodes/req%3Aunknown")

    assert (hidden.status_code, hidden.content) == (unknown.status_code, unknown.content)
    assert hidden.status_code == 404


def test_assessment_query_uses_one_canonical_spelling() -> None:
    service = _Service()
    client = TestClient(_app(service), base_url=ORIGIN)

    focused = client.get("/api/v1/assessment?focus=intent%3Aexport")
    paged = client.get("/api/v1/assessment?cursor=page%3A" + "b" * 64 + "%3A100")

    assert focused.status_code == 200
    assert paged.status_code == 200
    assert service.calls == [
        ("assessment", ("intent:export", None)),
        ("assessment", (None, "page:" + "b" * 64 + ":100")),
    ]


@pytest.mark.parametrize(
    "query",
    (
        "focus=req:raw",
        "focus=req%3aother",
        "focus=req%253Aother",
        "focus=req%3Aone&focus=req%3Atwo",
        "focus=",
        "Focus=req%3Aone",
        "%66ocus=req%3Aone",
        "node=req%3Aone",
        "cursor=page%3A" + "b" * 63 + "%3A100",
        "cursor=page%3A" + "b" * 64 + "%3A0100",
    ),
)
def test_assessment_rejects_alias_duplicate_and_noncanonical_queries_before_behavior(
    query: str,
) -> None:
    service = _Service()

    response = TestClient(_app(service), base_url=ORIGIN).get(f"/api/v1/assessment?{query}")

    assert response.status_code == 400
    assert response.json() == FIXED_ERROR
    assert service.calls == []


def test_assessment_node_path_requires_canonical_uppercase_percent_encoding() -> None:
    service = _Service()
    client = TestClient(_app(service), base_url=ORIGIN)

    canonical = client.get("/api/v1/assessment/nodes/req%3Aunknown")
    raw = client.get("/api/v1/assessment/nodes/req:unknown")
    lowercase = client.get("/api/v1/assessment/nodes/req%3aunknown")
    double = client.get("/api/v1/assessment/nodes/req%253Aunknown")

    assert canonical.status_code == 404
    assert raw.status_code == lowercase.status_code == double.status_code == 404
    assert service.calls == [("assessment_node", "req:unknown")]


class _StringSubclass(str):
    pass


async def _call_asgi(
    app: Callable[[Scope, Receive, Send], Awaitable[None]],
    *,
    path: object,
    raw_path: object,
    query_string: object = b"",
) -> list[Message]:
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    scope: dict[str, object] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": raw_path,
        "query_string": query_string,
        "root_path": "",
        "headers": [(b"host", b"localhost:43127")],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 43127),
        "state": {},
    }
    await app(cast(Scope, scope), receive, send)
    return sent


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("path", "raw_path", "query_string"),
    (
        (_StringSubclass("/api/v1/assessment"), b"/api/v1/assessment", b""),
        ("/api/v1/assessment", b"/api/v1/assessment", _StringSubclass("focus=x")),
        ("/api/v1/assessment", b"/api/v1/assessment" + b"x" * 4097, b""),
        ("/api/v1/assessment", b"/api/v1/assessment", b"focus=" + b"x" * 513),
    ),
)
async def test_assessment_rejects_scalar_subclasses_and_oversized_raw_targets(
    path: object, raw_path: object, query_string: object
) -> None:
    service = _Service()

    sent = await _call_asgi(_app(service), path=path, raw_path=raw_path, query_string=query_string)

    assert sent[0]["status"] in {400, 404, 414}
    assert service.calls == []


def _unassessed_node(node_id: str) -> dict[str, object]:
    return {
        "node_id": node_id,
        "node_type": "REQUIREMENT",
        "robustness": None,
        "confidence": None,
        "health": "unassessed",
        "worst_dimension": None,
        "dimensions": [],
        "blocking_case_refs": [],
        "recommended_next_action": None,
        "projected_robustness": None,
        "projected_confidence": None,
    }


def _response_with_nodes(nodes: list[dict[str, object]]) -> dict[str, object]:
    report = _empty_report()
    report["nodes"] = nodes
    return {
        "schema_version": 1,
        "assessment": report,
        "focus": None,
        "page": {"rows": nodes[:100], "next_cursor": None},
    }


def test_assessment_response_caps_graph_nodes_and_table_rows() -> None:
    too_many_nodes = [_unassessed_node(f"req:{index:04d}") for index in range(2001)]
    too_many_rows = _response_with_nodes(too_many_nodes[:101])
    cast(dict[str, object], too_many_rows["page"])["rows"] = too_many_nodes[:101]

    with pytest.raises(ValidationError):
        http_models.AssessmentResponse.model_validate(_response_with_nodes(too_many_nodes))
    with pytest.raises(ValidationError):
        http_models.AssessmentResponse.model_validate(too_many_rows)


def test_assessment_response_caps_identifiers_and_checks_per_dimension() -> None:
    oversized = _response_with_nodes([_unassessed_node("x" * 513)])
    check = {
        "rule_id": "rule",
        "points": 1,
        "severity": "orange",
        "explanation": "missing evidence",
        "references": [],
    }
    assessed = _unassessed_node("req:bounded")
    assessed.update(
        {
            "robustness": 99,
            "confidence": 99,
            "health": "orange",
            "worst_dimension": "evidence_strength",
            "dimensions": [
                {
                    "dimension": "evidence_strength",
                    "applicability": "required",
                    "score": 99,
                    "health": "orange",
                    "confidence": 99,
                    "passed": [{**check, "rule_id": f"rule:{index}"} for index in range(65)],
                    "failed": [],
                    "evidence_refs": [],
                    "related_refs": [],
                    "recommended_next_action": "add evidence",
                }
            ],
        }
    )

    with pytest.raises(ValidationError):
        http_models.AssessmentResponse.model_validate(oversized)
    with pytest.raises(ValidationError):
        http_models.AssessmentResponse.model_validate(_response_with_nodes([assessed]))


def test_assessment_focus_must_reference_the_same_report_scorecard() -> None:
    response = _response_with_nodes([])
    response["focus"] = {
        "reference": "branch:invented",
        "node": None,
        "branch": {"branch_id": "branch:invented", "node_ids": ["x" * 513]},
    }

    with pytest.raises(ValidationError):
        http_models.AssessmentResponse.model_validate(response)


@pytest.mark.anyio
async def test_assessment_cancellation_preserves_identity_and_scrubs_report_locals() -> None:
    secret = "PRIVATE-ASSESSMENT-FOCUS"
    cancellation = asyncio.CancelledError("cancelled")
    service = _Service()
    service.failure = cancellation

    with pytest.raises(asyncio.CancelledError) as caught:
        await _call_asgi(
            _app(service),
            path="/api/v1/assessment",
            raw_path=b"/api/v1/assessment",
            query_string=f"focus={secret}".encode("ascii"),
        )

    assert caught.value is cancellation
    trace = caught.value.__traceback__
    repository_locals: list[str] = []
    while trace is not None:
        if "/src/intent_engineering/" in trace.tb_frame.f_code.co_filename:
            repository_locals.append(repr(trace.tb_frame.f_locals))
        trace = trace.tb_next
    assert secret not in "".join(repository_locals)
