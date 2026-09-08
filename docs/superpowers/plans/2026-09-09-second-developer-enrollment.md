# Second-Developer Enrollment and Publication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a no-private-key-transport enrollment ceremony that moves Task 6 shared state to a stable-root version 2 authority, automatically restores developer B, accepts B's publications on A and unchanged CI, and turns concurrent publication into reviewed reconciliation.

**Architecture:** Keep the protected `intent-state` branch at exactly three artifacts. Put a canonical member/device/CI authority registry inside archive v2, pin a stable team root locally, certify device encryption/signing keys, and authorize ordinary releases from the parent registry. Enrollment is a signed public invite, a B-local WebAuthn and key-possession response, and an A-local WebAuthn/root-approved authority transition published through the existing Task 6 receipt and PR machinery. A dual-verifiable v1-to-v2 release provides the only upgrade path.

**Tech Stack:** Python 3.12, strict Pydantic models, `cryptography` Ed25519/X25519/HKDF/AES-GCM, OS keyring abstraction, existing WebAuthn verifier, Git/GitHub API adapter, Typer CLI, control-plane HTTP/browser UI, pytest/pytest-asyncio, Ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-09-09-second-developer-enrollment-design.md`

## Global Constraints

- Follow strict TDD: add one focused failing assertion, run it and inspect the intended failure, implement the smallest behavior, rerun focused tests, then refactor.
- Do not weaken Task 6 default/state protection, required-workflow, runner-group, exact-byte tooling, PR SHA binding, cancellation, or receipt invariants.
- Never serialize, export, log, pass on a command line, or transport a root, recipient, device-signing, content, or WebAuthn private key.
- Preserve the three-artifact state branch contract; `authority/team-authority.json` exists only inside archive v2.
- Keep the existing CI recipient key and workflow. Upgrade validator code through a CODEOWNERS-reviewed default-branch change before opening the first v2 state PR.
- Treat all persisted/network JSON as hostile: bounded read, duplicate-key rejection, strict schema, canonical bytes, repository/project binding, and fixed errors.
- A state-changing operation is previewed and WebAuthn-bound once, then every mutable preimage is rechecked immediately before each external write.
- Persist `external_write_attempted` before the first ambiguous GitHub call. A pending/ambiguous receipt cannot be cancelled.
- Use `rg` for discovery and `apply_patch` for hand edits. Avoid broad test gates until focused tests are green.

---

### Task 1: Define canonical version 2 authority and envelope models

**Files:**

- Modify: `src/intent_engineering/team_state/models.py`
- Modify: `src/intent_engineering/team_state/restore.py`
- Create: `src/intent_engineering/team_state/authority.py`
- Modify: `tests/unit/team_state/test_models.py`
- Create: `tests/unit/team_state/test_authority.py`
- Modify: `tests/unit/team_state/test_restore.py`

**Interfaces:**

```python
class TeamRootTrustV2(StrictModel): ...


class MemberRecordV2(StrictModel): ...


class DeviceCertificateClaimsV2(StrictModel): ...


class DeviceSignerCertificateV2(StrictModel): ...


class DeviceRevocationV2(StrictModel): ...


class TeamAuthorityPolicyV2(StrictModel): ...


class TeamAuthorityRegistryV2(StrictModel): ...


class TeamStateManifestV2(StrictModel): ...


class AuthorityAttestationV2(StrictModel): ...


class StateSignatureEnvelopeV2(StrictModel): ...


def canonical_authority_bytes(authority: TeamAuthorityRegistryV2) -> bytes: ...
def authority_digest(authority: TeamAuthorityRegistryV2) -> str: ...
def parse_team_manifest(content: bytes) -> TeamStateManifest | TeamStateManifestV2: ...
def parse_signature_envelope(
    content: bytes,
) -> StateSignatureEnvelope | StateSignatureEnvelopeV2: ...
```

- [ ] **Step 1: Write RED canonical-model tests**

Add exact fixtures for one sponsor device and the unchanged CI record. Assert canonical round-trip and derived root/member/certificate/authority IDs. Add table cases for duplicate JSON keys, Python list in strict mode, unsorted/duplicate members and certificates, mismatched derived IDs, wrong repository/project, invalid key sizes/algorithms, non-UTC timestamps, expiry over 366 days, inactive member with active device, more than 32 members/63 devices/64 recipients/256 revocations, and registry bytes over 256 KiB.

- [ ] **Step 2: Run the focused RED tests**

Run: `pytest -q tests/unit/team_state/test_models.py tests/unit/team_state/test_authority.py tests/unit/team_state/test_restore.py -x`

Expected: collection/import fails because the v2 types and parsers do not exist.

- [ ] **Step 3: Implement strict models and canonical parsers**

Keep v1 types unchanged. Add a discriminated parser based only on an exact integer `schema_version`, with canonical-byte equality after strict JSON validation. Derive IDs using separate domain strings. Validate the registry as a graph: members reference exact certificates, certificate GitHub/member fields agree, revoked certificates are not active, serials are unique, one CI record exists, active recipients equal the bounded derived set, and root/epoch fields agree everywhere.

- [ ] **Step 4: Run focused GREEN tests**

Run the Step 2 command. Expected: all selected model tests pass.

- [ ] **Step 5: Commit Task 1**

```bash
git add src/intent_engineering/team_state/models.py \
  src/intent_engineering/team_state/restore.py \
  src/intent_engineering/team_state/authority.py \
  tests/unit/team_state/test_models.py \
  tests/unit/team_state/test_authority.py \
  tests/unit/team_state/test_restore.py
git commit -m "feat: define version two team authority"
```

### Task 2: Add deterministic archive v2 with the encrypted authority registry

**Files:**

- Modify: `src/intent_engineering/team_state/archive.py`
- Modify: `src/intent_engineering/team_state/models.py`
- Modify: `tests/unit/team_state/test_archive.py`
- Modify: `tests/unit/team_state/test_models.py`

**Interfaces:**

```python
ARCHIVE_V2_MAGIC: Final = b"IEAR\x00\x02\x00\x00"
AUTHORITY_PATH: Final = "authority/team-authority.json"


def build_archive_v2(
    snapshot: CanonicalStateSnapshot,
    authority: TeamAuthorityRegistryV2,
) -> bytes: ...


def validate_archive_v2(content: bytes) -> RestoredSnapshotV2: ...
```

- [ ] **Step 1: Write RED archive tests**

Assert one exact golden byte sequence, v1 golden bytes remain unchanged, v2 contains the existing ten files plus exactly `authority/team-authority.json`, and decoded authority bytes hash to the manifest fixture. Add negatives for missing/renamed/duplicate/extra authority paths, v1 magic with v2 inventory, v2 magic without authority, non-canonical authority JSON, oversized authority, oversized bundle/state/file, bad mode/mtime/kind/hash, truncated framing, trailing bytes, and path traversal.

- [ ] **Step 2: Run RED**

Run: `pytest -q tests/unit/team_state/test_archive.py tests/unit/team_state/test_models.py -x`

Expected: v2 archive imports/assertions fail while existing v1 golden tests still pass.

- [ ] **Step 3: Implement version-dispatched archive functions**

Do not add compression or generic extension entries. Reuse the deterministic framing with new magic, exact inventory, and existing 24 MiB/16 MiB/8 MiB limits. Parse and validate the authority entry before returning a v2 restored snapshot.

- [ ] **Step 4: Run GREEN and format**

Run: `pytest -q tests/unit/team_state/test_archive.py tests/unit/team_state/test_models.py && ruff check src/intent_engineering/team_state/archive.py src/intent_engineering/team_state/models.py tests/unit/team_state/test_archive.py tests/unit/team_state/test_models.py`

- [ ] **Step 5: Commit Task 2**

```bash
git add src/intent_engineering/team_state/archive.py \
  src/intent_engineering/team_state/models.py \
  tests/unit/team_state/test_archive.py \
  tests/unit/team_state/test_models.py
git commit -m "feat: archive canonical team authority"
```

### Task 3: Implement root keys, device certificates, and version 2 signatures

**Files:**

- Modify: `src/intent_engineering/team_state/signing.py`
- Modify: `src/intent_engineering/team_state/keys.py`
- Modify: `src/intent_engineering/team_state/crypto.py`
- Modify: `src/intent_engineering/team_state/authority.py`
- Modify: `tests/unit/team_state/test_signing.py`
- Modify: `tests/unit/team_state/test_keys.py`
- Modify: `tests/unit/team_state/test_crypto.py`
- Modify: `tests/unit/team_state/test_authority.py`

**Interfaces:**

```python
@dataclass(frozen=True, slots=True)
class DevicePublicMaterial:
    device_id: str
    recipient_key_id: str
    recipient_public_key: bytes
    signature_id: str
    signing_public_key: bytes


class TeamRootKeyStore(Protocol):
    def create(self, binding: RootEnrollmentBinding) -> TeamRootTrustV2: ...
    def sign(self, root_key_id: str, preimage: bytes) -> bytes: ...


class DeviceKeyStore(Protocol):
    def create(self, binding: DeviceEnrollmentBinding) -> DevicePublicMaterial: ...
    def sign(self, signature_id: str, preimage: bytes) -> bytes: ...
    def prove_recipient_possession(
        self, recipient_key_id: str, challenge_public_key: bytes, subject: bytes
    ) -> bytes: ...


def issue_device_certificate(
    claims: DeviceCertificateClaimsV2,
    root_store: TeamRootKeyStore,
) -> DeviceSignerCertificateV2: ...


def verify_v2_envelope(
    manifest: TeamStateManifestV2,
    envelope: StateSignatureEnvelopeV2,
    root: TeamRootTrustV2,
    parent_authority: TeamAuthorityRegistryV2 | None,
    now: datetime,
) -> VerifiedEnvelopeV2: ...
```

- [ ] **Step 1: Write RED cryptographic tests**

Test deterministic IDs, keyring service/account separation, root certificate verification, state and authority domain separation, certificate expiry/skew, revocation, unknown device, wrong parent authority, insufficient/duplicate signatures, and an ordinary member attempting an authority mutation. Assert exported objects and captured logger/exception text contain none of the seeded private bytes or token values.

- [ ] **Step 2: Run RED**

Run: `pytest -q tests/unit/team_state/test_signing.py tests/unit/team_state/test_keys.py tests/unit/team_state/test_crypto.py tests/unit/team_state/test_authority.py -x`

Expected: new store/proof/certificate APIs are absent.

- [ ] **Step 3: Implement key stores and verification**

Generate keys in the backend without a serialization API. Root-sign canonical certificate and authority preimages. Build state-signature preimages from manifest/authority/parent identities. Require one active certified device for ordinary releases; require an active sponsor, exact WebAuthn decision digest, and root attestation for authority changes. Preserve the current encryption algorithm and recipient cap.

- [ ] **Step 4: Run GREEN**

Run the Step 2 command. Then run Ruff on only the four source and four test files.

- [ ] **Step 5: Commit Task 3**

```bash
git add src/intent_engineering/team_state/{signing.py,keys.py,crypto.py,authority.py} \
  tests/unit/team_state/{test_signing.py,test_keys.py,test_crypto.py,test_authority.py}
git commit -m "feat: certify team device signers"
```

### Task 4: Add public invite, join response, and sponsor approval services

**Files:**

- Create: `src/intent_engineering/team_state/enrollment.py`
- Modify: `src/intent_engineering/team_state/authority.py`
- Modify: `src/intent_engineering/team_state/keys.py`
- Create: `tests/unit/team_state/test_enrollment.py`
- Modify: `tests/unit/team_state/test_authority.py`
- Modify: `tests/unit/team_state/test_keys.py`

**Interfaces:**

```python
class TeamEnrollmentService:
    def create_invite(
        self,
        *,
        state: VerifiedRemoteStateV2,
        intended_identity: GitHubIdentity,
        now: datetime,
    ) -> TeamInviteV2: ...

    def create_join_response(
        self,
        *,
        invite: TeamInviteV2,
        local_identity: GitHubIdentity,
        decision: VerifiedHumanDecision,
        now: datetime,
    ) -> JoinResponseV2: ...

    def preview_approval(
        self,
        *,
        invite: TeamInviteV2,
        response: JoinResponseV2,
        current: VerifiedRemoteStateV2,
        now: datetime,
    ) -> JoinApprovalPreviewV2: ...

    def approve(
        self,
        *,
        preview: JoinApprovalPreviewV2,
        sponsor_decision: VerifiedHumanDecision,
        current: VerifiedRemoteStateV2,
        now: datetime,
    ) -> ApprovedAuthorityTransitionV2: ...
```

- [ ] **Step 1: Write RED enrollment tests**

Use independent A/B keyrings and WebAuthn fakes. Test the happy public exchange and assert A never sees B's private keys. Add negatives for wrong/renamed numeric identity, repository/project/root/base mismatch, expired/future invite, replayed invite, tampered public keys, missing Ed25519/X25519 possession, wrong WebAuthn subject/origin/RP/counter, sponsor lacking role, stale state/authority/tooling, duplicate account/device, cap overflow, and response/export byte limits.

- [ ] **Step 2: Run RED**

Run: `pytest -q tests/unit/team_state/test_enrollment.py tests/unit/team_state/test_authority.py tests/unit/team_state/test_keys.py -x`

Expected: enrollment service and public models do not exist.

- [ ] **Step 3: Implement the pure ceremony first**

Make public import/export canonical and bounded. The invite is sponsor-device-signed and binds an ephemeral X25519 challenge. B's response binds the full invite digest, both public keys, verified identity, and B's WebAuthn decision. Approval recomputes every digest and creates exact certificate/registry/attestation bytes only after a sponsor decision over the before/after preview.

- [ ] **Step 4: Run GREEN**

Run the Step 2 command. Confirm fixed errors do not include hostile response values.

- [ ] **Step 5: Commit Task 4**

```bash
git add src/intent_engineering/team_state/enrollment.py \
  src/intent_engineering/team_state/authority.py \
  src/intent_engineering/team_state/keys.py \
  tests/unit/team_state/test_enrollment.py \
  tests/unit/team_state/test_authority.py \
  tests/unit/team_state/test_keys.py
git commit -m "feat: add sponsor-approved team enrollment"
```

### Task 5: Persist pending join/root trust and implement the one-way v1 migration

**Files:**

- Modify: `src/intent_engineering/team_state/local_trust.py`
- Modify: `src/intent_engineering/team_state/restore.py`
- Modify: `src/intent_engineering/team_state/publication.py`
- Modify: `src/intent_engineering/team_state/signing.py`
- Modify: `tests/unit/team_state/test_local_trust.py`
- Modify: `tests/unit/team_state/test_restore.py`
- Modify: `tests/integration/team_state/test_publication.py`
- Modify: `tests/integration/team_state/test_restore.py`

**Interfaces:**

```python
class LocalTrustConfigV2(StrictModel): ...


class PendingJoinTrustV2(StrictModel): ...


class LocalTrustProvider:
    def load_versioned(self) -> LocalTrustConfig | LocalTrustConfigV2 | None: ...
    def load_pending_join(self) -> PendingJoinTrustV2 | None: ...
    def save_pending_join(self, receipt: PendingJoinTrustV2) -> None: ...
    def activate_join(
        self,
        *,
        pending_preimage: PendingJoinTrustV2,
        trust: LocalTrustConfigV2,
        install: StateInstallTransaction,
    ) -> None: ...


def prepare_v1_migration(
    *,
    current: VerifiedV1Release,
    legacy_trust: LocalTrustConfig,
    ci_recipient: CiRecipientRecord,
    sponsor_decision: VerifiedHumanDecision,
) -> PreparedPublication: ...
```

- [ ] **Step 1: Write RED local-state and migration tests**

Prove 0700/0600 modes, no-follow/same-path locks, canonical bytes, owner mismatch rejection, crash-safe atomic activation, and that pending receipts contain only public fields. Add v1-only restore success, exact dual-signed v1→v2 success, missing/extra/wrong legacy signature rejection, wrong root/CI/parent rejection, closed-unmerged migration leaving v1 trust unchanged, idempotent restart, schema-1 rollback rejection after v2, and an old binary-style parser returning upgrade-required rather than unavailable.

- [ ] **Step 2: Run RED**

Run: `pytest -q tests/unit/team_state/test_local_trust.py tests/unit/team_state/test_restore.py tests/integration/team_state/test_publication.py tests/integration/team_state/test_restore.py -x`

Expected: versioned trust and migration APIs are absent.

- [ ] **Step 3: Implement transactional trust migration**

Promote no existing private key implicitly: create a distinct root and device signer in the keyring, reuse A's recipient only through an exact certificate binding, and dual-sign the first v2 envelope. Validate the legacy bridge before root trust. Store the v2 trust only when the merged v2 release installs. Retain legacy keys for explicit later cleanup; never delete them on a failed or pending migration.

- [ ] **Step 4: Run GREEN**

Run the Step 2 command. Add a post-test filesystem/keyring assertion that the failed paths did not advance trust or remove old keys.

- [ ] **Step 5: Commit Task 5**

```bash
git add src/intent_engineering/team_state/{local_trust.py,restore.py,publication.py,signing.py} \
  tests/unit/team_state/{test_local_trust.py,test_restore.py} \
  tests/integration/team_state/{test_publication.py,test_restore.py}
git commit -m "feat: migrate shared state to stable root trust"
```

### Task 6: Derive v2 publication authority and teach unchanged CI to validate it

**Files:**

- Modify: `src/intent_engineering/team_state/publication.py`
- Modify: `src/intent_engineering/team_state/candidate.py`
- Modify: `src/intent_engineering/team_state/restore.py`
- Modify: `src/intent_engineering/team_state/crypto.py`
- Modify: `tests/integration/team_state/test_publication.py`
- Modify: `tests/integration/team_state/test_candidate.py`
- Modify: `tests/integration/team_state/test_restore.py`

**Interfaces:**

```python
@dataclass(frozen=True, slots=True)
class PublicationAuthorityV2:
    registry: TeamAuthorityRegistryV2
    local_member_id: str
    local_device_certificate_id: str
    remote_state: RemoteStateSnapshot
    publication_base_commit: str


def authority_from_verified_state(
    restored: VerifiedReleaseV2,
    local_trust: LocalTrustConfigV2,
) -> PublicationAuthorityV2: ...
```

- [ ] **Step 1: Write RED A/B/CI tests**

Create A, B, and CI principals. Assert B's ordinary publication encrypts to exact active A+B+CI recipients; A and CI accept it; authority bytes/digest/sequence are unchanged; no root private key is loaded; and the existing workflow/CI recipient descriptor is byte-identical. Add failures for caller-supplied omitted/extra recipients, stale/revoked/expired B certificate, B authority mutation, recipient cap, wrong parent, changed CI record, and a valid signature with a different archive authority file.

- [ ] **Step 2: Run RED**

Run: `pytest -q tests/integration/team_state/test_publication.py tests/integration/team_state/test_candidate.py tests/integration/team_state/test_restore.py -x`

Expected: publication still requires direct signer maps/exact v1 signing pins.

- [ ] **Step 3: Implement registry-derived authority**

Remove caller control over the effective v2 recipient/signing policy. Select active recipients and the local certificate from the verified parent registry. Authenticate the authority digest in manifest, encryption context, signature preimage, and decrypted archive. Extend the validator with schema dispatch while preserving v1 checks and the existing CI key lookup.

- [ ] **Step 4: Run GREEN**

Run the Step 2 command. Confirm the unchanged CI fixture decrypts and validates both migration and B publication.

- [ ] **Step 5: Commit Task 6**

```bash
git add src/intent_engineering/team_state/{publication.py,candidate.py,restore.py,crypto.py} \
  tests/integration/team_state/{test_publication.py,test_candidate.py,test_restore.py}
git commit -m "feat: accept certified member publications"
```

### Task 7: Compose enrollment with Task 6 receipts and GitHub TOCTOU fences

**Files:**

- Modify: `src/intent_engineering/team_state/setup.py`
- Modify: `src/intent_engineering/team_state/github.py`
- Modify: `src/intent_engineering/team_state/github_publication.py`
- Modify: `src/intent_engineering/team_state/enrollment.py`
- Modify: `tests/unit/team_state/test_setup.py`
- Modify: `tests/unit/team_state/test_github.py`
- Modify: `tests/unit/team_state/test_github_publication.py`
- Modify: `tests/integration/team_state/test_setup_bridge.py`

**Interfaces:**

```python
class EnrollmentApprovalRequest(StrictModel):
    preview: JoinApprovalPreviewV2
    github_preflight: GitHubProtectionPreview

class EnrollmentReceiptV2(StrictModel):
    invite_id: str
    response_digest: str
    authority_before_digest: str
    authority_after_digest: str
    publication_manifest_digest: str
    phase: Literal["approved", "publication-pending", "pr-pending", "merged", "closed"]

class GitHubSetupBridge:
    def preview_member_approval(...) -> EnrollmentApprovalRequest: ...
    def approve_member(...) -> GitHubEnableResult: ...
    def reconcile_member_approval(...) -> GitHubEnableResult: ...
```

- [ ] **Step 1: Write RED lifecycle tests**

Prove the approval preview binds exact state head/base, manifest/bundle/authority before/after, default commit/tooling bytes, protection snapshot, required-workflow descriptor/ref, Actions app ID, runner group/runner digest, identity, invite/response, and sponsor decision. Add races changing each preimage between preview and action. Add lost publication-ref response, lost PR response, restart with commit-only receipt, open-PR cancel rejection, attempted-write-without-receipt cancel rejection, closed-unmerged explicit restart, merge reconciliation, and legacy Task 6 draft migration.

- [ ] **Step 2: Run RED**

Run: `pytest -q tests/unit/team_state/test_setup.py tests/unit/team_state/test_github.py tests/unit/team_state/test_github_publication.py tests/integration/team_state/test_setup_bridge.py -x`

Expected: enrollment cannot yet enter the Task 6 guarded publication path.

- [ ] **Step 3: Implement monotonic receipt composition**

Persist the enrollment approval, then use the existing `EncryptedPublicationDraft`. Set its `external_write_attempted` flag before the publisher network call. Persist commit before PR call and PR receipt after exact response validation. Enrollment reconciliation reads rather than replaces publication authority. Never add a direct state ref update. Recheck exact live preflight immediately before publisher and PR calls.

- [ ] **Step 4: Run GREEN**

Run the Step 2 command. Verify cancellation exceptions leave both receipts and prepared artifacts intact.

- [ ] **Step 5: Commit Task 7**

```bash
git add src/intent_engineering/team_state/{setup.py,github.py,github_publication.py,enrollment.py} \
  tests/unit/team_state/{test_setup.py,test_github.py,test_github_publication.py} \
  tests/integration/team_state/test_setup_bridge.py
git commit -m "feat: publish enrollment through guarded state prs"
```

### Task 8: Activate B through automatic restore and add divergence reconciliation

**Files:**

- Modify: `src/intent_engineering/team_state/restore.py`
- Modify: `src/intent_engineering/team_state/local_trust.py`
- Modify: `src/intent_engineering/team_state/governance.py`
- Create: `src/intent_engineering/team_state/reconciliation.py`
- Modify: `src/intent_engineering/control_plane/models.py`
- Modify: `src/intent_engineering/control_plane/service.py`
- Modify: `tests/integration/team_state/test_restore.py`
- Modify: `tests/integration/team_state/test_governance.py`
- Create: `tests/unit/team_state/test_reconciliation.py`
- Modify: `tests/integration/control_plane/test_control_plane_service.py`

**Interfaces:**

```python
class TeamStateDivergenceCaseV2(StrictModel):
    repository_id: str
    project_id: str
    common_parent_bundle_digest: str
    remote_manifest_digest: str
    local_manifest_digest: str
    remote_authority_digest: str
    local_authority_digest: str
    remote_commit: str
    local_publication_commit: str | None
    changed_paths: tuple[str, ...]


class ReconciliationService:
    def preview(
        self,
        *,
        common: VerifiedReleaseV2,
        remote: VerifiedReleaseV2,
        local: CanonicalStateSnapshot,
    ) -> ReconciliationPreview: ...

    def prepare(
        self,
        *,
        preview: ReconciliationPreview,
        decision: VerifiedHumanDecision,
        current_remote: VerifiedReleaseV2,
    ) -> PreparedPublication: ...
```

- [ ] **Step 1: Write RED restore/reconciliation tests**

Test B pending trust rejects unrelated root, non-descendant release, absent/different B cert, changed response keys, inactive membership, wrong CI record, invalid authority attestation, and local divergence. Test exact merged enrollment atomically installs state/trust/governance and crash/retry is idempotent. For A/B concurrent children, assert no overwrite, a bounded secret-free case, deterministic three-way preview, conflict requiring explicit choice, fresh WebAuthn, recheck against current remote, and a final descendant publication. Authority conflicts must never auto-merge.

- [ ] **Step 2: Run RED**

Run: `pytest -q tests/integration/team_state/test_restore.py tests/integration/team_state/test_governance.py tests/unit/team_state/test_reconciliation.py tests/integration/control_plane/test_control_plane_service.py -x`

Expected: pending-join activation and typed reconciliation are absent.

- [ ] **Step 3: Implement atomic activation and reviewed reconciliation**

Permit pending trust to validate only the exact enrollment descendant. After cryptographic/decryption/archive checks, use one tree-fenced transaction to install state and advance public trust/governance. Build reconciliation from the common parent and current remote, surface unresolved semantic conflicts, bind the resolved snapshot/current base into WebAuthn, and prepare a normal registry-preserving publication.

- [ ] **Step 4: Run GREEN**

Run the Step 2 command. Add a recovery run after every injected transaction boundary and assert either the old complete state or new complete state, never a mixture.

- [ ] **Step 5: Commit Task 8**

```bash
git add src/intent_engineering/team_state/{restore.py,local_trust.py,governance.py,reconciliation.py} \
  src/intent_engineering/control_plane/{models.py,service.py} \
  tests/integration/team_state/{test_restore.py,test_governance.py} \
  tests/unit/team_state/test_reconciliation.py \
  tests/integration/control_plane/test_control_plane_service.py
git commit -m "feat: restore members and reconcile state divergence"
```

### Task 9: Expose the three-command CLI and browser approval journey

**Files:**

- Modify: `src/intent_engineering/cli/team.py`
- Create: `src/intent_engineering/cli/team_enrollment.py`
- Modify: `src/intent_engineering/control_plane/web.py`
- Modify: `src/intent_engineering/control_plane/static/app.js`
- Modify: `src/intent_engineering/control_plane/static/index.html`
- Modify: `tests/unit/cli/test_team.py`
- Create: `tests/unit/cli/test_team_enrollment.py`
- Modify: `tests/integration/control_plane/test_web.py`
- Modify: `tests/browser/test_control_plane.py`

**Interfaces:**

```text
intent team invite --project PATH --github-account-id ID --github-login LOGIN --output FILE
intent team join --project PATH --invite FILE --output FILE
intent team approve-join --project PATH --response FILE
```

- [ ] **Step 1: Write RED CLI/browser tests**

Assert exact minimal prompts, public output files with owner-safe creation and no overwrite, no private fields, B-local WebAuthn, A approval preview of identities/keys/authority diffs, progress/restart states, cancel hidden after any external-write attempt, closed-PR restart/discard requiring fresh approval, fixed errors, browser CSP/CSRF/origin behavior, and no token or response-body leakage. Verify normal B restore/publication requires no new command.

- [ ] **Step 2: Run RED**

Run: `pytest -q tests/unit/cli/test_team.py tests/unit/cli/test_team_enrollment.py tests/integration/control_plane/test_web.py tests/browser/test_control_plane.py -x`

Expected: commands, endpoints, and browser states are absent.

- [ ] **Step 3: Implement thin adapters**

Keep cryptographic and lifecycle rules in enrollment/setup services. CLI reads each public input once with a strict byte cap and writes with exclusive creation. Browser uses opaque local session IDs and never receives private key bytes. Reuse the existing WebAuthn ceremony and publication status components.

- [ ] **Step 4: Run GREEN**

Run the Step 2 command. Search captured output and generated fixtures for seeded private-key/token values.

- [ ] **Step 5: Commit Task 9**

```bash
git add src/intent_engineering/cli/{team.py,team_enrollment.py} \
  src/intent_engineering/control_plane/{web.py,static/app.js,static/index.html} \
  tests/unit/cli/{test_team.py,test_team_enrollment.py} \
  tests/integration/control_plane/test_web.py \
  tests/browser/test_control_plane.py
git commit -m "feat: guide second developer enrollment"
```

### Task 10: Prove the complete two-developer release and document operations

**Files:**

- Create: `tests/e2e/test_second_developer_enrollment.py`
- Modify: `tests/e2e/test_team_setup_bridge.py`
- Modify: `tests/e2e/test_release.py`
- Modify: `docs/intent-aware-agent.md`
- Modify: `docs/superpowers/specs/2026-09-09-second-developer-enrollment-design.md`

- [ ] **Step 1: Write RED end-to-end scenarios**

Use separate A/B checkout directories, keyring namespaces, WebAuthn credentials, and trust files plus the existing CI recipient. Cover: v1 migration; invite/response/approval; pending PR; B auto-restore after a rewritten linear merge; B publication accepted by A and CI; A/B competing publications; reviewed reconciliation; revoked B rejection; lost ref/PR responses; cancel after attempted write; closed-PR restart; >1 MiB bundle raw reads; oversized/cancelled reads; and a byte scan proving A never receives B private material and B never receives A/root private material.

- [ ] **Step 2: Run the e2e RED**

Run: `pytest -q tests/e2e/test_second_developer_enrollment.py -x`

Expected: at least the first not-yet-integrated journey assertion fails for the intended reason.

- [ ] **Step 3: Complete docs and fixtures without weakening production checks**

Document the organization/plan requirement inherited from Task 6, classic `repo + admin:org` setup-token scope, exact GitHub runner name/group/workflow restrictions, three public enrollment commands, migration permanence, root backup/loss limits, revocation, reconciliation, and safe closed-PR recovery. Do not document a direct state ref update, reusable token, personal runner as sufficient, or private-key export.

- [ ] **Step 4: Run scoped and full verification**

Run, in order:

```bash
pytest -q tests/e2e/test_second_developer_enrollment.py tests/e2e/test_team_setup_bridge.py
pytest -q tests/unit/team_state tests/integration/team_state tests/unit/cli tests/integration/control_plane tests/browser
ruff format --check src tests
ruff check src tests
mypy src
git diff --check
```

Expected: all commands exit zero. Review `git diff --stat` and `git status --short`; only planned files are changed and no key material, exported invitation/response, local receipt, or generated state artifact is tracked.

- [ ] **Step 5: Commit Task 10**

```bash
git add tests/e2e/test_second_developer_enrollment.py \
  tests/e2e/test_team_setup_bridge.py \
  tests/e2e/test_release.py \
  docs/intent-aware-agent.md \
  docs/superpowers/specs/2026-09-09-second-developer-enrollment-design.md
git commit -m "test: prove second developer team journey"
```

## Migration and rollback runbook

1. Merge the v2 validator/archive/authority implementation to the protected default branch through a CODEOWNERS-reviewed PR.
2. Re-run Task 6 preflight and bind the exact default commit, workflow bytes, required-workflow rule, protection snapshots, runner group, runner membership, and CI descriptor.
3. A previews and WebAuthn-approves the v1→v2 migration. The state PR uses the existing three artifacts and unchanged CI recipient.
4. If the PR is pending or any write response is ambiguous, retain all receipts/keys and reconcile. If it closes unmerged, use explicit restart/discard with fresh authority. Do not advance local trust.
5. After merge, A and CI atomically activate root trust only after dual validation. Keep legacy keys until a valid v2 descendant is observed.
6. Only then create B's invitation. B's activation remains pending until the exact enrollment descendant is merged.
7. Application rollback after v2 is read-only for shared state. It must report upgrade-required and must not publish v1. State rollback is a new reviewed v2 descendant, never ref rewinding.
8. Root loss has no software bypass. Root compromise uses a separately reviewed old-root-authorized rotation and member/device revocation; threshold/recovery roots require a new design review.

## Final review checklist

- [ ] State branch still has exactly manifest, bundle, and signatures.
- [ ] Authority registry is encrypted and canonical; required public certificates are cross-checked after decrypt.
- [ ] Stable root is pinned; ordinary publication never loads it.
- [ ] A, B, and CI retain separate private-key custody.
- [ ] CI recipient/workflow/runner restriction remains unchanged and exact.
- [ ] Every authority change has sponsor role, fresh WebAuthn, root attestation, parent binding, and monotonic sequence.
- [ ] Every ordinary publication preserves exact parent authority.
- [ ] v1 upgrade is dual-verifiable and downgrade is impossible after acceptance.
- [ ] B auto-restore activation is exact, atomic, and idempotent.
- [ ] Task 6 receipts dominate cancel/retry behavior.
- [ ] All network, JSON, archive, collection, expiry, ancestry, and pagination bounds are tested.
- [ ] All TOCTOU preimages are rechecked immediately before writes.
- [ ] Divergence creates a bounded review case and never overwrites either side.
- [ ] Fixed errors and logs contain no credentials, private material, or hostile provider bodies.
