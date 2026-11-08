"""Bounded, read-only discovery of likely product requirement documents."""

from __future__ import annotations

import os
import subprocess
from contextlib import closing
from fnmatch import fnmatchcase
from itertools import islice
from pathlib import Path, PurePosixPath

from intent_engineering.storage.secure import SecureDirectory

_SKIP = {"node_modules", "vendor", "dist", "build", "target", "venv", "__pycache__"}
_NAMES = {"prd", "requirements", "product-requirements", "product-requirements-document"}


def discover_prds(project: Path, exclusions: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Return at most twenty candidate paths, never source contents or inferred authority."""
    candidates: list[str] = []
    inspected = 0

    def ignored_paths(paths: list[str]) -> set[str]:
        if not paths:
            return set()
        result = subprocess.run(
            ["git", "-C", str(project), "check-ignore", "--stdin", "-z"],
            input="\0".join(paths).encode() + b"\0",
            capture_output=True,
            timeout=2,
            check=False,
        )
        return set(result.stdout.decode().split("\0")) if result.returncode in {0, 1} else set()

    def walk(directory: SecureDirectory, prefix: PurePosixPath, depth: int) -> None:
        nonlocal inspected
        with os.scandir(directory.descriptor) as entries:
            batch = list(islice(entries, max(0, 2000 - inspected)))
            ignored = ignored_paths([(prefix / entry.name).as_posix() for entry in batch])
            for entry in batch:
                inspected += 1
                if inspected > 2000 or len(candidates) >= 20:
                    return
                relative = prefix / entry.name
                if (
                    relative.as_posix() in ignored
                    or entry.name.startswith(".")
                    or entry.name in _SKIP
                    or any(
                        relative.match(pattern) or fnmatchcase(relative.as_posix(), pattern)
                        for pattern in exclusions
                    )
                ):
                    continue
                if entry.is_dir(follow_symlinks=False) and depth < 3:
                    with closing(directory.subdirectory(entry.name)) as child:
                        walk(child, relative, depth + 1)
                elif entry.is_file(follow_symlinks=False) and relative.suffix.casefold() == ".md":
                    stem = relative.stem.casefold().replace("_", "-")
                    if stem in _NAMES or stem.endswith("-prd"):
                        candidates.append(relative.as_posix())

    with closing(SecureDirectory.open(project)) as directory:
        walk(directory, PurePosixPath(), 0)
    return tuple(sorted(candidates))
