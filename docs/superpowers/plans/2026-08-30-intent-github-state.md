# GitHub Shared Intent State Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Distribute approved intent state through a protected GitHub `intent-state` branch using signed encrypted bundles, automatic restore, publication pull requests, and required validation.

**Architecture:** A provider-neutral team-state domain defines canonical manifests, bundles, lineage, key enrollment, restore, and publication. A Git transport adapter reads refs without checkout and a GitHub adapter opens PRs; cryptographic and semantic verification happens locally before any state replacement.

**Tech Stack:** Python 3.12, `cryptography>=50,<51`, `keyring>=25.7,<26`, existing secure storage/transactions, Git subprocess adapter, existing GitHub HTTP client, pytest.

**Spec:** `docs/superpowers/specs/2026-08-29-intent-dev-control-plane-design.md`

## Global Constraints

- Never store plaintext canonical state, credentials, or private keys in GitHub.
- Developers never check out or merge `intent-state` into code branches.
- Bundles are deterministic before randomized encryption; manifests and ciphertext digests are canonical and bounded.
- Restore validates signature, repository/project binding, lineage, decryption, every canonical ledger, ACL projection, and graph invariants before atomic replacement.
- State publication always targets a PR; no direct protected-branch write.
- A stale or divergent parent never overwrites current shared state.

---

### Task 1: Canonical manifest, bundle inventory, and lineage models

**Files:** Create `src/intent_engineering/team_state/{__init__,models}.py`; test `tests/unit/team_state/test_models.py`; modify `pyproject.toml` for direct cryptography/keyring dependencies.

**Interfaces:** `CanonicalStateSnapshot`, `TeamStateManifest`, `BundleInventory`, `RecipientRecord`, `PublicationLineage`, `RemoteStateSnapshot`, `PreparedPublication`, and `canonical_manifest_bytes()`.

```python
def canonical_manifest_bytes(manifest: TeamStateManifest) -> bytes: ...
```

- [ ] Write strict canonical model RED tests for exact IDs/digests, `Z` timestamps, sorted unique recipients/signers, parent/genesis rules, algorithm allowlist, byte bounds, duplicate keys, and typed noncanonical JSON.
- [ ] Implement frozen models and canonical bytes; use algorithm IDs `x25519-hkdf-sha256-aes256gcm-v1` and `webauthn-decision-v1` only.
- [ ] Run model/core compatibility and commit `feat: define shared intent bundle contracts`.

### Task 2: Deterministic archive and cryptographic envelope

**Files:** Create `src/intent_engineering/team_state/{archive,crypto}.py`; tests `tests/unit/team_state/test_archive.py`, `test_crypto.py`.

**Interfaces:** `build_archive(snapshot) -> bytes`, `validate_archive(bytes) -> RestoredSnapshot`, `encrypt_bundle(plaintext, recipients, aad) -> EncryptedBundle`, `decrypt_bundle(bundle, key, aad) -> bytes`.

```python
def build_archive(snapshot: CanonicalStateSnapshot) -> bytes: ...
def validate_archive(content: bytes) -> RestoredSnapshot: ...
def encrypt_bundle(plaintext: bytes, recipients: tuple[RecipientRecord, ...], aad: bytes) -> EncryptedBundle: ...
def decrypt_bundle(bundle: EncryptedBundle, private_key: bytes, aad: bytes) -> bytes: ...
```

- [ ] Write RED tests for deterministic file order/mode/time, path traversal, links/devices, duplicate entries, decompression bombs, ciphertext/AAD/recipient substitution, nonce reuse prevention, wrong key, truncation, cancellation, and plaintext/secret traceback retention.
- [ ] Implement a custom length-prefixed canonical archive rather than tar/zip ambiguity; enforce per-file and aggregate limits.
- [ ] Implement random 256-bit content key, AES-256-GCM payload, per-recipient X25519 shared secret + HKDF-SHA256 wrapped key, and exact manifest AAD using cryptography recipes/primitives.
- [ ] Run 100 randomized round trips plus tamper matrix and commit `feat: encrypt canonical intent state bundles`.

### Task 3: Operating-system recipient key store and enrollment

**Files:** Create `src/intent_engineering/team_state/keys.py`; modify `src/intent_engineering/control_plane/service.py` and web API/UI; tests `tests/unit/team_state/test_keys.py`, `tests/e2e/test_team_enrollment.py`.

**Interfaces:** `RecipientKeyStore.generate(project_id, actor) -> RecipientRecord`, `.private_key(key_id)`, `.delete(key_id)`; GitHub identity verifier port returns exact account ID/login.

```python
class RecipientKeyStore(Protocol):
    def generate(self, project_id: str, actor: str) -> RecipientRecord: ...
    def private_key(self, key_id: str) -> bytes: ...
    def delete(self, key_id: str) -> None: ...
```

- [ ] RED-test keyring unavailable/locked, duplicate enrollment, wrong GitHub identity, actor-policy mismatch, credential replacement, key deletion, cancellation, no private-key serialization/logging, and WebAuthn-bound enrollment.
- [ ] Implement a keyring adapter with an in-memory fake; store only base64url private bytes under service `intent-engineering/<project-id>`, account `<key-id>`.
- [ ] Bind one-time GitHub device/OAuth identity verification plus WebAuthn decision to recipient enrollment; preserve local-only labeling until complete.
- [ ] Commit `feat: enroll team state recipients`.

### Task 4: Git ref reader and atomic restore

**Files:** Create `src/intent_engineering/team_state/{git_ref,restore}.py`; modify readiness service; tests `tests/integration/team_state/test_restore.py`.

**Interfaces:** `GitRefReader.fetch_manifest(remote, ref)`, `.read_blob(ref, path)`; `TeamStateRestorer.ensure(runtime, remote_state, now) -> RestoreResult`.

```python
class GitRefReader:
    def fetch_manifest(self, remote: str, ref: str) -> RemoteStateSnapshot: ...
    def read_blob(self, ref: str, path: str) -> bytes: ...

class TeamStateRestorer:
    def ensure(self, runtime: Runtime, remote_state: RemoteStateSnapshot, now: datetime) -> RestoreResult: ...
```

- [ ] RED-test absent ref, unrelated/rewritten history, hostile remote names, subprocess output bounds, invalid manifest/signature/ciphertext/archive, wrong repository, missing key, local unpublished state, interrupted swap, special files, concurrent restore, offline cache, and exact no-op.
- [ ] Use argv-only Git commands with `--end-of-options`, fixed ref `refs/remotes/origin/intent-state`, bounded blobs, no checkout, and one held local transaction for preimage/atomic restore.
- [ ] Keep last verified manifest/ciphertext cache; offline mode may inspect it but cannot publish.
- [ ] Commit `feat: restore verified shared intent state`.

### Task 5: Deterministic publication and temporary worktree lifecycle

**Files:** Create `src/intent_engineering/team_state/publication.py`; tests `tests/integration/team_state/test_publication.py`; modify control-plane Team state view.

**Interfaces:** `PublicationService.preview() -> PublicationPreview`, `.prepare(VerifiedHumanDecision) -> PreparedPublication`.

```python
class PublicationService:
    def preview(self, *, now: datetime) -> PublicationPreview: ...
    def prepare(self, decision: VerifiedHumanDecision, *, now: datetime) -> PreparedPublication: ...
```

- [ ] RED-test held-state drift, recipient/policy drift, stale parent, deterministic plaintext/digest, randomized ciphertext, WebAuthn payload binding, temporary-path safety, branch collisions, push failure, cancellation, cleanup refusal, and no direct state-branch update.
- [ ] Snapshot every canonical target and authority extra once, validate, archive/encrypt/sign, create `mktemp` worktree outside repository, commit only manifest/bundle/signature, push `intent-publication/<digest>`, and always clean owned worktree safely.
- [ ] Return PR metadata rather than opening network inside the core service.
- [ ] Commit `feat: prepare reviewed intent state publications`.

### Task 6: GitHub publication PR adapter and team setup CLI

**Files:** Create `src/intent_engineering/cli/team.py`, `src/intent_engineering/team_state/github.py`; modify `src/intent_engineering/cli/app.py`, GitHub client; tests `tests/e2e/test_cli_team_github.py`, `tests/unit/team_state/test_github.py`.

**Interfaces:** CLI `intent team enable github`; adapter verifies repository, branch, CODEOWNERS/protection status and opens PR through exact GitHub API calls.

```python
class GitHubTeamStateClient:
    async def inspect(self, repository: str) -> GitHubTeamStateStatus: ...
    async def open_publication_pr(self, publication: PreparedPublication) -> PublicationPullRequest: ...
```

- [ ] RED-test preview-first behavior, missing scopes, private/public repository policy, preexisting incompatible branch, identity mismatch, stale preview, changed protection, PR duplication, API pagination/rate errors, secret-free failures, and cancellation cleanup.
- [ ] Implement no-network preview from local/Git metadata, then WebAuthn confirm, key enrollment, publication preparation, branch push, and PR create.
- [ ] Generate `.github/CODEOWNERS` suggestion and workflow changes through the code branch; configure protection only after a second explicit confirmed preview.
- [ ] Commit `feat: enable GitHub intent team state`.

### Task 7: State-branch and code-PR validation workflows

**Files:** Create `.github/workflows/intent-state.yml`; modify `.github/workflows/intent-check.yml`; tests `tests/e2e/test_github_action_workflow.py`, `tests/e2e/test_team_state_journey.py`; update docs.

- [ ] Write RED structural tests for least permissions, pinned actions, state/code branch separation, restore-before-check, no plaintext artifacts, exact check names, concurrency, and no automatic merge/publish.
- [ ] E2E two developers: first publishes baseline, second restores without checkout, two concurrent publications diverge, reviewed reconciliation republishes, code PR passes/fails against shared state.
- [ ] Run team-state/CLI/GitHub/control-plane suites, full offline suite, static gates, help, workflow validation, and diff-check.
- [ ] Commit `feat: share approved intent through GitHub`.
