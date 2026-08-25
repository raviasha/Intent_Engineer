from __future__ import annotations

import importlib
import subprocess
from collections.abc import Mapping
from types import ModuleType
from typing import Any

import pytest
import structlog
from pydantic import SecretStr, ValidationError
from structlog.testing import capture_logs


def _auth() -> ModuleType:
    return importlib.import_module("intent_engineering.capture.github.auth")


def _secret() -> str:
    # Build the sentinel at runtime so no token-shaped value is persisted in this test file.
    return "gh" + "p_" + "runtime-only-credential"


def _assert_redacted(secret: str, *values: object) -> None:
    assert all(secret not in repr(value) and secret not in str(value) for value in values), (
        "credential material leaked"
    )


def test_environment_token_wins_without_invoking_runner() -> None:
    auth = _auth()
    secret = _secret()
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> str:
        calls.append(argv)
        return "unused"

    credentials = auth.GitHubCredentials.resolve({"GH_TOKEN": secret}, runner)

    assert credentials.source is auth.CredentialSource.ENVIRONMENT
    assert credentials.token.get_secret_value() == secret
    assert calls == []


@pytest.mark.parametrize("environment", [{}, {"GH_TOKEN": ""}, {"GH_TOKEN": " \t\n"}])
def test_github_cli_token_is_used_only_when_environment_is_absent_or_blank(
    environment: Mapping[str, str],
) -> None:
    auth = _auth()
    secret = _secret()
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> str:
        calls.append(argv)
        return f" \n{secret}\t "

    credentials = auth.GitHubCredentials.resolve(environment, runner)

    assert credentials.source is auth.CredentialSource.GITHUB_CLI
    assert credentials.token.get_secret_value() == secret
    assert calls == [["gh", "auth", "token"]]


def test_production_runner_uses_exact_shell_free_subprocess_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth = _auth()
    secret = _secret()
    calls: list[tuple[object, dict[str, object]]] = []

    def fake_run(argv: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(["gh", "auth", "token"], 0, f" {secret}\n", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert auth.run_gh_token(["gh", "auth", "token"]) == secret
    assert calls == [
        (
            ["gh", "auth", "token"],
            {"check": True, "capture_output": True, "text": True, "shell": False},
        )
    ]


@pytest.mark.parametrize("failure", ["missing", "nonzero", "blank"])
def test_cli_failures_raise_the_same_redacted_actionable_error(failure: str) -> None:
    auth = _auth()
    secret = _secret()

    def runner(argv: list[str]) -> str:
        assert argv == ["gh", "auth", "token"]
        if failure == "missing":
            raise FileNotFoundError(secret)
        if failure == "nonzero":
            raise subprocess.CalledProcessError(1, argv, output=secret, stderr=secret)
        return " \t\n"

    with pytest.raises(auth.GitHubAuthError) as caught:
        auth.GitHubCredentials.resolve({}, runner)

    error = caught.value
    expected = "GitHub authentication unavailable; set GH_TOKEN or run `gh auth login`."
    assert str(error) == expected
    assert error.args == (expected,)
    assert error.__cause__ is None
    assert error.__context__ is None
    _assert_redacted(secret, error, error.args)


def test_credentials_and_public_errors_do_not_expose_secret_in_serialization_or_logs() -> None:
    auth = _auth()
    secret = _secret()
    credentials = auth.GitHubCredentials.resolve({"GH_TOKEN": f" {secret} "}, lambda _: "")
    error = auth.GitHubAuthError()

    serialized = (
        repr(credentials),
        str(credentials),
        credentials.model_dump(),
        credentials.model_dump_json(),
        repr(error),
        str(error),
        error.args,
    )
    _assert_redacted(secret, *serialized)

    with capture_logs() as events:
        structlog.get_logger().info("github_auth", credentials=credentials, error=error)

    _assert_redacted(secret, events)


@pytest.mark.parametrize("raw", [" ", "\t\n"])
def test_whitespace_never_becomes_an_accepted_token(raw: str) -> None:
    auth = _auth()

    with pytest.raises(auth.GitHubAuthError):
        auth.GitHubCredentials.resolve({"GH_TOKEN": raw}, lambda _: " \n\t ")


class _ExplodingEnvironment(Mapping[str, object]):
    def __getitem__(self, key: str) -> object:
        raise KeyError(key)

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(())

    def __len__(self) -> int:
        return 0

    def get(self, key: str, default: object = None) -> object:
        raise ValueError(_secret())


@pytest.mark.parametrize("environment", [{"GH_TOKEN": 42}, _ExplodingEnvironment()])
def test_malformed_environment_input_raises_only_the_redacted_auth_error(
    environment: Mapping[str, Any],
) -> None:
    auth = _auth()
    secret = _secret()

    with pytest.raises(auth.GitHubAuthError) as caught:
        auth.GitHubCredentials.resolve(environment, lambda _: secret)

    assert caught.value.__context__ is None
    _assert_redacted(secret, caught.value, caught.value.args)


def test_credentials_reject_unknown_fields_and_invalid_sources() -> None:
    auth = _auth()
    secret = _secret()
    protected = SecretStr(secret)

    with pytest.raises(ValidationError):
        auth.GitHubCredentials(token=protected, source="environment", unexpected=True)
    with pytest.raises(ValidationError):
        auth.GitHubCredentials(token=protected, source="config_file")
