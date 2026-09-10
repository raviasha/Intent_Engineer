# Task 7 Second-Developer Enrollment and Publication Design Addendum

**Date:** 2026-09-09  
**Status:** Approved implementation design addendum  
**Depends on:** Task 6 GitHub state preflight, protected `intent-state` publication, CI recipient provisioning, and automatic restore  
**Scope:** enroll a second human developer, let that developer restore and publish, and reconcile concurrent publication without transporting private keys

## Decision summary

Task 7 introduces a version 2 state envelope and a canonical, encrypted team authority registry. Trust changes from “every installation pins the complete set of allowed state-signing keys” to “every installation pins one stable team root and accepts a state signature from an active, root-certified device under the parent authority policy.” Human enrollment is a public, replay-resistant invite/response exchange followed by a sponsor's fresh WebAuthn approval. All recipient and signing private keys are generated and retained on the device that owns them.

The existing state branch continues to contain exactly three artifacts: manifest, encrypted bundle, and signature envelope. The authority registry is a canonical file inside the encrypted archive, never a fourth public artifact. The existing CI recipient, runner, restricted runner group, required-workflow rule, protected default-branch tooling binding, and Task 6 publication receipts remain the transport and enforcement boundary.

The first version 2 publication is an explicit, dual-verifiable migration from version 1. Existing version 1 state remains readable. Once a checkout has accepted a version 2 descendant, it never publishes or rolls back to version 1.

## Goals and non-goals

The design must prove this journey:

1. Developer A has a Task 6 repository and a merged version 1 state release.
2. A creates a bounded public invitation for one exact verified GitHub account.
3. Developer B verifies the repository and invitation, creates device-local encryption and signing keys, performs WebAuthn locally, and returns only a public response.
4. A verifies the response against live GitHub identity and current state, previews the exact authority transition, and approves it with fresh WebAuthn.
5. The transition is published through the existing protected state PR. CI decrypts it with its unchanged recipient key and validates the migration.
6. After merge, B automatically restores the state with its own recipient private key and activates root trust only after proving that the merged registry contains B's exact approved device certificate.
7. B publishes an ordinary state update. A and CI accept it without adding B's public key out of band.
8. Concurrent A/B publications fail closed as divergence and are resolved by a freshly reviewed reconciliation publication.

This scope does not implement threshold roots, automatic root recovery, unattended member approval, repository-hosted private material, history rewriting, or automatic semantic conflict resolution. A single sponsor-controlled root is the initial policy. Root rotation is modeled but deferred.

## Command and interaction surface

The enrollment UX adds only three explicit commands. Normal restore and publication continue through the existing `intent dev`/control-plane paths.

```text
A: intent team invite --project . --github-account-id 24680 --github-login bob \
     --output bob.intent-invite.json

B: intent team join --project . --invite bob.intent-invite.json \
     --output bob.intent-join.json

A: intent team approve-join --project . --response bob.intent-join.json
```

`invite` is read-only with respect to GitHub and writes one owner-controlled public receipt plus the requested public export. `join` performs verified GitHub identity lookup and WebAuthn, creates private keys directly in the OS keyring, and exports only public material. `approve-join` opens the existing browser preview, requires sponsor WebAuthn over the exact before/after authority digests, and then uses the Task 6 state publication lifecycle. A closed, unmerged PR is explicitly reconciled before retry; a pending or ambiguous external write cannot be cancelled.

The public files are safe to move through an authenticated chat, email, removable media, or another user-selected channel. The product does not upload them automatically and never suggests moving a keyring export.

## Cryptographic trust model

### Stable root and device keys

Each team has one Ed25519 root key for an authority epoch. Its identifier is `root:sha256:<hex>` over the raw 32-byte public key plus the exact project and repository identities. The root private key is generated into A's OS keyring during the version 1-to-version 2 migration. It is used only to attest authority registries and certify or revoke devices, never to sign ordinary state payloads.

Each human device has two independent private keys:

- an X25519 recipient key used to unwrap the encrypted bundle content key; and
- an Ed25519 device signer used to sign state manifests.

Both are generated locally and stored in the existing OS-keyring abstraction under distinct versioned service/account names. The device certificate binds both public keys, the verified GitHub identity, the member and device identities, and the digest of the local WebAuthn credential. Possession is proved in the join response. No private key, content key, WebAuthn private material, or keyring blob appears in a receipt, log, command line, browser payload, archive, or GitHub object.

CI remains an encryption-only principal. Its `CiRecipientRecord` is carried forward byte-for-byte into the version 2 registry. CI is never a member, sponsor, root holder, device signer, or WebAuthn authority.

### Publication authorization

An ordinary version 2 release is accepted when all of the following hold:

- its exact parent is the trusted current release;
- its manifest authority digest equals the parent's validated authority digest;
- the envelope has one to eight unique signatures from active, unexpired, non-revoked device certificates in that authority;
- the authority policy threshold is met (initially one active human device); and
- the decrypted archive authority bytes exactly hash to the manifest authority digest.

An authority-changing release additionally requires a root-signed `AuthorityAttestationV2` binding the previous and next authority digests, the parent release, the sponsor WebAuthn decision digest, and the exact membership operation. The acting device must belong to an active sponsor. An ordinary member cannot add, revoke, or promote a member merely by signing a state release.

### Why the certificate is public

The signature envelope includes only the certificates needed to verify that release. This lets B verify the migration envelope against the root pinned in the invitation before decrypting. After decryption, the same certificate and authority digest must appear in the canonical registry; disagreement is invalid. Certificates reveal the same public account/key facts already present in the join response. The encrypted registry remains the canonical membership and revocation source.

## Exact version 2 data contracts

All models use frozen strict Pydantic configuration, `extra="forbid"`, canonical UTF-8 JSON with sorted keys and no insignificant whitespace, duplicate-key rejection before validation, exact URL-safe base64 without padding, and explicit JSON-list-to-tuple conversion. Identifiers and collections are sorted and unique wherever order has no semantic meaning. Timestamps are UTC and canonicalized to `Z` with whole seconds.

### Root, membership, and device models

```python
class TeamRootTrustV2(StrictModel):
    schema_version: Literal[2] = 2
    project_id: ProjectId
    repository_id: RepositoryId
    authority_epoch: int  # 1..2**31-1
    root_key_id: RootKeyId
    root_public_key: Base64Url32
    created_at: UtcSecond
    predecessor_root_key_id: RootKeyId | None = None


class MemberRecordV2(StrictModel):
    member_id: MemberId
    actor: ActorId
    github_account_id: int  # positive, stable numeric identity
    github_login: GitHubLogin  # display/audit value; account id is authoritative
    role: Literal["sponsor", "member"]
    status: Literal["active", "revoked"]
    device_certificate_ids: tuple[DeviceCertificateId, ...]  # 1..8, sorted unique
    enrolled_at: UtcSecond
    revoked_at: UtcSecond | None = None


class DeviceCertificateClaimsV2(StrictModel):
    schema_version: Literal[2] = 2
    project_id: ProjectId
    repository_id: RepositoryId
    authority_epoch: int
    member_id: MemberId
    device_id: DeviceId
    github_account_id: int
    github_login: GitHubLogin
    recipient_key_id: RecipientKeyId
    recipient_public_key: Base64Url32  # X25519
    signature_id: SignatureId
    signing_public_key: Base64Url32  # Ed25519
    webauthn_credential_digest: Sha256Digest
    serial: int  # positive and unique in one authority epoch
    issued_at: UtcSecond
    expires_at: UtcSecond  # <= issued_at + 366 days


class DeviceSignerCertificateV2(StrictModel):
    claims: DeviceCertificateClaimsV2
    certificate_id: DeviceCertificateId  # digest of canonical claims bytes
    algorithm: Literal["ed25519-root-certificate-v1"]
    root_key_id: RootKeyId
    root_signature: Base64Url64


class DeviceRevocationV2(StrictModel):
    certificate_id: DeviceCertificateId
    revoked_at: UtcSecond
    reason: Literal["replaced", "lost", "compromised", "member-removed"]
    sponsor_member_id: MemberId
```

`member_id` is a domain-separated SHA-256 digest of project, repository, and numeric GitHub account ID. `device_id` is a random 128-bit public identifier created on the owning device. Key and certificate IDs are domain-separated SHA-256 digests and are recomputed on every parse. A login rename does not create a new member, but the live numeric account identity must still resolve to the response login during approval.

### Canonical authority registry

```python
class TeamAuthorityPolicyV2(StrictModel):
    ordinary_signature_threshold: Literal[1] = 1
    authority_change_sponsor_threshold: Literal[1] = 1
    max_active_members: int = 32
    max_active_devices: int = 63


class TeamAuthorityRegistryV2(StrictModel):
    schema_version: Literal[2] = 2
    project_id: ProjectId
    repository_id: RepositoryId
    authority_epoch: int
    sequence: int  # 1..2**63-1, exactly parent + 1 on a change
    root: TeamRootTrustV2
    policy: TeamAuthorityPolicyV2
    members: tuple[MemberRecordV2, ...]  # 1..32, sorted by member_id
    device_certificates: tuple[DeviceSignerCertificateV2, ...]  # 1..63
    revocations: tuple[DeviceRevocationV2, ...]  # 0..256
    ci_recipient: CiRecipientRecord
    previous_authority_digest: Sha256Digest | None
```

The canonical registry path is `authority/team-authority.json`. Archive version 2 contains the ten existing canonical state paths plus this exact path. Human active recipient IDs are derived from active, unrevoked certificates; the unchanged CI recipient is appended; the resulting sorted set must exactly equal the manifest recipient set. There may be at most 64 total encryption recipients.

### Manifest and signature envelope

```python
class TeamStateManifestV2(StrictModel):
    schema_version: Literal[2] = 2
    archive_version: Literal[2] = 2
    project_id: ProjectId
    repository_id: RepositoryId
    graph_version: int
    parent_bundle_digest: Sha256Digest | None
    bundle_digest: Sha256Digest
    bundle_size: int
    encryption_algorithm: Literal["x25519-hkdf-sha256-aes256gcm-v1"]
    recipient_key_ids: tuple[RecipientKeyId, ...]  # 2..64, sorted unique
    signing_policy: Literal["root-certified-device-threshold-v1"]
    authority_digest: Sha256Digest
    authority_epoch: int
    root_key_id: RootKeyId
    created_at: UtcSecond
    migration: V1MigrationBinding | None


class CertifiedStateSignatureV2(StrictModel):
    certificate_id: DeviceCertificateId
    signature_id: SignatureId
    algorithm: Literal["ed25519-v1"]
    signature: Base64Url64


class AuthorityAttestationV2(StrictModel):
    schema_version: Literal[2] = 2
    project_id: ProjectId
    repository_id: RepositoryId
    authority_epoch: int
    previous_authority_digest: Sha256Digest | None
    authority_digest: Sha256Digest
    parent_bundle_digest: Sha256Digest | None
    operation: Literal["v1-migration", "enroll", "revoke", "promote", "root-rotate"]
    subject_digest: Sha256Digest
    sponsor_member_id: MemberId
    sponsor_device_certificate_id: DeviceCertificateId
    sponsor_decision_digest: Sha256Digest
    decided_at: UtcSecond
    root_key_id: RootKeyId
    root_signature: Base64Url64


class V1MigrationProof(StrictModel):
    prior_manifest_digest: Sha256Digest
    legacy_signatures: tuple[StateSignature, ...]  # exact prior trusted v1 IDs


class StateSignatureEnvelopeV2(StrictModel):
    schema_version: Literal[2] = 2
    manifest_digest: Sha256Digest
    bundle_digest: Sha256Digest
    authority_digest: Sha256Digest
    certificates: tuple[DeviceSignerCertificateV2, ...]  # 1..8, sorted
    signatures: tuple[CertifiedStateSignatureV2, ...]  # 1..8, sorted
    authority_attestation: AuthorityAttestationV2 | None
    migration_proof: V1MigrationProof | None
```

The signed state preimage is canonical JSON containing domain, schema version, manifest digest, bundle digest, authority digest, authority epoch, repository identity, project identity, and exact parent bundle digest. The root certificate and authority-attestation preimages have distinct fixed domains. Cross-protocol signature reuse is therefore rejected.

For unchanged authority, both authority fields are absent. For an authority change, `authority_attestation` is required. `migration_proof` is present exactly once, on the first version 2 release, and the manifest migration binding names the exact version 1 parent manifest and signer set.

### Enrollment exchange

```python
class TeamInviteV2(StrictModel):
    schema_version: Literal[2] = 2
    invite_id: InviteId  # digest of canonical unsigned invitation
    nonce: Base64Url32
    project_id: ProjectId
    repository_id: RepositoryId
    default_branch: GitRefName
    state_branch: Literal["intent-state"]
    intended_github_account_id: int
    intended_github_login: GitHubLogin
    intended_role: Literal["member"] = "member"
    authority_digest: Sha256Digest
    authority_sequence: int
    root: TeamRootTrustV2
    sponsor_member_id: MemberId
    sponsor_certificate: DeviceSignerCertificateV2
    base_state_commit: GitObjectId
    base_bundle_digest: Sha256Digest
    created_at: UtcSecond
    expires_at: UtcSecond  # <= created_at + 24h
    sponsor_signature: Base64Url64


class JoinResponseV2(StrictModel):
    schema_version: Literal[2] = 2
    invite_id: InviteId
    invite_digest: Sha256Digest
    project_id: ProjectId
    repository_id: RepositoryId
    github_account_id: int
    github_login: GitHubLogin
    actor: ActorId
    device_id: DeviceId
    recipient_key_id: RecipientKeyId
    recipient_public_key: Base64Url32
    signature_id: SignatureId
    signing_public_key: Base64Url32
    webauthn_credential_id: CredentialId
    webauthn_public_key: Base64Url
    created_at: UtcSecond
    expires_at: UtcSecond
    device_possession_signature: Base64Url64
    recipient_possession_proof: Base64Url32
    webauthn_decision: VerifiedHumanDecision


class SponsorJoinApprovalV2(StrictModel):
    schema_version: Literal[2] = 2
    invite_id: InviteId
    join_response_digest: Sha256Digest
    authority_before_digest: Sha256Digest
    authority_after_digest: Sha256Digest
    certificate_id: DeviceCertificateId
    sponsor_member_id: MemberId
    sponsor_decision_digest: Sha256Digest
```

The recipient possession proof is a domain-separated X25519 shared-secret challenge using a response-bound ephemeral public key in the invitation; it proves possession without disclosing the private key. The WebAuthn decision subject includes every join response field, current live state commit and bundle digest, authority before/after digests, certificate claims, and expiry. Sponsor approval uses a separate WebAuthn challenge and subject. Assertions are verified once and persisted only as bounded public decision receipts/digests.

### Local receipts and trust

```python
class PendingJoinTrustV2(StrictModel):
    schema_version: Literal[2] = 2
    phase: Literal["response-ready", "awaiting-merge"]
    invite: TeamInviteV2
    response: JoinResponseV2
    local_recipient_key_id: RecipientKeyId
    local_signature_id: SignatureId
    expected_root_key_id: RootKeyId
    expected_authority_before_digest: Sha256Digest
    external_write_attempted: bool


class LocalTrustConfigV2(StrictModel):
    schema_version: Literal[2] = 2
    project_id: ProjectId
    repository_id: RepositoryId
    root: TeamRootTrustV2
    member_id: MemberId
    device_certificate_id: DeviceCertificateId
    recipient_key_id: RecipientKeyId
    signature_id: SignatureId
    accepted_authority_digest: Sha256Digest
    accepted_authority_sequence: int
    accepted_bundle_digest: Sha256Digest
```

These files contain public metadata only and use the current owner-only directory, no-follow path, same-path lock, canonical-byte, 0600 file, and atomic replacement rules. Private keys are loaded by key ID from the keyring. A pending join is not active trust: it can validate only a version 2 release rooted in the exact invite root, descending from the invite base, containing the exact join response keys and an active certificate for B. Activation and state installation are one transactional operation.

## Enrollment state machine

### Sponsor A

`none -> invited -> response_verified -> approved -> external_write_attempted -> pr_pending -> merged -> active`

- Invitation creation snapshots the exact live state ref, manifest, authority digest, default-branch tooling commit, required-workflow rule, branch protection policy, runner-group/runner digest, and verified GitHub account ID.
- Response import rechecks all of those values and the invite expiry before displaying a preview.
- WebAuthn approval binds the exact transition and Task 6 GitHub protection preview.
- Before the first GitHub write, the enrollment receipt and the existing `EncryptedPublicationDraft.external_write_attempted` marker are durable.
- A lost publication-ref or PR response is reconciled through the exact Task 6 commit/ref/PR receipt path. Cancel is disabled while the write outcome is ambiguous or a PR is open.
- A closed-unmerged PR requires an explicit restart/discard action with fresh authority and WebAuthn; it never silently reuses stale approval.

### Joiner B

`none -> keys_created -> response_ready -> awaiting_merge -> active`

- Failure before response export may delete only the public pending receipt; keyring keys may be explicitly removed by exact IDs after confirming no response was exported.
- Once `external_write_attempted` or exported-response acknowledgement is recorded, cancellation retains the receipt and keys until A's outcome is reconciled.
- Automatic restore observes the merged release, validates it, decrypts with B's key, proves exact inclusion, and atomically changes `awaiting_merge` to `active` while installing state.
- Expired, rejected, or closed-unmerged invitations use an explicit discard flow that proves there is no live PR/ref and deletes only the exact pending public receipt and exact locally named unused keys.

## Version 1 upgrade

The upgrade is a state publication, not an in-place local configuration edit.

1. A's current version 1 recipient and signing identities and the CI recipient are loaded under existing trust.
2. A generates a new root key and a new device signing key in A's keyring. A's existing X25519 recipient may be reused and is bound into A's device certificate.
3. A constructs registry sequence 1 with A as sponsor and the existing CI recipient.
4. A approves the exact migration with WebAuthn. The root certifies A's device and attests the registry.
5. The version 2 envelope includes the new certificate/signature and a `V1MigrationProof` signed by the exact legacy signer set expected by the version 1 parent.
6. The unchanged CI recipient decrypts the new bundle. The updated validator validates both the legacy bridge and version 2 chain, then atomically advances its public trust record to the root.
7. Human restore does the same. Legacy keyring entries are retained until the migration and one descendant have been observed, then are eligible for explicit cleanup.

The protected default branch must already contain the exact version 2 validator and archive implementation before the migration PR is opened. Task 6's default-tooling content checks, CODEOWNERS coverage of all workflows, `ci/launch.py`, and `src/intent_engineering/`, restricted runner-group proof, required-workflow rule, state protection, and bound live commit are rechecked immediately before publication.

A version 1 reader seeing schema 2 returns a fixed upgrade-required result; it never treats it as absent state. A version 2 reader accepts old schema 1 releases only while local trust has not accepted a schema 2 descendant. After advancement, version 1 is a rollback and is rejected even if correctly signed. Operational rollback of the application is read-only; recovery publishes a new version 2 descendant, never rewrites `intent-state`.

## Restore and publication behavior

### B automatic restore

The fixed-ref reader fetches only the protected `intent-state` ref and the three bounded artifacts, exactly as in Task 6. It verifies Git lineage and artifact bytes, validates the public version 2 envelope against the invite-pinned root, and only then decrypts. The decrypted archive must contain a canonical registry whose digest matches the manifest and whose exact active B certificate matches the pending response. Recipient sets, certificate sets, authority sequence, and CI record are recomputed rather than trusted from redundant fields.

Installation retains Task 6's tree fence and transactional preimage checks. The pending receipt, active local trust, governance record, and canonical state install commit together or not at all. A crash is idempotently recovered from canonical receipts.

### B ordinary publication and A acceptance

B uses the existing publication preview. Its authority is derived from the validated current registry, not caller-supplied recipient arrays. The bundle is encrypted to all active human device recipients plus the same CI recipient. B signs with its certified device key. Because the authority bytes are unchanged, no root key or sponsor WebAuthn is needed beyond the normal publication decision already required by the control plane.

A accepts the merged release because A pins the stable root, validates B's certificate and active membership in the parent registry, verifies the exact state signature and unchanged authority digest, decrypts with A's recipient key, and passes normal lineage and semantic checks. CI performs the same cryptographic and lineage validation with its existing private recipient key. No allow-list edit, CI re-provision, workflow change, or private-key transfer is needed.

## Divergence and reconciliation

Every preview binds the exact current state commit, parent bundle digest, authority digest/sequence, default tooling commit, required-workflow proof, protection snapshot, and runner proof. The bridge rechecks those preimages immediately before every external write. GitHub PR creation/reuse remains bound to the exact publication commit and exact live base SHA.

If A and B both publish from parent `N`, at most one can become the accepted child. The other path returns a bounded `TeamStateDivergenceCaseV2` containing public identities, the two manifest digests, parent digests, authority digests, graph versions, changed canonical paths, and exact remote/local commit IDs. It contains no plaintext state, bundle key, or private material.

Reconciliation begins from the current accepted remote release. The control plane renders a semantic three-way comparison against the common ancestor, produces an ordinary proposed change set, and requires fresh human review and WebAuthn. It never auto-merges governance ledgers, intent statements with conflicting identities, or authority registries. Any authority divergence is sponsor-only and requires a new root attestation. Publication uses a new bundle digest, new Task 6 draft, current exact base, and normal PR/check path. Force push, deletion, and direct `intent-state` update remain forbidden.

## Bounds and resource controls

The following are hard limits, not configuration defaults:

| Object | Bound |
|---|---:|
| Public invitation | 32 KiB |
| Public join response | 64 KiB |
| Local enrollment receipt | 128 KiB |
| Canonical authority registry | 256 KiB |
| Active members | 32 |
| Active human device recipients | 63 |
| Devices per member | 8 |
| Total encryption recipients including CI | 64 |
| Revocation records retained | 256 |
| Signatures/certificates in one release | 8 |
| Device certificate canonical bytes | 16 KiB |
| Invitation lifetime | 24 hours |
| WebAuthn decision lifetime | 5 minutes |
| Device certificate lifetime | 366 days |
| Git ancestry walk | existing 64 commits |
| Manifest/signature/bundle/state | existing 64 KiB / 64 KiB / 24 MiB / 16 MiB |

All network reads use the existing bounded JSON and raw-blob paths, response close/finally behavior, cancellation propagation, and pagination limits. SHA-1 Git object IDs identify objects but never substitute for bounded exact-byte hashing and parsing.

## ACL, secrecy, cancellation, and TOCTOU threat analysis

| Threat | Required control |
|---|---|
| Private-key transport | Generate recipient, device, root, and WebAuthn private material only in the owning OS keyring/authenticator. Public exports have schema-level allow-lists and secret-shaped-field negative tests. |
| Local receipt substitution | Owner-only directory/file modes, no symlink or hard-link traversal, canonical bytes, same-path locks, atomic writes, project/repository binding, and exact key IDs. |
| Invite replay or wrong repository | Random nonce, one exact account ID, repository/project/root/base/expiry binding, sponsor signature, durable invite status, and live recheck at response and approval. |
| Login rename/confusable identity | Numeric GitHub account ID is authoritative; login is re-resolved and shown as audit text. No repository-owner-derived CODEOWNER assumption. |
| Join response key substitution | Device Ed25519 proof, X25519 challenge proof, WebAuthn subject over all public keys and invite digest, and sponsor preview over exact certificate claims. |
| Compromised ordinary member adds another member | Authority changes require active sponsor WebAuthn and root attestation; ordinary releases must preserve exact authority digest. |
| Revoked or expired device publishes | Validate certificate time and revocation against parent authority at the signed release time, with bounded clock skew. |
| Root compromise/loss | Root is keyring-only and rarely used. Compromise requires an old-root-authorized rotation/revocation publication; loss has no automatic bypass and requires documented recovery governance outside this slice. |
| Required check spoofing | Preserve Task 6 exact required-workflow rule, Actions app binding, restricted runner group/workflow binding, CODEOWNERS coverage of every workflow and validator/launcher source, and no bypass actors. |
| Malicious PR changes validator | State PR executes the workflow from the protected default-branch source bound by the required-workflow rule; setup rechecks its exact tree/blob bytes and commit before publication. |
| Stale preview / branch race | Bind all state, authority, tooling, protection, ruleset, runner, identity, and receipt digests into WebAuthn; re-read them immediately before each write; exact mismatch invalidates approval. |
| Lost HTTP response | Persist external-write-attempted before the call; reconcile exact ref/commit/PR using Task 6 receipts. Never infer absence from a timeout. |
| Cancel after possible write | Reject cancel when any external write is attempted or any commit/PR receipt exists. Closed-unmerged recovery requires an explicit fresh-authority restart/discard flow. |
| Oversized/cyclic input | Strict depth/collection/byte bounds, duplicate rejection, deterministic non-compressed archive, maximum ancestry, and fail-closed pagination. |
| Downgrade or rollback | Pin accepted authority sequence/root and bundle lineage locally; reject schema 1 after schema 2 and reject lower authority sequence or non-descendant state. |
| Local unpublished divergence | Preserve existing semantic baseline comparison; return a review case, never overwrite. |

Fixed public errors remain coarse (`team enrollment unavailable`, `team enrollment changed`, `team state diverged`, `team enrollment requires reconciliation`) and never interpolate tokens, response bodies, key material, or filesystem contents. Detailed diagnostics may name only public IDs and bounded field categories.

## Composition with Task 6

- The `intent-state` branch contract remains three artifacts. Version dispatch happens inside their existing paths.
- `GitHubSetupBridge` continues to own protection, required workflow, runner proof, exact default tooling, WebAuthn-bound previews, publication commit creation, exact PR binding, and merge reconciliation.
- Enrollment approval produces a `PreparedPublication`; it does not add a direct ref-update path.
- `EncryptedPublicationDraft` remains the authoritative external-write receipt. Enrollment receipts refer to its manifest digest and do not duplicate or weaken its monotonic state.
- The bootstrap-anchor receipt remains relevant only to first Task 6 setup, not member enrollment.
- The required workflow and CI private recipient do not change. The validator implementation on the protected default branch is upgraded before the first version 2 publication.
- CI trust migrates from exact version 1 signer pins to the stable root only through the dual-verifiable migration release.
- Task 6 cancellation rules apply transitively: publication ambiguity always wins over the enrollment UI's desire to discard a draft.

## Acceptance criteria

The feature is complete only when tests prove:

1. canonical version 2 models and archive have one byte representation and reject every malformed, duplicate, oversized, reordered, cross-repository, and downgrade shape;
2. no public export or persisted receipt contains any private key or content key;
3. B cannot join without exact live identity, local WebAuthn, possession proofs, and A's fresh WebAuthn/root approval;
4. version 1 restores continue, the one-way migration dual-verifies, and version 1 rollback after migration fails closed;
5. B automatically restores only the merged release containing B's exact certificate and registry entry;
6. B's ordinary release is accepted by A and by the unchanged CI recipient/workflow;
7. revoked, expired, unknown, or authority-mutating B signatures fail;
8. concurrent A/B releases produce reviewable divergence and a freshly approved descendant reconciliation;
9. lost-response, closed-PR, cancel, crash, stale-preview, ref-race, tooling-race, and oversized-blob cases preserve recoverable receipts and never weaken Task 6 protection; and
10. end-to-end tests use two independent keyrings/checkouts and assert that no private material crosses between them.

## Release proof and operator outcome

The shipped release proof composes the complete customer journey over independent A/B checkout and
keyring namespaces plus the unchanged CI recipient. It covers the one-way v1 migration, public
invite/response/sponsor approval, pending protected publication, automatic B activation after a
linear merge rewrite, B publication accepted by A and CI, competing A/B children, fresh reviewed
reconciliation, and rejection after B revocation. Recovery coverage retains attempted-write
receipts across lost responses and cancellation, supports explicit closed-unmerged restart, and
streams valid bundles above the generic JSON limit while stopping at the explicit raw-media bound.

The operating guide is normative for the supported path: organization-owned repository, compatible
GitHub plan, short-lived classic `repo + admin:org` setup token, exact restricted runner group/name
and workflow bindings, and the three local `invite`, `join`, and `approve-join` commands. It forbids
direct state-ref updates, reusable setup tokens, personal runners as sufficient isolation, and all
private-key export. Root loss, revocation, migration permanence, reviewed reconciliation, and safe
closed-PR recovery are documented as explicit operator boundaries.
