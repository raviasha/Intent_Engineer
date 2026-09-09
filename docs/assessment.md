# Explainable intent-graph assessment

`intent assess` answers a narrow question: how completely does the currently visible evidence
support the intent graph? The result is a derived scorecard, not canonical truth. Assessment is:

- **explainable**: every deduction has a stable rubric rule, points, references, and a next action;
- **ACL-filtered**: it is computed only from the invoking actor's visible projection, without hidden
  counts, topology, identifiers, or values;
- **non-canonical**: scores, confidence, health, and colors are never written into graph or evidence
  state; and
- **model-independent**: rubric version 1 performs no model or connector call.

The service reads one descriptor-held snapshot of graph, evidence and ingestion lineage,
reconciliation cases, clarifications, semantic history, policy, and the principal projection. The
report binds each input digest and exposes a `semantic_digest`. The presentation timestamp is not
part of that digest, so CLI, MCP, and CI produce the same semantic identity for identical visible
state. Input order does not change the result.

## Run an assessment

Assess the entire visible project:

```bash
intent assess --project . --format json
```

Select one visible node or an intent branch with its exact identifier:

```bash
intent assess --project . --focus req:csv-export --format json
```

`--format` accepts `text`, `json`, or `markdown`. All formats contain the versioned report; JSON is
the stable machine-facing form. Command exit code `0` means a report was emitted; exit code `1` means the
snapshot, requested focus, or assessment input was unavailable; the fixed failure does not reveal
whether an identifier was hidden or absent. Assessment does not recover an incomplete transaction
or update canonical state.

## Read the scorecard

Rubric version 1 evaluates seven dimensions:

1. intent clarity;
2. evidence strength;
3. requirement coverage;
4. implementation traceability;
5. test verification;
6. consistency; and
7. freshness.

An applicable dimension starts at 100 and subtracts each distinct failed rule once, clamped to
0–100. `required` and `inherited` dimensions contribute to the version-one rollup. `optional`
dimensions are shown but do not contribute. `not_applicable` dimensions are displayed as **N/A**,
carry no score or confidence, and are excluded rather than treated as zero. Rubric version 1 uses
equal integer dimension weights. Node and branch contribution weights are published in scorecards,
and the exact assessment policy is bound by its digest. Rollups use deterministic integer
arithmetic.

Confidence is the percentage of declared rubric inputs resolved by visible evidence and topology;
it is not a probability that a claim is true. Health follows the worst significant required gap:

| Health | Meaning |
| --- | --- |
| Green | Every contributing dimension scores at least 75, confidence is at least 75, and there is no blocking conflict. |
| Orange | No contributing dimension is red, but at least one score is 50–74 or its confidence is below 75. |
| Red | A contributing dimension scores below 50, or a visible blocking conflict exists. |
| Unassessed (`unassessed`) | The node type or rubric is unsupported, or no applicable inputs can be evaluated; score and confidence are absent. |

Branch and project health never hide a critical red path behind an average. When a critical node on
an intent branch is red, both that branch and the project remain red. Robustness is capped at 49.
Each node scorecard lists its worst dimension, failed checks, exact deductions, visible
references, blocking cases, and recommended next action.

## MCP reads

Start a standalone stdio server when the client manages the process:

```bash
intent mcp --project .
```

The server exposes three assessment reads:

- `intent_assessment_summary` returns the complete visible assessment and `semantic_digest`;
- `intent_assessment_scorecard` accepts an exact `reference` and returns its visible node and/or
  branch scorecard; and
- `intent_assessment_gaps` returns a stable prefix, with `limit` from 1 through 100 and an optional
  `health` filter of `green`, `orange`, or `red`.

All three tools are read-only and non-destructive. They use the same assessment service as the CLI,
return no authority token, and make hidden and unknown references indistinguishable.

## CI comparison defaults

When an exact comparison base exists, the scheduled/manual `.github/workflows/intent-sync.yml`
restores and captures it in a disposable worktree, assesses base and head directly, uploads both
JSON reports with the drift report, and invokes the hidden machine adapter:

```bash
intent assessment-gate --project . \
  --base-report intent-assessment-base.json \
  --head-report intent-assessment.json \
  --format json
```

The default gate fails only for a newly red critical gap or path. Orange results are not warnings by
default, and minimum-confidence and general robustness-regression gates are disabled until policy
explicitly enables them. Gate exit code `0` means the comparison passed, exit code `5` means the
policy rejected the head report, and exit code `1` means the report or gate input was unavailable. A repository's
initial commit has no comparison base, so the workflow publishes its head report without running a
comparison gate. The later `intent check --require-review` remains a separate assurance decision.

Assessment reports do not approve requirements, resolve conflicts, or authorize changes. Graph
visualization with health styling, voluntary enrichment sessions, and simpler requirement
alternatives belong to later release stages and are not part of this assessment foundation.
