"""Repository-bound verification and atomic restore of approved shared intent state.

The protected Git ref stores a readable canonical manifest, a canonical JSON
encryption envelope, and a canonical signature envelope.  Decrypted state is a
canonical JSON object with a sorted list of ``{path,size,sha256,content_base64}``
entries.  Paths are selected from :data:`CANONICAL_STATE_PATHS`; no decrypted
path is ever passed to a filesystem API before that allowlist check.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import selectors
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Final, Literal, Protocol
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
from intent_engineering.core.models import ProjectConfig
from intent_engineering.core.models._base import StrictModel
from intent_engineering.intent_workflow.check import (
    SharedStateRestoreResult,
    SharedStateRestoreStatus,
)
from intent_engineering.storage.jsonl.approval_store import (
    JsonlApprovalStore,
    JsonlWritePlanStore,
)
from intent_engineering.storage.jsonl.strict import loads_strict_object
from intent_engineering.storage.secure import SecureDirectory, UnsafePathError
from intent_engineering.storage.transaction import LocalTransactionCoordinator
from intent_engineering.validation import validate_project

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
    process = subprocess.Popen(
        ("git", "-C", str(repo), *arguments),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
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
        return b"".join(chunks)
    finally:
        selector.close()
        output.close()
        if process.poll() is None:
            process.kill()
            process.wait()


def _origin_repository(repo: Path) -> str:
    raw = _run_git(repo, ("remote", "get-url", "origin"), maximum=MAX_GIT_TEXT_BYTES)
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
    def __init__(self, root: Path) -> None:
        self._root = root

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
        raw = (
            _run_git(
                self._root,
                ("rev-list", "--parents", "-n", "1", commit),
                maximum=256,
            )
            .decode("ascii")
            .strip()
        )
        parts = tuple(raw.split())
        if (
            not parts
            or parts[0] != commit
            or any(_GIT_COMMIT.fullmatch(item) is None for item in parts)
        ):
            raise ValueError("invalid shared-state lineage")
        if len(parts) > 2:
            raise ValueError("shared-state merges are unsupported")
        return parts[1:]

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
    return _VerifiedRelease(parsed)


def _verify_release_lineage(
    reader: _GitRefReader,
    tip_commit: str,
    trust: SharedStateTrust,
    now: datetime,
    marker: Mapping[str, object] | None,
) -> _VerifiedRelease:
    """Authenticate a bounded first-parent chain to the cached release or genesis."""
    commit = tip_commit
    expected_digest: str | None = None
    tip: _VerifiedRelease | None = None
    for _depth in range(MAX_ANCESTRY_COMMITS):
        release = _verify_release_manifest(reader, commit, trust, now)
        if tip is None:
            tip = release
        if expected_digest is not None and release.manifest.bundle_digest != expected_digest:
            raise _Stale("divergent shared-state lineage")
        if (
            marker is not None
            and marker["bundle_digest"] == release.manifest.bundle_digest
            and marker["ref_commit"] == commit
        ):
            return tip
        parents = reader.parents(commit)
        if not parents:
            if release.manifest.parent_bundle_digest is not None:
                raise _Stale("invalid shared-state genesis")
            if marker is not None:
                raise _Stale("local shared-state lineage diverged")
            return tip
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


def _read_local_marker(root: Path) -> dict[str, object] | None:
    try:
        project = SecureDirectory.open(root)
        workspace = project.subdirectory(".intent")
        marker = workspace.file("cache/shared-state.json")
    except (FileNotFoundError, UnsafePathError):
        return None
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
        workspace.close()
        project.close()


def _local_state_matches(root: Path, files: Mapping[str, bytes]) -> bool:
    """Authenticate an equal-state fast path against every decrypted canonical byte."""
    try:
        project = SecureDirectory.open(root)
        workspace = project.subdirectory(".intent")
    except (FileNotFoundError, OSError, UnsafePathError):
        return False
    try:
        for path in CANONICAL_STATE_PATHS:
            target = workspace.file(path)
            try:
                if target.read_optional_nonblocking(max_bytes=MAX_FILE_BYTES) != files[path]:
                    return False
            finally:
                target.close()
        return True
    except (OSError, UnsafePathError):
        return False
    finally:
        workspace.close()
        project.close()


def _write_staged_state(stage: Path, files: Mapping[str, bytes], marker: bytes) -> None:
    from intent_engineering.core.policy.project import initialize_project

    initialize_project(stage)
    project = SecureDirectory.open(stage)
    workspace = project.subdirectory(".intent")
    try:
        for path in sorted(files):
            target = workspace.file(path)
            try:
                target.atomic_write(files[path], reject_target_races=True)
            finally:
                target.close()
        marker_file = workspace.file("cache/shared-state.json")
        try:
            marker_file.atomic_write(marker, reject_target_races=True)
        finally:
            marker_file.close()
    finally:
        workspace.close()
        project.close()


def _validate_staged_state(
    stage: Path,
    manifest: SharedStateManifest,
    trust: SharedStateTrust,
) -> None:
    report = validate_project(stage)
    if not report.valid or report.graph_version != manifest.graph_version:
        raise ValueError("restored shared state is invalid")
    raw_config = (stage / ".intent/config.yaml").read_bytes()
    loaded = yaml.safe_load(raw_config.decode("utf-8"))
    config = ProjectConfig.model_validate(loaded)
    if config.project_id != trust.project_id or config.project_id != manifest.project_id:
        raise ValueError("restored project identity mismatch")
    plans = JsonlWritePlanStore(stage / ".intent/approvals/plans.jsonl")
    approvals = JsonlApprovalStore(stage / ".intent/approvals/approvals.jsonl")
    try:
        plans.list()
        approvals.list()
    finally:
        approvals.close()
        plans.close()
    policy = (stage / ".intent/approvals/policy.yaml").read_bytes()
    if policy:
        MutationPolicy.model_validate(load_strict_yaml_mapping_bytes(policy))


def _replace_existing_state(
    root: Path,
    files: Mapping[str, bytes],
    marker: bytes,
    fault_hook: Callable[[str], None],
) -> None:
    project = SecureDirectory.open(root)
    workspace = project.subdirectory(".intent")
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
    coordinator = LocalTransactionCoordinator(
        workspace.file("history/.local-transaction.json"),
        targets,
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
        with coordinator.transaction(rollback_base_exceptions=True) as transaction:
            for path in CANONICAL_STATE_PATHS:
                transaction.write(path_to_name[path], files[path])
                fault_hook(f"target:{path}")
            transaction.write(path_to_name["cache/checkpoints.yaml"], b"")
            transaction.write(path_to_name["cache/shared-state.json"], marker)
            fault_hook("existing_precommit")
    finally:
        coordinator.close()
        for target in targets.values():
            target.close()
        workspace.close()
        project.close()


def _install_fresh_state(root: Path, stage: Path, fault_hook: Callable[[str], None]) -> None:
    root_directory = SecureDirectory.open(root)
    stage_directory = SecureDirectory.open(stage)
    installed = False
    try:
        fault_hook("fresh_preinstall")
        os.rename(
            ".intent",
            ".intent",
            src_dir_fd=stage_directory.descriptor,
            dst_dir_fd=root_directory.descriptor,
        )
        installed = True
        fault_hook("fresh_installed")
        os.fsync(root_directory.descriptor)
    except BaseException:
        if installed:
            os.rename(
                ".intent",
                ".intent",
                src_dir_fd=root_directory.descriptor,
                dst_dir_fd=stage_directory.descriptor,
            )
            os.fsync(root_directory.descriptor)
        raise
    finally:
        stage_directory.close()
        root_directory.close()


class GitSharedStateRestorer:
    """Verify a protected Git ref and atomically install its approved state."""

    def __init__(
        self,
        trust_provider: TrustProvider,
        *,
        clock: Callable[[], datetime] | None = None,
        fault_hook: Callable[[str], None] | None = None,
    ) -> None:
        self._trust_provider = trust_provider
        self._clock = clock or (lambda: datetime.now(UTC))
        self._fault_hook = fault_hook or (lambda _stage: None)

    def verify_and_restore_approved_baseline(self, root: Path) -> SharedStateRestoreResult:
        stage: Path | None = None
        trust: SharedStateTrust | None = None
        private: X25519PrivateKey | None = None
        shared = b""
        wrapping_key = b""
        content_key = b""
        plaintext = b""
        files: dict[str, bytes] = {}
        try:
            trust = self._trust_provider.load()
            if trust is None:
                raise _Unavailable("shared-state trust unavailable")
            if type(trust) is not SharedStateTrust:
                raise ValueError("invalid shared-state trust")
            root = Path(os.path.abspath(root))
            if _origin_repository(root) != trust.repository_id:
                raise ValueError("repository identity mismatch")
            reader = _GitRefReader(root)
            commit = reader.commit()
            now = self._clock()
            if now.tzinfo is None or now.utcoffset() != timedelta(0):
                raise ValueError("invalid shared-state time")
            marker = _read_local_marker(root)
            tip = _verify_release_lineage(reader, commit, trust, now, marker)
            manifest = tip.manifest
            name = _release_name(manifest)
            bundle = reader.blob(commit, f"bundles/{name}.intent", MAX_BUNDLE_BYTES)
            if len(bundle) != manifest.bundle_size or _digest(bundle) != manifest.bundle_digest:
                raise ValueError("shared-state bundle mismatch")
            bundle_model = _parse_canonical_model(
                bundle,
                EncryptedStateBundle,
                MAX_BUNDLE_BYTES,
            )
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
            files = _parse_payload(plaintext)
            marker_bytes = _marker_bytes(manifest, commit)
            if (
                marker is not None
                and marker["bundle_digest"] == manifest.bundle_digest
                and _local_state_matches(root, files)
            ):
                report = validate_project(root)
                if report.valid and report.graph_version == manifest.graph_version:
                    return SharedStateRestoreResult(status=SharedStateRestoreStatus.VERIFIED)
            stage = Path(tempfile.mkdtemp(prefix=".intent-restore-", dir=root))
            _write_staged_state(stage, files, marker_bytes)
            _validate_staged_state(stage, manifest, trust)
            self._fault_hook("validated")
            if (root / ".intent").exists():
                _replace_existing_state(root, files, marker_bytes, self._fault_hook)
            else:
                _install_fresh_state(root, stage, self._fault_hook)
            return SharedStateRestoreResult(status=SharedStateRestoreStatus.VERIFIED)
        except _Unavailable:
            return SharedStateRestoreResult(status=SharedStateRestoreStatus.UNAVAILABLE)
        except _UpgradeRequired:
            return SharedStateRestoreResult(status=SharedStateRestoreStatus.UPGRADE_REQUIRED)
        except _Stale:
            return SharedStateRestoreResult(status=SharedStateRestoreStatus.STALE)
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
            private = None
            shared = b""
            wrapping_key = b""
            content_key = b""
            plaintext = b""
            files.clear()
            del self
            raise cancellation.with_traceback(None) from None
        finally:
            if stage is not None:
                shutil.rmtree(stage, ignore_errors=True)


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
    "seal_state_payload",
]
