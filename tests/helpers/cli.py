"""Black-box helpers for invoking the installed local CLI."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from intent_engineering.core.models import JsonValue


@dataclass(frozen=True)
class CliResult:
    """Captured UTF-8 process outcome."""

    returncode: int
    stdout: str
    stderr: str

    def json(self) -> dict[str, JsonValue]:
        """Decode the structured stdout emitted by one CLI command."""
        return cast(dict[str, JsonValue], json.loads(self.stdout))


def init_git_repo(path: Path) -> Path:
    """Create a minimal repository with an initial commit for Git capture."""
    repo = path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", "Intent Test"], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "intent@example.test"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    (repo / "README.md").write_text("# Local export\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "--quiet", "-m", "initial"], cwd=repo, check=True, capture_output=True
    )
    return repo


def run_intent(repo: Path, *args: str) -> CliResult:
    """Run the installed package with a deterministic, scrubbed environment."""
    environment = {"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PATH": "/usr/bin:/bin"}
    completed = subprocess.run(
        [str(Path(sys.executable).with_name("intent")), *args],
        cwd=repo,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return CliResult(completed.returncode, completed.stdout, completed.stderr)
