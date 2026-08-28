# Task 10 Brief — Public-alpha release proof

Base: `46e5e3d`.

Complete executable documentation and one offline end-to-end proof for the user-approved operating
model:

1. install the package and initialize a project once;
2. configure local, GitHub, and compatible MCP conversation sources without persisting credentials;
3. run `intent sync` manually or on a desired schedule to append source-authored immutable evidence
   versions and update derived graph/case state;
4. run drift/reconciliation reporting on its own desired schedule to compare intent, requirements,
   code, and tests;
5. let every authorized teammate contribute under authenticated local/provider aliases while
   preserving original author, timestamp, source, version, evidence references, and competing diffs;
6. require an independent authorized human for conflict/destructive resolution and require exact
   preview plus separate interactive approval for external provider writes.

Documentation must distinguish the shipped polling/scheduled workflow from unshipped webhooks,
hosted ingestion, collaboration UI, and unattended writes. Provider profiles are semantic
compatibility contracts, not universal compatibility claims. Examples contain environment
references only, never credentials.

The offline release harness must compose production initialization, runtime/store/orchestrator,
GitHub and MCP connector, MCP server, planner/approval/executor, validation, and rendering services;
only external HTTP/MCP sessions and the interactive terminal are fakes. It proves first sync,
idempotent second sync, multiple author identities/versions, preserved conflicting evidence,
review cases, MCP context, missing-approval rejection, separate-person approval, successful exact
write, changed-target rejection with zero mutation, and absence of sentinel credentials from every
regular project file and captured output. Follow RED → GREEN, full/static gates, and independent
read-only review before commit.
