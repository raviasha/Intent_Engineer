# Intent Developer Control Plane Design

**Date:** 2026-08-29  
**Status:** Approved design  
**Scope:** Local developer control plane, trusted human review, consolidated commands, plugin presets, and GitHub-backed team intent state

## 1. Purpose

Intent Engineering already provides the semantic graph, immutable evidence, prompt classification, reconciliation, scheduled assurance, and reviewed external-write foundations. The next release turns those capabilities into a coherent developer experience that normally requires no Intent-specific command during aligned feature work.

The design introduces:

- a repository-bound `intent dev` local control plane and browser UI;
- WebAuthn-backed human answers and approvals;
- a safe plugin preset that automatically ensures readiness on the first prompt;
- a consolidated `intent check` command for local and CI assurance;
- GitHub distribution of signed, encrypted approved intent state through a protected `intent-state` branch;
- a required GitHub status check that governs merges into `main`.

The design preserves local-first operation. GitHub transports and reviews encrypted state; it does not become the semantic authority and is not required for offline inspection of the last verified baseline.

## 2. Goals

1. Reduce the normal developer workflow to `intent dev`, or zero commands when the approved plugin preset is enabled.
2. Let a human complete onboarding, clarification, proposal review, conflict resolution, publication, and provider approval in one local browser UI.
3. Cryptographically distinguish human-authorized actions from agent-triggered advisory input.
4. Distribute the approved team baseline without requiring developers to check out or merge a second branch.
5. Make code pull requests fail a required GitHub check when blocking intent drift remains unresolved.
6. Preserve all existing granular CLI and MCP contracts for diagnostics and automation compatibility.
7. Keep every semantic mutation ChangeSet-backed, evidence-grounded, attributed, reviewable, and replay-safe.

## 3. Non-goals

- Hosted multi-tenant storage or a managed SaaS control plane.
- Treating GitHub, a requirement document, or a coding agent as universal truth.
- Automatic approval of intent, requirements, conflicts, publications, or provider writes.
- Complete mandatory interception of every coding-agent mutation path.
- Storing plaintext evidence, credentials, private keys, or decrypted state in GitHub.
- Replacing the existing graph, evidence, history, case, proposal, approval, or transaction models.
- Requiring a developer to check out `intent-state` alongside a code branch.

## 4. Chosen architecture

### 4.1 Local control plane

`intent dev` starts or reuses one repository-bound local service. It owns:

- repository and onboarding detection;
- retrieval and verification of shared approved state;
- atomic restoration of the ignored `.intent` cache;
- lightweight readiness, validation, and drift checks;
- the local browser UI;
- WebAuthn enrollment, challenges, and decision verification;
- clarification, proposal, case, and publication views;
- bounded background source capture;
- Git and passing-test evidence observation;
- coordination with the existing local MCP and workflow services.

The service binds an exact canonical repository identity and refuses cross-repository reuse. A single desktop broker may host several isolated repository services later, but the first implementation is one process per repository.

### 4.2 Local web UI

The UI exposes five primary views:

1. **Home** — readiness, current graph version, shared-state freshness, source health, and blocking cases.
2. **Onboarding** — PRD selection, immutable capture, extracted candidate intent, confidence, provenance, and baseline review.
3. **Inbox** — clarification questions, conflicts, drift cases, and state-lineage divergence.
4. **Proposal review** — exact graph diff, evidence sides, affected code/tests, selected nodes, and resulting digest.
5. **Team state** — local/shared lineage, encryption recipients, pending publication PR, and verification status.

The UI opens automatically only when attention is required or the user requests it. Routine aligned work remains quiet.

### 4.3 Plugin preset

The developer preset makes the first repository prompt invoke a safe readiness operation equivalent to:

```bash
intent ensure --preset developer
```

The operation may fetch, verify, restore, validate, start the local service, and return readiness. It is idempotent and may be invoked by an untrusted agent because it grants no human attribution or mutation authority.

The plugin continues to classify prompts as:

- `no_semantic_impact`;
- `aligned`;
- `new_or_ambiguous`;
- `conflicting`.

The plugin may open the correct UI page. It may not answer questions, sign decisions, activate graph changes, publish state, or approve provider writes.

### 4.4 GitHub team-state transport

The protected orphan-style `intent-state` branch contains only versioned release bundles and manifests:

```text
manifest.json
bundles/<graph-version>-<bundle-digest>.intent
signatures/<graph-version>-<bundle-digest>.json
```

Developers remain on their normal code branches. `intent ensure` fetches the ref without checking it out and reads objects directly from the remote-tracking ref. Publishing uses an internal temporary worktree and a pull request, then cleans up that worktree.

GitHub provides transport, pull-request review, CODEOWNERS, branch protection, and a second audit layer. Intent Engineering remains responsible for semantic provenance, authorship, ChangeSet history, graph validity, and state lineage.

## 5. Consolidated command surface

### 5.1 `intent dev`

The everyday entry point:

```bash
intent dev
intent dev --prd docs/PRD.md
intent dev --no-open
intent dev --offline
intent dev --status
```

It detects state, restores the latest verified baseline, starts the service, performs lightweight checks, and opens the UI only when needed. In an uninitialized repository, `--prd` begins consent-gated guided onboarding in the UI.

### 5.2 `intent ensure`

A bounded, machine-oriented readiness command used by the plugin and other clients. It returns a strict status such as:

- `ready`;
- `onboarding_required`;
- `human_attention_required`;
- `offline_stale`;
- `shared_state_unavailable`;
- `shared_state_invalid`;
- `upgrade_required`.

It never performs an authoritative human action.

### 5.3 `intent check`

One command replaces the ordinary status/validate/sync/drift sequence:

```bash
intent check
intent check --ci --require-review
```

It executes:

1. shared-state verification/restoration;
2. configured source capture;
3. canonical validation;
4. deterministic assurance;
5. drift rendering;
6. stable exit-code selection.

Existing commands remain public for inspection and compatibility.

### 5.4 `intent team enable github`

The one-time team setup command previews, then after WebAuthn confirmation:

- validates GitHub repository identity and permissions;
- creates or validates `intent-state`;
- establishes signing and encryption configuration;
- generates the workflow and CODEOWNERS template;
- checks or configures branch protection when explicitly authorized;
- publishes the first baseline through a pull request.

It never pushes directly to the protected state branch.

## 6. Human trust and WebAuthn

A localhost page alone is not a human-authentication boundary because the coding agent may access the same files and ports. Authoritative operations therefore require a WebAuthn assertion with user presence.

The first implementation supports platform authenticators such as Touch ID and Windows Hello. A signed action binds:

- project ID;
- canonical repository identity;
- action type;
- actor identity and registered credential ID;
- current graph version and state-bundle parent digest;
- proposal, case, plan, or answer digest;
- selected node IDs where applicable;
- resulting ChangeSet or bundle digest;
- server challenge nonce;
- canonical timestamp and expiry.

WebAuthn is required for:

- baseline confirmation;
- authoritative clarification answers;
- clarified proposal confirmation;
- conflict resolution;
- shared-state publication;
- external-provider approval.

Drafting, reading, capture, validation, and drift detection require no signature. A signature cannot be replayed across projects, repositories, graph versions, action types, or payloads.

Evidence authors, WebAuthn decision signers, and GitHub reviewers remain distinct identities.

### 6.1 Enrollment and actor identity

WebAuthn proves user presence and possession of a credential; it does not by itself establish which project actor owns that credential. Team-mode enrollment therefore uses a one-time GitHub device/OAuth identity verification, followed by WebAuthn credential creation. The enrollment record binds the verified GitHub account, configured project actor, credential public key, repository identity, and project ID. Project policy and the current approved recipient/actor set determine which roles that identity may exercise.

The coding agent may trigger the enrollment page but cannot complete GitHub identity verification or a user-verifying WebAuthn ceremony. Changing an actor mapping, replacing a credential, or enrolling a privileged role is itself an authoritative reviewed team-state change. Local-only mode may register a device credential against the displayed configured local actor, but must label the identity as local-only and cannot publish team state until GitHub identity enrollment is complete.

## 7. Shared bundle and key model

### 7.1 Bundle contents

An encrypted bundle contains the approved canonical state needed to restore a project:

- graph;
- evidence ledger;
- semantic ChangeSet history;
- reconciliation cases and transitions;
- clarification and proposal records;
- approval records;
- source-role and non-secret connector configuration;
- publication lineage.

Credentials and private keys are excluded.

### 7.2 Readable manifest

The bounded readable manifest contains only:

- schema version;
- project and repository identities;
- graph version;
- parent bundle digest;
- bundle digest and byte size;
- encryption algorithm;
- recipient public-key IDs;
- required decision-signature IDs;
- canonical creation time.

### 7.3 Encryption and signing

Each developer has an X25519 recipient key stored in the operating-system keychain. Public recipient keys are reviewed team state; private keys never enter GitHub or `.intent`. Bundle publication encrypts once for the approved recipient set and signs the canonical manifest and ciphertext digest.

Recipient addition, removal, and key rotation are authoritative team-state changes. Removing a developer causes the next bundle to exclude that recipient; historical Git objects remain encrypted to their historical recipient set and must be governed by repository-access policy.

## 8. Synchronization and publication

### 8.1 Restore

`intent ensure`:

1. fetches `origin/intent-state` without checkout;
2. bounds and parses the manifest;
3. validates project, repository, schema, size, and lineage;
4. verifies signatures before decryption;
5. decrypts into a descriptor-safe temporary directory;
6. validates the complete restored state;
7. atomically swaps the ignored local cache;
8. scrubs and removes temporary plaintext.

If remote is newer and local has no approved unpublished state, restoration advances automatically. Equal state is a byte-noop.

### 8.2 Publish

After a WebAuthn-confirmed local decision, the UI may prepare publication:

1. snapshot and validate all canonical state in one transaction;
2. build a deterministic encrypted bundle;
3. bind the publication signature to the parent and resulting digests;
4. create a temporary internal worktree;
5. add the bundle, signature, and manifest;
6. push a publication branch;
7. open a pull request against `intent-state`;
8. remove the temporary worktree.

The PR check independently reconstructs and validates the publication. Merge makes it the shared baseline.

### 8.3 Divergence

If local approved state and remote state descend from different parents, the system never chooses last-write-wins and never mechanically splices append-only ledgers. It opens a lineage reconciliation view containing both manifests, decision histories, and graph effects. The resulting reviewed ChangeSet publishes from the current remote parent.

## 9. Required GitHub check

The required code-PR check runs:

```bash
intent check --ci --require-review
```

It restores the approved bundle, captures the PR branch's Git and test evidence, validates the graph, runs deterministic assurance, and publishes a bounded drift report. It fails when:

- no approved baseline can be restored;
- the state bundle or signatures are invalid;
- an upgrade is required;
- blocking reconciliation cases affect the PR;
- required test evidence is missing or failing;
- code/test changes materially conflict with approved intent;
- the branch attempts to modify protected state outside the publication workflow.

It does not initialize an empty graph, approve a proposal, publish state, resolve a case, or perform a provider write.

Project configuration may name reviewed test commands or accepted machine-readable result files. `intent check` never executes an agent-supplied command. In CI, the workflow runs the repository's reviewed test step, captures its immutable result artifact, and passes that artifact to `intent check`; absent, malformed, stale, or failing results cannot satisfy test evidence.

## 10. Post-task evidence

The local service observes Git HEAD and bounded file changes and can ingest test results produced by `intent check`. Observation creates evidence only. It does not assert that a task is complete.

A completion claim still requires an authenticated human act or a future supported host whose exact repository, task, graph, paths, commit, and test evidence are transaction-bound. This preserves the current separation between implementation evidence and design truth.

## 11. Automation policy

Automatic operations:

- fetch and inspect team state;
- signature and lineage verification;
- atomic restoration of verified state;
- source capture;
- prompt classification;
- graph/evidence validation;
- deterministic drift detection;
- report generation;
- service startup and reuse;
- opening the relevant UI route.

Never automatic:

- human authorship attribution;
- clarification answers;
- proposal confirmation;
- conflict resolution;
- state publication;
- GitHub protection changes without explicit authorization;
- external-provider mutation or approval;
- overwriting divergent state.

## 12. Failure handling

- **GitHub unavailable:** use the last verified bundle, mark it stale, allow bounded offline inspection, and prohibit publication.
- **No cached baseline:** return `shared_state_unavailable`; do not create graph version zero.
- **Invalid signature or lineage:** retain the previous verified state and block governed work.
- **Missing decryption key:** request enrollment; never fall back to plaintext.
- **Unknown schema:** retain current state and return `upgrade_required`.
- **Interrupted download, decrypt, or disk write:** leave the previous cache byte-identical.
- **Remote history rewrite:** fail closed and require an administrator review.
- **Concurrent local startup:** converge on one service and one verified cache through bounded locks.
- **Concurrent publications:** reject stale-parent publication and open lineage reconciliation.
- **WebAuthn cancellation or timeout:** preserve the exact cancellation result and make no semantic change.
- **Plugin disabled:** ordinary coding remains available; the required GitHub check still governs merge.

## 13. Compatibility and migration

- All existing granular CLI commands remain supported.
- Existing `.intent` state is migrated only after full validation and an explicit backup.
- Current proposal, confirmation, reconciliation, write-preview, approval, and execution services remain the semantic implementation behind the UI.
- Existing MCP read tools remain available. Human-authority MCP calls remain fail-closed unless backed by a verified WebAuthn decision envelope.
- The existing advisory plugin remains non-mandatory and token-free.
- Teams may use local-only mode without GitHub shared state; `intent dev` then reports `local_only` and publication features remain unavailable.

## 14. Delivery milestones

### Milestone 1 — Local control plane and trusted review UI

Deliver `intent dev`, the local service and UI, WebAuthn enrollment, onboarding, authoritative clarification answers, proposal confirmation, and conflict review. A developer can go from an uninitialized repository to an approved baseline and complete a new-requirement clarification without composing granular commands.

### Milestone 2 — Consolidated checks and plugin preset

Deliver `intent ensure`, `intent check`, the developer preset, automatic first-prompt readiness, bounded capture, automatic attention routing, Git observation, and test-result ingestion. An onboarded developer normally runs no Intent command during aligned work.

### Milestone 3 — GitHub shared state

Deliver `intent team enable github`, encrypted/signed bundles, recipient enrollment, automatic restore, publication PRs, divergent-lineage review, and the required GitHub check. A new developer receives the team baseline without checking out another branch.

### Milestone 4 — Operational hardening

Deliver key rotation/removal, backup and rollback, schema migration, bundle compaction, audit export, multi-repository service management, offline recovery, installation diagnostics, and measured bounds.

## 15. Test strategy

All production behavior is tests-first and offline by default.

- Unit tests cover canonical models, challenge binding, signature verification, encryption envelopes, command planning, lineage, and exit codes.
- Contract tests cover browser APIs, WebAuthn envelopes, plugin readiness, Git bundle layout, and required-check output.
- Integration tests use real local stores, transactions, Git repositories, temporary worktrees, and deterministic provider fakes.
- Browser tests cover enrollment, user presence, cancellation, expiry, replay, wrong-origin, and changed-payload rejection.
- Multiprocess tests cover service startup, cache restoration, publication races, bounded cleanup, and worker termination.
- End-to-end tests cover fresh clone, automatic ensure, onboarding, aligned work, clarification, signed approval, publication, PR validation, merge, and restore by a second developer.
- Security tests cover hostile bundles, special files, symlinks, hardlinks, oversized input, duplicate keys, cycles, stale lineages, secret persistence, cancellation traceback retention, ACL visibility, and repository confusion.
- Compatibility tests preserve existing CLI and MCP contracts.

No default test contacts live GitHub, a WebAuthn provider, or an external content provider. Separately authorized release probes may validate the real GitHub and platform-authenticator boundaries.

## 16. Acceptance criteria

The design is complete when:

1. A developer can run `intent dev --prd <path>`, review extracted intent in the browser, use WebAuthn, and activate a valid baseline.
2. With the developer plugin preset enabled, the first prompt automatically restores and validates team state without a manual Intent command.
3. An aligned prompt proceeds with bounded context and no UI interruption.
4. A new requirement opens questions; a WebAuthn-authenticated human answer and exact proposal approval advance the graph.
5. Agent-submitted hook input can never satisfy a human-answer or approval requirement.
6. `intent check --ci --require-review` replaces the scheduled multi-command sequence and provides stable required-check behavior.
7. A new developer restores the approved baseline from GitHub without checking out `intent-state`.
8. Two competing state publications produce reviewable divergence rather than overwrite.
9. State bundles disclose no plaintext evidence or secrets in GitHub.
10. Disabling the plugin changes no ordinary coding behavior, while the required GitHub check still governs merge.
11. Existing local-only and granular command workflows remain supported.
12. Every graph mutation, human answer, publication, and provider approval remains provenance-backed and audit-replayable.
