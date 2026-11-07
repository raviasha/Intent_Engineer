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

## Improve the graph without applying it

Start a voluntary bounded session from the CLI or choose **Improve graph** in `intent dev`:

```bash
intent refine --project . --minutes 5
intent refine --project . --focus req:csv-export
intent refine --project . --session <session-id> --action pause --format json
intent refine --project . --session <session-id> --action resume --format json
```

Questions are selected deterministically from the visible scorecard and show the affected node,
dimension, rule, reason, and requested fields. An explicit answer is captured immediately as
authorized evidence. Raw answer text is not copied into the enrichment-session ledger; the ledger
stores its evidence reference so pause, process restart, and resume retain the provenance.

Enrichment is proposal-only governance. Answering, skipping, pausing, resuming, or asking for a
proposal does not edit the canonical graph. The proposed ChangeSet must pass the existing review
and approval boundary before it can become canonical. Scores remain derived projections and are
recomputed from the accepted graph and currently visible evidence.

## Compare simpler or different requirements

Alternatives are suggestions for satisfying the same intent with a simpler implementation, a
different or manual approach, a narrower scope, or reuse of an existing capability. They are
generated only after an explicit request. In `intent dev`, select a visible orange or red
requirement and choose **Suggest alternatives**. Each result shows:

- the current approved requirement beside the projected alternative;
- preserved and weakened desired outcomes;
- changed or removed constraints;
- implementation and operational complexity;
- supporting evidence, risks, trade-offs, and assumptions; and
- server-computed score changes by assessment dimension; and
- the exact requirement and constraint graph effects that independent review would activate.

Every alternative is labeled **Projected — not approved**. Generation and comparison do not edit
the graph. Choosing **Create review proposal** creates an ordinary pending requirement proposal;
the existing independent local review remains the only activation path.

The optional CLI uses the same bounded service:

```bash
intent alternatives req:csv-export --mode simplify --format json
intent alternatives req:csv-export --mode change_approach --format markdown
```

The reasoner sees only bounded ACL-visible intent, outcome, constraint labels, and safe evidence
summaries. Local validation requires the candidate to preserve the visible intent, account for every
visible desired outcome, and match every declared constraint change to an exact graph effect. The
same locally materialized activation shape drives both projected assessment and the pending proposal.

No AI provider is required for assessment or enrichment. Without a configured alternative
reasoner, this command returns the fixed provider-unavailable state and assessment remains
available. A coding host can instead call `intent_alternative_context`, draft up to four candidates,
and pass each one to `intent_alternative_validate`. Only locally validated candidates should be
shown. To select an exact host-generated candidate, choose **Validate host candidate** for the
requirement in Intent Dev, paste the candidate JSON, and choose **Validate host candidate locally**.
Intent Dev re-grounds it against the current ACL-filtered snapshot before enabling proposal review.

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

Two additional read-only enrichment tools accept one exact `session_id`:

- `intent_enrichment_status` returns the fresh authorized session projection; and
- `intent_enrichment_next_question` returns its current deterministic question.

They do not recover, advance, pause, resume, answer, skip, propose, or mutate a session or graph.
Expired, stale, corrupt, hidden, and absent sessions fail closed. Responses are bounded and contain
neither plaintext answers nor hidden evidence. No enrichment mutation tool is registered over MCP.

Three additional read-only alternative tools are explicit-request only:

- `intent_alternative_context` returns the bounded visible grounding packet;
- `intent_alternative_validate` validates one untrusted host candidate and returns its projected
  comparison; and
- `intent_alternatives` invokes a configured server reasoner, or returns a fixed provider-unavailable
  result.

They cannot submit, approve, or apply a proposal. The local control plane revalidates a selected
host candidate before it can create a pending proposal.

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
visualization, voluntary enrichment, and grounded requirement alternatives are available projections
over this foundation. This alternatives increment focuses on intent-to-requirement alignment; it
does not establish requirement-to-code or requirement-to-test matching. That traceability work,
automatic activation beyond existing reviewed governance, and the broader state-hardening backlog
remain deferred.
