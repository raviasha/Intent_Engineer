# Task 3 Brief: Reference Collaboration Provider Profiles

Implement Task 3 of `docs/superpowers/plans/2026-08-25-intent-engineering-mcp-writeback.md` on `feat/public-alpha` from base `e8c4090`.

## Outcome

Ship strict, versioned Slack, Notion, Jira, and Confluence semantic profiles, fixed offline provider fixtures, and illustrative local bindings. These data contracts must exercise Task 1's selectors/profile validation and Task 2's connector configuration without implementing the Task 4 read connector or any external mutation.

## Required behavior

- Provide exactly the read-object and write-operation matrices specified by the plan:
  - Slack: `message`, `thread`; `post_message`, `reply`, `update_message`.
  - Notion: `page`, `block`; `update_page`, `append_blocks`.
  - Jira: `issue`, `comment`; `update_issue`, `add_comment`.
  - Confluence: `page`, `comment`; `update_page`, `add_comment`.
- Every object maps complete discover/fetch operations plus stable external ID, version, provider-native author identity, observed time, locator, parent context where applicable, ACL principals, and non-empty semantic content.
- Preserve provider/workspace/account authorship rather than display-name-only attribution. Fixed fixtures must include multiple contributors so later connector tests can prove per-author provenance without last-write-wins behavior.
- Profiles remain provider-neutral semantic data. They must not import or execute a provider SDK, shell, template, expression, or external network call.
- Guarded writes declare a required target selector, required optimistic before-version selector, exact allowed fields, strict JSON Schema, explicit argument bindings, and required result-version selector. Profiles describe writes but never execute or approve them.
- Fixture-based content hashing must exclude transport envelopes while changing when selected semantic content changes.
- Example bindings must validate against their profile and `McpConnectorConfig`, map every semantic capability exactly once to illustrative server names, retain only `env:NAME` references, and state through naming/comments that users must adapt them to their chosen compatible MCP server.
- All fixtures use deterministic IDs and UTC timestamps and contain no live credentials, provider access, or claims of universal MCP-server compatibility.
- Do not implement the Task 4 connector, authorization engine, checkpoints, continuous ingestion loop, reconciliation, approval store, or write executor in this task.

## TDD and review gates

1. Write the full reference-profile contract matrix first and record the exact RED caused by absent profiles/fixtures.
2. Add the smallest complete profiles, fixtures, and bindings needed for GREEN.
3. Run the focused contract suite, Task 1 and Task 2 MCP suites, schema identity, Ruff, mypy, package tests, and the full offline suite.
4. Request independent read-only adversarial review before committing. Resolve all Critical and Important findings with new RED to GREEN regressions.
5. Commit product/tests together, then commit this brief, `task-3-report.md`, and the progress ledger separately. Stop before Task 4.
