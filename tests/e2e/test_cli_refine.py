"""Public CLI contracts for voluntary graph-enrichment sessions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
import typer
from typer.testing import CliRunner

import intent_engineering.cli.refine as refine_cli
from intent_engineering.cli.app import app


class _Session:
    def __init__(self, *, status: str = "open") -> None:
        self.id = "refine:session"
        self.status = status
        self.current_gap_id = "gap:one" if status in {"open", "paused"} else None

    def model_dump(self, *, mode: str) -> dict[str, object]:
        assert mode == "json"
        return {
            "schema_version": 1,
            "id": self.id,
            "status": self.status,
            "focus_id": None,
            "budget_minutes": 5,
            "remaining_budget_seconds": 300,
            "snapshot_digest": "sha256:" + "a" * 64,
            "current_gap_id": self.current_gap_id,
            "answered_gap_ids": [],
            "skipped_gap_ids": [],
            "answer_evidence_refs": [],
            "started_at": "2026-09-02T12:00:00Z",
            "updated_at": "2026-09-02T12:00:00Z",
        }


class _Question:
    gap_id = "gap:one"
    node_id = "requirement:owners"
    dimension = "intent_clarity"
    prompt = "Who owns this requirement?"
    reason = "Ownership is missing."


class _Service:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def start(self, minutes: int | None, focus: str | None) -> _Session:
        self.calls.append(("start", minutes, focus))
        return _Session()

    def next_question(self, session_id: str) -> _Question:
        self.calls.append(("next_question", session_id))
        return _Question()

    def answer(self, session_id: str, gap_id: str, answer: str) -> _Session:
        self.calls.append(("answer", session_id, gap_id, answer))
        return _Session(status="complete")

    def current(self, session_id: str) -> _Session:
        self.calls.append(("current", session_id))
        return _Session()

    def pause(self, session_id: str) -> _Session:
        self.calls.append(("pause", session_id))
        return _Session(status="paused")

    def resume(self, session_id: str) -> _Session:
        self.calls.append(("resume", session_id))
        return _Session()


class _Runtime:
    config = type("Config", (), {"local_actor": "local:owner"})()

    def close(self) -> None:
        return None


def test_refine_requires_scope_and_structured_mode_never_prompts(
    monkeypatch: Any, tmp_path: Path
) -> None:
    service = _Service()
    monkeypatch.setattr(refine_cli, "load_runtime", lambda _project: _Runtime())
    monkeypatch.setattr(refine_cli, "GraphEnrichmentService", lambda *_args, **_kwargs: service)
    runner = CliRunner()

    missing = runner.invoke(app, ["refine", "--project", str(tmp_path), "--format", "json"])
    structured = runner.invoke(
        app,
        ["refine", "--project", str(tmp_path), "--minutes", "5", "--format", "json"],
        input="must-not-be-read\n",
    )

    assert missing.exit_code == 2
    assert structured.exit_code == 0
    payload = json.loads(structured.stdout)
    assert payload["version"] == "1"
    assert payload["session"]["status"] == "open"
    assert payload["question"]["prompt"] == "Who owns this requirement?"
    assert service.calls == [("start", 5, None), ("next_question", "refine:session")]


def test_refine_tty_records_one_explicit_answer(monkeypatch: Any, tmp_path: Path) -> None:
    service = _Service()
    monkeypatch.setattr(refine_cli, "load_runtime", lambda _project: _Runtime())
    monkeypatch.setattr(refine_cli, "GraphEnrichmentService", lambda *_args, **_kwargs: service)
    monkeypatch.setattr(refine_cli, "_stdin_is_tty", lambda: True)

    result = CliRunner().invoke(
        app,
        ["refine", "--project", str(tmp_path), "--minutes", "5"],
        input="Workspace owners\n",
    )

    assert result.exit_code == 0
    assert cast(tuple[object, ...], service.calls[-1]) == (
        "answer",
        "refine:session",
        "gap:one",
        "Workspace owners",
    )
    assert "Workspace owners" not in result.stdout


def test_refine_structured_current_pause_and_resume_use_the_exact_session(
    monkeypatch: Any, tmp_path: Path
) -> None:
    service = _Service()
    monkeypatch.setattr(refine_cli, "load_runtime", lambda _project: _Runtime())
    monkeypatch.setattr(refine_cli, "GraphEnrichmentService", lambda *_args, **_kwargs: service)
    runner = CliRunner()

    results = [
        runner.invoke(
            app,
            [
                "refine",
                "--project",
                str(tmp_path),
                "--session",
                "refine:session",
                "--action",
                action,
                "--format",
                "json",
            ],
        )
        for action in ("current", "pause", "resume")
    ]

    assert [result.exit_code for result in results] == [0, 0, 0]
    assert [json.loads(result.stdout)["session"]["status"] for result in results] == [
        "open",
        "paused",
        "open",
    ]
    assert service.calls == [
        ("current", "refine:session"),
        ("next_question", "refine:session"),
        ("pause", "refine:session"),
        ("resume", "refine:session"),
        ("next_question", "refine:session"),
    ]


def test_refine_preserves_and_scrubs_cancellation(monkeypatch: Any, tmp_path: Path) -> None:
    class _Cancel(BaseException):
        pass

    signal = _Cancel("PRIVATE-CANCEL-ANSWER")

    class _CancelledService:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            return None

        def start(self, _minutes: int | None, _focus: str | None) -> _Session:
            raise signal

    monkeypatch.setattr(refine_cli, "load_runtime", lambda _project: _Runtime())
    monkeypatch.setattr(refine_cli, "GraphEnrichmentService", _CancelledService)

    with pytest.raises(_Cancel) as captured:
        refine_cli.refine_command(
            project=tmp_path,
            minutes=5,
            focus=None,
            session_id=None,
            action=refine_cli.RefineAction.START,
            output_format=refine_cli.OutputFormat.JSON,
        )

    assert captured.value is signal
    assert signal.args == ()
    assert signal.__dict__ == {}
    assert signal.__cause__ is None
    assert signal.__context__ is None
    current = signal.__traceback__
    while current is not None:
        if "/src/intent_engineering/" in current.tb_frame.f_code.co_filename:
            assert "PRIVATE-CANCEL-ANSWER" not in repr(current.tb_frame.f_locals)
        current = current.tb_next


def test_refine_emit_failure_is_fixed_and_does_not_expose_payload(
    monkeypatch: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    service = _Service()
    monkeypatch.setattr(refine_cli, "load_runtime", lambda _project: _Runtime())
    monkeypatch.setattr(refine_cli, "GraphEnrichmentService", lambda *_args, **_kwargs: service)

    def fail_emit(_payload: object, _format: object) -> None:
        raise RuntimeError("PRIVATE-EMIT-PAYLOAD")

    monkeypatch.setattr(refine_cli, "emit", fail_emit)

    with pytest.raises(typer.Exit) as captured:
        refine_cli.refine_command(
            project=tmp_path,
            minutes=5,
            focus=None,
            session_id=None,
            action=refine_cli.RefineAction.START,
            output_format=refine_cli.OutputFormat.JSON,
        )

    assert captured.value.exit_code == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "intent error: graph enrichment unavailable\n"
    assert "PRIVATE-EMIT-PAYLOAD" not in output.err


def test_refine_emit_cancellation_preserves_identity_and_scrubs_signal(
    monkeypatch: Any, tmp_path: Path
) -> None:
    class _Cancel(BaseException):
        pass

    signal = _Cancel("PRIVATE-EMIT-CANCEL")
    service = _Service()
    monkeypatch.setattr(refine_cli, "load_runtime", lambda _project: _Runtime())
    monkeypatch.setattr(refine_cli, "GraphEnrichmentService", lambda *_args, **_kwargs: service)

    def cancel_emit(_payload: object, _format: object) -> None:
        raise signal

    monkeypatch.setattr(refine_cli, "emit", cancel_emit)

    with pytest.raises(_Cancel) as captured:
        refine_cli.refine_command(
            project=tmp_path,
            minutes=5,
            focus=None,
            session_id=None,
            action=refine_cli.RefineAction.START,
            output_format=refine_cli.OutputFormat.JSON,
        )

    assert captured.value is signal
    assert signal.args == ()
    assert signal.__dict__ == {}
    assert signal.__cause__ is None
    assert signal.__context__ is None
