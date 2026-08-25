from __future__ import annotations

import json
import logging
import warnings
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import format_datetime
from types import MappingProxyType

import httpx
import pytest
from pydantic import ValidationError
from structlog.testing import capture_logs

from intent_engineering.capture.github.auth import GitHubCredentials
from intent_engineering.capture.github.client import GitHubClient, RetryPolicy
from intent_engineering.capture.github.errors import (
    GitHubApiError,
    GitHubNotFound,
    GitHubPermissionError,
    GitHubProtocolError,
    GitHubRateLimitError,
    GitHubTransientError,
)
from intent_engineering.capture.github.models import (
    GitHubCommit,
    GitHubIssue,
    GitHubIssueComment,
    GitHubPullRequest,
    GitHubReviewComment,
    GitHubUser,
    PageResult,
)

TransportFactory = Callable[[Callable[[httpx.Request], httpx.Response]], httpx.MockTransport]


def _client(
    credentials: GitHubCredentials,
    handler: Callable[[httpx.Request], httpx.Response],
    transport_factory: TransportFactory,
    **kwargs: object,
) -> GitHubClient:
    return GitHubClient(
        credentials,
        transport=transport_factory(handler),
        **kwargs,
    )


@pytest.mark.anyio
async def test_get_pages_follows_next_links_once_in_stable_order_and_sends_params_first_page_only(
    github_credentials: GitHubCredentials,
    transport_factory: TransportFactory,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                200,
                json=[{"id": 1, "number": 10}, {"id": 2, "number": 11}],
                headers={
                    "Link": '<https://api.github.com/repositories/1/issues?page=2>; rel="next"'
                },
            )
        return httpx.Response(200, json=[{"id": 3, "number": 12}])

    client = _client(github_credentials, handler, transport_factory)
    result = await client.get_pages("/repos/acme/demo/issues", {"state": "all", "per_page": "100"})
    await client.aclose()

    assert [item["id"] for item in result.items] == [1, 2, 3]
    assert requests[0].url == httpx.URL(
        "https://api.github.com/repos/acme/demo/issues?state=all&per_page=100"
    )
    assert requests[1].url == httpx.URL("https://api.github.com/repositories/1/issues?page=2")


@pytest.mark.anyio
async def test_uppercase_registered_next_relation_is_followed(
    github_credentials: GitHubCredentials,
    transport_factory: TransportFactory,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                200,
                json=[{"id": 1}],
                headers={"Link": '<https://api.github.com/items?page=2>; rel="NEXT"'},
            )
        return httpx.Response(200, json=[{"id": 2}])

    client = _client(github_credentials, handler, transport_factory)
    result = await client.get_pages("/items", {})
    await client.aclose()

    assert [item["id"] for item in result.items] == [1, 2]
    assert len(requests) == 2


@pytest.mark.anyio
async def test_first_page_304_sends_etag_and_returns_exact_not_modified_result(
    github_credentials: GitHubCredentials,
    transport_factory: TransportFactory,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(304, headers={"ETag": '"server-value"'})

    client = _client(github_credentials, handler, transport_factory)
    result = await client.get_pages("/repos/acme/demo/issues", {}, etag='"cached-v1"')
    await client.aclose()

    assert requests[0].headers["If-None-Match"] == '"cached-v1"'
    assert result == PageResult(items=(), etag='"cached-v1"', not_modified=True)


@pytest.mark.anyio
async def test_first_response_etag_is_retained_and_conditional_header_is_not_forwarded(
    github_credentials: GitHubCredentials,
    transport_factory: TransportFactory,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                200,
                json=[{"id": 1}],
                headers={
                    "ETag": '"fresh"',
                    "Link": '<https://api.github.com/items?page=2>; rel="next"',
                },
            )
        return httpx.Response(200, json=[{"id": 2}], headers={"ETag": '"later"'})

    client = _client(github_credentials, handler, transport_factory)
    result = await client.get_pages("/items", {}, etag='"cached"')
    await client.aclose()

    assert result.etag == '"fresh"'
    assert requests[0].headers["If-None-Match"] == '"cached"'
    assert "If-None-Match" not in requests[1].headers


@pytest.mark.anyio
@pytest.mark.parametrize(
    "unsafe_url",
    [
        "https://evil.example/items?page=2",
        "http://api.github.com/items?page=2",
        "https://user:pass@api.github.com/items?page=2",
        "not a valid absolute URL",
    ],
)
async def test_unsafe_next_link_is_rejected_before_a_second_request(
    github_credentials: GitHubCredentials,
    transport_factory: TransportFactory,
    unsafe_url: str,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=[{"id": 1}],
            headers={"Link": f'<{unsafe_url}>; rel="next"'},
        )

    client = _client(github_credentials, handler, transport_factory)
    with pytest.raises(GitHubProtocolError) as caught:
        await client.get_pages("/items", {})
    await client.aclose()

    assert len(requests) == 1
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert unsafe_url not in str(caught.value)


@pytest.mark.anyio
async def test_pagination_cycle_is_rejected_before_repeating_a_request(
    github_credentials: GitHubCredentials,
    transport_factory: TransportFactory,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=[{"id": len(requests)}],
            headers={"Link": '<https://api.github.com/items>; rel="next"'},
        )

    client = _client(github_credentials, handler, transport_factory)
    with pytest.raises(GitHubProtocolError):
        await client.get_pages("/items", {})
    await client.aclose()

    assert len(requests) == 1


@pytest.mark.anyio
async def test_pagination_link_with_fragment_is_rejected_before_second_request(
    github_credentials: GitHubCredentials,
    transport_factory: TransportFactory,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=[{"id": 1}],
            headers={"Link": '<https://api.github.com/items?page=2#cycle-bypass>; rel="next"'},
        )

    client = _client(github_credentials, handler, transport_factory)
    with pytest.raises(GitHubProtocolError):
        await client.get_pages("/items", {})
    await client.aclose()

    assert len(requests) == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    "malformed_link",
    [
        "garbage",
        '<https://api.github.com/items?page=2; rel="next"',
        "<https://api.github.com/items?page=2>; rel",
        '<>; rel="next"',
        '<https://api.github.com/items?page=2>; rel="next", broken',
        '<https://api.github.com/items<bad?page=2>; rel="next"',
        '<https://api.github.com/items?page=2>; rel="next"; rel="last"',
    ],
)
async def test_structurally_malformed_link_header_fails_closed(
    github_credentials: GitHubCredentials,
    transport_factory: TransportFactory,
    malformed_link: str,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[{"id": 1}], headers={"Link": malformed_link})

    client = _client(github_credentials, handler, transport_factory)
    with pytest.raises(GitHubProtocolError):
        await client.get_pages("/items", {})
    await client.aclose()

    assert len(requests) == 1


@pytest.mark.anyio
async def test_pagination_page_cap_is_enforced_before_an_extra_request(
    github_credentials: GitHubCredentials,
    transport_factory: TransportFactory,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        page = len(requests)
        return httpx.Response(
            200,
            json=[{"id": page}],
            headers={"Link": f'<https://api.github.com/items?page={page + 1}>; rel="next"'},
        )

    client = _client(github_credentials, handler, transport_factory, page_cap=2)
    with pytest.raises(GitHubProtocolError):
        await client.get_pages("/items", {})
    await client.aclose()

    assert len(requests) == 2


@pytest.mark.anyio
@pytest.mark.parametrize(
    "response_kwargs",
    [
        {"json": {"id": 1}},
        {"json": [1]},
        {"content": b"not-json", "headers": {"Content-Type": "application/json"}},
    ],
)
async def test_invalid_page_shape_is_rejected_without_response_body_leakage(
    github_credentials: GitHubCredentials,
    transport_factory: TransportFactory,
    response_kwargs: dict[str, object],
) -> None:
    body_sentinel = "not-json"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, **response_kwargs)

    client = _client(github_credentials, handler, transport_factory)
    with pytest.raises(GitHubProtocolError) as caught:
        await client.get_pages("/items", {})
    await client.aclose()

    assert body_sentinel not in str(caught.value)
    assert body_sentinel not in repr(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.anyio
async def test_page_results_are_deeply_immutable(
    github_credentials: GitHubCredentials,
    transport_factory: TransportFactory,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"id": 1, "nested": {"labels": ["bug"]}}])

    client = _client(github_credentials, handler, transport_factory)
    result = await client.get_pages("/items", {})
    await client.aclose()

    with pytest.raises(TypeError):
        result.items[0]["id"] = 2  # type: ignore[index]
    with pytest.raises(TypeError):
        result.items[0]["nested"]["labels"] = ()  # type: ignore[index, union-attr]
    assert result.items[0]["nested"]["labels"] == ("bug",)  # type: ignore[index]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status", "headers", "error_type"),
    [
        (401, {}, GitHubPermissionError),
        (403, {}, GitHubPermissionError),
        (404, {}, GitHubNotFound),
        (418, {}, GitHubApiError),
    ],
)
async def test_non_retryable_statuses_map_once_to_fixed_provider_errors(
    github_credentials: GitHubCredentials,
    transport_factory: TransportFactory,
    status: int,
    headers: dict[str, str],
    error_type: type[GitHubApiError],
) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            status,
            content=b"server-controlled-body",
            headers={"X-GitHub-Request-Id": "SAFE_123", **headers},
        )

    client = _client(github_credentials, handler, transport_factory)
    with pytest.raises(error_type) as caught:
        await client.get_pages("/repos/acme/demo/issues?private=hidden", {})
    await client.aclose()

    assert attempts == 1
    assert caught.value.status_code == status
    assert caught.value.endpoint == "/repos/acme/demo/issues"
    assert caught.value.request_id == "SAFE_123"
    assert "server-controlled-body" not in repr(caught.value)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status", "headers"),
    [
        (403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1787644800"}),
        (403, {"Retry-After": "60"}),
        (429, {}),
    ],
)
async def test_rate_limit_statuses_map_without_retry(
    github_credentials: GitHubCredentials,
    transport_factory: TransportFactory,
    status: int,
    headers: dict[str, str],
) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(status, headers=headers)

    now = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
    client = _client(github_credentials, handler, transport_factory, clock=lambda: now)
    with pytest.raises(GitHubRateLimitError):
        await client.get_pages("/items", {})
    await client.aclose()

    assert attempts == 1


@pytest.mark.anyio
async def test_rate_limit_retry_at_parses_epoch_seconds_delta_and_http_date(
    github_credentials: GitHubCredentials,
    transport_factory: TransportFactory,
) -> None:
    now = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
    cases = (
        (
            {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1787659200"},
            datetime.fromtimestamp(1787659200, tz=UTC),
        ),
        ({"Retry-After": "90"}, datetime(2026, 8, 25, 12, 1, 30, tzinfo=UTC)),
        (
            {"Retry-After": format_datetime(datetime(2026, 8, 25, 13, 0, tzinfo=UTC))},
            datetime(2026, 8, 25, 13, 0, tzinfo=UTC),
        ),
    )

    for headers, expected in cases:

        def handler(request: httpx.Request, headers: dict[str, str] = headers) -> httpx.Response:
            return httpx.Response(429, headers=headers)

        client = _client(github_credentials, handler, transport_factory, clock=lambda: now)
        with pytest.raises(GitHubRateLimitError) as caught:
            await client.get_pages("/items", {})
        await client.aclose()
        assert caught.value.retry_at == expected


@pytest.mark.anyio
@pytest.mark.parametrize(
    "headers",
    [
        {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "not-an-epoch"},
        {"Retry-After": "not-a-date"},
        {"Retry-After": "-5"},
    ],
)
async def test_malformed_rate_headers_yield_no_retry_time_or_parse_context(
    github_credentials: GitHubCredentials,
    transport_factory: TransportFactory,
    headers: dict[str, str],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, content=b"private-rate-body", headers=headers)

    client = _client(github_credentials, handler, transport_factory)
    with pytest.raises(GitHubRateLimitError) as caught:
        await client.get_pages("/items", {})
    await client.aclose()

    assert caught.value.retry_at is None
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "private-rate-body" not in repr(caught.value)


@pytest.mark.anyio
async def test_5xx_retries_with_exact_bounded_backoff_then_succeeds(
    github_credentials: GitHubCredentials,
    transport_factory: TransportFactory,
) -> None:
    attempts = 0
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(503, content=b"do-not-retain")
        return httpx.Response(200, json=[{"id": 1}])

    async def sleeper(delay: float) -> None:
        delays.append(delay)

    client = _client(
        github_credentials,
        handler,
        transport_factory,
        sleeper=sleeper,
        retry_policy=RetryPolicy(max_attempts=3, base_delay=0.25, max_delay=0.4),
    )
    result = await client.get_pages("/items", {})
    await client.aclose()

    assert [item["id"] for item in result.items] == [1]
    assert attempts == 3
    assert delays == [0.25, 0.4]


@pytest.mark.anyio
async def test_transport_failures_exhaust_to_detached_redacted_transient_error(
    github_credentials: GitHubCredentials,
    github_secret: str,
    transport_factory: TransportFactory,
) -> None:
    attempts = 0
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError(f"transport retained {github_secret}", request=request)

    async def sleeper(delay: float) -> None:
        delays.append(delay)

    client = _client(
        github_credentials,
        handler,
        transport_factory,
        sleeper=sleeper,
        retry_policy=RetryPolicy(max_attempts=2, base_delay=1.0, max_delay=1.0),
    )
    with pytest.raises(GitHubTransientError) as caught:
        await client.get_pages("/items", {})
    await client.aclose()

    error = caught.value
    assert attempts == 2
    assert delays == [1.0]
    assert error.attempt_count == 2
    assert error.__cause__ is None
    assert error.__context__ is None
    assert github_secret not in repr(error)
    assert not hasattr(error, "request")
    assert not hasattr(error, "response")


@pytest.mark.anyio
async def test_exhausted_5xx_retains_only_sanitized_metadata(
    github_credentials: GitHubCredentials,
    transport_factory: TransportFactory,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            content=b"private response",
            headers={"X-GitHub-Request-Id": "bad id\nsecret"},
        )

    async def sleeper(delay: float) -> None:
        return None

    client = _client(
        github_credentials,
        handler,
        transport_factory,
        sleeper=sleeper,
        retry_policy=RetryPolicy(max_attempts=2),
    )
    with pytest.raises(GitHubTransientError) as caught:
        await client.get_pages("/items", {})
    await client.aclose()

    error = caught.value
    assert error.status_code == 500
    assert error.attempt_count == 2
    assert error.request_id == "bad_id_secret"
    assert "private response" not in repr(error)


@pytest.mark.anyio
async def test_fixed_headers_base_timeout_and_redirect_policy_are_explicit(
    github_credentials: GitHubCredentials,
    github_secret: str,
    transport_factory: TransportFactory,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[])

    client = _client(github_credentials, handler, transport_factory)
    await client.get_pages("/items", {})

    assert requests[0].url == httpx.URL("https://api.github.com/items")
    assert requests[0].headers["Authorization"] == f"Bearer {github_secret}"
    assert requests[0].headers["Accept"] == "application/vnd.github+json"
    assert requests[0].headers["User-Agent"] == "intent-engineering/0.1.0"
    assert client.base_url == httpx.URL("https://api.github.com")
    assert client.timeout == httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=5.0)
    assert client.follow_redirects is False
    assert github_secret not in repr(requests[0])
    assert github_secret not in repr(client)
    await client.aclose()


@pytest.mark.anyio
async def test_owned_client_closes_but_injected_client_remains_open(
    github_credentials: GitHubCredentials,
    transport_factory: TransportFactory,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    owned = _client(github_credentials, handler, transport_factory)
    async with owned:
        assert owned.is_closed is False
    assert owned.is_closed is True

    injected = httpx.AsyncClient(
        base_url="https://api.github.com",
        transport=transport_factory(handler),
    )
    wrapper = GitHubClient(github_credentials, client=injected)
    await wrapper.aclose()

    assert injected.is_closed is False
    response = await injected.get("/still-open")
    assert response.status_code == 200
    await injected.aclose()


@pytest.mark.anyio
async def test_injected_client_headers_are_never_mutated_or_forwarded_after_wrapper_close(
    github_credentials: GitHubCredentials,
    github_secret: str,
    transport_factory: TransportFactory,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[])

    injected = httpx.AsyncClient(
        headers={"X-Caller": "preserved"},
        transport=transport_factory(handler),
    )
    original_headers = tuple(injected.headers.multi_items())
    wrapper = GitHubClient(github_credentials, client=injected)

    await wrapper.get_pages("/items", {})
    await wrapper.aclose()
    await injected.get("https://uploads.github.com/post-wrapper")

    assert requests[0].headers["Authorization"] == f"Bearer {github_secret}"
    assert "Authorization" not in requests[1].headers
    assert tuple(injected.headers.multi_items()) == original_headers
    assert injected.headers["X-Caller"] == "preserved"
    await injected.aclose()


@pytest.mark.anyio
async def test_injected_client_defaults_cannot_change_wrapper_requests(
    github_credentials: GitHubCredentials,
    github_secret: str,
    transport_factory: TransportFactory,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                200,
                json=[{"id": 1}],
                headers={"Link": '<https://api.github.com/items?page=2>; rel="next"'},
            )
        return httpx.Response(200, json=[{"id": 2}])

    injected = httpx.AsyncClient(
        auth=httpx.BasicAuth("caller", "caller-secret"),
        headers={
            "Accept": "text/plain",
            "Authorization": "Bearer caller-token",
            "If-None-Match": '"caller-etag"',
            "User-Agent": "caller-agent",
            "X-Caller": "preserved",
        },
        params={"caller": "leak", "page": "999"},
        timeout=httpx.Timeout(99.0),
        transport=transport_factory(handler),
    )
    original_headers = tuple(injected.headers.multi_items())
    original_params = tuple(injected.params.multi_items())
    original_timeout = injected.timeout
    wrapper = GitHubClient(github_credentials, client=injected)

    result = await wrapper.get_pages("/items", {"state": "all"}, etag='"wrapper-etag"')
    await wrapper.aclose()
    await injected.get("https://uploads.github.com/post-wrapper")

    assert [item["id"] for item in result.items] == [1, 2]
    assert requests[0].url == httpx.URL("https://api.github.com/items?state=all")
    assert requests[1].url == httpx.URL("https://api.github.com/items?page=2")
    assert requests[0].headers["Authorization"] == f"Bearer {github_secret}"
    assert requests[0].headers["Accept"] == "application/vnd.github+json"
    assert requests[0].headers["User-Agent"] == "intent-engineering/0.1.0"
    assert requests[0].headers["If-None-Match"] == '"wrapper-etag"'
    assert "If-None-Match" not in requests[1].headers
    assert requests[0].extensions["timeout"] == {
        "connect": 5.0,
        "read": 30.0,
        "write": 30.0,
        "pool": 5.0,
    }
    assert requests[1].extensions["timeout"] == requests[0].extensions["timeout"]
    assert requests[2].url.host == "uploads.github.com"
    assert requests[2].url.path == "/post-wrapper"
    assert requests[2].headers["Authorization"] != f"Bearer {github_secret}"
    assert tuple(injected.headers.multi_items()) == original_headers
    assert tuple(injected.params.multi_items()) == original_params
    assert injected.timeout == original_timeout
    await injected.aclose()


@pytest.mark.anyio
async def test_errors_requests_pytest_rendering_and_logs_never_expose_token(
    github_credentials: GitHubCredentials,
    github_secret: str,
    transport_factory: TransportFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    seen_request: httpx.Request | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_request
        seen_request = request
        return httpx.Response(
            401,
            content=github_secret.encode(),
            headers={"X-GitHub-Request-Id": f"SAFE:{github_secret}"},
        )

    client = _client(github_credentials, handler, transport_factory)
    caplog.set_level(logging.DEBUG)
    with capture_logs() as events, pytest.raises(GitHubPermissionError) as caught:
        await client.get_pages("/items", {})
    await client.aclose()

    assert seen_request is not None
    public_values = (
        repr(seen_request),
        repr(client),
        str(caught.value),
        repr(caught.value),
        caught.value.args,
        caught.getrepr(style="short"),
        caplog.text,
        events,
    )
    assert all(github_secret not in str(value) for value in public_values)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.anyio
@pytest.mark.parametrize("reflected_length", [94, 11])
async def test_long_token_prefix_reflected_in_request_id_is_fully_discarded(
    transport_factory: TransportFactory,
    reflected_length: int,
) -> None:
    token = "github" + "_pat_" + ("A" * 82)
    credentials = GitHubCredentials.resolve({"GH_TOKEN": token}, lambda _: "unused")

    def handler(request: httpx.Request) -> httpx.Response:
        reflected = token[:reflected_length]
        return httpx.Response(401, headers={"X-GitHub-Request-Id": f"RID:{reflected}:suffix"})

    client = _client(credentials, handler, transport_factory)
    with pytest.raises(GitHubPermissionError) as caught:
        await client.get_pages("/items", {})
    await client.aclose()

    assert caught.value.request_id is None
    assert "github_pat_" not in str(caught.value)
    assert token[:64] not in repr(caught.value)


@pytest.mark.anyio
async def test_long_token_fragment_near_endpoint_cutoff_is_fully_redacted(
    transport_factory: TransportFactory,
) -> None:
    token = "github" + "_pat_" + ("B" * 82)
    credentials = GitHubCredentials.resolve({"GH_TOKEN": token}, lambda _: "unused")
    path = f"/{'x' * 142}/prefix/{token}/private"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    client = _client(credentials, handler, transport_factory)
    with pytest.raises(GitHubPermissionError) as caught:
        await client.get_pages(path, {})
    await client.aclose()

    assert "github_pa" not in caught.value.endpoint
    assert "github_pa" not in str(caught.value)
    assert token[:16] not in repr(caught.value)
    assert len(caught.value.endpoint) <= 160


def _user_payload() -> dict[str, object]:
    return {"id": 1, "login": "octocat", "html_url": "https://github.com/octocat"}


def _common_payload() -> dict[str, object]:
    return {
        "id": 10,
        "number": 42,
        "title": "Preserve provenance",
        "body": "Evidence-backed",
        "state": "open",
        "user": _user_payload(),
        "labels": ({"name": "intent"},),
        "milestone": {"title": "alpha"},
        "updated_at": datetime(2026, 8, 25, 10, 0, tzinfo=UTC),
        "html_url": "https://github.com/acme/demo/issues/42",
    }


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (GitHubUser, _user_payload()),
        (GitHubIssue, _common_payload()),
        (
            GitHubPullRequest,
            {
                **_common_payload(),
                "base": {"ref": "main", "sha": "a" * 40},
                "head": {"ref": "feature", "sha": "b" * 40},
                "merge_commit_sha": None,
            },
        ),
        (
            GitHubCommit,
            {
                "sha": "c" * 40,
                "html_url": "https://github.com/acme/demo/commit/" + "c" * 40,
                "commit": {
                    "message": "Implement REST client",
                    "author": {
                        "name": "Octo Cat",
                        "date": datetime(2026, 8, 25, 10, 0, tzinfo=UTC),
                    },
                },
                "author": _user_payload(),
            },
        ),
        (
            GitHubIssueComment,
            {
                "id": 20,
                "body": "Looks good",
                "user": _user_payload(),
                "updated_at": datetime(2026, 8, 25, 10, 0, tzinfo=UTC),
                "html_url": "https://github.com/acme/demo/issues/42#issuecomment-20",
                "issue_url": "https://api.github.com/repos/acme/demo/issues/42",
            },
        ),
        (
            GitHubReviewComment,
            {
                "id": 30,
                "body": "Please test this",
                "user": _user_payload(),
                "updated_at": datetime(2026, 8, 25, 10, 0, tzinfo=UTC),
                "html_url": "https://github.com/acme/demo/pull/7#discussion_r30",
                "pull_request_url": "https://api.github.com/repos/acme/demo/pulls/7",
                "path": "src/client.py",
                "line": 10,
            },
        ),
    ],
)
def test_provider_models_accept_strict_complete_payloads(
    model: type[object], payload: object
) -> None:
    parsed = model.model_validate(payload)  # type: ignore[attr-defined]
    assert parsed is not None


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (GitHubIssue, {**_common_payload(), "user": None}),
        (
            GitHubIssueComment,
            {
                "id": 20,
                "body": "Deleted author",
                "user": None,
                "updated_at": datetime(2026, 8, 25, 10, 0, tzinfo=UTC),
                "html_url": "https://github.com/acme/demo/issues/42#issuecomment-20",
                "issue_url": "https://api.github.com/repos/acme/demo/issues/42",
            },
        ),
        (
            GitHubReviewComment,
            {
                "id": 30,
                "body": "Deleted reviewer",
                "user": None,
                "updated_at": datetime(2026, 8, 25, 10, 0, tzinfo=UTC),
                "html_url": "https://github.com/acme/demo/pull/7#discussion_r30",
                "pull_request_url": "https://api.github.com/repos/acme/demo/pulls/7",
                "path": "src/client.py",
                "line": 10,
            },
        ),
    ],
)
def test_provider_models_accept_null_deleted_actors(model: type[object], payload: object) -> None:
    parsed = model.model_validate(payload)  # type: ignore[attr-defined]
    assert parsed.user is None  # type: ignore[attr-defined]


@pytest.mark.parametrize("outer_author", [_user_payload(), None])
def test_commit_model_accepts_null_embedded_author(outer_author: object) -> None:
    commit = GitHubCommit.model_validate(
        {
            "sha": "d" * 40,
            "html_url": "https://github.com/acme/demo/commit/" + "d" * 40,
            "commit": {
                "message": "Commit from a deleted or unlinked author",
                "author": None,
            },
            "author": outer_author,
        }
    )

    assert commit.commit.author is None
    if outer_author is None:
        assert commit.author is None
    else:
        assert commit.author is not None


def test_provider_models_preserve_only_explicit_deeply_immutable_extra() -> None:
    user = GitHubUser.model_validate(
        {
            **_user_payload(),
            "extra": {"site_admin": False, "nested": {"roles": ["reader"]}},
        }
    )

    assert isinstance(user.extra, MappingProxyType)
    assert user.extra["nested"]["roles"] == ("reader",)  # type: ignore[index]
    with pytest.raises(TypeError):
        user.extra["site_admin"] = True  # type: ignore[index]
    with pytest.raises(TypeError):
        user.extra["nested"]["roles"] = ()  # type: ignore[index, union-attr]


def test_immutable_provider_values_serialize_without_warnings() -> None:
    user = GitHubUser.model_validate(
        {
            **_user_payload(),
            "extra": {"nested": {"roles": ["reader"]}},
        }
    )
    result = PageResult(items=({"id": 1, "labels": ["intent"]},), etag='"v1"')

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        serialized_user = json.loads(user.model_dump_json())
        serialized_result = json.loads(result.model_dump_json())

    assert serialized_user["extra"] == {"nested": {"roles": ["reader"]}}
    assert serialized_result["items"] == [{"id": 1, "labels": ["intent"]}]


@pytest.mark.parametrize(
    "invalid_extra",
    [
        {"mutable": {"reader"}},
        {"mutable": bytearray(b"reader")},
        {"arbitrary": object()},
        {"number": float("nan")},
        {"number": float("inf")},
        {1: "non-string-key"},
        {"nested": [{"valid": "shape"}, {"invalid": {1, 2}}]},
    ],
)
def test_provider_extra_rejects_every_non_json_or_non_finite_value(
    invalid_extra: object,
) -> None:
    with pytest.raises(ValidationError):
        GitHubUser.model_validate({**_user_payload(), "extra": invalid_extra})


def test_provider_extra_accepts_detached_finite_json_scalars() -> None:
    source = {
        "text": "value",
        "integer": 3,
        "number": 1.5,
        "enabled": True,
        "empty": None,
        "nested": ["reader", {"count": 1}],
    }

    user = GitHubUser.model_validate({**_user_payload(), "extra": source})
    source["nested"] = []

    assert user.extra["nested"] == ("reader", MappingProxyType({"count": 1}))


def test_provider_models_reject_unknown_fields_and_malformed_required_fields() -> None:
    with pytest.raises(ValidationError) as unknown:
        GitHubUser.model_validate({**_user_payload(), "server_field": "must-be-explicit"})
    with pytest.raises(ValidationError) as malformed:
        GitHubUser.model_validate({**_user_payload(), "id": "1"})
    with pytest.raises(ValidationError) as missing:
        GitHubUser.model_validate({"id": 1, "html_url": "https://github.com/octocat"})

    assert unknown.value.errors()[0]["type"] == "extra_forbidden"
    assert malformed.value.errors()[0]["type"] == "int_type"
    assert missing.value.errors()[0]["type"] == "missing"


def test_issue_model_parses_an_actual_frozen_json_timestamp_strictly() -> None:
    payload = {
        **_common_payload(),
        "updated_at": "2026-08-25T10:00:00Z",
        "labels": [{"name": "intent"}],
    }
    page = PageResult(items=(payload,), etag=None)

    issue = GitHubIssue.model_validate(page.items[0])

    assert issue.updated_at == datetime(2026, 8, 25, 10, 0, tzinfo=UTC)


@pytest.mark.parametrize("invalid_timestamp", ["not-a-date", 1, True])
def test_issue_model_rejects_malformed_or_non_string_json_timestamps(
    invalid_timestamp: object,
) -> None:
    payload = {**_common_payload(), "updated_at": invalid_timestamp}

    with pytest.raises(ValidationError):
        GitHubIssue.model_validate(payload)


def test_page_result_enforces_strict_not_modified_invariants() -> None:
    with pytest.raises(ValidationError):
        PageResult(items=({"id": 1},), etag='"v1"', not_modified=True)
    with pytest.raises(ValidationError):
        PageResult(items=[], etag=None, not_modified=False)  # type: ignore[arg-type]
