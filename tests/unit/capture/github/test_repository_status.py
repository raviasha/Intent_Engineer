"""Bounded, secret-safe GitHub repository diagnostics."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from intent_engineering.capture.github.auth import GitHubCredentials
from intent_engineering.capture.github.client import GitHubClient, RetryPolicy
from intent_engineering.capture.github.errors import GitHubPermissionError, GitHubProtocolError
from intent_engineering.capture.github.models import GitHubRepositoryStatus

TOKEN = "gh" + "p_doctor-secret-fragment"


def _repository_traceback_locals(error: BaseException) -> str:
    values: list[str] = []
    traceback = error.__traceback__
    while traceback is not None:
        if "/src/intent_engineering/" in traceback.tb_frame.f_code.co_filename:
            values.append(repr(traceback.tb_frame.f_locals))
        traceback = traceback.tb_next
    return "".join(values)


def _client(handler: object) -> GitHubClient:
    credentials = GitHubCredentials.resolve({"GH_TOKEN": TOKEN}, lambda _: "unused")
    return GitHubClient(
        credentials,
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
        retry_policy=RetryPolicy(max_attempts=1),
    )


@pytest.mark.anyio
async def test_repository_status_returns_only_validated_bounded_scalars() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"full_name": "acme/demo", "private_provider_field": TOKEN},
            headers={
                "X-RateLimit-Limit": "5000",
                "X-RateLimit-Remaining": "4999",
                "X-RateLimit-Used": "1",
                "X-RateLimit-Reset": "1787659200",
                "X-RateLimit-Resource": "core",
            },
        )

    client = _client(handler)
    result = await client.get_repository_status("acme/demo")
    await client.aclose()

    assert len(requests) == 1
    assert result == GitHubRepositoryStatus(
        repository="acme/demo",
        accessible=True,
        rate_limit=5000,
        rate_remaining=4999,
        rate_used=1,
        rate_reset_at=datetime(2026, 8, 25, 12, tzinfo=UTC),
        rate_resource="core",
    )
    assert TOKEN not in result.model_dump_json()


@pytest.mark.anyio
async def test_repository_status_permission_error_keeps_reviewed_identity_and_redaction() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            content=("PRIVATE-" + TOKEN).encode(),
            headers={"X-GitHub-Request-Id": f"prefix-{TOKEN}-suffix"},
        )

    client = _client(handler)
    with pytest.raises(GitHubPermissionError) as caught:
        await client.get_repository_status("acme/demo")
    await client.aclose()

    rendered = _repository_traceback_locals(caught.value)
    assert TOKEN not in str(caught.value)
    assert TOKEN not in rendered
    assert "PRIVATE" not in rendered
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("body", "headers"),
    [
        ({"full_name": "other/repository"}, {}),
        ({"full_name": "acme/demo"}, {"X-RateLimit-Limit": TOKEN}),
        ({"full_name": "acme/demo"}, {"X-RateLimit-Limit": "5000"}),
    ],
)
async def test_repository_status_malformed_input_is_fixed_and_forgets_rejected_values(
    body: dict[str, str],
    headers: dict[str, str],
) -> None:
    complete_headers = {
        "X-RateLimit-Limit": "5000",
        "X-RateLimit-Remaining": "4999",
        "X-RateLimit-Used": "1",
        "X-RateLimit-Reset": "1787659200",
        "X-RateLimit-Resource": "core",
        **headers,
    }
    if headers == {"X-RateLimit-Limit": "5000"}:
        complete_headers.pop("X-RateLimit-Remaining")

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={**body, "provider_secret": TOKEN}, headers=complete_headers
        )

    client = _client(handler)
    with pytest.raises(GitHubProtocolError) as caught:
        await client.get_repository_status("acme/demo")
    await client.aclose()

    rendered = _repository_traceback_locals(caught.value)
    assert str(caught.value) == ("GitHub response protocol error (endpoint /repos/acme/demo).")
    assert TOKEN not in rendered
    assert "provider_secret" not in rendered
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
