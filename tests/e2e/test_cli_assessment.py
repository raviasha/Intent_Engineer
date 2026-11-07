"""Black-box contracts for the read-only ``intent assess`` command."""

from __future__ import annotations

import json
import traceback
from collections.abc import Iterator
from pathlib import Path

import pytest
import structlog
import typer
import yaml  # type: ignore[import-untyped]
from typer.testing import CliRunner

import intent_engineering.cli.app as app_cli
from intent_engineering.cli import assessment as assessment_cli
from intent_engineering.cli.app import app
from intent_engineering.cli.runtime import load_assessment_runtime, load_runtime
from intent_engineering.core.models import EvidenceRecord, Graph
from intent_engineering.render.markdown import render_markdown
from intent_engineering.render.mermaid import render_mermaid
from intent_engineering.storage.secure import SecureDirectory
from intent_engineering.storage.transaction import LocalTransactionCoordinator
from intent_engineering.storage.yaml.graph_store import serialize_graph
from tests.helpers.cli import init_git_repo, run_intent

_FIXTURE = Path(__file__).parents[1] / "fixtures" / "assessment" / "rubric-v1.yaml"


def _durable_bytes(project: Path) -> dict[str, bytes]:
    return {
        path.relative_to(project).as_posix(): path.read_bytes()
        for path in sorted((project / ".intent").rglob("*"))
        if path.is_file() and not path.name.endswith(".lock")
    }


def _leave_transaction_journal(project: Path, stage: str) -> None:
    workspace = SecureDirectory.open(project / ".intent")
    paths = {
        "graph": "graph.yaml",
        "history": "history/changesets.jsonl",
        "cases": "reconciliation/cases.jsonl",
        "evidence": "evidence/evidence.jsonl",
        "receipts": "approvals/receipts.jsonl",
        "approvals": "approvals/approvals.jsonl",
        "intent_proposals": "history/intent-proposals.jsonl",
        "webauthn_credentials": "approvals/webauthn-credentials.jsonl",
        "webauthn_challenges": "approvals/webauthn-challenges.jsonl",
    }
    targets = {name: workspace.file(path) for name, path in paths.items()}

    def crash(current: str) -> None:
        if current == stage:
            raise SystemExit

    coordinator = LocalTransactionCoordinator(
        workspace.file("history/.local-transaction.json"),
        targets,
        fault_hook=crash,
    )
    try:
        with pytest.raises(SystemExit), coordinator.transaction() as transaction:
            transaction.write("graph", b"torn: [")
    finally:
        coordinator.close()
        for target in targets.values():
            target.close()
        workspace.close()


@pytest.fixture(autouse=True)
def _reset_cli_logging(request: pytest.FixtureRequest) -> None:
    request.addfinalizer(structlog.reset_defaults)


@pytest.fixture
def initialized_project(tmp_path: Path) -> Iterator[Path]:
    project = init_git_repo(tmp_path)
    assert run_intent(project, "init").returncode == 0
    payload = yaml.safe_load(_FIXTURE.read_text(encoding="utf-8"))
    runtime = load_runtime(project)
    try:
        (project / ".intent/graph.yaml").write_bytes(
            serialize_graph(Graph.model_validate(payload["graph"]))
        )
        for item in payload["evidence"]:
            runtime.evidence_store.put(EvidenceRecord.model_validate(item))
    finally:
        runtime.close()
    yield project


def test_assess_json_is_read_only_versioned_and_explainable(initialized_project: Path) -> None:
    """Catches assessment output mutating state or omitting its rubric explanations."""
    before = _durable_bytes(initialized_project)

    result = run_intent(initialized_project, "assess", "--format", "json")

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["version"] == "1"
    assert payload["assessment"]["schema_version"] == 1
    assert payload["assessment"]["nodes"][0]["dimensions"]
    assert payload["semantic_digest"].startswith("sha256:")
    assert _durable_bytes(initialized_project) == before


@pytest.mark.parametrize("output_format", ("text", "markdown"))
def test_assess_human_formats_remain_versioned_and_explainable(
    initialized_project: Path,
    output_format: str,
) -> None:
    """Catches human renderers silently replacing the shared versioned report contract."""
    result = run_intent(initialized_project, "assess", "--format", output_format)

    assert result.returncode == 0
    assert '"version": "1"' in result.stdout
    assert '"assessment"' in result.stdout
    if output_format == "markdown":
        assert result.stdout.startswith("# Intent Engineering\n\n```json\n")


def test_assess_focus_returns_visible_node_and_overlapping_branch_scorecards(
    initialized_project: Path,
) -> None:
    """Catches a root-node/branch ID collision making one visible scorecard unreachable."""
    result = run_intent(
        initialized_project,
        "assess",
        "--focus",
        "intent:export",
        "--format",
        "json",
    )

    assert result.returncode == 0
    focus = json.loads(result.stdout)["focus"]
    assert focus["reference"] == "intent:export"
    assert focus["node"]["node_id"] == "intent:export"
    assert focus["branch"]["branch_id"] == "intent:export"


def test_assess_unknown_or_hidden_focus_fails_fixed_and_stays_read_only(
    initialized_project: Path,
) -> None:
    """Catches focus lookup disclosing whether a reference is hidden or merely absent."""
    before = _durable_bytes(initialized_project)

    result = run_intent(
        initialized_project,
        "assess",
        "--focus",
        "PRIVATE-HIDDEN",
        "--format",
        "json",
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "intent error: assessment unavailable\n"
    assert "PRIVATE-HIDDEN" not in result.stderr
    assert _durable_bytes(initialized_project) == before


@pytest.mark.parametrize("stage", ("target:graph", "journal_committed"))
def test_assess_never_recovers_or_removes_an_incomplete_transaction(
    initialized_project: Path,
    stage: str,
) -> None:
    """Catches a nominally read-only assessment restoring targets or deleting a journal."""
    _leave_transaction_journal(initialized_project, stage)
    before = _durable_bytes(initialized_project)

    result = run_intent(initialized_project, "assess", "--format", "json")

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "intent error: assessment unavailable\n"
    assert _durable_bytes(initialized_project) == before


def test_assessment_gate_cli_parses_exact_reports_and_emits_the_bound_decision(
    initialized_project: Path,
) -> None:
    """Catches CI using a different report parser or gate path from the public implementation."""
    assessed = run_intent(initialized_project, "assess", "--format", "json")
    assert assessed.returncode == 0
    (initialized_project / "base-assessment.json").write_text(assessed.stdout, encoding="utf-8")
    (initialized_project / "head-assessment.json").write_text(assessed.stdout, encoding="utf-8")

    result = run_intent(
        initialized_project,
        "assessment-gate",
        "--base-report",
        "base-assessment.json",
        "--head-report",
        "head-assessment.json",
        "--format",
        "json",
    )

    assert result.returncode == 0
    gate = json.loads(result.stdout)["gate"]
    assert gate["schema_version"] == 1
    assert gate["exit_code"] == 0
    assert gate["base_assessment_digest"] == json.loads(assessed.stdout)["semantic_digest"]
    assert gate["head_assessment_digest"] == json.loads(assessed.stdout)["semantic_digest"]
    assert gate["gate_policy_digest"].startswith("sha256:")


@pytest.mark.parametrize(
    "content",
    (
        b'{"assessment":{}, "semantic_digest":"sha256:bad","version":"1"}\n',
        b'{"assessment":{},"assessment":{},"semantic_digest":"sha256:bad","version":"1"}\n',
        b"{" + b"x" * (32 * 1024 * 1024) + b"}\n",
    ),
    ids=("noncanonical", "duplicate", "oversized"),
)
def test_assessment_gate_cli_rejects_noncanonical_duplicate_or_oversized_reports(
    initialized_project: Path,
    content: bytes,
) -> None:
    """Catches attacker-controlled report bytes bypassing the bounded canonical parser."""
    (initialized_project / "base-assessment.json").write_bytes(content)
    assessed = run_intent(initialized_project, "assess", "--format", "json")
    (initialized_project / "head-assessment.json").write_text(assessed.stdout, encoding="utf-8")

    result = run_intent(
        initialized_project,
        "assessment-gate",
        "--base-report",
        "base-assessment.json",
        "--head-report",
        "head-assessment.json",
        "--format",
        "json",
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "intent error: assessment gate unavailable\n"


@pytest.mark.parametrize("mutation", ("array_order", "normalized_map"))
def test_assessment_gate_cli_rejects_model_noncanonical_report_material(
    initialized_project: Path,
    mutation: str,
) -> None:
    """Catches raw JSON that becomes canonical only after assessment-model normalization."""
    assessed = run_intent(initialized_project, "assess", "--format", "json")
    assert assessed.returncode == 0
    base = json.loads(assessed.stdout)
    if mutation == "array_order":
        dimensions = base["assessment"]["nodes"][0]["dimensions"]
        assert len(dimensions) > 1
        dimensions.reverse()
    else:
        weights = base["assessment"]["branches"][0]["contribution_weights"]
        assert weights
        base["assessment"]["branches"][0]["contribution_weights"] = {}
    (initialized_project / "base-assessment.json").write_text(
        json.dumps(base, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (initialized_project / "head-assessment.json").write_text(
        assessed.stdout,
        encoding="utf-8",
    )

    result = run_intent(
        initialized_project,
        "assessment-gate",
        "--base-report",
        "base-assessment.json",
        "--head-report",
        "head-assessment.json",
        "--format",
        "json",
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "intent error: assessment gate unavailable\n"


def test_assess_closes_runtime_on_post_load_failure(
    initialized_project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches descriptor leakage when rendering fails after a runtime is assembled."""
    runtime = load_runtime(initialized_project)
    closed = False
    original_close = runtime.close

    def close() -> None:
        nonlocal closed
        closed = True
        original_close()

    object.__setattr__(runtime, "close", close)
    monkeypatch.setattr(assessment_cli, "load_assessment_runtime", lambda _project: runtime)
    monkeypatch.setattr(
        assessment_cli,
        "_assessment_payload",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("PRIVATE-HIDDEN")),
    )

    result = CliRunner().invoke(
        app,
        ["assess", "--project", str(initialized_project), "--format", "json"],
    )

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == "intent error: assessment unavailable\n"
    assert "PRIVATE-HIDDEN" not in str(result.exception)
    assert closed is True


def test_render_assessment_is_stable_read_only_and_keeps_default_bytes(
    initialized_project: Path,
) -> None:
    """Catches the optional overlay mutating state or changing legacy render output."""
    before = _durable_bytes(initialized_project)
    legacy_one = initialized_project / "legacy-one"
    legacy_two = initialized_project / "legacy-two"
    assessed_one = initialized_project / "assessed-one"
    assessed_two = initialized_project / "assessed-two"

    first_legacy = run_intent(
        initialized_project,
        "render",
        "--output",
        str(legacy_one),
        "--format",
        "json",
    )
    first_assessed = run_intent(
        initialized_project,
        "render",
        "--assessment",
        "--output",
        str(assessed_one),
        "--format",
        "json",
    )
    second_assessed = run_intent(
        initialized_project,
        "render",
        "--assessment",
        "--output",
        str(assessed_two),
        "--format",
        "json",
    )
    second_legacy = run_intent(
        initialized_project,
        "render",
        "--output",
        str(legacy_two),
        "--format",
        "json",
    )

    assert first_legacy.returncode == second_legacy.returncode == 0
    assert first_assessed.returncode == second_assessed.returncode == 0
    assert (legacy_one / "graph.md").read_bytes() == (legacy_two / "graph.md").read_bytes()
    assert (legacy_one / "graph.mmd").read_bytes() == (legacy_two / "graph.mmd").read_bytes()
    assert (assessed_one / "graph.md").read_bytes() == (assessed_two / "graph.md").read_bytes()
    assert (assessed_one / "graph.mmd").read_bytes() == (assessed_two / "graph.mmd").read_bytes()
    assessed = (assessed_one / "graph.md").read_text(encoding="utf-8") + (
        assessed_one / "graph.mmd"
    ).read_text(encoding="utf-8")
    assert "Assessment (non-canonical)" in assessed
    assert "classDef health_" in assessed
    assert "Worst dimension" in assessed
    assert "snapshot: sha256:" in assessed
    assert "principal projection: sha256:" in assessed
    assert _durable_bytes(initialized_project) == before


@pytest.mark.parametrize("stage", ("journal_prepared", "journal_committed"))
def test_render_assessment_never_recovers_or_removes_an_incomplete_transaction(
    initialized_project: Path,
    stage: str,
) -> None:
    """Catches assessed rendering entering the normal recovering runtime."""
    _leave_transaction_journal(initialized_project, stage)
    before = _durable_bytes(initialized_project)

    result = run_intent(
        initialized_project,
        "render",
        "--assessment",
        "--format",
        "json",
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "intent error: assessment unavailable\n"
    assert _durable_bytes(initialized_project) == before


@pytest.mark.parametrize("fail_render", (False, True))
def test_render_assessment_closes_descriptor_runtime_on_success_or_failure(
    initialized_project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fail_render: bool,
) -> None:
    """Catches assessed render descriptors surviving either ordinary outcome."""
    runtime = load_assessment_runtime(initialized_project)
    close_count = 0
    original_close = runtime.close

    def close() -> None:
        nonlocal close_count
        close_count += 1
        original_close()

    object.__setattr__(runtime, "close", close)
    monkeypatch.setattr(app_cli, "load_assessment_runtime", lambda _project: runtime, raising=False)
    if fail_render:
        marker = "PRIVATE-RENDER-FAILURE-7391"

        def fail(*_args: object, **_kwargs: object) -> tuple[Path, Path]:
            raise ValueError(marker)

        monkeypatch.setattr(app_cli.GraphRenderer, "render_all", fail)

    failure: typer.Exit | None = None
    if fail_render:
        with pytest.raises(typer.Exit) as caught:
            app_cli.render_command(
                project=initialized_project,
                output=None,
                assessment=True,
                output_format=app_cli.OutputFormat.JSON,
            )
        failure = caught.value
    else:
        app_cli.render_command(
            project=initialized_project,
            output=None,
            assessment=True,
            output_format=app_cli.OutputFormat.JSON,
        )

    assert close_count == 1
    if fail_render:
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == "intent error: assessment unavailable\n"
        assert failure is not None
        assert failure.__cause__ is None
        assert failure.__context__ is None
        assert marker not in str(failure)
    else:
        assert json.loads(capsys.readouterr().out)["version"] == "1"


def test_render_assessment_cancellation_is_identity_preserving_and_scrubbed(
    initialized_project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches cancellation leaking report state or skipping descriptor cleanup."""

    class Cancelled(BaseException):
        pass

    marker = "PRIVATE-RENDER-CANCELLATION-4207"
    cancellation = Cancelled(marker)
    cancellation.private = marker
    cancellation.__cause__ = RuntimeError(marker)
    runtime = load_assessment_runtime(initialized_project)
    close_count = 0
    original_close = runtime.close

    def close() -> None:
        nonlocal close_count
        close_count += 1
        original_close()

    def cancel(*_args: object, **_kwargs: object) -> tuple[Path, Path]:
        raise cancellation

    object.__setattr__(runtime, "close", close)
    monkeypatch.setattr(app_cli, "load_assessment_runtime", lambda _project: runtime, raising=False)
    monkeypatch.setattr(app_cli.GraphRenderer, "render_all", cancel)

    with pytest.raises(Cancelled) as caught:
        app_cli.render_command(
            project=initialized_project,
            output=None,
            assessment=True,
            output_format=app_cli.OutputFormat.JSON,
        )

    assert caught.value is cancellation
    assert type(caught.value) is Cancelled
    assert caught.value.args == ()
    assert caught.value.__dict__ == {}
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert close_count == 1
    for frame, _line in traceback.walk_tb(caught.value.__traceback__):
        if frame.f_globals.get("__name__") != "intent_engineering.cli.app":
            continue
        assert frame.f_locals.get("runtime") is None
        assert frame.f_locals.get("snapshot") is None
        assert frame.f_locals.get("assessment_report") is None
        if "output_dir" in frame.f_locals:
            assert frame.f_locals["output_dir"] == Path()
        assert marker not in repr(frame.f_locals)


def test_render_assessment_emit_failure_is_fixed_unchained_and_closes_once(
    initialized_project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Catches assessed output serialization escaping its fixed failure boundary."""
    marker = "PRIVATE-ASSESSMENT-EMIT-FAILURE-1742"
    runtime = load_assessment_runtime(initialized_project)
    close_count = 0
    original_close = runtime.close

    def close() -> None:
        nonlocal close_count
        close_count += 1
        original_close()

    def fail_emit(*_args: object, **_kwargs: object) -> None:
        raise ValueError(marker)

    object.__setattr__(runtime, "close", close)
    monkeypatch.setattr(app_cli, "load_assessment_runtime", lambda _project: runtime, raising=False)
    monkeypatch.setattr(app_cli, "emit", fail_emit)

    with pytest.raises(typer.Exit) as caught:
        app_cli.render_command(
            project=initialized_project,
            output=None,
            assessment=True,
            output_format=app_cli.OutputFormat.JSON,
        )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "intent error: assessment unavailable\n"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert marker not in str(caught.value)
    assert close_count == 1


def test_render_assessment_emit_cancellation_is_identity_preserving_and_scrubbed(
    initialized_project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches assessed output cancellation retaining paths or descriptor state."""

    class Cancelled(BaseException):
        pass

    marker = "PRIVATE-ASSESSMENT-EMIT-CANCELLATION-6038"
    cancellation = Cancelled(marker)
    cancellation.private = marker
    cancellation.__cause__ = RuntimeError(marker)
    runtime = load_assessment_runtime(initialized_project)
    close_count = 0
    original_close = runtime.close

    def close() -> None:
        nonlocal close_count
        close_count += 1
        original_close()

    def cancel_emit(*_args: object, **_kwargs: object) -> None:
        raise cancellation

    object.__setattr__(runtime, "close", close)
    monkeypatch.setattr(app_cli, "load_assessment_runtime", lambda _project: runtime, raising=False)
    monkeypatch.setattr(app_cli, "emit", cancel_emit)

    with pytest.raises(Cancelled) as caught:
        app_cli.render_command(
            project=initialized_project,
            output=None,
            assessment=True,
            output_format=app_cli.OutputFormat.JSON,
        )

    assert caught.value is cancellation
    assert type(caught.value) is Cancelled
    assert caught.value.args == ()
    assert caught.value.__dict__ == {}
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert close_count == 1
    for frame, _line in traceback.walk_tb(caught.value.__traceback__):
        if frame.f_globals.get("__name__") != "intent_engineering.cli.app":
            continue
        if frame.f_code.co_name == "_render_assessed":
            assert frame.f_locals.get("runtime") is None
            assert frame.f_locals.get("project") == Path()
            assert frame.f_locals.get("output") is None
            assert frame.f_locals.get("markdown") is None
            assert frame.f_locals.get("mermaid") is None
        assert marker not in repr(frame.f_locals)


@pytest.mark.parametrize("assessment", (False, True))
def test_render_cancellation_clears_retained_frames_and_scrubs_secondary_close_signal(
    initialized_project: Path,
    monkeypatch: pytest.MonkeyPatch,
    assessment: bool,
) -> None:
    """Catches retained tracebacks or a discarded cleanup signal exposing private state."""

    class Cancelled(BaseException):
        pass

    primary_marker = "PRIVATE-RENDER-PRIMARY-4178"
    close_marker = "PRIVATE-RENDER-CLOSE-9526"
    primary = Cancelled(primary_marker)
    primary.private = primary_marker
    primary.__cause__ = RuntimeError(primary_marker)
    cleanup = Cancelled(close_marker)
    cleanup.private = close_marker
    cleanup.__cause__ = RuntimeError(close_marker)
    retained_primary: list[object] = []
    retained_cleanup: list[object] = []
    runtime = (
        load_assessment_runtime(initialized_project)
        if assessment
        else load_runtime(initialized_project)
    )
    close_count = 0
    original_close = runtime.close

    def close() -> None:
        nonlocal close_count
        close_count += 1
        original_close()
        private_close_material = close_marker
        try:
            raise cleanup
        except Cancelled as error:
            retained_cleanup.append(error.__traceback__)
            assert private_close_material
            raise

    def cancel_render(*_args: object, **_kwargs: object) -> tuple[Path, Path]:
        private_render_material = primary_marker
        try:
            raise primary
        except Cancelled as error:
            retained_primary.append(error.__traceback__)
            assert private_render_material
            raise

    object.__setattr__(runtime, "close", close)
    if assessment:
        monkeypatch.setattr(
            app_cli, "load_assessment_runtime", lambda _project: runtime, raising=False
        )
    else:
        monkeypatch.setattr(app_cli, "_runtime", lambda _project: runtime)
    monkeypatch.setattr(app_cli.GraphRenderer, "render_all", cancel_render)

    with pytest.raises(Cancelled) as caught:
        app_cli.render_command(
            project=initialized_project,
            output=None,
            assessment=assessment,
            output_format=app_cli.OutputFormat.JSON,
        )

    assert caught.value is primary
    assert type(caught.value) is Cancelled
    assert close_count == 1
    for signal in (primary, cleanup):
        assert signal.args == ()
        assert signal.__dict__ == {}
        assert signal.__cause__ is None
        assert signal.__context__ is None
    assert len(retained_primary) == len(retained_cleanup) == 1
    for retained in (*retained_primary, *retained_cleanup):
        for frame, _line in traceback.walk_tb(retained):  # type: ignore[arg-type]
            material = repr(frame.f_locals)
            assert primary_marker not in material
            assert close_marker not in material


@pytest.mark.parametrize("fail_render", (False, True))
def test_render_legacy_closes_runtime_and_preserves_bytes_or_fixed_failure(
    initialized_project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fail_render: bool,
) -> None:
    """Catches legacy rendering leaking runtime ownership or changing its projection."""
    runtime = load_runtime(initialized_project)
    principals = app_cli._authorized_principals(runtime)
    expected_markdown = render_markdown(
        app_cli._authorized_graph(runtime, principals),
        app_cli._authorized_cases(runtime, principals),
    ).encode()
    expected_mermaid = render_mermaid(app_cli._authorized_graph(runtime, principals)).encode()
    close_count = 0
    original_close = runtime.close

    def close() -> None:
        nonlocal close_count
        close_count += 1
        original_close()

    object.__setattr__(runtime, "close", close)
    monkeypatch.setattr(app_cli, "_runtime", lambda _project: runtime)
    marker = "PRIVATE-LEGACY-RENDER-FAILURE-2941"
    if fail_render:

        def fail(*_args: object, **_kwargs: object) -> tuple[Path, Path]:
            raise ValueError(marker)

        monkeypatch.setattr(app_cli.GraphRenderer, "render_all", fail)

    output = initialized_project / "legacy-owned-runtime"
    failure: typer.Exit | None = None
    try:
        if fail_render:
            with pytest.raises(typer.Exit) as caught:
                app_cli.render_command(
                    project=initialized_project,
                    output=output,
                    assessment=False,
                    output_format=app_cli.OutputFormat.JSON,
                )
            failure = caught.value
        else:
            app_cli.render_command(
                project=initialized_project,
                output=output,
                assessment=False,
                output_format=app_cli.OutputFormat.JSON,
            )

        assert close_count == 1
        if fail_render:
            captured = capsys.readouterr()
            assert captured.out == ""
            assert captured.err == "intent error: local operation failed\n"
            assert failure is not None
            assert failure.__cause__ is None
            assert failure.__context__ is None
            assert marker not in str(failure)
        else:
            assert json.loads(capsys.readouterr().out)["version"] == "1"
            assert (output / "graph.md").read_bytes() == expected_markdown
            assert (output / "graph.mmd").read_bytes() == expected_mermaid
    finally:
        if close_count == 0:
            original_close()


@pytest.mark.parametrize("cancel_at", ("render", "emit"))
def test_render_legacy_cancellation_closes_once_and_scrubs_command_state(
    initialized_project: Path,
    monkeypatch: pytest.MonkeyPatch,
    cancel_at: str,
) -> None:
    """Catches legacy render cancellation retaining runtime state or private errors."""

    class Cancelled(BaseException):
        pass

    marker = "PRIVATE-LEGACY-RENDER-CANCELLATION-8317"
    cancellation = Cancelled(marker)
    cancellation.private = marker
    cancellation.__cause__ = RuntimeError(marker)
    runtime = load_runtime(initialized_project)
    close_count = 0
    original_close = runtime.close

    def close() -> None:
        nonlocal close_count
        close_count += 1
        original_close()

    def cancel(*_args: object, **_kwargs: object) -> tuple[Path, Path]:
        raise cancellation

    object.__setattr__(runtime, "close", close)
    monkeypatch.setattr(app_cli, "_runtime", lambda _project: runtime)
    if cancel_at == "render":
        monkeypatch.setattr(app_cli.GraphRenderer, "render_all", cancel)
    else:
        monkeypatch.setattr(app_cli, "emit", cancel)

    try:
        with pytest.raises(Cancelled) as caught:
            app_cli.render_command(
                project=initialized_project,
                output=None,
                assessment=False,
                output_format=app_cli.OutputFormat.JSON,
            )

        assert caught.value is cancellation
        assert type(caught.value) is Cancelled
        assert caught.value.args == ()
        assert caught.value.__dict__ == {}
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None
        assert close_count == 1
        for frame, _line in traceback.walk_tb(caught.value.__traceback__):
            if frame.f_globals.get("__name__") != "intent_engineering.cli.app":
                continue
            assert frame.f_locals.get("runtime") is None
            if "principals" in frame.f_locals:
                assert frame.f_locals["principals"] == frozenset()
            assert marker not in repr(frame.f_locals)
    finally:
        if close_count == 0:
            original_close()
