"""Read-only command adapter for deterministic graph assessment."""

# ruff: noqa: B008

from __future__ import annotations

import json
from pathlib import Path

import typer

from intent_engineering.assessment.gate import AssessmentGate, GateResult
from intent_engineering.assessment.models import AssessmentReport
from intent_engineering.assessment.service import GraphAssessmentService
from intent_engineering.cli.output import OutputFormat, emit
from intent_engineering.cli.runtime import AssessmentRuntime, load_assessment_runtime
from intent_engineering.storage.jsonl.strict import loads_strict_object
from intent_engineering.storage.secure import SecureDirectory, SecureFile

MAX_ASSESSMENT_REPORT_BYTES = 32 * 1024 * 1024


def _assessment_payload(report: AssessmentReport, focus: str | None) -> dict[str, object]:
    payload: dict[str, object] = {
        "assessment": report,
        "semantic_digest": report.semantic_digest,
    }
    if focus is None:
        return payload
    node = next((item for item in report.nodes if item.node_id == focus), None)
    branch = next((item for item in report.branches if item.branch_id == focus), None)
    if node is None and branch is None:
        raise LookupError("assessment reference unavailable")
    payload["focus"] = {"reference": focus, "node": node, "branch": branch}
    return payload


def _detached_signal(error: BaseException) -> BaseException:
    error.__traceback__ = None
    error.__cause__ = None
    error.__context__ = None
    return error


def _parse_assessment_report(content: bytes) -> AssessmentReport:
    if not content or len(content) > MAX_ASSESSMENT_REPORT_BYTES or not content.endswith(b"\n"):
        raise ValueError("invalid assessment report")
    body = content[:-1]
    parsed = loads_strict_object(body.decode("utf-8"))
    if set(parsed) != {"assessment", "semantic_digest", "version"}:
        raise ValueError("invalid assessment report")
    canonical = json.dumps(
        parsed,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if canonical != body or type(parsed["version"]) is not str or parsed["version"] != "1":
        raise ValueError("invalid assessment report")
    assessment = parsed["assessment"]
    semantic_digest = parsed["semantic_digest"]
    if type(assessment) is not dict or type(semantic_digest) is not str:
        raise ValueError("invalid assessment report")
    report = AssessmentReport.model_validate_json(
        json.dumps(
            assessment,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
        strict=True,
    )
    if report.semantic_digest != semantic_digest:
        raise ValueError("invalid assessment report")
    expected = json.dumps(
        {
            "assessment": report.model_dump(mode="json"),
            "semantic_digest": report.semantic_digest,
            "version": "1",
        },
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if expected != body:
        raise ValueError("invalid assessment report")
    return report


def _read_assessment_report(directory: SecureDirectory, path: Path) -> AssessmentReport:
    source: SecureFile | None = None
    try:
        source = directory.file(path)
        content = source.read_optional_nonblocking(max_bytes=MAX_ASSESSMENT_REPORT_BYTES)
        if content is None:
            raise ValueError("missing assessment report")
        return _parse_assessment_report(content)
    finally:
        if source is not None:
            source.close()


def assess_command(
    project: Path = typer.Option(Path("."), "--project"),
    focus: str | None = typer.Option(None, "--focus"),
    output_format: OutputFormat = typer.Option(OutputFormat.TEXT, "--format"),
) -> None:
    """Assess one ACL-visible snapshot without changing canonical project state."""
    runtime: AssessmentRuntime | None = None
    payload: dict[str, object] | None = None
    signal: BaseException | None = None
    failed = False
    try:
        runtime = load_assessment_runtime(project)
        snapshot = runtime.assessment_snapshot(runtime.config.local_actor)
        report = GraphAssessmentService().assess(snapshot)
        payload = _assessment_payload(report, focus)
    except Exception:  # noqa: BLE001 - one fixed public assessment failure
        failed = True
    except BaseException as error:  # noqa: BLE001 - preserve cancellation identity
        signal = _detached_signal(error)
    finally:
        if runtime is not None:
            try:
                runtime.close()
            except Exception:  # noqa: BLE001 - closing failure is a fixed public failure
                failed = True
                payload = None
            except BaseException as error:  # noqa: BLE001 - preserve cancellation identity
                if signal is None:
                    signal = _detached_signal(error)
        runtime = None
        project = Path()
        focus = None

    if signal is not None:
        detached = signal
        signal = None
        payload = None
        raise detached.with_traceback(None)
    if failed or payload is None:
        payload = None
        typer.echo("intent error: assessment unavailable", err=True)
        raise typer.Exit(1) from None
    emit(payload, output_format)
    payload = None


def assessment_gate_command(
    base_report: Path = typer.Option(..., "--base-report"),
    head_report: Path = typer.Option(..., "--head-report"),
    project: Path = typer.Option(Path("."), "--project"),
    output_format: OutputFormat = typer.Option(OutputFormat.JSON, "--format"),
) -> None:
    """Compare exact canonical base and head assessment reports for CI."""
    directory: SecureDirectory | None = None
    result: GateResult | None = None
    signal: BaseException | None = None
    failed = False
    try:
        directory = SecureDirectory.open(project)
        base = _read_assessment_report(directory, base_report)
        head = _read_assessment_report(directory, head_report)
        result = AssessmentGate().evaluate(base=base, head=head)
    except Exception:  # noqa: BLE001 - one fixed report/gate failure boundary
        failed = True
    except BaseException as error:  # noqa: BLE001 - preserve cancellation identity
        signal = _detached_signal(error)
    finally:
        if directory is not None:
            directory.close()
        directory = None
        project = Path()
        base_report = Path()
        head_report = Path()
    if signal is not None:
        detached = signal
        signal = None
        result = None
        raise detached.with_traceback(None)
    if failed or result is None:
        result = None
        typer.echo("intent error: assessment gate unavailable", err=True)
        raise typer.Exit(1) from None
    exit_code = result.exit_code
    emit({"gate": result}, output_format)
    result = None
    if exit_code:
        raise typer.Exit(exit_code)


__all__ = ["MAX_ASSESSMENT_REPORT_BYTES", "assess_command", "assessment_gate_command"]
