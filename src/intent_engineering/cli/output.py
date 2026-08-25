"""Deterministic CLI rendering with a clean structured-output boundary."""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any

import typer
from pydantic import BaseModel


class OutputFormat(StrEnum):
    """The stable local output encodings supported by public commands."""

    TEXT = "text"
    JSON = "json"
    MARKDOWN = "markdown"


def normalize(value: object) -> Any:
    """Convert supported model values to JSON-safe builtin values."""
    if isinstance(value, BaseModel):
        return normalize(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {str(key): normalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize(item) for item in value]
    return value


def versioned(payload: object) -> dict[str, Any]:
    """Place every machine-readable payload in the version-one envelope."""
    value = normalize(payload)
    if isinstance(value, dict):
        return {"version": "1", **value}
    return {"version": "1", "result": value}


def emit(payload: object, output_format: OutputFormat = OutputFormat.TEXT) -> None:
    """Write one deterministic result to stdout and no operational logs."""
    value = versioned(payload)
    if output_format is OutputFormat.JSON:
        typer.echo(json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
        return
    if output_format is OutputFormat.MARKDOWN:
        typer.echo(
            "# Intent Engineering\n\n```json\n"
            + json.dumps(value, indent=2, sort_keys=True)
            + "\n```"
        )
        return
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
