# Task 6 report — attributed clarification and governed proposal confirmation

## Status

READY. Implementation, tests, verification, and seven scoped fix rounds are complete. The final
independent review verdict is `0 Critical / 0 Important / 0 Minor`, Ready.

Execution base: `f1ca752818b39c7d76a8657fdd769cdc1f33ec92`.

Task authority: `.superpowers/sdd/2026-08-26-intent-aware-agent-workflow/task-6-brief.md`.

## Implementation

- Added strict/frozen, content-addressed clarification question, answer, session, event, detached
  submission, schema-2 proposal, and schema-3 decision records. Raw question/answer bodies are
  captured only as immutable conversation evidence; the shared ledger retains bounded digests,
  evidence references, authors, UTC times, exact evidence/event predecessors, status, and the exact
  associated proposal ID.
- Added `ClarificationCoordinator.open`, `.answer`, and `.propose`. It validates exact task,
  conversation request/classification, graph baseline, ACL/principals, 1–16 unique bounded
  questions, required answers, monotonic chronology, source-role association, candidate authorship,
  timestamps, evidence provenance, graph applicability, and detached ChangeSet bindings. It never
  mutates the graph. Exact and concurrent replay converges; divergent/stale input fails closed.
- Extended Task 2's canonical framed ledger with exactly one optional clarification payload while
  preserving the legacy proposal/decision byte spelling. Clarification transitions and the
  following typed proposal are validated as one ordered association. Bootstrap and preflight now
  use the shared compatibility serializer; preflight ignores event frames while retaining legacy
  provisional-proposal behavior.
- Added `ProposalConfirmationService.confirm`. Every attempt reads one descriptor-held live
  config/policy/binding snapshot, re-resolves current person-level aliases, and rejects missing,
  malformed, revoked, drifted, or non-independent authority before graph mutation.
- Risk is derived from the actual proposal/current graph. Pure noncontradictory additions require a
  current contributor. Updates, supersessions, implementation changes, confidence weakening,
  contradictory relationships, destructive proposals, and conflicting authors require a stable
  `needs_human` review case plus a current independent approver disjoint from proposer/conflicting
  aliases.
- Accepted confirmation binds the exact proposal ID/digest, baseline, selected nodes, activation
  ChangeSet, actor/current aliases, proposer/conflict aliases, decision time/action, and review case.
  Decision append, graph, history, and optional case resolution commit atomically. Identical
  concurrent confirmation converges; stale/conflicting replay fails closed.
- Extended `LocalChangeSetExecutor` with exact descriptor-held read-only extras/preimages so live
  authority cannot change between confirmation validation and commit. No provider/model call,
  external write, capability, host hook, or Task 7/8 behavior was added.
- Public failures are fixed/context-free. Cancellation preserves exact signal identity, rolls back
  owned state, and clears question/answer/proposal/policy/actor material from repository traceback
  frames.

## TDD evidence

Before production edits, both required integration files were created and the exact command ran:

```text
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/integration/intent_workflow/test_clarification.py tests/integration/intent_workflow/test_proposal_governance.py -q -W error
```

Required RED: two collection errors from
`ModuleNotFoundError: intent_engineering.intent_workflow.clarification`; `2 errors in 0.27s`.

Hardening progression:

- Initial missing-surface GREEN: `3 passed in 0.55s`.
- First real integration RED: `6 failed, 6 errors in 0.40s`; subsequent GREEN:
  `12 passed in 0.48s`.
- Strengthened evidence/governance/concurrency/crash/cancellation/binding/provenance matrix GREEN:
  `31 passed in 0.92s`.
- Final self-audit tests-only RED for exact proposal-event association, monotonic answer time, and
  concurrent proposal replay: `3 failed, 30 passed in 0.98s`; final focused GREEN:
  `33 passed in 1.49s`.

### Independent review Fix Round 1

The first independent review reported `0 Critical / 5 Important / 0 Minor` and Not Ready. Before
any Fix Round production edit, regressions for all five findings were persisted and the exact
focused pair produced `7 failed, 31 passed in 1.86s`. The failures were exactly: omitted durable
association for a divergent later answer; erasable/reclassifiable existing-node provenance;
missing atomic closure; stale high-risk case creation; and incorrect affected references for
edge-only, confidence-only, and implementation-status-only risk.

Fix Round 1 now records every divergent human turn as an attributed evidence-backed `conflicted`
event without replacing the accepted answer, and blocks proposal while conflicts exist. A further
tests-first chronology probe failed in isolation and now proves that revising an earlier question
retains that answer as the conflict subject while using the latest conversation evidence as the
predecessor. Existing semantic updates must preserve creator/time, source authority, confidence
state/basis/reassessment, implementation status, and all current evidence; only typed confidence or
implementation groups may change their respective state. Stale undecided proposals fail before
review-case construction. Review cases derive exact node, edge-endpoint, confidence, and status
subjects with no fallback. Accepted confirmation appends a proposal/decision/activation-bound
`closed` event in the same decision+graph+history(+case) transaction, and ledger replay rejects a
torn or mismatched clarification decision/closure.

Final Fix Round focused GREEN: `39 passed in 1.17s`.

The persisted tests cover raw-evidence-only secrecy; author and predecessor chronology; question and
answer bounds; required/optional answers; exact open/answer/proposal replay; divergent answers;
stale task/session; ACL/source-role/candidate provenance; typed proposal association; deterministic
risk; contributor/reviewer authority; destructive review gating; policy revocation; cross-provider
person-alias self-bypass; malformed binding; exact decision/history/case binding; concurrent
confirmation; five transaction crash stages; cancellation identity, rollback, and traceback
secrecy. Existing Task 2 tests retain corrupt/noncanonical/FIFO/symlink/hardlink/interruption and
recovery coverage for the extended ledger.

### Independent review Fix Round 2

The second independent review reported `0 Critical / 3 Important / 0 Minor` and Not Ready. Fix
Round 2 began tests-first. The first tests-only focused run was `7 failed, 37 passed in 3.38s`; one
failure was a test-construction warning caused by flattening a nested typed proposal under `-W
error`. After correcting only that fixture construction, the exact focused command established the
authoritative behavioral RED of `6 failed, 38 passed in 2.55s` before any production edit. Those
six failures were exact conflict replay, a missing proposed-event proposal frame, and missing
two-sided evidence for node update plus edge-, confidence-, and status-only review cases. The
already-passing reordered, mismatched, and duplicate-frame parameters proved those hostile
associations were already rejected.

Fix Round 2 makes replay of an already-associated divergent turn return the same conflict/session
without changing evidence or ledger bytes; a third distinct answer still appends a new conflict in
exact predecessor order. Review cases now put current canonical graph evidence and proposed/session
evidence on distinct attributable sides whenever an affected current assertion exists, deriving the
current refs, authors, observation time, authority mode, and confidence from exact affected nodes
and evidence. The canonical decoder now requires every `proposed` event to be followed immediately
by its one exact session-bound typed proposal frame, while legacy proposal/decision frames and open
or answered clarification sessions retain their prior byte-compatible behavior.

Final Fix Round 2 focused GREEN: `44 passed in 2.34s`.

The final diff audit then identified that the proposal side still inherited `current=True` from the
shared evidence-side model. Exact current/proposal flag assertions were added first and produced
`4 failed, 40 passed in 1.13s`; setting only the proposal side to non-current restored the focused
suite to `44 passed in 1.23s`. All final gates below were rerun after that production edit.

### Independent review Fix Round 3

The final narrow re-review reported `0 Critical / 1 Important / 0 Minor` and Not Ready because the
case packet still collapsed heterogeneous current modes and hardcoded proposal epistemics to
`EXPLICIT`/`1.0`. Before any Fix Round 3 production edit, exact real-store regressions for a current
`DERIVED`/`0.63` node, an added proposed `INFERRED`/`0.8` node, a proposed confidence change to
`0.7`, honest edge/status metadata, and a mixed-endpoint edge produced the authoritative focused
RED: `6 failed, 41 passed in 3.79s`.

Review-case construction now derives one epistemic assertion for each exact affected current and
proposed semantic subject. It groups assertions only when source mode, confidence, evidence refs,
authorship, and observation time are all identical; otherwise it emits stable subject-qualified
current/proposal sides. Node additions and replacements use their own typed epistemics, confidence
changes use the exact new confidence without changing source authority, and edge/status proposals
use the exact endpoint/claim assertion metadata rather than invented defaults. Evidence refs,
authors, current flags, affected IDs, and ACL-grounded evidence lookup remain exact.

Final Fix Round 3 focused GREEN: `47 passed in 1.15s`.

The final ACL-grounding audit then added a current-evidence visibility probe. It failed as intended
at `1 failed, 47 passed in 1.09s`, proving a case could otherwise expose a current evidence ref not
visible to the confirming actor. Passing the live actor aliases into case construction and checking
each current assertion's exact refs before attribution restored the focused suite to `48 passed in
1.22s` with byte-identical failure state. Every final gate below was rerun after this production
edit.

### Independent review Fix Round 4

The next final narrow review reported `0 Critical / 1 Important / 0 Minor` and Not Ready because an
exact replay of an already-applied high-risk proposal tried to reconstruct its review case from the
post-activation graph before authenticating the durable decision. Before any Fix Round 4 production
edit, a real update proposal was review-gated for its author, applied by the independent approver,
snapshotted across ledger/cases/graph/history, and replayed with the exact same actor, aliases,
timestamp, and input. The exact focused RED was `1 failed, 48 passed in 1.78s`; five additional
actor/time/subset/policy/stale-state characterizations passed while the positive replay remained the
sole failure (`1 failed, 53 passed in 1.27s`).

Applied replay now branches before review-case reconstruction and authenticates the current live
role and aliases, exact proposal/digest/baseline/input, reconstructed V3 decision and activation
ChangeSet, sole bound closed event, canonical last history record, current graph version and exact
activation effects, resolved case transition, and case-evidence ACL. It returns a fresh detached
`APPLIED` result only after all durable bindings match. A changed actor, time, selected subset,
policy, aliases/binding, graph/history, closure, decision, or case remains the same fixed opaque
failure without mutation; no proposal-ID-only shortcut exists.

Final Fix Round 4 focused GREEN: `54 passed in 1.11s`. The prior Round 3 epistemic/ACL selection is
independently `7 passed`.

### Independent review Fix Round 5

The next scoped review reported `0 Critical / 2 Important / 0 Minor` and Not Ready because applied
replay authenticated only selected fields from affected graph objects and derived its expected
resolved-case transition from mutable stored case state. Before any Fix Round 5 production edit,
real-store regressions changed an affected confidence node's label, injected/removed/reordered its
evidence refs (including an ACL-invisible evidence record), and rewrote both canonical case versions'
impact, subject, affected refs, or evidence-side claim while retaining their IDs and fingerprint.
The authoritative focused RED was `8 failed, 54 passed in 2.66s`.

New V3 decisions commit a canonical digest of every complete directly affected node and edge after
activation, including version, semantic fields, evidence refs, authorship, source mode, confidence,
and status, while deliberately excluding unrelated concurrent graph objects. They also commit the
exact unresolved review-case preimage digest. Applied replay derives the expected review-case ID and
transition from the authenticated proposal, current preimage, and canonical review semantics; it
then verifies the committed unresolved preimage, exact resolved transition, full affected graph
effect, activation ChangeSet, history, decision, closure, live authority, and caller input before
returning. Same-version field tampering, hidden evidence manipulation, or coordinated case-version
forgery therefore fixed-fails with byte-identical durable state.

Final Fix Round 5 focused GREEN: `62 passed in 1.54s`. The prior Round 3/4 epistemic, ACL, and
authenticated-replay matrix is independently `13 passed, 24 deselected in 0.60s`.

### Independent review Fix Round 6

The final narrow review reported `0 Critical / 1 Important / 0 Minor` and Not Ready because review
case ACL validation preceded `_ensure_case`, but an independent reviewer's live aliases could be
revoked before the second snapshot without revalidating case evidence. Before any Fix Round 6
production edit, a deterministic real-store test persisted the canonical case, revoked the
reviewer's sole `jira:ben` access alias inside that boundary, and required a fixed failure with no
subsequent graph or decision bytes. The authoritative behavioral RED was `1 failed in 0.88s` (an
earlier command attempt had only a repository `PYTHONPATH` collection error and is not the RED).

The final post-case snapshot is now converted into one immutable authenticated bundle only after
revalidating the live project/policy/binding registry, contributor and reviewer roles, exact
person-level aliases and independence, proposal ledger and graph baseline, proposal/ChangeSet
bindings, live source-role associations, proposal evidence ACL, and the newly regenerated canonical
review case with every current/proposed evidence ref visible. Decision and activation are derived
only from that bundle. The executor additionally binds the exact evidence bytes at the atomic commit
boundary, alongside its existing config, policy, provider binding, proposal-ledger, graph, and case
checks, so later authority-relevant evidence drift fails before mutation.

Final Fix Round 6 race GREEN: `1 passed in 0.29s`; complete clarification/governance focused GREEN:
`63 passed in 1.36s`.

### Independent review Fix Round 7

The next narrow review reported `0 Critical / 1 Important / 0 Minor` and Not Ready because the
closed-session and executor-exception branches could return `APPLIED` after finding only matching
decision and closure frames. Before any Fix Round 7 production edit, a deterministic test injected
the exact computed V3 decision and exact bound closed event during confirmation while leaving the
graph at its baseline version, history empty, and the review case `needs_human`. The authoritative
tests-only RED was `1 failed in 0.79s`: the torn ledger incorrectly returned `APPLIED`.

All applied results now pass through one shared fresh-snapshot durable-state authenticator. Initial
replay, a closed-session concurrent race, an executor concurrent-loser recovery, and successful
post-commit confirmation each require the exact live proposal/decision/input/authority/ACL binding,
sole exact closure, exact activation ChangeSet as the last history entry, complete canonical graph
effect and version, and exact decision-bound unresolved-case preimage plus resolved transition.
Neither matching ledger frames nor a matching decision alone can produce `APPLIED`.

Final Fix Round 7 torn-state GREEN: `1 passed in 0.25s`; complete clarification/governance focused
GREEN: `64 passed in 1.54s`; prior replay/concurrency/tamper selection: `17 passed, 22 deselected in
0.83s`.

### Final independent review

The same scoped reviewer completed the final Round 7 review with `0 Critical / 0 Important / 0
Minor` and verdict Ready. No additional implementation change was requested.

## Final gates

- Mandated clarification/governance plus mutation-authorization gate: `88 passed in 2.12s`.
- Final Fix Round 7 broadened governance, mutation, ledger, bootstrap, preflight, recovery,
  executor, validation, and host contract compatibility: `304 passed in 3.84s`.
- Required Ruff command and all changed Python paths: `All checks passed!`.
- `.venv/bin/mypy src`: `Success: no issues found in 111 source files`.
- The first fresh full offline warnings-as-errors run after the final Fix Round 7 edit hit the known
  unchanged fork lock race once (`1 failed, 1295 passed in 53.57s`); the isolated unchanged case
  passed (`1 passed in 0.33s`). The authoritative fresh full rerun is
  `1296 passed in 54.29s`.
- `git diff --check`: no output.

Before Fix Round 1, an earlier fresh full run passed `1263 tests`; before it, one collection attempt correctly exposed a
package-level convenience-export circular import. That nonessential re-export was removed, and both
subsequent fresh full runs were clean. The final result above is authoritative.

## Files changed

- `src/intent_engineering/intent_workflow/clarification.py`
- `src/intent_engineering/intent_workflow/models.py`
- `src/intent_engineering/intent_workflow/proposal_store.py`
- `src/intent_engineering/intent_workflow/bootstrap.py`
- `src/intent_engineering/intent_workflow/preflight.py`
- `src/intent_engineering/intent_workflow/__init__.py`
- `src/intent_engineering/storage/executor.py`
- `tests/integration/intent_workflow/test_clarification.py`
- `tests/integration/intent_workflow/test_proposal_governance.py`
- `.superpowers/sdd/2026-08-26-intent-aware-agent-workflow/task-6-report.md`
- `.superpowers/sdd/2026-08-26-intent-aware-agent-workflow/progress.md`

## Self-review

- Legacy proposal and decision frames re-encode byte-for-byte without a new null field; new event
  frames remain canonical, strict, one-of, contiguous, newline-terminated, and bounded.
- Raw question/answer bodies never enter clarification events. Tests use an answer-only marker to
  distinguish raw evidence from intentionally derived semantic proposal text.
- Current aliases are recomputed from held live authority on every attempt; no caller or stored
  alias set is trusted. Approver overlap with proposer or conflicting-author aliases is a denial.
- Review-case creation never mutates the graph. Accepted graph/history/decision/case resolution is
  one recovery-domain transaction bound to exact authority preimages.
- No model/provider call or external provider mutation exists in the new module.
- The five protected untracked artifacts remain unstaged and were not read, modified, renamed, or
  deleted.

## Concerns

No known implementation concern remains. Final scoped review is Ready with
`0 Critical / 0 Important / 0 Minor`.
