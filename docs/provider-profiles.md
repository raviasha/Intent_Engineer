# Provider profiles and bindings

A provider profile is a versioned semantic contract. It declares object types, discovery/fetch
operations, identity/version/author/time/locator/content/ACL selectors, and guarded write schemas.
A local binding maps those semantic names to one compatible MCP server's concrete tools/resources,
scope, actor principals, transport, and environment references.

Profiles live under `profiles/mcp/`; the generated schema is
`schemas/mcp-provider-profile.schema.json`. Selectors are bounded declarative paths with explicit
missing-versus-null behavior. The fixed transform registry performs only documented coercions; no
profile can run Python, shell, templates, or `eval`. Inputs are strict JSON and detached/frozen at
the model boundary.

Bindings must match profile ID/version, declare every required capability, keep physical read and
write tools disjoint, and bind writes to the required target identity and optimistic version
precondition. Capability inspection validates the actual server before sync or write.

Supported transports are official-SDK stdio and Streamable HTTP. Stdio commands are direct argv,
never a shell; only safe inherited variables plus explicit `environment_refs` reach the child.
HTTP configuration rejects credential-bearing query strings and redirects. Resolved values must
never appear in profiles, graph/evidence, reports, logs, or errors.

ACLs are retained from provider data and projected fail-closed. `actor_principals` authenticates a
local teammate to provider principals for reads; write policy separately lists contributors,
approvers, and executors plus cross-provider person aliases. These are conservative local controls,
not enterprise RBAC.

Slack, Notion, Jira, and Confluence are tested reference contracts, not a claim that every server
uses the same tool names or response shapes. Copy an example from `examples/mcp-bindings/`, adapt
its non-secret names/scope, and run `intent connectors inspect/test` before enabling sync.
