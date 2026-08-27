### Task 4: Expose bootstrap and source-role onboarding through CLI and MCP

**Base:** `ef1c2ea`

**Files:**
- Create: `src/intent_engineering/cli/intent_workflow.py`
- Create: `src/intent_engineering/integrations/mcp_server/intent_workflow.py`
- Modify: `src/intent_engineering/cli/app.py`
- Modify: `src/intent_engineering/integrations/mcp_server/server.py`
- Modify only as needed: `src/intent_engineering/cli/runtime.py`
- Test: `tests/e2e/test_cli_intent_bootstrap.py`
- Test: `tests/contract/mcp/test_intent_workflow_tools.py`

**Interfaces:**
- Consumes Task 3 `BootstrapService`, the exact held `Runtime`, existing Markdown capture path,
  terminal abstraction, `McpReadServices`, and `_IntentMCPServer` error boundaries.
- Produces CLI `intent bootstrap`, `intent sources add`, `intent proposals list|show|confirm`, and
  optional MCP tools `intent_bootstrap_propose`, `intent_proposal_show`,
  `intent_proposal_confirm` behind a provider-neutral `IntentWorkflowPort`.

## Required behavior

1. Write CLI E2E and official installed-SDK MCP contract tests first; witness the missing-command/tool
   RED before any production edit.
2. `intent bootstrap --prd <relative-path>` resolves a descriptor-rooted, nonblocking, regular,
   single-link project file; captures it immutably through the real Markdown connector/store; and
   returns a bounded `agent_submission_required` packet with evidence refs, declared-intent source
   role, repository/graph context, and no invented candidate/proposal. Graph and proposal ledger
   remain unchanged. Repeating capture is a semantic no-op.
3. Reject absolute/traversal/symlink/hardlink/FIFO/socket/device/oversize or swapped PRD paths
   promptly without partial evidence/checkpoint/config/graph mutation. Fixed public errors/output and
   repository traceback locals must retain no source contents or sensitive path material.
4. `intent sources add` validates an existing connector ID plus canonical scope, updates one exact
   `ProjectConfig.source_roles` assignment atomically through held project storage, preserves all
   unrelated config bytes/semantic fields, canonicalizes ordering, and is idempotent. Exact scope
   overrides inherited scope. Invalid connector/scope/role or concurrent config change fails closed.
5. `intent proposals list|show` use runtime ACL projection and detached bounded output. Terminally
   unavailable/unauthorized proposal is indistinguishable from not found. No raw proposal/evidence
   objects reach generic serializers.
6. `intent proposals confirm` displays the complete proposal digest, exact candidate ChangeSet,
   nodes/edges, confirmed core subset, provisional exclusions, assumptions/questions/conflicts, and
   current graph version before prompting. It requires a real interactive TTY and an exact explicit
   confirmation; non-TTY, stale, unauthorized, conflict/destructive, or changed proposal/config
   rejects before mutation. It delegates activation to Task 3 and never manufactures an approval.
7. MCP registration is optional and additive. With no workflow port, the existing MCP surface is
   byte/behavior compatible. Use bounded strict Pydantic request models and official SDK calls.
   Pre-handler missing/unknown/invalid arguments, handler errors, cancellation, and unmatched tool
   paths return fixed context-free errors and retain no raw submission/proposal/actor/source data in
   wire error fields, logs, or repository traceback locals.
8. MCP propose accepts only a typed detached Task 3 submission. Show and confirm delegate to the
   same production services and governance; do not register any approval-creation or external-write
   tool. Tool annotations must accurately describe read/persistence/destructive/idempotent behavior.
9. Owned clients/resources close on success, fixed failure, cancellation, and interrupt while
   preserving the original signal. CLI and MCP operations must use one held runtime/config snapshot
   per request and must not mix replacement workspace state.

## Required tests

- Clean existing project + ordinary relative PRD capture returns `agent_submission_required`, one
  immutable evidence version, declared-intent context, graph version 0, no proposal; second call is
  byte/semantic no-op.
- PRD special-file/path/swap/size matrix is bounded and fail-closed.
- Source-role add validates real connector/scope, preserves config fields, sorts/idempotently replays,
  proves exact override precedence, and rejects concurrent replacement without rewrite.
- Proposal list/show/confirm exercise real Task 2/3 stores and full-preview-before-prompt ordering;
  non-TTY and rejection paths mutate nothing.
- Official MCP API covers registration omitted/present, valid typed proposal, show, confirmation,
  invalid/missing/unknown/oversize arguments, cancellation, fixed wire payload/log/traceback secrecy,
  and absence of an approval-minting tool.
- Legacy CLI/MCP help and read surfaces remain unchanged when workflow services are omitted.

## Gates

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/e2e/test_cli_intent_bootstrap.py tests/contract/mcp/test_intent_workflow_tools.py tests/e2e/test_cli_local.py tests/e2e/test_mcp_server.py -q -W error
.venv/bin/intent bootstrap --help
.venv/bin/intent sources --help
.venv/bin/intent proposals --help
.venv/bin/ruff check src/intent_engineering/cli/intent_workflow.py src/intent_engineering/integrations/mcp_server/intent_workflow.py tests/e2e/test_cli_intent_bootstrap.py tests/contract/mcp/test_intent_workflow_tools.py
.venv/bin/mypy src
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin -q -W error
```

Write `task-4-report.md`, update `progress.md`, and pause unstaged at PRECOMMIT_REVIEW_READY. Commit
only after independent review, using `feat: expose reviewed intent onboarding`.
