# Intent State Operational Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the local control plane and GitHub shared-state workflow recoverable under key rotation, employee removal, schema upgrades, offline operation, corruption, and multi-repository use.

**Architecture:** Hardening extends existing public models with explicit migration, rotation, backup, compaction, audit, and broker protocols. Each operation is previewed, WebAuthn-confirmed where authoritative, transactional, bounded, and independently recoverable.

**Tech Stack:** Existing control-plane/team-state stack, Python 3.12, pytest.

**Spec:** `docs/superpowers/specs/2026-08-29-intent-dev-control-plane-design.md`

## Global Constraints

- Milestones 1–3 are complete and their on-disk/wire formats are frozen at schema version 1.
- No migration, rotation, rollback, compaction, or recovery silently loses evidence, authorship, ChangeSets, decisions, or unresolved cases.
- Historical encrypted Git objects are not claimed erased by recipient removal.
- Every destructive administrative operation requires complete preview plus WebAuthn and policy authorization.

---

### Task 1: Backup inventory and verified rollback

**Files:** Create `src/intent_engineering/team_state/recovery.py`; test `tests/integration/team_state/test_recovery.py`; extend Team state UI.

```python
class RecoveryService:
    def list_verified(self) -> tuple[VerifiedBackup, ...]: ...
    def preview_rollback(self, bundle_digest: str) -> RollbackPreview: ...
    def prepare_rollback(self, decision: VerifiedHumanDecision) -> PreparedPublication: ...
```

- [ ] RED-test backup enumeration, complete validation, rollback preview, current-state preservation, downgrade prohibition, missing recipients, corrupt historical bundles, cancellation, and exact audit record.
- [ ] Implement restore-to-temporary plus full validation; rollback creates a new forward publication referencing the selected historical bundle and never rewrites Git history.
- [ ] Commit `feat: recover approved intent state safely`.

### Task 2: Recipient rotation and removal

**Files:** Create `src/intent_engineering/team_state/rotation.py`; test `tests/integration/team_state/test_rotation.py`; extend enrollment UI.

```python
class RotationService:
    def preview(self, recipient_ids: tuple[str, ...]) -> RotationPreview: ...
    def prepare(self, decision: VerifiedHumanDecision) -> PreparedPublication: ...
```

- [ ] RED-test new-key proof, old-key possession, lost-key administrator quorum, actor removal, policy drift, recipient ordering, re-encryption, stale parent, historical-disclosure warning, and no private-key transport.
- [ ] Implement WebAuthn-confirmed recipient-set ChangeSet and next-bundle re-encryption; deletion from local keyring occurs only after durable publication or explicit local-only removal confirmation.
- [ ] Commit `feat: rotate shared intent recipients`.

### Task 3: Schema migration framework

**Files:** Create `src/intent_engineering/team_state/migrations/{__init__,v1}.py`; create `src/intent_engineering/team_state/migrate.py`; test `tests/unit/team_state/test_migrations.py`.

- [ ] RED-test exact source/target versions, deterministic bytes, idempotence, unknown versions, downgrade rejection, semantic digest preservation/change declaration, migration cancellation, and fixture golden files.
- [ ] Define `StateMigration` protocol with `source_version`, `target_version`, and `migrate(snapshot) -> MigrationResult`; every semantic change emits an explicit migration ChangeSet.

```python
class StateMigration(Protocol):
    source_version: int
    target_version: int
    def migrate(self, snapshot: RestoredSnapshot) -> MigrationResult: ...
```
- [ ] Add preview and WebAuthn confirmation for semantic migrations; nonsemantic canonical rewrites still produce a new signed bundle with declared equivalence.
- [ ] Commit `feat: migrate shared intent schemas`.

### Task 4: Bundle compaction and audit export

**Files:** Create `src/intent_engineering/team_state/{compact,audit}.py`; tests `tests/integration/team_state/test_compaction.py`, `test_audit.py`; add UI actions.

```python
def compact(snapshot: RestoredSnapshot) -> CompactionResult: ...
def export_audit(snapshot: RestoredSnapshot, actor: str, format: AuditFormat) -> bytes: ...
```

- [ ] RED-test reachability inventory, unresolved/history preservation, deterministic compact bundle, retained predecessor proofs, size limits, no secret/plaintext report, ACL projections, and cancellation rollback.
- [ ] Compaction creates a new bundle that retains all canonical semantic records and a digest inventory of superseded transport bundles; it never rewrites the branch.
- [ ] Audit export is generated/noncanonical, actor-ACL-filtered, and supports JSON/Markdown with evidence bodies omitted by default.
- [ ] Commit `feat: compact and audit shared intent state`.

### Task 5: Multi-repository broker and diagnostics

**Files:** Create `src/intent_engineering/control_plane/broker.py`; modify `src/intent_engineering/cli/dev.py`; create `src/intent_engineering/cli/doctor_dev.py`; tests `tests/integration/control_plane/test_broker.py`, `tests/e2e/test_cli_dev_doctor.py`.

```python
class ControlPlaneBroker:
    def ensure(self, repository: Path) -> BrokerEntry: ...
    def status(self, repository: Path) -> BrokerStatus: ...
    def stop(self, repository: Path) -> None: ...
```

- [ ] RED-test repository isolation, port/process reuse, PID reuse, stale sockets, cross-repository request, concurrent start/stop, per-repo CSRF/origin, offline mixed status, graceful shutdown, and crash recovery.
- [ ] Implement a user-only broker registry containing only repository digest, PID instance token, origin, and freshness; never store project content or authority credentials in the registry.
- [ ] Add `intent doctor dev` diagnostics for installation, browser/WebAuthn support, GitHub reachability, keyring, state freshness, workflow/protection status, and safe cleanup previews.
- [ ] Commit `feat: harden multi-repository intent services`.

### Task 6: Adversarial release matrix and documentation

**Files:** Create `tests/e2e/test_intent_team_release.py`; modify README/adoption/MCP/GitHub docs; modify framework graph through the public ChangeSet path and persist the generated validated YAML.

- [ ] E2E onboarding, automatic ensure, signed clarification, publication, second-developer restore, code PR required check, key rotation, employee removal, offline work, conflicting publication, migration, rollback, and audit export in one deterministic two-repository harness.
- [ ] Scan every regular project/artifact file, output, log, fixed error, DOM projection, and repository traceback local for credential, plaintext, key, challenge, and OAuth sentinels.
- [ ] Run all milestone suites, repository-wide full tests with warnings as errors, Ruff, scoped format, mypy, plugin/skill/workflow validators, CLI help, framework graph validation, packaging wheel inspection, and diff checks.
- [ ] Document exact customer commands, offline limitations, historical recipient disclosure, branch protection, recovery, and uninstall behavior.
- [ ] Commit `feat: complete intent developer control plane hardening`.
