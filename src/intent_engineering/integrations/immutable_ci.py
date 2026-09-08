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
from datetime import UTC, datetime
from pathlib import Path

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
from intent_engineering.team_state.restore import (
    TRUST_ENVIRONMENT_VARIABLE,
    EnvironmentTrustProvider,
    read_approved_baseline,
)

_CONTEXT_LIMIT = 256 * 1024 * 1024
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
_REVISION = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_ROOT = Path("/project")
_OUTPUT = Path("/output")
_RESULT = Path(".intent-ci/test-results.json")
_BASE_IMAGE = "python@sha256:581429e3df12d76e6af4be5ab7d0e7fc2013eb57dc23d2de691411c8efdbb970"
_DEPENDENCY_DOCKERFILE = """\
FROM {base}
LABEL intent.ephemeral-ci="{nonce}"
COPY requirements.txt /build/requirements.txt
RUN python -I -m pip install --no-cache-dir --require-hashes --only-binary=:all: -r /build/requirements.txt
"""
_DOCKERFILE = """\
FROM {dependency_image}
LABEL intent.ephemeral-ci="{nonce}"
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


def _dependency_context(nonce: str) -> bytes:
    """Networked dependency build receives only protected lock and trusted Dockerfile."""
    result = io.BytesIO()
    with tarfile.open(fileobj=result, mode="w:") as archive:
        _member(
            archive,
            "Dockerfile",
            _DEPENDENCY_DOCKERFILE.format(base=_BASE_IMAGE, nonce=nonce).encode(),
        )
        _member(archive, "requirements.txt", _requirements())
    return result.getvalue()


def build_context(
    root: Path,
    *,
    at: datetime,
    revision: str | None = None,
    dependency_image: str = _BASE_IMAGE,
    nonce: str | None = None,
) -> bytes:
    """Capture immutable Git objects and authenticated baseline, never checkout bytes."""
    files = read_approved_baseline(root, EnvironmentTrustProvider(), at=at)
    commit = (
        _git(root, ("rev-parse", "--verify", "HEAD"), 256).strip()
        if revision is None
        else revision.encode("ascii")
    )
    if _REVISION.fullmatch(commit.decode("ascii")) is None:
        raise ValueError("immutable CI unavailable")
    state_tip = (
        _git(root, ("rev-parse", "--verify", "refs/remotes/origin/intent-state"), 256)
        .decode()
        .strip()
    )
    material = _git_material(root, commit.decode(), state_tip)
    # The origin is public routing metadata, never a credential-bearing URL.
    trust = EnvironmentTrustProvider().load()
    if trust is None:
        raise ValueError("immutable CI unavailable")
    origin = "https://" + trust.repository_id
    config = f'[core]\nrepositoryformatversion = {int(len(commit) == 64)}\n[remote "origin"]\nurl = {origin}\n'
    if len(commit) == 64:
        config += "[extensions]\nobjectFormat = sha256\n"
    material[".git/config"] = (config.encode("ascii"), 0o644)
    if dependency_image != _BASE_IMAGE and _IMAGE_ID.fullmatch(dependency_image) is None:
        raise ValueError("immutable CI unavailable")
    nonce = uuid.uuid4().hex if nonce is None else nonce
    package = Path(intent_engineering.__file__).parent
    result = io.BytesIO()
    with tarfile.open(fileobj=result, mode="w:") as archive:
        _member(
            archive,
            "Dockerfile",
            _DOCKERFILE.format(nonce=nonce, dependency_image=dependency_image).encode(),
        )
        _member(
            archive,
            "Dependency.Dockerfile",
            _DEPENDENCY_DOCKERFILE.format(base=_BASE_IMAGE, nonce=nonce).encode(),
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


def _run_stage(docker: str, image_id: str, stage: str, content: bytes) -> bytes:
    """Destroy the entire container/cgroup before releasing its bounded result."""
    container = "intent-ci-" + stage + "-" + uuid.uuid4().hex
    try:
        return _command(
            [
                docker,
                "run",
                "--name",
                container,
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


def run_immutable_check(
    root: Path, *, at: datetime, revision: str | None = None
) -> TestResultArtifact:
    """Test and independently consume in separate containers of one exact image."""
    _clear_results(root)
    docker = shutil.which("docker")
    if docker is None:
        raise ValueError("immutable CI unavailable")
    nonce = uuid.uuid4().hex
    try:
        dependency_image = (
            _command(
                [docker, "build", "--quiet", "--no-cache", "--force-rm", "-"],
                content=_dependency_context(nonce),
            )
            .decode("ascii")
            .strip()
        )
        if _IMAGE_ID.fullmatch(dependency_image) is None:
            raise ValueError("immutable CI unavailable")
        context = build_context(
            root, at=at, revision=revision, dependency_image=dependency_image, nonce=nonce
        )
        with tarfile.open(fileobj=io.BytesIO(context), mode="r:") as archive:
            commit_file = archive.extractfile("repository/.git/HEAD")
            assert commit_file is not None
            commit = commit_file.read().decode().strip()
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
        tested = _run_stage(docker, image_id, "test", json.dumps({"at": at.isoformat()}).encode())
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
                    "trust": os.environ.get(TRUST_ENVIRONMENT_VARIABLE, ""),
                    "result": tested.decode("utf-8"),
                }
            ).encode(),
        )
        artifact = _strict_result(raw, commit, at)
        if raw != tested:
            raise ValueError("immutable CI unavailable")
    finally:
        # Legacy --no-cache builds retain no separate BuildKit cache. The unique
        # label resolves only our final/intermediate images, including failed builds.
        inventory = [
            docker,
            "image",
            "ls",
            "--all",
            "--quiet",
            "--no-trunc",
            "--filter",
            "label=intent.ephemeral-ci=" + nonce,
        ]
        identifiers = _command(inventory, timeout=30, maximum=64 * 1024).decode().split()
        if len(identifiers) > 64 or any(
            _IMAGE_ID.fullmatch(identifier) is None for identifier in identifiers
        ):
            raise ValueError("immutable CI unavailable")
        for identifier in dict.fromkeys(identifiers):
            with contextlib.suppress(OSError, ValueError, subprocess.SubprocessError):
                _command([docker, "image", "rm", identifier], timeout=30)
        if _command(inventory, timeout=30, maximum=64 * 1024).strip():
            raise ValueError("immutable CI unavailable")
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
        set(request) != {"at", "trust", "result"}
        or type(request["trust"]) is not str
        or type(request["result"]) is not str
    ):
        raise ValueError("immutable CI unavailable")
    at = datetime.fromisoformat(request["at"])
    tested = request["result"].encode("utf-8")
    os.environ[TRUST_ENVIRONMENT_VARIABLE] = request["trust"]
    try:
        files = read_approved_baseline(_ROOT, EnvironmentTrustProvider(), at=at)
    finally:
        os.environ.pop(TRUST_ENVIRONMENT_VARIABLE, None)
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
    from intent_engineering.team_state.restore import CANONICAL_STATE_PATHS

    raw = sys.stdin.buffer.read(1025)
    if len(raw) > 1024:
        raise ValueError("immutable CI unavailable")
    request = json.loads(raw)
    if set(request) != {"at"}:
        raise ValueError("immutable CI unavailable")
    at = datetime.fromisoformat(request["at"])
    directory = SecureDirectory.open(_ROOT)
    try:
        guard = ImmutableExecutionGuard(directory)
        files = {}
        for relative in (*CANONICAL_STATE_PATHS, "cache/shared-state.json"):
            guard.require_file(directory, ".intent/" + relative)
            files[relative] = directory.read_relative(
                ".intent/" + relative, nonblocking=True, max_bytes=8 * 1024 * 1024
            ).content
    finally:
        directory.close()
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
