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
    rows.extend(
        ("document_requirements", f["node_id"], ["req:local"])
        for f in result.get("requirements_refs", [])
    )
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
    assert checkpoint["completed_comparisons"] == 6
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
    assert len(result["missing_comparisons"]) == 4
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


def test_removed_file_has_verified_absence_evidence_and_cannot_claim_coverage(env):
    (env.root / "app.py").unlink()
    result = call(
        env,
        "start",
        scope="Review removed implementation",
        files=[{"path": "app.py", "role": "code", "missing": True}],
    )
    assert result["code_refs"][0]["missing"] is True
    inputs = InitialReconciliationService(env).inputs(result["run_id"])
    assert inputs["nodes"][0]["id"] in {"intent:privacy", "req:local"}
    assert any(e["id"] == result["code_refs"][0]["evidence_ref"] for e in inputs["evidence"])
    with pytest.raises(ValueError, match="absent"):
        call(env, "checkpoint", run_id=result["run_id"], comparisons=reviews(result))
    assert not (env.root / "app.py").exists()


def test_absence_cannot_be_asserted_for_existing_file_or_symlink(env):
    for path in ("app.py", "alias.py"):
        if path == "alias.py":
            (env.root / path).symlink_to(env.root / "app.py")
        with pytest.raises(ValueError):
            call(
                env,
                "start",
                scope="Missing file",
                files=[{"path": path, "role": "code", "missing": True}],
            )


def test_removed_directory_is_absent_but_reappearance_invalidates_review(env):
    result = call(
        env,
        "start",
        scope="Removed directory",
        files=[{"path": "removed/code.py", "role": "code", "missing": True}],
    )
    (env.root / "removed").mkdir()
    (env.root / "removed/code.py").write_text("restored = True")
    assert call(env, "status", run_id=result["run_id"])["status"] == "stale"
    assert InitialReconciliationService(env).inputs(result["run_id"])["status"] == "stale"


def test_changed_directory_membership_guard_prevents_publication(env, monkeypatch):
    result = start(env)
    call(env, "checkpoint", run_id=result["run_id"], comparisons=reviews(result))
    service = InitialReconciliationService(
        env, clock=lambda: NOW, input_guard=lambda: not (env.root / "new.py").exists()
    )
    original = service._publish

    def publish_then_edit(*args):
        original(*args)
        (env.root / "new.py").write_text("unreviewed = True")

    monkeypatch.setattr(service, "_publish", publish_then_edit)
    with pytest.raises(ValueError, match="changed during publication"):
        service.run(
            InitialReconciliationRequest(
                action="finish", session_id="setup", turn_id="finish", run_id=result["run_id"]
            )
        )
    assert env.graph_store.load().version == 1
    assert (env.root / "new.py").read_text() == "unreviewed = True"


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
    assert any(
        c["label"].startswith("CODE_LAG:") for c in result["context"]["open_reconciliation_cases"]
    )
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


def test_source_read_without_interpretation_cannot_complete_initial_reconciliation(env):
    from intent_engineering.intent_workflow.source_refresh import SourceRefreshService
    from tests.integration.intent_workflow.test_source_refresh import (
        captured,
        observe,
        request,
        selection,
    )

    refresh = SourceRefreshService(env, clock=lambda: NOW)
    refresh.sources(selection())
    refresh.refresh(request())
    refresh.refresh(request(observations=[observe([captured(env)], generation=1)]))
    started = start(env)
    call(env, "checkpoint", run_id=started["run_id"], comparisons=reviews(started))
    result = call(env, "finish", run_id=started["run_id"])
    assert result["status"] == "incomplete"
    assert env.graph_store.load().version == 1


def test_conflicting_authoritative_sources_remain_open_and_are_not_rewritten(env):
    from intent_engineering.intent_workflow.background_sources import (
        BackgroundSource,
        capture_source,
    )
    from intent_engineering.intent_workflow.source_refresh import SourceRefreshService
    from tests.integration.intent_workflow.test_source_refresh import (
        captured,
        observe,
        reconcile,
        request,
        selection,
    )

    refresh = SourceRefreshService(env, clock=lambda: NOW)
    refresh.sources(selection(collection=True))
    refresh.refresh(request())
    local = captured(env, content="Keep all project data local.")
    cloud = capture_source(
        env,
        BackgroundSource(
            provider="notion",
            object_id="page:sync",
            version="1",
            locator="https://notion.so/sync",
            author="notion:bob",
            observed_at=NOW,
            content="Upload project data to the cloud.",
        ),
    )["evidence_ref"]
    pending = refresh.refresh(request(observations=[observe([local, cloud], generation=1)]))
    refresh.refresh(request(reconciliation_ref=reconcile(env, pending)))
    before = env.evidence_store.ledger("background:notion")
    started = start(env)
    rows = reviews(started)
    rows.append(
        {
            "axis": "source_conflict",
            "subject_ref": "intent:privacy",
            "related_refs": [],
            "status": "conflicting",
            "evidence_refs": started["evidence_refs"],
            "explanation": "The designated sources require both local-only and cloud storage.",
            "authority_basis": "Both authors are preserved; neither requirement nor code decides.",
            "recommendation": "Ask the user to resolve local-only versus cloud storage.",
            "recommendation_target": "source_conflict",
            "confidence": 0.8,
        }
    )
    call(env, "checkpoint", run_id=started["run_id"], comparisons=rows)
    result = call(env, "finish", run_id=started["run_id"])
    assert result["status"] == "ready_with_findings"
    case = env.cases()[0]
    assert case.case_type.value == "CONFLICTING_SOURCES"
    assert case.status.value == "open"
    assert {local, cloud} <= {r for side in case.evidence_sides for r in side.evidence_refs}
    assert env.evidence_store.ledger("background:notion") == before
    assert validate_project_directory(env.project_directory).valid


def test_every_declared_prd_requires_its_own_evidence_backed_comparison(env):
    (env.root / "extra.md").write_text("A new uncaptured requirement.")
    result = call(
        env,
        "start",
        scope="Both PRDs and production code",
        files=[
            {"path": "prd.md", "role": "requirements"},
            {"path": "extra.md", "role": "requirements"},
            {"path": "app.py", "role": "code"},
        ],
    )
    rows = [c for c in reviews(result) if c["axis"] != "document_requirements"]
    call(env, "checkpoint", run_id=result["run_id"], comparisons=rows)
    assert call(env, "finish", run_id=result["run_id"])["status"] == "incomplete"
    documents = [c for c in reviews(result) if c["axis"] == "document_requirements"]
    assert len(documents) == 2
    documents[1].update(
        status="missing",
        related_refs=[],
        recommendation="Capture the additional PRD requirement and assess its intent.",
        recommendation_target="requirements",
    )
    omitted = {
        **documents[0],
        "evidence_refs": [
            r
            for r in result["evidence_refs"]
            if r != result["requirements_refs"][0]["evidence_ref"]
        ],
    }
    with pytest.raises(ValueError, match="endpoint"):
        call(env, "checkpoint", turn_id="omit-doc", run_id=result["run_id"], comparisons=[omitted])
    call(env, "checkpoint", turn_id="docs", run_id=result["run_id"], comparisons=documents)
    finished = call(env, "finish", run_id=result["run_id"])
    assert finished["status"] == "ready_with_findings"
    assert finished["findings"][0]["axis"] == "document_requirements"
    assert env.cases()[0].subject_ref == result["requirements_refs"][1]["node_id"]
    assert validate_project_directory(env.project_directory).valid


def test_requirement_comparisons_must_consider_the_related_current_prd(env):
    result = start(env)
    rows = reviews(result)
    prd_ref = next(e.id for e in env.evidence() if e.source_locator == "prd.md")
    rows[2]["evidence_refs"] = [r for r in rows[2]["evidence_refs"] if r != prd_ref]
    call(env, "checkpoint", run_id=result["run_id"], comparisons=rows)
    with pytest.raises(ValueError, match="current requirements document"):
        call(env, "finish", run_id=result["run_id"])


def test_reassessment_supersedes_and_restores_only_owned_scoped_mappings(env):
    result = start(env)
    call(env, "checkpoint", run_id=result["run_id"], comparisons=reviews(result))
    call(env, "finish", run_id=result["run_id"])
    edge = next(e for e in env.graph_store.load().edges if e.relation.value == "IMPLEMENTED_BY")
    (env.root / "app.py").write_text("pass\n")
    result = call(
        env,
        "start",
        turn_id="scan2",
        scope="All production files in this fixture",
        files=[{"path": "prd.md", "role": "requirements"}, {"path": "app.py", "role": "code"}],
    )
    rows = reviews(result)
    for row in rows:
        if row["axis"] in {"requirements_code", "code_requirements", "intent_code"}:
            row.update(
                status="missing",
                related_refs=[],
                recommendation="Restore local persistence.",
                recommendation_target="code",
            )
    call(env, "checkpoint", turn_id="review2", run_id=result["run_id"], comparisons=rows)
    call(env, "finish", turn_id="finish2", run_id=result["run_id"])
    assert next(e for e in env.graph_store.load().edges if e.id == edge.id).status == "superseded"
    cases = env.cases()
    (env.root / "app.py").write_text("def store(data):\n    save_locally(data)\n")
    result = call(
        env,
        "start",
        turn_id="scan3",
        scope="All production files in this fixture",
        files=[{"path": "prd.md", "role": "requirements"}, {"path": "app.py", "role": "code"}],
    )
    call(env, "checkpoint", turn_id="review3", run_id=result["run_id"], comparisons=reviews(result))
    call(env, "finish", turn_id="finish3", run_id=result["run_id"])
    restored = [e for e in env.graph_store.load().edges if e.id == edge.id]
    assert len(restored) == 1 and restored[0].status == "active"
    assert env.cases() == cases  # New alignment never silently resolves historical issues.
    assert validate_project_directory(env.project_directory).valid


def test_regular_filenames_with_spaces_can_be_assessed(env):
    (env.root / "Product Requirements.md").write_text("Keep project data local.")
    (env.root / "source files").mkdir()
    (env.root / "source files" / "main.py").write_text("pass\n")
    result = call(
        env,
        "start",
        scope="Paths with spaces",
        files=[
            {"path": "Product Requirements.md", "role": "requirements"},
            {"path": "source files/main.py", "role": "code"},
        ],
    )
    assert result["status"] == "running"
    assert result["code_refs"][0]["path"] == "source files/main.py"


@pytest.mark.parametrize("action", ["status", "start", "checkpoint", "finish"])
@pytest.mark.parametrize("complete", [False, True])
def test_revoked_evidence_cannot_leak_through_status_or_exact_retry(env, action, complete):
    import yaml

    from intent_engineering.capture.base import RawSourceObject, normalize_raw_source

    policy = env.root / ".intent" / "approvals" / "policy.yaml"
    policy.parent.mkdir(exist_ok=True)
    policy_data = {
        "schema_version": 1,
        "contributors": ["local"],
        "approvers": ["local"],
        "executors": ["local"],
        "identities": {"local": ["local", "team:private"]},
    }
    policy.write_text(yaml.safe_dump(policy_data))
    record = normalize_raw_source(
        RawSourceObject(
            connector_type="private-fixture",
            external_object_id="private",
            external_version="1",
            source_locator="PRIVATE-LOCATOR",
            author="team:alice",
            observed_at=NOW,
            content_hash="sha256:" + "a" * 64,
            payload={"content": "PRIVATE-CONTENT"},
            acl=("team:private",),
        )
    )
    env.evidence_store.associate("private-fixture", record)
    BackgroundIntentService(env, clock=lambda: NOW).submit(
        BackgroundSubmission.model_validate(
            {
                "session_id": "setup",
                "turn_id": "private-req",
                "graph_version": 1,
                "user_statement": "Capture the private requirement.",
                "classification": "clear",
                "understanding": "PRIVATE-INTERPRETATION",
                "evidence_refs": [record.id],
                "nodes": [
                    {
                        "id": "req:private",
                        "type": "REQUIREMENT",
                        "label": "Private need",
                        "source_mode": "inferred",
                        "confidence": 0.8,
                        "basis": "Private source",
                        "evidence_refs": [record.id],
                    }
                ],
            }
        )
    )
    result = start(env)
    rows = reviews(result)
    rows[0]["explanation"] = "PRIVATE-INTERPRETATION"
    rows.extend(
        {
            "axis": axis,
            "subject_ref": "req:private",
            "status": "missing",
            "evidence_refs": result["evidence_refs"],
            "explanation": "PRIVATE-INTERPRETATION",
            "authority_basis": "Private source",
            "recommendation": "Clarify private intent.",
            "recommendation_target": "clarify_intent",
            "confidence": 0.8,
        }
        for axis in ("requirements_intent", "requirements_code")
    )
    call(env, "checkpoint", run_id=result["run_id"], comparisons=rows)
    if complete:
        assert call(env, "finish", run_id=result["run_id"])["status"] == "ready_with_findings"
    policy_data["identities"] = {"local": ["local"]}
    policy.write_text(yaml.safe_dump(policy_data))
    if action == "start":
        response = start(env)
    elif action == "checkpoint":
        response = call(env, action, run_id=result["run_id"], comparisons=rows)
    else:
        response = call(env, action, run_id=result["run_id"])
    assert response == {"status": "unavailable", "notify_user": False, "findings": []}
    service = InitialReconciliationService(env)
    assert service.inputs(result["run_id"]) == {"status": "unavailable"}
    assert service.baseline() is None


def test_reassessment_preserves_user_owned_and_out_of_scope_edges(env):
    from intent_engineering.core.models import ChangeSet
    from intent_engineering.storage.executor import LocalChangeSetExecutor

    result = start(env)
    call(env, "checkpoint", run_id=result["run_id"], comparisons=reviews(result))
    call(env, "finish", run_id=result["run_id"])
    graph = env.graph_store.load()
    template = next(e for e in graph.edges if e.relation.value == "IMPLEMENTED_BY")
    node = next(n for n in graph.nodes if n.id == template.to_id)
    outside_node = node.model_copy(update={"id": "code:outside", "label": "outside.py"})
    manual = template.model_copy(
        update={"id": "trace:manual", "created_by": "local", "last_modified_by": "local"}
    )
    outside = template.model_copy(update={"id": "trace:outside", "to_id": outside_node.id})
    change = ChangeSet(
        id="changeset:manual-traces",
        actor="local",
        timestamp=NOW,
        baseline_graph_version=graph.version,
        evidence_refs=node.evidence_refs,
        nodes_added=(outside_node,),
        nodes_updated=(),
        nodes_superseded=(),
        edges_added=(manual, outside),
        edges_updated=(),
        edges_superseded=(),
        confidence_changes=(),
        implementation_status_changes=(),
        reconciliation_cases_created=(),
        reconciliation_cases_resolved=(),
        validation_status="validated",
    )
    LocalChangeSetExecutor(env.graph_store, env.case_store, env.transactions).apply(change)
    (env.root / "app.py").write_text("pass\n")
    result = call(
        env,
        "start",
        turn_id="scan2",
        scope="Declared files only",
        files=[{"path": "prd.md", "role": "requirements"}, {"path": "app.py", "role": "code"}],
    )
    rows = reviews(result)
    for row in rows:
        if row["axis"] in {"requirements_code", "code_requirements", "intent_code"}:
            row.update(
                status="missing",
                related_refs=[],
                recommendation="Restore behavior.",
                recommendation_target="code",
            )
    call(env, "checkpoint", turn_id="review2", run_id=result["run_id"], comparisons=rows)
    call(env, "finish", turn_id="finish2", run_id=result["run_id"])
    edges = {e.id: e for e in env.graph_store.load().edges}
    assert edges[template.id].status == "superseded"
    assert edges[manual.id] == manual
    assert edges[outside.id] == outside


@pytest.mark.parametrize("path", ["doc\nname.md", "doc\tname.md", "doc\x7fname.md"])
def test_control_characters_are_not_valid_assessment_paths(env, path):
    with pytest.raises(ValueError):
        call(env, "start", scope="Invalid path", files=[{"path": path, "role": "requirements"}])
