"""Public CLI comparison for explicit intent-aligned requirement alternatives."""

from types import SimpleNamespace

from typer.testing import CliRunner

from intent_engineering.cli.app import app
from tests.integration.alternatives.test_service import RecordingReasoner
from tests.unit.alternatives.fixtures import assessment_pair


class FakeAssessmentRuntime:
    def __init__(self) -> None:
        self.config = SimpleNamespace(local_actor="actor:owner")
        self.snapshot, _report = assessment_pair()
        self.closed = False

    def assessment_snapshot(self, actor: str):
        assert actor == "actor:owner"
        return self.snapshot

    def close(self) -> None:
        self.closed = True


def test_cli_compares_current_and_manual_alternative(monkeypatch) -> None:
    runtime = FakeAssessmentRuntime()
    monkeypatch.setattr(
        "intent_engineering.cli.alternatives.load_assessment_runtime",
        lambda project: runtime,
    )
    monkeypatch.setattr(
        "intent_engineering.cli.alternatives._configured_reasoner",
        lambda: RecordingReasoner(),
    )

    result = CliRunner().invoke(
        app,
        ["alternatives", "req:export", "--mode", "change_approach"],
    )

    assert result.exit_code == 0, result.output
    assert "Current requirement" in result.stdout
    assert "Automate scheduled CSV export" in result.stdout
    assert "Proposed requirement" in result.stdout
    assert "An owner exports the report on request" in result.stdout
    assert "Projected — not approved" in result.stdout
    assert "Preserved intent" in result.stdout
    assert "Preserved outcomes" in result.stdout
    assert "Make the report available on request" in result.stdout
    assert "Exact graph effects" in result.stdout
    assert runtime.closed is True


def test_cli_provider_unavailable_is_successful_and_keeps_assessment_available(
    monkeypatch,
) -> None:
    runtime = FakeAssessmentRuntime()
    monkeypatch.setattr(
        "intent_engineering.cli.alternatives.load_assessment_runtime",
        lambda project: runtime,
    )
    monkeypatch.setattr(
        "intent_engineering.cli.alternatives._configured_reasoner",
        lambda: None,
    )

    result = CliRunner().invoke(app, ["alternatives", "req:export"])

    assert result.exit_code == 0, result.output
    assert "Provider unavailable; assessment remains available." in result.stdout
    assert runtime.closed is True


def test_cli_json_is_versioned_and_never_submits(monkeypatch) -> None:
    runtime = FakeAssessmentRuntime()
    monkeypatch.setattr(
        "intent_engineering.cli.alternatives.load_assessment_runtime",
        lambda project: runtime,
    )
    monkeypatch.setattr(
        "intent_engineering.cli.alternatives._configured_reasoner",
        lambda: RecordingReasoner(),
    )

    result = CliRunner().invoke(
        app,
        [
            "alternatives",
            "req:export",
            "--mode",
            "change_approach",
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert '"version":"1"' in result.stdout
    assert "proposal" not in result.stdout.lower()
    assert runtime.snapshot.graph.version == 7
