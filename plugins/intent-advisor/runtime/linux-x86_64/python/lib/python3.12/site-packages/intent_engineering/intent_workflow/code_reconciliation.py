"""Explicit, checkout-local jobs for read-only semantic reconciliation."""

from __future__ import annotations

import fcntl
import os
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from fnmatch import fnmatchcase
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Protocol
from uuid import uuid4

from pydantic import Field

from intent_engineering.core.models._base import StrictModel
from intent_engineering.intent_workflow.background import _digest
from intent_engineering.intent_workflow.dev_observer import _run_git_bounded
from intent_engineering.intent_workflow.initial_reconciliation import (
    INTENT_TYPES,
    REQUIREMENT_TYPES,
    InitialReconciliationService,
)
from intent_engineering.intent_workflow.initial_reconciliation_models import (
    AssessmentFile,
    Comparison,
    InitialReconciliationRequest,
)
from intent_engineering.storage._atomic import same_path_lock
from intent_engineering.storage.secure import SecureDirectory, SecureFile

if TYPE_CHECKING:
    from intent_engineering.cli.runtime import Runtime

# New files are implementation candidates, not inferred semantic matches. Explicit
# baseline files remain included regardless of extension. Test-to-code edges are absent.
CODE_SUFFIXES = frozenset(
    [
        ".py",
        ".pyi",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".ts",
        ".tsx",
        ".java",
        ".kt",
        ".kts",
        ".go",
        ".rs",
        ".c",
        ".h",
        ".cpp",
        ".hpp",
        ".cs",
        ".swift",
        ".m",
        ".mm",
        ".rb",
        ".php",
        ".scala",
        ".sh",
        ".bash",
        ".zsh",
        ".ps1",
        ".sql",
        ".graphql",
        ".proto",
        ".html",
        ".css",
        ".scss",
        ".vue",
        ".svelte",
        ".dart",
        ".ex",
        ".exs",
        ".erl",
        ".hs",
        ".clj",
        ".lua",
        ".r",
        ".jl",
        ".yaml",
        ".yml",
        ".toml",
        ".json",
        ".tf",
        ".nix",
    ]
)
CODE_NAMES = {"Dockerfile", "Makefile", "Justfile", "Containerfile", "CMakeLists.txt"}
_PRIVATE_PARTS = {".git", ".intent", ".codex", ".agents", ".superpowers"}


class ReviewRunner(Protocol):
    def review(
        self, inputs: dict[str, Any], slots: list[dict[str, str]]
    ) -> tuple[Comparison, ...]: ...


class _Job(StrictModel):
    # Legacy field retained as an in-flight manual cancellation gate.
    enabled: bool = False
    generation: int = Field(default=0, ge=0)
    status: str = "pending"
    completed_signature: str | None = None
    run_id: str | None = None
    review_signature: str | None = None
    review_scope: str | None = None
    retry_after: datetime | None = None


@dataclass(frozen=True)
class _Snapshot:
    files: tuple[AssessmentFile, ...]
    signature: str
    baseline: dict[str, Any]
    unchanged_baseline: bool


class CodeReconciliationService:
    """One semantic worker per checkout; canonical locks never span a model call."""

    def __init__(
        self,
        runtime: Runtime,
        *,
        runner: ReviewRunner | None = None,
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        batch_size: int = 12,
    ) -> None:
        if not 1 <= batch_size <= 128:
            raise ValueError("invalid review batch size")
        self.runtime = runtime
        self.clock = clock or (lambda: datetime.now(UTC))
        self.sleeper = sleeper
        self.batch_size = batch_size
        self.assessment = InitialReconciliationService(runtime, clock=self.clock)
        self.runner = runner

    def _target(self) -> SecureFile:
        project = self.runtime.project_directory
        key = _digest([str(project.path), project.identity])
        with closing(SecureDirectory.open(self._state_root(), create=True)) as directory:
            return directory.file(key + ".json")

    @staticmethod
    def _state_root() -> Path:
        root = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
        if not root.is_absolute():
            raise ValueError("background state unavailable")
        return root / "intent-engineering" / "code-reconciliation"

    @staticmethod
    def _load(target: SecureFile) -> _Job:
        raw = target.read_optional_nonblocking(max_bytes=65536)
        return _Job.model_validate_json(raw) if raw else _Job()

    @staticmethod
    def _save(target: SecureFile, state: _Job) -> None:
        target.atomic_write(state.model_dump_json().encode(), mode=0o600)

    def configure(self, *, enabled: bool) -> dict[str, Any]:
        """Retire legacy automatic enablement; disable also cancels a running review."""
        with closing(self._target()) as target, same_path_lock(target):
            state = self._load(target)
            state.enabled = False
            state.generation += 1
            state.status = "on_demand_only" if enabled else "disabled"
            self._save(target, state)
        return self.status()

    def _view(self, state: _Job, *, status: str | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {}
        if state.run_id:
            result = self.assessment.run(
                InitialReconciliationRequest(
                    action="status",
                    session_id="code-reconciliation",
                    turn_id="status",
                    run_id=state.run_id,
                )
            )
        actual = status or state.status
        if actual in {"ready", "ready_with_findings"} and result.get("status") not in {
            "ready",
            "ready_with_findings",
        }:
            actual = "pending" if result.get("status") == "stale" else "unavailable"
        return {
            **result,
            "status": actual,
            "enabled": False,
            "execution_mode": "on_demand",
            "notify_user": False,
            "findings": result.get("findings", [])
            if actual in {"ready", "ready_with_findings"}
            else [],
            "trigger_coverage": "explicit reconcile-code --run only",
            "source_freshness": "latest captured evidence; provider refresh is host-operated",
        }

    def status(self) -> dict[str, Any]:
        with closing(self._target()) as target, same_path_lock(target):
            state = self._load(target)
        if state.enabled and state.status in {"ready", "ready_with_findings"}:
            try:
                snapshot = self._snapshot()
                if snapshot is None or snapshot.signature != state.completed_signature:
                    return self._view(state, status="pending")
            except (OSError, ValueError):
                return self._view(state, status="unavailable")
        return self._view(state)

    def _excluded(self, path: str) -> bool:
        p = PurePosixPath(path)
        # XDG_STATE_HOME may intentionally live inside a checkout. Our own job
        # updates must never become code candidates or recursively trigger review.
        cache = self._state_root()
        if cache.is_relative_to(self.runtime.root):
            relative_cache = PurePosixPath(cache.relative_to(self.runtime.root).as_posix())
            if p == relative_cache or relative_cache in p.parents:
                return True
        return bool(set(p.parts) & _PRIVATE_PARTS) or any(
            p.match(pattern) or fnmatchcase(path, pattern)
            for pattern in self.runtime.config.source_exclusions
        )

    def _snapshot(self) -> _Snapshot | None:
        baseline = self.assessment.baseline()
        if baseline is None:
            return None
        previous = baseline["code_refs"] + baseline["requirements_refs"]
        items = {f["path"]: AssessmentFile(path=f["path"], role=f["role"]) for f in previous}
        parents = {PurePosixPath(f["path"]).parent for f in baseline["code_refs"]}
        # Git is used only for membership, including untracked and deleted paths.
        # No diff, clean-tree requirement, checkout, repository hooks or code execution.
        if (self.runtime.root / ".git").exists():
            raw = _run_git_bounded(
                self.runtime.root,
                ("ls-files", "--cached", "--others", "--exclude-standard", "-z"),
                max_bytes=1048576,
            )
            for path in raw.split("\0"):
                p = PurePosixPath(path)
                if (
                    not path
                    or self._excluded(path)
                    or not any(parent == p.parent or parent in p.parents for parent in parents)
                ):
                    continue
                if p.suffix.lower() in CODE_SUFFIXES or p.name in CODE_NAMES:
                    items.setdefault(path, AssessmentFile(path=path, role="code"))
        if len(items) > 4096:
            raise ValueError("review scope exceeds file limit")
        files = []
        signatures = []
        total = 0
        for path, item in sorted(items.items()):
            if self._excluded(path):
                # Explicit scope becoming inaccessible is incomplete, not silently dropped.
                raise ValueError("declared assessment path is excluded")
            item = item.model_copy(update={"missing": self.assessment.file_missing(path)})
            raw_file = self.assessment._file(item)
            total += len(raw_file)
            if total > 64 * 1024 * 1024 or b"\0" in raw_file:
                raise ValueError("review scope unavailable")
            raw_file.decode("utf-8")
            files.append(item)
            signatures.append({**item.model_dump(), "digest": sha256(raw_file).hexdigest()})
        with self.assessment.refresh._transaction() as (actor, _principals, _now):
            graph = self.runtime.graph_store.load()
            semantic = [
                n.model_dump(mode="json")
                for n in graph.nodes
                if n.type in INTENT_TYPES | REQUIREMENT_TYPES and n.status == "active"
            ]
            sources = self.assessment._sources(actor)
        old_files = sorted(
            [
                {
                    "path": f["path"],
                    "role": f["role"],
                    "missing": f.get("missing", False),
                    "digest": f["digest"],
                }
                for f in previous
            ],
            key=lambda f: f["path"],
        )
        current = self.assessment.run(
            InitialReconciliationRequest(
                action="status",
                session_id="code-reconciliation",
                turn_id="status",
                run_id=baseline["run_id"],
            )
        )
        return _Snapshot(
            tuple(files),
            _digest([signatures, semantic, sources]),
            baseline,
            signatures == old_files and current["status"] in {"ready", "ready_with_findings"},
        )

    def _matches(self, signature: str) -> bool:
        try:
            snapshot = self._snapshot()
            return snapshot is not None and snapshot.signature == signature
        except (OSError, ValueError):
            return False

    def _update(self, target: SecureFile, **values: Any) -> _Job:
        with same_path_lock(target):
            state = self._load(target)
            for key, value in values.items():
                setattr(state, key, value)
            self._save(target, state)
            return state

    def _review(self, target: SecureFile, snapshot: _Snapshot) -> str:
        state = self._load(target)
        # File/semantic signatures deliberately omit graph version. Reuse only a
        # fresh completed assessment, including unrelated canonical graph changes.
        if snapshot.unchanged_baseline:
            self._update(
                target,
                completed_signature=snapshot.signature,
                run_id=snapshot.baseline["run_id"],
                status=snapshot.baseline["status"],
            )
            return snapshot.baseline["status"]  # type: ignore[no-any-return]
        # Recheck after the quiet interval; newer content is never reviewed as old content.
        self.sleeper(2.0)
        if not self._matches(snapshot.signature):
            return "stale"
        scope = state.review_scope if state.review_signature == snapshot.signature else None
        if scope is None:
            scope = (
                snapshot.baseline["scope"].split("\nBackground code-change review:")[0]
                + "\nBackground code-change review: "
                + uuid4().hex
                + ". Includes declared files and recognized nonignored code/config files "
                "under declared code directories. Source evidence is the latest captured state."
            )
        self._update(
            target, review_scope=scope, review_signature=snapshot.signature, status="running"
        )
        service = InitialReconciliationService(
            self.runtime, clock=self.clock, input_guard=lambda: self._matches(snapshot.signature)
        )
        session = "code-reconciliation"
        result = service.run(
            InitialReconciliationRequest(
                action="start",
                session_id=session,
                turn_id=uuid4().hex,
                scope=scope,
                files=snapshot.files,
            )
        )
        if result["status"] == "stale":
            return "stale"
        run_id = result["run_id"]
        self._update(target, run_id=run_id)
        if any(
            s["dirty"] or not s["initialized"] or not s["reconciled"] for s in result["sources"]
        ):
            return "awaiting_source_refresh"
        todo = list(result["missing_comparisons"]) + [
            {"axis": c["axis"], "subject_ref": c["subject_ref"]}
            for c in result["comparisons"]
            if c["status"] == "unknown"
        ]
        todo.sort(key=lambda s: (s["axis"] != "document_requirements", s["axis"], s["subject_ref"]))
        if self.runner is None:
            from intent_engineering.intent_workflow.codex_review import CodexReviewRunner

            self.runner = CodexReviewRunner()
        while todo:
            with same_path_lock(target):
                if not self._load(target).enabled:
                    return "disabled"
            inputs = service.inputs(run_id)
            if inputs["status"] in {"stale", "unavailable"}:
                return str(inputs["status"])
            # Review PRDs first so subsequent batches can use their requirement mappings.
            size = 1 if todo[0]["axis"] == "document_requirements" else self.batch_size
            slots, todo = todo[:size], todo[size:]
            rows = self.runner.review(inputs, slots)
            requested = {(s["axis"], s["subject_ref"]) for s in slots}
            actual = [(r.axis, r.subject_ref) for r in rows if r.axis != "source_conflict"]
            if set(actual) != requested or len(actual) != len(requested):
                raise ValueError("review did not cover requested comparisons")
            result = service.run(
                InitialReconciliationRequest(
                    action="checkpoint",
                    session_id=session,
                    turn_id=uuid4().hex,
                    run_id=run_id,
                    comparisons=rows,
                )
            )
            if result["status"] in {"stale", "unavailable"}:
                return str(result["status"])
        with same_path_lock(target):
            if not self._load(target).enabled:
                return "disabled"
        try:
            result = service.run(
                InitialReconciliationRequest(
                    action="finish",
                    session_id=session,
                    turn_id=uuid4().hex,
                    run_id=run_id,
                )
            )
        except ValueError:
            if not self._matches(snapshot.signature):
                return "stale"
            # Individually valid but contradictory checkpoints need a fresh review.
            self._update(target, review_scope=None)
            raise
        if result["status"] in {"ready", "ready_with_findings"}:
            self._update(target, completed_signature=snapshot.signature)
        return str(result["status"])

    def run(self, *, automatic: bool = False) -> dict[str, Any]:
        """Run an explicitly requested review; legacy automatic requests are inert."""
        if automatic:
            # Do not read inputs, create state, queue work, or instantiate a reviewer.
            # Persisted enablement from older releases cannot override this policy.
            return {
                "status": "on_demand_only",
                "enabled": False,
                "execution_mode": "on_demand",
                "notify_user": False,
                "findings": [],
            }
        with (
            closing(self._target()) as target,
            closing(target.sibling("worker-" + target.path.name)) as lease,
        ):
            fd = lease.open_lock()
            owned = False
            try:
                with same_path_lock(target):
                    state = self._load(target)
                    state.generation += 1
                    self._save(target, state)
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        return {"status": "queued", "notify_user": False, "findings": []}
                    owned = True
                    # Explicit demand supersedes an old disabled configuration.
                    # A subsequent --disable still cancels this worker safely.
                    state.enabled = True
                    self._save(target, state)
                while True:
                    with same_path_lock(target):
                        state = self._load(target)
                        generation = state.generation
                    if not state.enabled:
                        outcome = "disabled"
                    else:
                        try:
                            snapshot = self._snapshot()
                            outcome = (
                                "awaiting_initial_assessment"
                                if snapshot is None
                                else self._review(target, snapshot)
                            )
                        except Exception:  # noqa: BLE001 - fixed, quiet background boundary
                            outcome = "retry_pending"
                    if outcome == "stale":
                        self._update(target, status="pending")
                        continue
                    with same_path_lock(target):
                        state = self._load(target)
                        state.status = outcome if state.enabled else "disabled"
                        state.retry_after = (
                            self.clock() + timedelta(seconds=60)
                            if outcome in {"retry_pending", "incomplete", "unavailable"}
                            else None
                        )
                        self._save(target, state)
                        if (
                            state.enabled
                            and state.generation != generation
                            and outcome in {"ready", "ready_with_findings"}
                        ):
                            continue
                        # Handoff while holding the state lock prevents a lost last event.
                        fcntl.flock(fd, fcntl.LOCK_UN)
                        owned = False
                        break
                return self._view(state)
            finally:
                if owned:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
