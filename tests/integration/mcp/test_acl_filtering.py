"""Conservative local-principal authorization for collaboration evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest
import yaml  # type: ignore[import-untyped]

from intent_engineering.capture.mcp import McpConnectorConfig, load_profile
from intent_engineering.capture.mcp.authorization import AuthorizationDecision, authorize
from intent_engineering.capture.mcp.connector import McpConnector
from intent_engineering.core.models import JsonValue

ROOT = Path(__file__).resolve().parents[3]
PROFILES = ROOT / "profiles" / "mcp"


class OneMessageRuntime:
    def __init__(self, raw: dict[str, JsonValue]) -> None:
        self.raw = raw

    async def validate_binding(self, _server: object, _binding: object) -> None:
        return None

    async def call(
        self, _server: object, tool_name: str, arguments: dict[str, JsonValue]
    ) -> JsonValue:
        if tool_name == "search_messages":
            return {"messages": [{"id": self.raw["id"]}], "next_cursor": None}
        if tool_name == "get_message":
            return dict(self.raw)
        raise AssertionError(tool_name)

    async def read_resource(self, _server: object, _uri: str) -> JsonValue:
        raise AssertionError("unexpected resource")


def _config() -> McpConnectorConfig:
    loaded = yaml.safe_load(
        (PROFILES / "example-bindings" / "slack.yaml").read_text(encoding="utf-8")
    )
    assert type(loaded) is dict
    return McpConnectorConfig.model_validate(loaded)


def _message() -> dict[str, JsonValue]:
    loaded = json.loads(
        (ROOT / "tests" / "fixtures" / "mcp" / "slack" / "message.json").read_text()
    )
    assert type(loaded) is dict and type(loaded["raw"]) is dict
    return cast(dict[str, JsonValue], loaded["raw"])


def test_authorization_is_public_or_principal_intersection_only() -> None:
    mappings = {
        "local-asha": frozenset({"U123", "slack-group:ENG"}),
        "local-ben": frozenset({"U456"}),
    }

    assert authorize("local-unknown", mappings, frozenset()) is AuthorizationDecision.ALLOW
    assert authorize("local-asha", mappings, frozenset({"U123"})) is AuthorizationDecision.ALLOW
    assert authorize("local-ben", mappings, frozenset({"U123"})) is AuthorizationDecision.DENY
    assert (
        authorize("local-unknown", mappings, frozenset({"team-private"}))
        is AuthorizationDecision.DENY
    )


@pytest.mark.anyio
async def test_restricted_object_is_removed_before_the_connector_cache() -> None:
    raw = _message()
    connector = McpConnector(
        OneMessageRuntime(raw),
        config=_config(),
        profile=load_profile(PROFILES / "slack.yaml"),
        object_name="message",
        local_actor="local-ben",
    )

    discovered = await connector.discover(None)

    assert discovered == ()
    assert connector.cached_evidence_ids == ()


@pytest.mark.anyio
async def test_authorized_actor_keeps_provider_acl_and_authorship() -> None:
    raw = _message()
    connector = McpConnector(
        OneMessageRuntime(raw),
        config=_config(),
        profile=load_profile(PROFILES / "slack.yaml"),
        object_name="message",
        local_actor="local-asha",
    )

    discovered = await connector.discover(None)
    fetched = await connector.fetch(
        discovered[0].external_object_id,
        discovered[0].external_version,
    )
    record = connector.normalize(fetched)

    assert record.author == "U123"
    assert record.acl == ("U123", "slack-group:ENG")
