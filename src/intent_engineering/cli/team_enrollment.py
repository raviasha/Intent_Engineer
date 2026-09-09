"""Bounded public exchange files for the local team enrollment journey."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Annotated, Literal

import typer

from intent_engineering.storage.secure import SecureFile
from intent_engineering.team_state.enrollment import (
    JoinResponseV2,
    TeamEnrollmentError,
    TeamInviteV2,
    export_join_response,
    export_team_invite,
    parse_join_response,
    parse_team_invite,
)


def _read_public_file(path: Path, maximum: int) -> bytes:
    target = None
    try:
        target = SecureFile.from_path(path.absolute(), create_parents=False)
        metadata = os.stat(target.name, dir_fd=target.parent_fd, follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError("unsafe public file")
        return target.read_bytes_nonblocking(max_bytes=maximum)
    except Exception:  # noqa: BLE001 - fixed public file boundary
        raise TeamEnrollmentError("team enrollment unavailable") from None
    finally:
        if target is not None:
            target.close()


def read_invite(path: Path) -> TeamInviteV2:
    return parse_team_invite(_read_public_file(path, 32 * 1024))


def read_response(path: Path) -> JoinResponseV2:
    return parse_join_response(_read_public_file(path, 64 * 1024))


def write_public_file(path: Path, value: TeamInviteV2 | JoinResponseV2) -> None:
    """Export canonical public data to an exclusively created owner-only regular file."""
    target = None
    descriptor = None
    try:
        if type(value) is TeamInviteV2:
            content = export_team_invite(value)
        elif type(value) is JoinResponseV2:
            content = export_join_response(value)
        else:
            raise ValueError("invalid public export")
        target = SecureFile.from_path(path.absolute(), create_parents=False)
        descriptor = os.open(
            target.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=target.parent_fd,
        )
        os.fchmod(descriptor, 0o600)
        pending = memoryview(content)
        while pending:
            written = os.write(descriptor, pending)
            if written <= 0:
                raise ValueError("public export incomplete")
            pending = pending[written:]
        os.fsync(descriptor)
        os.fsync(target.parent_fd)
    except Exception:  # noqa: BLE001 - fixed public file boundary
        raise TeamEnrollmentError("team enrollment unavailable") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if target is not None:
            target.close()


def _begin(
    action: Literal["invite", "join", "approve-join"],
    project: Path,
    *,
    output: Path | None = None,
    invite: Path | None = None,
    response: Path | None = None,
    account_id: str | None = None,
    login: str | None = None,
) -> None:
    from intent_engineering.cli.dev import dev_command
    from intent_engineering.cli.runtime import load_runtime
    from intent_engineering.cli.team import discover_github_repository
    from intent_engineering.control_plane.team_enrollment import save_enrollment_request
    from intent_engineering.team_state.keys import GitHubIdentity

    runtime = None
    try:
        public_invite = read_invite(invite) if invite is not None else None
        public_response = read_response(response) if response is not None else None
        identity = (
            GitHubIdentity(account_id=account_id, login=login)
            if account_id is not None and login is not None
            else None
        )
        if output is not None:
            target = SecureFile.from_path(output.absolute(), create_parents=False)
            try:
                if target.exists():
                    raise ValueError("team enrollment unavailable")
            finally:
                target.close()
        repository = "github.com/" + discover_github_repository(project)
        runtime = load_runtime(project)
        save_enrollment_request(
            runtime,
            action=action,
            project_id=runtime.config.project_id,
            repository_id=repository,
            identity=identity,
            invite=public_invite,
            response=public_response,
            output=str(output.absolute()) if output else None,
        )
    except Exception:  # noqa: BLE001 - never reflect input bodies or provider errors
        typer.echo("intent error: team enrollment unavailable", err=True)
        raise typer.Exit(1) from None
    finally:
        if runtime is not None:
            runtime.close()
    typer.echo(f"Complete the {action} review in your local browser.")
    dev_command(project=project, prd=None, no_open=False, offline=False, status=False)


def invite_command(
    github_account_id: Annotated[str, typer.Option("--github-account-id")],
    github_login: Annotated[str, typer.Option("--github-login")],
    output: Annotated[Path, typer.Option("--output")],
    project: Annotated[Path, typer.Option("--project")] = Path("."),
) -> None:
    """Create a signed public invitation through local browser review."""
    _begin("invite", project, output=output, account_id=github_account_id, login=github_login)


def join_command(
    invite: Annotated[Path, typer.Option("--invite")],
    output: Annotated[Path, typer.Option("--output")],
    project: Annotated[Path, typer.Option("--project")] = Path("."),
) -> None:
    """Review an invite and authorize this device with local WebAuthn."""
    _begin("join", project, invite=invite, output=output)


def approve_join_command(
    response: Annotated[Path, typer.Option("--response")],
    project: Annotated[Path, typer.Option("--project")] = Path("."),
) -> None:
    """Review the exact member authority change and publication locally."""
    _begin("approve-join", project, response=response)
