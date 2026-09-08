"""Protected-branch launcher: proposed commits are Git data, never host tooling."""

from __future__ import annotations

import base64
import os
import re
import signal
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from intent_engineering.integrations.immutable_ci import _git, run_immutable_check
from intent_engineering.intent_workflow.dev_observer import (
    _GIT_ENVIRONMENT,
    _GIT_EXECUTABLE,
    _git_pin_matches,
    _pin_git_executable,
)
from intent_engineering.storage.jsonl.strict import loads_strict_object
from intent_engineering.storage.secure import _read_descriptor
from intent_engineering.team_state.restore import TRUST_ENVIRONMENT_VARIABLE

_REVISION = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")


@dataclass(frozen=True)
class ProtectedRequest:
    revision: str
    repository: str
    number: int | None
    base: str | None = None


def _mapping(value: object) -> Mapping[str, object]:
    if type(value) is not dict:
        raise ValueError("protected CI unavailable")
    return cast(dict[str, object], value)


def _text(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > 1024
        or any(character in value for character in "\0\r\n")
    ):
        raise ValueError("protected CI unavailable")
    return value


def load_request(
    root: Path, event: object, environment: Mapping[str, str], *, state: bool = False
) -> ProtectedRequest:
    """Verify the protected checkout and bounded event fields before using PR data."""
    try:
        payload = _mapping(event)
        repository = _mapping(payload["repository"])
        name = _text(repository["full_name"])
        branch = _text(repository["default_branch"])
        tooling = environment["INTENT_CI_TOOLING_SHA"]
        if (
            _REPOSITORY.fullmatch(name) is None
            or environment["GITHUB_REPOSITORY"] != name
            or environment["GITHUB_SERVER_URL"] != "https://github.com"
            or environment["GITHUB_REF"] != "refs/heads/" + branch
            or _REVISION.fullmatch(tooling) is None
            or _git(root, ("rev-parse", "--verify", "HEAD"), 256).decode().strip() != tooling
        ):
            raise ValueError("unprotected tooling")
        if environment["GITHUB_EVENT_NAME"] == "workflow_dispatch" and not state:
            return ProtectedRequest(tooling, name, None)
        if environment["GITHUB_EVENT_NAME"] != "pull_request_target":
            raise ValueError("unprotected event")
        pull = _mapping(payload["pull_request"])
        base = _mapping(pull["base"])
        number = payload["number"]
        revision = _text(_mapping(pull["head"])["sha"])
        if (
            type(number) is not int
            or not 1 <= number <= 2**31 - 1
            or _text(base["ref"]) != ("intent-state" if state else branch)
            or _text(_mapping(base["repo"])["full_name"]) != name
            or _REVISION.fullmatch(revision) is None
        ):
            raise ValueError("unprotected proposal")
        base_sha = _text(base["sha"]) if state else None
        if base_sha is not None and _REVISION.fullmatch(base_sha) is None:
            raise ValueError("unprotected state base")
        return ProtectedRequest(revision, name, number, base_sha)
    except (KeyError, TypeError, ValueError, OSError):
        raise ValueError("protected CI unavailable") from None


def fetch_proposed_revision(root: Path, request: ProtectedRequest, token: str) -> None:
    """Fetch one bounded PR head with read-only auth; never perform a checkout."""
    if request.number is None:
        return
    if not token or len(token) > 4096 or TRUST_ENVIRONMENT_VARIABLE in os.environ:
        raise ValueError("protected CI unavailable")
    header = (
        "AUTHORIZATION: basic " + base64.b64encode(("x-access-token:" + token).encode()).decode()
    )
    pin = _pin_git_executable()
    process = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            (
                "import os,resource,sys\n"
                "resource.setrlimit(resource.RLIMIT_FSIZE,(134217728,134217728))\n"
                "os.execv(sys.argv[1],sys.argv[1:])\n"
            ),
            _GIT_EXECUTABLE,
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "credential.helper=",
            "-c",
            "fetch.unpackLimit=1",
            "fetch",
            "--no-tags",
            "--depth=64" if request.base is not None else "--depth=1",
            "--no-write-fetch-head",
            "https://github.com/" + request.repository,
            f"+refs/pull/{request.number}/head:refs/intent-ci/proposed",
            *(
                ["+refs/heads/intent-state:refs/remotes/origin/intent-state"]
                if request.base is not None
                else []
            ),
        ],
        cwd=root,
        env={
            **_GIT_ENVIRONMENT,
            "GIT_ALLOW_PROTOCOL": "https",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": header,
        },
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        if process.wait(timeout=120) != 0 or not _git_pin_matches(pin):
            raise ValueError("protected CI unavailable")
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
    if (
        _git(root, ("rev-parse", "--verify", "refs/intent-ci/proposed"), 256).decode().strip()
        != request.revision
    ):
        raise ValueError("protected CI unavailable")
    if (
        request.base is not None
        and _git(root, ("rev-parse", "--verify", "refs/remotes/origin/intent-state"), 256)
        .decode()
        .strip()
        != request.base
    ):
        raise ValueError("protected CI unavailable")


def main(root: Path) -> int:
    try:
        descriptor = os.open(
            os.environ["GITHUB_EVENT_PATH"], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        )
        try:
            event = loads_strict_object(
                _read_descriptor(descriptor, max_bytes=1024 * 1024).decode()
            )
        finally:
            os.close(descriptor)
        request = load_request(
            root, event, os.environ, state=sys.argv[1:] in (["fetch-state"], ["validate-state"])
        )
        if sys.argv[1:] in (["fetch"], ["fetch-state"]):
            fetch_proposed_revision(root, request, os.environ.get("GH_TOKEN", ""))
        elif sys.argv[1:] == ["validate-state"]:
            from intent_engineering.team_state.candidate import validate_candidate
            from intent_engineering.team_state.restore import EnvironmentTrustProvider

            if request.base is None:
                raise ValueError("protected CI unavailable")
            validate_candidate(
                root,
                EnvironmentTrustProvider(),
                base=request.base,
                head=request.revision,
                at=datetime.now(UTC),
            )
        elif sys.argv[1:] == ["check"]:
            if (
                request.number is not None
                and _git(root, ("rev-parse", "--verify", "refs/intent-ci/proposed"), 256)
                .decode()
                .strip()
                != request.revision
            ):
                raise ValueError("protected CI unavailable")
            run_immutable_check(root, at=datetime.now(UTC), revision=request.revision)
        else:
            raise ValueError("protected CI unavailable")
        return 0
    except Exception:  # noqa: BLE001 - no event, credential, PR output, or trust leakage
        print("Intent protected CI unavailable", file=sys.stderr)
        return 1
