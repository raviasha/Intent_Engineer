"""Canonical protected workflow assets shared by setup and provider verification."""

from importlib.resources import files


def state_workflow() -> str:
    """Return the reviewed, packaged state-validator workflow."""
    return files("intent_engineering.integrations").joinpath("intent-state.yml").read_text("utf-8")


def check_workflow() -> str:
    """Return the reviewed, packaged code-check workflow."""
    return files("intent_engineering.integrations").joinpath("intent-check.yml").read_text("utf-8")
