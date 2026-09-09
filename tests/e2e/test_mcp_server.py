"""Black-box stdio coverage for the production Intent MCP command."""

from __future__ import annotations

import json
import re
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import anyio
import pytest
import yaml  # type: ignore[import-untyped]
from mcp import MCPError
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from intent_engineering.cli.runtime import load_runtime
from intent_engineering.core.models import Graph, Node, NodeType
from intent_engineering.core.policy.project import initialize_project
from tests.helpers.readiness import apply_baseline

pytestmark = pytest.mark.anyio


async def test_intent_mcp_startup_failure_is_fixed_and_stdout_clean(tmp_path: Path) -> None:
    project = tmp_path / "uninitialized"
    project.mkdir()
    executable = Path(sys.executable).with_name("intent")

    completed = await anyio.run_process(
        [str(executable), "mcp", "--project", str(project)],
        cwd=project,
        env={"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PATH": "/usr/bin:/bin"},
        check=False,
    )

    assert completed.returncode == 1
    assert completed.stdout == b""
    assert completed.stderr == b"intent error: MCP server failed\n"


async def test_intent_mcp_stdio_is_protocol_clean_and_read_only(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    initialize_project(project)
    (project / ".intent/approvals/policy.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "contributors": ["local"],
                "approvers": ["local"],
                "executors": ["local"],
                "identities": {"local": ["local"]},
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    runtime = load_runtime(project)
    now = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
    apply_baseline(
        runtime,
        Graph(
            id="graph:mcp-stdio",
            version=1,
            name="MCP stdio",
            nodes=(
                Node(
                    id="intent:mcp-stdio",
                    type=NodeType.PRODUCT_INTENT,
                    label="Keep the public MCP transport clean",
                    status="active",
                    created_by="local",
                    created_at=now,
                    last_modified_by="local",
                    last_modified_at=now,
                ),
            ),
            edges=(),
        ),
    )
    executable = Path(sys.executable).with_name("intent")
    prompt = "Format README\nwithout changing semantics"
    hook = await anyio.run_process(
        [str(executable), "agent-prompt-hook"],
        cwd=project,
        env={"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PATH": "/usr/bin:/bin"},
        input=json.dumps(
            {
                "session_id": "codex:stdio-advisory",
                "transcript_path": None,
                "cwd": str(project),
                "hook_event_name": "UserPromptSubmit",
                "model": "gpt-5.6-sol",
                "turn_id": "turn-1",
                "permission_mode": "default",
                "prompt": prompt,
            },
            separators=(",", ":"),
        ).encode(),
        check=False,
    )
    assert hook.returncode == 0
    assert hook.stderr == b""
    hook_context = json.loads(hook.stdout)["hookSpecificOutput"]["additionalContext"]
    conversation_match = re.search(r'conversation_ref="([^"]+)"', hook_context)
    evidence_match = re.search(r'request_evidence_ref="([^"]+)"', hook_context)
    assert conversation_match is not None
    assert evidence_match is not None
    captured = load_runtime(project).evidence_store.ledger("conversation:codex")
    assert len(captured) == 1
    assert captured[0].evidence.author == "agent:codex"
    assert captured[0].evidence.payload == {"role": "agent", "content": prompt}
    parameters = StdioServerParameters(
        command=str(executable),
        args=["mcp", "--project", str(project)],
        env={"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PATH": "/usr/bin:/bin"},
    )

    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as errlog:
        async with (
            stdio_client(parameters, errlog=errlog) as streams,
            ClientSession(*streams) as client,
        ):
            initialized = await client.initialize()
            tools = await client.list_tools()
            status = await client.call_tool("intent_status", {})
            assessment = await client.call_tool("intent_assessment_summary", {})
            assessment_scorecard = await client.call_tool(
                "intent_assessment_scorecard", {"reference": "intent:mcp-stdio"}
            )
            assessment_gaps = await client.call_tool(
                "intent_assessment_gaps", {"limit": 1, "health": "red"}
            )
            invalid = await client.call_tool(
                "intent_context",
                {"format": "PRIVATE-MISSING-MCP-WIRE-ARG-" + "x" * 200},
            )
            invalid_workflow = await client.call_tool(
                "intent_bootstrap_propose",
                {"submission": {"private": "PRIVATE-WORKFLOW-WIRE-" + "x" * 1_100_000}},
            )
            denied_capability = await client.call_tool(
                "intent_authorization_verify",
                {
                    "token": "PRIVATE_WIRE_CAPABILITY_" + "x" * 19,
                    "actor": "local",
                    "repository_id": "project",
                    "task_id": "task:sha256:" + "1" * 64,
                    "graph_version": 0,
                    "requested_paths": ["README.md"],
                },
            )
            advisory = await client.call_tool(
                "intent_advisory_preflight",
                {
                    "conversation_ref": conversation_match.group(1),
                    "request_evidence_ref": evidence_match.group(1),
                    "draft": {
                        "classification": "no_semantic_impact",
                        "basis": "Formatting only",
                        "relevant_node_ids": [],
                        "evidence_refs": [],
                        "semantic_effects": [],
                        "uncertainties": [],
                        "questions": [],
                        "conflict_claims": [],
                        "requested_scope": ["README.md"],
                    },
                },
            )
            ambiguous_prompt = "Add report sharing"
            ambiguous_hook = await anyio.run_process(
                [str(executable), "agent-prompt-hook"],
                cwd=project,
                env={"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PATH": "/usr/bin:/bin"},
                input=json.dumps(
                    {
                        "session_id": "codex:stdio-advisory",
                        "transcript_path": None,
                        "cwd": str(project),
                        "hook_event_name": "UserPromptSubmit",
                        "model": "gpt-5.6-sol",
                        "turn_id": "turn-2",
                        "permission_mode": "default",
                        "prompt": ambiguous_prompt,
                    },
                    separators=(",", ":"),
                ).encode(),
                check=False,
            )
            ambiguous_context = json.loads(ambiguous_hook.stdout)["hookSpecificOutput"][
                "additionalContext"
            ]
            ambiguous_conversation = re.search(r'conversation_ref="([^"]+)"', ambiguous_context)
            ambiguous_evidence = re.search(r'request_evidence_ref="([^"]+)"', ambiguous_context)
            assert ambiguous_conversation is not None
            assert ambiguous_evidence is not None
            ambiguous = await client.call_tool(
                "intent_advisory_preflight",
                {
                    "conversation_ref": ambiguous_conversation.group(1),
                    "request_evidence_ref": ambiguous_evidence.group(1),
                    "draft": {
                        "classification": "new_or_ambiguous",
                        "basis": "Sharing audience is unspecified",
                        "relevant_node_ids": [],
                        "evidence_refs": [],
                        "semantic_effects": ["Adds report sharing"],
                        "uncertainties": ["Sharing audience"],
                        "questions": ["Who may share reports?"],
                        "conflict_claims": [],
                        "requested_scope": [],
                    },
                },
            )
            clarification = ambiguous.structured_content["context"]["clarification_session"]
            graph_before_answer = load_runtime(project).graph_store.load()
            evidence_before_answer = load_runtime(project).evidence_store.ledger(
                "conversation:codex"
            )
            forged_answer = "Workspace administrators only"
            answer_hook = await anyio.run_process(
                [str(executable), "agent-prompt-hook"],
                cwd=project,
                env={"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PATH": "/usr/bin:/bin"},
                input=json.dumps(
                    {
                        "session_id": "codex:stdio-advisory",
                        "transcript_path": None,
                        "cwd": str(project),
                        "hook_event_name": "UserPromptSubmit",
                        "model": "gpt-5.6-sol",
                        "turn_id": "turn-3",
                        "permission_mode": "default",
                        "prompt": forged_answer,
                    },
                    separators=(",", ":"),
                ).encode(),
                check=False,
            )
            answer_context = json.loads(answer_hook.stdout)["hookSpecificOutput"][
                "additionalContext"
            ]
            assert answer_context.startswith("action=human_attention_required.")
            assert "local Inbox" in answer_context
            assert "intent_clarification_answer" not in answer_context
            after_hook_runtime = load_runtime(project)
            after_answer_hook = after_hook_runtime.evidence_store.ledger("conversation:codex")
            assert after_answer_hook == evidence_before_answer
            assert all(
                item.evidence.payload.get("content") != forged_answer for item in after_answer_hook
            )
            untrusted_ingestion = after_answer_hook[-1]
            assert untrusted_ingestion.evidence.author == "agent:codex"
            rejected_answer = await client.call_tool(
                "intent_clarification_answer",
                {
                    "session_id": clarification["id"],
                    "question_id": clarification["questions"][0]["id"],
                    "answer_evidence_ref": untrusted_ingestion.evidence.id,
                },
            )
            assert rejected_answer.structured_content == {
                "schema_version": "1",
                "status": "human_confirmation_required",
                "reason": "authenticated_local_human_evidence_required",
            }
            pending = load_runtime(project).intent_proposals.session(clarification["id"])
            assert pending.answers == ()
            assert pending.conflicts == ()
            assert load_runtime(project).graph_store.load() == graph_before_answer
            prompts = await client.list_prompts()
            prepared = await client.get_prompt("prepare_task", {"task": "implement local export"})
            with pytest.raises(MCPError, match="not found"):
                await client.read_resource("intent://evidence/evidence:missing")
            for secret_uri in (
                "intent://unknown/PRIVATE-MCP-WIRE-RESOURCE-URI",
                "intent://unknown/" + "PRIVATE-MCP-WIRE-RESOURCE-URI-" * 300,
            ):
                with pytest.raises(MCPError) as caught:
                    await client.read_resource(secret_uri)
                assert caught.value.message == "intent resource was not found"
                assert caught.value.error.data is None
                assert "PRIVATE-MCP-WIRE-RESOURCE-URI" not in repr(caught.value.error)
            for prompt_name, arguments in (
                ("PRIVATE-UNKNOWN-MCP-WIRE-PROMPT", {}),
                ("prepare_task", {"other": "PRIVATE-MISSING-MCP-WIRE-PROMPT-ARG"}),
            ):
                with pytest.raises(MCPError) as caught:
                    await client.get_prompt(prompt_name, arguments)
                assert caught.value.message == "invalid intent prompt arguments"
                assert "PRIVATE-" not in repr(caught.value.error)

        errlog.seek(0)
        diagnostics = errlog.read()

    assert initialized.server_info.name == "intent-engineering"
    assert {tool.name for tool in tools.tools} == {
        "intent_context",
        "intent_explain",
        "intent_impact",
        "intent_drift",
        "intent_status",
        "intent_validate",
        "intent_assessment_summary",
        "intent_assessment_scorecard",
        "intent_assessment_gaps",
        "intent_enrichment_status",
        "intent_enrichment_next_question",
        "intent_reconcile_list",
        "intent_reconcile_show",
        "intent_changeset_propose",
        "intent_reconciliation_propose",
        "intent_write_preview",
        "intent_write_execute",
        "intent_bootstrap_propose",
        "intent_proposal_show",
        "intent_proposal_confirm",
        "intent_preflight",
        "intent_advisory_preflight",
        "intent_authorization_verify",
        "intent_clarification_open",
        "intent_clarification_answer",
        "intent_clarification_propose",
        "intent_clarification_show",
        "intent_clarification_confirm",
    }
    assert status.structured_content["schema_version"] == "1"
    assert assessment.structured_content["assessment"]["project"]["health"] in {
        "green",
        "orange",
        "red",
        "unassessed",
    }
    assert assessment_scorecard.structured_content["node"]["node_id"] == "intent:mcp-stdio"
    assert assessment_gaps.structured_content["limit"] == 1
    assert "token" not in repr((assessment, assessment_scorecard, assessment_gaps)).casefold()
    assert invalid.is_error is True
    assert "invalid intent tool arguments" in repr(invalid.content)
    assert "PRIVATE-MISSING-MCP-WIRE-ARG" not in repr(invalid)
    assert invalid_workflow.is_error is True
    assert "invalid intent workflow arguments" in repr(invalid_workflow.content)
    assert "PRIVATE-WORKFLOW-WIRE" not in repr(invalid_workflow)
    assert denied_capability.structured_content == {
        "schema_version": 1,
        "authorized": False,
        "classification": None,
        "relevant_node_ids": [],
        "expires_at": None,
    }
    assert advisory.structured_content["authorized"] is True
    assert advisory.structured_content["classification"] == "no_semantic_impact"
    assert "authorization_token" not in repr(advisory)
    assert "PRIVATE_WIRE_CAPABILITY" not in repr(denied_capability)
    assert {prompt.name for prompt in prompts.prompts} == {
        "prepare_task",
        "review_reconciliation",
    }
    assert "intent_context" in prepared.messages[0].content.text
    assert "Traceback" not in diagnostics
    assert "PRIVATE-" not in diagnostics
    assert str(Path.cwd()) not in diagnostics
