# Intent Developer Control Plane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `intent dev`, a repository-bound local browser UI, and WebAuthn-backed authoritative onboarding, clarification, proposal, reconciliation, and approval actions.

**Architecture:** A small Starlette/uvicorn control plane reuses the existing Runtime, stores bounded credential/challenge records through descriptor-safe stores, and delegates semantic work to existing onboarding, clarification, confirmation, reconciliation, and write services. WebAuthn establishes user presence for canonical decision envelopes; HTTP handlers never implement graph semantics directly.

**Tech Stack:** Python 3.12, Typer, Starlette, uvicorn, `webauthn>=3,<4`, Pydantic, existing secure storage/transactions, vanilla HTML/CSS/JavaScript, pytest.

**Spec:** `docs/superpowers/specs/2026-08-29-intent-dev-control-plane-design.md`

## Global Constraints

- Preserve local-first operation, immutable evidence, exact authorship, stable IDs, ChangeSet history, and explicit reconciliation.
- Bind every service, challenge, credential, and decision to one canonical repository and project identity.
- Listen on loopback only; reject non-canonical Host, Origin, content type, JSON types, and oversized bodies before behavior.
- WebAuthn user verification is required for every authoritative action; localhost alone grants no authority.
- Agent-submitted hook evidence remains `agent:codex` inferred context and can never satisfy human authority.
- No HTTP handler directly edits graph, evidence, history, case, proposal, approval, or receipt files.
- Preserve all existing granular CLI and MCP behavior.
- Tests are deterministic, fixed-time, offline, and written before production behavior.

---

### Task 1: Control-plane models and dependency boundary

**Files:**
- Create: `src/intent_engineering/control_plane/__init__.py`
- Create: `src/intent_engineering/control_plane/models.py`
- Modify: `pyproject.toml`
- Test: `tests/unit/control_plane/test_models.py`

**Interfaces:**
- Produces: `DecisionAction`, `DecisionSubject`, `HumanDecisionPayload`, `CredentialRecord`, `ChallengeRecord`, `DevStatus`, and `AttentionRoute`.
- `HumanDecisionPayload.canonical_bytes() -> bytes` is the sole signed decision representation consumed by later tasks.

- [ ] **Step 1: Write strict model tests**

```python
def test_decision_payload_is_project_repository_version_and_digest_bound() -> None:
    payload = HumanDecisionPayload(
        schema_version=1,
        project_id="project:alpha",
        repository_id="repo:sha256:" + "a" * 64,
        actor="local:asha",
        action=DecisionAction.CONFIRM_PROPOSAL,
        graph_version=7,
        parent_bundle_digest="sha256:" + "b" * 64,
        subject=DecisionSubject(kind="proposal", id="proposal:" + "c" * 64),
        subject_digest="sha256:" + "d" * 64,
        selected_node_ids=("requirement:export",),
        result_digest="sha256:" + "e" * 64,
        challenge="challenge:" + "f" * 64,
        issued_at=datetime(2026, 8, 30, tzinfo=UTC),
        expires_at=datetime(2026, 8, 30, 0, 5, tzinfo=UTC),
    )
    assert payload == HumanDecisionPayload.model_validate_json(payload.model_dump_json())
    assert payload.canonical_bytes().endswith(b"\n")
```

Also reject subclasses, duplicate/sorted-ID violations, non-`Z` timestamps, expiry over five minutes, empty selections for selection-bound actions, unknown actions, oversized IDs, and extra fields.

- [ ] **Step 2: Run the model test and witness RED**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/unit/control_plane/test_models.py -q -W error`

Expected: collection failure for missing `intent_engineering.control_plane.models`.

- [ ] **Step 3: Add direct dependencies and strict models**

Add direct runtime dependencies:

```toml
"starlette>=1.0,<2",
"uvicorn>=0.31,<1",
"webauthn>=3,<4",
```

Implement frozen Pydantic models, bounded enums, canonical JSON serialization with `sort_keys=True`, compact separators, UTF-8 byte limits, and canonical UTC `Z` timestamps.

- [ ] **Step 4: Run focused and model compatibility tests**

Run: `... pytest tests/unit/control_plane/test_models.py tests/unit/intent_workflow/test_models.py -q -W error`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml src/intent_engineering/control_plane tests/unit/control_plane/test_models.py
git commit -m "feat: define trusted control-plane contracts"
```

### Task 2: Descriptor-safe credential and challenge stores

**Files:**
- Create: `src/intent_engineering/control_plane/webauthn_store.py`
- Test: `tests/unit/control_plane/test_webauthn_store.py`
- Modify: `src/intent_engineering/cli/runtime.py`
- Modify: `tests/unit/cli/test_runtime_paths.py`

**Interfaces:**
- Produces: `WebAuthnCredentialStore.list()`, `.put(record)`, `WebAuthnChallengeStore.issue(record)`, `.consume(challenge_id, now)`.
- Runtime gains `webauthn_credentials`, `webauthn_challenges`, and transaction targets `webauthn_credentials`/`webauthn_challenges`.

- [ ] **Step 1: Write canonical ledger, replay, corruption, FIFO, symlink, hardlink, concurrency, cancellation, and traceback-secrecy tests**

```python
def test_challenge_is_consumed_exactly_once(tmp_path: Path) -> None:
    store = challenge_store(tmp_path)
    record = challenge_record()
    assert store.issue(record)
    assert store.consume(record.id, record.issued_at) == record
    with pytest.raises(ValueError, match="challenge unavailable"):
        store.consume(record.id, record.issued_at)
```

Use subprocess watchdogs for special files and multiprocessing `spawn` for exact one-winner tests.

- [ ] **Step 2: Run RED**

Run: `... pytest tests/unit/control_plane/test_webauthn_store.py -q -W error`

Expected: missing module failure.

- [ ] **Step 3: Implement framed canonical JSONL stores using existing `SecureFile`, durable append, and transaction patterns**

Reject noncanonical typed frames by comparing the validated model's canonical bytes to durable bytes. Challenges are append-only issue/consume events; latest valid transition determines availability. Never rewrite a nonempty ledger.

- [ ] **Step 4: Bind stores to project initialization and runtime recovery**

Add `.intent/approvals/webauthn-credentials.jsonl` and `.intent/approvals/webauthn-challenges.jsonl` as exact transaction targets, preserving legacy target-set recovery.

- [ ] **Step 5: Run storage/runtime compatibility**

Run: `... pytest tests/unit/control_plane/test_webauthn_store.py tests/unit/storage/test_transaction.py tests/unit/cli/test_runtime_paths.py tests/integration/test_startup_transaction_recovery.py -q -W error`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/intent_engineering/control_plane/webauthn_store.py src/intent_engineering/cli/runtime.py tests/unit/control_plane/test_webauthn_store.py tests/unit/cli/test_runtime_paths.py
git commit -m "feat: persist bounded WebAuthn authority state"
```

### Task 3: WebAuthn enrollment and decision verification service

**Files:**
- Create: `src/intent_engineering/control_plane/webauthn_service.py`
- Test: `tests/unit/control_plane/test_webauthn_service.py`
- Modify: `src/intent_engineering/control_plane/__init__.py`

**Interfaces:**
- Produces: `WebAuthnService.registration_options(actor, origin, now)`, `.register(response, actor, origin, now)`, `.authentication_options(payload, origin, now)`, `.verify(response, payload, origin, now) -> VerifiedHumanDecision`.
- Consumes Task 1 models and Task 2 stores.

- [ ] **Step 1: Add a deterministic authenticator port and tests**

Define `WebAuthnVerifier` protocol around the four `webauthn` package calls so tests use a deterministic fake without emulating platform cryptography.

```python
class WebAuthnVerifier(Protocol):
    def registration_options(self, request: RegistrationRequest) -> bytes: ...
    def verify_registration(self, response: bytes, request: RegistrationRequest) -> VerifiedRegistration: ...
    def authentication_options(self, request: AuthenticationRequest) -> bytes: ...
    def verify_authentication(self, response: bytes, request: AuthenticationRequest) -> VerifiedAuthentication: ...
```

Test wrong origin/RP ID, missing user verification, cloned-sign-counter rollback, challenge replay, expiry, actor/repository/version/payload substitution, cancellation identity, and secret-free errors/tracebacks.

- [ ] **Step 2: Run RED**

Run: `... pytest tests/unit/control_plane/test_webauthn_service.py -q -W error`

- [ ] **Step 3: Implement the service**

Use RP ID `localhost`, exact expected origin selected by the service, `user_verification="required"`, five-minute challenges, and atomic challenge consumption plus credential counter update. Return fixed `HumanAuthorityError("human authority unavailable")` for public failures.

- [ ] **Step 4: Run focused security matrix**

Run: `... pytest tests/unit/control_plane/test_webauthn_service.py tests/unit/control_plane/test_webauthn_store.py -q -W error`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/intent_engineering/control_plane tests/unit/control_plane
git commit -m "feat: verify WebAuthn human decisions"
```

### Task 4: Repository-bound control-plane application service

**Files:**
- Create: `src/intent_engineering/control_plane/service.py`
- Test: `tests/integration/control_plane/test_service.py`
- Modify: `src/intent_engineering/intent_workflow/clarification.py`
- Modify: `src/intent_engineering/cli/intent_workflow.py`

**Interfaces:**
- Produces: `ControlPlaneService.status()`, `.onboard_preview(prd)`, `.proposal_preview(id)`, `.answer_preview(session_id, question_id, answer)`, `.decision_options(payload)`, `.apply_decision(assertion, payload)`.
- Consumes one held Runtime and Task 3 `VerifiedHumanDecision`; delegates mutations to existing production services.

- [ ] **Step 1: Write real-store integration tests for all authoritative actions**

Cover onboarding activation, clarification answer evidence capture, clarified proposal confirmation, conflict resolution, and guarded write approval. Every test asserts exact preview digest, WebAuthn payload binding, one graph/history transition, actor attribution, evidence provenance, replay result, and no mutation for changed config/policy/graph/evidence/case/proposal/credential state.

- [ ] **Step 2: Run RED**

Run: `... pytest tests/integration/control_plane/test_service.py -q -W error`

- [ ] **Step 3: Add authenticated service entry points without weakening existing public MCP failure behavior**

The service reconstructs previews from held descriptors, verifies the signed payload, then invokes the existing coordinator/confirmation/resolution/approval operation inside the same authority-bound transaction. Add narrowly typed internal methods where current CLI-only confirmation is coupled to a terminal.

- [ ] **Step 4: Test cancellation and crash rollback at every transaction stage**

Inject `KeyboardInterrupt`/`CancelledError` before decision append, graph write, history write, case transition, and receipt append. Assert exact signal identity, byte-identical rollback, fixed public errors, and no private answer/assertion in repository traceback locals.

- [ ] **Step 5: Run workflow compatibility**

Run: `... pytest tests/integration/control_plane/test_service.py tests/integration/intent_workflow/test_bootstrap.py tests/integration/intent_workflow/test_clarification.py tests/integration/intent_workflow/test_proposal_governance.py tests/e2e/test_cli_write_approval.py -q -W error`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/intent_engineering/control_plane/service.py src/intent_engineering/intent_workflow/clarification.py src/intent_engineering/cli/intent_workflow.py tests/integration/control_plane/test_service.py
git commit -m "feat: apply authenticated human workflow decisions"
```

### Task 5: Strict loopback HTTP API

**Files:**
- Create: `src/intent_engineering/control_plane/web.py`
- Create: `src/intent_engineering/control_plane/http_models.py`
- Test: `tests/contract/control_plane/test_http_api.py`

**Interfaces:**
- Produces: `build_control_plane_app(service, *, origin, csrf_secret) -> Starlette`.
- Routes: `/api/v1/status`, `/api/v1/inbox`, `/api/v1/proposals/{id}`, `/api/v1/webauthn/register/options`, `/api/v1/webauthn/register/verify`, `/api/v1/decisions/options`, `/api/v1/decisions/verify`.

- [ ] **Step 1: Write strict raw-boundary tests before handlers exist**

Reject non-loopback Host/Origin, missing CSRF, cookies without `SameSite=Strict`, methods other than the exact route method, bodies over 256 KiB, non-UTF-8, duplicate JSON keys, non-exact container/scalar subclasses, cycles/aliases in direct calls, extra fields, noncanonical timestamps, and secret-bearing errors.

- [ ] **Step 2: Run RED**

Run: `... pytest tests/contract/control_plane/test_http_api.py -q -W error`

- [ ] **Step 3: Implement strict middleware and detached response models**

Bind `127.0.0.1`, exact random port, origin `http://localhost:<port>`, per-process CSRF cookie/header, `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`, restrictive CSP, and fixed JSON errors. Read-only projections must apply existing ACL/principal filtering before serialization.

- [ ] **Step 4: Wire only `ControlPlaneService` methods**

Handlers validate raw bytes, build strict request models, call one service method, serialize detached outputs, and scrub request/response/body/model locals in `finally` including cancellation paths.

- [ ] **Step 5: Run API and service contracts**

Run: `... pytest tests/contract/control_plane/test_http_api.py tests/integration/control_plane/test_service.py -q -W error`

- [ ] **Step 6: Commit**

```bash
git add src/intent_engineering/control_plane/web.py src/intent_engineering/control_plane/http_models.py tests/contract/control_plane/test_http_api.py
git commit -m "feat: expose strict local review API"
```

### Task 6: Browser UI and WebAuthn ceremonies

**Files:**
- Create: `src/intent_engineering/control_plane/assets/index.html`
- Create: `src/intent_engineering/control_plane/assets/app.js`
- Create: `src/intent_engineering/control_plane/assets/styles.css`
- Modify: `pyproject.toml`
- Test: `tests/contract/control_plane/test_web_assets.py`
- Test: `tests/e2e/test_intent_dev_web.py`

**Interfaces:**
- The browser consumes only Task 5 `/api/v1` routes.
- The build contains no Node toolchain; package static assets with the Python wheel.

- [ ] **Step 1: Write asset and browser-flow tests**

Assert five navigable views, escaped text rendering via `textContent`, no `innerHTML` for untrusted data, WebAuthn `navigator.credentials.create/get` calls, complete proposal/evidence preview, explicit selected nodes, cancel/no-op, expired challenge handling, and no credential/raw evidence in URL, localStorage, console, or DOM after completion.

- [ ] **Step 2: Run RED**

Run: `... pytest tests/contract/control_plane/test_web_assets.py tests/e2e/test_intent_dev_web.py -q -W error`

- [ ] **Step 3: Implement accessible static UI**

Use semantic HTML, keyboard navigation, visible focus, status announcements, explicit destructive-action labels, and a single `fetchJson` boundary. Render Home, Onboarding, Inbox, Proposal review, and Team state from detached API projections.

- [ ] **Step 4: Implement browser WebAuthn conversion helpers**

Convert base64url challenge/credential fields to `ArrayBuffer`, require user verification, send only the credential response to verify routes, and clear buffers/references in `finally`.

- [ ] **Step 5: Run UI/API E2E**

Use a deterministic fake browser authenticator at the HTTP boundary and one separately marked manual platform-authenticator release probe. Default tests remain offline.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml src/intent_engineering/control_plane/assets tests/contract/control_plane/test_web_assets.py tests/e2e/test_intent_dev_web.py
git commit -m "feat: add local intent review UI"
```

### Task 7: `intent dev` process lifecycle and release proof

**Files:**
- Create: `src/intent_engineering/cli/dev.py`
- Modify: `src/intent_engineering/cli/app.py`
- Modify: `src/intent_engineering/control_plane/__init__.py`
- Test: `tests/e2e/test_cli_intent_dev.py`
- Modify: `README.md`
- Modify: `docs/intent-aware-agent.md`

**Interfaces:**
- Produces CLI: `intent dev [--project PATH] [--prd PATH] [--no-open] [--offline] [--status]`.
- Uses one PID/metadata file under `.intent/cache/control-plane.json` with canonical repository identity and loopback origin; stale process metadata is never trusted without OS/process verification.

- [ ] **Step 1: Write lifecycle RED tests**

Cover uninitialized `--prd`, ready startup, second invocation reuse, stale PID, foreign process PID reuse, symlink/FIFO metadata, concurrent starts, browser-open failure, SIGINT/CancelledError cleanup, cross-repository denial, and fixed stdout/stderr without secrets.

- [ ] **Step 2: Run RED**

Run: `... pytest tests/e2e/test_cli_intent_dev.py -q -W error`

- [ ] **Step 3: Implement bounded lifecycle**

Acquire a descriptor-safe startup lock, load one Runtime, choose loopback port, start uvicorn, atomically publish metadata, optionally open the browser with an unguessable fragment bootstrap value, and remove metadata only when it still names the owned process instance.

- [ ] **Step 4: Prove the complete Milestone 1 journey**

The E2E starts without `.intent`, previews PRD onboarding, registers a fake WebAuthn credential, activates baseline, opens/answers clarification, confirms exact proposal, resolves a conflict, and rejects agent-only/no-signature/replay/drift attempts. Assert one Runtime, exact authorship, ChangeSet/history, and zero secrets across files/output/logs/tracebacks.

- [ ] **Step 5: Update docs and help**

Make `intent dev --prd docs/PRD.md` the primary local flow while retaining granular commands under advanced diagnostics. Document that team identity enrollment and shared state arrive in Milestone 3.

- [ ] **Step 6: Run final Milestone 1 gates**

Run focused control-plane tests, existing onboarding/clarification/reconciliation/write suites, `ruff check`, `ruff format --check` on changed paths, `mypy src/intent_engineering`, full offline pytest with `-W error`, all affected help commands, and `git diff --check`.

- [ ] **Step 7: Commit**

```bash
git add src/intent_engineering/cli/dev.py src/intent_engineering/cli/app.py src/intent_engineering/control_plane tests/e2e/test_cli_intent_dev.py README.md docs/intent-aware-agent.md
git commit -m "feat: launch trusted local intent control plane"
```

