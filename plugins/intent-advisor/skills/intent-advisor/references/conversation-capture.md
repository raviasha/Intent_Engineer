# Capture intent from the working conversation

Use after accepted setup, including an existing graph with no relevant source intent. Keep
first-use setup/deferral behavior intact. The working agent interprets the conversation and
calls the public tools; hooks provide the reminder, not independent semantic extraction.

For each feature discussion, planning turn, answer or changed constraint:

1. Read `intent_background_context(task, session_id)` and reuse the chat's prior answers and
   relevant source evidence. Follow source-refresh for dependent development. No configured
   sources, or no mention of this feature in successfully checked sources, does not prevent
   capturing the user's new intent. Unavailable sources are not evidence of absence; preserve
   that limitation and hold affected implementation while independent discussion continues.
2. Identify supported needs, goals, outcomes and constraints separately from candidate solutions
   and unanswered choices. The user's statements are evidence. Agent suggestions are not user
   decisions; “maybe” does not adopt a suggestion or resolve a conflict. Keep tentative options
   tentative rather than creating settled requirements. Consult relevant authoritative sources;
   preserve conflicts and ask for resolution instead of silently overriding them.
3. Persist the clear portion now with `intent_background_submit`, `classification=clear`, the
   exact current `user_statement`, current `graph_version`, supported nodes/links and real evidence
   refs. Paraphrases and inferred purpose use `source_mode=inferred` with confidence <=0.85 and
   a basis; explicit labels quote their source. Reuse stable IDs and existing nodes rather than
   duplicating them. Refer to earlier recorded chat evidence when deriving meaning across turns;
   do not relabel earlier statements or agent suggestions as the current user's exact words.
4. If a consequential gap remains, reread context after the mutation, then persist a separate
   `needs_clarification` or `conflicting` submission, with no mutations, at the current graph
   version. Cite the clear capture's evidence where relevant. Show the supported understanding
   and ask the returned question in this same chat. Reuse an existing unanswered question rather
   than recording duplicates. Independent work continues. No gap means no question or approval.
5. On the next answer, reuse the same session and exact `question_ref`, record the user's exact
   answer quote, and maintain the newly clarified meaning. An ambiguous reply leaves the original
   question unanswered: clarify it in chat without submitting an answer or resolved decision.
   Context after a successful answer must no longer list that question as unanswered. Do not ask
   the user to repeat an answer, run a command, visit an Inbox or approve the graph representation.
6. Check each tool result before saying intent was captured. Failed writes are not success;
   reread and re-evaluate on stale graph versions. Keep normal successful capture quiet. Surface
   only useful clarification, conflict or a persistence failure affecting the work.

## Submission identities within one host turn

Keep the hook session ID. Treat the hook turn ID as a base, not a reusable submission key.
Use stable distinct suffixes, such as `<turn>:refresh:1` for source interpretation,
`<turn>:capture:1` for clear chat meaning and `<turn>:question:1` for the remaining gap. Keep
these separate from the task refresh ID. An identical retry keeps the same ID and payload;
a different payload gets a new suffix. A stale rejected submission must be re-evaluated against
fresh context before a new attempt. Never reuse an already-successful ID for changed content.

Example: “Customers need offline reports; not sure CSV or PDF.” Capture the supported offline
report need first as inferred intent. Do not choose a format. In a separate submission ask
whether customers need spreadsheet analysis or a formatted document. When they answer “CSV,
for offline spreadsheet analysis,” record that answer and link the supported CSV requirement
to the existing need. The absence of this feature in Notion or Slack is not a reason to wait.

This workflow maintains the local graph only. It does not authorize editing source documents,
PRDs or code, and does not verify implementation correctness.
