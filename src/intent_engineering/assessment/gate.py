"""Deterministic CI policy evaluation over detached assessment reports."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import ConfigDict, Field, field_validator

from intent_engineering.assessment.models import (
    AssessmentDimension,
    AssessmentHealth,
    AssessmentReport,
    BranchScorecard,
    ProjectScorecard,
)
from intent_engineering.core.models._base import StrictModel


class AssessmentGatePolicy(StrictModel):
    """Versioned opt-in CI rules; only newly red critical branches fail by default."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, validate_default=True)

    schema_version: Literal[1] = 1
    new_red: bool = True
    minimum_confidence: int | None = Field(default=None, ge=0, le=100)
    orange_warning: bool = False
    robustness_regression: bool = False

    @field_validator("schema_version", "minimum_confidence", mode="before")
    @classmethod
    def require_exact_integers(cls, value: object) -> object:
        if value is None:
            return None
        if type(value) is not int:
            raise ValueError("assessment gate integers must be exact")
        return value

    @field_validator("new_red", "orange_warning", "robustness_regression", mode="before")
    @classmethod
    def require_exact_booleans(cls, value: object) -> bool:
        if type(value) is not bool:
            raise ValueError("assessment gate flags must be exact booleans")
        return value

    def canonical_bytes(self) -> bytes:
        """Return the canonical gate policy material bound into every decision."""
        return json.dumps(
            self.model_dump(mode="json"),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    @property
    def digest(self) -> str:
        """Return the stable identity of the exact comparison rules."""
        return f"sha256:{hashlib.sha256(self.canonical_bytes()).hexdigest()}"


class GateGapIdentity(StrictModel):
    """One failed-rule identity plus non-identifying diagnostic references."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, validate_default=True)

    rule_id: str
    critical_node_id: str
    dimension: AssessmentDimension
    references: tuple[str, ...] = ()

    @field_validator("rule_id", "critical_node_id", mode="before")
    @classmethod
    def require_exact_identifiers(cls, value: object) -> str:
        if type(value) is not str or not value:
            raise ValueError("gate gap identifiers must be exact non-empty strings")
        return value

    @field_validator("references")
    @classmethod
    def canonicalize_references(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(type(item) is not str or not item for item in value):
            raise ValueError("gate gap references must be exact non-empty strings")
        return tuple(sorted(set(value)))

    def canonical_bytes(self) -> bytes:
        """Return the canonical identity material for deterministic set comparison."""
        return json.dumps(
            {
                "critical_node_id": self.critical_node_id,
                "dimension": self.dimension.value,
                "rule_id": self.rule_id,
            },
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    @property
    def digest(self) -> str:
        """Return a delimiter-safe stable identity for CI messages."""
        return f"sha256:{hashlib.sha256(self.canonical_bytes()).hexdigest()}"


class GateResult(StrictModel):
    """One immutable, provenance-bound gate decision."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, validate_default=True)

    schema_version: Literal[1] = 1
    exit_code: Literal[0, 5]
    base_assessment_digest: str
    head_assessment_digest: str
    gate_policy_digest: str
    new_red_gaps: tuple[GateGapIdentity, ...] = ()
    failures: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @field_validator("failures", "warnings")
    @classmethod
    def canonicalize_messages(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(type(item) is not str or not item for item in value):
            raise ValueError("assessment gate messages must be exact non-empty strings")
        return tuple(sorted(set(value)))

    @field_validator("new_red_gaps")
    @classmethod
    def canonicalize_new_red_gaps(
        cls,
        value: tuple[GateGapIdentity, ...],
    ) -> tuple[GateGapIdentity, ...]:
        if len({item.digest for item in value}) != len(value):
            raise ValueError("new red gate gaps must have unique identities")
        return tuple(sorted(value, key=lambda item: item.digest))


def _red_branch_ids(report: AssessmentReport) -> frozenset[str]:
    if report.branches:
        return frozenset(
            branch.branch_id for branch in report.branches if branch.health is AssessmentHealth.RED
        )
    if report.project.health is AssessmentHealth.RED:
        return frozenset({"project"})
    return frozenset()


def _red_critical_node_ids(
    report: AssessmentReport,
    *,
    branch_ids: frozenset[str],
) -> frozenset[str]:
    critical_ids = {
        node_id
        for branch in report.branches
        if branch.branch_id in branch_ids
        for node_id in branch.node_ids
    }
    return frozenset(
        node.node_id
        for node in report.nodes
        if node.node_id in critical_ids and node.health is AssessmentHealth.RED
    )


def _red_gap_identities(report: AssessmentReport) -> tuple[GateGapIdentity, ...]:
    critical_ids = {node_id for branch in report.branches for node_id in branch.node_ids}
    gaps = {
        gap.digest: gap
        for node in report.nodes
        if node.node_id in critical_ids and node.health is AssessmentHealth.RED
        for result in node.dimensions
        if result.health is AssessmentHealth.RED
        for check in result.failed
        for gap in (
            GateGapIdentity(
                rule_id=check.rule_id,
                critical_node_id=node.node_id,
                dimension=result.dimension,
                references=check.references,
            ),
        )
    }
    return tuple(gaps[digest] for digest in sorted(gaps))


class AssessmentGate:
    """Evaluate explicit branch-level assessment rules without mutating either report."""

    def evaluate(
        self,
        *,
        base: AssessmentReport,
        head: AssessmentReport,
        policy: AssessmentGatePolicy | None = None,
    ) -> GateResult:
        """Return a stable exit decision for the exact base and head report identities."""
        if type(base) is not AssessmentReport or type(head) is not AssessmentReport:
            raise TypeError("assessment gate requires exact reports")
        active_policy = policy or AssessmentGatePolicy()
        if type(active_policy) is not AssessmentGatePolicy:
            raise TypeError("assessment gate requires an exact policy")
        if (
            base.project_id != head.project_id
            or base.graph_id != head.graph_id
            or base.policy_digest != head.policy_digest
            or base.principal_projection_digest != head.principal_projection_digest
        ):
            raise ValueError("assessment gate unavailable")

        failures: list[str] = []
        warnings: list[str] = []
        new_red_gaps: tuple[GateGapIdentity, ...] = ()
        if active_policy.new_red:
            base_gap_digests = {gap.digest for gap in _red_gap_identities(base)}
            new_red_gaps = tuple(
                gap for gap in _red_gap_identities(head) if gap.digest not in base_gap_digests
            )
            failures.extend(f"new_red_gap:{gap.digest}" for gap in new_red_gaps)
            base_red_branches = _red_branch_ids(base)
            head_red_branches = _red_branch_ids(head)
            covered_nodes = {gap.critical_node_id for gap in new_red_gaps}
            covered_branches = {
                branch.branch_id for branch in head.branches if set(branch.node_ids) & covered_nodes
            }
            failures.extend(
                f"new_red:{branch_id}"
                for branch_id in sorted((head_red_branches - base_red_branches) - covered_branches)
            )
            shared_red_branches = base_red_branches & head_red_branches
            failures.extend(
                f"new_red:{node_id}"
                for node_id in sorted(
                    (
                        _red_critical_node_ids(head, branch_ids=shared_red_branches)
                        - _red_critical_node_ids(base, branch_ids=shared_red_branches)
                    )
                    - covered_nodes
                )
            )

        head_branches = {branch.branch_id: branch for branch in head.branches}
        base_branches = {branch.branch_id: branch for branch in base.branches}
        scorecards: tuple[BranchScorecard | ProjectScorecard, ...]
        if head_branches:
            scorecards = tuple(head_branches[identifier] for identifier in sorted(head_branches))
        else:
            scorecards = (head.project,)

        if active_policy.minimum_confidence is not None:
            threshold = active_policy.minimum_confidence
            for scorecard in scorecards:
                confidence = scorecard.confidence
                if confidence is None or confidence < threshold:
                    rendered = "unassessed" if confidence is None else str(confidence)
                    identifier = getattr(scorecard, "branch_id", "project")
                    failures.append(f"minimum_confidence:{identifier}:{rendered}<{threshold}")

        if active_policy.orange_warning:
            warnings.extend(
                f"orange:{getattr(scorecard, 'branch_id', 'project')}"
                for scorecard in scorecards
                if scorecard.health is AssessmentHealth.ORANGE
            )

        if active_policy.robustness_regression:
            pairs: tuple[
                tuple[
                    str,
                    BranchScorecard | ProjectScorecard,
                    BranchScorecard | ProjectScorecard,
                ],
                ...,
            ] = (("project", base.project, head.project),)
            if head_branches:
                pairs += tuple(
                    (identifier, base_branches[identifier], head_branches[identifier])
                    for identifier in sorted(set(base_branches) & set(head_branches))
                )
            for identifier, base_scorecard, head_scorecard in pairs:
                before = base_scorecard.robustness
                after = head_scorecard.robustness
                if before is not None and after is not None and after < before:
                    failures.append(f"robustness_regression:{identifier}:{before}>{after}")

        canonical_failures = tuple(sorted(set(failures)))
        return GateResult(
            exit_code=5 if canonical_failures else 0,
            base_assessment_digest=base.semantic_digest,
            head_assessment_digest=head.semantic_digest,
            gate_policy_digest=active_policy.digest,
            new_red_gaps=new_red_gaps,
            failures=canonical_failures,
            warnings=tuple(sorted(set(warnings))),
        )


__all__ = ["AssessmentGate", "AssessmentGatePolicy", "GateGapIdentity", "GateResult"]
