"""Repository-bound verification and atomic restore of approved shared intent state.

The protected Git ref stores a readable canonical manifest, a canonical JSON
encryption envelope, and a canonical signature envelope.  Decrypted state is a
canonical JSON object with a sorted list of ``{path,size,sha256,content_base64}``
entries.  Paths are selected from :data:`CANONICAL_STATE_PATHS`; no decrypted
path is ever passed to a filesystem API before that allowlist check.
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import re
import secrets
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Annotated, Final, Literal, Protocol, Self
from urllib.parse import urlparse

import yaml  # type: ignore[import-untyped]
from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pydantic import ConfigDict, Field, ValidationInfo, field_validator, model_validator

from intent_engineering.capture.mcp.profile_loader import load_strict_yaml_mapping_bytes
from intent_engineering.cli.writes import MutationPolicy
from intent_engineering.core.models import ChangeSet, ProjectConfig
from intent_engineering.core.models._base import StrictModel
from intent_engineering.intent_workflow.check import (
    SharedStateRestoreResult,
    SharedStateRestoreStatus,
)
from intent_engineering.mutations.models import ApprovalRecord, WritePlan
from intent_engineering.storage.jsonl.approval_store import parse_immutable_records
from intent_engineering.storage.jsonl.strict import loads_strict_object
from intent_engineering.storage.secure import (
    SecureDirectory,
    SecureFile,
    UnsafePathError,
    configured_graph_relative,
)
from intent_engineering.storage.transaction import LocalTransactionCoordinator
from intent_engineering.storage.yaml.graph_store import parse_graph
from intent_engineering.validation import validate_canonical_snapshot

ALGORITHM: Final = "x25519-hkdf-sha256-aes256gcm-v1"
SIGNATURE_ALGORITHM: Final = "ed25519-v1"
STATE_REF = "refs/remotes/origin/intent-state"
TRUST_ENVIRONMENT_VARIABLE = "INTENT_CI_SHARED_STATE_TRUST"
MAX_MANIFEST_BYTES = 64 * 1024
MAX_SIGNATURE_BYTES = 64 * 1024
MAX_BUNDLE_BYTES = 24 * 1024 * 1024
MAX_STATE_BYTES = 16 * 1024 * 1024
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_STATE_FILES = 32
MAX_TRUST_BYTES = 32 * 1024
MAX_GIT_TEXT_BYTES = 4096
MAX_FETCH_OUTPUT_BYTES = 64 * 1024
MAX_GIT_EXECUTABLE_BYTES = 16 * 1024 * 1024
_FETCH_TIMEOUT_SECONDS = 10.0
MAX_CLOCK_SKEW = timedelta(minutes=5)
MAX_ANCESTRY_COMMITS = 64

CANONICAL_STATE_PATHS = (
    "approvals/approvals.jsonl",
    "approvals/plans.jsonl",
    "approvals/policy.yaml",
    "approvals/receipts.jsonl",
    "config.yaml",
    "evidence/evidence.jsonl",
    "graph.yaml",
    "history/changesets.jsonl",
    "history/intent-proposals.jsonl",
    "reconciliation/cases.jsonl",
)
_REQUIRED_PATHS = frozenset(CANONICAL_STATE_PATHS)
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_KEY_ID = re.compile(r"^[a-z][a-z0-9._:-]{0,127}$")
_PROJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_REPOSITORY_ID = re.compile(r"^[a-z0-9.-]+/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_GIT_EXECUTABLE = Path("/usr/bin/git")
_FETCH_ENVIRONMENT: Mapping[str, str] = {
    "GIT_ASKPASS": "/usr/bin/false",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_NO_REPLACE_OBJECTS": "1",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_TERMINAL_PROMPT": "0",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "PATH": "/usr/bin:/bin",
    "SSH_ASKPASS": "/usr/bin/false",
}
_FETCH_SUPERVISOR = (
    "import os,subprocess,sys,time;"
    "fd=int(sys.argv[1]);"
    "child=subprocess.Popen(sys.argv[2:],stdin=subprocess.DEVNULL);"
    "code=child.wait();"
    "os.write(fd,(str(code)+'\\n').encode('ascii'));"
    "os.close(fd);"
    "time.sleep(3600)"
)
_FETCH_GIT_OPTIONS = (
    "--no-pager",
    "--no-replace-objects",
    "-c",
    "core.askPass=",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.sshCommand=/usr/bin/false",
    "-c",
    "credential.helper=",
    "-c",
    "credential.interactive=never",
    "-c",
    "fetch.fsckObjects=true",
    "-c",
    "gc.auto=0",
    "-c",
    "http.extraHeader=",
    "-c",
    "http.proxy=",
    "-c",
    "https.proxy=",
    "-c",
    "protocol.allow=never",
    "-c",
    "protocol.ext.allow=never",
    "-c",
    "protocol.file.allow=never",
    "-c",
    "protocol.git.allow=never",
    "-c",
    "protocol.ssh.allow=never",
    "-c",
    "protocol.https.allow=always",
    "-c",
    "submodule.recurse=false",
)
_GIT_COMMIT = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


class _RestoreModel(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def _json_tuple(value: object, info: ValidationInfo) -> object:
    if info.mode == "json" and type(value) is list:
        return tuple(value)
    if info.mode == "python" and type(value) is not tuple:
        raise ValueError("invalid shared-state collection")
    return value


def _unique_sorted(values: tuple[str, ...]) -> tuple[str, ...]:
    if not values or values != tuple(sorted(values)) or len(values) != len(set(values)):
        raise ValueError("invalid shared-state identifiers")
    if any(_KEY_ID.fullmatch(value) is None for value in values):
        raise ValueError("invalid shared-state identifier")
    return values


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _digest(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _b64encode(content: bytes) -> str:
    return base64.urlsafe_b64encode(content).rstrip(b"=").decode("ascii")


def _b64decode(
    value: str,
    *,
    expected_size: int | None = None,
    allow_empty: bool = False,
) -> bytes:
    if (
        type(value) is not str
        or (not value and not allow_empty)
        or len(value) > MAX_BUNDLE_BYTES * 2
    ):
        raise ValueError("invalid shared-state base64")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, UnicodeError) as error:
        raise ValueError("invalid shared-state base64") from error
    if _b64encode(decoded) != value or (
        expected_size is not None and len(decoded) != expected_size
    ):
        raise ValueError("invalid shared-state base64")
    return decoded


class SharedStateManifest(_RestoreModel):
    """The only readable metadata stored on the protected state ref."""

    schema_version: Literal[1] = 1
    project_id: Annotated[str, Field(pattern=_PROJECT_ID.pattern)]
    repository_id: Annotated[str, Field(pattern=_REPOSITORY_ID.pattern)]
    graph_version: Annotated[int, Field(ge=1)]
    parent_bundle_digest: Annotated[str, Field(pattern=_SHA256.pattern)] | None = None
    bundle_digest: Annotated[str, Field(pattern=_SHA256.pattern)]
    bundle_size: Annotated[int, Field(gt=0, le=MAX_BUNDLE_BYTES)]
    encryption_algorithm: Literal["x25519-hkdf-sha256-aes256gcm-v1"] = ALGORITHM
    recipient_key_ids: Annotated[tuple[str, ...], Field(max_length=64)]
    required_signature_ids: Annotated[tuple[str, ...], Field(max_length=64)]
    created_at: datetime

    @field_validator("schema_version", "graph_version", "bundle_size", mode="before")
    @classmethod
    def require_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("invalid shared-state integer")
        return value

    @field_validator("recipient_key_ids", "required_signature_ids", mode="before")
    @classmethod
    def require_json_lists(cls, value: object, info: ValidationInfo) -> object:
        return _json_tuple(value, info)

    @field_validator("recipient_key_ids", "required_signature_ids")
    @classmethod
    def require_sorted_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _unique_sorted(values)

    @field_validator("created_at")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("shared-state time must be UTC")
        return value.astimezone(UTC)


class WrappedContentKey(_RestoreModel):
    recipient_key_id: Annotated[str, Field(pattern=_KEY_ID.pattern)]
    nonce: str
    ciphertext: str


class EncryptedStateBundle(_RestoreModel):
    schema_version: Literal[1] = 1
    algorithm: Literal["x25519-hkdf-sha256-aes256gcm-v1"] = ALGORITHM
    ephemeral_public_key: str
    nonce: str
    ciphertext: str
    wrapped_keys: Annotated[tuple[WrappedContentKey, ...], Field(min_length=1, max_length=64)]

    @field_validator("wrapped_keys", mode="before")
    @classmethod
    def require_wrapped_list(cls, value: object, info: ValidationInfo) -> object:
        return _json_tuple(value, info)

    @model_validator(mode="after")
    def require_sorted_wrapped_keys(self) -> EncryptedStateBundle:
        ids = tuple(item.recipient_key_id for item in self.wrapped_keys)
        _unique_sorted(ids)
        _b64decode(self.ephemeral_public_key, expected_size=32)
        _b64decode(self.nonce, expected_size=12)
        _b64decode(self.ciphertext)
        for item in self.wrapped_keys:
            _b64decode(item.nonce, expected_size=12)
            _b64decode(item.ciphertext)
        return self


class StateSignature(_RestoreModel):
    signature_id: Annotated[str, Field(pattern=_KEY_ID.pattern)]
    algorithm: Literal["ed25519-v1"] = SIGNATURE_ALGORITHM
    signature: str


class StateSignatureEnvelope(_RestoreModel):
    schema_version: Literal[1] = 1
    manifest_digest: Annotated[str, Field(pattern=_SHA256.pattern)]
    bundle_digest: Annotated[str, Field(pattern=_SHA256.pattern)]
    signatures: Annotated[tuple[StateSignature, ...], Field(min_length=1, max_length=64)]

    @field_validator("signatures", mode="before")
    @classmethod
    def require_signature_list(cls, value: object, info: ValidationInfo) -> object:
        return _json_tuple(value, info)

    @model_validator(mode="after")
    def require_sorted_signatures(self) -> StateSignatureEnvelope:
        _unique_sorted(tuple(item.signature_id for item in self.signatures))
        for item in self.signatures:
            _b64decode(item.signature, expected_size=64)
        return self


class StatePayloadEntry(_RestoreModel):
    path: str
    size: Annotated[int, Field(ge=0, le=MAX_FILE_BYTES)]
    sha256: Annotated[str, Field(pattern=_SHA256.pattern)]
    content_base64: str

    @field_validator("size", mode="before")
    @classmethod
    def require_integer_size(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("invalid shared-state size")
        return value


class StatePayload(_RestoreModel):
    schema_version: Literal[1] = 1
    state_digest: Annotated[str, Field(pattern=_SHA256.pattern)]
    entries: Annotated[
        tuple[StatePayloadEntry, ...], Field(min_length=1, max_length=MAX_STATE_FILES)
    ]

    @field_validator("entries", mode="before")
    @classmethod
    def require_entry_list(cls, value: object, info: ValidationInfo) -> object:
        return _json_tuple(value, info)


@dataclass(frozen=True)
class TrustedSigningKey:
    signature_id: str
    public_key: bytes

    def __post_init__(self) -> None:
        if _KEY_ID.fullmatch(self.signature_id) is None or len(self.public_key) != 32:
            raise ValueError("invalid trusted signing key")
        Ed25519PublicKey.from_public_bytes(self.public_key)


@dataclass(frozen=True)
class SharedStateTrust:
    project_id: str
    repository_id: str
    recipient_key_id: str
    recipient_private_key: bytes
    signing_keys: tuple[TrustedSigningKey, ...]

    def __post_init__(self) -> None:
        if (
            _PROJECT_ID.fullmatch(self.project_id) is None
            or _REPOSITORY_ID.fullmatch(self.repository_id) is None
            or _KEY_ID.fullmatch(self.recipient_key_id) is None
            or len(self.recipient_private_key) != 32
            or not self.signing_keys
        ):
            raise ValueError("invalid shared-state trust")
        ids = tuple(item.signature_id for item in self.signing_keys)
        _unique_sorted(ids)
        X25519PrivateKey.from_private_bytes(self.recipient_private_key)


class TrustProvider(Protocol):
    def load(self) -> SharedStateTrust | None: ...


@dataclass(frozen=True)
class StaticTrustProvider:
    trust: SharedStateTrust | None

    def load(self) -> SharedStateTrust | None:
        return self.trust


class _EnvironmentSigningKey(_RestoreModel):
    signature_id: str
    public_key_base64: str


class _EnvironmentTrust(_RestoreModel):
    schema_version: Literal[1] = 1
    project_id: str
    repository_id: str
    recipient_key_id: str
    recipient_private_key_base64: str
    signing_keys: tuple[_EnvironmentSigningKey, ...]

    @field_validator("signing_keys", mode="before")
    @classmethod
    def require_signing_list(cls, value: object, info: ValidationInfo) -> object:
        return _json_tuple(value, info)


class EnvironmentTrustProvider:
    """Load bounded CI trust material from one fixed process environment variable."""

    def __init__(self, environment: Mapping[str, str] | None = None) -> None:
        self._environment = os.environ if environment is None else environment

    def load(self) -> SharedStateTrust | None:
        raw = self._environment.get(TRUST_ENVIRONMENT_VARIABLE)
        if raw is None:
            return None
        encoded = raw.encode("utf-8")
        if not encoded or len(encoded) > MAX_TRUST_BYTES:
            raise ValueError("shared-state trust unavailable")
        loads_strict_object(encoded.decode("utf-8"))
        parsed = _EnvironmentTrust.model_validate_json(encoded)
        return SharedStateTrust(
            project_id=parsed.project_id,
            repository_id=parsed.repository_id,
            recipient_key_id=parsed.recipient_key_id,
            recipient_private_key=_b64decode(
                parsed.recipient_private_key_base64,
                expected_size=32,
            ),
            signing_keys=tuple(
                TrustedSigningKey(
                    signature_id=item.signature_id,
                    public_key=_b64decode(item.public_key_base64, expected_size=32),
                )
                for item in parsed.signing_keys
            ),
        )


@dataclass(frozen=True)
class SharedStateArtifacts:
    manifest: bytes
    bundle: bytes
    signatures: bytes
    bundle_path: str
    signature_path: str


def _manifest_aad(
    *,
    project_id: str,
    repository_id: str,
    graph_version: int,
    parent_bundle_digest: str | None,
    created_at: datetime,
    recipient_key_ids: tuple[str, ...],
    required_signature_ids: tuple[str, ...],
) -> bytes:
    return _canonical_json(
        {
            "schema_version": 1,
            "project_id": project_id,
            "repository_id": repository_id,
            "graph_version": graph_version,
            "parent_bundle_digest": parent_bundle_digest,
            "encryption_algorithm": ALGORITHM,
            "recipient_key_ids": list(recipient_key_ids),
            "required_signature_ids": list(required_signature_ids),
            "created_at": created_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        }
    )


def _validate_state_path(path: str) -> str:
    if (
        type(path) is not str
        or path not in _REQUIRED_PATHS
        or path.startswith("/")
        or "\\" in path
        or any(part in {"", ".", ".."} for part in path.split("/"))
    ):
        raise ValueError("invalid shared-state path")
    return path


def _entries_digest(entries: list[dict[str, object]]) -> str:
    return _digest(_canonical_json({"schema_version": 1, "entries": entries}))


def build_state_payload(files: Mapping[str, bytes]) -> bytes:
    """Build the bounded canonical plaintext JSON payload from exact approved files."""
    if type(files) is not dict or set(files) != _REQUIRED_PATHS:
        raise ValueError("incomplete shared-state payload")
    entries: list[dict[str, object]] = []
    total = 0
    for path in sorted(files):
        content = files[path]
        _validate_state_path(path)
        if type(content) is not bytes or len(content) > MAX_FILE_BYTES:
            raise ValueError("invalid shared-state content")
        total += len(content)
        if total > MAX_STATE_BYTES:
            raise ValueError("shared-state payload is oversized")
        entries.append(
            {
                "path": path,
                "size": len(content),
                "sha256": _digest(content),
                "content_base64": _b64encode(content),
            }
        )
    return _canonical_json(
        {
            "schema_version": 1,
            "state_digest": _entries_digest(entries),
            "entries": entries,
        }
    )


def _parse_payload(content: bytes) -> dict[str, bytes]:
    if not content or len(content) > MAX_BUNDLE_BYTES:
        raise ValueError("invalid shared-state payload")
    loads_strict_object(content.decode("utf-8"))
    payload = StatePayload.model_validate_json(content)
    if content != _canonical_json(payload.model_dump(mode="json")):
        raise ValueError("noncanonical shared-state payload")
    paths = tuple(item.path for item in payload.entries)
    if (
        paths != tuple(sorted(paths))
        or len(paths) != len(set(paths))
        or set(paths) != _REQUIRED_PATHS
    ):
        raise ValueError("invalid shared-state inventory")
    entries_for_digest: list[dict[str, object]] = []
    files: dict[str, bytes] = {}
    total = 0
    for entry in payload.entries:
        path = _validate_state_path(entry.path)
        decoded = _b64decode(entry.content_base64, allow_empty=True)
        if len(decoded) != entry.size or _digest(decoded) != entry.sha256:
            raise ValueError("invalid shared-state entry")
        total += len(decoded)
        if total > MAX_STATE_BYTES:
            raise ValueError("shared-state payload is oversized")
        entries_for_digest.append(entry.model_dump(mode="json"))
        files[path] = decoded
    if payload.state_digest != _entries_digest(entries_for_digest):
        raise ValueError("invalid shared-state digest")
    return files


def _derive_wrapping_key(shared_secret: bytes, aad: bytes, recipient_id: str) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=b"intent.shared-state.wrap.v1\0" + aad + b"\0" + recipient_id.encode(),
    ).derive(shared_secret)


def seal_state_payload(
    plaintext: bytes,
    *,
    project_id: str,
    repository_id: str,
    graph_version: int,
    parent_bundle_digest: str | None,
    created_at: datetime,
    recipient_public_keys: Mapping[str, bytes],
    signing_private_keys: Mapping[str, bytes],
    schema_version: int = 1,
) -> SharedStateArtifacts:
    """Create pure signed/encrypted bytes; this function performs no publication."""
    if schema_version != 1:
        raise ValueError("unsupported shared-state schema")
    recipient_ids = tuple(sorted(recipient_public_keys))
    signer_ids = tuple(sorted(signing_private_keys))
    _unique_sorted(recipient_ids)
    _unique_sorted(signer_ids)
    aad = _manifest_aad(
        project_id=project_id,
        repository_id=repository_id,
        graph_version=graph_version,
        parent_bundle_digest=parent_bundle_digest,
        created_at=created_at,
        recipient_key_ids=recipient_ids,
        required_signature_ids=signer_ids,
    )
    content_key = AESGCM.generate_key(bit_length=256)
    payload_nonce = os.urandom(12)
    ephemeral = X25519PrivateKey.generate()
    wrapped: list[WrappedContentKey] = []
    for recipient_id in recipient_ids:
        public_bytes = recipient_public_keys[recipient_id]
        if type(public_bytes) is not bytes or len(public_bytes) != 32:
            raise ValueError("invalid recipient public key")
        shared = ephemeral.exchange(X25519PublicKey.from_public_bytes(public_bytes))
        wrapping_key = _derive_wrapping_key(shared, aad, recipient_id)
        nonce = os.urandom(12)
        wrapped.append(
            WrappedContentKey(
                recipient_key_id=recipient_id,
                nonce=_b64encode(nonce),
                ciphertext=_b64encode(
                    AESGCM(wrapping_key).encrypt(
                        nonce,
                        content_key,
                        aad + b"\0" + recipient_id.encode(),
                    )
                ),
            )
        )
    bundle_model = EncryptedStateBundle(
        ephemeral_public_key=_b64encode(ephemeral.public_key().public_bytes_raw()),
        nonce=_b64encode(payload_nonce),
        ciphertext=_b64encode(AESGCM(content_key).encrypt(payload_nonce, plaintext, aad)),
        wrapped_keys=tuple(wrapped),
    )
    bundle = _canonical_json(bundle_model.model_dump(mode="json"))
    manifest_model = SharedStateManifest(
        project_id=project_id,
        repository_id=repository_id,
        graph_version=graph_version,
        parent_bundle_digest=parent_bundle_digest,
        bundle_digest=_digest(bundle),
        bundle_size=len(bundle),
        recipient_key_ids=recipient_ids,
        required_signature_ids=signer_ids,
        created_at=created_at,
    )
    manifest = _canonical_json(manifest_model.model_dump(mode="json"))
    signed = _canonical_json(
        {
            "schema_version": 1,
            "manifest_digest": _digest(manifest),
            "bundle_digest": manifest_model.bundle_digest,
        }
    )
    signatures = tuple(
        StateSignature(
            signature_id=signature_id,
            signature=_b64encode(
                Ed25519PrivateKey.from_private_bytes(signing_private_keys[signature_id]).sign(
                    signed
                )
            ),
        )
        for signature_id in signer_ids
    )
    signature_envelope = _canonical_json(
        StateSignatureEnvelope(
            manifest_digest=_digest(manifest),
            bundle_digest=manifest_model.bundle_digest,
            signatures=signatures,
        ).model_dump(mode="json")
    )
    digest_hex = manifest_model.bundle_digest.removeprefix("sha256:")
    name = f"{graph_version}-{digest_hex}"
    return SharedStateArtifacts(
        manifest=manifest,
        bundle=bundle,
        signatures=signature_envelope,
        bundle_path=f"bundles/{name}.intent",
        signature_path=f"signatures/{name}.json",
    )


class _Unavailable(ValueError):
    pass


class _UpgradeRequired(ValueError):
    pass


class _Stale(ValueError):
    pass


class _Diverged(ValueError):
    pass


def _parse_canonical_model(
    content: bytes, model: type[_RestoreModel], maximum: int
) -> _RestoreModel:
    if not content or len(content) > maximum:
        raise ValueError("invalid shared-state object")
    loaded = loads_strict_object(content.decode("utf-8"))
    version = loaded.get("schema_version")
    if type(version) is int and version != 1:
        raise _UpgradeRequired("unsupported shared-state schema")
    parsed = model.model_validate_json(content)
    if content != _canonical_json(parsed.model_dump(mode="json")):
        raise ValueError("noncanonical shared-state object")
    return parsed


def _run_git(repo: Path, arguments: tuple[str, ...], *, maximum: int) -> bytes:
    git_token = _git_executable_token()
    process = subprocess.Popen(
        (
            str(_GIT_EXECUTABLE),
            "--no-pager",
            "--no-replace-objects",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
            "-C",
            str(repo),
            *arguments,
        ),
        cwd="/",
        env=dict(_FETCH_ENVIRONMENT),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        shell=False,
    )
    output = process.stdout
    assert output is not None
    selector = selectors.DefaultSelector()
    selector.register(output, selectors.EVENT_READ)
    deadline = time.monotonic() + 10
    chunks: list[bytes] = []
    retained = 0
    try:
        while True:
            remaining_time = deadline - time.monotonic()
            if remaining_time <= 0:
                raise subprocess.TimeoutExpired(process.args, 10)
            if not selector.select(remaining_time):
                raise subprocess.TimeoutExpired(process.args, 10)
            chunk = os.read(output.fileno(), min(65536, maximum + 1 - retained))
            if not chunk:
                break
            chunks.append(chunk)
            retained += len(chunk)
            if retained > maximum:
                raise ValueError("Git shared-state output is oversized")
        return_code = process.wait(timeout=max(0.001, deadline - time.monotonic()))
        if return_code != 0:
            raise subprocess.CalledProcessError(return_code, process.args)
        if _git_executable_token() != git_token:
            raise ValueError("Git executable unavailable")
        return b"".join(chunks)
    finally:
        selector.close()
        output.close()
        if process.poll() is None:
            process.kill()
            process.wait()


def _git_executable_token() -> tuple[int, int, int, int, str]:
    descriptor = os.open(
        _GIT_EXECUTABLE,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    content = bytearray()
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_mode & 0o022
            or metadata.st_size > MAX_GIT_EXECUTABLE_BYTES
        ):
            raise ValueError("Git executable unavailable")
        while chunk := os.read(descriptor, 64 * 1024):
            content.extend(chunk)
            if len(content) > MAX_GIT_EXECUTABLE_BYTES:
                raise ValueError("Git executable unavailable")
        return (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mtime_ns,
            metadata.st_size,
            hashlib.sha256(content).hexdigest(),
        )
    finally:
        content.clear()
        os.close(descriptor)


def _stop_fetch_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (PermissionError, ProcessLookupError):
        return
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass


def _run_bounded_fetch_git(arguments: tuple[str, ...]) -> None:
    """Run pinned Git below a live group leader retained until bounded cleanup."""
    process: subprocess.Popen[bytes] | None = None
    selector = selectors.DefaultSelector()
    git_token: tuple[int, int, int, int, str] | None = None
    status_read = -1
    status_write = -1
    status = bytearray()
    try:
        git_token = _git_executable_token()
        status_read, status_write = os.pipe()
        os.set_inheritable(status_read, False)
        os.set_inheritable(status_write, True)
        argv = (
            sys.executable,
            "-I",
            "-S",
            "-c",
            _FETCH_SUPERVISOR,
            str(status_write),
            str(_GIT_EXECUTABLE),
            *_FETCH_GIT_OPTIONS,
            *arguments,
        )
        process = subprocess.Popen(
            argv,
            cwd="/",
            env=dict(_FETCH_ENVIRONMENT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            pass_fds=(status_write,),
            start_new_session=True,
            shell=False,
            bufsize=0,
        )
        assert process.stdout is not None
        assert process.stderr is not None
        os.close(status_write)
        status_write = -1
        selector.register(process.stdout, selectors.EVENT_READ)
        selector.register(process.stderr, selectors.EVENT_READ)
        selector.register(status_read, selectors.EVENT_READ)
        total = 0
        deadline = time.monotonic() + _FETCH_TIMEOUT_SECONDS
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("shared-state fetch unavailable")
            events = selector.select(remaining)
            if not events:
                raise TimeoutError("shared-state fetch unavailable")
            for key, _mask in events:
                file_object = key.fileobj
                descriptor = file_object if isinstance(file_object, int) else file_object.fileno()
                chunk = os.read(descriptor, 64 * 1024)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if descriptor == status_read:
                    status.extend(chunk)
                    if len(status) > 16 or b"\n" not in status:
                        if len(status) > 16:
                            raise ValueError("shared-state fetch unavailable")
                        continue
                    _stop_fetch_process(process)
                    continue
                total += len(chunk)
                if total > MAX_FETCH_OUTPUT_BYTES:
                    raise ValueError("shared-state fetch unavailable")
        if bytes(status) != b"0\n":
            raise ValueError("shared-state fetch unavailable")
        if _git_executable_token() != git_token:
            raise ValueError("shared-state fetch unavailable")
    except (OSError, subprocess.SubprocessError, TimeoutError, ValueError) as error:
        error.__traceback__ = None
        raise _Unavailable("shared-state ref unavailable") from None
    finally:
        selector.close()
        status.clear()
        if status_write >= 0:
            os.close(status_write)
        if status_read >= 0:
            os.close(status_read)
        if process is not None:
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
            if process.poll() is None:
                _stop_fetch_process(process)
        process = None
        git_token = None


def _trusted_github_url(repository_id: str) -> str:
    matched = re.fullmatch(
        r"github\.com/([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/([A-Za-z0-9][A-Za-z0-9_.-]{0,99})",
        repository_id,
    )
    if matched is None or matched.group(2) in {".", ".."} or matched.group(2).endswith(".git"):
        raise _Unavailable("shared-state ref unavailable")
    owner, repository = matched.groups()
    return f"https://github.com/{owner}/{repository}.git"


def _refresh_state_ref(repository_id: str) -> _GitRefReader:
    """Fetch the protected ref in a clean repository derived only from trusted identity."""
    temporary: tempfile.TemporaryDirectory[str] | None = None
    try:
        transport = _trusted_github_url(repository_id)
        temporary = tempfile.TemporaryDirectory(prefix="intent-state-fetch-")
        root = Path(temporary.name)
        _run_bounded_fetch_git(("init", "--bare", "--quiet", str(root)))
        _run_bounded_fetch_git(
            (
                "-C",
                str(root),
                "fetch",
                "--force",
                "--no-tags",
                "--no-recurse-submodules",
                "--no-write-fetch-head",
                "--quiet",
                transport,
                "refs/heads/intent-state:refs/remotes/origin/intent-state",
            )
        )
        reader = _GitRefReader(root, temporary=temporary)
        temporary = None
        return reader
    except _Unavailable:
        raise
    except (OSError, subprocess.SubprocessError, TimeoutError, ValueError) as error:
        error.__traceback__ = None
        raise _Unavailable("shared-state ref unavailable") from None
    finally:
        if temporary is not None:
            temporary.cleanup()


def _origin_repository(repo: Path) -> str:
    raw = _run_git(
        repo,
        ("config", "--local", "--no-includes", "--get", "remote.origin.url"),
        maximum=MAX_GIT_TEXT_BYTES,
    )
    value = raw.decode("utf-8").strip()
    if re.fullmatch(r"[^/@:]+@([^/:]+):(.+)", value):
        matched = re.fullmatch(r"[^/@:]+@([^/:]+):(.+)", value)
        assert matched is not None
        host, path = matched.groups()
    else:
        parsed = urlparse(value)
        host, path = parsed.hostname or "", parsed.path.lstrip("/")
    path = path.removesuffix(".git")
    identity = f"{host.lower()}/{path}"
    if _REPOSITORY_ID.fullmatch(identity) is None:
        raise ValueError("invalid Git repository identity")
    return identity


class _GitRefReader:
    def __init__(
        self,
        root: Path,
        *,
        temporary: tempfile.TemporaryDirectory[str] | None = None,
    ) -> None:
        self._root = root
        self._temporary = temporary

    def close(self) -> None:
        temporary = self._temporary
        self._temporary = None
        if temporary is not None:
            temporary.cleanup()

    def commit(self) -> str:
        try:
            raw = _run_git(
                self._root,
                ("rev-parse", "--verify", "--quiet", "--end-of-options", f"{STATE_REF}^{{commit}}"),
                maximum=128,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise _Unavailable("shared-state ref unavailable") from error
        commit = raw.decode("ascii").strip()
        if _GIT_COMMIT.fullmatch(commit) is None:
            raise ValueError("invalid shared-state commit")
        return commit

    def parents(self, commit: str) -> tuple[str, ...]:
        # Read the immutable object itself: rev-list hides parents at shallow
        # boundaries and may apply local history grafts.
        raw = _run_git(self._root, ("cat-file", "commit", commit), maximum=MAX_MANIFEST_BYTES)
        header = raw.split(b"\n\n", 1)[0].split(b"\n")
        parents = tuple(line[7:].decode("ascii") for line in header if line.startswith(b"parent "))
        if not header or any(_GIT_COMMIT.fullmatch(item) is None for item in parents):
            raise ValueError("invalid shared-state lineage")
        if len(parents) > 1:
            raise ValueError("shared-state merges are unsupported")
        return parents

    def blob(self, commit: str, path: str, maximum: int) -> bytes:
        if (
            type(path) is not str
            or not path
            or path.startswith("/")
            or "\\" in path
            or any(part in {"", ".", ".."} for part in path.split("/"))
        ):
            raise ValueError("invalid shared-state Git path")
        listing = _run_git(
            self._root,
            ("ls-tree", "-z", "--full-tree", commit, "--", path),
            maximum=MAX_GIT_TEXT_BYTES,
        )
        if not listing.endswith(b"\0") or listing.count(b"\0") != 1:
            raise ValueError("shared-state blob unavailable")
        metadata, listed_path = listing[:-1].split(b"\t", 1)
        fields = metadata.split()
        if (
            listed_path.decode("utf-8") != path
            or len(fields) != 3
            or fields[0] != b"100644"
            or fields[1] != b"blob"
        ):
            raise ValueError("unsafe shared-state blob")
        object_id = fields[2].decode("ascii")
        size_raw = (
            _run_git(
                self._root,
                ("cat-file", "-s", object_id),
                maximum=64,
            )
            .decode("ascii")
            .strip()
        )
        if not size_raw.isascii() or not size_raw.isdigit():
            raise ValueError("invalid shared-state blob size")
        size = int(size_raw)
        if size <= 0 or size > maximum:
            raise ValueError("shared-state blob is oversized")
        content = _run_git(
            self._root,
            ("cat-file", "blob", object_id),
            maximum=maximum,
        )
        if len(content) != size:
            raise ValueError("invalid shared-state blob")
        return content


@dataclass(frozen=True)
class _VerifiedRelease:
    manifest: SharedStateManifest
    commit: str


@dataclass(frozen=True)
class _VerifiedLineage:
    tip: _VerifiedRelease
    baseline: _VerifiedRelease | None


def _release_name(manifest: SharedStateManifest) -> str:
    digest_hex = manifest.bundle_digest.removeprefix("sha256:")
    return f"{manifest.graph_version}-{digest_hex}"


def _verify_release_manifest(
    reader: _GitRefReader,
    commit: str,
    trust: SharedStateTrust,
    now: datetime,
) -> _VerifiedRelease:
    manifest_bytes = reader.blob(commit, "manifest.json", MAX_MANIFEST_BYTES)
    parsed = _parse_canonical_model(
        manifest_bytes,
        SharedStateManifest,
        MAX_MANIFEST_BYTES,
    )
    assert isinstance(parsed, SharedStateManifest)
    if parsed.project_id != trust.project_id or parsed.repository_id != trust.repository_id:
        raise ValueError("shared-state identity mismatch")
    if parsed.created_at > now + MAX_CLOCK_SKEW:
        raise ValueError("invalid shared-state time")
    signature_bytes = reader.blob(
        commit,
        f"signatures/{_release_name(parsed)}.json",
        MAX_SIGNATURE_BYTES,
    )
    signature_envelope = _parse_canonical_model(
        signature_bytes,
        StateSignatureEnvelope,
        MAX_SIGNATURE_BYTES,
    )
    assert isinstance(signature_envelope, StateSignatureEnvelope)
    if (
        signature_envelope.manifest_digest != _digest(manifest_bytes)
        or signature_envelope.bundle_digest != parsed.bundle_digest
        or tuple(item.signature_id for item in signature_envelope.signatures)
        != parsed.required_signature_ids
    ):
        raise ValueError("shared-state signatures mismatch")
    trusted = {item.signature_id: item.public_key for item in trust.signing_keys}
    if tuple(sorted(trusted)) != parsed.required_signature_ids:
        raise ValueError("shared-state signer policy mismatch")
    signed = _canonical_json(
        {
            "schema_version": 1,
            "manifest_digest": _digest(manifest_bytes),
            "bundle_digest": parsed.bundle_digest,
        }
    )
    for signature in signature_envelope.signatures:
        Ed25519PublicKey.from_public_bytes(trusted[signature.signature_id]).verify(
            _b64decode(signature.signature, expected_size=64),
            signed,
        )
    return _VerifiedRelease(parsed, commit)


def _verify_release_lineage(
    reader: _GitRefReader,
    tip_commit: str,
    trust: SharedStateTrust,
    now: datetime,
    marker: Mapping[str, object] | None,
) -> _VerifiedLineage:
    """Authenticate the complete bounded chain; an unsigned marker is not a checkpoint."""
    commit = tip_commit
    expected_digest: str | None = None
    tip: _VerifiedRelease | None = None
    baseline: _VerifiedRelease | None = None
    seen: set[str] = set()
    maximum_graph_version: int | None = None
    for _depth in range(MAX_ANCESTRY_COMMITS):
        release = _verify_release_manifest(reader, commit, trust, now)
        if (
            maximum_graph_version is not None
            and release.manifest.graph_version > maximum_graph_version
        ):
            raise _Stale("shared-state graph version rollback")
        maximum_graph_version = release.manifest.graph_version
        if tip is None:
            tip = release
        if release.manifest.bundle_digest in seen:
            raise _Stale("replayed shared-state release")
        seen.add(release.manifest.bundle_digest)
        if expected_digest is not None and release.manifest.bundle_digest != expected_digest:
            raise _Stale("divergent shared-state lineage")
        marker_matches = (
            marker is not None
            and marker["bundle_digest"] == release.manifest.bundle_digest
            and marker["ref_commit"] == commit
            and marker["graph_version"] == release.manifest.graph_version
        )
        if marker_matches:
            baseline = release
        parents = reader.parents(commit)
        if not parents:
            if release.manifest.parent_bundle_digest is not None:
                raise _Stale("invalid shared-state genesis")
            if marker is not None and baseline is None:
                raise _Stale("local shared-state lineage diverged")
            return _VerifiedLineage(tip, baseline)
        if release.manifest.parent_bundle_digest is None:
            raise _Stale("invalid shared-state parent")
        expected_digest = release.manifest.parent_bundle_digest
        commit = parents[0]
    raise _Stale("shared-state history exceeds traversal bound")


def _marker_bytes(manifest: SharedStateManifest, commit: str) -> bytes:
    return _canonical_json(
        {
            "schema_version": 1,
            "bundle_digest": manifest.bundle_digest,
            "graph_version": manifest.graph_version,
            "ref_commit": commit,
        }
    )


def _read_local_marker(workspace: SecureDirectory | None) -> dict[str, object] | None:
    if workspace is None:
        return None
    marker = workspace.file("cache/shared-state.json")
    try:
        raw = marker.read_optional_nonblocking(max_bytes=4096)
        if raw is None:
            return None
        loaded = loads_strict_object(raw.decode("utf-8"))
        if raw != _canonical_json(loaded):
            raise ValueError("invalid local shared-state marker")
        if (
            set(loaded) != {"schema_version", "bundle_digest", "graph_version", "ref_commit"}
            or loaded["schema_version"] != 1
            or type(loaded["bundle_digest"]) is not str
            or _SHA256.fullmatch(loaded["bundle_digest"]) is None
            or type(loaded["graph_version"]) is not int
            or type(loaded["ref_commit"]) is not str
            or _GIT_COMMIT.fullmatch(loaded["ref_commit"]) is None
        ):
            raise ValueError("invalid local shared-state marker")
        return loaded
    finally:
        marker.close()


def _validate_prior_marker(marker: Mapping[str, object] | None) -> dict[str, object] | None:
    if marker is None:
        return None
    value = dict(marker)
    if (
        set(value) != {"schema_version", "bundle_digest", "graph_version", "ref_commit"}
        or value["schema_version"] != 1
        or type(value["bundle_digest"]) is not str
        or _SHA256.fullmatch(value["bundle_digest"]) is None
        or type(value["graph_version"]) is not int
        or type(value["ref_commit"]) is not str
        or _GIT_COMMIT.fullmatch(value["ref_commit"]) is None
    ):
        raise ValueError("invalid prior shared-state marker")
    return value


def _decrypt_release_payload(
    reader: _GitRefReader, release: _VerifiedRelease, trust: SharedStateTrust
) -> dict[str, bytes]:
    manifest = release.manifest
    bundle = reader.blob(
        release.commit, f"bundles/{_release_name(manifest)}.intent", MAX_BUNDLE_BYTES
    )
    if len(bundle) != manifest.bundle_size or _digest(bundle) != manifest.bundle_digest:
        raise ValueError("shared-state bundle mismatch")
    bundle_model = _parse_canonical_model(bundle, EncryptedStateBundle, MAX_BUNDLE_BYTES)
    assert isinstance(bundle_model, EncryptedStateBundle)
    wrapped = {item.recipient_key_id: item for item in bundle_model.wrapped_keys}
    if (
        tuple(sorted(wrapped)) != manifest.recipient_key_ids
        or trust.recipient_key_id not in wrapped
    ):
        raise ValueError("shared-state recipient mismatch")
    aad = _manifest_aad(
        project_id=manifest.project_id,
        repository_id=manifest.repository_id,
        graph_version=manifest.graph_version,
        parent_bundle_digest=manifest.parent_bundle_digest,
        created_at=manifest.created_at,
        recipient_key_ids=manifest.recipient_key_ids,
        required_signature_ids=manifest.required_signature_ids,
    )
    private = X25519PrivateKey.from_private_bytes(trust.recipient_private_key)
    shared = private.exchange(
        X25519PublicKey.from_public_bytes(
            _b64decode(bundle_model.ephemeral_public_key, expected_size=32)
        )
    )
    selected = wrapped[trust.recipient_key_id]
    wrapping_key = _derive_wrapping_key(shared, aad, trust.recipient_key_id)
    content_key = AESGCM(wrapping_key).decrypt(
        _b64decode(selected.nonce, expected_size=12),
        _b64decode(selected.ciphertext),
        aad + b"\0" + trust.recipient_key_id.encode(),
    )
    plaintext = AESGCM(content_key).decrypt(
        _b64decode(bundle_model.nonce, expected_size=12),
        _b64decode(bundle_model.ciphertext),
        aad,
    )
    return _parse_payload(plaintext)


def _same_semantic_baseline(current: Mapping[str, bytes], baseline: Mapping[str, bytes]) -> bool:
    """Compare interpreted graph and complete decision ledgers, not cache-marker claims."""
    for path in CANONICAL_STATE_PATHS:
        if path == "evidence/evidence.jsonl" or current[path] == baseline[path]:
            continue
        if path == "graph.yaml":
            if parse_graph(current[path]) != parse_graph(baseline[path]):
                return False
        elif path.endswith(".yaml"):
            if yaml.safe_load(current[path]) != yaml.safe_load(baseline[path]):
                return False
        else:
            local = tuple(
                loads_strict_object(line)
                for line in current[path].decode().splitlines()
                if line.strip()
            )
            approved = tuple(
                loads_strict_object(line)
                for line in baseline[path].decode().splitlines()
                if line.strip()
            )
            if path == "history/changesets.jsonl":
                if tuple(ChangeSet.model_validate(item) for item in local) != tuple(
                    ChangeSet.model_validate(item) for item in approved
                ):
                    return False
                continue
            if local != approved:
                return False
    return True


def _assert_named_directory(parent: SecureDirectory, name: str, expected: SecureDirectory) -> None:
    observed = parent.subdirectory(name)
    try:
        if observed.identity != expected.identity:
            raise UnsafePathError()
    finally:
        observed.close()


def _assert_project_binding(project: SecureDirectory) -> None:
    observed = SecureDirectory.open(project.path)
    try:
        if observed.identity != project.identity:
            raise UnsafePathError()
    finally:
        observed.close()


def _change_token(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


class _TreeFence:
    """Hold every scanned inode and reject any change across the complete byte scan."""

    def __init__(
        self,
        workspace: SecureDirectory,
        paths: Mapping[str, bytes | None],
        parent: SecureDirectory | None,
    ) -> None:
        self.directories: dict[str, SecureDirectory] = {"": workspace.duplicate()}
        self.file_descriptors: list[int] = []
        self.tokens: dict[int, tuple[int, ...]] = {}
        self.names: list[tuple[int, str, tuple[int, ...] | None]] = []
        self.parent = parent.duplicate() if parent is not None else None
        try:
            self._pin_directory(self.directories[""])
            if self.parent is not None:
                self._pin_directory(self.parent)
                self.names.append(
                    (
                        self.parent.descriptor,
                        ".intent",
                        self.tokens[self.directories[""].descriptor],
                    )
                )
            for path in sorted(paths):
                parts = Path(path).parts
                directory_parts = parts if path.endswith("/") else parts[:-1]
                prefix = ""
                for part in directory_parts:
                    next_prefix = f"{prefix}/{part}" if prefix else part
                    if next_prefix not in self.directories:
                        ancestor = self.directories[prefix]
                        opened = ancestor.subdirectory(part)
                        self.directories[next_prefix] = opened
                        self._pin_directory(opened)
                        self.names.append(
                            (ancestor.descriptor, part, self.tokens[opened.descriptor])
                        )
                    prefix = next_prefix
                if path.endswith("/"):
                    continue
                ancestor = self.directories[prefix]
                if paths[path] is None:
                    self.names.append((ancestor.descriptor, parts[-1], None))
                    continue
                descriptor = os.open(
                    parts[-1],
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                    dir_fd=ancestor.descriptor,
                )
                self.file_descriptors.append(descriptor)
                metadata = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_nlink != 1
                    or metadata.st_size > MAX_FILE_BYTES
                ):
                    raise UnsafePathError()
                token = _change_token(metadata)
                self.tokens[descriptor] = token
                self.names.append((ancestor.descriptor, parts[-1], token))
            self.verify()
        except BaseException:
            self.close()
            raise

    def _pin_directory(self, directory: SecureDirectory) -> None:
        self.tokens[directory.descriptor] = _change_token(os.fstat(directory.descriptor))

    def verify(self) -> None:
        # No content reads, callbacks or path reopening follow this terminal token
        # barrier. ctime prevents a writer from hiding a change by restoring mtime.
        if any(
            self._named_token(parent, name) != expected for parent, name, expected in self.names
        ) or any(
            _change_token(os.fstat(descriptor)) != expected
            for descriptor, expected in self.tokens.items()
        ):
            raise UnsafePathError()

    @staticmethod
    def _named_token(parent: int, name: str) -> tuple[int, ...] | None:
        try:
            return _change_token(os.stat(name, dir_fd=parent, follow_symlinks=False))
        except FileNotFoundError:
            return None

    def close(self) -> None:
        for descriptor in self.file_descriptors:
            os.close(descriptor)
        self.file_descriptors.clear()
        for directory in self.directories.values():
            directory.close()
        if self.parent is not None:
            self.parent.close()


class _PinnedState:
    """Bind immutable authenticated bytes to the directory and file identities used."""

    def __init__(
        self,
        workspace: SecureDirectory,
        files: Mapping[str, bytes],
        identities: Mapping[str, tuple[tuple[int, int], ...]] | None = None,
    ) -> None:
        self.workspace = workspace
        self.files = dict(files)
        self.identities: dict[str, tuple[tuple[int, int], ...]] = {}
        self.inventory_sealed = False
        if identities is not None:
            if set(identities) != set(files):
                raise UnsafePathError()
            self.identities = dict(identities)
            return
        for path, content in self.files.items():
            observed = workspace.read_relative(path, nonblocking=True, max_bytes=MAX_FILE_BYTES)
            if observed.content != content:
                raise UnsafePathError()
            self.identities[path] = observed.identities

    def _inventory(self) -> tuple[tuple[str, bytes, tuple[tuple[int, int], ...]], ...]:
        entries: list[tuple[str, bytes, tuple[tuple[int, int], ...]]] = []
        total_bytes = 0

        def walk(directory: SecureDirectory, prefix: str, depth: int) -> None:
            nonlocal total_bytes
            if depth > 4:
                raise UnsafePathError()
            with os.scandir(directory.descriptor) as children:
                for child in children:
                    if len(entries) >= 64:
                        raise UnsafePathError()
                    path = prefix + child.name
                    metadata = child.stat(follow_symlinks=False)
                    if stat.S_ISDIR(metadata.st_mode):
                        opened = directory.subdirectory(child.name)
                        try:
                            if opened.identity != (metadata.st_dev, metadata.st_ino):
                                raise UnsafePathError()
                            entries.append((path + "/", b"", (opened.identity,)))
                            walk(opened, path + "/", depth + 1)
                        finally:
                            opened.close()
                    else:
                        observed = self.workspace.read_relative(
                            path, nonblocking=True, max_bytes=MAX_FILE_BYTES
                        )
                        total_bytes += len(observed.content)
                        if total_bytes > MAX_STATE_BYTES + 4096:
                            raise UnsafePathError()
                        entries.append((path, observed.content, observed.identities))

        walk(self.workspace, "", 0)
        return tuple(entries)

    def seal_inventory(self) -> None:
        """Only empty generated runtime files may accompany the authenticated payload."""
        auxiliary = {
            "approvals/webauthn-credentials.jsonl",
            "approvals/webauthn-challenges.jsonl",
            "cache/checkpoints.yaml",
            "history/.local-transaction.json",
        }
        directories = {
            "approvals/",
            "cache/",
            "connectors/",
            "evidence/",
            "history/",
            "reconciliation/",
            "renders/",
        }
        lock_paths = {
            str(Path(path).with_name(f".{Path(path).name}.lock"))
            for path in {*self.files, *auxiliary}
        }
        for path, content, identities in self._inventory():
            if path not in self.files:
                if path not in auxiliary | lock_paths | directories or content:
                    raise UnsafePathError()
                self.files[path] = b""
                self.identities[path] = identities
        self.inventory_sealed = True
        self.verify()

    def verify(self, parent: SecureDirectory | None = None) -> None:
        fence = _TreeFence(self.workspace, self.files, parent)
        try:
            self._verify_content()
            fence.verify()
        finally:
            fence.close()

    def _verify_content(self) -> None:
        if self.inventory_sealed:
            inventory = self._inventory()
            if {path for path, _, _ in inventory} != set(self.files):
                raise UnsafePathError()
            for path, content, identities in inventory:
                if content != self.files[path] or identities != self.identities[path]:
                    raise UnsafePathError()
            return
        for path, content in self.files.items():
            observed = self.workspace.read_relative(
                path,
                expected_identities=self.identities[path],
                nonblocking=True,
                max_bytes=MAX_FILE_BYTES,
            )
            if observed.content != content:
                raise UnsafePathError()


def _rename_entry_exclusive(
    source: SecureDirectory, source_name: str, destination: SecureDirectory, destination_name: str
) -> None:
    """Move only into an absent name, using the already held parent descriptors."""
    library = ctypes.CDLL(None, use_errno=True)
    try:
        function = library.renameat2
        flag = 1  # RENAME_NOREPLACE
    except AttributeError:
        try:
            function = library.renameatx_np
            flag = 4  # RENAME_EXCL
        except AttributeError as error:
            raise UnsafePathError() from error
    function.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    function.restype = ctypes.c_int
    if (
        function(
            source.descriptor,
            os.fsencode(source_name),
            destination.descriptor,
            os.fsencode(destination_name),
            flag,
        )
        != 0
    ):
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number))


def _rename_directory_exclusive(source: SecureDirectory, destination: SecureDirectory) -> None:
    _rename_entry_exclusive(source, ".intent", destination, ".intent")


def _private_directory(project: SecureDirectory) -> SecureDirectory:
    """Reserve a collision-safe recovery container through the held repository root."""
    for _attempt in range(32):
        name = f".intent-quarantine-{secrets.token_hex(16)}"
        try:
            os.mkdir(name, 0o700, dir_fd=project.descriptor)
        except FileExistsError:
            continue
        return project.subdirectory(name)
    raise UnsafePathError()


def _remove_empty_directory(project: SecureDirectory, directory: SecureDirectory) -> None:
    try:
        _assert_named_directory(project, directory.path.name, directory)
        os.rmdir(directory.path.name, dir_fd=project.descriptor)
    except (OSError, UnsafePathError):
        return


def _discard_stage(
    project: SecureDirectory, stage: SecureDirectory, state: _PinnedState | None
) -> None:
    """Detach staging; scrub only held authenticated inodes, never mutable names."""
    with os.scandir(stage.descriptor) as entries:
        names = [entry.name for _, entry in zip(range(2), entries, strict=False)]
    if names:
        quarantine = _private_directory(project)
        descriptors: dict[int, tuple[int, ...]] = {}
        try:
            if names != [".intent"] or state is None:
                _assert_named_directory(project, stage.path.name, stage)
                _rename_entry_exclusive(project, stage.path.name, quarantine, "staging")
                return
            # Move the actual named entry before authenticating it. An unfamiliar
            # replacement remains private and recoverable, even after a later swap.
            _rename_directory_exclusive(stage, quarantine)
            _assert_named_directory(quarantine, ".intent", state.workspace)
            for path in state.files:
                if path.endswith("/"):
                    continue
                target = state.workspace.file(path)
                try:
                    descriptor = os.open(
                        target.name,
                        os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                        dir_fd=target.parent_fd,
                    )
                    descriptors[descriptor] = ()
                    metadata = os.fstat(descriptor)
                    descriptors[descriptor] = _change_token(metadata)
                    if (
                        not stat.S_ISREG(metadata.st_mode)
                        or metadata.st_nlink != 1
                        or (metadata.st_dev, metadata.st_ino) != state.identities[path][-1]
                    ):
                        raise UnsafePathError()
                finally:
                    target.close()
            state.verify(quarantine)
            if any(
                _change_token(os.fstat(descriptor)) != token
                for descriptor, token in descriptors.items()
            ):
                raise UnsafePathError()
            for descriptor, token in descriptors.items():
                if _change_token(os.fstat(descriptor)) != token:
                    raise UnsafePathError()
                os.ftruncate(descriptor, 0)
                os.fsync(descriptor)
                if os.fstat(descriptor).st_size != 0:
                    raise UnsafePathError()
            # Keep private zero-byte tombstones. Unlink/rmtree would re-trust names
            # that an uncooperative writer can replace after the authentication.
        except (OSError, UnsafePathError):
            pass
        finally:
            for descriptor in descriptors:
                os.close(descriptor)
            _remove_empty_directory(project, quarantine)
            quarantine.close()
    _remove_empty_directory(project, stage)


def _write_staged_state(
    stage: SecureDirectory, files: Mapping[str, bytes], marker: bytes, *, fresh: bool = True
) -> None:
    workspace = stage.subdirectory(".intent", create=True)
    try:
        for name in ("approvals", "cache", "connectors", "evidence", "history", "reconciliation"):
            directory = workspace.subdirectory(name, create=True)
            directory.close()
        payload = {
            **files,
            "approvals/webauthn-credentials.jsonl": b"",
            "approvals/webauthn-challenges.jsonl": b"",
            "cache/shared-state.json": marker,
        }
        for path in sorted(payload):
            target = workspace.file(path)
            try:
                target.atomic_write(payload[path], reject_target_races=fresh)
            finally:
                target.close()
    finally:
        workspace.close()


class _RestorePreimage:
    """Bounded preimage material, captured under the restore transaction's complete locks."""

    def __init__(
        self,
        project: SecureDirectory,
        workspace: SecureDirectory,
        targets: Mapping[str, SecureFile],
        path_to_name: Mapping[str, str],
    ) -> None:
        self.project = project
        self.workspace = workspace
        self.targets = {path: targets[name] for path, name in path_to_name.items()}
        self.directory_metadata = {"": os.fstat(workspace.descriptor)}
        self.directory_descriptors = {"": workspace.descriptor}
        self.metadata: dict[str, os.stat_result | None] = {}
        for path, name in path_to_name.items():
            target = targets[name]
            prefix = str(Path(path).parent)
            prefix = "" if prefix == "." else prefix
            self.directory_descriptors.setdefault(prefix, target.parent_fd)
            observed = os.fstat(target.parent_fd)
            previous = self.directory_metadata.setdefault(prefix, observed)
            if _change_token(previous) != _change_token(observed):
                raise UnsafePathError()
            try:
                self.metadata[path] = os.stat(
                    target.name, dir_fd=target.parent_fd, follow_symlinks=False
                )
            except FileNotFoundError:
                self.metadata[path] = None
        fence = _TreeFence(
            workspace,
            {path: None if metadata is None else b"" for path, metadata in self.metadata.items()},
            project,
        )
        try:
            self.content = {
                path: targets[name].read_optional_nonblocking(max_bytes=MAX_FILE_BYTES)
                for path, name in path_to_name.items()
            }
            if (
                sum(len(value) for value in self.content.values() if value is not None)
                > MAX_STATE_BYTES
            ):
                raise UnsafePathError()
            for path, name in path_to_name.items():
                expected = self.metadata[path]
                target = targets[name]
                if _TreeFence._named_token(target.parent_fd, target.name) != (
                    None if expected is None else _change_token(expected)
                ):
                    raise UnsafePathError()
            for prefix, metadata in self.directory_metadata.items():
                if _change_token(os.fstat(fence.directories[prefix].descriptor)) != _change_token(
                    metadata
                ):
                    raise UnsafePathError()
            fence.verify()
        finally:
            fence.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        error_type: type[BaseException] | None,
        _error: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        if error_type is not None:
            self._restore_directory_bindings()
            restored = self.project.subdirectory(".intent")
            try:
                self._verify_reconstruction(restored, "")
            finally:
                restored.close()

    @staticmethod
    def _matches(parent: SecureDirectory, name: str, expected: os.stat_result) -> bool:
        try:
            observed = os.stat(name, dir_fd=parent.descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return (observed.st_dev, observed.st_ino) == (expected.st_dev, expected.st_ino)

    @staticmethod
    def _restore_metadata(descriptor: int, metadata: os.stat_result) -> None:
        os.fchmod(descriptor, stat.S_IMODE(metadata.st_mode))
        os.utime(descriptor, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))

    def _verify_reconstruction(self, candidate: SecureDirectory, prefix: str) -> None:
        members = {
            path.removeprefix(prefix + "/") if prefix else path: content
            for path, content in self.content.items()
            if not prefix or path.startswith(prefix + "/")
        }
        fence = _TreeFence(candidate, members, None)
        try:
            for path, expected in members.items():
                target = candidate.file(path)
                try:
                    if target.read_optional_nonblocking(max_bytes=MAX_FILE_BYTES) != expected:
                        raise UnsafePathError()
                    metadata = self.metadata[f"{prefix}/{path}" if prefix else path]
                    if metadata is not None:
                        current = os.stat(
                            target.name, dir_fd=target.parent_fd, follow_symlinks=False
                        )
                        if (stat.S_IMODE(current.st_mode), current.st_mtime_ns) != (
                            stat.S_IMODE(metadata.st_mode),
                            metadata.st_mtime_ns,
                        ):
                            raise UnsafePathError()
                finally:
                    target.close()
            for relative, metadata in self.directory_metadata.items():
                if prefix and relative != prefix:
                    continue
                local = "" if prefix else relative
                directory = fence.directories[local]
                current = os.fstat(directory.descriptor)
                if (stat.S_IMODE(current.st_mode), current.st_mtime_ns) != (
                    stat.S_IMODE(metadata.st_mode),
                    metadata.st_mtime_ns,
                ):
                    raise UnsafePathError()
            fence.verify()
        finally:
            fence.close()

    def _reconstruct(self, live_parent: SecureDirectory, name: str, prefix: str) -> None:
        recovery = _private_directory(self.project)
        candidate = recovery.subdirectory("replacement", create=True)
        try:
            if not prefix:
                for relative in self.directory_metadata:
                    if relative:
                        directory = candidate.subdirectory(relative, create=True)
                        directory.close()
            for path, content in self.content.items():
                if content is None or (prefix and not path.startswith(prefix + "/")):
                    continue
                relative = path.removeprefix(prefix + "/") if prefix else path
                target = candidate.file(relative)
                try:
                    target.atomic_write(content, reject_target_races=True)
                    metadata = self.metadata[path]
                    assert metadata is not None
                    descriptor = os.open(
                        target.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=target.parent_fd
                    )
                    try:
                        self._restore_metadata(descriptor, metadata)
                    finally:
                        os.close(descriptor)
                finally:
                    target.close()
            if not prefix:
                for relative, metadata in self.directory_metadata.items():
                    if relative:
                        directory = candidate.subdirectory(relative)
                        try:
                            self._restore_metadata(directory.descriptor, metadata)
                        finally:
                            directory.close()
            self._restore_metadata(candidate.descriptor, self.directory_metadata[prefix])
            self._verify_reconstruction(candidate, prefix)
            try:
                _rename_entry_exclusive(live_parent, name, recovery, "unfamiliar")
            except FileNotFoundError:
                pass
            _rename_entry_exclusive(recovery, "replacement", live_parent, name)
            _assert_named_directory(live_parent, name, candidate)
            self._verify_reconstruction(candidate, prefix)
            os.fsync(live_parent.descriptor)
        finally:
            candidate.close()
            _remove_empty_directory(self.project, recovery)
            recovery.close()

    def _restore_directory_bindings(self) -> None:
        _assert_project_binding(self.project)
        if not self._matches(self.project, ".intent", self.directory_metadata[""]):
            self._reconstruct(self.project, ".intent", "")
            return
        replaced_directories = {
            prefix
            for prefix, metadata in self.directory_metadata.items()
            if prefix and not self._matches(self.workspace, prefix, metadata)
        }
        for prefix in sorted(replaced_directories):
            self._reconstruct(self.workspace, prefix, prefix)
        for path in self.content:
            prefix = str(Path(path).parent)
            prefix = "" if prefix == "." else prefix
            if prefix in replaced_directories:
                continue
            self._repair_target(path)
        # Routine transaction rollback replaces file inodes and changes mtimes.
        # That is not a substituted directory: retain the live lock domain and all
        # unrelated connector/runtime files, and repair only our pinned targets.
        for prefix, metadata in self.directory_metadata.items():
            if prefix not in replaced_directories:
                descriptor = self.directory_descriptors[prefix]
                current = os.fstat(descriptor)
                if (stat.S_IMODE(current.st_mode), current.st_mtime_ns) != (
                    stat.S_IMODE(metadata.st_mode),
                    metadata.st_mtime_ns,
                ):
                    self._restore_metadata(descriptor, metadata)
                    os.fsync(descriptor)

    def _repair_target(self, path: str) -> None:
        target = self.targets[path]
        parent = SecureDirectory(os.dup(target.parent_fd), target.path.parent)
        fence: _TreeFence | None = None
        matches = False
        try:
            try:
                present = _TreeFence._named_token(target.parent_fd, target.name) is not None
                fence = _TreeFence(parent, {target.name: b"" if present else None}, None)
                observed = target.read_optional_nonblocking(max_bytes=MAX_FILE_BYTES)
                fence.verify()
                matches = observed == self.content[path]
            except (OSError, UnsafePathError):
                matches = False
            if matches:
                metadata = self.metadata[path]
                if metadata is not None:
                    assert fence is not None
                    descriptor = fence.file_descriptors[0]
                    current = os.fstat(descriptor)
                    if (stat.S_IMODE(current.st_mode), current.st_mtime_ns) != (
                        stat.S_IMODE(metadata.st_mode),
                        metadata.st_mtime_ns,
                    ):
                        self._restore_metadata(descriptor, metadata)
                        os.fsync(descriptor)
                return
            self._reconstruct_target(parent, path)
        finally:
            if fence is not None:
                fence.close()
            parent.close()

    def _reconstruct_target(self, parent: SecureDirectory, path: str) -> None:
        """Repair one canonical name without deleting unfamiliar displaced bytes."""
        recovery = _private_directory(self.project)
        candidate = recovery.file("replacement")
        try:
            content = self.content[path]
            if content is not None:
                candidate.atomic_write(content, reject_target_races=True)
                descriptor = os.open(
                    candidate.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=candidate.parent_fd
                )
                try:
                    metadata = self.metadata[path]
                    assert metadata is not None
                    self._restore_metadata(descriptor, metadata)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            try:
                _rename_entry_exclusive(parent, Path(path).name, recovery, "unfamiliar")
            except FileNotFoundError:
                pass
            if content is not None:
                _rename_entry_exclusive(recovery, "replacement", parent, Path(path).name)
            os.fsync(parent.descriptor)
        finally:
            candidate.close()
            _remove_empty_directory(self.project, recovery)
            recovery.close()


def _validation_snapshot(files: Mapping[str, bytes]) -> dict[str, bytes | None]:
    return {
        "config": files["config.yaml"],
        "graph": files["graph.yaml"],
        "history": files["history/changesets.jsonl"],
        "cases": files["reconciliation/cases.jsonl"],
        "evidence": files["evidence/evidence.jsonl"],
        "receipts": files["approvals/receipts.jsonl"],
        "checkpoints": files.get("cache/checkpoints.yaml"),
    }


def _validate_authenticated_state(
    files: Mapping[str, bytes],
    manifest: SharedStateManifest,
    trust: SharedStateTrust,
) -> None:
    report = validate_canonical_snapshot(_validation_snapshot(files))
    if not report.valid or report.graph_version != manifest.graph_version:
        raise ValueError("restored shared state is invalid")
    raw_config = files["config.yaml"]
    loaded = yaml.safe_load(raw_config.decode("utf-8"))
    config = ProjectConfig.model_validate(loaded)
    if (
        config.project_id != trust.project_id
        or config.project_id != manifest.project_id
        or str(configured_graph_relative(config.graph_path)) != "graph.yaml"
    ):
        raise ValueError("restored project identity mismatch")
    parse_immutable_records(files["approvals/plans.jsonl"], WritePlan)
    parse_immutable_records(files["approvals/approvals.jsonl"], ApprovalRecord)
    policy = files["approvals/policy.yaml"]
    if policy:
        MutationPolicy.model_validate(load_strict_yaml_mapping_bytes(policy))


def _replace_existing_state(
    project: SecureDirectory,
    workspace: SecureDirectory,
    files: Mapping[str, bytes],
    baseline_files: Mapping[str, bytes],
    marker: bytes,
    fault_hook: Callable[[str], None],
) -> None:
    path_to_name = {
        "approvals/approvals.jsonl": "approvals",
        "approvals/plans.jsonl": "plans",
        "approvals/policy.yaml": "policy",
        "approvals/receipts.jsonl": "receipts",
        "config.yaml": "config",
        "evidence/evidence.jsonl": "evidence",
        "graph.yaml": "graph",
        "history/changesets.jsonl": "history",
        "history/intent-proposals.jsonl": "intent_proposals",
        "reconciliation/cases.jsonl": "cases",
        "approvals/webauthn-challenges.jsonl": "webauthn_challenges",
        "approvals/webauthn-credentials.jsonl": "webauthn_credentials",
        "cache/checkpoints.yaml": "checkpoints",
        "cache/shared-state.json": "shared_state",
    }
    targets = {name: workspace.file(path) for path, name in path_to_name.items()}
    terminal_state: _PinnedState | None = None

    def verify_committed(stage: str) -> None:
        if stage == "journal_cleaned" and terminal_state is not None:
            _assert_project_binding(project)
            terminal_state.verify(project)

    coordinator = LocalTransactionCoordinator(
        workspace.file("history/.local-transaction.json"),
        targets,
        fault_hook=verify_committed,
        legacy_target_sets=(
            frozenset({"graph", "history", "cases"}),
            frozenset({"graph", "history", "cases", "evidence", "receipts"}),
            frozenset({"graph", "history", "cases", "evidence", "receipts", "intent_proposals"}),
            frozenset(
                {
                    "graph",
                    "history",
                    "cases",
                    "evidence",
                    "receipts",
                    "intent_proposals",
                    "webauthn_credentials",
                    "webauthn_challenges",
                }
            ),
        ),
    )
    try:
        for target in targets.values():
            target.read_optional_nonblocking(max_bytes=MAX_STATE_BYTES)
        with (
            coordinator.coordinated(),
            _RestorePreimage(project, workspace, targets, path_to_name) as preimage,
        ):
            _assert_named_directory(project, ".intent", workspace)
            current = {path: content or b"" for path, content in preimage.content.items()}
            if not _same_semantic_baseline(current, baseline_files) and not _same_semantic_baseline(
                current, files
            ):
                raise _Diverged("unpublished local state requires reconciliation")
            evidence_path = "evidence/evidence.jsonl"
            if current[evidence_path] != baseline_files[evidence_path]:
                if current[evidence_path] != files[evidence_path] and (
                    files[evidence_path] != baseline_files[evidence_path]
                    or not current[evidence_path].startswith(baseline_files[evidence_path])
                ):
                    raise _Diverged("local evidence requires reconciliation")
                local_state = _PinnedState(
                    workspace, {path: current[path] for path in CANONICAL_STATE_PATHS}
                )
                if not validate_canonical_snapshot(_validation_snapshot(current)).valid:
                    raise ValueError("invalid local evidence")
                local_state.verify(project)
                files = {**files, evidence_path: current[evidence_path]}
            if (
                all(current[path] == files[path] for path in files)
                and current["cache/shared-state.json"] == marker
            ):
                pinned = _PinnedState(workspace, {**files, "cache/shared-state.json": marker})
                report = validate_canonical_snapshot(_validation_snapshot(current))
                if not report.valid:
                    raise ValueError("invalid local shared state")
                pinned.verify(project)
                _assert_named_directory(project, ".intent", workspace)
                _assert_project_binding(project)
                return
            with coordinator.transaction(rollback_base_exceptions=True) as transaction:
                installed: dict[str, bytes] = {}
                identities: dict[str, tuple[tuple[int, int], ...]] = {}
                for path in CANONICAL_STATE_PATHS:
                    transaction.write(path_to_name[path], files[path])
                    installed[path] = files[path]
                    identities[path] = workspace.read_relative(
                        path, nonblocking=True, max_bytes=MAX_FILE_BYTES
                    ).identities
                    fault_hook(f"target:{path}")
                for path, content in (
                    ("cache/checkpoints.yaml", b""),
                    ("cache/shared-state.json", marker),
                ):
                    transaction.write(path_to_name[path], content)
                    installed[path] = content
                    identities[path] = workspace.read_relative(
                        path, nonblocking=True, max_bytes=MAX_FILE_BYTES
                    ).identities
                fault_hook("existing_precommit")
                _assert_project_binding(project)
                _assert_named_directory(project, ".intent", workspace)
                terminal_state = _PinnedState(workspace, installed, identities)
                terminal_state.verify(project)
    finally:
        coordinator.close()
        for target in targets.values():
            target.close()


def _install_fresh_state(
    root_directory: SecureDirectory,
    stage_directory: SecureDirectory,
    state: _PinnedState,
    fault_hook: Callable[[str], None],
) -> None:
    installed = False
    recovery = _private_directory(root_directory)
    try:
        fault_hook("fresh_preinstall")
        _assert_project_binding(root_directory)
        _assert_named_directory(root_directory, stage_directory.path.name, stage_directory)
        _assert_named_directory(stage_directory, ".intent", state.workspace)
        state.verify(stage_directory)
        # Previously writable staged inodes never become the installed authority.
        # Materialize the authenticated immutable bytes into new regular files.
        _write_staged_state(
            stage_directory,
            {path: state.files[path] for path in CANONICAL_STATE_PATHS},
            state.files["cache/shared-state.json"],
            fresh=False,
        )
        refreshed = _PinnedState(
            state.workspace,
            {path: content for path, content in state.files.items() if not path.endswith("/")},
        )
        refreshed.seal_inventory()
        state.files = refreshed.files
        state.identities = refreshed.identities
        state.inventory_sealed = True
        state.verify(stage_directory)
        _rename_directory_exclusive(stage_directory, root_directory)
        installed = True
        fault_hook("fresh_installed")
        _assert_project_binding(root_directory)
        _assert_named_directory(root_directory, ".intent", state.workspace)
        os.fsync(root_directory.descriptor)
        state.verify(root_directory)
    except BaseException:
        if installed:
            # Move the actual named entry, not the inode we hoped was promoted.
            # An occupied recovery name cannot strand it in the live workspace.
            for _attempt in range(32):
                try:
                    _rename_directory_exclusive(root_directory, recovery)
                    break
                except FileExistsError:
                    recovery.close()
                    recovery = _private_directory(root_directory)
            else:
                raise UnsafePathError()
            os.fsync(root_directory.descriptor)
            _discard_stage(root_directory, recovery, state)
        raise
    finally:
        _remove_empty_directory(root_directory, recovery)
        recovery.close()


def read_approved_baseline(
    root: Path, trust_provider: TrustProvider, *, at: datetime
) -> dict[str, bytes]:
    """Return only authenticated immutable Git release bytes, without restoring paths.

    This read-only export is the build-material boundary for immutable CI images;
    local graph/config bytes never become Docker build inputs.
    """
    trust = trust_provider.load()
    if type(trust) is not SharedStateTrust or _origin_repository(root) != trust.repository_id:
        raise ValueError("approved baseline unavailable")
    if at.tzinfo is None or at.utcoffset() != timedelta(0):
        raise ValueError("approved baseline unavailable")
    reader = _GitRefReader(root)
    commit = reader.commit()
    lineage = _verify_release_lineage(reader, commit, trust, at, None)
    files = _decrypt_release_payload(reader, lineage.tip, trust)
    _validate_authenticated_state(files, lineage.tip.manifest, trust)
    return {**files, "cache/shared-state.json": _marker_bytes(lineage.tip.manifest, commit)}


class GitSharedStateRestorer:
    """Verify a protected Git ref and atomically install its approved state."""

    def __init__(
        self,
        trust_provider: TrustProvider,
        *,
        clock: Callable[[], datetime] | None = None,
        fault_hook: Callable[[str], None] | None = None,
        refresh_remote: bool = False,
        prior_marker: Mapping[str, object] | None = None,
    ) -> None:
        if type(refresh_remote) is not bool:
            raise ValueError("invalid shared-state refresh mode")
        self._trust_provider = trust_provider
        self._clock = clock or (lambda: datetime.now(UTC))
        self._fault_hook = fault_hook or (lambda _stage: None)
        self._refresh_remote = refresh_remote
        self._prior_marker = _validate_prior_marker(prior_marker)

    def verify_and_restore_approved_baseline(self, root: Path) -> SharedStateRestoreResult:
        project: SecureDirectory | None = None
        workspace: SecureDirectory | None = None
        stage: SecureDirectory | None = None
        staged_workspace: SecureDirectory | None = None
        state: _PinnedState | None = None
        trust: SharedStateTrust | None = None
        reader: _GitRefReader | None = None
        files: dict[str, bytes] = {}
        baseline_files: dict[str, bytes] = {}
        try:
            trust = self._trust_provider.load()
            if trust is None:
                raise _Unavailable("shared-state trust unavailable")
            if type(trust) is not SharedStateTrust:
                raise ValueError("invalid shared-state trust")
            root = Path(os.path.abspath(root))
            if _origin_repository(root) != trust.repository_id:
                raise ValueError("repository identity mismatch")
            project = SecureDirectory.open(root)
            try:
                os.stat(".intent", dir_fd=project.descriptor, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                workspace = project.subdirectory(".intent")
            if self._refresh_remote:
                reader = _refresh_state_ref(trust.repository_id)
            else:
                reader = _GitRefReader(root)
            commit = reader.commit()
            now = self._clock()
            if now.tzinfo is None or now.utcoffset() != timedelta(0):
                raise ValueError("invalid shared-state time")
            marker = _read_local_marker(workspace)
            if marker is None:
                marker = self._prior_marker
            elif self._prior_marker is not None and marker != self._prior_marker:
                raise ValueError("local shared-state marker mismatch")
            lineage = _verify_release_lineage(reader, commit, trust, now, marker)
            manifest = lineage.tip.manifest
            files = _decrypt_release_payload(reader, lineage.tip, trust)
            _validate_authenticated_state(files, manifest, trust)
            baseline_files = (
                _decrypt_release_payload(reader, lineage.baseline, trust)
                if lineage.baseline is not None and lineage.baseline.commit != commit
                else files
            )
            marker_bytes = _marker_bytes(manifest, commit)
            if workspace is not None:
                self._fault_hook("validated")
                _assert_project_binding(project)
                _replace_existing_state(
                    project, workspace, files, baseline_files, marker_bytes, self._fault_hook
                )
                return SharedStateRestoreResult(status=SharedStateRestoreStatus.VERIFIED)
            stage_path = Path(tempfile.mkdtemp(prefix=".intent-restore-", dir=root))
            stage = project.subdirectory(stage_path.name)
            _write_staged_state(stage, files, marker_bytes)
            staged_workspace = stage.subdirectory(".intent")
            state = _PinnedState(
                staged_workspace, {**files, "cache/shared-state.json": marker_bytes}
            )
            state.seal_inventory()
            self._fault_hook("validated")
            _assert_project_binding(project)
            _assert_named_directory(project, stage.path.name, stage)
            _assert_named_directory(stage, ".intent", staged_workspace)
            state.verify()
            _install_fresh_state(project, stage, state, self._fault_hook)
            return SharedStateRestoreResult(status=SharedStateRestoreStatus.VERIFIED)
        except _Unavailable:
            return SharedStateRestoreResult(status=SharedStateRestoreStatus.UNAVAILABLE)
        except _UpgradeRequired:
            return SharedStateRestoreResult(status=SharedStateRestoreStatus.UPGRADE_REQUIRED)
        except _Stale:
            return SharedStateRestoreResult(status=SharedStateRestoreStatus.STALE)
        except _Diverged:
            return SharedStateRestoreResult(status=SharedStateRestoreStatus.DIVERGED)
        except (
            InvalidSignature,
            InvalidTag,
            OSError,
            subprocess.SubprocessError,
            UnicodeError,
            ValueError,
        ):
            return SharedStateRestoreResult(status=SharedStateRestoreStatus.INVALID)
        except Exception:  # noqa: BLE001 - fixed, secret-free restore failure boundary
            return SharedStateRestoreResult(status=SharedStateRestoreStatus.INVALID)
        except BaseException as cancellation:  # noqa: BLE001 - propagate scrubbed cancellation
            # Cancellation remains observable, but neither decrypted bytes nor the
            # injected private-key provider may survive in its traceback frames.
            trust = None
            files.clear()
            baseline_files.clear()
            del self
            raise cancellation.with_traceback(None) from None
        finally:
            if stage is not None and project is not None:
                try:
                    _discard_stage(project, stage, state)
                except (OSError, UnsafePathError):
                    pass
                finally:
                    stage.close()
            if staged_workspace is not None:
                staged_workspace.close()
            if state is not None:
                state.files.clear()
            if workspace is not None:
                workspace.close()
            if project is not None:
                project.close()
            if reader is not None:
                reader.close()


__all__ = [
    "ALGORITHM",
    "CANONICAL_STATE_PATHS",
    "MAX_BUNDLE_BYTES",
    "TRUST_ENVIRONMENT_VARIABLE",
    "EnvironmentTrustProvider",
    "GitSharedStateRestorer",
    "SharedStateArtifacts",
    "SharedStateManifest",
    "SharedStateTrust",
    "StaticTrustProvider",
    "TrustedSigningKey",
    "build_state_payload",
    "read_approved_baseline",
    "seal_state_payload",
]
