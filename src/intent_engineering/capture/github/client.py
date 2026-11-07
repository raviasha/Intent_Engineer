"""Deterministic, secret-safe async GitHub REST client."""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any, Self

import anyio
import httpx
from pydantic import ConfigDict, Field

from intent_engineering.capture.github.auth import GitHubCredentials
from intent_engineering.capture.github.errors import (
    GitHubApiError,
    GitHubNotFound,
    GitHubPermissionError,
    GitHubProtocolError,
    GitHubRateLimitError,
    GitHubTransientError,
    endpoint_overlaps_secret,
    provider_key_overlaps_secret,
    provider_value_overlaps_secret,
    request_id_overlaps_secret,
    sanitize_endpoint,
    sanitize_request_id,
)
from intent_engineering.capture.github.models import GitHubRepositoryStatus, PageResult
from intent_engineering.core.models._base import StrictModel

if TYPE_CHECKING:
    from intent_engineering.team_state.github import GitHubJsonResponse

GITHUB_API_BASE_URL = httpx.URL("https://api.github.com")
GITHUB_ACCEPT = "application/vnd.github+json"
GITHUB_USER_AGENT = "intent-engineering/0.1.0"
GITHUB_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=5.0)

type Clock = Callable[[], datetime]
type Sleeper = Callable[[float], Awaitable[None]]

_LINK_TOKEN_PUNCTUATION = frozenset("!#$%&'*+-.^_`|~")
_INVALID_LINK_TARGET_CHARACTERS = frozenset('<>"{}|\\^`')
_CANONICAL_REPOSITORY = re.compile(
    r"(?!-)(?!.*--)[a-z0-9-]{1,39}(?<!-)/[a-z0-9][a-z0-9._-]{0,99}\Z"
)
_RATE_SCALAR_MAX = 1_000_000_000
_MAX_RESPONSE_BYTES = 1024 * 1024
_MAX_RAW_RESPONSE_BYTES = 32 * 1024 * 1024


class RetryPolicy(StrictModel):
    """Fixed-at-construction deterministic retry settings."""

    model_config = ConfigDict(frozen=True, strict=True)

    max_attempts: int = Field(default=3, ge=1, le=10)
    base_delay: float = Field(default=0.5, ge=0.0, le=60.0)
    max_delay: float = Field(default=4.0, ge=0.0, le=60.0)

    def delay_after(self, attempt: int) -> float:
        """Return bounded exponential delay after a one-indexed failed attempt."""
        return float(min(self.base_delay * (2 ** (attempt - 1)), self.max_delay))


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _effective_port(url: httpx.URL) -> int | None:
    if url.port is not None:
        return url.port
    return 443 if url.scheme == "https" else None


def _is_allowed_origin(url: httpx.URL) -> bool:
    return (
        url.scheme == "https"
        and url.host == GITHUB_API_BASE_URL.host
        and _effective_port(url) == _effective_port(GITHUB_API_BASE_URL)
        and not url.userinfo
        and not url.fragment
    )


def _retry_at(headers: httpx.Headers, clock: Clock) -> datetime | None:
    retry_after = headers.get("Retry-After")
    if retry_after is not None:
        if retry_after.isascii() and retry_after.isdecimal():
            try:
                seconds = int(retry_after)
                if seconds < 0:
                    return None
                return clock().astimezone(UTC) + timedelta(seconds=seconds)
            except (OverflowError, ValueError):
                return None
        try:
            parsed = parsedate_to_datetime(retry_after)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC)
        except (OverflowError, TypeError, ValueError):
            return None

    reset = headers.get("X-RateLimit-Reset")
    if reset is None:
        return None
    try:
        return datetime.fromtimestamp(int(reset), tz=UTC)
    except (OSError, OverflowError, TypeError, ValueError):
        return None


def _bounded_rate_integer(value: str | None, *, maximum: int = _RATE_SCALAR_MAX) -> int | None:
    if value is None or not value.isascii() or not value.isdecimal() or len(value) > 12:
        return None
    parsed = int(value)
    return parsed if parsed <= maximum else None


def _repository_rate_status(
    headers: httpx.Headers,
) -> tuple[int, int, int, datetime, str] | None:
    limit = _bounded_rate_integer(headers.get("X-RateLimit-Limit"))
    remaining = _bounded_rate_integer(headers.get("X-RateLimit-Remaining"))
    used = _bounded_rate_integer(headers.get("X-RateLimit-Used"))
    reset = _bounded_rate_integer(headers.get("X-RateLimit-Reset"), maximum=253_402_300_799)
    resource = headers.get("X-RateLimit-Resource")
    if (
        limit is None
        or remaining is None
        or used is None
        or reset is None
        or remaining > limit
        or used > limit
        or resource is None
        or re.fullmatch(r"[A-Za-z0-9_-]{1,32}", resource) is None
    ):
        return None
    try:
        reset_at = datetime.fromtimestamp(reset, tz=UTC)
    except (OSError, OverflowError, ValueError):
        return None
    return limit, remaining, used, reset_at, resource


def _is_link_token_character(value: str) -> bool:
    return value.isascii() and (value.isalnum() or value in _LINK_TOKEN_PUNCTUATION)


def _parse_link_header(value: str) -> tuple[tuple[str, Mapping[str, str]], ...]:
    """Parse the RFC Link subset used by GitHub and reject incomplete structures."""
    entries: list[tuple[str, Mapping[str, str]]] = []
    index = 0
    length = len(value)

    def skip_whitespace(position: int) -> int:
        while position < length and value[position] in " \t":
            position += 1
        return position

    while True:
        index = skip_whitespace(index)
        if index >= length or value[index] != "<":
            raise ValueError("malformed Link target")
        target_end = value.find(">", index + 1)
        if target_end < 0:
            raise ValueError("unterminated Link target")
        target = value[index + 1 : target_end]
        if not target or any(
            not 0x21 <= ord(character) <= 0x7E or character in _INVALID_LINK_TARGET_CHARACTERS
            for character in target
        ):
            raise ValueError("invalid Link target")
        index = target_end + 1
        parameters: dict[str, str] = {}

        while True:
            index = skip_whitespace(index)
            if index >= length or value[index] == ",":
                break
            if value[index] != ";":
                raise ValueError("malformed Link parameter separator")
            index = skip_whitespace(index + 1)
            name_start = index
            while index < length and _is_link_token_character(value[index]):
                index += 1
            if index == name_start:
                raise ValueError("missing Link parameter name")
            name = value[name_start:index].lower()
            index = skip_whitespace(index)
            if index >= length or value[index] != "=":
                raise ValueError("missing Link parameter value")
            index = skip_whitespace(index + 1)
            if index >= length:
                raise ValueError("missing Link parameter value")

            if value[index] == '"':
                index += 1
                parsed_value: list[str] = []
                while index < length and value[index] != '"':
                    if value[index] == "\\":
                        index += 1
                        if index >= length:
                            raise ValueError("unterminated Link quoted value")
                    if ord(value[index]) < 0x20 or ord(value[index]) == 0x7F:
                        raise ValueError("invalid Link quoted value")
                    parsed_value.append(value[index])
                    index += 1
                if index >= length:
                    raise ValueError("unterminated Link quoted value")
                index += 1
                parameter_value = "".join(parsed_value)
            else:
                value_start = index
                while index < length and _is_link_token_character(value[index]):
                    index += 1
                if index == value_start:
                    raise ValueError("invalid Link parameter value")
                parameter_value = value[value_start:index]
            if name in parameters:
                raise ValueError("duplicate Link parameter")
            parameters[name] = parameter_value

        entries.append((target, parameters))
        if index >= length:
            return tuple(entries)
        index = skip_whitespace(index + 1)
        if index >= length:
            raise ValueError("trailing Link separator")


class GitHubClient:
    """Provider-local GET client with bounded retries and secure pagination."""

    def __init__(
        self,
        credentials: GitHubCredentials,
        *,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Clock = _utc_now,
        sleeper: Sleeper = anyio.sleep,
        retry_policy: RetryPolicy | None = None,
        page_cap: int = 100,
    ) -> None:
        if client is not None and transport is not None:
            raise ValueError("client and transport are mutually exclusive")
        if page_cap < 1:
            raise ValueError("page_cap must be positive")

        # This is the sole reveal point: the secret moves directly into an in-memory header.
        authorization = f"Bearer {credentials.token.get_secret_value()}"
        headers = {
            "Accept": GITHUB_ACCEPT,
            "User-Agent": GITHUB_USER_AGENT,
            "Authorization": authorization,
        }
        self._owned = client is None
        if client is None:
            self._client = httpx.AsyncClient(
                base_url=GITHUB_API_BASE_URL,
                headers=headers,
                timeout=GITHUB_TIMEOUT,
                follow_redirects=False,
                transport=transport,
            )
        else:
            self._client = client
        self._request_headers: httpx.Headers | None = httpx.Headers(headers)
        self._clock = clock
        self._sleeper = sleeper
        self._retry_policy = retry_policy or RetryPolicy()
        self._page_cap = page_cap
        self._closed = False

    def __repr__(self) -> str:
        return "GitHubClient(base_url='https://api.github.com')"

    @property
    def base_url(self) -> httpx.URL:
        return GITHUB_API_BASE_URL

    @property
    def timeout(self) -> httpx.Timeout:
        return GITHUB_TIMEOUT

    @property
    def follow_redirects(self) -> bool:
        return False

    @property
    def is_closed(self) -> bool:
        return self._closed or self._client.is_closed

    async def __aenter__(self) -> Self:
        if self.is_closed:
            raise RuntimeError("GitHub client is closed")
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close only an AsyncClient constructed by this wrapper."""
        if self._closed:
            return
        if self._owned and not self._client.is_closed:
            await self._client.aclose()
            self._client.headers.pop("Authorization", None)
        self._request_headers = None
        self._closed = True

    def _absolute_url(self, value: str, *, pagination: bool) -> httpx.URL | None:
        malformed = False
        try:
            parsed = httpx.URL(value)
            if pagination:
                if not parsed.is_absolute_url:
                    malformed = True
                    resolved = parsed
                else:
                    resolved = parsed
            else:
                if parsed.is_absolute_url:
                    resolved = parsed
                elif value.startswith("/") and not value.startswith("//"):
                    resolved = GITHUB_API_BASE_URL.join(value)
                else:
                    malformed = True
                    resolved = parsed
        except (TypeError, ValueError):
            malformed = True
            resolved = GITHUB_API_BASE_URL
        if malformed or not _is_allowed_origin(resolved):
            return None
        return resolved

    def _authorization_secret(self) -> str | None:
        headers = self._request_headers
        if headers is None:
            headers = self._client.headers
        authorization = headers.get("Authorization")
        if authorization is None or not authorization.startswith("Bearer "):
            return None
        return str.removeprefix(authorization, "Bearer ")

    def _safe_request_id(self, value: str | None) -> str | None:
        secret = self._authorization_secret()
        if value is not None and secret is not None and request_id_overlaps_secret(value, secret):
            return None
        return sanitize_request_id(value)

    def _safe_endpoint(self, value: str) -> str:
        secret = self._authorization_secret()
        if secret is not None and endpoint_overlaps_secret(value, secret):
            return "/redacted"
        return sanitize_endpoint(value)

    def _provider_material_overlaps_credential(self, value: object) -> bool:
        """Inspect detached provider scalars without retaining the credential in failures."""
        authorization_secret = self._authorization_secret()
        if authorization_secret is None:
            return False
        pending: list[tuple[object, bool]] = [(value, False)]
        overlap = False
        while pending and not overlap:
            item, is_key = pending.pop()
            if isinstance(item, Mapping):
                pending.extend((key, True) for key in item)
                pending.extend((nested, False) for nested in item.values())
            elif isinstance(item, (list, tuple)):
                pending.extend((nested, False) for nested in item)
            elif item is not None and type(item) in {str, int, float, bool}:
                overlap = (
                    provider_key_overlaps_secret(str(item), authorization_secret)
                    if is_key
                    else provider_value_overlaps_secret(str(item), authorization_secret)
                )
        authorization_secret = None
        return overlap

    @staticmethod
    async def _close_response(response: httpx.Response) -> None:
        with anyio.CancelScope(shield=True):
            await response.aclose()

    async def _read_bounded_response(
        self,
        response: httpx.Response,
        endpoint: str,
        *,
        max_bytes: int = _MAX_RESPONSE_BYTES,
    ) -> bytes:
        content = bytearray()
        try:
            async for chunk in response.aiter_bytes():
                if len(chunk) > max_bytes - len(content):
                    raise GitHubProtocolError(endpoint)
                content.extend(chunk)
            return bytes(content)
        finally:
            content.clear()

    async def _request_page(
        self,
        url: httpx.URL,
        *,
        params: Mapping[str, str] | None,
        etag: str | None,
        endpoint: str,
        readable_statuses: frozenset[int],
        max_bytes: int = _MAX_RESPONSE_BYTES,
        accept: str | None = None,
    ) -> tuple[httpx.Response, bytes]:
        if self.is_closed:
            raise RuntimeError("GitHub client is closed")
        headers = (
            self._request_headers.copy() if self._request_headers is not None else httpx.Headers()
        )
        if etag is not None:
            headers["If-None-Match"] = etag
        if accept is not None:
            headers["Accept"] = accept
        request_url = url.copy_merge_params(params) if params else url
        for attempt in range(1, self._retry_policy.max_attempts + 1):
            transport_failed = False
            response: httpx.Response | None = None
            try:
                request = httpx.Request(
                    "GET",
                    request_url,
                    headers=headers or None,
                    extensions={"timeout": GITHUB_TIMEOUT.as_dict()},
                )
                response = await self._client.send(
                    request,
                    auth=None,
                    follow_redirects=False,
                    stream=True,
                )
                if response.status_code in readable_statuses:
                    content = await self._read_bounded_response(
                        response, endpoint, max_bytes=max_bytes
                    )
                else:
                    content = b""
            except httpx.TransportError:
                transport_failed = True
            except BaseException:
                if response is not None:
                    await self._close_response(response)
                raise

            if transport_failed:
                if response is not None:
                    await self._close_response(response)
                if attempt == self._retry_policy.max_attempts:
                    raise GitHubTransientError(endpoint, attempt_count=attempt)
                await self._sleeper(self._retry_policy.delay_after(attempt))
                continue

            assert response is not None
            if 500 <= response.status_code <= 599:
                status_code = response.status_code
                request_id = self._safe_request_id(response.headers.get("X-GitHub-Request-Id"))
                await self._close_response(response)
                if attempt == self._retry_policy.max_attempts:
                    raise GitHubTransientError(
                        endpoint,
                        attempt_count=attempt,
                        status_code=status_code,
                        request_id=request_id,
                    )
                await self._sleeper(self._retry_policy.delay_after(attempt))
                continue
            return response, content
        raise AssertionError("retry loop must return or raise")

    def _status_error(self, response: httpx.Response, endpoint: str) -> GitHubApiError:
        status = response.status_code
        request_id = self._safe_request_id(response.headers.get("X-GitHub-Request-Id"))
        is_primary_limit = status == 403 and response.headers.get("X-RateLimit-Remaining") == "0"
        is_secondary_limit = status == 403 and "Retry-After" in response.headers
        if status == 429 or is_primary_limit or is_secondary_limit:
            return GitHubRateLimitError(
                status,
                endpoint,
                request_id,
                retry_at=_retry_at(response.headers, self._clock),
            )
        if status in {401, 403}:
            return GitHubPermissionError(status, endpoint, request_id)
        if status == 404:
            return GitHubNotFound(status, endpoint, request_id)
        return GitHubApiError(status, endpoint, request_id)

    def _next_url(self, response: httpx.Response, endpoint: str) -> str | None:
        raw_link = response.headers.get("Link")
        if raw_link is None:
            return None
        parse_failed = False
        try:
            entries = _parse_link_header(raw_link)
        except ValueError:
            parse_failed = True
            entries = ()
        if parse_failed:
            raise GitHubProtocolError(endpoint)
        next_urls = [
            target
            for target, parameters in entries
            if any(relation.casefold() == "next" for relation in parameters.get("rel", "").split())
        ]
        if len(next_urls) > 1:
            raise GitHubProtocolError(endpoint)
        return next_urls[0] if next_urls else None

    async def get_repository_status(self, repository: str) -> GitHubRepositoryStatus:
        """Probe one repository and return only validated, bounded diagnostic scalars."""
        if type(repository) is not str or _CANONICAL_REPOSITORY.fullmatch(repository) is None:
            raise GitHubProtocolError("/")
        current = self._absolute_url(f"/repos/{repository}", pagination=False)
        if current is None:  # pragma: no cover - canonical repository makes this unreachable
            raise GitHubProtocolError("/")
        endpoint = self._safe_endpoint(current.path)
        response, content = await self._request_page(
            current,
            params=None,
            etag=None,
            endpoint=endpoint,
            readable_statuses=frozenset(range(200, 300)),
        )
        if not 200 <= response.status_code <= 299:
            error = self._status_error(response, endpoint)
            await self._close_response(response)
            del response
            raise error from None

        malformed = False
        payload: Any = None
        try:
            payload = json.loads(content)
        except (UnicodeError, ValueError):
            malformed = True
        rate_status = _repository_rate_status(response.headers)
        reflected_provider_material = self._provider_material_overlaps_credential(
            (
                payload.get("full_name") if type(payload) is dict else None,
                response.headers.get("X-RateLimit-Limit"),
                response.headers.get("X-RateLimit-Remaining"),
                response.headers.get("X-RateLimit-Used"),
                response.headers.get("X-RateLimit-Reset"),
                response.headers.get("X-RateLimit-Resource"),
            )
        )
        if (
            malformed
            or type(payload) is not dict
            or type(payload.get("full_name")) is not str
            or payload.get("full_name", "").casefold() != repository
            or rate_status is None
            or reflected_provider_material
        ):
            malformed = True
        await self._close_response(response)
        del response
        payload = None
        if malformed or rate_status is None:
            rate_status = None
            repository = ""
            raise GitHubProtocolError(endpoint) from None
        limit, remaining, used, reset_at, resource = rate_status
        return GitHubRepositoryStatus(
            repository=repository,
            accessible=True,
            rate_limit=limit,
            rate_remaining=remaining,
            rate_used=used,
            rate_reset_at=reset_at,
            rate_resource=resource,
        )

    async def request_json_object(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        payload: Mapping[str, object] | None = None,
        allowed_statuses: frozenset[int] = frozenset({200}),
    ) -> GitHubJsonResponse:
        """Issue one bounded object request; mutating requests are never retried."""
        if (
            method not in {"GET", "POST", "PUT"}
            or type(allowed_statuses) is not frozenset
            or not allowed_statuses
            or any(
                type(status) is not int
                or (
                    not 200 <= status <= 299
                    and not (method == "GET" and status == 404)
                    and not (method == "POST" and status == 422)
                )
                for status in allowed_statuses
            )
            or (method == "GET" and payload is not None)
        ):
            raise GitHubProtocolError("/")
        current = self._absolute_url(path, pagination=False)
        if current is None:
            raise GitHubProtocolError("/")
        endpoint = self._safe_endpoint(current.path)
        response: httpx.Response | None = None
        try:
            if method == "GET":
                response, content = await self._request_page(
                    current,
                    params=params,
                    etag=None,
                    endpoint=endpoint,
                    readable_statuses=allowed_statuses,
                )
            else:
                headers = (
                    self._request_headers.copy()
                    if self._request_headers is not None
                    else httpx.Headers()
                )
                request = httpx.Request(
                    method,
                    current.copy_merge_params(params) if params else current,
                    headers=headers or None,
                    json=None if payload is None else dict(payload),
                    extensions={"timeout": GITHUB_TIMEOUT.as_dict()},
                )
                try:
                    response = await self._client.send(
                        request,
                        auth=None,
                        follow_redirects=False,
                        stream=True,
                    )
                except httpx.TransportError:
                    raise GitHubTransientError(endpoint, attempt_count=1) from None
            if response.status_code not in allowed_statuses:
                raise self._status_error(response, endpoint)
            if method != "GET":
                try:
                    content = await self._read_bounded_response(response, endpoint)
                except httpx.TransportError:
                    raise GitHubTransientError(endpoint, attempt_count=1) from None
            malformed = False
            parsed: Any = None
            try:
                parsed = json.loads(content)
            except (UnicodeError, ValueError):
                malformed = True
            oauth_scopes = response.headers.get("X-OAuth-Scopes")
            if (
                malformed
                or type(parsed) is not dict
                or (oauth_scopes is not None and len(oauth_scopes.encode("utf-8")) > 4096)
                or self._provider_material_overlaps_credential((parsed, oauth_scopes))
            ):
                raise GitHubProtocolError(endpoint)
            from intent_engineering.team_state.github import GitHubJsonResponse

            return GitHubJsonResponse(
                status_code=response.status_code,
                payload=dict(parsed),
                headers={} if oauth_scopes is None else {"X-OAuth-Scopes": oauth_scopes},
            )
        finally:
            if response is not None:
                await self._close_response(response)

    async def request_bytes(
        self,
        method: str,
        path: str,
        *,
        max_bytes: int,
        accept: str,
    ) -> bytes:
        """Issue one streamed raw-media GET under an explicit caller-supplied bound."""
        if (
            method != "GET"
            or type(max_bytes) is not int
            or not 1 <= max_bytes <= _MAX_RAW_RESPONSE_BYTES
            or accept != "application/vnd.github.raw+json"
        ):
            raise GitHubProtocolError("/")
        current = self._absolute_url(path, pagination=False)
        if current is None:
            raise GitHubProtocolError("/")
        endpoint = self._safe_endpoint(current.path)
        response: httpx.Response | None = None
        try:
            response, content = await self._request_page(
                current,
                params=None,
                etag=None,
                endpoint=endpoint,
                readable_statuses=frozenset({200}),
                max_bytes=max_bytes,
                accept=accept,
            )
            if response.status_code != 200:
                raise self._status_error(response, endpoint)
            return content
        finally:
            if response is not None:
                await self._close_response(response)

    async def get_pages(
        self,
        path: str,
        params: Mapping[str, str],
        etag: str | None = None,
    ) -> PageResult:
        """Fetch validated GitHub JSON-object arrays in RFC Link order."""
        current = self._absolute_url(path, pagination=False)
        if current is None:
            raise GitHubProtocolError("/")
        endpoint = self._safe_endpoint(current.path)
        page_params: Mapping[str, str] | None = params
        page_etag = etag
        page_number = 0
        first_etag: str | None = None
        seen: set[str] = set()
        items: list[Mapping[str, object]] = []

        while True:
            request_url = current.copy_merge_params(page_params) if page_params else current
            request_key = str(request_url)
            if request_key in seen:
                raise GitHubProtocolError(endpoint)
            seen.add(request_key)

            response, content = await self._request_page(
                current,
                params=page_params,
                etag=page_etag,
                endpoint=endpoint,
                readable_statuses=frozenset({200, 304}),
            )
            page_number += 1

            if response.status_code == 304:
                await self._close_response(response)
                if page_number != 1 or etag is None:
                    raise GitHubProtocolError(endpoint)
                return PageResult(items=(), etag=etag, not_modified=True)

            if not 200 <= response.status_code <= 299:
                error = self._status_error(response, endpoint)
                await self._close_response(response)
                del response
                raise error from None

            malformed_json = False
            try:
                payload: Any = json.loads(content)
            except (UnicodeError, ValueError):
                malformed_json = True
                payload = None
            if (
                malformed_json
                or type(payload) is not list
                or any(type(item) is not dict for item in payload)
            ):
                await self._close_response(response)
                del response
                payload = None
                raise GitHubProtocolError(endpoint) from None

            reflected_provider_material = self._provider_material_overlaps_credential(
                (
                    response.headers.get("ETag"),
                    response.headers.get("Link"),
                    payload,
                )
            )
            if reflected_provider_material:
                await self._close_response(response)
                del response
                payload = None
                first_etag = None
                items.clear()
                raise GitHubProtocolError(endpoint) from None

            if page_number == 1:
                first_etag = response.headers.get("ETag")

            items.extend(payload)
            next_failed = False
            try:
                next_value = self._next_url(response, endpoint)
            except GitHubProtocolError:
                next_failed = True
                next_value = None
            await self._close_response(response)
            del response
            payload = None
            if next_failed:
                raise GitHubProtocolError(endpoint) from None
            if next_value is None:
                return PageResult(items=tuple(items), etag=first_etag)
            if page_number >= self._page_cap:
                raise GitHubProtocolError(endpoint)
            next_url = self._absolute_url(next_value, pagination=True)
            if next_url is None:
                raise GitHubProtocolError(endpoint)
            current = next_url
            endpoint = self._safe_endpoint(current.path)
            page_params = None
            page_etag = None
