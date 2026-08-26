# Task 4 Brief: Authorized Read-Side MCP Connector

Implement Task 4 of `docs/superpowers/plans/2026-08-25-intent-engineering-mcp-writeback.md` on `feat/public-alpha` from base `b54c209`.

## Outcome

Adapt one configured MCP profile object type into the repository's asynchronous `Connector` port. A successful run discovers and fetches provider objects, filters them through the local actor's provider-principal mapping, and emits immutable version-addressed evidence with provider-native authorship and ACLs intact. A durable checkpoint advances only after the complete authorized evidence prefix has been consumed.

## Required behavior

- A connector instance is scoped to one `McpConnectorConfig`, one profile object type, and one local actor. Its connector ID includes an actor/scope discriminator so a ledger produced for one actor cannot be replayed as authorized input for another.
- Validate the local binding through the shared `McpRuntime` at the start of each discovery generation. Invoke only the profile-declared operation and locally bound capability; never construct a parallel SDK or transport path.
- Support bounded `none`, `cursor`, and `page` discovery. Reject duplicate/cyclic cursors, duplicate discovered versions, malformed result shapes, and more than 128 pages or 10,000 fetched objects with fixed connector errors.
- Fetch each discovered reference through the profile's fetch operation, then select stable external ID, external version, provider-native author, UTC observed time, locator, optional parent, ACL, and semantic content using Task 1 selectors.
- Namespace external object and parent identities by profile ID. Hash only canonical selected semantic content, excluding transport envelopes. Evidence payloads contain the profile/version/object type plus selected semantic content, not the raw provider response.
- Preserve distinct same-object versions and authors as separate `EvidenceRecord`s. Do not use last-write-wins or collapse competing contributors.
- `authorize()` allows public evidence or a non-empty intersection between the local actor's mapped provider principals and the evidence ACL. A missing actor mapping or disjoint restricted ACL is denied. Denied provider objects never enter the connector cache, evidence store, graph reasoning, reports, or MCP resources.
- The checkpoint is strict canonical JSON bound to profile ID/version, object type, actor/scope identity, provider continuation state, and observed external object versions. It may advance only after a complete generation and the exact durable consumed evidence prefix. A later-page or fetch failure may return already-authorized records for immediate durable persistence, but finalization must fail and retain the prior checkpoint.
- Cancellation invalidates connector-owned generation state and propagates. All ordinary provider/profile/schema failures cross the public connector boundary as fixed, redacted `ConnectorError`s without retaining provider payloads in public exception arguments or connector state.
- This task is read-only. Do not implement graph approval policy, write plans, approvals, external mutation, continuous scheduling, CLI wiring, or reconciliation.

## TDD and review gates

1. Add the contract, real-orchestrator partial/retry, and ACL-filtering tests first; record the missing-module RED.
2. Implement the smallest authorization and connector modules needed for GREEN.
3. Run Task 4 focused tests, all MCP suites, secure/package tests, schema identity, Ruff, mypy, and the full offline suite.
4. Request independent read-only adversarial review before committing. Resolve every Critical and Important finding through new RED-to-GREEN tests.
5. Commit product/tests together, then commit this brief, `task-4-report.md`, and the progress ledger separately. Stop before Task 5.
