"""Exact, repository-bound staging of GitHub code-branch suggestions."""

from __future__ import annotations

import base64
import hashlib
import os
import re
import secrets
import stat
import subprocess
from pathlib import Path, PurePosixPath
from typing import Self

from pydantic import ConfigDict, model_validator

from intent_engineering.core.models._base import StrictModel
from intent_engineering.integrations.workflows import check_workflow as canonical_check_workflow
from intent_engineering.storage.secure import SecureDirectory, UnsafePathError
from intent_engineering.team_state.restore import _run_git as _run_git_bounded

_CODEOWNERS_PATH = PurePosixPath(".github/CODEOWNERS")
_WORKFLOW_PATH = PurePosixPath(".github/workflows/intent-state.yml")
_CHECK_WORKFLOW_PATH = PurePosixPath(".github/workflows/intent-check.yml")
_MAX_SUGGESTION_BYTES = 64 * 1024
_MAX_GIT_OUTPUT_BYTES = 4096
_COMMIT = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


class CodeSuggestionError(ValueError):
    """Raised when code suggestions cannot be proven safe and current."""


class CodeSuggestionPreview(StrictModel):
    """JSON-safe exact content and code-tree preimages reviewed for staging."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        revalidate_instances="always",
        strict=True,
        validate_default=True,
    )

    branch: str
    head_commit: str
    codeowners_content: str
    workflow_content: str
    check_workflow_content: str
    codeowners_preimage: str | None
    workflow_preimage: str | None
    check_workflow_preimage: str | None
    digest: str

    @model_validator(mode="after")
    def validate_exact_preview(self) -> Self:
        if not _valid_branch(self.branch) or _COMMIT.fullmatch(self.head_commit) is None:
            raise ValueError("invalid code suggestion binding")
        codeowners = _suggestion_bytes(self.codeowners_content)
        workflow = _suggestion_bytes(self.workflow_content)
        check_workflow = _suggestion_bytes(self.check_workflow_content)
        codeowners_preimage = _decode_preimage(self.codeowners_preimage)
        workflow_preimage = _decode_preimage(self.workflow_preimage)
        check_workflow_preimage = _decode_preimage(self.check_workflow_preimage)
        if _DIGEST.fullmatch(self.digest) is None or self.digest != _preview_digest(
            self.branch,
            self.head_commit,
            codeowners,
            workflow,
            codeowners_preimage,
            workflow_preimage,
            check_workflow,
            check_workflow_preimage,
        ):
            raise ValueError("invalid code suggestion digest")
        return self


def preview_code_suggestions(
    root: Path,
    codeowners: str,
    workflow: str,
    check_workflow: str | None = None,
) -> CodeSuggestionPreview:
    """Capture exact suggestions and their current developer-branch preimages."""
    project = SecureDirectory.open(root)
    try:
        check_workflow = canonical_check_workflow() if check_workflow is None else check_workflow
        branch, head_commit = _code_head(project)
        codeowners_bytes = _suggestion_bytes(codeowners)
        workflow_bytes = _suggestion_bytes(workflow)
        check_workflow_bytes = _suggestion_bytes(check_workflow)
        codeowners_preimage = _read_optional(project, _CODEOWNERS_PATH)
        workflow_preimage = _read_optional(project, _WORKFLOW_PATH)
        check_workflow_preimage = _read_optional(project, _CHECK_WORKFLOW_PATH)
        return CodeSuggestionPreview(
            branch=branch,
            head_commit=head_commit,
            codeowners_content=codeowners,
            workflow_content=workflow,
            check_workflow_content=check_workflow,
            codeowners_preimage=_encode_preimage(codeowners_preimage),
            workflow_preimage=_encode_preimage(workflow_preimage),
            check_workflow_preimage=_encode_preimage(check_workflow_preimage),
            digest=_preview_digest(
                branch,
                head_commit,
                codeowners_bytes,
                workflow_bytes,
                codeowners_preimage,
                workflow_preimage,
                check_workflow_bytes,
                check_workflow_preimage,
            ),
        )
    finally:
        project.close()


def stage_code_suggestions(root: Path, preview: CodeSuggestionPreview) -> None:
    """Stage the exact reviewed bytes without overwriting incompatible files."""
    if type(preview) is not CodeSuggestionPreview:
        raise CodeSuggestionError("invalid code suggestion preview")
    preview = CodeSuggestionPreview.model_validate(preview, strict=True)
    project = SecureDirectory.open(root)
    try:
        if _code_head(project) != (preview.branch, preview.head_commit):
            raise CodeSuggestionError("stale code suggestion preview")
        proposals = (
            (
                _CODEOWNERS_PATH,
                _suggestion_bytes(preview.codeowners_content),
                _decode_preimage(preview.codeowners_preimage),
            ),
            (
                _WORKFLOW_PATH,
                _suggestion_bytes(preview.workflow_content),
                _decode_preimage(preview.workflow_preimage),
            ),
            (
                _CHECK_WORKFLOW_PATH,
                _suggestion_bytes(preview.check_workflow_content),
                _decode_preimage(preview.check_workflow_preimage),
            ),
        )
        observed = tuple(_read_optional(project, path) for path, _, _ in proposals)
        if any(current != preimage for current, (_, _, preimage) in zip(observed, proposals)):
            raise CodeSuggestionError("stale code suggestion preview")
        if any(
            current is not None and current != content
            for current, (_, content, _) in zip(observed, proposals)
        ):
            raise CodeSuggestionError("incompatible code suggestion")
        for current, (path, content, _) in zip(observed, proposals):
            if current == content:
                continue
            if _code_head(project) != (preview.branch, preview.head_commit):
                raise CodeSuggestionError("stale code suggestion preview")
            target = project.file(path, create_parents=True)
            try:
                if _code_head(project) != (preview.branch, preview.head_commit):
                    raise CodeSuggestionError("stale code suggestion preview")
                _install_absent(target.parent_fd, target.name, content)
            finally:
                target.close()
    finally:
        project.close()


def _code_head(project: SecureDirectory) -> tuple[str, str]:
    branch = _git(project, "symbolic-ref", "--quiet", "--short", "HEAD", allow_failure=True)
    if branch is None or not _valid_branch(branch):
        raise CodeSuggestionError("developer code branch required")
    head_commit = _git(project, "rev-parse", "--verify", "HEAD")
    if head_commit is None or _COMMIT.fullmatch(head_commit) is None:
        raise CodeSuggestionError("developer code branch required")
    return branch, head_commit


def _git(project: SecureDirectory, *arguments: str, allow_failure: bool = False) -> str | None:
    try:
        output = _run_git_bounded(
            project.path,
            tuple(arguments),
            maximum=_MAX_GIT_OUTPUT_BYTES,
        )
    except (OSError, subprocess.SubprocessError, TimeoutError, ValueError) as error:
        if allow_failure:
            return None
        raise CodeSuggestionError("developer code branch unavailable") from error
    try:
        value = output.decode("ascii").strip()
    except UnicodeDecodeError as error:
        raise CodeSuggestionError("developer code branch unavailable") from error
    if not value or "\n" in value or "\r" in value:
        raise CodeSuggestionError("developer code branch unavailable")
    return value


def _install_absent(parent_fd: int, name: str, content: bytes) -> None:
    """Publish one owned temporary inode only if the reviewed target remains absent."""
    temporary = ""
    descriptor = -1
    identity: tuple[int, int] | None = None
    linked = False
    view = memoryview(content)
    try:
        for _attempt in range(32):
            temporary = f".{name}.{secrets.token_hex(16)}.tmp"
            try:
                descriptor = os.open(
                    temporary,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                    0o644,
                    dir_fd=parent_fd,
                )
            except FileExistsError:
                continue
            break
        if descriptor < 0:
            raise UnsafePathError()
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise UnsafePathError()
        identity = metadata.st_dev, metadata.st_ino
        offset = 0
        while offset < len(view):
            written = os.write(descriptor, view[offset:])
            if written <= 0:
                raise UnsafePathError()
            offset += written
        os.fsync(descriptor)
        prepared = os.fstat(descriptor)
        if (
            not stat.S_ISREG(prepared.st_mode)
            or prepared.st_nlink != 1
            or (prepared.st_dev, prepared.st_ino) != identity
            or prepared.st_size != len(content)
        ):
            raise UnsafePathError()
        try:
            os.link(
                temporary,
                name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileExistsError as error:
            raise CodeSuggestionError("stale code suggestion preview") from error
        linked = True
        os.unlink(temporary, dir_fd=parent_fd)
        temporary = ""
        os.fsync(parent_fd)
        final = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(final.st_mode)
            or final.st_nlink != 1
            or (final.st_dev, final.st_ino) != identity
            or final.st_size != len(content)
        ):
            raise UnsafePathError()
    except OSError as error:
        raise UnsafePathError() from error
    finally:
        view.release()
        if descriptor >= 0:
            os.close(descriptor)
        if temporary and identity is not None:
            try:
                leftover = os.stat(temporary, dir_fd=parent_fd, follow_symlinks=False)
                if (leftover.st_dev, leftover.st_ino) == identity:
                    os.unlink(temporary, dir_fd=parent_fd)
                    os.fsync(parent_fd)
            except (FileNotFoundError, OSError):
                pass
        if linked:
            content = b""


def _valid_branch(branch: str) -> bool:
    return bool(
        branch != "intent-state"
        and _BRANCH.fullmatch(branch) is not None
        and ".." not in branch
        and "//" not in branch
        and not branch.endswith(("/", ".", ".lock"))
    )


def _read_optional(project: SecureDirectory, relative: PurePosixPath) -> bytes | None:
    parts = relative.parts
    descriptor = os.dup(project.descriptor)
    try:
        for component in parts[:-1]:
            try:
                metadata = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                return None
            if not stat.S_ISDIR(metadata.st_mode):
                raise UnsafePathError()
            try:
                child = os.open(
                    component,
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=descriptor,
                )
            except OSError as error:
                raise UnsafePathError() from error
            opened = os.fstat(child)
            if not stat.S_ISDIR(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
                metadata.st_dev,
                metadata.st_ino,
            ):
                os.close(child)
                raise UnsafePathError()
            os.close(descriptor)
            descriptor = child
        try:
            metadata = os.stat(parts[-1], dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise UnsafePathError()
        target = project.file(relative)
        try:
            return target.read_bytes_nonblocking(max_bytes=_MAX_SUGGESTION_BYTES)
        finally:
            target.close()
    finally:
        os.close(descriptor)


def _suggestion_bytes(value: str) -> bytes:
    if type(value) is not str or "\x00" in value:
        raise CodeSuggestionError("invalid code suggestion content")
    content = value.encode("utf-8")
    if not content or len(content) > _MAX_SUGGESTION_BYTES:
        raise CodeSuggestionError("invalid code suggestion content")
    return content


def _encode_preimage(content: bytes | None) -> str | None:
    if content is None:
        return None
    return base64.urlsafe_b64encode(content).rstrip(b"=").decode("ascii")


def _decode_preimage(value: str | None) -> bytes | None:
    if value is None:
        return None
    if type(value) is not str or len(value) > (_MAX_SUGGESTION_BYTES * 4 // 3 + 4):
        raise ValueError("invalid code suggestion preimage")
    try:
        decoded = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (UnicodeEncodeError, ValueError) as error:
        raise ValueError("invalid code suggestion preimage") from error
    if len(decoded) > _MAX_SUGGESTION_BYTES or _encode_preimage(decoded) != value:
        raise ValueError("invalid code suggestion preimage")
    return decoded


def _preview_digest(
    branch: str,
    head_commit: str,
    codeowners: bytes,
    workflow: bytes,
    codeowners_preimage: bytes | None,
    workflow_preimage: bytes | None,
    check_workflow: bytes,
    check_workflow_preimage: bytes | None,
) -> str:
    digest = hashlib.sha256()
    for label, value in (
        (b"branch", branch.encode("utf-8")),
        (b"head", head_commit.encode("ascii")),
        (b"codeowners", codeowners),
        (b"workflow", workflow),
        (b"codeowners-preimage", codeowners_preimage),
        (b"workflow-preimage", workflow_preimage),
        (b"check-workflow", check_workflow),
        (b"check-workflow-preimage", check_workflow_preimage),
    ):
        digest.update(len(label).to_bytes(2, "big"))
        digest.update(label)
        if value is None:
            digest.update(b"\x00")
        else:
            digest.update(b"\x01")
            digest.update(len(value).to_bytes(8, "big"))
            digest.update(value)
    return f"sha256:{digest.hexdigest()}"
