"""Shell-free deterministic local Git evidence connector."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import cast

import anyio

from intent_engineering.capture.base import (
    ConnectorError,
    RawSourceObject,
    SourceObject,
    normalize_raw_source,
)
from intent_engineering.core.models import EvidenceRecord, JsonValue


def run_git(repo: Path, args: Sequence[str]) -> str:
    """Run Git with structured arguments and captured UTF-8 output."""
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        shell=False,
    )
    return completed.stdout


def _sha_from_object_id(object_id: str) -> str:
    if not object_id.startswith("commit:"):
        raise ValueError(f"unsupported Git object ID: {object_id}")
    return object_id.removeprefix("commit:")


class GitConnector:
    """Capture immutable Git commits without retaining diffs."""

    connector_id = "git"

    def __init__(self, repo: Path) -> None:
        self.repo = repo.resolve()

    @staticmethod
    def _source_object(sha: str) -> SourceObject:
        return SourceObject(
            external_object_id=f"commit:{sha}",
            external_version=sha,
            locator=f"git:commit:{sha}",
        )

    async def discover(self, cursor: str | None) -> Sequence[SourceObject]:
        """Return commits after the prior SHA in oldest-first order."""
        try:
            inside_work_tree = await anyio.to_thread.run_sync(
                run_git,
                self.repo,
                ["rev-parse", "--is-inside-work-tree"],
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise ConnectorError("Git discovery failed") from error
        if inside_work_tree.strip() != "true":
            raise ConnectorError("Git discovery failed")
        try:
            await anyio.to_thread.run_sync(
                run_git,
                self.repo,
                ["rev-parse", "--verify", "--quiet", "HEAD"],
            )
        except subprocess.CalledProcessError:
            return ()
        revision = "HEAD" if cursor is None else f"{cursor}..HEAD"
        try:
            output = await anyio.to_thread.run_sync(
                run_git,
                self.repo,
                ["rev-list", "--reverse", revision],
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise ConnectorError("Git discovery failed") from error
        return tuple(self._source_object(sha) for sha in output.splitlines() if sha)

    async def fetch(self, object_id: str, version: str) -> RawSourceObject:
        """Fetch commit data, deduplicating per-parent merge paths in sorted order."""
        sha = _sha_from_object_id(object_id)
        if version != sha:
            raise ConnectorError("Git fetch failed")
        try:
            metadata = await anyio.to_thread.run_sync(
                run_git,
                self.repo,
                ["show", "-s", "--format=%H%x00%an <%ae>%x00%aI%x00%P%x00%s%x00%b%x00", sha],
            )
            fields = metadata.split("\x00")
            if len(fields) < 7 or fields[0] != sha:
                raise ValueError("unexpected Git commit metadata")
            paths_output = await anyio.to_thread.run_sync(
                run_git,
                self.repo,
                ["diff-tree", "--root", "-m", "--no-commit-id", "--name-only", "-r", "-z", sha],
            )
            changed_paths = sorted({path for path in paths_output.split("\x00") if path})
            payload: dict[str, JsonValue] = {
                "sha": fields[0],
                "author": fields[1],
                "timestamp": fields[2],
                "parents": cast(JsonValue, fields[3].split() if fields[3] else []),
                "subject": fields[4],
                "body": fields[5].rstrip("\n"),
                "changed_paths": cast(JsonValue, changed_paths),
            }
            content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
                "utf-8"
            )
            return RawSourceObject(
                connector_type=self.connector_id,
                external_object_id=object_id,
                external_version=version,
                author=fields[1],
                observed_at=datetime.fromisoformat(fields[2]),
                source_locator=f"git:commit:{sha}",
                content_hash=f"sha256:{sha256(content).hexdigest()}",
                payload=payload,
            )
        except (OSError, subprocess.SubprocessError, UnicodeError, ValueError) as error:
            raise ConnectorError("Git fetch failed") from error

    def normalize(self, raw: RawSourceObject) -> EvidenceRecord:
        """Normalize raw commit metadata into shared immutable evidence."""
        return normalize_raw_source(raw)

    def next_checkpoint(self, discovered: Sequence[SourceObject]) -> str | None:
        """Advance to the newest successfully discovered commit SHA."""
        if not discovered:
            return None
        return discovered[-1].external_version
