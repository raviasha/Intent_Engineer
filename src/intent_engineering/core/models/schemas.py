"""Deterministic JSON Schema generation for public model contracts."""

from __future__ import annotations

import json
from collections.abc import Mapping

from pydantic import BaseModel

from intent_engineering.core.models.evidence import EvidenceRecord
from intent_engineering.core.models.graph import Graph
from intent_engineering.core.models.reconciliation import ReconciliationCase

_SCHEMA_MODELS: Mapping[str, type[BaseModel]] = {
    "Graph": Graph,
    "EvidenceRecord": EvidenceRecord,
    "ReconciliationCase": ReconciliationCase,
}


def schema_bytes(model_name: str) -> bytes:
    """Return canonical UTF-8 JSON Schema bytes for a supported public model."""
    try:
        model = _SCHEMA_MODELS[model_name]
    except KeyError as error:
        raise ValueError(f"unsupported schema model: {model_name}") from error
    return (
        json.dumps(model.model_json_schema(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
