"""Real-store background jobs with a deterministic semantic reviewer, never a live model."""

# ruff: noqa: F811 - imported pytest fixture used by dependent fixtures

import subprocess

import pytest

from intent_engineering.intent_workflow.code_reconciliation import CodeReconciliationService
from intent_engineering.intent_workflow.initial_reconciliation_models import Comparison
from tests.integration.intent_workflow.test_initial_reconciliation import (
    NOW,
    call,
    env,  # noqa: F401 - shared real-store fixture
    reviews,
    start,
)


class Reviewer:
    def __init__(self, effect=None):
        self.calls = []
        self.effect = effect

    def review(self, inputs, slots):
        self.calls.append(slots)
        if self.effect:
            self.effect()
        code = [f["node_id"] for f in inputs["code_refs"] if not f["missing"]]
        absent = {f["node_id"] for f in inputs["code_refs"] if f["missing"]}
        rows = []
        for slot in slots:
            axis = slot["axis"]
            related = {
                "intent_requirements": ["req:local"],
                "requirements_intent": ["intent:privacy"],
                "requirements_code": code,
                "code_requirements": ["req:local"],
                "intent_code": code,
                "document_requirements": ["req:local"],
            }[axis]
            gap = axis == "requirements_code" or slot["subject_ref"] in absent or not related
            status = (
                "missing"
                if not related or slot["subject_ref"] in absent
                else ("partial" if gap else "aligned")
            )
            rows.append(
                Comparison.model_validate(
                    {
                        **slot,
                        "related_refs": related,
                        "status": status,
                        "evidence_refs": inputs["evidence_refs"],
                        "explanation": "Storage does not fully implement the captured local-only requirement."
                        if gap
                        else "Compared captured intent and current scoped implementation.",
                        "authority_basis": "User source intent requires local-only storage.",
                        "recommendation": "Restore local-only storage." if gap else None,
                        "recommendation_target": "code" if gap else None,
                        "confidence": 0.8,
                    }
                )
            )
        return tuple(rows)


@pytest.fixture
def ready(env, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    first = start(env)
    call(env, "checkpoint", run_id=first["run_id"], comparisons=reviews(first))
    call(env, "finish", run_id=first["run_id"])
    return env


def worker(runtime, reviewer=None, **kw):
    return CodeReconciliationService(
        runtime,
        runner=reviewer or Reviewer(),
        clock=lambda: NOW,
        sleeper=lambda _seconds: None,
        **kw,
    )


def git_project(runtime):
    subprocess.run(["/usr/bin/git", "init", "-q", str(runtime.root)], check=True)
    (runtime.root / ".gitignore").write_text(".intent/\nstate/\nignored/\n")
    subprocess.run(["/usr/bin/git", "-C", str(runtime.root), "add", "app.py", "prd.md"], check=True)


def test_unchanged_baseline_skips_model_and_canonical_writes(ready):
    reviewer = Reviewer()
    before = ready.graph_store.load().version
    result = worker(ready, reviewer).run()
    assert result["status"] == "ready"
    assert result["notify_user"] is False
    assert reviewer.calls == []
    assert ready.graph_store.load().version == before


def test_changed_code_runs_review_and_flags_without_code_writes_or_notifications(ready):
    before = b"def store(data):\n    send_to_cloud(data)\n"
    (ready.root / "app.py").write_bytes(before)
    reviewer = Reviewer()
    result = worker(ready, reviewer).run()
    assert result["status"] == "ready_with_findings"
    assert result["notify_user"] is False
    assert result["findings"][0]["recommendation"] == "Restore local-only storage."
    assert (ready.root / "app.py").read_bytes() == before
    assert len(ready.cases()) == 1
    calls = len(reviewer.calls)
    worker(ready, reviewer).run()
    assert len(reviewer.calls) == calls
    assert len(ready.cases()) == 1


def test_git_new_deleted_and_renamed_code_are_in_review_but_ignored_files_are_not(ready):
    git_project(ready)
    (ready.root / "app.py").rename(ready.root / "renamed.py")
    (ready.root / "new.ts").write_text("export const feature = true")
    (ready.root / "ignored").mkdir()
    (ready.root / "ignored/secret.py").write_text("private = True")
    result = worker(ready).run()
    files = {f["path"]: f for f in result["code_refs"]}
    assert set(files) == {"app.py", "renamed.py", "new.ts"}
    assert files["app.py"]["missing"] is True
    assert result["status"] == "ready_with_findings"
    assert not (ready.root / "app.py").exists()


def test_edit_during_review_discards_old_comparisons_and_drains_latest_files(ready):
    (ready.root / "app.py").write_text("first = True")

    def edit_once():
        if (ready.root / "app.py").read_text() == "first = True":
            (ready.root / "app.py").write_text("latest = True")

    reviewer = Reviewer(edit_once)
    result = worker(ready, reviewer).run()
    assert len(reviewer.calls) >= 2
    assert result["status"] == "ready_with_findings"
    assert len(ready.cases()) == 1
    assert (ready.root / "app.py").read_text() == "latest = True"


def test_new_file_during_review_invalidates_membership_even_without_another_hook(ready):
    git_project(ready)
    (ready.root / "app.py").write_text("changed = True")

    def add_once():
        (ready.root / "later.py").write_text("added = True")

    result = worker(ready, Reviewer(add_once)).run()
    assert {f["path"] for f in result["code_refs"]} == {"app.py", "later.py"}
    assert len(ready.cases()) == 1


def test_failure_is_retryable_and_completed_batches_resume(ready):
    (ready.root / "app.py").write_text("changed = True")
    reviewer = Reviewer()
    original = reviewer.review

    def fail_second(inputs, slots):
        if reviewer.calls:
            raise RuntimeError("model unavailable; PRIVATE INFORMATION")
        return original(inputs, slots)

    reviewer.review = fail_second
    failed = worker(ready, reviewer, batch_size=1).run()
    assert failed["status"] == "retry_pending"
    assert "PRIVATE" not in str(failed)
    resumed = Reviewer()
    result = worker(ready, resumed, batch_size=1).run()
    assert result["status"] == "ready_with_findings"
    assert len(resumed.calls) == 5


def test_overlapping_events_queue_without_duplicate_semantic_worker(ready):
    (ready.root / "app.py").write_text("changed = True")
    other = Reviewer()
    returned = []

    def event():
        returned.append(worker(ready, other).run())

    result = worker(ready, Reviewer(event)).run()
    assert result["status"] == "ready_with_findings"
    assert other.calls == []
    assert all(r["status"] == "queued" for r in returned)
    assert len(ready.cases()) == 1


def test_disabled_worker_and_unfinished_onboarding_never_start_model(env, ready):
    reviewer = Reviewer()
    service = worker(ready, reviewer)
    service.configure(enabled=False)
    (ready.root / "app.py").write_text("changed = True")
    assert service.run()["status"] == "disabled"
    assert reviewer.calls == []
    service.configure(enabled=True)
    assert service.run()["status"] == "ready_with_findings"


def test_unreadable_symlink_and_non_git_scope_do_not_claim_alignment(ready, tmp_path):
    (ready.root / "app.py").unlink()
    (ready.root / "outside.py").write_text("secret = True")
    (ready.root / "app.py").symlink_to(ready.root / "outside.py")
    reviewer = Reviewer()
    assert worker(ready, reviewer).run()["status"] == "retry_pending"
    assert reviewer.calls == []


def test_invalid_or_missing_comparisons_never_finish(ready):
    (ready.root / "app.py").write_text("changed = True")
    reviewer = Reviewer()
    reviewer.review = lambda _inputs, _slots: ()
    assert worker(ready, reviewer).run()["status"] == "retry_pending"
    assert ready.cases() == ()


def test_pending_source_refresh_does_not_invoke_model_or_claim_ready(ready):
    from intent_engineering.intent_workflow.source_refresh import SourceRefreshService
    from tests.integration.intent_workflow.test_source_refresh import selection

    SourceRefreshService(ready, clock=lambda: NOW).sources(selection())
    (ready.root / "app.py").write_text("changed = True")
    reviewer = Reviewer()
    assert worker(ready, reviewer).run()["status"] == "awaiting_source_refresh"
    assert reviewer.calls == []


def test_unknown_review_remains_incomplete_and_can_be_reassessed(ready):
    (ready.root / "app.py").write_text("changed = True")
    reviewer = Reviewer()
    original = reviewer.review

    def uncertain(inputs, slots):
        return tuple(
            row.model_copy(
                update={
                    "status": "unknown",
                    "recommendation": "Inspect the unresolved storage path.",
                    "recommendation_target": "code",
                }
            )
            for row in original(inputs, slots)
        )

    reviewer.review = uncertain
    assert worker(ready, reviewer).run()["status"] == "incomplete"
    assert ready.cases() == ()
    assert worker(ready).run()["status"] == "ready_with_findings"


def test_disable_during_review_prevents_publication(ready):
    (ready.root / "app.py").write_text("changed = True")
    reviewer = Reviewer(lambda: worker(ready).configure(enabled=False))
    assert worker(ready, reviewer).run()["status"] == "disabled"
    assert ready.cases() == ()


def test_status_detects_new_membership_without_exposing_stale_findings(ready):
    git_project(ready)
    service = worker(ready)
    assert service.run()["status"] == "ready"
    (ready.root / "new.py").write_text("feature = True")
    assert service.status()["status"] == "pending"
    assert service.status()["findings"] == []


def test_before_initial_assessment_no_worker_starts(env, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    reviewer = Reviewer()
    assert worker(env, reviewer).run()["status"] == "awaiting_initial_assessment"
    assert reviewer.calls == []


def test_automatic_failed_reviews_have_cooldown_but_manual_retry_can_resume(ready):
    (ready.root / "app.py").write_text("changed = True")
    reviewer = Reviewer()

    def unavailable(inputs, slots):
        reviewer.calls.append(slots)
        raise ValueError("runtime unavailable")

    reviewer.review = unavailable
    service = worker(ready, reviewer)
    assert service.run(automatic=True)["status"] == "retry_pending"
    attempts = len(reviewer.calls)
    assert service.run(automatic=True)["status"] == "retry_pending"
    assert len(reviewer.calls) == attempts
    assert worker(ready).run()["status"] == "ready_with_findings"


def test_unrelated_graph_mutation_cannot_leave_completed_signature_stuck_pending(ready):
    from intent_engineering.core.models import ChangeSet
    from intent_engineering.storage.executor import LocalChangeSetExecutor

    reviewer = Reviewer()
    service = worker(ready, reviewer)
    assert service.run()["status"] == "ready"
    graph = ready.graph_store.load()
    original = next(n for n in graph.nodes if n.type == "FILE")
    change = ChangeSet(
        id="changeset:unrelated-file",
        actor="local",
        timestamp=NOW,
        baseline_graph_version=graph.version,
        evidence_refs=original.evidence_refs,
        nodes_added=(original.model_copy(update={"id": "file:outside", "label": "outside.py"}),),
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
    LocalChangeSetExecutor(ready.graph_store, ready.case_store, ready.transactions).apply(change)
    assert service.status()["status"] == "pending"
    assert service.run()["status"] == "ready_with_findings"
    assert reviewer.calls
