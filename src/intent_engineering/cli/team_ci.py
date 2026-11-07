"""Preview-first independent CI key provisioning on a trusted runner account."""

from __future__ import annotations

import json
from typing import Annotated

import typer

from intent_engineering.cli.runtime import github_repository_scope
from intent_engineering.team_state.ci import CiKeyStore, CiTrustError, provision_preview

ci_app = typer.Typer(help="Provision a dedicated self-hosted CI encryption recipient.")


@ci_app.command("provision")
def provision_command(
    project_id: Annotated[str, typer.Option("--project-id")],
    repository: Annotated[str, typer.Option("--repository")],
    runner: Annotated[str, typer.Option("--runner")],
    confirm: Annotated[str | None, typer.Option("--confirm")] = None,
) -> None:
    """Run as the CI service account; output contains only a public descriptor."""
    try:
        scope = "github.com/" + github_repository_scope({"GITHUB_REPOSITORY": repository})
        preview = provision_preview(project_id, scope, runner)
    except Exception:  # noqa: BLE001 - fixed public configuration boundary
        typer.echo(f"intent error: {CiTrustError()}", err=True)
        raise typer.Exit(1) from None
    if confirm is None:
        typer.echo(json.dumps(preview, sort_keys=True, separators=(",", ":")))
        raise typer.Exit(4)
    if confirm != preview["preview_digest"]:
        typer.echo("intent error: CI provisioning preview changed", err=True)
        raise typer.Exit(1)
    try:
        descriptor = CiKeyStore(project_id, scope, runner).provision()
    except Exception:  # noqa: BLE001 - never print keyring provider details
        typer.echo(f"intent error: {CiTrustError()}", err=True)
        raise typer.Exit(1) from None
    typer.echo(descriptor.model_dump_json())
