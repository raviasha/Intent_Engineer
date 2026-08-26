# Task 5 — Immutable write plans and independent approvals

Status: DONE. Task 6 provider execution was not started.

Base: `f1960b1`.

Product/tests commit: `5ba7f9b` (`feat: create independently approved write plans`).

## Outcome

Added strict, deeply immutable external-write records, a provider-neutral preview planner, independent interactive approval creation, and separate append-only plan and approval ledgers.

Every plan preserves the reconciliation case, connector/profile identity, guarded target and current version, exact before/after content, fully bound provider arguments, all evidence references, all conflicting authors, creator, creation time, and expiry. Its identifier is derived from the canonical hash of every semantic and authorization-relevant field. Changing the target, current version, preview, provider arguments, evidence, or authors changes the hash.

Planning accepts only a `NEEDS_HUMAN` reconciliation case, exact built-in JSON, an allowed profile field set, schema-valid content, and a nonempty change. It never constitutes approval.

Approval requires an exact interactive `approve <plan-id>` confirmation from a separately configured authorized approver while the plan is unexpired. The approver may be neither the proposer nor any conflicting evidence author. Approval binds the exact plan hash, target version, approver, approval time, and expiry. No approval creation API is exposed through MCP.

Plan and approval stores are separate descriptor-safe append-only JSONL ledgers. Exact duplicates are no-ops. Reused identities with changed bytes, malformed or duplicate-key JSON, unknown fields, model-copy validation bypasses, symlink paths, and hardlink paths fail closed. Corrupt persisted content produces one fixed context-free error without retaining the content in repository traceback locals.

This slice deliberately does not call a provider, persist execution receipts, resolve review cases, add CLI commands, or expose mutation tools through MCP. Those remain Task 6 and later.

## TDD evidence

The initial tests-only RED failed during collection because `intent_engineering.mutations` did not exist. The first implementation run exposed canonical timestamp mismatches, strict JSON round-trip problems, and schema validation at the wrong boundary. Focused RED/GREEN iterations aligned canonical UTC encoding, retained exact immutable JSON, validated content before provider argument binding, and validated JSONL records through their strict JSON representation.

A final security regression first demonstrated that corrupt persisted values survived in public exception cause/context. The store parser was moved behind a non-raising decode boundary; the exact regression and the complete mutation suite then passed.

The first independent review found that local actors and provider principals could alias the same person, contributor authorization was missing, write contracts did not guarantee target/version guards, full captured objects conflicted with writable schemas, and standalone approvals lacked semantic hash/time bounds. RED/GREEN fixes added contributor and approver policy gates, complete binding/write-contract hashes, exact provider capability binding, guarded argument-source requirements, writable projections over full immutable before/after state, persisted identity aliases, and self-validating approval windows.

The second review proved that target-provider aliases alone were insufficient for cross-source cases, object types were not bound to write operations, and optional writable fields were inadvertently mandatory. The final model resolves explicit person-level aliases across local, repository/email, and provider namespaces; persists proposer and approver aliases; requires each write contract to declare its target object type; and delegates required-field semantics to JSON Schema. Exact local, cross-provider, wrong-object, and optional-field regressions were added before production fixes.

The final re-review found a self-consistent reload path that could omit creator aliases and an optional-field binding gap. The approval boundary now reauthenticates the stored creator through the exact hash-matched binding and authoritative identity registry before considering an approver. A non-vacuous regression for two local identities sharing one provider principal proves rejection. Absent optional fields omit only their provider field bindings; the complete declared operation remains hash-bound. Final independent review: 0 Critical / 0 Important / 0 Minor; Ready.

## Files

- `src/intent_engineering/mutations/{__init__,models,planner,approval}.py`
- `src/intent_engineering/storage/jsonl/approval_store.py`
- `tests/unit/mutations/{__init__,test_planner,test_approval}.py`
- `src/intent_engineering/capture/mcp/profile_models.py`
- `profiles/mcp/{slack,notion,jira,confluence}.yaml`
- `schemas/mcp-provider-profile.schema.json`
- `tests/unit/capture/mcp/conftest.py`
- `tests/contract/mcp/test_reference_profiles.py`

## Verification

| Gate | Result |
| --- | --- |
| mutation/profile focused selection | 105 passed |
| full offline suite | 898 passed in 31.97s |
| tracked plus Task 5 Ruff | clean |
| `.venv/bin/mypy src/intent_engineering` | 88 source files, clean |
| `git diff --check` | clean |
| independent review | 0 Critical / 0 Important / 0 Minor; Ready |

The five protected untracked artifacts remain untouched. Tests use no live provider, network credential, GUI, or raw Git object access.
