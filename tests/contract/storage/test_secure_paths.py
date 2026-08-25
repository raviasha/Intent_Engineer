"""Security regressions for descriptor-rooted canonical store I/O."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from intent_engineering.storage.jsonl.evidence_store import JsonlEvidenceStore
from intent_engineering.storage.yaml.graph_store import YamlGraphStore
from tests.contract.storage.test_evidence_store_contract import evidence_record
from tests.contract.storage.test_graph_store_contract import graph


def test_graph_store_rejects_a_final_component_symlink(tmp_path: Path) -> None:
    outside = tmp_path / "outside.yaml"
    YamlGraphStore(outside).initialize(graph())
    link = tmp_path / "graph.yaml"
    link.symlink_to(outside)

    with pytest.raises(ValueError, match="unsafe canonical path"):
        YamlGraphStore(link).load()


def test_evidence_store_rejects_a_hardlinked_canonical_file(tmp_path: Path) -> None:
    outside = tmp_path / "outside.jsonl"
    outside.write_text(
        json.dumps(evidence_record().model_dump(mode="json"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    linked = tmp_path / "evidence.jsonl"
    os.link(outside, linked)

    with pytest.raises(ValueError, match="unsafe canonical path"):
        JsonlEvidenceStore(linked)


def test_store_keeps_the_held_parent_when_the_path_is_swapped(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    path = state / "evidence.jsonl"
    store = JsonlEvidenceStore(path)

    held = tmp_path / "held-state"
    state.rename(held)
    attacker = tmp_path / "attacker"
    attacker.mkdir()
    state.symlink_to(attacker, target_is_directory=True)

    assert store.put(evidence_record()) is True
    assert (held / "evidence.jsonl").is_file()
    assert not (attacker / "evidence.jsonl").exists()
