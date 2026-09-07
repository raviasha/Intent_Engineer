"""The CI boundary rejects writable hosts rather than guessing snapshot stability."""

from pathlib import Path

import pytest

from intent_engineering.intent_workflow.immutable_execution import (
    ImmutableExecutionGuard,
    ImmutableExecutionUnavailable,
)
from intent_engineering.storage.secure import SecureDirectory


def test_mutable_host_cannot_self_assert_ci_eligibility(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("INTENT_IMMUTABLE_EXECUTION", "true")
    monkeypatch.setenv("CI", "true")
    directory = SecureDirectory.open(tmp_path)
    try:
        with pytest.raises(ImmutableExecutionUnavailable, match="immutable execution unavailable"):
            ImmutableExecutionGuard(directory)
    finally:
        directory.close()
