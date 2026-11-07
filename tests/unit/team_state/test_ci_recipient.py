"""Independent runner keys remain local while reviewed public trust survives restart."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from typer.testing import CliRunner


class Backend:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, account: str) -> str | None:
        return self.values.get((service, account))

    def set_password(self, service: str, account: str, value: str) -> None:
        self.values[service, account] = value


def test_ci_cli_preview_never_mutates_and_confirm_only_exports_public_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Preview must not generate keys; confirmed restarts must not rotate them."""
    import keyring

    from intent_engineering.cli.team import team_app

    backend = Backend()
    monkeypatch.setattr(keyring, "get_password", backend.get_password)
    monkeypatch.setattr(keyring, "set_password", backend.set_password)
    args = [
        "ci",
        "provision",
        "--project-id",
        "project",
        "--repository",
        "acme/project",
        "--runner",
        "release-01",
    ]
    runner = CliRunner()
    preview = runner.invoke(team_app, args)
    assert preview.exit_code == 4, preview.output
    assert not backend.values
    digest = json.loads(preview.stdout)["preview_digest"]
    result = runner.invoke(team_app, [*args, "--confirm", digest])
    assert result.exit_code == 0, result.output
    descriptor = json.loads(result.stdout)
    assert descriptor["kind"] == "ci"
    assert "github_account_id" not in descriptor
    assert len(backend.values) == 1
    assert all(secret not in result.stdout for secret in backend.values.values())
    assert runner.invoke(team_app, [*args, "--confirm", digest]).stdout == result.stdout


def test_ci_trust_restart_requires_exact_key_and_protected_public_config(tmp_path: Path) -> None:
    """Foreign, missing, or checkout-writable trust must not become CI authority."""
    from intent_engineering.team_state.ci import CiKeyStore, CiTrustConfig, CiTrustProvider

    backend = Backend()
    store = CiKeyStore(
        "project",
        "github.com/acme/project",
        "release-01",
        backend=backend,
        lock_root=tmp_path / "locks",
    )
    recipient = store.provision()
    signer = Ed25519PrivateKey.generate().public_key().public_bytes_raw()
    config = CiTrustConfig(
        recipient=recipient,
        signing_public_keys={"signer:release": base64.b64encode(signer).decode()},
    )
    protected = tmp_path / "protected"
    protected.mkdir(mode=0o700)
    target = protected / "trust.json"
    target.write_bytes(config.canonical_bytes())
    target.chmod(0o600)
    trust = CiTrustProvider(target, backend=backend, lock_root=tmp_path / "locks").load()
    assert trust.recipient_key_id == recipient.key_id
    assert trust.repository_id == "github.com/acme/project"
    assert trust.signing_keys[0].public_key == signer
    assert all(secret.encode() not in target.read_bytes() for secret in backend.values.values())
    with pytest.raises(ValueError, match="CI key unavailable"):
        CiTrustProvider(
            tmp_path / "outside" / ".." / "protected" / "trust.json",
            backend=backend,
            lock_root=tmp_path / "locks",
            checkout_root=protected,
        ).load()
    target.chmod(0o666)
    with pytest.raises(ValueError, match="CI key unavailable"):
        CiTrustProvider(target, backend=backend, lock_root=tmp_path / "locks").load()
    target.chmod(0o600)
    backend.values.clear()
    with pytest.raises(ValueError, match="CI key unavailable"):
        CiTrustProvider(target, backend=backend, lock_root=tmp_path / "locks").load()


def test_production_ci_refuses_environment_secret_json_and_checkout_config(tmp_path: Path) -> None:
    """A workflow must never silently regain the hosted private-key JSON path."""
    from intent_engineering.team_state.ci import ci_trust_from_environment

    for environment in (
        {},
        {"INTENT_CI_SHARED_STATE_TRUST": "private-material"},
        {
            "INTENT_CI_TRUST_PATH": str(tmp_path / "trust.json"),
            "INTENT_CI_SHARED_STATE_TRUST": "private-material",
        },
    ):
        with pytest.raises(ValueError, match="CI key unavailable") as caught:
            ci_trust_from_environment(tmp_path, environment).load()
        assert "private-material" not in str(caught.value)
    with pytest.raises(ValueError, match="CI key unavailable"):
        ci_trust_from_environment(
            tmp_path, {"INTENT_CI_TRUST_PATH": str(tmp_path / "trust.json")}
        ).load()


def test_public_ci_descriptor_rejects_boolean_schema_version() -> None:
    """JSON true is not a supported integer schema version."""
    from intent_engineering.team_state.models import CiRecipientRecord

    with pytest.raises(ValueError):
        CiRecipientRecord.model_validate_json(
            json.dumps(
                {
                    "schema_version": True,
                    "project_id": "project",
                    "repository_id": "github.com/acme/project",
                    "runner_id": "release-01",
                    "public_key": base64.urlsafe_b64encode(b"i" * 32).rstrip(b"=").decode(),
                }
            )
        )


def test_ci_keyring_failure_scrubs_provider_exception_locals(tmp_path: Path) -> None:
    """Error reporters inspecting traceback locals must not receive provider secret text."""
    import traceback

    from intent_engineering.team_state.ci import CiKeyStore

    class Unavailable(Backend):
        def get_password(self, service: str, account: str) -> str | None:
            raise RuntimeError("provider-secret-canary")

    store = CiKeyStore(
        "project",
        "github.com/acme/project",
        "release-01",
        backend=Unavailable(),
        lock_root=tmp_path / "locks",
    )
    with pytest.raises(ValueError) as caught:
        store.provision()
    assert "provider-secret-canary" not in str(caught.value)
    for frame, _ in traceback.walk_tb(caught.value.__traceback__):
        if frame.f_globals.get("__name__") == "intent_engineering.team_state.ci":
            assert not any(
                "provider-secret-canary" in str(value)
                for value in frame.f_locals.values()
                if isinstance(value, BaseException)
            )
