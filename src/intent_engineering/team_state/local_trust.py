"""Bounded public team enrollment for reconstructing local OS-keyring trust."""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NoReturn

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from pydantic import ConfigDict, Field, field_validator, model_validator

from intent_engineering.capture.mcp.profile_loader import load_strict_yaml_mapping_bytes
from intent_engineering.core.models import ProjectConfig
from intent_engineering.core.models._base import StrictModel
from intent_engineering.storage._atomic import same_path_lock
from intent_engineering.storage.jsonl.strict import loads_strict_object
from intent_engineering.storage.secure import SecureDirectory, SecureFile
from intent_engineering.team_state.keys import (
    GitHubIdentity,
    KeyringRecipientKeyStore,
    RecipientEnrollmentBinding,
    _key_id,
)
from intent_engineering.team_state.models import RecipientRecord
from intent_engineering.team_state.restore import (
    TRUST_ENVIRONMENT_VARIABLE,
    EnvironmentTrustProvider,
    RecipientKeyStoreTrustProvider,
    SharedStateTrust,
    TrustedSigningKey,
    TrustProvider,
)

if TYPE_CHECKING:
    from intent_engineering.cli.runtime import Runtime

MAX_LOCAL_TRUST_BYTES = 32 * 1024
_FILENAME = "team-trust.json"


class LocalTrustError(ValueError):
    """Fixed public failure without provider or document details."""

    def __init__(self) -> None:
        super().__init__("local team trust unavailable")


def _failure(caught: BaseException) -> NoReturn:
    caught.__traceback__ = None
    caught.__cause__ = None
    caught.__context__ = None
    if isinstance(caught, Exception):
        raise LocalTrustError() from None
    raise caught.with_traceback(None) from None


class LocalTrustConfig(StrictModel):
    """Public reviewed identity and keys; no private key is serializable here."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    project_id: str
    repository_id: str
    recipient_key_id: str
    recipient: RecipientRecord
    signing_public_keys: dict[str, str] = Field(min_length=1, max_length=64)

    @field_validator("schema_version", mode="before")
    @classmethod
    def require_integer_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("invalid local trust version")
        return value

    def enrollment_binding(self) -> RecipientEnrollmentBinding:
        recipient = self.recipient
        return RecipientEnrollmentBinding(
            project_id=recipient.project_id,
            repository_id=recipient.repository_id,
            actor=recipient.actor,
            github_identity=GitHubIdentity(
                account_id=recipient.github_account_id,
                login=recipient.github_login,
            ),
            webauthn_credential_id=recipient.webauthn_credential_id,
            webauthn_credential_public_key=recipient.webauthn_credential_public_key,
            enrolled_at=recipient.enrolled_at,
        )

    def trusted_signing_keys(self) -> tuple[TrustedSigningKey, ...]:
        keys = []
        for signature_id, encoded in sorted(self.signing_public_keys.items()):
            if len(encoded) != 44:
                raise ValueError("invalid public signing key")
            public = base64.b64decode(encoded, validate=True)
            if base64.b64encode(public).decode("ascii") != encoded:
                raise ValueError("invalid public signing key")
            keys.append(TrustedSigningKey(signature_id, public))
        return tuple(keys)

    @model_validator(mode="after")
    def validate_binding(self) -> LocalTrustConfig:
        recipient = RecipientRecord.model_validate(self.recipient.model_dump(mode="python"))
        binding = self.enrollment_binding()
        if (
            self.project_id != recipient.project_id
            or self.repository_id != recipient.repository_id
            or self.recipient_key_id != recipient.key_id
            or self.recipient_key_id != _key_id(binding)
        ):
            raise ValueError("invalid local trust binding")
        self.trusted_signing_keys()
        return self


def _read(target: SecureFile) -> LocalTrustConfig | None:
    content = target.read_optional_nonblocking(max_bytes=MAX_LOCAL_TRUST_BYTES)
    if content is None:
        return None
    loads_strict_object(content.decode("utf-8"))
    return LocalTrustConfig.model_validate_json(content)


def _check_project(directory: SecureDirectory, project_id: str) -> None:
    target = directory.file("config.yaml")
    try:
        content = target.read_optional_nonblocking(max_bytes=64 * 1024)
        if content is not None:
            config = ProjectConfig.model_validate_json(
                json.dumps(load_strict_yaml_mapping_bytes(content), allow_nan=False)
            )
            if config.project_id != project_id:
                raise ValueError("invalid local trust project")
    finally:
        target.close()


def save_local_trust(
    runtime: Runtime,
    recipient: RecipientRecord,
    signing_public_keys: Mapping[str, bytes],
) -> None:
    """Save an exact reviewed public binding once; incompatible writes fail closed."""
    caught: BaseException
    try:
        if any(type(key) is not bytes or len(key) != 32 for key in signing_public_keys.values()):
            raise ValueError("invalid signing keys")
        config = LocalTrustConfig(
            project_id=runtime.config.project_id,
            repository_id=recipient.repository_id,
            recipient_key_id=recipient.key_id,
            recipient=recipient,
            signing_public_keys={
                name: base64.b64encode(public).decode("ascii")
                for name, public in sorted(signing_public_keys.items())
            },
        )
        content = config.model_dump_json().encode("utf-8")
        if len(content) > MAX_LOCAL_TRUST_BYTES:
            raise ValueError("invalid local trust size")
        target = runtime.workspace_directory.file(_FILENAME)
        try:
            with same_path_lock(target):
                _check_project(runtime.workspace_directory, config.project_id)
                old = _read(target)
                if old is not None and old != config:
                    raise ValueError("local trust changed")
                if old is None:
                    target.atomic_write(content, reject_target_races=True)
        finally:
            target.close()
        return
    except BaseException as error:  # noqa: BLE001 - fixed public/cancellation boundary
        caught = error
    _failure(caught)


def load_local_trust(root: Path) -> LocalTrustConfig | None:
    """Read public trust without opening a canonical runtime or creating paths."""
    caught: BaseException
    try:
        project = SecureDirectory.open(root)
        try:
            try:
                os.stat(".intent", dir_fd=project.descriptor, follow_symlinks=False)
            except FileNotFoundError:
                return None
            workspace = project.subdirectory(".intent")
            try:
                target = workspace.file(_FILENAME)
                try:
                    config = _read(target)
                    if config is not None:
                        _check_project(workspace, config.project_id)
                    return config
                finally:
                    target.close()
            finally:
                workspace.close()
        finally:
            project.close()
    except BaseException as error:  # noqa: BLE001 - fixed public/cancellation boundary
        caught = error
    _failure(caught)


class LocalTrustProvider:
    """Reconstruct the production recipient store from durable public enrollment."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def load(self) -> SharedStateTrust | None:
        caught: BaseException
        trust = None
        try:
            config = load_local_trust(self._root)
            if config is None:
                return None
            provider = RecipientKeyStoreTrustProvider(
                project_id=config.project_id,
                repository_id=config.repository_id,
                recipient_key_id=config.recipient_key_id,
                signing_keys=config.trusted_signing_keys(),
                key_store=KeyringRecipientKeyStore(config.enrollment_binding()),
            )
            trust = provider.load()
            if trust is not None:
                public = (
                    X25519PrivateKey.from_private_bytes(
                        trust.recipient_private_key,
                    )
                    .public_key()
                    .public_bytes_raw()
                )
                if (
                    base64.urlsafe_b64encode(public).rstrip(b"=").decode()
                    != config.recipient.public_key
                ):
                    raise ValueError("local recipient key changed")
            return trust
        except BaseException as error:  # noqa: BLE001 - scrub provider traceback
            caught = error
        trust = None
        _failure(caught)


def local_or_environment_trust(
    root: Path,
    environment: Mapping[str, str] | None = None,
) -> TrustProvider:
    """Use explicit environment trust when present, otherwise enrolled local trust."""
    selected = os.environ if environment is None else environment
    if TRUST_ENVIRONMENT_VARIABLE in selected:
        return EnvironmentTrustProvider(selected)
    return LocalTrustProvider(root)


__all__ = [
    "LocalTrustConfig",
    "LocalTrustError",
    "LocalTrustProvider",
    "load_local_trust",
    "local_or_environment_trust",
    "save_local_trust",
]
