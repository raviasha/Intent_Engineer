"""Trusted, bounded Git-object-to-read-only-image CI execution adapter.

Only the rootful container daemon and protected-base adapter are trusted. Reviewed
commands never provide Dockerfiles, entrypoints, mounts, image IDs or eligibility
assertions. Local evidence cannot authorize this path: every command is executed
again, then its container is destroyed before consumption in a fresh container
of the same exact immutable image.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
import zlib
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType

import anyio

import intent_engineering
from intent_engineering.cli.runtime import CheckRuntimeAdapter
from intent_engineering.integrations.github_action import _clear_results, _write, run_tests
from intent_engineering.intent_workflow.check import (
    MAX_TEST_RESULT_BYTES,
    CheckRequest,
    CheckService,
    SharedStateRestoreResult,
    SharedStateRestoreStatus,
    TestResultArtifact,
)
from intent_engineering.intent_workflow.dev_observer import (
    MAX_COMMIT_SNAPSHOT_BYTES,
    _git_pin_matches,
    _pin_git_executable,
    _run_git_bytes_bounded,
)
from intent_engineering.intent_workflow.immutable_execution import ImmutableExecutionGuard
from intent_engineering.storage.secure import SecureDirectory
from intent_engineering.team_state.ci import CiTrustError
from intent_engineering.team_state.restore import (
    CANONICAL_STATE_PATHS,
    EnvironmentTrustProvider,
    StaticTrustProvider,
    TrustProvider,
    read_approved_baseline,
)

_CONTEXT_LIMIT = 256 * 1024 * 1024
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
_CONTAINER_ID = re.compile(r"[0-9a-f]{64}")
_REVISION = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_OWNER = re.compile(r"[A-Za-z0-9_.:/-]{1,256}")
_OWNERSHIP_LABEL = "intent.ephemeral-ci"
_NONCE_LABEL = "intent.ephemeral-ci.nonce"
_MAX_OWNED_RESOURCES = 64
_ROOT = Path("/project")
_OUTPUT = Path("/output")
_RESULT = Path(".intent-ci/test-results.json")
_BASE_IMAGE = "python@sha256:581429e3df12d76e6af4be5ab7d0e7fc2013eb57dc23d2de691411c8efdbb970"
_DEPENDENCY_DOCKERFILE = """\
FROM {base}
LABEL intent.ephemeral-ci="{owner}" intent.ephemeral-ci.nonce="{nonce}"
COPY requirements.txt /build/requirements.txt
RUN python -I -m pip install --no-cache-dir --require-hashes --only-binary=:all: -r /build/requirements.txt
"""
_DOCKERFILE = """\
FROM {dependency_image}
LABEL intent.ephemeral-ci="{owner}" intent.ephemeral-ci.nonce="{nonce}"
COPY adapter/ /usr/local/lib/python3.12/site-packages/intent_engineering/
COPY repository/ /project/
COPY baseline/ /build/
RUN cp --remove-destination /usr/local/bin/python3.12 /usr/bin/python3 && cp --remove-destination /bin/dash /bin/sh
RUN python -I -m intent_engineering.integrations.immutable_ci materialize
ENV PYTHONDONTWRITEBYTECODE=1
WORKDIR /project
USER 65532:65532
ENTRYPOINT ["/usr/local/bin/python", "-I", "-m", "intent_engineering.integrations.immutable_ci"]
"""


def _git(root: Path, arguments: tuple[str, ...], maximum: int) -> bytes:
    pin = _pin_git_executable()
    content = _run_git_bytes_bounded(root, arguments, max_bytes=maximum)
    if not _git_pin_matches(pin):
        raise ValueError("immutable CI unavailable")
    return content


def _member(archive: tarfile.TarFile, name: str, content: bytes) -> None:
    item = tarfile.TarInfo(name)
    item.mode = 0o644
    item.size = len(content)
    archive.addfile(item, io.BytesIO(content))


def _git_material(root: Path, commit: str, state_tip: str) -> dict[str, tuple[bytes, int]]:
    """Materialize only verified objects for HEAD and the bounded state lineage.

    No checkout files, hooks, filters, index, Git configuration, or unbounded
    compressed history enters the image. Missing code ancestors are explicitly
    represented as a shallow boundary for passive Git capture.
    """
    result: dict[str, tuple[bytes, int]] = {}
    objects: dict[str, bytes] = {}
    total = 0
    entries = 0
    deadline = time.monotonic() + 120

    def read_object(kind: str, object_id: str, maximum: int) -> bytes:
        nonlocal total
        if time.monotonic() > deadline:
            raise ValueError("immutable CI unavailable")
        if object_id in objects:
            return objects[object_id]
        if _REVISION.fullmatch(object_id) is None:
            raise ValueError("immutable CI unavailable")
        content = _git(root, ("cat-file", kind, object_id), maximum)
        encoded = kind.encode() + b" " + str(len(content)).encode() + b"\0" + content
        digest = (
            hashlib.sha1(encoded, usedforsecurity=False)
            if len(object_id) == 40
            else hashlib.sha256(encoded)
        ).hexdigest()
        total += len(encoded)
        if digest != object_id or total > MAX_COMMIT_SNAPSHOT_BYTES or len(objects) >= 16384:
            raise ValueError("immutable CI unavailable")
        objects[object_id] = content
        result[".git/objects/" + object_id[:2] + "/" + object_id[2:]] = (
            zlib.compress(encoded),
            0o644,
        )
        return content

    def read_tree(object_id: str, prefix: str, *, code: bool) -> None:
        nonlocal entries
        raw = read_object("tree", object_id, 1024 * 1024)
        offset = 0
        while offset < len(raw):
            end = raw.index(b"\0", offset)
            mode, name = raw[offset:end].split(b" ", 1)
            width = len(commit) // 2
            child = raw[end + 1 : end + 1 + width].hex()
            offset = end + 1 + width
            path = prefix + name.decode("utf-8")
            entries += 1
            if (
                entries > 4096
                or len(path.encode()) > 4096
                or any(part in {"", ".", "..", ".git"} for part in path.split("/"))
                or "\\" in path
            ):
                raise ValueError("immutable CI unavailable")
            if code and path.split("/", 1)[0] in {".intent", ".intent-ci"}:
                raise ValueError("immutable CI unavailable")
            if mode == b"40000":
                read_tree(child, path + "/", code=code)
            elif mode in {b"100644", b"100755"}:
                content = read_object("blob", child, 16 * 1024 * 1024)
                if code:
                    result[path] = (content, 0o755 if mode == b"100755" else 0o644)
            else:
                raise ValueError("immutable CI unavailable")

    def read_commit(object_id: str, *, code: bool) -> tuple[str, ...]:
        raw = read_object("commit", object_id, 64 * 1024)
        headers = raw.split(b"\n\n", 1)[0].splitlines()
        trees = [line[5:].decode("ascii") for line in headers if line.startswith(b"tree ")]
        if len(trees) != 1:
            raise ValueError("immutable CI unavailable")
        read_tree(trees[0], "", code=code)
        return tuple(line[7:].decode("ascii") for line in headers if line.startswith(b"parent "))

    read_commit(commit, code=True)
    state = state_tip
    for _depth in range(64):
        parents = read_commit(state, code=False)
        if not parents:
            break
        if len(parents) != 1:
            raise ValueError("immutable CI unavailable")
        state = parents[0]
    else:
        raise ValueError("immutable CI unavailable")
    result[".git/HEAD"] = (commit.encode() + b"\n", 0o644)
    result[".git/shallow"] = (commit.encode() + b"\n", 0o644)
    result[".git/refs/remotes/origin/intent-state"] = (state_tip.encode() + b"\n", 0o644)
    return result


def _requirements() -> bytes:
    lock = Path(__file__).with_name("ci-runtime.lock")
    if lock.is_symlink() or lock.stat().st_size > 256 * 1024:
        raise ValueError("immutable CI unavailable")
    return lock.read_bytes()


def _dependency_context(nonce: str, owner: str | None = None) -> bytes:
    """Networked dependency build receives only protected lock and trusted Dockerfile."""
    result = io.BytesIO()
    with tarfile.open(fileobj=result, mode="w:") as archive:
        _member(
            archive,
            "Dockerfile",
            _DEPENDENCY_DOCKERFILE.format(
                base=_BASE_IMAGE, nonce=nonce, owner=nonce if owner is None else owner
            ).encode(),
        )
        _member(archive, "requirements.txt", _requirements())
    return result.getvalue()


def _build_context(
    root: Path,
    *,
    at: datetime,
    revision: str | None = None,
    trust_provider: TrustProvider | None = None,
    state_tip: str | None = None,
    dependency_image: str = _BASE_IMAGE,
    nonce: str | None = None,
    owner: str | None = None,
) -> bytes:
    """Capture immutable Git objects and authenticated baseline, never checkout bytes."""
    provider = EnvironmentTrustProvider() if trust_provider is None else trust_provider
    captured_state_tip = (
        _git(root, ("rev-parse", "--verify", "refs/remotes/origin/intent-state"), 256)
        .decode()
        .strip()
    )
    if _REVISION.fullmatch(captured_state_tip) is None or (
        state_tip is not None and state_tip != captured_state_tip
    ):
        raise ValueError("immutable CI unavailable")
    trust = provider.load()
    if trust is None:
        raise ValueError("immutable CI unavailable")
    files = read_approved_baseline(root, StaticTrustProvider(trust), at=at)
    if (
        _git(root, ("rev-parse", "--verify", "refs/remotes/origin/intent-state"), 256)
        .decode()
        .strip()
        != captured_state_tip
    ):
        raise ValueError("immutable CI unavailable")
    commit = (
        _git(root, ("rev-parse", "--verify", "HEAD"), 256).strip()
        if revision is None
        else revision.encode("ascii")
    )
    if _REVISION.fullmatch(commit.decode("ascii")) is None:
        raise ValueError("immutable CI unavailable")
    material = _git_material(root, commit.decode(), captured_state_tip)
    # The origin is public routing metadata, never a credential-bearing URL.
    origin = "https://" + trust.repository_id
    trust = None
    config = f'[core]\nrepositoryformatversion = {int(len(commit) == 64)}\n[remote "origin"]\nurl = {origin}\n'
    if len(commit) == 64:
        config += "[extensions]\nobjectFormat = sha256\n"
    material[".git/config"] = (config.encode("ascii"), 0o644)
    if dependency_image != _BASE_IMAGE and _IMAGE_ID.fullmatch(dependency_image) is None:
        raise ValueError("immutable CI unavailable")
    nonce = uuid.uuid4().hex if nonce is None else nonce
    owner = nonce if owner is None else owner
    if _OWNER.fullmatch(owner) is None:
        raise ValueError("immutable CI unavailable")
    package = Path(intent_engineering.__file__).parent
    result = io.BytesIO()
    with tarfile.open(fileobj=result, mode="w:") as archive:
        _member(
            archive,
            "Dockerfile",
            _DOCKERFILE.format(
                nonce=nonce, owner=owner, dependency_image=dependency_image
            ).encode(),
        )
        _member(
            archive,
            "Dependency.Dockerfile",
            _DEPENDENCY_DOCKERFILE.format(base=_BASE_IMAGE, nonce=nonce, owner=owner).encode(),
        )
        _member(archive, "requirements.txt", _requirements())
        for relative, (content, mode) in sorted(material.items()):
            item = tarfile.TarInfo("repository/" + relative)
            item.mode = mode
            item.size = len(content)
            archive.addfile(item, io.BytesIO(content))
        for relative, content in sorted(files.items()):
            _member(archive, "baseline/" + relative, content)
        paths = sorted(
            path
            for path in package.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        )
        if len(paths) > 4096:
            raise ValueError("immutable CI unavailable")
        for path in paths:
            if path.is_symlink() or path.stat().st_size > 16 * 1024 * 1024:
                raise ValueError("immutable CI unavailable")
            _member(archive, "adapter/" + path.relative_to(package).as_posix(), path.read_bytes())
            if result.tell() > _CONTEXT_LIMIT:
                raise ValueError("immutable CI unavailable")
    return result.getvalue()


def build_context(
    root: Path,
    *,
    at: datetime,
    revision: str | None = None,
    trust_provider: TrustProvider | None = None,
    state_tip: str | None = None,
    dependency_image: str = _BASE_IMAGE,
    nonce: str | None = None,
    owner: str | None = None,
) -> bytes:
    """Scrub authenticated plaintext/private frames at the public build boundary."""
    try:
        return _build_context(
            root,
            at=at,
            revision=revision,
            trust_provider=trust_provider,
            state_tip=state_tip,
            dependency_image=dependency_image,
            nonce=nonce,
            owner=owner,
        )
    except BaseException as error:  # noqa: BLE001 - preserve cancellation, scrub all material
        trust_provider = None
        error.__traceback__ = None
        error.__cause__ = None
        error.__context__ = None
        if isinstance(error, CiTrustError):
            raise error.with_traceback(None) from None
        if isinstance(error, Exception):
            raise ValueError("immutable CI unavailable") from None  # noqa: TRY004
        raise error.with_traceback(None) from None


def _command(
    argv: list[str], *, content: bytes = b"", timeout: int = 900, maximum: int = 2 * 1024 * 1024
) -> bytes:
    """Bound both retained output and execution; no shell or secret-bearing argv."""
    with tempfile.TemporaryFile() as input_file:
        input_file.write(content)
        input_file.seek(0)
        process = subprocess.Popen(
            argv,
            stdin=input_file,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            env={
                **{
                    key: value
                    for key, value in os.environ.items()
                    if key
                    in {
                        "PATH",
                        "HOME",
                        "TMPDIR",
                        "DOCKER_HOST",
                        "DOCKER_CONTEXT",
                        "DOCKER_CONFIG",
                        "DOCKER_CERT_PATH",
                        "DOCKER_TLS_VERIFY",
                        "SSL_CERT_FILE",
                    }
                },
                "DOCKER_BUILDKIT": "0",
            },
        )
        selector = selectors.DefaultSelector()
        retained = bytearray()
        total = 0
        deadline = time.monotonic() + timeout
        try:
            assert process.stdout is not None and process.stderr is not None
            selector.register(process.stdout, selectors.EVENT_READ, True)
            selector.register(process.stderr, selectors.EVENT_READ, False)
            while selector.get_map():
                if time.monotonic() >= deadline:
                    raise ValueError("immutable CI unavailable")
                for key, _mask in selector.select(min(1, deadline - time.monotonic())):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(chunk)
                    if total > maximum:
                        raise ValueError("immutable CI unavailable")
                    if key.data:
                        retained.extend(chunk)
            if process.wait(timeout=max(0.01, deadline - time.monotonic())) != 0:
                raise ValueError("immutable CI unavailable")
            return bytes(retained)
        finally:
            selector.close()
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()


def _run_stage(
    docker: str,
    image_id: str,
    stage: str,
    content: bytes,
    *,
    nonce: str,
    owner: str,
) -> bytes:
    """Destroy the entire container/cgroup before releasing its bounded result."""
    container = "intent-ci-" + stage + "-" + uuid.uuid4().hex
    try:
        return _command(
            [
                docker,
                "run",
                "--name",
                container,
                "--label=" + _OWNERSHIP_LABEL + "=" + owner,
                "--label=" + _NONCE_LABEL + "=" + nonce,
                "--read-only",
                "--network=none",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--user=65532:65532",
                "--pids-limit=128",
                "--memory=2g",
                "--cpus=2",
                "--tmpfs=/tmp:rw,noexec,nosuid,size=64m,uid=65532,gid=65532",
                "--tmpfs=/output:rw,noexec,nosuid,size=128m,uid=65532,gid=65532",
                "--tmpfs=/project/.intent-ci:rw,noexec,nosuid,size=1m,uid=65532,gid=65532",
                "-i",
                image_id,
                stage,
            ],
            content=content,
            maximum=2 * 1024 * 1024,
        )
    finally:
        # Docker's completed removal tears down the PID namespace and cgroup,
        # including descendants that escaped a reviewed process group/session.
        _command([docker, "rm", "--force", container], timeout=30)
        if _command(
            [docker, "ps", "--all", "--quiet", "--filter", "name=^/" + container + "$"],
            timeout=30,
        ).strip():
            raise ValueError("immutable CI unavailable")


def _remove_owned_resources(
    docker: str,
    *,
    inventory: list[str],
    remove: list[str],
    identifier_pattern: re.Pattern[str],
) -> None:
    """Remove only a bounded, exact-label inventory and fail on no progress."""
    previous: frozenset[str] | None = None
    for _attempt in range(_MAX_OWNED_RESOURCES + 1):
        try:
            identifiers = _command(inventory, timeout=30, maximum=64 * 1024).decode("ascii").split()
        except UnicodeDecodeError:
            raise ValueError("immutable CI unavailable") from None
        current = frozenset(identifiers)
        if not identifiers:
            return
        if (
            len(identifiers) > _MAX_OWNED_RESOURCES
            or len(current) != len(identifiers)
            or any(identifier_pattern.fullmatch(identifier) is None for identifier in identifiers)
            or current == previous
        ):
            raise ValueError("immutable CI unavailable")
        previous = current
        for identifier in identifiers:
            with contextlib.suppress(OSError, ValueError, subprocess.SubprocessError):
                _command([docker, *remove, identifier], timeout=30)
    raise ValueError("immutable CI unavailable")


def cleanup_ephemeral_ci(owner: str) -> None:
    """Sweep only this runtime's labelled containers, then its labelled images."""
    if _OWNER.fullmatch(owner) is None:
        raise ValueError("immutable CI unavailable")
    docker = shutil.which("docker")
    if docker is None:
        raise ValueError("immutable CI unavailable")
    _remove_owned_resources(
        docker,
        inventory=[
            docker,
            "ps",
            "--all",
            "--quiet",
            "--no-trunc",
            "--filter",
            "label=" + _OWNERSHIP_LABEL + "=" + owner,
        ],
        remove=["rm", "--force"],
        identifier_pattern=_CONTAINER_ID,
    )
    _remove_owned_resources(
        docker,
        inventory=[
            docker,
            "image",
            "ls",
            "--all",
            "--quiet",
            "--no-trunc",
            "--filter",
            "label=" + _OWNERSHIP_LABEL + "=" + owner,
        ],
        remove=["image", "rm"],
        identifier_pattern=_IMAGE_ID,
    )


class _TerminationRequested(Exception):
    """Internal signal-to-unwind marker; never crosses the public boundary."""


@contextlib.contextmanager
def _termination_as_exception() -> Iterator[None]:
    """Let Python unwind cleanup on Actions SIGINT/SIGTERM without a traceback."""

    def terminate(_signum: int, _frame: FrameType | None) -> None:
        raise _TerminationRequested

    signals = (signal.SIGINT, signal.SIGTERM)
    previous = {number: signal.getsignal(number) for number in signals}
    try:
        for number in signals:
            signal.signal(number, terminate)
        try:
            yield
        except _TerminationRequested:
            raise ValueError("immutable CI unavailable") from None
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def _strict_result(raw: bytes, commit: str, at: datetime) -> TestResultArtifact:
    if len(raw) > MAX_TEST_RESULT_BYTES:
        raise ValueError("immutable CI unavailable")
    artifact = TestResultArtifact.model_validate_json(raw)
    if (
        raw != artifact.canonical_bytes()
        or artifact.status != "passed"
        or artifact.commit_sha != commit
        or artifact.observed_at != at
    ):
        raise ValueError("immutable CI unavailable")
    return artifact


def _run_immutable_check(
    root: Path,
    *,
    at: datetime,
    revision: str | None = None,
    trust_provider: TrustProvider | None = None,
    state_tip: str | None = None,
    owner: str | None = None,
) -> TestResultArtifact:
    """Test and independently consume in separate containers of one exact image."""
    _clear_results(root)
    docker = shutil.which("docker")
    if docker is None:
        raise ValueError("immutable CI unavailable")
    nonce = uuid.uuid4().hex
    owner = nonce if owner is None else owner
    if _OWNER.fullmatch(owner) is None:
        raise ValueError("immutable CI unavailable")
    try:
        dependency_image = (
            _command(
                [docker, "build", "--quiet", "--no-cache", "--force-rm", "-"],
                content=_dependency_context(nonce, owner),
            )
            .decode("ascii")
            .strip()
        )
        if _IMAGE_ID.fullmatch(dependency_image) is None:
            raise ValueError("immutable CI unavailable")
        context = build_context(
            root,
            at=at,
            revision=revision,
            trust_provider=trust_provider,
            state_tip=state_tip,
            dependency_image=dependency_image,
            nonce=nonce,
            owner=owner,
        )
        with tarfile.open(fileobj=io.BytesIO(context), mode="r:") as archive:
            commit_file = archive.extractfile("repository/.git/HEAD")
            state_file = archive.extractfile("repository/.git/refs/remotes/origin/intent-state")
            assert commit_file is not None
            assert state_file is not None
            commit = commit_file.read().decode().strip()
            captured_state_tip = state_file.read().decode().strip()
            baseline = {
                relative: member.read()
                for relative in (*CANONICAL_STATE_PATHS, "cache/shared-state.json")
                if (member := archive.extractfile("baseline/" + relative)) is not None
            }
        if len(baseline) != len(CANONICAL_STATE_PATHS) + 1:
            raise ValueError("immutable CI unavailable")
        baseline_digest = _baseline_digest(baseline)
        image_id = (
            _command(
                [docker, "build", "--network=none", "--quiet", "--no-cache", "--force-rm", "-"],
                content=context,
            )
            .decode("ascii")
            .strip()
        )
        if _IMAGE_ID.fullmatch(image_id) is None:
            raise ValueError("immutable CI unavailable")
        tested = _run_stage(
            docker,
            image_id,
            "test",
            json.dumps({"at": at.isoformat()}).encode(),
            nonce=nonce,
            owner=owner,
        )
        _strict_result(tested, commit, at)
        # The test container no longer exists. Only its strict canonical result,
        # never writable assurance files, crosses into the fresh consumer.
        raw = _run_stage(
            docker,
            image_id,
            "consume",
            json.dumps(
                {
                    "at": at.isoformat(),
                    "baseline_digest": baseline_digest,
                    "result": tested.decode("utf-8"),
                    "state_tip": captured_state_tip,
                }
            ).encode(),
            nonce=nonce,
            owner=owner,
        )
        artifact = _strict_result(raw, commit, at)
        if raw != tested:
            raise ValueError("immutable CI unavailable")
    finally:
        # Legacy --no-cache builds retain no separate BuildKit cache. The unique
        # label resolves only our final/intermediate images, including failed builds.
        _remove_owned_resources(
            docker,
            inventory=[
                docker,
                "image",
                "ls",
                "--all",
                "--quiet",
                "--no-trunc",
                "--filter",
                "label=" + _NONCE_LABEL + "=" + nonce,
            ],
            remove=["image", "rm"],
            identifier_pattern=_IMAGE_ID,
        )
    directory = SecureDirectory.open(root)
    try:
        target = directory.file(_RESULT, create_parents=True)
        try:
            target.atomic_write(raw, reject_target_races=True)
        finally:
            target.close()
    finally:
        directory.close()
    return artifact


def run_immutable_check(
    root: Path,
    *,
    at: datetime,
    revision: str | None = None,
    trust_provider: TrustProvider | None = None,
    state_tip: str | None = None,
    owner: str | None = None,
) -> TestResultArtifact:
    """Scrub decrypted context frames at the public immutable execution boundary."""
    try:
        with _termination_as_exception():
            return _run_immutable_check(
                root,
                at=at,
                revision=revision,
                trust_provider=trust_provider,
                state_tip=state_tip,
                owner=owner,
            )
    except BaseException as error:  # noqa: BLE001 - preserve cancellation, scrub all material
        trust_provider = None
        error.__traceback__ = None
        error.__cause__ = None
        error.__context__ = None
        if isinstance(error, CiTrustError):
            raise error.with_traceback(None) from None
        if isinstance(error, Exception):
            raise ValueError("immutable CI unavailable") from None  # noqa: TRY004
        raise error.with_traceback(None) from None


def _materialize() -> None:
    """Image-build-only trusted materializer using bounded, hash-verified Git objects."""
    build = Path("/build")
    commit = (_ROOT / ".git/HEAD").read_text().strip()
    if _REVISION.fullmatch(commit) is None:
        raise ValueError("immutable CI unavailable")
    _git(_ROOT, ("read-tree", commit), 4096)
    # COPY baseline's contents into /build; only the authenticated allowlist is used.
    from intent_engineering.team_state.restore import CANONICAL_STATE_PATHS

    for relative in (*CANONICAL_STATE_PATHS, "cache/shared-state.json"):
        target = _ROOT / ".intent" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(build / relative, target)
    for relative in (".intent-ci", ".venv", ".pytest_cache", ".mypy_cache", ".ruff_cache"):
        (_ROOT / relative).mkdir(exist_ok=True)
    _command(
        ["/usr/local/bin/python", "-m", "venv", "--system-site-packages", "/project/.venv"],
        timeout=60,
    )
    # Framework-installed console command, not repository-provided shell text.
    Path("/usr/local/bin/intent").write_text(
        "#!/usr/local/bin/python\nfrom intent_engineering.cli.app import app\napp()\n"
    )
    Path("/usr/local/bin/intent").chmod(0o755)
    _git(_ROOT, ("config", "--system", "--add", "safe.directory", "/project"), 1024)


def _workspace(files: dict[str, bytes]) -> Path:
    workspace = Path(tempfile.mkdtemp(prefix="assurance-", dir=_OUTPUT)) / ".intent"
    workspace.mkdir()
    for relative, content in files.items():
        path = workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return workspace


def _baseline_digest(files: dict[str, bytes]) -> str:
    digest = hashlib.sha256(b"intent.authenticated-baseline.v1\0")
    for relative, content in sorted(files.items()):
        encoded = relative.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return "sha256:" + digest.hexdigest()


def _image_baseline() -> dict[str, bytes]:
    directory = SecureDirectory.open(_ROOT)
    try:
        guard = ImmutableExecutionGuard(directory)
        files = {}
        for relative in (*CANONICAL_STATE_PATHS, "cache/shared-state.json"):
            guard.require_file(directory, ".intent/" + relative)
            files[relative] = directory.read_relative(
                ".intent/" + relative, nonblocking=True, max_bytes=8 * 1024 * 1024
            ).content
        return files
    finally:
        directory.close()


class _VerifiedImageBaseline:
    def __init__(self, files: dict[str, bytes]) -> None:
        self._files = files

    def verify_and_restore_approved_baseline(self, root: Path) -> SharedStateRestoreResult:
        directory = SecureDirectory.open(root)
        try:
            guard = ImmutableExecutionGuard(directory)
            for relative, expected in self._files.items():
                guard.require_file(directory, ".intent/" + relative)
                if (
                    directory.read_relative(
                        ".intent/" + relative, nonblocking=True, max_bytes=8 * 1024 * 1024
                    ).content
                    != expected
                ):
                    raise ValueError("immutable CI unavailable")
        finally:
            directory.close()
        return SharedStateRestoreResult(status=SharedStateRestoreStatus.VERIFIED)


def _accepted_result(raw: bytes, evidence_id: str | None) -> bytes:
    """Never export a mutable output replacement after the final consumer accepted it."""
    if len(raw) > MAX_TEST_RESULT_BYTES:
        raise ValueError("immutable CI unavailable")
    artifact = TestResultArtifact.model_validate_json(raw)
    if raw != artifact.canonical_bytes() or artifact.evidence().id != evidence_id:
        raise ValueError("immutable CI unavailable")
    return raw


async def _consume() -> bytes:
    raw = sys.stdin.buffer.read(128 * 1024 + 1)
    if len(raw) > 128 * 1024:
        raise ValueError("immutable CI unavailable")
    request = json.loads(raw)
    if (
        set(request) != {"at", "baseline_digest", "result", "state_tip"}
        or type(request["baseline_digest"]) is not str
        or type(request["result"]) is not str
        or type(request["state_tip"]) is not str
        or re.fullmatch(r"sha256:[0-9a-f]{64}", request["baseline_digest"]) is None
        or _REVISION.fullmatch(request["state_tip"]) is None
    ):
        raise ValueError("immutable CI unavailable")
    at = datetime.fromisoformat(request["at"])
    tested = request["result"].encode("utf-8")
    files = _image_baseline()
    if (
        _baseline_digest(files) != request["baseline_digest"]
        or _git(_ROOT, ("rev-parse", "--verify", "refs/remotes/origin/intent-state"), 256)
        .decode()
        .strip()
        != request["state_tip"]
    ):
        raise ValueError("immutable CI unavailable")
    request.clear()
    raw = b""
    restorer = _VerifiedImageBaseline(files)
    restorer.verify_and_restore_approved_baseline(_ROOT)
    commit = _git(_ROOT, ("rev-parse", "--verify", "HEAD"), 256).decode().strip()
    _strict_result(tested, commit, at)
    _write(_ROOT, _RESULT, tested)
    adapter = CheckRuntimeAdapter(
        _ROOT, assurance_workspace=_workspace(files), shared_state_restorer=restorer
    )
    try:
        result = await CheckService(adapter, clock=lambda: at).run(
            CheckRequest(ci=True, require_review=True, test_results=_RESULT)
        )
        if result.exit_code != 0:
            raise ValueError("immutable CI unavailable")
        return _accepted_result(adapter.read_test_results(_RESULT), result.test_evidence_id)
    finally:
        adapter.close()


async def _test() -> bytes:
    """No decryption key or inherited trust enters the reviewed-code container."""
    raw = sys.stdin.buffer.read(1025)
    if len(raw) > 1024:
        raise ValueError("immutable CI unavailable")
    request = json.loads(raw)
    if set(request) != {"at"}:
        raise ValueError("immutable CI unavailable")
    at = datetime.fromisoformat(request["at"])
    files = _image_baseline()
    artifact = await run_tests(_ROOT, at, assurance_workspace=_workspace(files))
    return artifact.canonical_bytes()


def main() -> int:
    try:
        if sys.argv[1:] == ["materialize"]:
            _materialize()
        elif sys.argv[1:] in (["test"], ["consume"]):
            with contextlib.redirect_stdout(sys.stderr):
                result = anyio.run(_test if sys.argv[1:] == ["test"] else _consume)
            sys.stdout.buffer.write(result)
        elif not sys.argv[1:]:
            run_immutable_check(Path.cwd(), at=datetime.now(UTC))
        else:
            raise ValueError("immutable CI unavailable")
        return 0
    except Exception:  # noqa: BLE001 - never expose trust, decrypted context or runner output
        print("Intent immutable CI unavailable", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
