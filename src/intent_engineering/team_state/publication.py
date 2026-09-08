"""Reviewed preparation of encrypted team-state publication branches."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import selectors
import shutil
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
from typing import Protocol

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from intent_engineering.capture.mcp.profile_loader import load_strict_yaml_mapping_bytes
from intent_engineering.cli.runtime import Runtime
from intent_engineering.cli.writes import MutationPolicy
from intent_engineering.control_plane.models import (
    DecisionAction,
    DecisionSubject,
    HumanDecisionPayload,
)
from intent_engineering.control_plane.webauthn_service import VerifiedHumanDecision
from intent_engineering.core.models import ProjectConfig
from intent_engineering.mutations.models import ApprovalRecord, WritePlan
from intent_engineering.storage.jsonl.approval_store import parse_immutable_records
from intent_engineering.storage.secure import SecureFile, configured_graph_relative
from intent_engineering.team_state.archive import build_archive
from intent_engineering.team_state.crypto import EncryptedBundle, decrypt_bundle
from intent_engineering.team_state.models import (
    CANONICAL_STATE_PATHS,
    CanonicalStateFile,
    CanonicalStateSnapshot,
    PreparedPublication,
    RecipientRecord,
    RemoteStateSnapshot,
    TeamStateManifest,
)
from intent_engineering.team_state.restore import (
    StateSignatureEnvelope,
    _git_executable_token,
    _manifest_aad,
    seal_state_payload,
)
from intent_engineering.validation import validate_canonical_snapshot

_GENESIS_PARENT = "sha256:" + "0" * 64
_DECISION_LIFETIME = timedelta(minutes=5)
_TARGET_PATHS = {
    "approvals": "approvals/approvals.jsonl",
    "receipts": "approvals/receipts.jsonl",
    "evidence": "evidence/evidence.jsonl",
    "graph": "graph.yaml",
    "history": "history/changesets.jsonl",
    "intent_proposals": "history/intent-proposals.jsonl",
    "cases": "reconciliation/cases.jsonl",
}
_EXTRA_PATHS = {
    "publication_config": "config.yaml",
    "publication_plans": "approvals/plans.jsonl",
    "publication_policy": "approvals/policy.yaml",
}
_MAX_GIT_OUTPUT_BYTES = 64 * 1024
_GIT_TIMEOUT_SECONDS = 10.0
_COMMIT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
_GIT_EXECUTABLE = Path("/usr/bin/git")
_GIT_SUPERVISOR = (
    "import os,subprocess,sys,time;"
    "fd=int(sys.argv[1]);"
    "child=subprocess.Popen(sys.argv[2:],stdin=subprocess.DEVNULL);"
    "code=child.wait();"
    "os.write(fd,(str(code)+'\\n').encode('ascii'));"
    "os.close(fd);"
    "time.sleep(3600)"
)


@dataclass(frozen=True, slots=True)
class PublicationAuthority:
    recipients: tuple[RecipientRecord, ...]
    signing_private_keys: Mapping[str, bytes]
    remote_state: RemoteStateSnapshot | None
    publication_base_commit: str | None = None


@dataclass(frozen=True, slots=True)
class PublicationPreview:
    """Credential-free projection of the exact encrypted release awaiting review."""

    payload: HumanDecisionPayload
    manifest: TeamStateManifest
    snapshot_digest: str
    recipient_key_ids: tuple[str, ...]
    branch: str


class PublicationPublisher(Protocol):
    def publish(self, publication: PreparedPublication, *, base_commit: str | None) -> None: ...


class PublicationCleanupError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class _GitResult:
    returncode: int
    stdout: bytes


class TemporaryWorktreePublisher:
    """Push exact publication artifacts from one safely owned temporary worktree."""

    def __init__(
        self,
        repository: Path,
        *,
        temp_root: Path | None = None,
        transport: Path | None = None,
        allow_local_transport: bool = False,
    ) -> None:
        self._repository = Path(os.path.abspath(repository))
        self._temp_root = Path(
            os.path.abspath(temp_root if temp_root is not None else tempfile.gettempdir())
        )
        try:
            repository_real = self._repository.resolve(strict=True)
            root_real = self._temp_root.resolve(strict=True)
            metadata = os.lstat(root_real)
        except (FileNotFoundError, OSError) as error:
            raise ValueError("invalid temporary publication root") from error
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or root_real == repository_real
            or repository_real in root_real.parents
        ):
            raise ValueError("invalid temporary publication root")
        self._repository = repository_real
        self._temp_root = root_real
        if transport is not None and not allow_local_transport:
            raise ValueError("local publication transport is unavailable")
        self._transport = None if transport is None else str(Path(os.path.abspath(transport)))
        self._allow_local_transport = allow_local_transport

    @staticmethod
    def _environment() -> dict[str, str]:
        return {
            "GIT_AUTHOR_NAME": "Intent Engineering",
            "GIT_AUTHOR_EMAIL": "intent-state@localhost",
            "GIT_ASKPASS": "/usr/bin/false",
            "GIT_COMMITTER_NAME": "Intent Engineering",
            "GIT_COMMITTER_EMAIL": "intent-state@localhost",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
            "SSH_ASKPASS": "/usr/bin/false",
        }

    @staticmethod
    def _trusted_transport(repository_id: str) -> str:
        matched = re.fullmatch(
            r"github\.com/([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/"
            r"([A-Za-z0-9][A-Za-z0-9_.-]{0,99})",
            repository_id,
        )
        if matched is None or matched.group(2) in {".", ".."} or matched.group(2).endswith(".git"):
            raise ValueError("publication branch unavailable")
        owner, repository = matched.groups()
        return f"https://github.com/{owner}/{repository}.git"

    @staticmethod
    def _stop_git(process: subprocess.Popen[bytes]) -> None:
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

    def _git(
        self,
        cwd: Path,
        *arguments: str,
        check: bool = True,
        allow_file: bool = False,
    ) -> _GitResult:
        process: subprocess.Popen[bytes] | None = None
        selector = selectors.DefaultSelector()
        status_read = -1
        status_write = -1
        status = bytearray()
        output = bytearray()
        output_size = 0
        token: tuple[int, int, int, int, str] | None = None
        try:
            token = _git_executable_token()
            status_read, status_write = os.pipe()
            os.set_inheritable(status_read, False)
            os.set_inheritable(status_write, True)
            argv = (
                sys.executable,
                "-I",
                "-S",
                "-c",
                _GIT_SUPERVISOR,
                str(status_write),
                str(_GIT_EXECUTABLE),
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
                f"protocol.file.allow={'always' if allow_file else 'never'}",
                "-c",
                "protocol.git.allow=never",
                "-c",
                "protocol.ssh.allow=never",
                "-c",
                "protocol.https.allow=always",
                "-c",
                "submodule.recurse=false",
                "-C",
                str(cwd),
                *arguments,
            )
            process = subprocess.Popen(
                argv,
                cwd="/",
                env=self._environment(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                close_fds=True,
                pass_fds=(status_write,),
                start_new_session=True,
                shell=False,
                bufsize=0,
            )
            assert process.stdout is not None and process.stderr is not None
            os.close(status_write)
            status_write = -1
            selector.register(process.stdout, selectors.EVENT_READ)
            selector.register(process.stderr, selectors.EVENT_READ)
            selector.register(status_read, selectors.EVENT_READ)
            deadline = time.monotonic() + _GIT_TIMEOUT_SECONDS
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not (events := selector.select(remaining)):
                    raise TimeoutError("publication Git unavailable")
                for key, _mask in events:
                    file_object = key.fileobj
                    descriptor = (
                        file_object if isinstance(file_object, int) else file_object.fileno()
                    )
                    chunk = os.read(descriptor, 64 * 1024)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    elif descriptor == status_read:
                        status.extend(chunk)
                        if len(status) > 16:
                            raise ValueError("publication Git unavailable")
                        if b"\n" in status:
                            self._stop_git(process)
                    else:
                        output_size += len(chunk)
                        if output_size > _MAX_GIT_OUTPUT_BYTES:
                            raise ValueError("publication Git unavailable")
                        if descriptor == process.stdout.fileno():
                            output.extend(chunk)
            try:
                returncode = int(bytes(status).strip())
            except ValueError as error:
                raise ValueError("publication Git unavailable") from error
            if check and returncode != 0:
                raise subprocess.CalledProcessError(returncode, argv)
            if _git_executable_token() != token:
                raise ValueError("publication Git unavailable")
            return _GitResult(returncode=returncode, stdout=bytes(output))
        finally:
            selector.close()
            status.clear()
            output.clear()
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
                    self._stop_git(process)

    def _cleanup(self, owner: Path, owner_identity: tuple[int, int]) -> None:
        current = os.lstat(owner)
        if (current.st_dev, current.st_ino) != owner_identity or not stat.S_ISDIR(current.st_mode):
            raise PublicationCleanupError("publication cleanup refused")
        for child in tuple(owner.iterdir()):
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
        try:
            os.rmdir(owner)
        except OSError as error:
            raise PublicationCleanupError("publication cleanup refused") from error

    def publish(self, publication: PreparedPublication, *, base_commit: str | None) -> None:
        publication = PreparedPublication.model_validate(publication.model_dump(mode="python"))
        owner = Path(tempfile.mkdtemp(prefix="intent-publication-", dir=self._temp_root))
        owner_stat = os.lstat(owner)
        owner_identity = (owner_stat.st_dev, owner_stat.st_ino)
        checkout = owner / "checkout"
        git_directory = owner / "release.git"
        failure: BaseException | None = None
        try:
            checkout.mkdir()
            self._git(
                owner,
                "init",
                "--quiet",
                "--separate-git-dir",
                str(git_directory),
                str(checkout),
            )
            transport = self._transport or self._trusted_transport(publication.repository_id)
            existing = self._git(
                checkout,
                "ls-remote",
                "--exit-code",
                "--heads",
                transport,
                f"refs/heads/{publication.branch}",
                check=False,
                allow_file=self._allow_local_transport,
            )
            if existing.returncode == 0:
                raise ValueError("publication branch unavailable")
            if existing.returncode not in {2}:
                raise ValueError("publication branch unavailable")
            self._git(checkout, "symbolic-ref", "HEAD", "refs/heads/build")
            if base_commit is not None:
                if re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", base_commit) is None:
                    raise ValueError("publication branch unavailable")
                self._git(
                    checkout,
                    "fetch",
                    "--force",
                    "--no-tags",
                    "--no-recurse-submodules",
                    "--no-write-fetch-head",
                    "--quiet",
                    transport,
                    "refs/heads/intent-state:refs/remotes/publication/intent-state",
                    allow_file=self._allow_local_transport,
                )
                resolved_parent = self._git(
                    checkout,
                    "rev-parse",
                    "--verify",
                    "refs/remotes/publication/intent-state^{commit}",
                ).stdout.strip()
                if resolved_parent != base_commit.encode("ascii"):
                    raise ValueError("publication branch unavailable")
                self._git(checkout, "update-ref", "refs/heads/build", base_commit)
            self._git(checkout, "read-tree", "--empty")
            artifacts = {
                "manifest.json": publication.manifest_bytes,
                publication.bundle_path: publication.bundle,
                publication.signature_path: publication.signatures,
            }
            for relative, content in artifacts.items():
                target = checkout / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            self._git(checkout, "add", "--", *artifacts)
            self._git(
                checkout,
                "commit",
                "--quiet",
                "-m",
                f"Publish intent state {publication.manifest.bundle_digest}",
            )
            if base_commit is None:
                state_ref = self._git(
                    checkout,
                    "ls-remote",
                    "--exit-code",
                    "--heads",
                    transport,
                    "refs/heads/intent-state",
                    check=False,
                    allow_file=self._allow_local_transport,
                )
                if state_ref.returncode != 2:
                    raise ValueError("publication branch unavailable")
            self._git(
                checkout,
                "push",
                "--porcelain",
                transport,
                f"refs/heads/build:refs/heads/{publication.branch}",
                allow_file=self._allow_local_transport,
            )
        except BaseException as error:  # noqa: BLE001 - cancellation identity is preserved
            failure = error
        cleanup_failure: BaseException | None = None
        try:
            self._cleanup(owner, owner_identity)
        except BaseException as error:  # noqa: BLE001 - fail closed on owned-path ambiguity
            cleanup_failure = error
        if failure is not None:
            if isinstance(failure, Exception):
                raise ValueError("publication branch unavailable") from None
            raise failure.with_traceback(None)
        if cleanup_failure is not None:
            if isinstance(cleanup_failure, PublicationCleanupError):
                raise cleanup_failure
            raise PublicationCleanupError("publication cleanup refused") from None


class PublicationService:
    def __init__(
        self,
        runtime: Runtime,
        *,
        repository_id: str,
        decision_repository_id: str,
        authority: Callable[[], PublicationAuthority],
        publisher: PublicationPublisher,
        challenge_source: Callable[[], bytes] = lambda: secrets.token_bytes(32),
    ) -> None:
        self._runtime = runtime
        self._repository_id = repository_id
        self._decision_repository_id = decision_repository_id
        self._authority = authority
        self._publisher = publisher
        self._challenge_source = challenge_source
        self._publication_base_commit: str | None = None
        self._pending: (
            tuple[
                PublicationPreview,
                PreparedPublication,
                bytes,
                bytes,
                str | None,
            ]
            | None
        ) = None

    def bind_publication_base_commit(self, commit: str) -> None:
        """Bind the next reviewed release to the exact state-branch anchor."""
        if (
            self._pending is not None
            or type(commit) is not str
            or _COMMIT.fullmatch(commit) is None
        ):
            raise ValueError("publication parent binding changed")
        self._publication_base_commit = commit

    def _current_authority(self) -> PublicationAuthority:
        authority = self._authority()
        if self._publication_base_commit is None:
            return authority
        if (
            authority.publication_base_commit is not None
            and authority.publication_base_commit != self._publication_base_commit
        ):
            raise ValueError("publication parent binding changed")
        return PublicationAuthority(
            recipients=authority.recipients,
            signing_private_keys=authority.signing_private_keys,
            remote_state=authority.remote_state,
            publication_base_commit=self._publication_base_commit,
        )

    @staticmethod
    def _digest(content: bytes) -> str:
        return f"sha256:{hashlib.sha256(content).hexdigest()}"

    def _capture(self) -> tuple[CanonicalStateSnapshot, bytes]:
        extras: dict[str, SecureFile] = {}
        try:
            for name, path in _EXTRA_PATHS.items():
                extras[name] = self._runtime.workspace_directory.file(path)
            captured = self._runtime.transactions.snapshot(extras)
            files: dict[str, bytes] = {}
            for name, path in _TARGET_PATHS.items():
                content = captured.content.get(name)
                if content is not None and type(content) is not bytes:
                    raise ValueError("canonical publication state unavailable")
                files[path] = b"" if content is None else content
            for name, path in _EXTRA_PATHS.items():
                content = captured.content.get(name)
                if content is not None and type(content) is not bytes:
                    raise ValueError("canonical publication state unavailable")
                files[path] = b"" if content is None else content
            validation = validate_canonical_snapshot(
                {
                    "config": files["config.yaml"],
                    "graph": files["graph.yaml"],
                    "history": files["history/changesets.jsonl"],
                    "cases": files["reconciliation/cases.jsonl"],
                    "evidence": files["evidence/evidence.jsonl"],
                    "receipts": files["approvals/receipts.jsonl"],
                    "checkpoints": None,
                }
            )
            if not validation.valid or validation.graph_version is None:
                raise ValueError("canonical publication state is invalid")
            config = ProjectConfig.model_validate_json(
                json.dumps(load_strict_yaml_mapping_bytes(files["config.yaml"]))
            )
            if (
                config != self._runtime.config
                or config.project_id != self._runtime.config.project_id
                or str(configured_graph_relative(config.graph_path)) != "graph.yaml"
            ):
                raise ValueError("publication project identity changed")
            parse_immutable_records(files["approvals/approvals.jsonl"], ApprovalRecord)
            parse_immutable_records(files["approvals/plans.jsonl"], WritePlan)
            if files["approvals/policy.yaml"]:
                MutationPolicy.model_validate(
                    load_strict_yaml_mapping_bytes(files["approvals/policy.yaml"])
                )
            snapshot = CanonicalStateSnapshot(
                project_id=config.project_id,
                repository_id=self._repository_id,
                graph_version=validation.graph_version,
                files=tuple(
                    CanonicalStateFile(path=path, content=files[path])
                    for path in CANONICAL_STATE_PATHS
                ),
            )
            archive = build_archive(snapshot)
            return snapshot, archive
        finally:
            for extra in extras.values():
                extra.close()

    @staticmethod
    def _authority_material(
        authority: PublicationAuthority,
        *,
        project_id: str,
        repository_id: str,
    ) -> tuple[tuple[RecipientRecord, ...], dict[str, bytes], str | None, str | None, bytes]:
        if type(authority) is not PublicationAuthority:
            raise ValueError("publication authority unavailable")
        recipients = tuple(
            RecipientRecord.model_validate(item.model_dump(mode="python"))
            for item in authority.recipients
        )
        key_ids = tuple(item.key_id for item in recipients)
        if not recipients or key_ids != tuple(sorted(key_ids)) or len(key_ids) != len(set(key_ids)):
            raise ValueError("publication recipients are invalid")
        if any(
            item.project_id != recipients[0].project_id
            or item.repository_id != recipients[0].repository_id
            for item in recipients
        ):
            raise ValueError("publication recipients are invalid")
        signing = dict(authority.signing_private_keys)
        if not signing or any(
            type(key) is not str or type(value) is not bytes for key, value in signing.items()
        ):
            raise ValueError("publication signing authority unavailable")
        signer_public = {
            key: Ed25519PrivateKey.from_private_bytes(value).public_key().public_bytes_raw()
            for key, value in signing.items()
        }
        remote = authority.remote_state
        publication_base_commit = authority.publication_base_commit
        if (
            publication_base_commit is not None
            and _COMMIT.fullmatch(publication_base_commit) is None
        ):
            raise ValueError("publication parent binding changed")
        if remote is not None:
            remote = RemoteStateSnapshot.model_validate(remote.model_dump(mode="python"))
            if (
                remote.repository_id != repository_id
                or remote.manifest.project_id != project_id
                or remote.manifest.repository_id != repository_id
            ):
                raise ValueError("publication parent binding changed")
            if publication_base_commit is not None and publication_base_commit != remote.commit:
                raise ValueError("publication parent binding changed")
            publication_base_commit = remote.commit
        canonical = json.dumps(
            {
                "recipients": [item.model_dump(mode="json") for item in recipients],
                "signers": {
                    key: base64.urlsafe_b64encode(value).decode("ascii")
                    for key, value in sorted(signer_public.items())
                },
                "remote": None if remote is None else remote.model_dump(mode="json"),
                "publication_base_commit": publication_base_commit,
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return (
            recipients,
            signing,
            None if remote is None else remote.manifest.bundle_digest,
            publication_base_commit,
            canonical,
        )

    def preview(self, *, now: datetime) -> PublicationPreview:
        if now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise ValueError("publication time must be UTC")
        snapshot, archive = self._capture()
        recipients, signing, parent_digest, parent_commit, authority_bytes = (
            self._authority_material(
                self._current_authority(),
                project_id=snapshot.project_id,
                repository_id=snapshot.repository_id,
            )
        )
        if any(
            item.project_id != snapshot.project_id or item.repository_id != snapshot.repository_id
            for item in recipients
        ):
            raise ValueError("publication recipient binding changed")
        recipient_keys = {
            item.key_id: base64.urlsafe_b64decode(
                item.public_key + "=" * (-len(item.public_key) % 4)
            )
            for item in recipients
        }
        artifacts = seal_state_payload(
            archive,
            project_id=snapshot.project_id,
            repository_id=snapshot.repository_id,
            graph_version=snapshot.graph_version,
            parent_bundle_digest=parent_digest,
            created_at=now.astimezone(UTC),
            recipient_public_keys=recipient_keys,
            signing_private_keys=signing,
        )
        manifest = TeamStateManifest.model_validate_json(artifacts.manifest)
        digest_hex = manifest.bundle_digest.removeprefix("sha256:")
        prepared = PreparedPublication(
            repository_id=snapshot.repository_id,
            branch=f"intent-publication/{digest_hex}",
            manifest=manifest,
            manifest_bytes=artifacts.manifest,
            bundle=artifacts.bundle,
            signatures=artifacts.signatures,
            bundle_path=artifacts.bundle_path,
            signature_path=artifacts.signature_path,
        )
        challenge = self._challenge_source()
        if type(challenge) is not bytes or len(challenge) < 16:
            raise ValueError("publication challenge unavailable")
        snapshot_digest = self._digest(archive)
        payload = HumanDecisionPayload(
            project_id=snapshot.project_id,
            repository_id=self._decision_repository_id,
            actor=self._runtime.config.local_actor,
            action=DecisionAction.PUBLISH_STATE,
            graph_version=snapshot.graph_version,
            parent_bundle_digest=parent_digest or _GENESIS_PARENT,
            subject=DecisionSubject(
                kind="publication",
                id=f"publication:{snapshot_digest.removeprefix('sha256:')}",
            ),
            subject_digest=self._digest(authority_bytes + archive),
            result_digest=manifest.bundle_digest,
            challenge=f"challenge:{hashlib.sha256(challenge).hexdigest()}",
            issued_at=now.astimezone(UTC),
            expires_at=now.astimezone(UTC) + _DECISION_LIFETIME,
        )
        preview = PublicationPreview(
            payload=payload,
            manifest=manifest,
            snapshot_digest=snapshot_digest,
            recipient_key_ids=manifest.recipient_key_ids,
            branch=prepared.branch,
        )
        self._pending = (preview, prepared, archive, authority_bytes, parent_commit)
        return preview

    def pending_publication(self) -> PreparedPublication:
        """Return only the encrypted draft; it carries no human authority."""
        if self._pending is None:
            raise ValueError("publication draft unavailable")
        return self._pending[1]

    def recover_preview(
        self,
        prepared: PreparedPublication,
        *,
        recipient_private_key: bytes,
        now: datetime,
    ) -> PublicationPreview:
        """Reauthenticate an encrypted draft against current state and issue fresh authority."""
        plaintext = b""
        signing: dict[str, bytes] = {}
        try:
            prepared = PreparedPublication.model_validate(prepared.model_dump(mode="python"))
            current = self.preview(now=now)
            pending = self._pending
            assert pending is not None
            _, _, archive, authority_bytes, parent_commit = pending
            manifest = prepared.manifest
            if (
                manifest.project_id != current.manifest.project_id
                or manifest.repository_id != current.manifest.repository_id
                or manifest.graph_version != current.manifest.graph_version
                or manifest.parent_bundle_digest != current.manifest.parent_bundle_digest
                or manifest.recipient_key_ids != current.manifest.recipient_key_ids
                or manifest.required_signature_ids != current.manifest.required_signature_ids
                or manifest.created_at > now
            ):
                raise ValueError("publication draft changed")
            envelope = StateSignatureEnvelope.model_validate_json(prepared.signatures)
            signed = json.dumps(
                {
                    "schema_version": 1,
                    "manifest_digest": self._digest(prepared.manifest_bytes),
                    "bundle_digest": manifest.bundle_digest,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            if (
                envelope.manifest_digest != self._digest(prepared.manifest_bytes)
                or envelope.bundle_digest != manifest.bundle_digest
                or tuple(item.signature_id for item in envelope.signatures)
                != manifest.required_signature_ids
            ):
                raise ValueError("publication draft signatures changed")
            _, signing, _, _, _ = self._authority_material(
                self._current_authority(),
                project_id=manifest.project_id,
                repository_id=manifest.repository_id,
            )
            for signature in envelope.signatures:
                Ed25519PrivateKey.from_private_bytes(
                    signing[signature.signature_id]
                ).public_key().verify(
                    base64.urlsafe_b64decode(
                        signature.signature + "=" * (-len(signature.signature) % 4)
                    ),
                    signed,
                )
            aad = _manifest_aad(
                project_id=manifest.project_id,
                repository_id=manifest.repository_id,
                graph_version=manifest.graph_version,
                parent_bundle_digest=manifest.parent_bundle_digest,
                created_at=manifest.created_at,
                recipient_key_ids=manifest.recipient_key_ids,
                required_signature_ids=manifest.required_signature_ids,
            )
            plaintext = decrypt_bundle(
                EncryptedBundle.model_validate_json(prepared.bundle), recipient_private_key, aad
            )
            if plaintext != archive:
                raise ValueError("publication draft snapshot changed")
            payload = HumanDecisionPayload.model_validate_json(
                current.payload.model_copy(
                    update={"result_digest": manifest.bundle_digest}
                ).model_dump_json()
            )
            preview = PublicationPreview(
                payload=payload,
                manifest=manifest,
                snapshot_digest=current.snapshot_digest,
                recipient_key_ids=manifest.recipient_key_ids,
                branch=prepared.branch,
            )
            self._pending = (preview, prepared, archive, authority_bytes, parent_commit)
            return preview
        except BaseException:
            self._pending = None
            raise
        finally:
            plaintext = b""
            recipient_private_key = b""
            signing.clear()

    def prepare(
        self,
        decision: VerifiedHumanDecision,
        *,
        now: datetime,
    ) -> PreparedPublication:
        pending = self._pending
        if pending is None or type(decision) is not VerifiedHumanDecision:
            raise ValueError("publication decision unavailable")
        preview, prepared, expected_archive, expected_authority, parent_commit = pending
        credential = decision.credential
        if (
            decision.payload != preview.payload
            or decision.verified_at < preview.payload.issued_at
            or decision.verified_at > preview.payload.expires_at
            or now < decision.verified_at
            or now > preview.payload.expires_at
            or credential.local_only
            or credential.project_id != preview.payload.project_id
            or credential.repository_id != preview.payload.repository_id
            or credential.actor != preview.payload.actor
        ):
            raise ValueError("publication decision changed")
        snapshot, archive = self._capture()
        recipients, _signing, current_parent, current_commit, authority_bytes = (
            self._authority_material(
                self._current_authority(),
                project_id=snapshot.project_id,
                repository_id=snapshot.repository_id,
            )
        )
        exact_recipients = tuple(
            item
            for item in recipients
            if item.project_id == credential.project_id
            and item.repository_id == self._repository_id
            and item.actor == credential.actor
            and item.webauthn_credential_id == credential.credential_id
            and item.webauthn_credential_public_key == credential.public_key
            and item.github_account_id == credential.github_account_id
            and item.github_login == credential.github_login
        )
        if len(exact_recipients) != 1:
            raise ValueError("publication decision changed")
        if (
            archive != expected_archive
            or authority_bytes != expected_authority
            or current_parent != prepared.manifest.parent_bundle_digest
            or current_commit != parent_commit
            or snapshot.graph_version != prepared.manifest.graph_version
        ):
            raise ValueError("publication state changed")
        self._publisher.publish(prepared, base_commit=parent_commit)
        self._pending = None
        self._publication_base_commit = None
        return prepared
