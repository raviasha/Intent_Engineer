# Background intent workflow

The November 9 request replaces routine review-centric onboarding and advisory routing.
Intent Engineering stays in the development conversation. Each task gets a lightweight
relevance check; unrelated and aligned tasks proceed silently. Missing or unclear relevant
intent and specific conflicts receive one consequential question with cited understanding.
Reuse recorded answers. Code/test matching remains deferred.

First-use exception, clarified November 9: offer to create the graph in a separate chat
and connect Notion, Confluence, Slack or other selected sources. The original task continues.
The host creates the chat after acceptance; the CLI prepares and records the handoff without
pretending to create chats or authenticate providers. Source/account/scope questions and
provider authentication happen in the setup chat. Explicit deferral schedules an offer after
three distinct completed work turns. Running setup suppresses duplicate offers. Source reads
retain provider/object/revision/author/time/locator/content as immutable local evidence. Later
authorized reads can update intent, preserving source versions and disagreements. Continuous
remote polling is outside this change.

Once meaning is clear, maintain the local graph through typed, validated ChangeSets with
immutable evidence and history. No routine dashboard, proposal approval, WebAuthn, export,
or report. Explicit user statements and agent inferences remain distinguishable. A chat
statement is evidence, not an authenticated approval. Preserve disagreements and all old
pending proposals; never convert those proposals into approvals during migration.

Implementation choice: add a bounded conversational service and CLI/MCP surface using the
existing evidence store, transaction coordinator and graph applier. The hook supplies
background guidance without starting a dashboard or globally gating repository work.
The host agent supplies semantic relevance and typed interpretations; deterministic code
checks references, provenance, replay, baseline versions and mutation scope. Existing
governed/team publication and approval tools retain their contracts.

Updates to existing meaning require an explicit conversational decision tied to a prior
question, an exact user quote already specifying the revision, or an unambiguous newer
source revision from the same author with quoted old/new labels. Multi-source or changed-author
positions and chat overrides require clarification. Inferences cannot
overwrite explicit meaning. Existing cross-author disputes remain open and their positions
cannot be overwritten through background maintenance. Optional legacy review remains
available when explicitly requested.
