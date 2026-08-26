# Task 4 — ACL-aware MCP evidence connector

Status: DONE. Task 5 was not started in this slice.

Product/tests commit: `9954c73` (`feat: add acl-aware mcp evidence capture`).

## Outcome

Added the production read-side MCP connector for the versioned Slack, Notion, Jira, and Confluence profiles. One connector instance is bound to one server configuration, profile object type, local actor, mapped provider principals, and scope. It discovers only declared read operations, normalizes immutable authored evidence, filters unauthorized objects before caching or persistence, and advances a canonical checkpoint only after the exact durable evidence prefix is known.

The durable evidence envelope preserves provider author, observed time, source locator, external object/version lineage, ACL principals, semantic content hash, profile identity/version, object type, parent context, and full source/scope hashes. Connector identities bind independently recomputable profile, source, scope, and actor/principal digests. Full identities are used in deep validation; no truncated hash is treated as authoritative.

Later-page failures retain already persisted evidence while leaving the prior checkpoint bytes unchanged. A fresh connector replays the durable uncheckpointed prefix, completes the source, and becomes byte-stable on the following no-op run. Duplicate denied, unchanged, and accepted versions fail closed rather than collapsing author history.

Argumentized resources are validated through the official MCP v2 `list_resource_templates` capability, expanded only from declared bindings, and require URI-reserved placeholder boundaries so distinct argument tuples cannot collapse to one provider URI. Literal resources remain supported without implicit arguments.

Deep validation now authenticates partial uncheckpointed MCP associations, checkpoint cursor/profile/source/scope identity, exact consumed evidence prefixes, observed version maps, and MCP semantic content hashes. Public checkpoint decode failures use fixed, context-free errors and retain neither malformed strings nor decoded provider cursors in repository traceback locals.

This task is deliberately read-only. It does not create write plans, approvals, receipts, provider mutations, schedulers, or MCP mutation tools.

## TDD and review evidence

The initial tests-only RED failed at collection because `intent_engineering.capture.mcp.connector` and `authorization` did not exist. The first GREEN established the connector, ACL policy, canonical checkpoints, real-orchestrator partial recovery, and semantic validation.

The first independent review found transform-free null ACL/author handling, incomplete source scoping, noncanonical cursor acceptance, cache-dependent duplicate detection, argumentized-resource execution, and missing uncheckpointed association validation. Focused RED/GREEN cycles added exact regressions and brought the focused connector/validation suite to 49 passing tests.

The second review found five remaining Important gaps: partial evidence was not fully bound to profile/source identity, cursor validation used only a hash prefix, malformed public cursor decoding retained input, production capability validation ignored MCP resource templates, and multi-placeholder resource expansion was ambiguous. Six focused failures reproduced those defects. The fixes added full profile/source/scope identities, fixed redacted decode boundaries, official resource-template enumeration, and reserved-delimiter URI templates.

The final narrow review found one scoped-mismatch traceback leak through a decoded checkpoint local. Its exact regression failed first, then passed after scope authentication moved behind the non-raising result boundary. Final independent review: 0 Critical / 0 Important; Ready.

## Files

- `src/intent_engineering/capture/mcp/authorization.py`
- `src/intent_engineering/capture/mcp/connector.py`
- `src/intent_engineering/capture/mcp/{__init__,profile_models,runtime,session}.py`
- `src/intent_engineering/validation/service.py`
- `tests/fakes/mcp_session.py`
- `tests/contract/capture/test_mcp_connector_contract.py`
- `tests/integration/mcp/test_acl_filtering.py`
- `tests/integration/mcp/test_read_sync.py`
- `tests/unit/capture/mcp/test_runtime.py`

## Final verification

| Gate | Result |
| --- | --- |
| focused MCP + validation selection | 208 passed |
| full offline suite | 861 passed in 29.27s |
| tracked plus Task 4 Ruff | clean |
| `.venv/bin/mypy src` | 83 source files, no issues |
| `git diff --check` | clean |
| final independent review | 0 Critical / 0 Important; Ready |

The five protected untracked artifacts remained untouched. Tests made no live provider calls, used no credentials, and did not inspect raw Git objects or open GUI applications.

## Compatibility boundary

Provider bindings remain explicit local compatibility contracts. Changing a profile, bound operation, server descriptor, scope, actor, or principal set changes the connector identity and prevents replay under the old ledger. Evidence captured through a different source contract with the same external version fails closed rather than silently aliasing provenance.
