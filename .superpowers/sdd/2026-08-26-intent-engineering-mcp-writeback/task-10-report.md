# Task 10 — Public-alpha release proof

Status: COMPLETE.

Base: `46e5e3d`.
Product/docs/tests commit: `f1a4cb1`.

## Outcome

Documented the shipped install-once, capture, manage, scheduled sync, separate drift/reconciliation,
coding-agent MCP, and guarded write workflow. The documentation explicitly distinguishes scheduled
polling from unshipped hosted ingestion/webhooks and distinguishes proposal/preview from independent
interactive approval. It explains how authenticated provider/local aliases preserve each teammate's
original author, timestamp, locator, version/predecessor, evidence, ACL, and competing diff without
last-write-wins.

Added strict credential-free example bindings for Slack, Notion, Jira, and Confluence. Every example
loads through the production profile/binding models and validates against its checked-in profile.

Added an offline public-alpha harness that uses one initialized project and one production runtime
for GitHub, Slack/MCP, markdown, and Git synchronization through the real orchestrator, reasoner,
detectors, transaction coordinator, and stores. Validation, drift rendering, the Intent MCP server,
WriteWorkflow planner/approval/executor, transaction committer, and immutable receipts all consume
that same state. Only deterministic HTTP/MCP provider boundaries and the injectable interactive
terminal are substituted. It proves:

- the first combined sync adds 11 immutable evidence objects, applies two graph changes, and creates
  a source-derived `CONFLICTING_SOURCES` case; the identical second sync is a semantic no-op;
- a later Slack revision creates exactly one new evidence version with a different author and an
  explicit predecessor, instead of overwriting the earlier teammate's contribution;
- validation, the Markdown drift report, and production MCP context all observe that same graph,
  case, and evidence domain;
- missing approval rejects at the production MCP mutation surface with zero provider calls;
- a different authorized human creates two exact approvals, the first execution performs exactly
  one provider mutation and persists a receipt plus reviewer-authored write evidence, while the
  second rejects after the target changes with zero mutations; and
- unique GitHub, Slack, and Jira credential sentinels pass through production credential-resolution
  boundaries and occur in no regular project file, captured output, or structured log event.

## TDD evidence

The first tests-only run failed collection with
`ModuleNotFoundError: tests.e2e.public_alpha_harness`. Independent review then demonstrated that the
first GREEN harness was compositionally vacuous: its sync used a no-op reasoner in a separate store
domain, its only case was seeded, its sentinel scan omitted outputs/logs, its clean setup omitted the
referenced profile, and its write assertions were too weak. The replacement harness first failed on
a generated-repository path collision, then exposed that source-created cases must pass through the
real human-review transition before a write preview. Each was fixed without weakening production
contracts. The final smoke and clean-project documentation/profile test pass repeatedly.

## Files

- `README.md`
- `CONTRIBUTING.md`
- `docs/mcp.md`
- `docs/provider-profiles.md`
- `examples/mcp-bindings/`
- `tests/e2e/public_alpha_harness.py`
- `tests/e2e/test_public_alpha.py`

## Verification

| Gate | Result |
| --- | --- |
| public-alpha smoke/docs/profile contracts | 2 passed repeatedly |
| full offline suite, warnings as errors | 1,041 passed in 40.38s on final snapshot |
| full coverage suite | 1,041 passed in 47.95s; 10,528 statements / 1,280 missed / 88% |
| tracked plus new Ruff | clean |
| mypy | 101 source files, clean |
| clean temporary project `init` + `validate` | valid |
| `intent`, `mcp`, `sync`, `drift`, and `write` help | exit 0 |
| diff check | clean |
| independent review | 0 Critical / 0 Important / 0 Minor; Ready |

The worktree root's pre-existing ignored `.intent/` is structurally invalid. It was not overwritten
with `intent init --force`; release validation is proven on the harness's clean temporary project.
Coverage source discovery includes the protected untracked `dogfood 2.py`, so the report records the
exact result without modifying or excluding that artifact. The five protected artifacts remain
byte-identical.
