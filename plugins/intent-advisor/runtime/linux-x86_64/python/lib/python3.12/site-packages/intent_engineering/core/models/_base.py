"""Shared fail-closed configuration for public semantic records."""

from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    """Reject undeclared input so canonical rewrites cannot discard user data."""

    model_config = ConfigDict(extra="forbid")
