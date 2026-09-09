"""Explicit three-way review of concurrent state; authority is never auto-merged."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Literal

from intent_engineering.control_plane.models import (
    DecisionAction,
    DecisionSubject,
    HumanDecisionPayload,
    TeamStateDivergenceCaseV2,
)
from intent_engineering.control_plane.webauthn_service import VerifiedHumanDecision
from intent_engineering.team_state.models import CanonicalStateFile, CanonicalStateSnapshot
from intent_engineering.team_state.publication import (
    PreparedPublicationV2,
    PublicationAuthorityV2,
    V2DeviceSigner,
    prepare_v2_publication,
)
from intent_engineering.team_state.restore import VerifiedReleaseV2, _validate_v2_snapshot

_AUTHORITY_PATH = "authority/team-authority.json"
Choice = Literal["local", "remote"]


def _digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def snapshot_digest(snapshot: CanonicalStateSnapshot) -> str:
    return _digest(
        json.dumps(
            snapshot.inventory().model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()
    )


@dataclass(frozen=True)
class ReconciliationPreview:
    case: TeamStateDivergenceCaseV2
    conflicts: tuple[str, ...]
    common: VerifiedReleaseV2 = field(repr=False)
    remote: VerifiedReleaseV2 = field(repr=False)
    local: CanonicalStateSnapshot = field(repr=False)
    resolved: CanonicalStateSnapshot | None = field(repr=False)
    choices: tuple[tuple[str, Choice], ...] = ()


class ReconciliationService:
    """Prepare a normal publication only after exact current-base human review."""

    def __init__(
        self,
        *,
        authority_provider: Callable[[VerifiedReleaseV2], PublicationAuthorityV2],
        device_signer: V2DeviceSigner,
        clock: Callable[[], datetime],
    ) -> None:
        self._authority_provider = authority_provider
        self._device_signer = device_signer
        self._clock = clock

    def actor(self, remote: VerifiedReleaseV2) -> str:
        authority = self._authority_provider(remote)
        return next(
            m.actor for m in authority.registry.members if m.member_id == authority.local_member_id
        )

    def preview(
        self, *, common: VerifiedReleaseV2, remote: VerifiedReleaseV2, local: CanonicalStateSnapshot
    ) -> ReconciliationPreview:
        if (
            type(common) is not VerifiedReleaseV2
            or type(remote) is not VerifiedReleaseV2
            or common.snapshot is None
            or remote.snapshot is None
            or type(local) is not CanonicalStateSnapshot
            or remote.manifest.parent_bundle_digest != common.manifest.bundle_digest
            or remote.manifest.repository_id != common.manifest.repository_id
            or remote.manifest.project_id != common.manifest.project_id
            or local.repository_id != common.manifest.repository_id
            or local.project_id != common.manifest.project_id
        ):
            raise ValueError("reconciliation base changed")
        common_files = {f.path: f.content for f in common.snapshot.files}
        remote_files = {f.path: f.content for f in remote.snapshot.files}
        local_files = {f.path: f.content for f in local.files}
        changed, conflicts, merged = [], [], []
        for path, before in common_files.items():
            left, right = local_files[path], remote_files[path]
            if left != before or right != before:
                changed.append(path)
            if left != right and left != before and right != before:
                conflicts.append(path)
            merged.append(CanonicalStateFile(path=path, content=right if left == before else left))
        if remote.authority != common.authority:
            changed.append(_AUTHORITY_PATH)
            conflicts.append(_AUTHORITY_PATH)
        case = TeamStateDivergenceCaseV2(
            repository_id=local.repository_id,
            project_id=local.project_id,
            common_parent_bundle_digest=common.manifest.bundle_digest,
            remote_manifest_digest=_digest(remote.manifest_bytes),
            local_manifest_digest=snapshot_digest(local),
            remote_authority_digest=remote.manifest.authority_digest,
            local_authority_digest=common.manifest.authority_digest,
            remote_commit=remote.commit,
            local_publication_commit=None,
            changed_paths=tuple(sorted(changed)),
        )
        resolved = (
            None
            if conflicts
            else CanonicalStateSnapshot(
                project_id=local.project_id,
                repository_id=local.repository_id,
                graph_version=max(local.graph_version, remote.snapshot.graph_version),
                files=tuple(merged),
            )
        )
        return ReconciliationPreview(
            case, tuple(sorted(conflicts)), common, remote, local, resolved
        )

    def resolve(
        self, *, preview: ReconciliationPreview, choices: Mapping[str, Choice]
    ) -> ReconciliationPreview:
        original = self.preview(common=preview.common, remote=preview.remote, local=preview.local)
        if set(choices) != set(original.conflicts) or any(
            v not in {"local", "remote"} for v in choices.values()
        ):
            raise ValueError("reconciliation choices incomplete")
        if _AUTHORITY_PATH in choices and choices[_AUTHORITY_PATH] != "remote":
            raise ValueError("reconciliation authority requires separate enrollment review")
        base = {f.path: f.content for f in original.common.snapshot.files}  # type: ignore[union-attr]
        remote = {f.path: f.content for f in original.remote.snapshot.files}  # type: ignore[union-attr]
        local = {f.path: f.content for f in original.local.files}
        files = tuple(
            CanonicalStateFile(
                path=p,
                content=(
                    remote[p]
                    if choices.get(p) == "remote" or (p not in choices and local[p] == base[p])
                    else local[p]
                ),
            )
            for p in base
        )
        resolved = CanonicalStateSnapshot(
            project_id=original.local.project_id,
            repository_id=original.local.repository_id,
            graph_version=max(original.local.graph_version, original.remote.manifest.graph_version),
            files=files,
        )
        return replace(original, resolved=resolved, choices=tuple(sorted(choices.items())))

    def _check(self, preview: ReconciliationPreview) -> None:
        rebuilt = self.preview(common=preview.common, remote=preview.remote, local=preview.local)
        if rebuilt.conflicts:
            rebuilt = self.resolve(preview=rebuilt, choices=dict(preview.choices))
        if rebuilt != preview or preview.resolved is None:
            raise ValueError("reconciliation preview changed")
        _validate_v2_snapshot(
            preview.resolved,
            preview.remote.manifest.model_copy(
                update={"graph_version": preview.resolved.graph_version}
            ),
        )

    def decision_payload(
        self,
        *,
        preview: ReconciliationPreview,
        repository_id: str,
        actor: str,
        challenge: str,
        now: datetime,
    ) -> HumanDecisionPayload:
        self._check(preview)
        assert preview.resolved is not None
        subject = _digest(preview.case.canonical_bytes())
        result = _digest(
            json.dumps(
                {
                    "snapshot": snapshot_digest(preview.resolved),
                    "choices": preview.choices,
                    "remote_commit": preview.remote.commit,
                    "remote_manifest": preview.case.remote_manifest_digest,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        )
        return HumanDecisionPayload(
            project_id=preview.local.project_id,
            repository_id=repository_id,
            actor=actor,
            action=DecisionAction.RECONCILE_STATE,
            graph_version=preview.local.graph_version,
            parent_bundle_digest=preview.remote.manifest.bundle_digest,
            subject=DecisionSubject(kind="team_divergence", id="team_divergence:" + subject[7:]),
            subject_digest=subject,
            result_digest=result,
            challenge=challenge,
            issued_at=now,
            expires_at=now + timedelta(minutes=5),
        )

    def prepare(
        self,
        *,
        preview: ReconciliationPreview,
        decision: VerifiedHumanDecision,
        current_remote: VerifiedReleaseV2,
    ) -> PreparedPublicationV2:
        self._check(preview)
        if type(decision) is not VerifiedHumanDecision or current_remote != preview.remote:
            raise ValueError("reconciliation remote changed")
        payload, credential, now = decision.payload, decision.credential, self._clock()
        expected = self.decision_payload(
            preview=preview,
            repository_id=payload.repository_id,
            actor=payload.actor,
            challenge=payload.challenge,
            now=payload.issued_at,
        )
        authority = self._authority_provider(current_remote)
        if (
            authority.remote_state != current_remote
            or authority.registry != current_remote.authority
            or authority.publication_base_commit != current_remote.commit
        ):
            raise ValueError("reconciliation publication authority changed")
        member = next(
            (m for m in authority.registry.members if m.member_id == authority.local_member_id),
            None,
        )
        if (
            payload != expected
            or not payload.issued_at <= decision.verified_at <= now <= payload.expires_at
            or credential.project_id != payload.project_id
            or credential.repository_id != payload.repository_id
            or credential.actor != payload.actor
            or member is None
            or member.actor != payload.actor
        ):
            raise ValueError("reconciliation decision changed")
        assert preview.resolved is not None
        return prepare_v2_publication(
            snapshot=preview.resolved,
            authority=authority,
            device_signer=self._device_signer,
            now=now,
        )
