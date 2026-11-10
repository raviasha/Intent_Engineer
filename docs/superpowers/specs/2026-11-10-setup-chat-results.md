# Initial setup results returned to the original agent

User clarification: separate setup chat and graph creation worked. The missing experience is
automatic transfer of findings and recommendations to the original active chat's agent.
Preserve setup progress but route its completed assessment back, without requiring user commands.

## Return contract

Capture the actual original host thread/host IDs and the separate opaque hook session ID
at setup preparation. Persist them in user-local onboarding metadata, never canonical graph
state or authorization evidence. Refuse destination rebinding. On completed, current assessment,
retain its run ID and return a packet with a stable update ID, findings/recommendations,
evidence, scope/limits and a host prompt. The runtime itself sends no messages.

The setup agent verifies human return-message authorization from trusted original chat history
(the setup offer includes it), then uses the host send_message_to_thread tool. Only after
confirmed send does it acknowledge the exact update ID. Before uncertain retries, inspect the
destination for that ID. Do not blindly resend delivered packets. Records are host-reported,
not proof of user approval. No implementation changes are authorized by findings.

If messaging cannot proceed, the original chat's next prompt hook requests automatic agent
retrieval and acknowledgement. Route only to the recorded opaque session; other chats stay quiet.
The user never has to run the retrieval command. Stale/revoked/incomplete results cannot be
handed off or acknowledged as current. Legacy setups without a destination cannot be silently
routed. The local runtime cannot wake terminated chats. Subsequent code workers remain silent.

## Approach

Use structured `chat_update` responses plus a required setup-chat output recipe.
This builds on the existing assessment completeness/freshness guard. Prompt-only
reminders were insufficient; delivery uses the existing authorized host messaging tool. The local runtime returns presentation data; it does
not claim it sent a message or wake a terminated chat. No new dashboard or forced
Stop-hook continuation is introduced.

The presentation includes a stable update ID, kind, plain-language status,
reviewed/total/unknown counts, ready findings with recommendations and evidence,
limitations, and the next action. Repeated status reads produce the same ID so the
host can suppress duplicate updates. Stale/incomplete/unavailable states never
claim completion. Unavailable results contain no cached private details. A ready
assessment without findings explicitly says no gaps were found within its scope.

Only public initial-setup CLI/MCP responses receive this payload. The underlying
assessment service and silent code-change worker remain unchanged. The handoff
requires an opening progress message, updates at actual milestones, and a final
summary without waiting for a user status question. Findings never authorize edits.

## Verification

Test start/checkpoint/finish and restart/status through the public CLI and MCP,
including stable IDs, no-findings, stale/incomplete and access loss. Verify that
code-change worker outputs remain silent. Exercise the host instructions with
controlled setup-agent scenarios. Publish a versioned marketplace update only
after source CI and all supported native packaging checks pass.
