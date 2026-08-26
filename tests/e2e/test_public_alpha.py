"""Executable public-alpha release proof."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from intent_engineering.capture.mcp import load_profile
from intent_engineering.capture.mcp.profile_loader import load_connector_config_bytes
from tests.e2e.public_alpha_harness import PublicAlphaHarness
from tests.helpers.cli import init_git_repo, run_intent

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
    profiles = (ROOT / "docs/provider-profiles.md").read_text(encoding="utf-8")
    examples = (ROOT / "examples/mcp-bindings/README.md").read_text(encoding="utf-8")

    for text in (readme, guide):
        assert "intent sync" in text
        assert "intent drift" in text
        assert "intent write approve" in text
        assert "unattended external writes" in text
    assert "polling" in guide
    assert "original author" in guide.lower()
    assert "--connector-id" in guide
    assert "--approval-id" in guide
    assert "last-write-wins" in readme
    assert "not a claim" in profiles
    assert "copy both" in examples
    assert "profiles/mcp/slack.yaml" in examples

    clean_project = init_git_repo(tmp_path)
    assert run_intent(clean_project, "init").returncode == 0
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
