"""Security regressions for descriptor-rooted canonical store I/O."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from intent_engineering.storage import secure
from intent_engineering.storage.jsonl.evidence_store import JsonlEvidenceStore
from intent_engineering.storage.secure import SecureDirectory, UnsafePathError
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


def test_membership_scan_stat_to_fifo_swap_fails_without_blocking_or_mutation(
    tmp_path: Path,
) -> None:
    connector_directory = tmp_path / "connectors"
    connector_directory.mkdir()
    target = connector_directory / "race.yaml"
    target.write_bytes(b"binding: safe\n")
    program = """\
import os
import stat
import sys
from pathlib import Path
import intent_engineering.storage.secure as secure
directory = secure.SecureDirectory.open(Path(sys.argv[1]))
original_stat = secure.os.stat
swapped = False
def swap_after_stat(path, *args, **kwargs):
    global swapped
    metadata = original_stat(path, *args, **kwargs)
    if path == "race.yaml" and kwargs.get("follow_symlinks") is False and not swapped:
        swapped = True
        directory_fd = kwargs["dir_fd"]
        os.unlink(path, dir_fd=directory_fd)
        os.mkfifo(path, dir_fd=directory_fd)
    return metadata
secure.os.stat = swap_after_stat
try:
    directory.walk_regular_files_bounded(
        ".yaml",
        max_files=256,
        max_depth=8,
        max_file_bytes=1_048_576,
        max_total_bytes=8_388_608,
        reject_symlinks=True,
    )
except secure.UnsafePathError:
    raise SystemExit(0)
finally:
    directory.close()
raise SystemExit(1)
"""
    after: os.stat_result | None = None
    try:
        completed = subprocess.run(
            [sys.executable, "-c", program, str(connector_directory)],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
        after = os.lstat(target)
    except subprocess.TimeoutExpired as error:
        pytest.fail(f"connector membership FIFO race did not finish: {error}")
    finally:
        target.unlink(missing_ok=True)

    assert completed.returncode == 0
    assert completed.stdout == completed.stderr == ""
    assert after is not None
    assert stat.S_ISFIFO(after.st_mode)


def test_bounded_regular_file_walk_enforces_exact_yaml_count_boundary(tmp_path: Path) -> None:
    directory = SecureDirectory.open(tmp_path)
    try:
        for index in range(256):
            (tmp_path / f"binding-{index:03}.yaml").write_bytes(b"")
        assert (
            len(
                directory.walk_regular_files_bounded(
                    ".yaml",
                    max_files=256,
                    max_depth=8,
                    max_file_bytes=1_048_576,
                    max_total_bytes=8_388_608,
                    reject_symlinks=True,
                )
            )
            == 256
        )
        (tmp_path / "binding-256.yaml").write_bytes(b"")

        with pytest.raises(UnsafePathError):
            directory.walk_regular_files_bounded(
                ".yaml",
                max_files=256,
                max_depth=8,
                max_file_bytes=1_048_576,
                max_total_bytes=8_388_608,
                reject_symlinks=True,
            )
    finally:
        directory.close()


def test_bounded_regular_file_walk_enforces_exact_depth_boundary(tmp_path: Path) -> None:
    exact = tmp_path.joinpath(*(f"level-{index}" for index in range(8)))
    exact.mkdir(parents=True)
    (exact / "binding.yaml").write_bytes(b"")
    directory = SecureDirectory.open(tmp_path)
    try:
        assert (
            len(
                directory.walk_regular_files_bounded(
                    ".yaml",
                    max_files=256,
                    max_depth=8,
                    max_file_bytes=1_048_576,
                    max_total_bytes=8_388_608,
                    reject_symlinks=True,
                )
            )
            == 1
        )
        too_deep = exact / "level-8"
        too_deep.mkdir()
        (too_deep / "binding.yaml").write_bytes(b"")

        with pytest.raises(UnsafePathError):
            directory.walk_regular_files_bounded(
                ".yaml",
                max_files=256,
                max_depth=8,
                max_file_bytes=1_048_576,
                max_total_bytes=8_388_608,
                reject_symlinks=True,
            )
    finally:
        directory.close()


def test_bounded_regular_file_walk_enforces_exact_per_file_boundary(tmp_path: Path) -> None:
    target = tmp_path / "binding.yaml"
    target.write_bytes(b"x" * 1_048_576)
    directory = SecureDirectory.open(tmp_path)
    try:
        records = directory.walk_regular_files_bounded(
            ".yaml",
            max_files=256,
            max_depth=8,
            max_file_bytes=1_048_576,
            max_total_bytes=8_388_608,
            reject_symlinks=True,
        )
        assert len(records[0][1].content) == 1_048_576
        target.write_bytes(b"x" * 1_048_577)

        with pytest.raises(UnsafePathError):
            directory.walk_regular_files_bounded(
                ".yaml",
                max_files=256,
                max_depth=8,
                max_file_bytes=1_048_576,
                max_total_bytes=8_388_608,
                reject_symlinks=True,
            )
    finally:
        directory.close()


def test_bounded_regular_file_walk_enforces_exact_aggregate_boundary(tmp_path: Path) -> None:
    for index in range(8):
        (tmp_path / f"binding-{index}.yaml").write_bytes(b"x" * 1_048_576)
    directory = SecureDirectory.open(tmp_path)
    try:
        records = directory.walk_regular_files_bounded(
            ".yaml",
            max_files=256,
            max_depth=8,
            max_file_bytes=1_048_576,
            max_total_bytes=8_388_608,
            reject_symlinks=True,
        )
        assert sum(len(source.content) for _relative, source in records) == 8_388_608
        (tmp_path / "binding-over.yaml").write_bytes(b"x")

        with pytest.raises(UnsafePathError):
            directory.walk_regular_files_bounded(
                ".yaml",
                max_files=256,
                max_depth=8,
                max_file_bytes=1_048_576,
                max_total_bytes=8_388_608,
                reject_symlinks=True,
            )
    finally:
        directory.close()


def test_bounded_membership_walk_authenticates_identity_without_reading_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "binding.yaml"
    target.write_bytes(b"PRIVATE-BINDING-CONTENT")
    directory = SecureDirectory.open(tmp_path)

    def forbidden_read(_descriptor: int, *, max_bytes: int | None = None) -> bytes:
        del max_bytes
        raise AssertionError("membership-only scan read file content")

    monkeypatch.setattr(secure, "_read_descriptor", forbidden_read)
    try:
        records = directory.walk_regular_files_bounded(
            ".yaml",
            max_files=256,
            max_depth=8,
            max_file_bytes=1_048_576,
            max_total_bytes=8_388_608,
            reject_symlinks=True,
            read_content=False,
        )
    finally:
        directory.close()

    assert records[0][0].as_posix() == "binding.yaml"
    assert records[0][1].content == b""
    assert records[0][1].identities[-1] == (os.lstat(target).st_dev, os.lstat(target).st_ino)
