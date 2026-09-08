"""Executable public-alpha release proof."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest
import yaml  # type: ignore[import-untyped]

from intent_engineering.capture.mcp import load_profile
from intent_engineering.capture.mcp.profile_loader import load_connector_config_bytes
from intent_engineering.intent_workflow.models import TaskClassification
from tests.e2e.public_alpha_harness import PublicAlphaHarness
from tests.helpers.cli import init_git_repo, run_intent
from tests.integration.mcp.test_read_sync import McpSyncHarness

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def public_alpha(tmp_path: Path):
    harness = PublicAlphaHarness(tmp_path)
    try:
        yield harness
    finally:
        harness.close()


def test_public_alpha_smoke(public_alpha: PublicAlphaHarness) -> None:
    initialized = public_alpha.init()
    assert initialized.returncode == 0
    assert public_alpha.validate()["valid"] is True

    first, second = public_alpha.sync_twice()
    assert first["evidence_added"] > 0
    assert first["changes_applied"] > 0
    assert first["cases_created"] > 0
    assert second["evidence_added"] == 0
    assert second["changes_applied"] == 0
    assert public_alpha.authors() >= {"octocat", "U123"}
    assert public_alpha.case_types() >= {"CONFLICTING_SOURCES"}
    assert public_alpha.validate()["valid"] is True

    revision = public_alpha.sync_conversation_revision()
    assert revision["evidence_added"] == 1
    versions = public_alpha.conversation_versions()
    assert [author for author, _ in versions] == ["U123", "U456"]
    assert versions[0][1] is None
    assert versions[1][1] is not None
    assert "CONFLICTING\\_SOURCES" in public_alpha.drift()

    context = public_alpha.mcp_context("local export")
    assert context["schema_version"] == "1"
    preview = public_alpha.external_write_preview(public_alpha.review_case_id())
    assert preview["plan_hash"].startswith("sha256:")
    missing = public_alpha.execute_without_approval(preview["plan_id"])
    assert missing["status"] == "rejected"
    assert missing["provider_calls"] == 0
    approval = public_alpha.interactive_approve(preview["plan_id"])
    assert approval["actor"] != preview["created_by"]
    success = public_alpha.execute(preview["plan_id"], approval["id"])
    assert success["status"] == "succeeded"
    assert success["provider_mutations"] == 1
    assert success["plan_id"] == preview["plan_id"]
    assert success["approval_id"] == approval["id"]
    assert success["evidence_author"] == "local:reviewer"
    assert success["evidence_plan_id"] == preview["plan_id"]
    changed = public_alpha.changed_target_rejection()
    assert changed["status"] == "rejected"
    assert changed["provider_mutations"] == 0
    assert public_alpha.persisted_sentinels() == ()


def test_public_alpha_docs_and_bindings_match_the_shipped_operating_model(
    tmp_path: Path,
) -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    guide = (ROOT / "docs/mcp.md").read_text(encoding="utf-8")
    adoption = (ROOT / "docs/intent-aware-agent.md").read_text(encoding="utf-8")
    github = (ROOT / "docs/github.md").read_text(encoding="utf-8")
    guided_spec = (
        ROOT / "docs/superpowers/specs/2026-08-28-guided-onboarding-plugin-design.md"
    ).read_text(encoding="utf-8")
    profiles = (ROOT / "docs/provider-profiles.md").read_text(encoding="utf-8")
    examples = (ROOT / "examples/mcp-bindings/README.md").read_text(encoding="utf-8")

    for text in (readme, guide, adoption):
        assert "intent sync" in text
        assert "intent drift" in text
        assert "intent write approve" in text
        assert "unattended external writes" in text
    for command in (
        "intent init --project .",
        "intent bootstrap --prd docs/prd.md",
        "intent sources add",
        "intent connectors test",
        "intent proposals show",
        "intent proposals confirm",
        "intent preflight --task",
        "intent context --task",
        "intent reconcile resolve",
    ):
        assert command in adoption
    for truth in (
        "agent_submission_required",
        "MandatoryHookUnavailable",
        "Codex mandatory mutation hook is unavailable",
        "disabled",
        "new_or_ambiguous",
        "conflicting",
        "insufficient evidence",
        "does not mint",
        "optional",
        "confidence",
        "ClarificationCoordinator",
    ):
        assert truth in adoption
    assert "insufficient_evidence" not in adoption
    assert {item.value for item in TaskClassification} == {
        "aligned",
        "conflicting",
        "new_or_ambiguous",
        "no_semantic_impact",
    }
    assert adoption.index("intent onboard --project . --prd docs/PRD.md") < adoption.index(
        "intent_bootstrap_propose"
    )
    for text in (readme, guide, adoption):
        assert (
            "git clone https://github.com/raviasha/Intent_Engineer.git "
            "/absolute/path/to/Intent_Engineer"
        ) in text
        assert "python -m pip install /absolute/path/to/Intent_Engineer" in text
        assert "codex plugin marketplace add /absolute/path/to/Intent_Engineer" in text
        assert "codex plugin add intent-advisor@intent-engineering-local" in text
        assert "cd /absolute/path/to/target-repository" in text
        assert "new task" in text.casefold() or "restart" in text.casefold()
        assert "wheel-only" in text.casefold()
        assert "CLI and MCP" in text
        assert "plugin marketplace" in text.casefold()
        assert "codex plugin marketplace add ." not in text
        assert "separate terminal" not in text.casefold()
        assert "launches the plugin-owned MCP server" in text
        assert "agent:codex" in text
        assert "human_confirmation_required" in text
        assert "non-MCP local human" in text
        assert "no CLI command" in text or "no shipped CLI command" in text
        assert "exact human prompt as attributed" not in text
        assert "human-authored evidence" not in text
    assert "clients that launch and connect stdio themselves" in guide
    assert "intent validate --project ." in github
    assert "intent sync --project . --sources markdown,git,github" in github
    assert "intent drift --project . --format markdown --output intent-drift.md" in github
    assert "approved baseline" in github.casefold()
    assert "clean checkout" in github.casefold()
    assert "no_semantic_impact" in guided_spec
    assert "non_requirement" not in guided_spec
    assert "| `mechanical` |" not in guided_spec
    assert "untrusted `agent:codex` evidence" in guided_spec
    assert "independently authenticated non-MCP local human" in guided_spec
    assert "intent sources add markdown" in adoption
    assert "intent sources add '<source-role-connector-id>'" in adoption
    assert "polling" in guide
    assert "original author" in guide.lower()
    assert "--connector-id" in guide
    assert "--approval-id" in guide
    assert "last-write-wins" in readme
    assert "not a claim" in profiles
    assert "copy both" in examples
    assert "profiles/mcp/slack.yaml" in examples

    clean_project = init_git_repo(tmp_path)
    (clean_project / "docs").mkdir()
    (clean_project / "docs/prd.md").write_text(
        "# Local export\n\nExports remain local by default.\n",
        encoding="utf-8",
    )
    assert run_intent(clean_project, "init").returncode == 0
    bootstrap = run_intent(
        clean_project,
        "bootstrap",
        "--prd",
        "docs/prd.md",
        "--project",
        ".",
        "--format",
        "json",
    )
    assert bootstrap.returncode == 4, bootstrap.stderr
    assert bootstrap.json()["status"] == "agent_submission_required"
    prd_role = run_intent(
        clean_project,
        "sources",
        "add",
        "markdown",
        "docs/prd.md",
        "--role",
        "declared_intent",
        "--project",
        ".",
        "--format",
        "json",
    )
    assert prd_role.returncode == 0, prd_role.stderr
    diagnostic = run_intent(
        clean_project,
        "preflight",
        "--task",
        "inspect local export",
        "--format",
        "json",
    )
    assert diagnostic.returncode == 0, diagnostic.stderr
    assert diagnostic.json()["mode"] == "diagnostic"
    assert diagnostic.json()["authorization_issued"] is False
    assert "token" not in diagnostic.stdout
    (clean_project / "profiles/mcp").mkdir(parents=True)
    shutil.copy2(ROOT / "profiles/mcp/slack.yaml", clean_project / "profiles/mcp/slack.yaml")
    shutil.copy2(
        ROOT / "examples/mcp-bindings/slack.yaml",
        clean_project / ".intent/connectors/slack.yaml",
    )
    inspection = run_intent(
        clean_project,
        "connectors",
        "inspect",
        "slack-local",
        "--format",
        "json",
    )
    assert inspection.returncode == 0, inspection.stderr
    assert inspection.json()["profile_id"] == "slack"

    for provider in ("slack", "notion", "jira", "confluence"):
        binding = load_connector_config_bytes(
            (ROOT / f"examples/mcp-bindings/{provider}.yaml").read_bytes()
        )
        profile = load_profile(ROOT / binding.profile_path)
        binding.binding.validate_against(profile)
        rendered = (ROOT / f"examples/mcp-bindings/{provider}.yaml").read_text()
        assert f"env:{provider.upper()}_TOKEN" in rendered
        assert "test_secret" not in rendered


def test_review_fix_public_cli_uses_canonical_mcp_authority_identity(
    tmp_path: Path,
) -> None:
    project = init_git_repo(tmp_path)
    assert run_intent(project, "init").returncode == 0
    config_path = project / ".intent/config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["local_actor"] = "local-asha"
    config_path.write_text(yaml.safe_dump(config, sort_keys=True), encoding="utf-8")
    (project / "profiles/mcp").mkdir(parents=True)
    shutil.copy2(ROOT / "profiles/mcp/slack.yaml", project / "profiles/mcp/slack.yaml")
    shutil.copy2(
        ROOT / "examples/mcp-bindings/slack.yaml",
        project / ".intent/connectors/slack.yaml",
    )

    captured = McpSyncHarness(tmp_path / "captured-slack")
    actual_connector = captured.connector()
    result = asyncio.run(captured.orchestrator.run("capture", (actual_connector,)))
    assert result.evidence_added == 1
    record = captured.evidence_store.ledger(
        actual_connector.connector_id,
        connector_type="mcp",
    )[0].evidence

    listed = run_intent(project, "connectors", "list", "--format", "json")
    inspected = run_intent(
        project,
        "connectors",
        "inspect",
        "slack-local",
        "--format",
        "json",
    )
    assert listed.returncode == inspected.returncode == 0
    listed_ids = listed.json()["connectors"][0]["source_role_connector_ids"]
    inspected_ids = inspected.json()["source_role_connector_ids"]
    assert listed_ids == inspected_ids
    assert inspected_ids["message"] == actual_connector.connector_id

    ambiguous = run_intent(
        project,
        "sources",
        "add",
        "slack-local",
        record.source_locator,
        "--role",
        "proposed_intent",
        "--format",
        "json",
    )
    assert ambiguous.returncode == 1
    configured = run_intent(
        project,
        "sources",
        "add",
        actual_connector.connector_id,
        record.source_locator,
        "--role",
        "proposed_intent",
        "--format",
        "json",
    )
    assert configured.returncode == 0, configured.stderr
    assert configured.json()["source_role"]["connector_id"] == actual_connector.connector_id
    persisted = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert persisted["source_roles"] == [
        {
            "connector_id": actual_connector.connector_id,
            "inherited": False,
            "role": "proposed_intent",
            "scope": record.source_locator,
        }
    ]


def test_scheduled_workflow_is_read_only_and_orders_capture_before_assurance() -> None:
    workflow_path = ROOT / ".github/workflows/intent-sync.yml"
    raw = workflow_path.read_text(encoding="utf-8")
    workflow = yaml.safe_load(raw)

    assert set(workflow["on"]) == {"schedule", "workflow_dispatch"}
    assert workflow["on"]["schedule"] == [{"cron": "17 2 * * *"}]
    job = workflow["jobs"]["drift"]
    assert job["permissions"] == {
        "contents": "read",
        "issues": "read",
        "pull-requests": "read",
    }
    steps = job["steps"]
    assert steps[0] == {
        "uses": "actions/checkout@11d5960a326750d5838078e36cf38b85af677262",
        "with": {"fetch-depth": 0, "persist-credentials": False},
    }
    assert steps[1] == {
        "uses": "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065",
        "with": {"python-version": "3.12"},
    }
    assert [step["name"] for step in steps[2:7]] == [
        "Install Intent Engineering",
        "Restore verified approved intent state",
        "Capture configured source versions",
        "Validate captured canonical state",
        "Render drift and assurance report",
    ]
    assert steps[7] == {
        "uses": "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02",
        "with": {
            "name": "intent-drift",
            "path": "intent-drift.md",
            "retention-days": 7,
            "if-no-files-found": "error",
        },
    }
    commands = [step.get("run", "") for step in steps]
    assert commands[2] == "python -m pip install ."
    assert commands[3] == "python -m intent_engineering.integrations.github_action restore"
    assert commands[4] == "intent sync --project . --sources markdown,git,github"
    assert commands[5] == "intent validate --project ."
    assert commands[6] == ("intent drift --project . --format markdown --output intent-drift.md")
    assessment_index = commands.index(
        "intent assess --project . --format json > intent-assessment.json"
    )
    review_index = commands.index("intent check --require-review")
    assert assessment_index < review_index
    assert steps[review_index - 1]["name"] == "Gate assessment against exact comparison base"
    assert steps[4]["env"] == {
        "GH_TOKEN": "${{ secrets.GITHUB_TOKEN }}",
        "GITHUB_REPOSITORY": "${{ github.repository }}",
    }
    forbidden = (
        "proposals confirm",
        "reconcile resolve",
        "write preview",
        "write approve",
        "write execute",
        "intent init",
        "contents: write",
        "echo $",
    )
    assert not any(value in raw for value in forbidden)


def test_guided_adoption_docs_and_plugin_independent_assurance_are_ordered() -> None:
    journey_markers = (
        "git clone https://github.com/raviasha/Intent_Engineer.git",
        "python -m pip install /absolute/path/to/Intent_Engineer",
        "codex plugin marketplace add /absolute/path/to/Intent_Engineer",
        "cd /absolute/path/to/target-repository",
        "intent onboard --project . --prd docs/PRD.md --yes",
        "Approve the baseline",
        "ordinary prompts",
        "Clarification and review",
        "Scheduled CLI assurance",
    )
    for path in (ROOT / "README.md", ROOT / "docs/intent-aware-agent.md", ROOT / "docs/mcp.md"):
        text = path.read_text(encoding="utf-8")
        positions = tuple(text.index(marker) for marker in journey_markers)
        assert positions == tuple(sorted(positions)), path
        assert "plugins/intent-advisor" in text
        assert "intent_advisory_preflight" in text
        assert "MandatoryHookUnavailable" in text
        assert "codex plugin marketplace add /absolute/path/to/Intent_Engineer" in text
        assert "codex plugin add intent-advisor@intent-engineering-local" in text
        assert "codex plugin marketplace add ." not in text
        assert "wheel-only" in text.casefold()

    marketplace = json.loads(
        (ROOT / ".agents/plugins/marketplace.json").read_text(encoding="utf-8")
    )
    assert marketplace["name"] == "intent-engineering-local"
    assert marketplace["plugins"] == [
        {
            "name": "intent-advisor",
            "source": {"source": "local", "path": "./plugins/intent-advisor"},
            "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
            "category": "Developer Tools",
        }
    ]

    workflow_path = ROOT / ".github/workflows/intent-sync.yml"
    raw = workflow_path.read_text(encoding="utf-8")
    workflow = yaml.safe_load(raw)
    assert set(workflow["on"]) == {"schedule", "workflow_dispatch"}
    steps = workflow["jobs"]["drift"]["steps"]
    assert steps[0] == {
        "uses": "actions/checkout@11d5960a326750d5838078e36cf38b85af677262",
        "with": {"fetch-depth": 0, "persist-credentials": False},
    }
    assert steps[1] == {
        "uses": "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065",
        "with": {"python-version": "3.12"},
    }
    assert [step.get("run") for step in steps[2:7]] == [
        "python -m pip install .",
        "python -m intent_engineering.integrations.github_action restore",
        "intent sync --project . --sources markdown,git,github",
        "intent validate --project .",
        "intent drift --project . --format markdown --output intent-drift.md",
    ]
    assert "intent-advisor" not in raw
    assert "plugins/" not in raw
    assert "codex" not in raw.casefold()
    assert not (ROOT / ".github/workflows/intent-engineering.yml").exists()
    scheduled = [
        path
        for path in (ROOT / ".github/workflows").glob("intent-*.yml")
        if "schedule:" in path.read_text(encoding="utf-8")
    ]
    assert scheduled == [workflow_path]
