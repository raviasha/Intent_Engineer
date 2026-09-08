# CI Recipient Provisioning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this approved follow-up task-by-task.

**Goal:** Real setup bundles are decryptable by an independently provisioned self-hosted CI recipient, without developer private-key export.

**Architecture:** Add a distinct machine public-recipient model, a runner-local OS-keyring provisioning/trust adapter, and a public-descriptor import into existing setup authority. Protected validation consumes only protected runner-local public configuration plus its independent keyring secret.

**Tech Stack:** Python, Pydantic, cryptography X25519, keyring, existing SecureFile/WebAuthn/publication and protected CI adapters.

**Spec:** `docs/superpowers/specs/2026-09-09-ci-recipient-provisioning-design.md`

## Global Constraints

- Developer private keys are never exported.
- No recipient private key is stored in GitHub, `.intent`, command arguments, logs, or workflow secrets.
- Only human recipients may satisfy a publication decision credential.
- Generated production workflow targets dedicated `[self-hosted, intent-state]` runners.
- Do not commit or push this follow-up.

### Task 1: Machine encryption recipient

Files: `team_state/models.py`, `crypto.py`, `publication.py`; new unit/integration tests.
Interface: `CiRecipientRecord` with `kind='ci'`, project/repository/runner binding, key ID and X25519 public key; `EncryptionRecipient` union and `validate_encryption_recipient(value)`.

- [ ] Write and run RED tests: `encrypt_bundle(plaintext, (human, ci), aad)` must decrypt for both independent keys; CI cannot match `VerifiedHumanDecision`; altered machine binding fails.
- [ ] Implement strict distinct model and union revalidation in encryption/publication; retain human-only credential filter.
- [ ] Run focused crypto/publication tests, inspect error/cancellation frames and refactor only while green.

### Task 2: Runner keyring provisioning and public trust

Files: new `team_state/ci.py`, `cli/team_ci.py`, tests `test_ci_recipient.py`.
Interfaces: `provision_preview(project_id, repository_id, runner_id)`, `CiKeyStore(...).provision() -> CiRecipientRecord`, `CiTrustProvider(public_config_path).load()`, strict public `CiTrustConfig`.

- [ ] Write/run RED: unconfirmed CLI never writes a key; confirmed provision emits public descriptor only; repeat reuses exact key; unavailable/foreign key/config is rejected without sensitive diagnostics.
- [ ] Implement bounded canonical public files, secure descriptor reads, stable keyring account+owned lock and fixed cancellation-preserving failure boundaries. No private export API.
- [ ] Run unit/CLI tests and static gates.

### Task 3: Setup and protected validation

Files: `cli/team.py`, `team_state/setup.py`, `integrations/protected_ci.py`, browser/error handling if needed; existing bridge tests; public operator documentation.

- [ ] Write/run RED: `--ci-recipient` descriptor enters preview/decision; missing descriptor blocks setup; exact production bundle validates on CI with no developer key; changed CI descriptor invalidates review.
- [ ] Add optional descriptor data to the preview schema for compatibility, but require it for production confirmation/publication. Compose sorted human+CI recipients and expose reviewed public `CiTrustConfig` in publication preview.
- [ ] Generated workflow uses protected self-hosted runner and public config path; protected validation must reject env-only secret JSON and missing key. Existing environment-trust APIs remain explicitly legacy/test only.
- [ ] Run focused setup/CI/crypto/publication suites, Ruff/format, strict source mypy and diff checks. Report exact counts and deployment tradeoffs without commit/push.
