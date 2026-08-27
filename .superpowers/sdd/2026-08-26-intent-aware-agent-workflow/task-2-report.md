# Task 2 report — descriptor-safe proposal and decision ledger

## Status

Complete. The implementation adds one canonical framed ledger for `IntentProposal` and
`ProposalDecision`, initializes and exposes its exact descriptor-held file, and extends the shared
transaction/recovery domain without invalidating either historical journal target set.

## TDD evidence

### Required RED

Command:

```text
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/unit/intent_workflow/test_proposal_store.py -q -W error
```

Exact result:

```text
ERROR tests/unit/intent_workflow/test_proposal_store.py
ModuleNotFoundError: No module named 'intent_engineering.intent_workflow.proposal_store'
1 error in 0.15s
```

This was the expected collection failure before any Task 2 production file existed.

### Ledger GREEN

The first complete unit implementation run finished with:

```text
...................                                                      [100%]
19 passed in 0.13s
```

Additional test-first hardening witnessed these focused RED/GREEN cycles:

- legacy reinitialization RED: missing `intent-proposals.jsonl`, `1 failed in 0.74s`;
  GREEN: `1 passed in 0.86s`;
- read-cancellation traceback RED: decoded secret remained in a repository frame,
  `1 failed in 0.28s`; GREEN: `1 passed in 0.10s`.

## Final gates

Focused ledger, startup recovery, and runtime paths:

```text
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/unit/intent_workflow/test_proposal_store.py tests/integration/test_startup_transaction_recovery.py tests/unit/cli/test_runtime_paths.py -q -W error
..............................                                           [100%]
30 passed in 0.31s
```

Required Ruff gate:

```text
.venv/bin/ruff check src/intent_engineering/intent_workflow/proposal_store.py tests/unit/intent_workflow/test_proposal_store.py
All checks passed!
```

All other changed source/test paths were also checked with Ruff and returned
`All checks passed!`.

Required type gate:

```text
.venv/bin/mypy src
Success: no issues found in 105 source files
```

Fresh full offline warnings-as-errors gate:

```text
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin -q -W error
1073 passed in 40.59s
```

The first full run found two pre-Task-2 expectations that still asserted the five-target recovery
set and a wholly empty `history/` directory (`2 failed, 1069 passed in 41.60s`). Their minimal Task 2
expectation updates passed in isolation (`2 passed in 1.04s`). A later full attempt hit the known,
unrelated receipt-store fork race once (`1 failed, 1072 passed in 46.15s`); the exact isolated test
then passed (`1 passed in 0.23s`) and the required clean full rerun above passed.

## Changed files

- `src/intent_engineering/intent_workflow/proposal_store.py` — framed strict ledger, fixed public
  failure, canonical append/read/indexing, exact decision binding, transaction-target authentication,
  and cancellation buffer clearing.
- `src/intent_engineering/core/policy/project.py` — safely creates or authenticates the empty
  single-link ledger during new and legacy-idempotent initialization; includes it in durable-state
  checks.
- `src/intent_engineering/cli/runtime.py` — adds `Runtime.intent_proposals`, the sixth transaction
  target, exact held-target store construction, and both prior legacy target sets.
- `tests/unit/intent_workflow/test_proposal_store.py` — real ledger bytes, replay, corruption,
  special-file watchdog, transaction recovery/binding, and traceback/cancellation tests.
- `tests/integration/test_startup_transaction_recovery.py` — proves both three-target and five-target
  historical journals recover before store parsing.
- `tests/unit/cli/test_runtime_paths.py` — proves safe initialization, runtime binding, missing-ledger
  legacy loading, and idempotent legacy repair without graph rewrite.
- `tests/e2e/test_cli_local.py` — updates the complete shared recovery-domain assertion with the new
  target.
- `tests/integration/github/test_no_secret_persistence.py` — updates fresh-workspace expectations for
  the required zero-byte history ledger.

## Self-review

- Every nonempty frame must end in a newline, strict-decode as one object, reproduce the exact
  canonical UTF-8 JSON bytes, and carry its contiguous zero-based sequence.
- The one-of envelope remains focused on proposals and decisions so Task 6 can extend it explicitly.
- Replayed proposal/decision API calls are no-ops with byte-identical storage; duplicate durable
  frames, conflicting bodies, a second decision, or a decision before/wrongly bound to its proposal
  fail without rewrite.
- Decision acceptance checks the content-addressed proposal ID/digest and exact baseline graph
  version. Model revalidation preserves actor/authorship fields without treating a proposal as
  canonical graph authority.
- Reads use `SecureFile.read_bytes_nonblocking`; FIFO, symlink, hardlink, duplicate-key,
  noncanonical, sequence-gap, duplicate, and unterminated ledgers fail through one fixed error under
  a one-second fork watchdog. Exact inode/type and bytes remain unchanged.
- Public invalid/corrupt paths raise outside parser exception contexts. Append and read interruption
  tests confirm private proposal text is absent from repository traceback locals.
- Runtime constructs the store from `transactions.target_file("intent_proposals")` and the store
  authenticates that target before use. Coordinated operations recover a prepared journal first.
- New initialization fsyncs an empty single-link regular file. Reinitialization repairs only a
  missing ledger and preserves graph bytes; legacy runtime loading also permits the absent target.
- `git diff --check` is clean. No protected artifact was staged, rewritten, renamed, or deleted.

## Concerns

- The repository's known receipt-store multiprocessing race occurred once during a full-suite run;
  it passed immediately in isolation and the subsequent complete suite passed. No Task 2 product
  change was made for that unrelated nondeterministic failure.
- No open Task 2 correctness concern remains.
