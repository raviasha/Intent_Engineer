"""CLI contracts for local MCP connector discovery and diagnostics."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import traceback
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
import yaml  # type: ignore[import-untyped]
from typer.testing import CliRunner

from intent_engineering.capture.mcp import McpConnectorConfig
from intent_engineering.cli.app import app
from intent_engineering.cli.connectors import (
    ConnectorConfigurationError,
    connector_catalog,
    load_connector_catalog,
)
from intent_engineering.cli.runtime import load_runtime, run_selected_sync
from intent_engineering.core.models import EvidenceRecord, JsonValue, ProjectConfig
from intent_engineering.core.policy.project import initialize_project
from intent_engineering.sync.models import SyncRunResult, SyncRunStatus

ROOT = Path(__file__).resolve().parents[2]


@dataclass
class _FakeCatalog:
    tested: list[str]

    def summaries(self) -> tuple[dict[str, object], ...]:
        return (
            {
                "id": "slack-local",
                "profile_id": "slack",
                "profile_version": "1",
                "transport": "stdio",
                "enabled": True,
            },
        )

    def inspect(self, connector_id: str) -> dict[str, object]:
        assert connector_id == "slack-local"
        return {
            "id": connector_id,
            "read_operations": ["discover_messages", "fetch_message"],
            "write_operations": ["post_message"],
            "environment_names": ["SLACK_TOKEN"],
        }

    async def test(self, connector_id: str) -> dict[str, object]:
        self.tested.append(connector_id)
        return {
            "profile": "slack",
            "read_ready": True,
            "write_ready": True,
        }


def test_connector_commands_report_redacted_configuration_and_capabilities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from intent_engineering.cli import connectors

    catalog = _FakeCatalog([])
    monkeypatch.setattr("intent_engineering.cli.app._configure_logging", lambda: None)
    monkeypatch.setattr(connectors, "load_connector_catalog", lambda _project: catalog)
    runner = CliRunner()

    listed = runner.invoke(
        app, ["connectors", "list", "--project", str(tmp_path), "--format", "json"]
    )
    inspected = runner.invoke(
        app,
        ["connectors", "inspect", "slack-local", "--project", str(tmp_path), "--format", "json"],
    )
    tested = runner.invoke(
        app,
        ["connectors", "test", "slack-local", "--project", str(tmp_path), "--format", "json"],
    )

    assert listed.exit_code == inspected.exit_code == tested.exit_code == 0
    assert json.loads(listed.stdout)["connectors"][0]["id"] == "slack-local"
    assert json.loads(inspected.stdout)["environment_names"] == ["SLACK_TOKEN"]
    assert "env:" not in inspected.stdout
    assert json.loads(tested.stdout) == {
        "profile": "slack",
        "read_ready": True,
        "version": "1",
        "write_ready": True,
    }
    assert catalog.tested == ["slack-local"]


def _configured_project(tmp_path: Path, provider: str = "slack") -> Path:
    project = tmp_path / "project"
    project.mkdir()
    initialize_project(project)
    profile_dir = project / "profiles/mcp"
    profile_dir.mkdir(parents=True)
    (profile_dir / f"{provider}.yaml").write_bytes(
        (ROOT / f"profiles/mcp/{provider}.yaml").read_bytes()
    )
    loaded = yaml.safe_load(
        (ROOT / f"profiles/mcp/example-bindings/{provider}.yaml").read_text(encoding="utf-8")
    )
    config = McpConnectorConfig.model_validate(loaded)
    connector_dir = project / ".intent/connectors"
    assert connector_dir.is_dir()
    (connector_dir / f"{provider}.yaml").write_text(
        yaml.safe_dump(config.model_dump(mode="json"), sort_keys=True),
        encoding="utf-8",
    )
    return project


def test_catalog_loads_descriptor_safe_config_without_resolving_credentials(tmp_path: Path) -> None:
    project = _configured_project(tmp_path)

    catalog = load_connector_catalog(project)

    assert catalog.summaries() == (
        {
            "id": "slack-local",
            "profile_id": "slack",
            "profile_version": "1",
            "transport": "stdio",
            "enabled": True,
        },
    )
    inspected = catalog.inspect("slack-local")
    assert inspected["environment_names"] == ["SLACK_TOKEN"]
    assert "env:SLACK_TOKEN" not in json.dumps(inspected)


def test_catalog_rejects_symlinked_binding_without_reading_target(tmp_path: Path) -> None:
    project = _configured_project(tmp_path)
    connector = project / ".intent/connectors/slack.yaml"
    target = tmp_path / "private-binding.yaml"
    secret = "private-connector-secret"
    target.write_text(secret, encoding="utf-8")
    connector.unlink()
    connector.symlink_to(target)

    with pytest.raises(ConnectorConfigurationError) as caught:
        load_connector_catalog(project)

    rendered = "".join(traceback.format_exception(caught.value))
    assert secret not in rendered
    assert target.read_text(encoding="utf-8") == secret


def test_catalog_rejects_special_connector_yaml_entry(tmp_path: Path) -> None:
    project = _configured_project(tmp_path)
    fifo = project / ".intent/connectors/fake.yaml"
    os.mkfifo(fifo)
    try:
        with pytest.raises(ConnectorConfigurationError):
            load_connector_catalog(project)
    finally:
        fifo.unlink(missing_ok=True)


def test_catalog_rejects_fifo_profile_without_blocking(tmp_path: Path) -> None:
    project = _configured_project(tmp_path)
    profile = project / "profiles/mcp/slack.yaml"
    profile.unlink()
    os.mkfifo(profile)
    program = """\
from pathlib import Path
from intent_engineering.cli.connectors import (
    ConnectorConfigurationError, load_connector_catalog,
)
try:
    load_connector_catalog(Path(sys.argv[1]))
except ConnectorConfigurationError:
    raise SystemExit(0)
raise SystemExit(1)
"""
    try:
        completed = subprocess.run(
            [sys.executable, "-c", f"import sys\n{program}", str(project)],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except subprocess.TimeoutExpired as error:
        pytest.fail(f"FIFO connector profile load did not finish: {error}")
    finally:
        profile.unlink(missing_ok=True)

    assert completed.returncode == 0
    assert completed.stdout == completed.stderr == ""


def test_catalog_loads_profile_from_the_runtime_root_descriptor(tmp_path: Path) -> None:
    project = _configured_project(tmp_path)
    runtime = load_runtime(project)
    original = tmp_path / "held-project"
    project.rename(original)
    project.mkdir()
    replacement_profiles = project / "profiles/mcp"
    replacement_profiles.mkdir(parents=True)
    replacement = yaml.safe_load((ROOT / "profiles/mcp/slack.yaml").read_text())
    replacement["display_name"] = "REPLACEMENT PROFILE"
    (replacement_profiles / "slack.yaml").write_text(
        yaml.safe_dump(replacement, sort_keys=True), encoding="utf-8"
    )

    catalog = connector_catalog(runtime)

    assert catalog.configured[0].profile.display_name == "Slack"


@pytest.mark.anyio
async def test_production_connector_test_invokes_only_declared_reads(tmp_path: Path) -> None:
    project = _configured_project(tmp_path)
    calls: list[str] = []

    class ReadOnlyRuntime:
        async def validate_binding(self, _server: object, _binding: object) -> None:
            return None

        async def inspect_capabilities(
            self, _server: object
        ) -> tuple[frozenset[str], frozenset[str]]:
            return (
                frozenset(
                    {
                        "search_messages",
                        "get_message",
                        "search_threads",
                        "get_thread",
                        "post_message",
                        "reply_to_thread",
                        "update_message",
                    }
                ),
                frozenset(),
            )

        async def call(
            self, _server: object, name: str, _arguments: dict[str, JsonValue]
        ) -> JsonValue:
            calls.append(name)
            if name == "search_messages":
                return {"messages": [], "next_cursor": None}
            if name == "search_threads":
                return {"threads": [], "next_cursor": None}
            raise AssertionError(f"write or fetch capability invoked: {name}")

        async def read_resource(self, _server: object, _uri: str) -> JsonValue:
            raise AssertionError("Slack diagnostic must not read an undeclared resource")

    loaded = load_connector_catalog(project)
    catalog = type(loaded)(
        loaded.runtime,
        loaded.configured,
        mcp_runtime=ReadOnlyRuntime(),  # type: ignore[arg-type]
    )

    result = await catalog.test("slack-local")

    assert result == {"profile": "slack", "read_ready": True, "write_ready": True}
    assert calls == ["search_messages", "search_threads"]
    assert not {"post_message", "reply_to_thread", "update_message"}.intersection(calls)


def test_catalog_rejects_a_physical_tool_shared_by_read_and_write_semantics(
    tmp_path: Path,
) -> None:
    project = _configured_project(tmp_path)
    config_path = project / ".intent/connectors/slack.yaml"
    loaded = yaml.safe_load(config_path.read_text())
    loaded["binding"]["tools"]["discover_messages"] = loaded["binding"]["tools"][  # type: ignore[index]
        "post_message"
    ]
    config_path.write_text(yaml.safe_dump(loaded, sort_keys=True), encoding="utf-8")

    with pytest.raises(ConnectorConfigurationError):
        load_connector_catalog(project)


def test_cli_projection_accepts_configured_provider_principal_for_local_actor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _configured_project(tmp_path)
    config_path = project / ".intent/config.yaml"
    config = ProjectConfig.model_validate(yaml.safe_load(config_path.read_text()))
    config_path.write_text(
        yaml.safe_dump(
            config.model_copy(update={"local_actor": "local-asha"}).model_dump(mode="json"),
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    runtime = load_runtime(project)
    runtime.evidence_store.put(
        EvidenceRecord(
            id="mcp:slack:message:1",
            connector_type="mcp",
            external_object_id="message-1",
            external_version="1",
            author="U123",
            observed_at=datetime(2026, 8, 26, 12, 0, tzinfo=UTC),
            source_locator="mcp://slack/message-1",
            content_hash="sha256:" + "1" * 64,
            payload={"kind": "message"},
            acl=("U123",),
        )
    )
    runtime.evidence_store.put(
        EvidenceRecord(
            id="mcp:slack:message:2",
            connector_type="mcp",
            external_object_id="message-2",
            external_version="1",
            author="U123",
            observed_at=datetime(2026, 8, 26, 12, 1, tzinfo=UTC),
            source_locator="mcp://slack/message-2",
            content_hash="sha256:" + "2" * 64,
            payload={"kind": "message"},
            acl=("U123",),
        )
    )
    monkeypatch.setattr("intent_engineering.cli.app._configure_logging", lambda: None)
    from intent_engineering.cli import app as cli_app

    resolutions = 0
    real_principals = cli_app.configured_actor_principals

    def counted_principals(selected_runtime: object) -> frozenset[str]:
        nonlocal resolutions
        resolutions += 1
        return real_principals(selected_runtime)  # type: ignore[arg-type]

    monkeypatch.setattr(cli_app, "configured_actor_principals", counted_principals)

    result = CliRunner().invoke(app, ["status", "--project", str(project), "--format", "json"])

    assert result.exit_code == 0
    assert cast(dict[str, object], json.loads(result.stdout))["evidence_count"] == 2
    assert resolutions == 1


class _RecordingSync:
    def __init__(self) -> None:
        self.connector_ids: tuple[str, ...] = ()

    async def run(self, run_id: str, connectors: object) -> SyncRunResult:
        assert run_id == "run:combined"
        self.connector_ids = tuple(item.connector_id for item in connectors)  # type: ignore[union-attr]
        return SyncRunResult(
            run_id=run_id,
            status=SyncRunStatus.SUCCESS,
            connectors={},
            evidence_added=0,
            changes_applied=0,
            cases_created=0,
            duration_ms=0,
        )


@pytest.mark.anyio
async def test_mcp_selection_enters_one_combined_orchestrator_run(tmp_path: Path) -> None:
    from dataclasses import replace
    from typing import cast

    from intent_engineering.capture.base import Connector

    project = _configured_project(tmp_path)
    runtime = load_runtime(project)
    recording = _RecordingSync()
    selected = load_connector_catalog(project).read_connectors()

    result = await run_selected_sync(
        replace(runtime, sync=cast(object, recording)),  # type: ignore[arg-type]
        "markdown,git,mcp",
        "run:combined",
        mcp_connectors=cast(tuple[Connector, ...], selected),
    )

    assert result.status is SyncRunStatus.SUCCESS
    assert recording.connector_ids[:2] == ("markdown", "git")
    assert len(recording.connector_ids) == 4
