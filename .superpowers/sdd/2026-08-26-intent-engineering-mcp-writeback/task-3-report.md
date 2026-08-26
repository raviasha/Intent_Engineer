# Task 3 — Reference collaboration provider profiles

Status: DONE. Task 4 was not started.

Product/tests commit: `ab6e85b` (`feat: add typed collaboration source profiles`).

## Outcome

Added strict version-1 semantic profiles for Slack, Notion, Jira, and Confluence, with complete illustrative local bindings and deterministic offline provider fixtures. The profiles consume the Task 1 schema/selectors and Task 2 connector configuration without implementing a provider SDK path, connector, checkpoint, authorization engine, planner, approval, or write executor.

The shipped matrices are:

- Slack: `message`, `thread`; `post_message`, `reply`, `update_message`.
- Notion: `page`, `block`; `update_page`, `append_blocks`.
- Jira: `issue`, `comment`; `update_issue`, `add_comment`.
- Confluence: `page`, `comment`; `update_page`, `add_comment`.

Every read object maps a stable provider object ID, version, version author, observed UTC time, locator, parent context, ACL principals, and selected semantic content. Provider fixtures contain two distinct primary authors, object-specific ACLs, and two earlier revisions of one stable object with distinct versions, version authors, and semantic hashes. Original-object authors remain selected content where the provider distinguishes original authorship from version authorship.

Every write mapping is declarative and guarded by a target selector, optimistic before-version selector, exact allowed fields, strict JSON Schema, explicit arguments, and result-version selector. Slack message, thread, and channel identities derive only from the version-guarded target; no caller field can redirect the operation to a different object. Notion nested property and block shapes reject unknown or empty structures.

Example bindings map every semantic read/write capability, validate against `McpConnectorConfig`, contain only `env:NAME` credential references, and intentionally use illustrative server command/tool names rather than claiming universal compatibility.

## TDD evidence

Initial tests-only RED:

```text
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest \
  -p anyio.pytest_plugin tests/contract/mcp/test_reference_profiles.py -v
16 failed in 0.19s
```

All failures were the expected fixed `ProfileValidationError` because the four profiles and fixture trees did not exist. Adding the four profile/binding/fixture slices produced the first GREEN: 16 passed in 0.27s.

## Review fix round 1

The first independent read-only review reported 0 Critical, 3 Important, and 1 Minor:

1. Slack post/reply identity fields could disagree with the precondition target, and update lacked a complete channel/message identity.
2. Notion nested `properties` and `children` shapes were unconstrained.
3. Fixtures had only one revision per stable object and therefore could not prove no-last-write-wins behavior.
4. ACL fixtures were syntactically nonempty but identical.

New tests were written before profile/fixture changes. The exact review selector produced 12 failures: four ACL distinctions, four absent revision fixtures, one Slack target-binding contract, and three Notion nested-schema cases. GREEN was 24/24 after:

- making Slack targets canonical channel/thread/message references and deriving all identity arguments from `target_id`;
- making Slack allowed fields content-only;
- defining exact nested Notion property/block schemas;
- adding two cross-author, same-object, different-version revisions for every provider;
- distinguishing original object authors from version authors where applicable; and
- making primary ACL fixtures author-specific.

## Review fix round 2

The second review reported 0 Critical, 1 Important, and 1 Minor:

1. Each primary snapshot reused a revision version while containing different author/content, which made version identity ambiguous.
2. Shared group principals made both example actors authorized for both primary objects.

New tests first asserted that primary versions cannot collide with revision versions, every revision author belongs to its ACL, and each example actor produces a distinct one-to-one ACL decision. The selector produced 8 failures. The fixtures now use three distinct versions per stable object (two historical revisions plus the primary snapshot), and example actor mappings contain only their provider-native account IDs. The focused suite returned 24/24.

Final independent re-review found 0 Critical, 0 Important, and 0 Minor.

## Files

- `profiles/mcp/{slack,notion,jira,confluence}.yaml`
- `profiles/mcp/example-bindings/{slack,notion,jira,confluence}.yaml`
- `tests/contract/mcp/test_reference_profiles.py`
- `tests/fixtures/mcp/{slack,notion,jira,confluence}/`

## Final verification

| Gate | Result |
| --- | --- |
| focused reference-profile contracts | 24 passed |
| Task 1–3 MCP + package + secure-path selection | 162 passed in 1.18s |
| full offline suite | 826 passed in 23.79s |
| tracked plus Task 3 Ruff | clean |
| Task 3 format check | clean |
| `.venv/bin/mypy src/intent_engineering` | 81 source files, no issues |
| profile schema identity | passed, checked-in bytes unchanged |
| `git diff --check` | clean |
| final independent review | 0 Critical / 0 Important / 0 Minor |

The five protected untracked artifacts retain their previously recorded SHA-256 values. Tests made no live provider calls, used no credentials, and did not inspect raw Git objects or open GUI applications.

## Compatibility boundary

These are typed semantic reference profiles and illustrative bindings for compatible MCP servers. Provider server tool names and normalized payload shapes vary; users must adapt bindings and, when necessary, versioned profiles to their selected server. Task 4 will consume these contracts through the shared runtime and add conservative ACL-aware, versioned evidence ingestion.
