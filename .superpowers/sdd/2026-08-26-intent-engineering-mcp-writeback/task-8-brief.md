# Task 8 Brief — Read-only Intent MCP server

Base: `09412aa`.

Implement the plan's versioned, read-only Intent MCP surface without starting Task 9 mutation
tools. The production `intent mcp` command must run the official MCP Python SDK v2 stdio server
with protocol-clean stdout. Register exactly the read tools `intent_context`, `intent_explain`,
`intent_impact`, `intent_drift`, `intent_status`, `intent_validate`, `intent_reconcile_list`, and
`intent_reconcile_show`; graph/evidence/schema/case/report resources; and the `prepare_task` and
`review_reconciliation` prompts.

Every response is schema-versioned and deterministic. Resolve one immutable authorization snapshot
from the configured local actor, provider principals, and authoritative policy aliases for every
request. Apply it before graph, evidence, case, context, drift, or impact serialization. An absent,
unauthorized, or terminally hidden object is indistinguishable from not found. Public errors are
fixed and contain no local path, persisted payload, credential, cause, context, or repository-frame
local. MCP tools remain read-only and may never create an approval, plan, receipt, ChangeSet, or
provider call.

Tests must use the real production server with the official SDK's in-memory or stdio transport,
remain offline, assert the exact tool/prompt/resource contract, prove ACL denial, prove byte-stable
workspace reads, and verify CLI stdio startup without stdout contamination. Preserve the five
authorized untracked artifacts byte-for-byte. Follow RED → GREEN, full/static verification, and an
independent read-only precommit review.
