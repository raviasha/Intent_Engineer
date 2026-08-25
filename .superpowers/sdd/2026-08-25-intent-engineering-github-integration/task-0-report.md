# Task 0 report: durable replay and evidence chronology

## Commits

- Base commit: `6d13a9ea06de84d32da69015812f5062eb224969`
- Final implementation commit: `f871681c34bcfd053a1ba1be4cbcac99b6ec74b1`
- Implementation commits:
  - `1a15e95` — `fix(sync): replay durable evidence past checkpoints`
  - `314bd99` — `fix(reconcile): derive lag chronology from evidence`
  - `f871681` — `fix(evidence): authenticate connector ingestion chains`
- This report is committed separately after the implementation commits.

## RED evidence

### Durable pending replay

Command:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/integration/sync/test_recovery.py -q
```

Observed before production changes: `2 failed, 12 passed`. The deletion retry produced zero graph changes because the already-durable record was absent from fresh discovery. The change retry began with source v2 and omitted already-durable pending v1.

Legacy migration RED:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/integration/sync/test_recovery.py::test_legacy_evidence_and_checkpoint_migrate_with_one_semantic_replay -q
```

Observed: `1 failed`. Rediscovering a pre-`ingested_by` row under the new ingestion boundary caused the connector run to fail due to an immutable-ID conflict.

Checkpoint-contract RED:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/unit/core/models/test_evidence.py::test_checkpoint_consumption_boundary_is_versioned_and_unique -q
```

Observed: `1 failed`. Duplicate evidence IDs were accepted in the consumption boundary.

### Evidence-derived chronology

Command:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/unit/reconcile/test_evidence_detection.py tests/unit/reconcile/test_detectors.py -q
```

Observed before production changes: `6 failed, 13 passed`. CODE_LAG, REQUIREMENT_LAG, and TEST_LAG disappeared when ordinal fixture inputs were removed, while tied/non-current resolved evidence still emitted CODE_LAG. This demonstrated that lag rules trusted declaration integers instead of immutable provenance.

## GREEN and static verification

Required focused gate:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin \
  tests/integration/sync \
  tests/unit/reconcile/test_evidence_detection.py \
  tests/unit/reconcile/test_detectors.py \
  tests/integration/test_fixture_matrix.py -q
```

Result: `59 passed in 2.71s`.

Full suite:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin -q
```

Result: `339 passed in 17.10s`.

The requested whole-tree Ruff command was also run:

```bash
.venv/bin/ruff check src/intent_engineering tests
```

It reported only the preserved untracked Desktop artifact: `N999 Invalid module name: 'dogfood 2'` at `src/intent_engineering/core/policy/dogfood 2.py:1:1`. No tracked file was implicated. The tracked-file-safe equivalent was then run:

```bash
git ls-files -z -- '*.py' | xargs -0 .venv/bin/ruff check
```

Result: `All checks passed!`.

Type checking:

```bash
.venv/bin/mypy src/intent_engineering
```

Result: `Success: no issues found in 65 source files`.

## Architecture and compatibility decisions

- `SyncCheckpoint` now carries a versioned, provider-neutral semantic-consumption boundary: the cumulative immutable evidence IDs successfully processed for that connector. The source cursor remains the discovery boundary; it is no longer assumed to identify semantic consumption.
- `EvidenceRecord.ingested_by` associates new durable records with the connector instance without adding provider vocabulary to core. Legacy records with no field use a migration fallback to `connector_type == connector_id`.
- Retry reconstruction enumerates unconsumed durable connector records in JSONL append order before discovery, merges newly fetched records afterward, de-duplicates exact identities, and reconstructs immediate predecessor links from durable version chains. `evidence_added` still counts only new JSONL appends.
- A legacy checkpoint with no consumed IDs performs one safe semantic rescan. After successful completion its boundary is populated, so the next retry is a no-op. A legacy same-ID record is considered equivalent when the sole difference is the newly supplied ingestion boundary; it is not rewritten and does not conflict.
- Lag inputs now use evidence ordering rather than declaration ordinals. Same-object versions are ordered by durable append position. Different objects/sources are ordered by `observed_at`. Equal timestamps are tied; mixed pairwise ordering is unknown. Detectors requiring an order accept only a proven strict relationship and otherwise fail closed.
- Legacy ordinal fields remain accepted in detection declarations for migration only. When both sides provide ordinals, their relative order must match derived evidence chronology; inconsistent declarations create no case. Ordinals are omitted from trusted `DetectionInput` construction.
- Deterministic fixture repositories now produce CODE_LAG, REQUIREMENT_LAG, and TEST_LAG from controlled Markdown modification times and Git author timestamps. Renumbering consistent legacy ordinals does not alter classification or fingerprints.

## Changed files

- `schemas/evidence.schema.json`
- `src/intent_engineering/capture/checkpoints.py`
- `src/intent_engineering/core/models/evidence.py`
- `src/intent_engineering/core/models/project.py`
- `src/intent_engineering/reconcile/detectors.py`
- `src/intent_engineering/reconcile/evidence_detection.py`
- `src/intent_engineering/storage/interfaces.py`
- `src/intent_engineering/storage/jsonl/evidence_store.py`
- `src/intent_engineering/storage/yaml/checkpoint_store.py`
- `src/intent_engineering/sync/orchestrator.py`
- `tests/helpers/fixtures.py`
- `tests/integration/sync/test_combined_detection.py`
- `tests/integration/sync/test_recovery.py`
- `tests/unit/cli/test_runtime_metadata.py`
- `tests/unit/core/models/test_evidence.py`
- `tests/unit/reconcile/builders.py`
- `tests/unit/reconcile/test_evidence_detection.py`

## Remaining concerns

- The cumulative consumed-ID tuple gives an explicit, migration-safe alpha contract but grows linearly with connector evidence history and rewrites the checkpoint YAML when the boundary advances. A future storage migration can replace it with an evidence-ledger sequence/watermark once the store exposes a stable per-connector append offset; that change should preserve versioned checkpoint decoding and the one-safe-rescan fallback.
- Legacy fallback can identify rows only when the historical `connector_type` equals the connector ID. Existing Markdown and Git rows satisfy this. Any pre-contract custom connector that used a different instance ID needs an explicit migration rather than a guessed association.
- The exact whole-tree Ruff gate remains intentionally obstructed by the preserved untracked `dogfood 2.py` Desktop artifact; tracked Python files pass.

## Fix round 1: authenticated ingestion chains and currentness

The independent review found that the first implementation still trusted caller tuple order for same-object chronology, copied declaration `current`, and stored one connector owner on the immutable evidence record. Commit `f871681c34bcfd053a1ba1be4cbcac99b6ec74b1` closes those findings. The earlier `ingested_by` and caller-order decisions above are superseded by this section.

### Fix-round RED evidence

Evidence detection:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/unit/reconcile/test_evidence_detection.py -q
```

Result before production changes: `2 failed, 8 passed`. Reversing the same immutable v1/v2 records changed the result from CODE_LAG to no case, and an old exact v1 reference declared current still emitted CODE_LAG despite durable v2.

Ingestion ledger contracts:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/contract/storage/test_evidence_store_contract.py -q
```

Result before production changes: `3 failed, 5 passed`. There was no connector-association ledger, connector-scoped predecessor chain, or explicit custom legacy migration failure.

Checkpoint association validation:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/unit/validation/test_service.py::test_checkpoint_consumption_boundary_rejects_missing_or_foreign_associations -q
```

Result before production changes: `1 failed`. A missing consumed evidence ID produced no diagnostic.

Custom legacy validation was separately observed RED: `1 failed, 2 passed`; only `evidence.id_mismatch` appeared before the explicit `evidence.legacy_association_ambiguous` diagnostic was added.

### Fix-round GREEN and static verification

Expanded focused gate:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin \
  tests/integration/sync \
  tests/contract/storage/test_evidence_store_contract.py \
  tests/unit/reconcile/test_evidence_detection.py \
  tests/unit/reconcile/test_detectors.py \
  tests/unit/validation/test_service.py \
  tests/integration/test_fixture_matrix.py -q
```

Result: `83 passed in 2.88s`.

Full suite:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin -q
```

Result: `348 passed in 17.00s`.

Tracked static checks:

```bash
git ls-files -z -- '*.py' | xargs -0 .venv/bin/ruff check
.venv/bin/mypy src/intent_engineering
```

Results: `All checks passed!` and `Success: no issues found in 65 source files`.

The exact whole-tree Ruff command still reports only `N999` for the preserved untracked `src/intent_engineering/core/policy/dogfood 2.py` artifact.

### Fix-round architecture and migration semantics

- `EvidenceRecord` is provider-neutral again; `ingested_by` was removed from the model and generated public schema.
- The evidence JSONL store now accepts a strict versioned union: legacy raw `EvidenceRecord` rows and atomic `EvidenceIngestion` envelopes. Each envelope embeds the immutable record and binds it to a connector ID, per-connector sequence, and connector-scoped immediate predecessor in one durable append.
- Distinct connector IDs may associate independently with the exact same immutable evidence. Only the first global evidence identity increments `evidence_added`; each connector still gets its own ledger entry, semantic replay, checkpoint boundary, and predecessor chain.
- Built-in raw Markdown/Git rows are migrated by appending explicit envelopes before replay. Custom connector/type legacy ownership is never guessed: the store raises an actionable explicit-association error and deep validation emits `evidence.legacy_association_ambiguous`.
- `EvidenceDelta` carries the authenticated ingestion envelopes into combined detection. Same-object order comes only from shared connector sequences, so permuting the record tuple cannot affect results.
- Side currentness is derived from the authenticated connector/provider/object chain. Declaration `current` is optional legacy consistency metadata; a stale exact reference claiming current fails closed.
- Checkpoint consumed IDs are checked at runtime and by deep validation. Missing and foreign associations fail rather than suppressing replay. Corrupt, non-contiguous, duplicate, or predecessor-invalid envelopes produce redacted `evidence.invalid` diagnostics.
- The cumulative consumed-ID checkpoint tuple remains an alpha tradeoff. It grows linearly and rewrites YAML; the authenticated per-connector ledger now provides the stable sequence needed for a future watermark migration without changing evidence identity.

### Fix-round changed files

- `schemas/evidence.schema.json`
- `src/intent_engineering/cli/runtime.py`
- `src/intent_engineering/core/models/__init__.py`
- `src/intent_engineering/core/models/evidence.py`
- `src/intent_engineering/reconcile/evidence_detection.py`
- `src/intent_engineering/storage/interfaces.py`
- `src/intent_engineering/storage/jsonl/evidence_store.py`
- `src/intent_engineering/sync/orchestrator.py`
- `src/intent_engineering/validation/service.py`
- `tests/contract/storage/test_evidence_store_contract.py`
- `tests/e2e/test_cli_local.py`
- `tests/integration/sync/test_combined_detection.py`
- `tests/integration/sync/test_recovery.py`
- `tests/unit/cli/test_runtime_metadata.py`
- `tests/unit/reconcile/test_evidence_detection.py`
- `tests/unit/validation/test_service.py`
