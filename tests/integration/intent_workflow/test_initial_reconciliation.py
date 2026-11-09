"""Initial reconciliation persists real comparisons, not an assertion of universal sync."""

from datetime import UTC, datetime

import pytest

from intent_engineering.cli.runtime import load_runtime
from intent_engineering.core.policy.project import initialize_project
from intent_engineering.intent_workflow.background import BackgroundIntentService
from intent_engineering.intent_workflow.background_models import BackgroundSubmission
from intent_engineering.intent_workflow.initial_reconciliation import InitialReconciliationService
from intent_engineering.intent_workflow.initial_reconciliation_models import (
    InitialReconciliationRequest,
)
from intent_engineering.validation import validate_project_directory

NOW = datetime(2026, 11, 9, tzinfo=UTC)


@pytest.fixture
def env(tmp_path):
    initialize_project(tmp_path)
    (tmp_path / "prd.md").write_text("Store project data locally.")
    (tmp_path / "app.py").write_text("def store(data):\n    return data\n")
    runtime = load_runtime(tmp_path)
    draft = BackgroundSubmission.model_validate(
        {
            "session_id": "setup",
            "turn_id": "intent",
            "graph_version": 0,
            "user_statement": "Keep data local. Store project data locally.",
            "classification": "clear",
            "understanding": "Local storage protects privacy.",
            "nodes": [
                {
                    "id": "intent:privacy",
                    "type": "PRODUCT_INTENT",
                    "label": "Keep data local.",
                    "source_mode": "explicit",
                    "confidence": 0.95,
                    "basis": "User statement",
                },
                {
                    "id": "req:local",
                    "type": "REQUIREMENT",
                    "label": "Store project data locally.",
                    "source_mode": "explicit",
                    "confidence": 0.95,
                    "basis": "User statement",
                },
            ],
        }
    )
    BackgroundIntentService(runtime, clock=lambda: NOW).submit(draft)
    yield runtime
    runtime.close()


def call(runtime, action, **kw):
    return InitialReconciliationService(runtime, clock=lambda: NOW).run(
        InitialReconciliationRequest.model_validate(
            {
                "action": action,
                "session_id": "setup",
                "turn_id": action,
                **kw,
            }
        )
    )


def start(runtime):
    return call(
        runtime,
        "start",
        scope="All production files in this fixture",
        files=[
            {"path": "prd.md", "role": "requirements"},
            {"path": "app.py", "role": "code"},
        ],
    )


def reviews(result, *, mismatch=False):
    code = result["code_refs"][0]["node_id"]
    evidence = result["evidence_refs"]
    rows = [
        ("intent_requirements", "intent:privacy", ["req:local"]),
        ("requirements_intent", "req:local", ["intent:privacy"]),
        ("requirements_code", "req:local", [code]),
        ("code_requirements", code, ["req:local"]),
        ("intent_code", "intent:privacy", [code]),
    ]
    return [
        {
            "axis": axis,
            "subject_ref": subject,
            "related_refs": related,
            "status": "partial" if mismatch and axis == "requirements_code" else "aligned",
            "evidence_refs": evidence,
            "explanation": "Compared local data handling.",
            "authority_basis": "The explicit user intent requires local storage.",
            "recommendation": "Persist data locally."
            if mismatch and axis == "requirements_code"
            else None,
            "recommendation_target": "code" if mismatch and axis == "requirements_code" else None,
            "confidence": 0.8,
        }
        for axis, subject, related in rows
    ]


def test_checkpoint_resumes_and_publishes_evidence_backed_cases_without_code_writes(env):
    before = (env.root / "app.py").read_bytes()
    started = start(env)
    assert started["status"] == "running"
    assert started["notify_user"] is False
    checkpoint = call(
        env, "checkpoint", run_id=started["run_id"], comparisons=reviews(started, mismatch=True)
    )
    assert checkpoint["completed_comparisons"] == 5
    assert env.cases() == ()
    finished = call(env, "finish", run_id=started["run_id"])
    assert finished["status"] == "ready_with_findings"
    assert finished["notify_user"] is True
    assert len(finished["findings"]) == 1
    assert finished["findings"][0]["recommendation"] == "Persist data locally."
    assert finished["findings"][0]["status"] == "partial"
    assert (env.root / "app.py").read_bytes() == before
    assert len(env.cases()) == 1
    assert env.cases()[0].status.value == "open"
    assert env.cases()[0].evidence_sides[0].evidence_refs
    graph = env.graph_store.load()
    assert any(n.type == "FILE" for n in graph.nodes)
    assert any(e.relation.value == "IMPLEMENTED_BY" for e in graph.edges)
    assert validate_project_directory(env.project_directory).valid
    assert call(env, "finish", run_id=started["run_id"]) == finished
    assert len(env.cases()) == 1


def test_incomplete_review_never_finishes_even_with_nonempty_graph(env):
    started = start(env)
    call(env, "checkpoint", run_id=started["run_id"], comparisons=reviews(started)[:2])
    result = call(env, "finish", run_id=started["run_id"])
    assert result["status"] == "incomplete"
    assert result["notify_user"] is False
    assert len(result["missing_comparisons"]) == 3
    assert env.cases() == ()


def test_changed_code_invalidates_checkpoint_and_does_not_overwrite_user_work(env):
    started = start(env)
    call(env, "checkpoint", run_id=started["run_id"], comparisons=reviews(started))
    (env.root / "app.py").write_text("def store(data):\n    send_to_cloud(data)\n")
    result = call(env, "finish", run_id=started["run_id"])
    assert result["status"] == "stale"
    assert env.graph_store.load().version == 1
    assert env.cases() == ()
    assert "send_to_cloud" in (env.root / "app.py").read_text()


def test_all_five_axes_are_required_and_aligned_links_do_not_close_old_cases(env):
    started = start(env)
    call(env, "checkpoint", run_id=started["run_id"], comparisons=reviews(started))
    result = call(env, "finish", run_id=started["run_id"])
    assert result["status"] == "ready"
    assert result["findings"] == []
    assert result["test_code_traceability"] == "deferred"
    assert result["scope"] == "All production files in this fixture"
    assert result["semantic_assurance"] == "host_reported_with_validated_evidence"


@pytest.mark.parametrize(
    "path", ["../outside.py", "/tmp/outside.py", ".intent/config.yaml", ".git/config"]
)
def test_unsafe_artifact_paths_are_rejected(env, path):
    with pytest.raises(ValueError):
        call(env, "start", scope="unsafe", files=[{"path": path, "role": "code"}])


def test_symlinks_are_not_followed(env):
    (env.root / "linked.py").symlink_to(env.root / "app.py")
    with pytest.raises((ValueError, OSError)):
        call(env, "start", scope="linked", files=[{"path": "linked.py", "role": "code"}])


def test_unsupported_claim_evidence_is_rejected_transactionally(env):
    started = start(env)
    rows = reviews(started)
    rows[0]["evidence_refs"] = ["evidence:does-not-exist"]
    with pytest.raises(ValueError):
        call(env, "checkpoint", run_id=started["run_id"], comparisons=rows)
    assert call(env, "status", run_id=started["run_id"])["completed_comparisons"] == 0


def test_missing_intent_and_uncovered_intent_become_distinct_actionable_findings(env):
    started = start(env)
    rows = reviews(started)
    for row in rows[:2]:
        row.update(
            status="missing",
            related_refs=[],
            recommendation="Capture supported requirements and their purpose.",
            recommendation_target="clarify_intent",
        )
    call(env, "checkpoint", run_id=started["run_id"], comparisons=rows)
    result = call(env, "finish", run_id=started["run_id"])
    assert {f["axis"] for f in result["findings"]} == {"intent_requirements", "requirements_intent"}
    assert len(env.cases()) == 2
    assert all(
        c.impact.endswith("Capture supported requirements and their purpose.") for c in env.cases()
    )


def test_unknown_coverage_cannot_be_ready_but_explicit_deferral_is_preserved(env):
    started = start(env)
    rows = reviews(started)
    rows[2].update(
        status="unknown",
        recommendation="Inspect the unavailable implementation.",
        recommendation_target="code",
    )
    call(env, "checkpoint", run_id=started["run_id"], comparisons=rows)
    assert call(env, "finish", run_id=started["run_id"])["status"] == "incomplete"
    rows[2].update(
        status="deferred",
        recommendation="Implement after the documented pilot.",
        recommendation_target="defer",
    )
    call(env, "checkpoint", turn_id="defer", run_id=started["run_id"], comparisons=rows)
    result = call(env, "finish", run_id=started["run_id"])
    assert result["findings"][0]["status"] == "deferred"
    assert env.cases() == ()


def test_exact_start_retry_and_other_sessions_resume_one_run(env):
    started = start(env)
    assert start(env)["run_id"] == started["run_id"]
    result = call(
        env,
        "start",
        session_id="resumed",
        turn_id="resume",
        scope=started["scope"],
        files=[
            {"path": "prd.md", "role": "requirements"},
            {"path": "app.py", "role": "code"},
        ],
    )
    assert result["run_id"] == started["run_id"]


def test_source_authority_is_required_and_source_changes_invalidate_result(env):
    from intent_engineering.intent_workflow.source_refresh import SourceRefreshService, SourceSignal
    from tests.integration.intent_workflow.test_source_refresh import (
        captured,
        observe,
        request,
        selection,
    )

    refresh = SourceRefreshService(env, clock=lambda: NOW)
    refresh.sources(selection())
    refresh.refresh(request())
    source_ref = captured(env)
    refresh.refresh(request(observations=[observe([source_ref], generation=1)]))
    started = start(env)
    rows = reviews(started)
    rows[0]["evidence_refs"] = [r for r in started["evidence_refs"] if r != source_ref]
    with pytest.raises(ValueError, match="designated source intent"):
        call(env, "checkpoint", run_id=started["run_id"], comparisons=rows)
    call(env, "checkpoint", run_id=started["run_id"], comparisons=reviews(started))
    refresh.signal(SourceSignal(source_id="source:privacy", event_id="change", observed_at=NOW))
    assert call(env, "finish", run_id=started["run_id"])["status"] == "stale"


def test_completed_result_is_not_current_after_graph_changes(env):
    started = start(env)
    call(env, "checkpoint", run_id=started["run_id"], comparisons=reviews(started))
    call(env, "finish", run_id=started["run_id"])
    BackgroundIntentService(env, clock=lambda: NOW).submit(
        BackgroundSubmission.model_validate(
            {
                "session_id": "main",
                "turn_id": "new-intent",
                "graph_version": 2,
                "user_statement": "Keep backups local.",
                "understanding": "Local backups also matter.",
                "classification": "clear",
                "nodes": [
                    {
                        "id": "intent:backups",
                        "type": "PRODUCT_INTENT",
                        "label": "Keep backups local.",
                        "source_mode": "explicit",
                        "confidence": 0.95,
                        "basis": "User",
                    }
                ],
            }
        )
    )
    assert call(env, "status", run_id=started["run_id"])["status"] == "stale"


def test_contradictory_reverse_mapping_does_not_publish_aligned_trace(env):
    started = start(env)
    rows = reviews(started)
    rows[3].update(
        status="missing",
        related_refs=[],
        recommendation="Find the requirement for this behavior.",
        recommendation_target="requirements",
    )
    call(env, "checkpoint", run_id=started["run_id"], comparisons=rows)
    with pytest.raises(ValueError, match="contradictory coverage"):
        call(env, "finish", run_id=started["run_id"])
    assert env.graph_store.load().version == 1


def test_published_code_trace_and_findings_reach_task_context(env):
    started = start(env)
    call(env, "checkpoint", run_id=started["run_id"], comparisons=reviews(started, mismatch=True))
    call(env, "finish", run_id=started["run_id"])
    result = BackgroundIntentService(env).context(
        task="local project data store app.py", session_id="developer"
    )
    assert result["context"]["code_refs"]
    assert any(c["label"].startswith("CODE_LAG:") for c in result["context"]["open_reconciliation_cases"])
    assert result["context"]["test_refs"] == []


def test_concurrent_code_change_during_publication_rolls_back_graph_not_user_edit(env, monkeypatch):
    from intent_engineering.storage.executor import LocalChangeSetExecutor

    started = start(env)
    call(env, "checkpoint", run_id=started["run_id"], comparisons=reviews(started))
    original = LocalChangeSetExecutor.apply

    def race(executor, *args, **kwargs):
        result = original(executor, *args, **kwargs)
        (env.root / "app.py").write_text("user_edited = True\n")
        return result

    monkeypatch.setattr(LocalChangeSetExecutor, "apply", race)
    with pytest.raises(ValueError, match="inputs changed"):
        call(env, "finish", run_id=started["run_id"])
    assert env.graph_store.load().version == 1
    assert env.cases() == ()
    assert (env.root / "app.py").read_text() == "user_edited = True\n"
