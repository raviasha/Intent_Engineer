### Task 10: Prove the complete operating model and document adoption

**Base:** `049f158fdde65095f15494a8f4654c672e3adaa3`

**Approved-plan paths:**
- Create `tests/e2e/test_intent_aware_agent_workflow.py`
- Create `tests/e2e/intent_aware_agent_harness.py`
- Modify `tests/e2e/test_public_alpha.py`
- Modify `README.md`
- Modify `docs/mcp.md`
- Create `docs/intent-aware-agent.md`
- Modify `.github/workflows/intent-sync.yml`
- Modify `CONTRIBUTING.md`
- Create/update only Task 10 brief, report, and progress metadata outside this list.

This task is release proof and documentation, not a new product-feature round. Modify production code
only if the production-composition test exposes a concrete missing boundary required by Tasks 1–9;
add a focused RED first and keep any such change minimal.

## Controlling rulings

1. The user journey is: existing repository and PRD/sources -> reviewed intent/provenance graph ->
   every agent request classified -> aligned work authorized, ambiguous/new work clarified and
   proposed, conflicts independently reviewed -> post-task evidence linked -> scheduled capture and
   assurance report drift. Authorship, source version, predecessor, ACL, and human decisions remain
   explicit throughout.
2. Task 8 is final: installed Codex cannot prove mandatory coverage of every mutation path. No Codex
   plugin is shipped. `MandatoryHookUnavailable` is the truthful default. The provider-neutral host
   adapter may be demonstrated with a deterministic supported-host fixture, but docs and tests must
   not imply that Codex invokes preflight or post-task automatically.
3. Disabling integration leaves ordinary coding behavior unchanged. An operator may still invoke
   CLI/MCP preflight and post-task/assurance services manually or from a future supported host.
4. Background work may capture, validate, classify, and open evidence-backed cases. It never creates
   an approval, confirms a proposal, resolves a conflict, executes an external write, or silently
   changes declared intent/requirements.

## Required tests-first release proof

1. Create only both new E2E files first and run the exact focused command. Witness a real RED at the
   first absent integrated composition boundary before modifying existing tests/docs/workflows or
   production code.
2. Build one offline `IntentAwareAgentHarness` over one initialized project, one held `Runtime`, one
   evidence ledger, graph, history, cases, proposal/decision ledgers, authorization issuer, and one
   shared transaction coordinator. The harness may fake only external HTTP/MCP/model/supported-host
   and terminal boundaries. It must use real Tasks 1–9 services, real stores/executor, official
   in-memory MCP calls, real CLI entry points where the journey documents CLI, and the actual
   host-neutral adapter. Do not seed the main graph/case/result expected by the proof directly.
3. Start from an ordinary existing repository plus Markdown PRD. Run real bootstrap capture/review/
   activation and assert at least one confirmed core node plus provisional/reviewed content, exact
   evidence author/source/version/predecessor/ACL, graph version, history row, and byte-stable replay.
4. In the same state, ingest Git plus at least one real fake conversation connector revision from a
   second teammate. Preserve distinct versions/authors/predecessors; never collapse them through
   last-write-wins. Exercise source-role authority and verify MCP/CLI views read this same state.
5. Run a real aligned preflight, issue a process-local grant, authorize an exact supported-host
   mutation fixture before effect, then link real immutable Git commit and passing test-run evidence
   through `after_task`/`PostTaskService`. Assert one implementation claim/history change, exact
   requirement/code/test links, author aliases, changed-path/repository/commit bindings, and no token
   or proof in any durable/public representation.
6. Run a real new-or-ambiguous request. Persist its human/agent conversation turns, required questions
   and answers, exact chronology, proposal, contributor confirmation, and resulting graph/history.
   Run a conflicting request/source revision that creates an evidence-backed needs-human case and
   prove the proposer/author cannot self-review; a distinct authoritative reviewer is required.
7. Exercise mandatory Codex detection and assert the fixed `MandatoryHookUnavailable` result and no
   plugin directory. Separately show disabled host mode is a no-op. Do not fabricate a supported
   Codex hook event or token.
8. Run scheduled combined capture/assurance over the same state. Assert the first run persists real
   source versions, graph changes and/or cases as appropriate; a second identical run has zero
   semantic changes. Assert at least one of the eight Task 9 checks from real topology/evidence,
   stable fingerprints, terminal suppression, all-source visibility, and exact checkpoint behavior.
   Validate and render drift/MCP context from the same final state.
9. Prove provider-write governance non-vacuously through production composition: missing approval ->
   exact rejection and zero mutation; changed target -> exact rejection, one fresh read and zero
   mutation; success -> exactly one mutation, exact result, plan/approval IDs, receipt, and reviewer-
   authored write evidence. No injected service may bypass the production MCP/workflow composition.
10. Inject unique GitHub, Slack/MCP, Jira/write, request, authorization, and test-result sentinels
    through their real credential/request/evidence boundaries. Capture stdout, stderr, structured
    logs, fixed errors, and repository traceback locals. Scan all regular project files plus captured
    outputs/logs/errors; credentials/tokens/proofs and rejected private payloads must be absent. Do not
    use literals that never enter the tested boundary as the secrecy oracle.
11. Assert exact graph/history/case/evidence/proposal/decision consistency after each phase, no direct
    canonical-store fabrication for expected outcomes, deterministic deep-detached results, and
    byte-identical state after every rejected/no-op path.

## Documentation and adoption contract

1. `docs/intent-aware-agent.md` must be an executable, honest guide for:
   - install and `intent init --project .` in an existing repository;
   - point at a Markdown PRD, capture/review/confirm bootstrap, and assign source roles;
   - copy both provider profile and binding, configure actor aliases/credentials by reference, test
     the connector, and ingest Slack/Jira/Confluence/Notion-compatible sources;
   - inspect proposal, answer clarification, confirm as contributor, and require independent review
     for conflicts;
   - call preflight/context before a task and interpret aligned/new/conflicting/insufficient results;
   - the fixed unsupported mandatory-Codex result, the provider-neutral future-host seam, manual
     diagnostics, and how disabled mode behaves;
   - link post-task Git/test evidence; run frequent capture and separately scheduled drift/assurance;
   - review cases and keep external writes preview/approve/execute separated.
2. Every documented command must exist and pass `--help` or be an explicitly labeled example using a
   public API that is actually present. Do not document a CLI command for a service that has no CLI.
3. README keeps a concise existing-repo happy path and links the guide. `docs/mcp.md` retains exact
   project-neutral setup and copies both `profiles/mcp/<provider>.yaml` and its binding. State clearly
   that active-agent reasoning can submit proposals, scheduled semantic inference is optional, and
   confidence never authorizes canonical changes.
4. CONTRIBUTING documents the test-first/provenance/approval invariants and the exact offline release
   proof. No hosted service, universal provider support, OAuth, webhook, automatic Codex enforcement,
   or unattended external write claim is allowed.

## Scheduled workflow contract

Keep capture and assurance/reporting as visibly separate ordered steps: checkout, Python, install,
initialize/load existing state safely, validate, combined source sync, drift/assurance report, upload
artifact. Preserve manual + scheduled triggers, least permissions (`contents: read`, provider reads
only), bounded environment references, and deterministic source selection. The workflow must contain
no proposal confirmation, approval creation, conflict resolution, provider write preview/approve/
execute, shell credential echo, or repository write permission. Add an offline structural test for
the exact trigger, permissions, order, and forbidden commands.

## Required gates and handoff

Use the approved fast-safe cadence. Run focused tests while developing; near precommit run the
focused/broad suites, one fresh full offline warnings-as-errors suite, one coverage suite, Ruff
check/format, mypy, every documented help command, workflow structural test, and diff check. Record
exact counts, durations, statement/miss/coverage percent, source-file count, and whether a configured
coverage threshold exists. Coverage collection is evidence, not permission to add unrelated tests.

Request one independent adversarial review over the complete Tasks 1–10 operating proof. Fix grouped
Critical/Important findings tests-first, then run affected gates and one final finding-only review;
do not repeatedly expand adjacent scope. Pause unstaged at `PRECOMMIT_REVIEW_READY`. After Ready,
commit the exact release-proof/docs/workflow allowlist as `feat: complete intent-aware agent workflow`,
then commit only Task 10 report/progress metadata separately if the plan still requires separation.
Never stage the five protected artifacts.
