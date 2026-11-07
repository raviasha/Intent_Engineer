"""CLI comparison for explicit intent-aligned requirement alternatives."""

# ruff: noqa: B008

from __future__ import annotations

import traceback
from pathlib import Path

import typer

from intent_engineering.alternatives import (
    AlternativeMode,
    AlternativeReasoner,
    AlternativeResult,
    AlternativeStatus,
    RequirementAlternativeService,
)
from intent_engineering.cli.output import OutputFormat, emit
from intent_engineering.cli.runtime import AssessmentRuntime, load_assessment_runtime


def _configured_reasoner() -> AlternativeReasoner | None:
    """Return no implicit provider; hosts may inject one through the read-only tools."""
    return None


def _scrub_signal(error: BaseException) -> BaseException:
    old_traceback = error.__traceback__
    error.args = ()
    error.__dict__.clear()
    error.__traceback__ = None
    error.__cause__ = None
    error.__context__ = None
    if old_traceback is not None:
        traceback.clear_frames(old_traceback)
    return error


def _text_result(result: AlternativeResult) -> str:
    if result.status is AlternativeStatus.PROVIDER_UNAVAILABLE:
        return "Provider unavailable; assessment remains available."
    if result.status is not AlternativeStatus.AVAILABLE:
        return "No validated requirement alternatives are available."
    lines = ["Requirement alternatives"]
    for index, comparison in enumerate(result.alternatives, start=1):
        candidate = comparison.alternative.candidate
        subject_update = next(
            (
                effect
                for effect in candidate.graph_effects
                if effect.effect.value == "update_node"
                and effect.target_ref == candidate.subject_ref
            ),
            None,
        )
        lines.extend(
            [
                "",
                f"Alternative {index} — Projected — not approved",
                (
                    f"Current requirement: {comparison.current_scorecard.node_id} — "
                    f"{subject_update.before if subject_update is not None else 'unavailable'}"
                ),
                f"Proposed requirement: {candidate.proposed_text}",
                "Preserved intent: " + ", ".join(candidate.preserved_intent_refs),
                "Preserved outcomes: " + ", ".join(candidate.preserved_outcome_refs),
                "Weakened outcomes: "
                + (
                    "; ".join(
                        f"{item.outcome_ref} — {item.rationale}"
                        for item in candidate.weakened_outcomes
                    )
                    or "none declared"
                ),
                "Constraint changes: "
                + (
                    "; ".join(
                        f"{item.constraint_ref} ({item.effect.value})"
                        f" → {item.proposed_text or 'superseded'} — {item.rationale}"
                        for item in candidate.constraint_effects
                    )
                    or "none declared"
                ),
                (
                    "Complexity: "
                    f"implementation={candidate.complexity.implementation.value}, "
                    f"operations={candidate.complexity.operations.value} — "
                    f"{candidate.complexity_rationale}"
                ),
                "Risks: " + "; ".join(candidate.risks),
                "Trade-offs: " + "; ".join(candidate.tradeoffs),
                "Assumptions: " + "; ".join(candidate.assumptions),
                "Exact graph effects: "
                + "; ".join(
                    f"{item.effect.value} {item.target_ref}"
                    f" {item.field or ''}: {item.before or '∅'} → {item.after or '∅'}"
                    for item in candidate.graph_effects
                ),
                "Projected dimension deltas: "
                + ", ".join(
                    f"{item.dimension.value}={item.delta:+d}"
                    if item.delta is not None
                    else f"{item.dimension.value}=N/A"
                    for item in comparison.dimension_deltas
                ),
            ]
        )
    return "\n".join(lines)


def alternatives_command(
    subject: str = typer.Argument(..., help="Visible requirement node ID."),
    project: Path = typer.Option(Path("."), "--project"),
    mode: list[AlternativeMode] | None = typer.Option(None, "--mode"),
    output_format: OutputFormat = typer.Option(OutputFormat.TEXT, "--format"),
) -> None:
    """Compare simpler or different requirements against the same underlying intent."""
    runtime: AssessmentRuntime | None = None
    result: AlternativeResult | None = None
    signal: BaseException | None = None
    failed = False
    try:
        runtime = load_assessment_runtime(project)
        active_runtime = runtime
        service = RequirementAlternativeService(
            snapshot_loader=lambda: active_runtime.assessment_snapshot(
                active_runtime.config.local_actor
            ),
            reasoner=_configured_reasoner(),
        )
        selected_modes = tuple(mode or (AlternativeMode.SIMPLIFY,))
        result = service.request(subject_ref=subject, modes=selected_modes)
    except Exception:  # noqa: BLE001 - one fixed public CLI failure
        failed = True
    except BaseException as caught:  # noqa: BLE001 - preserve cancellation identity
        signal = _scrub_signal(caught)
    finally:
        subject = ""
        mode = None
        project = Path()
        if runtime is not None:
            try:
                runtime.close()
            except Exception:  # noqa: BLE001 - closing failure is a fixed public failure
                failed = True
                result = None
            except BaseException as caught:  # noqa: BLE001 - preserve close cancellation
                if signal is None:
                    signal = _scrub_signal(caught)
        runtime = None
    if signal is not None:
        detached = signal
        signal = None
        result = None
        raise detached.with_traceback(None)
    if failed or result is None:
        typer.echo("intent error: requirement alternatives unavailable", err=True)
        raise typer.Exit(1) from None
    if output_format is OutputFormat.TEXT:
        typer.echo(_text_result(result))
    else:
        emit({"alternatives": result}, output_format)
    result = None


__all__ = ["alternatives_command"]
