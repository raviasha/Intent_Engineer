"""Repository-local, non-secret handoff for the trusted GitHub setup UI."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import traceback
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Literal, cast

import anyio
from pydantic import ConfigDict, Field, model_validator

from intent_engineering.capture.github.auth import GitHubCredentials
from intent_engineering.capture.github.client import GitHubClient
from intent_engineering.capture.github.models import PageResult
from intent_engineering.cli.team import GitHubEnablePreview, GitHubEnableResult
from intent_engineering.control_plane.models import (
    DecisionAction,
    DecisionSubject,
    HumanDecisionPayload,
)
from intent_engineering.control_plane.webauthn_service import VerifiedHumanDecision
from intent_engineering.core.models._base import StrictModel
from intent_engineering.storage._atomic import same_path_lock
from intent_engineering.storage.transaction import LocalTransactionCoordinator
from intent_engineering.team_state.ci import CiTrustConfig
from intent_engineering.team_state.enrollment import (
    ApprovedAuthorityTransitionV2,
    EnrollmentPublicationPlanV2,
    EnrollmentTransitionProofV2,
    JoinApprovalPreviewV2,
    JoinResponseV2,
    PreparedEnrollmentPublicationV2,
    TeamEnrollmentService,
    TeamInviteV2,
    VerifiedRemoteStateV2,
    authenticate_enrollment_publication,
    build_sponsor_decision_payload,
    enrollment_transition_plan_digest,
)
from intent_engineering.team_state.github import (
    GitHubDefaultBranchBaseline,
    GitHubDefaultBranchTooling,
    GitHubJsonResponse,
    GitHubProtectionPreview,
    GitHubTeamStateApi,
    GitHubTeamStateClient,
    GitHubTeamStateError,
    GitHubTeamStateStatus,
)
from intent_engineering.team_state.keys import GitHubIdentity
from intent_engineering.team_state.models import (
    MAX_BUNDLE_BYTES,
    CanonicalStateSnapshot,
    PreparedPublication,
    RecipientRecord,
    TeamAuthorityRegistryV2,
    TeamStateManifest,
    TeamStateManifestV2,
    canonical_manifest_bytes,
)
from intent_engineering.team_state.publication import (
    PreparedPublicationV2,
    PublicationAuthority,
    PublicationService,
)
from intent_engineering.team_state.restore import VerifiedReleaseV2
from intent_engineering.team_state.signing import SigningKeyStore

if TYPE_CHECKING:
    from intent_engineering.cli.runtime import Runtime
    from intent_engineering.control_plane.service import ControlPlaneService
    from intent_engineering.team_state.suggestions import CodeSuggestionPreview


PreparedStatePublication = (
    PreparedPublication | PreparedPublicationV2 | PreparedEnrollmentPublicationV2
)
_DISCARDED_ENROLLMENT_STATE = b'{"discarded":true}'


class GitHubSetupRequest(StrictModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    preview: GitHubEnablePreview


class GitHubEnrollmentContext(StrictModel):
    """Owner-protected public GitHub/tooling context retained after initial setup."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    request: GitHubSetupRequest

    def canonical_bytes(self) -> bytes:
        return self.model_dump_json().encode()


class EnrollmentApprovalRequest(StrictModel):
    """Exact human and GitHub preimages approved for one membership change."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    preview: JoinApprovalPreviewV2
    github_preflight: GitHubProtectionPreview
    publication_plan: EnrollmentPublicationPlanV2
    transition_plan_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def require_exact_plan(self) -> EnrollmentApprovalRequest:
        if (
            self.publication_plan.authority != self.preview.authority_after
            or self.publication_plan.manifest.authority_digest
            != self.preview.authority_after_digest
            or self.publication_plan.manifest.parent_bundle_digest
            != self.preview.base_bundle_digest
            or self.publication_plan.parent_commit != self.preview.base_state_commit
            or self.transition_plan_digest
            != enrollment_transition_plan_digest(self.preview, self.publication_plan)
        ):
            raise ValueError("team enrollment approval changed")
        return self

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()

    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(self.canonical_bytes()).hexdigest()


class EnrollmentReceiptV2(StrictModel):
    """Public, monotonic receipt linking one approval to its exact state PR."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    invite_id: str = Field(min_length=1, max_length=256)
    response_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    authority_before_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    authority_after_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    publication_manifest_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    approval_request_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    approved_github_preflight: GitHubProtectionPreview
    sponsor_decision: HumanDecisionPayload
    transition_proof: EnrollmentTransitionProofV2
    phase: Literal["approved", "publication-pending", "pr-pending", "merged", "closed"]
    publication_commit: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    pull_request_number: int | None = Field(default=None, gt=0)
    pull_request_url: str | None = Field(default=None, min_length=1, max_length=2048)
    merged_commit: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")

    @model_validator(mode="after")
    def require_exact_phase(self) -> EnrollmentReceiptV2:
        has_pr = self.pull_request_number is not None and self.pull_request_url is not None
        if (
            self.transition_proof.authority_before_digest != self.authority_before_digest
            or self.transition_proof.authority_after_digest != self.authority_after_digest
            or self.transition_proof.response_digest != self.response_digest
            or self.transition_proof.invite_id != self.invite_id
            or self.transition_proof.publication_manifest_digest != self.publication_manifest_digest
            or self.sponsor_decision.result_digest != self.approval_request_digest
            or "sha256:" + hashlib.sha256(self.sponsor_decision.canonical_bytes()).hexdigest()
            != self.transition_proof.authority_attestation.sponsor_decision_digest
            or (self.pull_request_number is None) != (self.pull_request_url is None)
            or (self.phase in {"approved"} and self.publication_commit is not None)
            or (self.phase in {"approved", "publication-pending"} and has_pr)
            or (self.phase in {"pr-pending", "merged", "closed"} and not has_pr)
            or (
                self.phase in {"pr-pending", "merged", "closed"} and self.publication_commit is None
            )
            or (self.phase == "merged") != (self.merged_commit is not None)
        ):
            raise ValueError("team enrollment receipt changed")
        if has_pr:
            expected = (
                f"https://github.com/{self.transition_proof.root.repository_id.removeprefix('github.com/')}"
                f"/pull/{self.pull_request_number}"
            )
            if self.pull_request_url != expected:
                raise ValueError("team enrollment receipt changed")
        return self

    def canonical_bytes(self) -> bytes:
        return self.model_dump_json().encode()


def _require_current_enrollment_decision(
    receipt: EnrollmentReceiptV2,
    request: EnrollmentApprovalRequest,
    now: datetime,
) -> None:
    payload = receipt.sponsor_decision
    sponsor = next(
        member
        for member in request.preview.authority_after.members
        if member.member_id == request.preview.invite.sponsor_member_id
    )
    expected_subject_digest = _digest(
        {
            "approval_request_digest": request.digest(),
            "preview": request.preview.model_dump(mode="json"),
        }
    )
    expected_repository_id = (
        "repo:sha256:"
        + hashlib.sha256(
            b"intent.team-enrollment.local-repository.v2\0"
            + request.preview.invite.repository_id.encode()
        ).hexdigest()
    )
    if (
        receipt.approval_request_digest != request.digest()
        or payload.project_id != request.preview.invite.project_id
        or payload.repository_id != expected_repository_id
        or payload.actor != sponsor.actor
        or payload.action is not DecisionAction.APPROVE_EXTERNAL_WRITE
        or payload.graph_version != request.preview.authority_after.sequence
        or payload.parent_bundle_digest != request.preview.base_bundle_digest
        or payload.subject.kind != "join_approval"
        or payload.subject.id
        != "join_approval:" + request.preview.response_digest.removeprefix("sha256:")
        or payload.subject_digest != expected_subject_digest
        or payload.result_digest != request.digest()
        or not payload.issued_at <= now < payload.expires_at
    ):
        raise ValueError("team enrollment changed")


class EncryptedPublicationDraft(StrictModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    manifest: TeamStateManifest | TeamStateManifestV2
    authority: TeamAuthorityRegistryV2 | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    transition_proof: EnrollmentTransitionProofV2 | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    bundle: str = Field(max_length=MAX_BUNDLE_BYTES * 2)
    signatures: str = Field(max_length=1024 * 1024)
    anchor: str = Field(pattern=r"^[0-9a-f]{40}$")
    external_write_attempted: bool = False
    publication_commit: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    pull_request_number: int | None = Field(default=None, gt=0)
    pull_request_url: str | None = Field(default=None, min_length=1, max_length=2048)

    @model_validator(mode="after")
    def validate_pending_receipt(self) -> EncryptedPublicationDraft:
        enrollment_attestation = False
        if type(self.manifest) is TeamStateManifestV2:
            from intent_engineering.team_state.models import StateSignatureEnvelopeV2

            try:
                envelope = StateSignatureEnvelopeV2.model_validate_json(
                    base64.b64decode(self.signatures, validate=True)
                )
            except (TypeError, ValueError):
                raise ValueError("invalid publication authority") from None
            enrollment_attestation = (
                envelope.authority_attestation is not None
                and envelope.authority_attestation.operation == "enroll"
            )
        if (type(self.manifest) is TeamStateManifest) != (self.authority is None) or (
            type(self.manifest) is TeamStateManifestV2
            and self.authority is not None
            and (
                self.manifest.authority_digest
                != "sha256:" + hashlib.sha256(self.authority.canonical_bytes()).hexdigest()
                or self.manifest.recipient_key_ids != self.authority.active_recipient_key_ids()
            )
        ):
            raise ValueError("invalid publication authority")
        if enrollment_attestation != (self.transition_proof is not None):
            raise ValueError("invalid enrollment publication proof")
        if self.transition_proof is not None and (
            type(self.manifest) is not TeamStateManifestV2
            or self.transition_proof.publication_manifest_digest
            != "sha256:" + hashlib.sha256(self.manifest.canonical_bytes()).hexdigest()
            or self.transition_proof.authority_after_digest != self.manifest.authority_digest
            or self.transition_proof.authority_attestation != envelope.authority_attestation
        ):
            raise ValueError("invalid enrollment publication proof")
        if self.publication_commit is not None and not self.external_write_attempted:
            raise ValueError("invalid publication receipt")
        if self.publication_commit is None and (
            self.pull_request_number is not None or self.pull_request_url is not None
        ):
            raise ValueError("invalid publication receipt")
        if (self.pull_request_number is None) != (self.pull_request_url is None):
            raise ValueError("invalid publication receipt")
        if self.pull_request_number is not None:
            expected = (
                f"https://github.com/{self.manifest.repository_id.removeprefix('github.com/')}"
                f"/pull/{self.pull_request_number}"
            )
            if self.pull_request_url != expected:
                raise ValueError("invalid publication receipt")
        return self

    def publication(
        self,
    ) -> PreparedStatePublication:
        suffix = (
            f"{self.manifest.graph_version}-{self.manifest.bundle_digest.removeprefix('sha256:')}"
        )
        if type(self.manifest) is TeamStateManifest:
            return PreparedPublication(
                repository_id=self.manifest.repository_id,
                branch=f"intent-publication/{self.manifest.bundle_digest.removeprefix('sha256:')}",
                manifest=self.manifest,
                manifest_bytes=canonical_manifest_bytes(self.manifest),
                bundle=base64.b64decode(self.bundle, validate=True),
                signatures=base64.b64decode(self.signatures, validate=True),
                bundle_path=f"bundles/{suffix}.intent",
                signature_path=f"signatures/{suffix}.json",
            )
        assert self.authority is not None
        from intent_engineering.team_state.models import StateSignatureEnvelopeV2

        envelope = StateSignatureEnvelopeV2.model_validate_json(
            base64.b64decode(self.signatures, validate=True)
        )
        publication_type = (
            PreparedEnrollmentPublicationV2
            if envelope.authority_attestation is not None
            and envelope.authority_attestation.operation == "enroll"
            else PreparedPublicationV2
        )
        publication = publication_type(
            repository_id=self.manifest.repository_id,
            branch=f"intent-publication/{self.manifest.bundle_digest.removeprefix('sha256:')}",
            manifest=cast(TeamStateManifestV2, self.manifest),
            manifest_bytes=canonical_manifest_bytes(self.manifest),
            bundle=base64.b64decode(self.bundle, validate=True),
            envelope=envelope,
            signatures=base64.b64decode(self.signatures, validate=True),
            bundle_path=f"bundles/{suffix}.intent",
            signature_path=f"signatures/{suffix}.json",
            authority=self.authority,
        )
        if type(publication) is PreparedEnrollmentPublicationV2:
            assert self.transition_proof is not None
            return authenticate_enrollment_publication(publication, self.transition_proof)
        return publication


class GitHubBootstrapReceipt(StrictModel):
    """Durable public receipt for one owned canonical empty orphan anchor."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    repository_id: str
    anchor: str = Field(pattern=r"^[0-9a-f]{40}$")
    tree: str = Field(pattern=r"^[0-9a-f]{40}$")
    tooling: GitHubDefaultBranchTooling
    authority_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


def _parse_publication_draft(content: bytes) -> tuple[EncryptedPublicationDraft, bytes, bool]:
    try:
        raw = json.loads(content)
    except (TypeError, ValueError):
        raise ValueError("GitHub publication draft changed") from None
    if type(raw) is not dict:
        raise ValueError("GitHub publication draft changed")
    legacy = "external_write_attempted" not in raw
    if legacy:
        raw["external_write_attempted"] = True
        migrated_input = json.dumps(
            raw,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode()
        draft = EncryptedPublicationDraft.model_validate_json(migrated_input)
    else:
        draft = EncryptedPublicationDraft.model_validate_json(content)
    if legacy:
        if draft.model_dump_json(exclude={"external_write_attempted"}).encode() != content:
            raise ValueError("GitHub publication draft changed")
    elif draft.model_dump_json().encode() != content:
        raise ValueError("GitHub publication draft changed")
    return draft, draft.model_dump_json().encode(), legacy


def _bootstrap_receipt(
    runtime: Runtime,
    *,
    receipt: GitHubBootstrapReceipt | None = None,
    discard: bool = False,
) -> GitHubBootstrapReceipt | None:
    target = runtime.workspace_directory.file("team-bootstrap.json")
    try:
        with same_path_lock(target):
            content = target.read_optional_nonblocking(max_bytes=65536)
            if receipt is None:
                if content is None:
                    return None
                existing = GitHubBootstrapReceipt.model_validate_json(content)
                if existing.model_dump_json().encode() != content:
                    raise ValueError("GitHub bootstrap receipt changed")
                return existing
            encoded = receipt.model_dump_json().encode()
            if len(encoded) > 65536:
                raise ValueError("GitHub bootstrap receipt unavailable")
            if content is not None and content != encoded:
                raise ValueError("GitHub bootstrap receipt changed")
            if discard:
                if content is not None:
                    target.unlink()
                return None
            if content is None:
                target.atomic_write(encoded, reject_target_races=True)
            return receipt
    finally:
        target.close()


def _enrollment_receipt(
    runtime: Runtime,
    *,
    receipt: EnrollmentReceiptV2 | None = None,
    discard: bool = False,
) -> EnrollmentReceiptV2 | None:
    """Read or monotonically advance one public enrollment-publication receipt."""
    _recover_enrollment_publication_state(runtime)
    target = runtime.workspace_directory.file("team-enrollment-receipt.json")
    try:
        with same_path_lock(target):
            content = target.read_optional_nonblocking(max_bytes=128 * 1024)
            if content == _DISCARDED_ENROLLMENT_STATE:
                content = None
            existing: EnrollmentReceiptV2 | None = None
            if content is not None:
                existing = EnrollmentReceiptV2.model_validate_json(content)
                if existing.canonical_bytes() != content:
                    raise ValueError("team enrollment receipt changed")
            if receipt is None:
                if discard:
                    if existing is None or existing.phase not in {"approved", "merged", "closed"}:
                        raise ValueError("team enrollment requires reconciliation")
                    target.unlink()
                    return None
                return existing
            value = EnrollmentReceiptV2.model_validate_json(receipt.canonical_bytes())
            encoded = value.canonical_bytes()
            if len(encoded) > 128 * 1024:
                raise ValueError("team enrollment receipt unavailable")
            if existing is None:
                if discard:
                    return None
                if value.phase != "approved":
                    raise ValueError("team enrollment receipt changed")
                target.atomic_write(encoded, reject_target_races=True)
                return value
            if discard:
                if existing != value or existing.phase not in {"approved", "merged", "closed"}:
                    raise ValueError("team enrollment requires reconciliation")
                target.unlink()
                return None
            immutable = {
                "invite_id",
                "response_digest",
                "authority_before_digest",
                "authority_after_digest",
                "publication_manifest_digest",
                "approval_request_digest",
                "approved_github_preflight",
                "sponsor_decision",
                "transition_proof",
            }
            if any(getattr(existing, name) != getattr(value, name) for name in immutable):
                raise ValueError("team enrollment receipt changed")
            if existing == value:
                return existing
            ranks = {
                "approved": 0,
                "publication-pending": 1,
                "pr-pending": 2,
                "merged": 3,
                "closed": 3,
            }
            closing = existing.phase == "pr-pending" and value.phase == "closed"
            same_phase_commit = (
                existing.phase == value.phase == "publication-pending"
                and existing.publication_commit is None
                and value.publication_commit is not None
            )
            if (
                (
                    ranks[value.phase] <= ranks[existing.phase]
                    and not closing
                    and not same_phase_commit
                )
                or (
                    existing.publication_commit is not None
                    and existing.publication_commit != value.publication_commit
                )
                or (
                    existing.pull_request_number is not None
                    and (
                        existing.pull_request_number != value.pull_request_number
                        or existing.pull_request_url != value.pull_request_url
                    )
                )
                or existing.merged_commit is not None
            ):
                raise ValueError("team enrollment receipt changed")
            target.atomic_write(encoded, reject_target_races=True)
            return value
    finally:
        target.close()


def _draft(
    runtime: Runtime,
    *,
    prepared: PreparedStatePublication | None = None,
    transition_proof: EnrollmentTransitionProofV2 | None = None,
    anchor: str | None = None,
    publication_commit: str | None = None,
    pull_request_number: int | None = None,
    pull_request_url: str | None = None,
    external_write_attempted: bool | None = None,
    restart_closed: bool = False,
    discard: bool = False,
) -> EncryptedPublicationDraft | None:
    _recover_enrollment_publication_state(runtime)
    target = runtime.workspace_directory.file("team-publication.json")
    try:
        with same_path_lock(target):
            content = target.read_optional_nonblocking(
                max_bytes=MAX_BUNDLE_BYTES * 2 + 2 * 1024 * 1024
            )
            if content == _DISCARDED_ENROLLMENT_STATE:
                content = None
            if prepared is not None:
                existing: EncryptedPublicationDraft | None = None
                if content is not None:
                    existing, migrated, legacy = _parse_publication_draft(content)
                    if legacy:
                        target.atomic_write(migrated, reject_target_races=True)
                        content = migrated
                effective_proof = (
                    transition_proof
                    if transition_proof is not None
                    else (existing.transition_proof if existing is not None else None)
                )
                if type(prepared) is PreparedEnrollmentPublicationV2:
                    if effective_proof is None:
                        raise ValueError("enrollment publication authentication failed")
                    prepared = authenticate_enrollment_publication(prepared, effective_proof)
                draft = EncryptedPublicationDraft(
                    manifest=prepared.manifest,
                    authority=(
                        cast(
                            PreparedPublicationV2 | PreparedEnrollmentPublicationV2,
                            prepared,
                        ).authority
                        if type(prepared)
                        in {PreparedPublicationV2, PreparedEnrollmentPublicationV2}
                        else None
                    ),
                    transition_proof=effective_proof,
                    bundle=base64.b64encode(prepared.bundle).decode("ascii"),
                    signatures=base64.b64encode(prepared.signatures).decode("ascii"),
                    anchor=cast(str, anchor),
                    external_write_attempted=(
                        external_write_attempted
                        if external_write_attempted is not None
                        else (existing.external_write_attempted if existing is not None else False)
                    ),
                    publication_commit=publication_commit,
                    pull_request_number=pull_request_number,
                    pull_request_url=pull_request_url,
                )
                encoded = draft.model_dump_json().encode()
                if content is None:
                    if discard:
                        return None
                    target.atomic_write(encoded, reject_target_races=True)
                    return draft
                assert existing is not None
                if (
                    existing.manifest != draft.manifest
                    or existing.authority != draft.authority
                    or existing.transition_proof != draft.transition_proof
                    or existing.bundle != draft.bundle
                    or existing.signatures != draft.signatures
                    or existing.anchor != draft.anchor
                ):
                    raise ValueError("GitHub publication draft changed")
                if discard:
                    target.unlink()
                    return None
                if existing == draft:
                    return existing
                existing_rank = (
                    3
                    if existing.pull_request_number is not None
                    else 2
                    if existing.publication_commit is not None
                    else 1
                    if existing.external_write_attempted
                    else 0
                )
                draft_rank = (
                    3
                    if draft.pull_request_number is not None
                    else 2
                    if draft.publication_commit is not None
                    else 1
                    if draft.external_write_attempted
                    else 0
                )
                closed_restart = (
                    restart_closed
                    and existing_rank == 3
                    and draft_rank == 2
                    and existing.publication_commit == draft.publication_commit
                )
                if (draft_rank <= existing_rank and not closed_restart) or (
                    existing.publication_commit is not None
                    and existing.publication_commit != draft.publication_commit
                ):
                    raise ValueError("GitHub publication draft changed")
                target.atomic_write(encoded, reject_target_races=True)
                return draft
            if content is None:
                return None
            draft, migrated, legacy = _parse_publication_draft(content)
            if legacy:
                target.atomic_write(migrated, reject_target_races=True)
            return draft
    finally:
        target.close()


def _recover_enrollment_publication_state(runtime: Runtime) -> None:
    session_target = runtime.workspace_directory.file("team-enrollment-session.json")
    approval_target = runtime.workspace_directory.file("team-enrollment-approval.json")
    draft_target = runtime.workspace_directory.file("team-publication.json")
    receipt_target = runtime.workspace_directory.file("team-enrollment-receipt.json")
    session_journal = runtime.workspace_directory.file("team-enrollment-session-transaction.json")
    journal = runtime.workspace_directory.file("team-enrollment-restart-transaction.json")
    session_coordinator: LocalTransactionCoordinator | None = None
    coordinator: LocalTransactionCoordinator | None = None
    try:
        session_coordinator = LocalTransactionCoordinator(
            session_journal,
            {
                "session": session_target,
                "approval": approval_target,
                "draft": draft_target,
                "receipt": receipt_target,
            },
            legacy_target_sets=(frozenset({"session", "approval"}),),
        )
        session_coordinator.recover()
        coordinator = LocalTransactionCoordinator(
            journal,
            {"draft": draft_target, "receipt": receipt_target},
        )
        coordinator.recover()
    finally:
        if coordinator is not None:
            coordinator.close()
        if session_coordinator is not None:
            session_coordinator.close()
        journal.close()
        session_journal.close()
        receipt_target.close()
        draft_target.close()
        approval_target.close()
        session_target.close()


def _stage_enrollment_approval(
    runtime: Runtime,
    *,
    publication: PreparedEnrollmentPublicationV2,
    transition_proof: EnrollmentTransitionProofV2,
    anchor: str,
    receipt: EnrollmentReceiptV2,
    fault_hook: Callable[[str], None] | None = None,
) -> tuple[EncryptedPublicationDraft, EnrollmentReceiptV2]:
    """Atomically stage the exact authenticated publication and approval receipt."""
    publication = authenticate_enrollment_publication(publication, transition_proof)
    draft = EncryptedPublicationDraft(
        manifest=publication.manifest,
        authority=publication.authority,
        transition_proof=transition_proof,
        bundle=base64.b64encode(publication.bundle).decode("ascii"),
        signatures=base64.b64encode(publication.signatures).decode("ascii"),
        anchor=anchor,
    )
    draft_content = draft.model_dump_json().encode()
    receipt_content = receipt.canonical_bytes()
    if (
        receipt.phase != "approved"
        or receipt.transition_proof != transition_proof
        or receipt.publication_manifest_digest
        != "sha256:" + hashlib.sha256(publication.manifest_bytes).hexdigest()
        or len(receipt_content) > 128 * 1024
    ):
        raise ValueError("team enrollment changed")
    draft_target = runtime.workspace_directory.file("team-publication.json")
    receipt_target = runtime.workspace_directory.file("team-enrollment-receipt.json")
    journal = runtime.workspace_directory.file("team-enrollment-restart-transaction.json")
    coordinator: LocalTransactionCoordinator | None = None
    try:
        coordinator = LocalTransactionCoordinator(
            journal,
            {"draft": draft_target, "receipt": receipt_target},
            fault_hook=fault_hook,
        )
        with coordinator.transaction(rollback_base_exceptions=True) as transaction:
            existing_draft = transaction.read_optional_bounded(
                "draft", max_bytes=MAX_BUNDLE_BYTES * 2 + 2 * 1024 * 1024
            )
            existing_receipt = transaction.read_optional_bounded("receipt", max_bytes=128 * 1024)
            if existing_draft is None and existing_receipt is None:
                transaction.write("draft", draft_content)
                transaction.write("receipt", receipt_content)
            elif existing_draft != draft_content or existing_receipt != receipt_content:
                raise ValueError("team enrollment changed")
        return draft, receipt
    finally:
        if coordinator is not None:
            coordinator.close()
        journal.close()
        receipt_target.close()
        draft_target.close()


def _discard_member_publication_state(
    runtime: Runtime, *, draft: EncryptedPublicationDraft
) -> None:
    """Retire only the exact ordinary member publication draft."""
    publication = draft.publication()
    if type(publication) is not PreparedPublicationV2 or draft.transition_proof is not None:
        raise ValueError("team publication changed")
    discarded = _draft(
        runtime,
        prepared=publication,
        anchor=draft.anchor,
        external_write_attempted=draft.external_write_attempted,
        publication_commit=draft.publication_commit,
        pull_request_number=draft.pull_request_number,
        pull_request_url=draft.pull_request_url,
        discard=True,
    )
    if discarded is not None:
        raise ValueError("team publication changed")


def save_setup_request(runtime: Runtime, preview: GitHubEnablePreview) -> None:
    request = GitHubSetupRequest(preview=preview)
    content = request.model_dump_json().encode("utf-8")
    if len(content) > 65536:
        raise ValueError("GitHub setup request unavailable")
    target = runtime.workspace_directory.file("team-setup.json")
    try:
        with same_path_lock(target):
            old = target.read_optional_nonblocking(max_bytes=65536)
            if old is not None and old != content:
                raise ValueError("GitHub setup request changed")
            if old is None:
                target.atomic_write(content, reject_target_races=True)
    finally:
        target.close()


def load_setup_request(runtime: Runtime) -> GitHubSetupRequest | None:
    target = runtime.workspace_directory.file("team-setup.json")
    try:
        content = target.read_optional_nonblocking(max_bytes=65536)
        if content is None:
            return None
        request = GitHubSetupRequest.model_validate_json(content)
        if (
            request.preview.project_id != runtime.config.project_id
            or request.preview.actor != runtime.config.local_actor
            or content != request.model_dump_json().encode()
            or request.preview.preview_digest
            != _digest(request.preview.model_dump(mode="json", exclude={"state", "preview_digest"}))
        ):
            raise ValueError("GitHub setup request changed")
        return request
    finally:
        target.close()


def _enrollment_context(
    runtime: Runtime,
    *,
    context: GitHubEnrollmentContext | None = None,
) -> GitHubEnrollmentContext | None:
    target = runtime.workspace_directory.file("team-github-context.json")
    try:
        with same_path_lock(target):
            content = target.read_optional_nonblocking(max_bytes=65536)
            if context is None:
                if content is None:
                    return None
                existing = GitHubEnrollmentContext.model_validate_json(content)
                if (
                    existing.canonical_bytes() != content
                    or existing.request.preview.project_id != runtime.config.project_id
                    or existing.request.preview.actor != runtime.config.local_actor
                    or existing.request.preview.preview_digest
                    != _digest(
                        existing.request.preview.model_dump(
                            mode="json", exclude={"state", "preview_digest"}
                        )
                    )
                ):
                    raise ValueError("GitHub enrollment context changed")
                return existing
            value = GitHubEnrollmentContext.model_validate_json(context.canonical_bytes())
            if (
                value.request.preview.project_id != runtime.config.project_id
                or value.request.preview.actor != runtime.config.local_actor
                or value.request.preview.preview_digest
                != _digest(
                    value.request.preview.model_dump(
                        mode="json", exclude={"state", "preview_digest"}
                    )
                )
            ):
                raise ValueError("GitHub enrollment context changed")
            encoded = value.canonical_bytes()
            if content is None:
                target.atomic_write(encoded, reject_target_races=True)
            elif content != encoded:
                raise ValueError("GitHub enrollment context changed")
            return value
    finally:
        target.close()


def _complete_setup_request(runtime: Runtime, request: GitHubSetupRequest) -> None:
    """Remove only the exact completed handoff; allow later explicit setup requests."""
    _enrollment_context(runtime, context=GitHubEnrollmentContext(request=request))
    target = runtime.workspace_directory.file("team-setup.json")
    try:
        with same_path_lock(target):
            content = target.read_optional_nonblocking(max_bytes=65536)
            if content is None:
                return
            if content != request.model_dump_json().encode():
                raise ValueError("GitHub setup request changed")
            target.unlink()
    finally:
        target.close()


def github_api() -> GitHubTeamStateApi:
    """Resolve local credentials only within a confirmed UI operation."""
    return GitHubClient(GitHubCredentials.resolve(os.environ))


class _OneTimeIdentity:
    def __init__(self, identity: GitHubIdentity) -> None:
        self.identity: GitHubIdentity | None = identity

    def verify(self, proof: bytes) -> GitHubIdentity:
        identity, self.identity = self.identity, None
        if proof != b"local-github-inspection" or identity is None:
            raise ValueError("GitHub identity unavailable")
        return identity


class _PublicationTransport:
    def __init__(self) -> None:
        from intent_engineering.team_state.github_publication import GitHubApiPublisher

        self.publisher: GitHubApiPublisher | None = None
        self.published_commit: str | None = None

    def publish(self, publication: PreparedStatePublication, *, base_commit: str | None) -> None:
        publisher = self.publisher
        if publisher is None:
            raise ValueError("GitHub publication transport unavailable")

        async def run() -> str:
            return await publisher.publish(publication, base_commit=base_commit)

        self.published_commit = None
        self.published_commit = anyio.from_thread.run(run)


class _GuardedApi:
    """Recheck server-owned human and repository authority before every provider write."""

    def __init__(
        self, api: GitHubTeamStateApi, before_write: Callable[[], Awaitable[None]]
    ) -> None:
        self.api, self.before_write = api, before_write

    async def request_json_object(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        payload: Mapping[str, object] | None = None,
        allowed_statuses: frozenset[int] = frozenset({200}),
    ) -> GitHubJsonResponse:
        if method != "GET":
            await self.before_write()
        return await self.api.request_json_object(
            method, path, params=params, payload=payload, allowed_statuses=allowed_statuses
        )

    async def get_pages(
        self, path: str, params: Mapping[str, str], etag: str | None = None
    ) -> PageResult:
        return await self.api.get_pages(path, params, etag)

    async def request_bytes(
        self,
        method: str,
        path: str,
        *,
        max_bytes: int,
        accept: str,
    ) -> bytes:
        return await self.api.request_bytes(
            method,
            path,
            max_bytes=max_bytes,
            accept=accept,
        )

    async def aclose(self) -> None:
        await self.api.aclose()


class _CloseOnceApi:
    """Make nested GitHub cancellation boundaries share one physical close."""

    def __init__(self, api: GitHubTeamStateApi) -> None:
        self.api = api
        self.closed = False

    async def request_json_object(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        payload: Mapping[str, object] | None = None,
        allowed_statuses: frozenset[int] = frozenset({200}),
    ) -> GitHubJsonResponse:
        return await self.api.request_json_object(
            method, path, params=params, payload=payload, allowed_statuses=allowed_statuses
        )

    async def get_pages(
        self, path: str, params: Mapping[str, str], etag: str | None = None
    ) -> PageResult:
        return await self.api.get_pages(path, params, etag)

    async def request_bytes(
        self,
        method: str,
        path: str,
        *,
        max_bytes: int,
        accept: str,
    ) -> bytes:
        return await self.api.request_bytes(method, path, max_bytes=max_bytes, accept=accept)

    async def aclose(self) -> None:
        if not self.closed:
            self.closed = True
            await self.api.aclose()


def _setup_failure(error: BaseException, message: str) -> BaseException:
    old_traceback = error.__traceback__
    if old_traceback is not None:
        traceback.clear_frames(old_traceback)
    old_traceback = None
    error.args = ()
    error.__dict__.clear()
    error.__traceback__ = None
    error.__cause__ = None
    error.__context__ = None
    return error if not isinstance(error, Exception) else ValueError(message)


def _digest(value: object) -> str:
    content = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()
    return "sha256:" + hashlib.sha256(content).hexdigest()


class GitHubSetupBridge:
    """Server-owned setup session; browser data never fabricates a verified decision."""

    def __init__(
        self,
        service: ControlPlaneService,
        *,
        request: GitHubSetupRequest | None = None,
    ) -> None:
        saved_request = load_setup_request(service._runtime)
        context = _enrollment_context(service._runtime)
        persisted = (
            saved_request
            if saved_request is not None
            else (None if context is None else context.request)
        )
        if request is not None:
            request = GitHubSetupRequest.model_validate(request.model_dump(mode="python"))
            if persisted is not None and persisted != request:
                raise ValueError("GitHub setup changed")
        else:
            request = persisted
        if request is None:
            raise ValueError("GitHub setup unavailable")
        configured_repository = getattr(service, "_team_repository_id", None)
        if request.preview.project_id != service._runtime.config.project_id or (
            configured_repository is not None
            and request.preview.repository_id != configured_repository
        ):
            raise ValueError("GitHub setup changed")
        self.service = service
        self.request = request
        self.repository = request.preview.repository_id.removeprefix("github.com/")
        self.identity: GitHubIdentity | None = None
        self.pending: HumanDecisionPayload | None = None
        self.reviewed: GitHubTeamStateStatus | None = None
        self.suggestions: CodeSuggestionPreview | None = None
        self.authority_digest: str | None = None
        self.protection_digest: str | None = None
        self.tooling: GitHubDefaultBranchTooling | None = None
        self.baseline: GitHubDefaultBranchBaseline | None = None
        self.setup_phase: str | None = None
        self.publication: PublicationService | None = None
        self.recipient_snapshot: RecipientRecord | None = None
        self.transport = _PublicationTransport()
        self.publication_restart_required = False
        self.guard = anyio.Lock()
        service._team_repository_id = request.preview.repository_id
        service._restore_team_recipient()

    def status(self) -> str:
        """Advisory local progress; each next action reinspects live provider authority."""
        if self.service._team_recipient is None:
            return "setup_required"
        draft = _draft(self.service._runtime)
        if draft is not None:
            if draft.manifest.repository_id != self.request.preview.repository_id:
                raise ValueError("GitHub publication draft changed")
            return (
                "publication_pending"
                if draft.pull_request_number is not None
                else "publication_recovery_required"
                if draft.external_write_attempted
                else "publication_draft"
            )
        try:
            SigningKeyStore(
                self.service._runtime.config.project_id,
                self.request.preview.repository_id,
                self.service._runtime.config.local_actor,
            ).public_keys()
        except ValueError:
            from intent_engineering.team_state.suggestions import preview_code_suggestions

            suggestions = preview_code_suggestions(
                self.service._runtime.root,
                self.request.preview.codeowners_suggestion,
                self.request.preview.workflow_suggestion,
                self.request.preview.check_workflow_suggestion,
            )
            installed = all(
                preimage is not None
                and base64.urlsafe_b64decode(preimage + "=" * (-len(preimage) % 4))
                == content.encode("utf-8")
                for preimage, content in (
                    (suggestions.codeowners_preimage, suggestions.codeowners_content),
                    (suggestions.workflow_preimage, suggestions.workflow_content),
                    (suggestions.check_workflow_preimage, suggestions.check_workflow_content),
                )
            )
            return "code_changes_staged" if installed else "enrolled"
        return "protection_configured"

    def _authority(self) -> str:
        from intent_engineering.control_plane.service import _parent_digest

        authority = self.service._authority()
        try:
            content = dict(authority.snapshot.content)
            content.pop("webauthn_credentials", None)
            return _parent_digest(content, authority.membership_digest)
        finally:
            authority.close()

    def _recipient(self, status: GitHubTeamStateStatus) -> RecipientRecord:
        self.service._restore_team_recipient()
        recipient = self.service._team_recipient
        if (
            recipient is None
            or self.service._team_enrollment_blocked
            or recipient.repository_id != status.repository_id
            or recipient.github_account_id != status.account_id
            or recipient.github_login != status.login
            or (self.recipient_snapshot is not None and recipient != self.recipient_snapshot)
        ):
            raise ValueError("GitHub enrollment changed")
        return recipient

    def _publication_authority(self) -> PublicationAuthority:
        reviewed = self.reviewed
        if reviewed is None or reviewed.branch_commit is None or not reviewed.protection_compatible:
            raise ValueError("GitHub publication authority unavailable")
        self._recipient(reviewed)
        recipient = self.service._team_recipient
        if recipient is None or self._authority() != self.authority_digest:
            raise ValueError("GitHub publication authority changed")
        signing = SigningKeyStore(recipient.project_id, recipient.repository_id, recipient.actor)
        ci = self.request.preview.ci_recipient
        if (
            ci is None
            or ci.project_id != recipient.project_id
            or ci.repository_id != recipient.repository_id
        ):
            raise ValueError("CI recipient unavailable")
        return PublicationAuthority(
            recipients=tuple(sorted((recipient, ci), key=lambda item: item.key_id)),
            signing_private_keys=signing.load_signing_keys(),
            remote_state=None,
            publication_base_commit=reviewed.branch_commit,
        )

    async def _anchor(self, api: GitHubTeamStateApi, status: GitHubTeamStateStatus) -> None:
        """An existing genesis base is trusted only as a verified orphan empty tree."""
        if status.branch_commit is None:
            return
        commit = await api.request_json_object(
            "GET", f"/repos/{self.repository}/git/commits/{status.branch_commit}"
        )
        tree = commit.payload.get("tree")
        if (
            commit.payload.get("sha") != status.branch_commit
            or commit.payload.get("parents") != []
            or type(tree) is not dict
        ):
            raise ValueError("GitHub bootstrap anchor changed")
        sha = tree.get("sha")
        if type(sha) is not str or len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha):
            raise ValueError("GitHub bootstrap anchor changed")
        result = await api.request_json_object("GET", f"/repos/{self.repository}/git/trees/{sha}")
        if (
            result.payload.get("sha") != sha
            or result.payload.get("tree") != []
            or result.payload.get("truncated") is not False
        ):
            raise ValueError("GitHub bootstrap anchor changed")

    async def _finalize_merged_publication(
        self,
        client: GitHubTeamStateClient,
        status: GitHubTeamStateStatus,
    ) -> dict[str, object] | None:
        """Persist trust only after the exact reviewed PR commit becomes protected state."""
        draft = _draft(self.service._runtime)
        if draft is None or draft.publication_commit is None:
            return None
        if status.branch_commit == draft.anchor:
            if draft.pull_request_number is not None:
                state = await client.publication_pull_request_state(
                    draft.publication(),
                    expected_head_commit=draft.publication_commit,
                    expected_base_commit=draft.anchor,
                    pull_request_number=draft.pull_request_number,
                    transition_proof=draft.transition_proof,
                )
                self.publication_restart_required = state == "closed"
            return None
        if (
            status.branch_commit is None
            or draft.pull_request_number is None
            or draft.pull_request_url is None
        ):
            raise ValueError("GitHub publication merge changed")
        await client.confirm_publication_merge(
            draft.publication(),
            expected_head_commit=draft.publication_commit,
            expected_base_commit=draft.anchor,
            pull_request_number=draft.pull_request_number,
            transition_proof=draft.transition_proof,
        )
        from intent_engineering.team_state.local_trust import save_local_trust

        save_local_trust(
            self.service._runtime,
            self._recipient(status),
            SigningKeyStore(
                self.service._runtime.config.project_id,
                status.repository_id,
                self.service._runtime.config.local_actor,
            ).public_keys(),
        )
        _draft(
            self.service._runtime,
            prepared=draft.publication(),
            anchor=draft.anchor,
            publication_commit=draft.publication_commit,
            pull_request_number=draft.pull_request_number,
            pull_request_url=draft.pull_request_url,
            discard=True,
        )
        _complete_setup_request(self.service._runtime, self.request)
        self.service._github_setup_bridge = None
        return {
            "state": "published",
            "pull_request_url": draft.pull_request_url,
            "repository_id": status.repository_id,
        }

    async def _member_preflight(
        self,
        api: GitHubTeamStateApi,
        current: VerifiedRemoteStateV2,
        sponsor_certificate_id: str,
        *,
        require_sponsor: bool = True,
    ) -> tuple[GitHubTeamStateStatus, GitHubTeamStateClient, GitHubProtectionPreview]:
        """Reconstruct every GitHub-owned enrollment preimage from live reads."""
        current = VerifiedRemoteStateV2.model_validate(current.model_dump(mode="python"))
        sponsor = next(
            certificate
            for certificate in current.authority.device_certificates
            if certificate.certificate_id == sponsor_certificate_id
        )
        sponsor_member = next(
            member
            for member in current.authority.members
            if member.member_id == sponsor.claims.member_id
        )
        if (
            (require_sponsor and sponsor_member.role != "sponsor")
            or sponsor_member.status != "active"
            or sponsor.certificate_id not in sponsor_member.device_certificate_ids
        ):
            raise ValueError("team enrollment changed")
        client = GitHubTeamStateClient(
            api,
            expected_account_id=str(sponsor.claims.github_account_id),
            expected_login=sponsor.claims.github_login,
        )
        status = await client.inspect(self.repository)
        if (
            status.repository_id != current.authority.repository_id
            or status.branch_commit != current.state_commit
            or status.default_branch != current.default_branch
            or status.default_branch_commit != current.default_branch_commit
            or not status.protection_compatible
        ):
            raise ValueError("team enrollment changed")
        tooling = await client.verify_default_branch_tooling(
            codeowners=self.request.preview.codeowners_suggestion.encode("utf-8"),
            workflow=self.request.preview.workflow_suggestion.encode("utf-8"),
            check_workflow=self.request.preview.check_workflow_suggestion.encode("utf-8"),
            runner_id=(
                self.request.preview.ci_recipient.runner_id
                if self.request.preview.ci_recipient is not None
                else ""
            ),
        )
        if _digest(tooling.model_dump(mode="json")) != current.tooling_digest:
            raise ValueError("team enrollment changed")
        protection = client.protection_preview()
        if protection.requires_change or protection.branch_creation_required:
            raise ValueError("team enrollment changed")
        return status, client, protection

    async def publish_member_state(
        self,
        *,
        publication: PublicationService,
        decision: VerifiedHumanDecision,
        current: VerifiedRemoteStateV2,
        certificate_id: str,
        protection: GitHubProtectionPreview,
        verify_local: Callable[[], None],
    ) -> dict[str, object]:
        """Publish an ordinary v2 member release through the existing guarded PR transport."""
        from intent_engineering.team_state.github_publication import GitHubApiPublisher
        from intent_engineering.team_state.publication import PreparedPublicationV2

        api: _CloseOnceApi | None = None
        try:
            api = _CloseOnceApi(github_api())
            status, _, live = await self._member_preflight(
                api, current, certificate_id, require_sponsor=False
            )
            if live != protection:
                raise ValueError("team publication changed")
            prepared = cast(
                PreparedPublication | PreparedPublicationV2, publication.pending_publication()
            )
            if type(prepared) is not PreparedPublicationV2:
                raise ValueError("team publication changed")
            old = _draft(self.service._runtime)
            if old is not None and (
                old.publication() != prepared or old.anchor != current.state_commit
            ):
                raise ValueError("team publication changed")
            _draft(
                self.service._runtime,
                prepared=prepared,
                anchor=current.state_commit,
                external_write_attempted=True,
                publication_commit=None if old is None else old.publication_commit,
                pull_request_number=None if old is None else old.pull_request_number,
                pull_request_url=None if old is None else old.pull_request_url,
            )

            async def require_live() -> None:
                assert api is not None
                verify_local()
                _, _, actual = await self._member_preflight(
                    api, current, certificate_id, require_sponsor=False
                )
                if actual != protection or self.service._now() > decision.payload.expires_at:
                    raise ValueError("team publication changed")

            guarded = _GuardedApi(api, require_live)
            certificate = next(
                c
                for c in current.authority.device_certificates
                if c.certificate_id == certificate_id
            )
            client = GitHubTeamStateClient(
                guarded,
                expected_account_id=str(certificate.claims.github_account_id),
                expected_login=certificate.claims.github_login,
            )
            reviewed = await client.inspect(self.repository)
            if reviewed != status:
                raise ValueError("team publication changed")
            self.transport.publisher = GitHubApiPublisher(guarded, client, status)
            try:
                await anyio.to_thread.run_sync(
                    lambda: publication.prepare(decision, now=self.service._now())
                )
            finally:
                self.transport.publisher = None
            commit = self.transport.published_commit
            if commit is None:
                raise ValueError("team publication unavailable")
            _draft(
                self.service._runtime,
                prepared=prepared,
                anchor=current.state_commit,
                external_write_attempted=True,
                publication_commit=commit,
            )
            pr = await client.open_publication_pr(prepared, expected_head_commit=commit)
            _draft(
                self.service._runtime,
                prepared=prepared,
                anchor=current.state_commit,
                external_write_attempted=True,
                publication_commit=commit,
                pull_request_number=pr.number,
                pull_request_url=pr.url,
            )
            return {
                "state": "publication_pending",
                "repository_id": pr.repository_id,
                "pull_request_url": pr.url,
            }
        except BaseException as error:  # noqa: BLE001 - shared fixed publication boundary
            failure = _setup_failure(error, "team enrollment unavailable")
            del error, self, publication, decision, current, verify_local
            raise failure.with_traceback(None) from None
        finally:
            if api is not None:
                active_error = sys.exc_info()[1]
                with anyio.CancelScope(shield=True):
                    try:
                        await api.aclose()
                    except BaseException as close_error:  # noqa: BLE001 - fixed provider-close boundary
                        if active_error is None:
                            failure = _setup_failure(close_error, "team enrollment unavailable")
                            raise failure.with_traceback(None) from None

    async def reconcile_member_publication(
        self,
        *,
        current: VerifiedRemoteStateV2,
        certificate_id: str,
        restart_closed: bool = False,
    ) -> dict[str, object]:
        """Inspect, complete, or explicitly retire one exact ordinary member PR."""
        from intent_engineering.team_state.publication import PreparedPublicationV2

        api: _CloseOnceApi | None = None
        try:
            if type(restart_closed) is not bool:
                raise ValueError("team publication requires reconciliation")
            draft = _draft(self.service._runtime)
            if draft is None:
                return {"state": "member-active"}
            publication = draft.publication()
            if type(publication) is not PreparedPublicationV2 or draft.transition_proof is not None:
                raise ValueError("team publication changed")
            certificate = next(
                item
                for item in current.authority.device_certificates
                if item.certificate_id == certificate_id
            )
            api = _CloseOnceApi(github_api())
            client = GitHubTeamStateClient(
                api,
                expected_account_id=str(certificate.claims.github_account_id),
                expected_login=certificate.claims.github_login,
            )
            status = await client.inspect(self.repository)
            if status.repository_id != current.authority.repository_id:
                raise ValueError("team publication changed")
            if status.branch_commit != draft.anchor:
                if (
                    restart_closed
                    or draft.publication_commit is None
                    or draft.pull_request_number is None
                    or draft.pull_request_url is None
                ):
                    raise ValueError("team publication requires reconciliation")
                merged = await client.confirm_publication_merge(
                    publication,
                    expected_head_commit=draft.publication_commit,
                    expected_base_commit=draft.anchor,
                    pull_request_number=draft.pull_request_number,
                )
                if merged.branch_commit is None:
                    raise ValueError("team publication requires reconciliation")
                _discard_member_publication_state(self.service._runtime, draft=draft)
                return {
                    "state": "published",
                    "repository_id": status.repository_id,
                    "pull_request_url": draft.pull_request_url,
                }
            if (
                draft.publication_commit is None
                or draft.pull_request_number is None
                or draft.pull_request_url is None
            ):
                if restart_closed:
                    raise ValueError("team publication requires reconciliation")
                return {"state": "publication_recovery_required"}
            _, _, protection = await self._member_preflight(
                api, current, certificate_id, require_sponsor=False
            )
            if protection.requires_change or protection.branch_creation_required:
                raise ValueError("team publication changed")
            state = await client.publication_pull_request_state(
                publication,
                expected_head_commit=draft.publication_commit,
                expected_base_commit=draft.anchor,
                pull_request_number=draft.pull_request_number,
            )
            if state == "merged":
                merged = await client.confirm_publication_merge(
                    publication,
                    expected_head_commit=draft.publication_commit,
                    expected_base_commit=draft.anchor,
                    pull_request_number=draft.pull_request_number,
                )
                if merged.branch_commit is None:
                    raise ValueError("team publication requires reconciliation")
                _discard_member_publication_state(self.service._runtime, draft=draft)
                return {
                    "state": "published",
                    "repository_id": status.repository_id,
                    "pull_request_url": draft.pull_request_url,
                }
            if state == "closed":
                if restart_closed:
                    _discard_member_publication_state(self.service._runtime, draft=draft)
                    return {"state": "member-active", "repository_id": status.repository_id}
                return {
                    "state": "publication_closed",
                    "repository_id": status.repository_id,
                    "pull_request_url": draft.pull_request_url,
                }
            if restart_closed:
                raise ValueError("team publication requires reconciliation")
            return {
                "state": "publication_pending",
                "repository_id": status.repository_id,
                "pull_request_url": draft.pull_request_url,
            }
        except BaseException as error:  # noqa: BLE001 - fixed member recovery boundary
            failure = _setup_failure(error, "team enrollment unavailable")
            del error, self, current
            raise failure.with_traceback(None) from None
        finally:
            if api is not None:
                active_error = sys.exc_info()[1]
                with anyio.CancelScope(shield=True):
                    try:
                        await api.aclose()
                    except BaseException as close_error:  # noqa: BLE001 - fixed close boundary
                        if active_error is None:
                            failure = _setup_failure(close_error, "team enrollment unavailable")
                            raise failure.with_traceback(None) from None

    async def _require_join_identity(
        self,
        api: GitHubTeamStateApi,
        request: EnrollmentApprovalRequest,
    ) -> None:
        """Re-resolve the invited account without trusting the response's display identity."""
        response = await api.request_json_object(
            "GET", f"/users/{request.preview.response.github_login}"
        )
        account_id = response.payload.get("id")
        login = response.payload.get("login")
        if (
            type(account_id) is not int
            or account_id != request.preview.response.github_account_id
            or type(login) is not str
            or login != request.preview.response.github_login
        ):
            raise ValueError("team enrollment changed")

    async def preview_member_approval(
        self,
        *,
        enrollment: TeamEnrollmentService,
        invite: TeamInviteV2,
        response: JoinResponseV2,
        current: VerifiedRemoteStateV2,
        parent: VerifiedReleaseV2,
        snapshot: CanonicalStateSnapshot,
        now: datetime,
    ) -> EnrollmentApprovalRequest:
        try:
            return await self._preview_member_approval_unsafe(
                enrollment=enrollment,
                invite=invite,
                response=response,
                current=current,
                parent=parent,
                snapshot=snapshot,
                now=now,
            )
        except BaseException as error:  # noqa: BLE001 - preserve cancellation identity
            failure = _setup_failure(error, "team enrollment unavailable")
            del self, enrollment, invite, response, current, parent, snapshot, now, error
            raise failure.with_traceback(None) from None

    async def _preview_member_approval_unsafe(
        self,
        *,
        enrollment: TeamEnrollmentService,
        invite: TeamInviteV2,
        response: JoinResponseV2,
        current: VerifiedRemoteStateV2,
        parent: VerifiedReleaseV2,
        snapshot: CanonicalStateSnapshot,
        now: datetime,
    ) -> EnrollmentApprovalRequest:
        """Bind one sponsor preview to the exact live GitHub protection/tooling state."""
        api: GitHubTeamStateApi | None = None
        try:
            api = _CloseOnceApi(github_api())
            _, _, protection = await self._member_preflight(
                api, current, invite.sponsor_certificate.certificate_id
            )
            preview = enrollment.preview_approval(
                invite=invite,
                response=response,
                current=current,
                now=now,
            )
            if (
                preview.default_branch_commit != current.default_branch_commit
                or preview.tooling_digest != current.tooling_digest
                or preview.base_state_commit != current.state_commit
                or preview.base_bundle_digest != current.bundle_digest
            ):
                raise ValueError("team enrollment changed")
            plan = enrollment.plan_publication(
                snapshot=snapshot,
                parent=parent,
                preview=preview,
                now=now,
            )
            return EnrollmentApprovalRequest(
                preview=preview,
                github_preflight=protection,
                publication_plan=plan,
                transition_plan_digest=enrollment_transition_plan_digest(preview, plan),
            )
        except BaseException:
            if api is not None:
                try:
                    with anyio.CancelScope(shield=True):
                        await api.aclose()
                except BaseException as cleanup:  # noqa: BLE001 - secondary signal is scrubbed
                    _setup_failure(cleanup, "team enrollment unavailable")
                    del cleanup
                api = None
            raise
        finally:
            if api is not None:
                await api.aclose()

    @staticmethod
    def _enrollment_receipt_for(
        request: EnrollmentApprovalRequest,
        publication: PreparedEnrollmentPublicationV2,
        *,
        sponsor_decision: VerifiedHumanDecision,
        transition_proof: EnrollmentTransitionProofV2,
        phase: Literal["approved", "publication-pending", "pr-pending", "merged", "closed"],
        publication_commit: str | None = None,
        pull_request_number: int | None = None,
        pull_request_url: str | None = None,
        merged_commit: str | None = None,
    ) -> EnrollmentReceiptV2:
        return EnrollmentReceiptV2(
            invite_id=request.preview.invite.invite_id,
            response_digest=request.preview.response_digest,
            authority_before_digest=request.preview.authority_before_digest,
            authority_after_digest=request.preview.authority_after_digest,
            publication_manifest_digest=(
                "sha256:" + hashlib.sha256(publication.manifest_bytes).hexdigest()
            ),
            approval_request_digest=request.digest(),
            approved_github_preflight=request.github_preflight,
            sponsor_decision=sponsor_decision.payload,
            transition_proof=transition_proof,
            phase=phase,
            publication_commit=publication_commit,
            pull_request_number=pull_request_number,
            pull_request_url=pull_request_url,
            merged_commit=merged_commit,
        )

    async def approve_member(
        self,
        *,
        request: EnrollmentApprovalRequest,
        enrollment: TeamEnrollmentService,
        sponsor_decision: VerifiedHumanDecision,
        sponsor_pre_assertion_sign_count: int,
        sponsor_assertion: bytes,
        current: VerifiedRemoteStateV2,
        parent: VerifiedReleaseV2,
        snapshot: CanonicalStateSnapshot,
        now: datetime,
    ) -> GitHubEnableResult:
        try:
            return await self._approve_member_unsafe(
                request=request,
                enrollment=enrollment,
                sponsor_decision=sponsor_decision,
                sponsor_pre_assertion_sign_count=sponsor_pre_assertion_sign_count,
                sponsor_assertion=sponsor_assertion,
                current=current,
                parent=parent,
                snapshot=snapshot,
                now=now,
            )
        except BaseException as error:  # noqa: BLE001 - preserve cancellation identity
            failure = _setup_failure(error, "team enrollment unavailable")
            sponsor_assertion = b""
            del self, request, enrollment, sponsor_decision, sponsor_pre_assertion_sign_count
            del sponsor_assertion, current, parent, snapshot, now, error
            raise failure.with_traceback(None) from None

    async def _approve_member_unsafe(
        self,
        *,
        request: EnrollmentApprovalRequest,
        enrollment: TeamEnrollmentService,
        sponsor_decision: VerifiedHumanDecision,
        sponsor_pre_assertion_sign_count: int,
        sponsor_assertion: bytes,
        current: VerifiedRemoteStateV2,
        parent: VerifiedReleaseV2,
        snapshot: CanonicalStateSnapshot,
        now: datetime,
    ) -> GitHubEnableResult:
        """Approve and publish one authority change through the guarded Task 6 PR path."""
        api: GitHubTeamStateApi | None = None
        try:
            api = _CloseOnceApi(github_api())
            status, _, protection = await self._member_preflight(
                api, current, request.preview.invite.sponsor_certificate.certificate_id
            )
            fresh = enrollment.preview_approval(
                invite=request.preview.invite,
                response=request.preview.response,
                current=current,
                now=now,
            )
            if request.preview != fresh or request.github_preflight != protection:
                raise ValueError("team enrollment changed")

            staged_transition: ApprovedAuthorityTransitionV2 | None = None
            publication: PreparedEnrollmentPublicationV2 | None = None
            transition_proof: EnrollmentTransitionProofV2 | None = None
            receipt: EnrollmentReceiptV2 | None = None

            def persist_approval(transition: ApprovedAuthorityTransitionV2) -> None:
                nonlocal staged_transition, publication, transition_proof, receipt
                if staged_transition is not None:
                    raise ValueError("team enrollment changed")
                staged_transition = transition
                publication = enrollment.prepare_publication(
                    snapshot=snapshot,
                    parent=parent,
                    transition=transition,
                    now=now,
                    plan=request.publication_plan,
                )
                transition_proof = enrollment.transition_proof(
                    preview=request.preview,
                    parent=parent,
                    publication=publication,
                )
                receipt = self._enrollment_receipt_for(
                    request,
                    publication,
                    sponsor_decision=sponsor_decision,
                    transition_proof=transition_proof,
                    phase="approved",
                )
                _stage_enrollment_approval(
                    self.service._runtime,
                    publication=publication,
                    transition_proof=transition_proof,
                    anchor=current.state_commit,
                    receipt=receipt,
                )

            transition = enrollment.approve(
                preview=request.preview,
                sponsor_decision=sponsor_decision,
                sponsor_pre_assertion_sign_count=sponsor_pre_assertion_sign_count,
                sponsor_assertion=sponsor_assertion,
                current=current,
                now=now,
                persist_approval=persist_approval,
                approval_request_digest=request.digest(),
            )
            if (
                transition != staged_transition
                or publication is None
                or transition_proof is None
                or receipt is None
            ):
                raise ValueError("team enrollment changed")
            _draft(
                self.service._runtime,
                prepared=publication,
                transition_proof=transition_proof,
                anchor=current.state_commit,
                external_write_attempted=True,
            )
            receipt = receipt.model_copy(update={"phase": "publication-pending"})
            _enrollment_receipt(self.service._runtime, receipt=receipt)
            authoritative_receipt = receipt

            async def require_live() -> None:
                assert api is not None
                live_draft = _draft(self.service._runtime)
                live_receipt = _enrollment_receipt(self.service._runtime)
                if (
                    live_draft is None
                    or live_receipt != authoritative_receipt
                    or live_draft.publication() != publication
                    or live_draft.transition_proof != transition_proof
                    or live_receipt.approval_request_digest != request.digest()
                    or live_receipt.approved_github_preflight != request.github_preflight
                ):
                    raise ValueError("team enrollment changed")
                await self._require_join_identity(api, request)
                live_now = self.service._now()
                _require_current_enrollment_decision(live_receipt, request, live_now)
                if type(sponsor_decision) is VerifiedHumanDecision:
                    expected_decision = build_sponsor_decision_payload(
                        preview=request.preview,
                        credential=sponsor_decision.credential,
                        challenge=b"placeholder-challenge",
                        now=sponsor_decision.payload.issued_at,
                        approval_request_digest=request.digest(),
                    ).model_copy(update={"challenge": sponsor_decision.payload.challenge})
                    live_preview = enrollment.preview_approval(
                        invite=request.preview.invite,
                        response=request.preview.response,
                        current=current,
                        now=live_now,
                    )
                    if (
                        sponsor_decision.payload != expected_decision
                        or not sponsor_decision.payload.issued_at
                        <= live_now
                        < sponsor_decision.payload.expires_at
                        or live_preview != request.preview
                    ):
                        raise ValueError("team enrollment changed")
                _, _, live_protection = await self._member_preflight(
                    api,
                    current,
                    request.preview.invite.sponsor_certificate.certificate_id,
                )
                if live_protection != request.github_preflight:
                    raise ValueError("team enrollment changed")

            guarded = _GuardedApi(api, require_live)
            live_client = GitHubTeamStateClient(
                guarded,
                expected_account_id=status.account_id,
                expected_login=status.login,
            )
            live_status = await live_client.inspect(self.repository)
            if live_status != status:
                raise ValueError("team enrollment changed")
            from intent_engineering.team_state.github_publication import GitHubApiPublisher

            commit = await GitHubApiPublisher(guarded, live_client, status).publish(
                publication,
                base_commit=current.state_commit,
                transition_proof=transition_proof,
            )
            _draft(
                self.service._runtime,
                prepared=publication,
                transition_proof=transition_proof,
                anchor=current.state_commit,
                external_write_attempted=True,
                publication_commit=commit,
            )
            receipt = receipt.model_copy(update={"publication_commit": commit})
            _enrollment_receipt(self.service._runtime, receipt=receipt)
            authoritative_receipt = receipt
            publication_pr = await live_client.open_publication_pr(
                publication,
                expected_head_commit=commit,
                transition_proof=transition_proof,
            )
            _draft(
                self.service._runtime,
                prepared=publication,
                transition_proof=transition_proof,
                anchor=current.state_commit,
                external_write_attempted=True,
                publication_commit=commit,
                pull_request_number=publication_pr.number,
                pull_request_url=publication_pr.url,
            )
            receipt = receipt.model_copy(
                update={
                    "phase": "pr-pending",
                    "pull_request_number": publication_pr.number,
                    "pull_request_url": publication_pr.url,
                }
            )
            _enrollment_receipt(self.service._runtime, receipt=receipt)
            return GitHubEnableResult(
                state="published",
                repository_id=status.repository_id,
                preview_digest=_digest(request.model_dump(mode="json")),
                protection_digest=request.github_preflight.digest,
                pull_request_url=publication_pr.url,
            )
        except BaseException:
            if api is not None:
                try:
                    with anyio.CancelScope(shield=True):
                        await api.aclose()
                except BaseException as cleanup:  # noqa: BLE001 - secondary signal is scrubbed
                    _setup_failure(cleanup, "team enrollment unavailable")
                    del cleanup
                api = None
            raise
        finally:
            sponsor_assertion = b""
            if api is not None:
                await api.aclose()

    async def reconcile_member_approval(
        self,
        *,
        request: EnrollmentApprovalRequest,
        current: VerifiedRemoteStateV2,
        restart_closed: bool = False,
    ) -> GitHubEnableResult:
        try:
            return await self._reconcile_member_approval_unsafe(
                request=request,
                current=current,
                restart_closed=restart_closed,
            )
        except BaseException as error:  # noqa: BLE001 - preserve cancellation identity
            message = (
                "team enrollment requires reconciliation"
                if str(error) == "team enrollment requires reconciliation"
                else "team enrollment unavailable"
            )
            failure = _setup_failure(error, message)
            del self, request, current, restart_closed, error, message
            raise failure.with_traceback(None) from None

    async def _reconcile_member_approval_unsafe(
        self,
        *,
        request: EnrollmentApprovalRequest,
        current: VerifiedRemoteStateV2,
        restart_closed: bool,
    ) -> GitHubEnableResult:
        """Resume only the exact receipted enrollment publication or attest its merge."""
        api: GitHubTeamStateApi | None = None
        try:
            if type(restart_closed) is not bool:
                raise ValueError("team enrollment requires reconciliation")
            draft = _draft(self.service._runtime)
            if draft is None:
                raise ValueError("team enrollment requires reconciliation")
            publication = draft.publication()
            receipt = _enrollment_receipt(self.service._runtime)
            if receipt is None:
                raise ValueError("team enrollment requires reconciliation")
            if (
                type(publication) is not PreparedEnrollmentPublicationV2
                or receipt.invite_id != request.preview.invite.invite_id
                or receipt.response_digest != request.preview.response_digest
                or receipt.authority_before_digest != request.preview.authority_before_digest
                or receipt.authority_after_digest != request.preview.authority_after_digest
                or receipt.transition_proof.authority_before_digest
                != request.preview.authority_before_digest
                or receipt.transition_proof.authority_after_digest
                != request.preview.authority_after_digest
                or receipt.publication_manifest_digest
                != "sha256:" + hashlib.sha256(publication.manifest_bytes).hexdigest()
                or receipt.approval_request_digest != request.digest()
                or receipt.approved_github_preflight != request.github_preflight
                or publication.authority != request.preview.authority_after
                or draft.anchor != request.preview.base_state_commit
            ):
                raise ValueError("team enrollment changed")
            api = _CloseOnceApi(github_api())
            sponsor = request.preview.invite.sponsor_certificate.claims
            client = GitHubTeamStateClient(
                api,
                expected_account_id=str(sponsor.github_account_id),
                expected_login=sponsor.github_login,
            )
            status = await client.inspect(self.repository)
            preview_digest = _digest(request.model_dump(mode="json"))
            if receipt.phase == "closed":
                if (
                    not restart_closed
                    or status.branch_commit != draft.anchor
                    or draft.publication_commit is None
                    or draft.pull_request_number is None
                    or draft.pull_request_url is None
                ):
                    raise ValueError("team enrollment requires reconciliation")
                _, _, live_protection = await self._member_preflight(
                    api,
                    current,
                    request.preview.invite.sponsor_certificate.certificate_id,
                )
                if live_protection != request.github_preflight:
                    raise ValueError("team enrollment changed")
                state = await client.publication_pull_request_state(
                    publication,
                    expected_head_commit=draft.publication_commit,
                    expected_base_commit=draft.anchor,
                    pull_request_number=draft.pull_request_number,
                    transition_proof=receipt.transition_proof,
                )
                if state != "closed":
                    raise ValueError("team enrollment changed")
                return GitHubEnableResult(
                    state="bootstrap_required",
                    repository_id=status.repository_id,
                    preview_digest=preview_digest,
                    protection_digest=request.github_preflight.digest,
                    control_plane_path="/",
                )
            if status.branch_commit != draft.anchor:
                if (
                    draft.publication_commit is None
                    or draft.pull_request_number is None
                    or draft.pull_request_url is None
                ):
                    raise ValueError("team enrollment requires reconciliation")
                merged = await client.confirm_publication_merge(
                    publication,
                    expected_head_commit=draft.publication_commit,
                    expected_base_commit=draft.anchor,
                    pull_request_number=draft.pull_request_number,
                    transition_proof=receipt.transition_proof,
                )
                if merged.branch_commit is None:
                    raise ValueError("team enrollment requires reconciliation")
                merged_receipt = receipt.model_copy(
                    update={
                        "phase": "merged",
                        "publication_commit": draft.publication_commit,
                        "pull_request_number": draft.pull_request_number,
                        "pull_request_url": draft.pull_request_url,
                        "merged_commit": merged.branch_commit,
                    }
                )
                _enrollment_receipt(self.service._runtime, receipt=merged_receipt)
                return GitHubEnableResult(
                    state="published",
                    repository_id=status.repository_id,
                    preview_digest=preview_digest,
                    protection_digest=request.github_preflight.digest,
                    pull_request_url=draft.pull_request_url,
                )

            _, _, live_protection = await self._member_preflight(
                api,
                current,
                request.preview.invite.sponsor_certificate.certificate_id,
            )
            if live_protection != request.github_preflight:
                raise ValueError("team enrollment changed")
            authoritative_receipt = receipt

            async def require_live() -> None:
                assert api is not None
                live_draft = _draft(self.service._runtime)
                live_receipt = _enrollment_receipt(self.service._runtime)
                if (
                    live_draft is None
                    or live_receipt != authoritative_receipt
                    or live_draft.publication() != publication
                    or live_draft.transition_proof != authoritative_receipt.transition_proof
                    or live_receipt.approval_request_digest != request.digest()
                    or live_receipt.approved_github_preflight != request.github_preflight
                ):
                    raise ValueError("team enrollment changed")
                await self._require_join_identity(api, request)
                _require_current_enrollment_decision(
                    live_receipt,
                    request,
                    self.service._now(),
                )
                _, _, protection = await self._member_preflight(
                    api,
                    current,
                    request.preview.invite.sponsor_certificate.certificate_id,
                )
                if protection != request.github_preflight:
                    raise ValueError("team enrollment changed")

            guarded = _GuardedApi(api, require_live)
            guarded_client = GitHubTeamStateClient(
                guarded,
                expected_account_id=status.account_id,
                expected_login=status.login,
            )
            if await guarded_client.inspect(self.repository) != status:
                raise ValueError("team enrollment changed")
            if not draft.external_write_attempted:
                draft = cast(
                    EncryptedPublicationDraft,
                    _draft(
                        self.service._runtime,
                        prepared=publication,
                        anchor=draft.anchor,
                        external_write_attempted=True,
                    ),
                )
                receipt = receipt.model_copy(update={"phase": "publication-pending"})
                _enrollment_receipt(self.service._runtime, receipt=receipt)
                authoritative_receipt = receipt
            from intent_engineering.team_state.github_publication import GitHubApiPublisher

            commit = await GitHubApiPublisher(guarded, guarded_client, status).publish(
                publication,
                base_commit=draft.anchor,
                transition_proof=receipt.transition_proof,
            )
            if draft.publication_commit is not None and draft.publication_commit != commit:
                raise ValueError("team enrollment changed")
            draft = cast(
                EncryptedPublicationDraft,
                _draft(
                    self.service._runtime,
                    prepared=publication,
                    anchor=draft.anchor,
                    external_write_attempted=True,
                    publication_commit=commit,
                    pull_request_number=draft.pull_request_number,
                    pull_request_url=draft.pull_request_url,
                ),
            )
            if receipt.publication_commit is None:
                receipt = receipt.model_copy(update={"publication_commit": commit})
                _enrollment_receipt(self.service._runtime, receipt=receipt)
                authoritative_receipt = receipt
            publication_pr = await guarded_client.open_publication_pr(
                publication,
                expected_head_commit=commit,
                transition_proof=receipt.transition_proof,
            )
            if draft.pull_request_number is not None and (
                draft.pull_request_number != publication_pr.number
                or draft.pull_request_url != publication_pr.url
            ):
                raise ValueError("team enrollment changed")
            _draft(
                self.service._runtime,
                prepared=publication,
                anchor=draft.anchor,
                external_write_attempted=True,
                publication_commit=commit,
                pull_request_number=publication_pr.number,
                pull_request_url=publication_pr.url,
            )
            if receipt.phase != "pr-pending":
                receipt = receipt.model_copy(
                    update={
                        "phase": "pr-pending",
                        "publication_commit": commit,
                        "pull_request_number": publication_pr.number,
                        "pull_request_url": publication_pr.url,
                    }
                )
                _enrollment_receipt(self.service._runtime, receipt=receipt)
            state = await guarded_client.publication_pull_request_state(
                publication,
                expected_head_commit=commit,
                expected_base_commit=draft.anchor,
                pull_request_number=publication_pr.number,
                transition_proof=receipt.transition_proof,
            )
            if state == "closed":
                _enrollment_receipt(
                    self.service._runtime,
                    receipt=receipt.model_copy(update={"phase": "closed"}),
                )
                raise ValueError("team enrollment requires reconciliation")
            return GitHubEnableResult(
                state="published",
                repository_id=status.repository_id,
                preview_digest=preview_digest,
                protection_digest=request.github_preflight.digest,
                pull_request_url=publication_pr.url,
            )
        except BaseException:
            if api is not None:
                try:
                    with anyio.CancelScope(shield=True):
                        await api.aclose()
                except BaseException as cleanup:  # noqa: BLE001 - secondary signal is scrubbed
                    _setup_failure(cleanup, "team enrollment unavailable")
                    del cleanup
                api = None
            raise
        finally:
            if api is not None:
                await api.aclose()

    async def action(
        self, action: str, *, payload: HumanDecisionPayload | None = None, response: bytes = b""
    ) -> dict[str, object]:
        signal: BaseException | None = None
        try:
            async with self.guard:
                return await self._action(action, payload=payload, response=response)
        except BaseException as caught:  # noqa: BLE001 - scrub external frames, preserve cancellation
            caught.__traceback__ = None
            caught.__cause__ = None
            caught.__context__ = None
            signal = (
                caught
                if not isinstance(caught, Exception)
                else ValueError("GitHub setup unavailable")
            )
            self.publication = None
            self.transport.publisher = None
        finally:
            response = b""
            payload = None
        assert signal is not None
        raise signal.with_traceback(None) from None

    async def _action(
        self, action: str, *, payload: HumanDecisionPayload | None, response: bytes
    ) -> dict[str, object]:
        from intent_engineering.team_state.suggestions import (
            preview_code_suggestions,
            stage_code_suggestions,
        )

        service = self.service
        if load_setup_request(service._runtime) != self.request:
            raise ValueError("GitHub setup request changed")
        if action == "cancel":
            draft = _draft(service._runtime)
            bootstrap = _bootstrap_receipt(service._runtime)
            if (
                draft is not None
                and (
                    draft.external_write_attempted
                    or draft.publication_commit is not None
                    or draft.pull_request_number is not None
                    or draft.pull_request_url is not None
                )
            ) or bootstrap is not None:
                raise ValueError(
                    "GitHub provider state must be reconciled before setup can restart"
                )
            self.pending = None
            self.publication = None
            self.transport.publisher = None
            service.cancel_team_enrollment()
            if draft is not None:
                if draft.manifest.repository_id != self.request.preview.repository_id:
                    raise ValueError("GitHub publication draft changed")
                _draft(
                    service._runtime,
                    prepared=draft.publication(),
                    anchor=draft.anchor,
                    discard=True,
                )
            _complete_setup_request(service._runtime, self.request)
            service._github_setup_bridge = None
            return {"state": "cancelled"}
        api: GitHubTeamStateApi | None = None
        client: GitHubTeamStateClient | None = None
        decision: VerifiedHumanDecision | None = None

        async def require_write_authority() -> None:
            if client is None or decision is None or api is None:
                raise ValueError("GitHub write authority unavailable")
            now = service._now()
            if (
                not decision.payload.issued_at
                <= decision.verified_at
                <= now
                <= decision.payload.expires_at
                or self._authority() != self.authority_digest
            ):
                raise ValueError("GitHub write authority changed")
            reviewed = client._reviewed
            if reviewed is None or await client._inspect(self.repository) != reviewed:
                raise ValueError("GitHub repository authority changed")
            self._recipient(reviewed)
            if (
                preview_code_suggestions(
                    service._runtime.root,
                    self.request.preview.codeowners_suggestion,
                    self.request.preview.workflow_suggestion,
                    self.request.preview.check_workflow_suggestion,
                )
                != self.suggestions
            ):
                raise ValueError("GitHub code suggestions changed")
            await self._anchor(api, reviewed)
            if self.baseline is None:
                raise ValueError("GitHub default-branch baseline unavailable")
            live_baseline = await client.verify_default_branch_baseline()
            if live_baseline != self.baseline:
                raise ValueError("GitHub default-branch baseline changed")
            if self.setup_phase in {"protection", "publication"}:
                if self.tooling is None:
                    raise ValueError("GitHub default-branch tooling unavailable")
                live_tooling = await client.verify_default_branch_tooling(
                    codeowners=self.request.preview.codeowners_suggestion.encode("utf-8"),
                    workflow=self.request.preview.workflow_suggestion.encode("utf-8"),
                    check_workflow=self.request.preview.check_workflow_suggestion.encode("utf-8"),
                    runner_id=self.request.preview.ci_recipient.runner_id
                    if self.request.preview.ci_recipient is not None
                    else "",
                )
                if live_tooling != self.tooling:
                    raise ValueError("GitHub default-branch tooling changed")
            if (
                self._authority() != self.authority_digest
                or load_setup_request(service._runtime) != self.request
            ):
                raise ValueError("GitHub local authority changed")

        try:
            api = _GuardedApi(github_api(), require_write_authority)
            user = await api.request_json_object("GET", "/user")
            identity = GitHubIdentity.model_validate(
                {"account_id": str(user.payload.get("id")), "login": user.payload.get("login")}
            )
            if self.identity is not None and identity != self.identity:
                raise ValueError("GitHub identity changed")
            self.identity = identity
            client = GitHubTeamStateClient(
                api, expected_account_id=identity.account_id, expected_login=identity.login
            )
            status = await client.inspect(self.repository)
            if status.repository_id != self.request.preview.repository_id:
                raise ValueError("GitHub repository changed")
            if self.request.preview.code_owner != f"@{status.login}":
                raise ValueError(
                    "GitHub code owner unavailable; configure exactly one github:<login> alias "
                    "for the local actor"
                )
            completed = await self._finalize_merged_publication(client, status)
            if completed is not None:
                return completed
            if action == "inspect":
                draft = _draft(service._runtime)
                return {
                    "state": (
                        "identity_verified"
                        if service._team_recipient is None
                        else "publication_restart_required"
                        if self.publication_restart_required
                        else self.status()
                    ),
                    "repository_id": status.repository_id,
                    "github_account_id": status.account_id,
                    "github_login": status.login,
                    "enrollment": service.team_enrollment_status(),
                    "pull_request_url": draft.pull_request_url if draft is not None else None,
                }
            if action == "enroll":
                verifier = _OneTimeIdentity(identity)
                service._github_identity_verifier = verifier
                try:
                    options = service.team_enrollment_options(b"local-github-inspection")
                    return cast(dict[str, object], json.loads(options))
                finally:
                    verifier.identity = None
                    service._github_identity_verifier = None
            self._recipient(status)
            suggestions = preview_code_suggestions(
                service._runtime.root,
                self.request.preview.codeowners_suggestion,
                self.request.preview.workflow_suggestion,
                self.request.preview.check_workflow_suggestion,
            )
            if action == "publication-preview":
                baseline = await client.verify_default_branch_baseline()
                publication_tooling = await client.verify_default_branch_tooling(
                    codeowners=self.request.preview.codeowners_suggestion.encode("utf-8"),
                    workflow=self.request.preview.workflow_suggestion.encode("utf-8"),
                    check_workflow=self.request.preview.check_workflow_suggestion.encode("utf-8"),
                    runner_id=self.request.preview.ci_recipient.runner_id
                    if self.request.preview.ci_recipient is not None
                    else "",
                )
                await self._anchor(api, status)
                ci_recipient = self.request.preview.ci_recipient
                if ci_recipient is None:
                    raise ValueError("CI recipient unavailable")
                if not status.protection_compatible or status.branch_commit is None:
                    raise ValueError("GitHub protection required")
                self.reviewed, self.authority_digest = status, self._authority()
                self.recipient_snapshot = service._team_recipient
                self.suggestions = suggestions
                self.baseline = baseline
                self.tooling = publication_tooling
                self.setup_phase = "publication"
                self.publication = PublicationService(
                    service._runtime,
                    repository_id=status.repository_id,
                    decision_repository_id=service.repository_id,
                    authority=self._publication_authority,
                    publisher=self.transport,
                    challenge_source=service._challenge_source,
                )
                draft = _draft(service._runtime)
                if draft is None:
                    preview = self.publication.preview(now=service._now())
                    _draft(
                        service._runtime,
                        prepared=self.publication.pending_publication(),
                        anchor=status.branch_commit,
                    )
                else:
                    recipient = service._team_recipient
                    key_store = service._team_key_store
                    if (
                        draft.anchor != status.branch_commit
                        or recipient is None
                        or key_store is None
                    ):
                        raise ValueError("GitHub publication draft changed")
                    preview = self.publication.recover_preview(
                        cast(PreparedPublication, draft.publication()),
                        recipient_private_key=key_store.private_key(recipient.key_id),
                        now=service._now(),
                    )
                self.pending = preview.payload
                signing = SigningKeyStore(
                    service._runtime.config.project_id,
                    status.repository_id,
                    service._runtime.config.local_actor,
                )
                return {
                    "payload": preview.payload.model_dump(mode="json"),
                    "preview": {
                        "bundle_digest": preview.manifest.bundle_digest,
                        "parent_bundle_digest": preview.manifest.parent_bundle_digest,
                        "branch": preview.branch,
                        "recipient_key_ids": list(preview.recipient_key_ids),
                        "manifest": preview.manifest.model_dump(mode="json"),
                        "signing_public_keys": {
                            key: base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")
                            for key, value in signing.public_keys().items()
                        },
                        "ci_trust": CiTrustConfig(
                            recipient=ci_recipient,
                            signing_public_keys={
                                key: base64.b64encode(value).decode("ascii")
                                for key, value in signing.public_keys().items()
                            },
                        ).model_dump(mode="json"),
                    },
                }
            if action == "protection-preview":
                try:
                    baseline = await client.verify_default_branch_baseline()
                except GitHubTeamStateError:
                    return {
                        "state": "default_branch_prerequisite",
                        "repository_id": status.repository_id,
                        "guidance": (
                            "Protect the default branch with enforced administrators, stale-review "
                            "dismissal, at least one human approval, no bypass allowances, and no "
                            "force pushes or deletions before staging setup files."
                        ),
                    }
                installed = True
                for existing, suggested in (
                    (suggestions.codeowners_preimage, suggestions.codeowners_content),
                    (suggestions.workflow_preimage, suggestions.workflow_content),
                    (suggestions.check_workflow_preimage, suggestions.check_workflow_content),
                ):
                    current = (
                        None
                        if existing is None
                        else base64.urlsafe_b64decode(existing + "=" * (-len(existing) % 4))
                    )
                    if current is not None and current != suggested.encode("utf-8"):
                        raise ValueError("GitHub code suggestions conflict")
                    installed = installed and current == suggested.encode("utf-8")
                tooling: GitHubDefaultBranchTooling | None = None
                if installed:
                    try:
                        tooling = await client.verify_default_branch_tooling(
                            codeowners=self.request.preview.codeowners_suggestion.encode("utf-8"),
                            workflow=self.request.preview.workflow_suggestion.encode("utf-8"),
                            check_workflow=self.request.preview.check_workflow_suggestion.encode(
                                "utf-8"
                            ),
                            runner_id=self.request.preview.ci_recipient.runner_id
                            if self.request.preview.ci_recipient is not None
                            else "",
                        )
                    except GitHubTeamStateError:
                        return {
                            "state": "code_changes_staged",
                            "repository_id": status.repository_id,
                            "guidance": (
                                "Commit and merge the exact staged CODEOWNERS and both protected workflows "
                                "to the protected default branch, restrict the intent-state runner "
                                "group to exactly those workflows, then preview protection again."
                            ),
                        }
                authority = self._authority()
                bootstrap = _bootstrap_receipt(service._runtime)
                if bootstrap is not None:
                    if (
                        bootstrap.repository_id != status.repository_id
                        or bootstrap.tooling != tooling
                        or bootstrap.authority_digest != authority
                        or (status.branch_present and status.branch_commit != bootstrap.anchor)
                    ):
                        raise ValueError("GitHub bootstrap receipt changed")
                    if status.branch_present:
                        await self._anchor(api, status)
                protection = client.protection_preview() if tooling is not None else None
                self.recipient_snapshot = service._team_recipient
                subject = _digest(
                    {
                        "phase": "protection" if tooling is not None else "code_changes",
                        "protection": (
                            None if protection is None else protection.model_dump(mode="json")
                        ),
                        "status": status.model_dump(mode="json"),
                        "suggestions": suggestions.model_dump(mode="json"),
                        "tooling": None if tooling is None else tooling.model_dump(mode="json"),
                        "authority": authority,
                        "baseline": baseline.model_dump(mode="json"),
                        "recipient": service._team_recipient.model_dump(mode="json")
                        if service._team_recipient
                        else None,
                    }
                )
                now = service._now()
                self.pending = HumanDecisionPayload(
                    project_id=service._runtime.config.project_id,
                    repository_id=service.repository_id,
                    actor=service._runtime.config.local_actor,
                    action=DecisionAction.APPROVE_EXTERNAL_WRITE,
                    graph_version=service._runtime.graph_store.load().version,
                    parent_bundle_digest="sha256:" + "0" * 64,
                    subject=DecisionSubject(
                        kind="github_setup", id="github_setup:" + subject.removeprefix("sha256:")
                    ),
                    subject_digest=subject,
                    result_digest=subject,
                    challenge=service._nonce(),
                    issued_at=now,
                    expires_at=now + timedelta(minutes=5),
                )
                self.reviewed, self.suggestions, self.authority_digest = (
                    status,
                    suggestions,
                    authority,
                )
                self.protection_digest = None if protection is None else protection.digest
                self.tooling = tooling
                self.baseline = baseline
                self.setup_phase = "protection" if tooling is not None else "code_changes"
                return {
                    "payload": self.pending.model_dump(mode="json"),
                    "preview": {
                        "phase": self.setup_phase,
                        "protection": (
                            None if protection is None else protection.model_dump(mode="json")
                        ),
                        "suggestions": suggestions.model_dump(mode="json"),
                        "tooling": None if tooling is None else tooling.model_dump(mode="json"),
                        "github_account_id": status.account_id,
                        "github_login": status.login,
                    },
                }
            if (
                action not in {"options", "verify"}
                or payload is None
                or self.pending is None
                or payload != self.pending
            ):
                raise ValueError("GitHub setup decision changed")
            if status != self.reviewed or self._authority() != self.authority_digest:
                raise ValueError("GitHub setup authority changed")
            if suggestions != self.suggestions:
                raise ValueError("GitHub code suggestions changed")
            if action == "options":
                return cast(
                    dict[str, object],
                    json.loads(
                        service._webauthn.authentication_options(
                            payload, service._origin, service._now()
                        )
                    ),
                )
            decision = service._webauthn.verify(response, payload, service._origin, service._now())
            recipient = service._team_recipient
            if (
                recipient is None
                or decision.credential.local_only
                or decision.credential.credential_id != recipient.webauthn_credential_id
                or decision.credential.public_key != recipient.webauthn_credential_public_key
                or decision.credential.github_account_id != status.account_id
                or decision.credential.github_login != status.login
            ):
                raise ValueError("GitHub decision credential changed")
            self.pending = None
            if self._authority() != self.authority_digest:
                raise ValueError("GitHub setup authority changed")
            if payload.action is DecisionAction.PUBLISH_STATE:
                from intent_engineering.team_state.github_publication import GitHubApiPublisher

                publication = self.publication
                if publication is None:
                    raise ValueError("GitHub publication unavailable")
                pending_draft = _draft(service._runtime)
                if pending_draft is None or pending_draft.anchor != status.branch_commit:
                    raise ValueError("GitHub publication draft changed")
                if self.publication_restart_required:
                    if (
                        pending_draft.publication_commit is None
                        or pending_draft.pull_request_number is None
                        or await client.publication_pull_request_state(
                            pending_draft.publication(),
                            expected_head_commit=pending_draft.publication_commit,
                            expected_base_commit=pending_draft.anchor,
                            pull_request_number=pending_draft.pull_request_number,
                        )
                        != "closed"
                    ):
                        raise ValueError("GitHub publication restart changed")
                    _draft(
                        service._runtime,
                        prepared=pending_draft.publication(),
                        anchor=pending_draft.anchor,
                        external_write_attempted=True,
                        publication_commit=pending_draft.publication_commit,
                        restart_closed=True,
                    )
                    pending_draft = cast(EncryptedPublicationDraft, _draft(service._runtime))
                _draft(
                    service._runtime,
                    prepared=pending_draft.publication(),
                    anchor=pending_draft.anchor,
                    external_write_attempted=True,
                    publication_commit=pending_draft.publication_commit,
                    pull_request_number=pending_draft.pull_request_number,
                    pull_request_url=pending_draft.pull_request_url,
                )
                self.transport.publisher = GitHubApiPublisher(api, client, status)
                try:
                    prepared = await anyio.to_thread.run_sync(
                        lambda: publication.prepare(decision, now=service._now())
                    )
                finally:
                    self.transport.publisher = None
                self.publication = None
                if self.transport.published_commit is None:
                    raise ValueError("GitHub publication commit unavailable")
                _draft(
                    service._runtime,
                    prepared=prepared,
                    anchor=status.branch_commit,
                    external_write_attempted=True,
                    publication_commit=self.transport.published_commit,
                )
                pr = await client.open_publication_pr(
                    prepared, expected_head_commit=self.transport.published_commit
                )
                _draft(
                    service._runtime,
                    prepared=prepared,
                    anchor=status.branch_commit,
                    external_write_attempted=True,
                    publication_commit=self.transport.published_commit,
                    pull_request_number=pr.number,
                    pull_request_url=pr.url,
                )
                return {
                    "state": "publication_pending",
                    "pull_request_url": pr.url,
                    "repository_id": pr.repository_id,
                }
            if self.setup_phase == "code_changes":
                await require_write_authority()
                stage_code_suggestions(service._runtime.root, suggestions)
                self.suggestions = preview_code_suggestions(
                    service._runtime.root,
                    self.request.preview.codeowners_suggestion,
                    self.request.preview.workflow_suggestion,
                    self.request.preview.check_workflow_suggestion,
                )
                return {
                    "state": "code_changes_staged",
                    "repository_id": status.repository_id,
                    "guidance": (
                        "Commit and merge the staged CODEOWNERS and both protected workflows to the "
                        "protected default branch, then preview branch protection again."
                    ),
                }
            if self.setup_phase != "protection" or self.protection_digest is None:
                raise ValueError("GitHub setup phase changed")
            tooling = self.tooling
            if tooling is None or self.authority_digest is None:
                raise ValueError("GitHub bootstrap authority unavailable")
            bootstrap = _bootstrap_receipt(service._runtime)

            def record_bootstrap(anchor: str, tree: str) -> None:
                _bootstrap_receipt(
                    service._runtime,
                    receipt=GitHubBootstrapReceipt(
                        repository_id=status.repository_id,
                        anchor=anchor,
                        tree=tree,
                        tooling=tooling,
                        authority_digest=cast(str, self.authority_digest),
                    ),
                )

            updated = await client.configure_protection(
                self.protection_digest,
                record_bootstrap=record_bootstrap,
                expected_bootstrap_anchor=(None if bootstrap is None else bootstrap.anchor),
            )
            await self._anchor(api, updated)
            await require_write_authority()
            completed_bootstrap = _bootstrap_receipt(service._runtime)
            if completed_bootstrap is not None:
                _bootstrap_receipt(
                    service._runtime,
                    receipt=completed_bootstrap,
                    discard=True,
                )
            SigningKeyStore(
                service._runtime.config.project_id,
                status.repository_id,
                service._runtime.config.local_actor,
            ).signing_keys()
            return {
                "state": "protection_configured",
                "repository_id": updated.repository_id,
                "anchor": updated.branch_commit,
            }
        finally:
            response = b""
            if api is not None:
                failure = sys.exc_info()[1]
                with anyio.CancelScope(shield=True):
                    try:
                        await api.aclose()
                    except BaseException:
                        if failure is None:
                            raise
