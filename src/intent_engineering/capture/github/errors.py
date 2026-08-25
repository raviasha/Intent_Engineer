"""Redacted public errors for GitHub provider boundaries."""

from __future__ import annotations

import base64
import re
from collections.abc import Callable
from datetime import datetime

GITHUB_AUTH_ERROR_MESSAGE = (
    "GitHub authentication unavailable; set GH_TOKEN or run `gh auth login`."
)


class GitHubAuthError(RuntimeError):
    """Report an actionable authentication failure without retaining provider output."""

    def __init__(self) -> None:
        super().__init__(GITHUB_AUTH_ERROR_MESSAGE)


class GitHubCheckpointError(ValueError):
    """A fixed public failure for malformed or foreign GitHub cursors."""

    def __init__(self) -> None:
        super().__init__("invalid GitHub checkpoint")


_SAFE_ENDPOINT_CHARACTER = re.compile(r"[^A-Za-z0-9/._~%:@+-]")
_SAFE_REQUEST_ID_CHARACTER = re.compile(r"[^A-Za-z0-9:_-]")
_MAX_ENDPOINT_LENGTH = 160
_MAX_REQUEST_ID_LENGTH = 64
_MIN_CREDENTIAL_FRAGMENT_LENGTH = 12


def sanitize_endpoint(value: str) -> str:
    """Return a length-bounded path safe for public error rendering."""
    return _sanitize_endpoint_text(value)[:_MAX_ENDPOINT_LENGTH]


def _sanitize_endpoint_text(value: str) -> str:
    sanitized = _SAFE_ENDPOINT_CHARACTER.sub("_", value)
    if not sanitized.startswith("/"):
        sanitized = f"/{sanitized}"
    return sanitized


def sanitize_request_id(value: str | None) -> str | None:
    """Return only inert, bounded GitHub request-id characters."""
    if value is None:
        return None
    sanitized = _sanitize_request_id_text(value)[:_MAX_REQUEST_ID_LENGTH]
    return sanitized or None


def _sanitize_request_id_text(value: str) -> str:
    return _SAFE_REQUEST_ID_CHARACTER.sub("_", value.strip())


def request_id_overlaps_secret(value: str, secret: str) -> bool:
    """Detect secret material before public request-id truncation can hide the overlap."""
    return _public_prefix_overlaps_secret(
        value,
        secret,
        sanitizer=_sanitize_request_id_text,
        max_length=_MAX_REQUEST_ID_LENGTH,
    )


def endpoint_overlaps_secret(value: str, secret: str) -> bool:
    """Detect credential fragments before public endpoint truncation."""
    return _public_prefix_overlaps_secret(
        value,
        secret,
        sanitizer=_sanitize_endpoint_text,
        max_length=_MAX_ENDPOINT_LENGTH,
    )


def provider_value_overlaps_secret(value: str, secret: str) -> bool:
    """Reject exact or conservatively long raw, sanitized, and encoded reflections."""
    return _value_overlaps_secret(
        value,
        secret,
        sanitizer=_sanitize_request_id_text,
        max_length=None,
    )


def provider_key_overlaps_secret(value: str, secret: str) -> bool:
    """Apply the same conservative rule to provider-controlled mapping keys."""
    return _value_overlaps_secret(
        value,
        secret,
        sanitizer=_sanitize_request_id_text,
        max_length=None,
    )


def _public_prefix_overlaps_secret(
    value: str,
    secret: str,
    *,
    sanitizer: Callable[[str], str],
    max_length: int,
) -> bool:
    return _value_overlaps_secret(
        value,
        secret,
        sanitizer=sanitizer,
        max_length=max_length,
    )


def _value_overlaps_secret(
    value: str,
    secret: str,
    *,
    sanitizer: Callable[[str], str],
    max_length: int | None,
) -> bool:
    """Match full credentials or high-signal fragments without short-prefix collisions."""
    full_candidates = (value.strip(), sanitizer(value))
    secret_bytes = secret.encode("utf-8")
    secret_variants = (
        secret,
        sanitizer(secret),
        base64.b64encode(secret_bytes).decode("ascii"),
        secret_bytes.hex(),
    )
    if any(
        secret_variant in candidate
        for candidate in full_candidates
        for secret_variant in secret_variants
        if candidate and secret_variant
    ):
        return True
    candidates: tuple[str, ...] = full_candidates
    if max_length is not None:
        candidates = tuple(candidate[:max_length] for candidate in candidates)
    return any(
        _has_meaningful_overlap(candidate, secret_variant)
        for candidate in candidates
        for secret_variant in secret_variants
        if candidate and secret_variant
    )


def _has_meaningful_overlap(candidate: str, secret_variant: str) -> bool:
    if secret_variant in candidate:
        return True
    if (
        len(candidate) < _MIN_CREDENTIAL_FRAGMENT_LENGTH
        or len(secret_variant) < _MIN_CREDENTIAL_FRAGMENT_LENGTH
    ):
        return False
    return any(
        secret_variant[index : index + _MIN_CREDENTIAL_FRAGMENT_LENGTH] in candidate
        for index in range(len(secret_variant) - _MIN_CREDENTIAL_FRAGMENT_LENGTH + 1)
    )


def _render_details(
    *,
    status_code: int | None,
    endpoint: str,
    request_id: str | None,
    retry_at: datetime | None = None,
    attempt_count: int | None = None,
) -> str:
    details = [f"endpoint {endpoint}"]
    if status_code is not None:
        details.insert(0, f"status {status_code}")
    if request_id is not None:
        details.append(f"request id {request_id}")
    if retry_at is not None:
        details.append(f"retry at {retry_at.isoformat()}")
    if attempt_count is not None:
        details.append(f"attempts {attempt_count}")
    return "; ".join(details)


class GitHubApiError(RuntimeError):
    """A non-success GitHub response represented by safe scalar metadata only."""

    def __init__(self, status_code: int, endpoint: str, request_id: str | None = None) -> None:
        self.status_code = status_code
        self.endpoint = sanitize_endpoint(endpoint)
        self.request_id = sanitize_request_id(request_id)
        details = _render_details(
            status_code=self.status_code,
            endpoint=self.endpoint,
            request_id=self.request_id,
        )
        super().__init__(f"GitHub API error ({details}).")


class GitHubPermissionError(GitHubApiError):
    """GitHub rejected the caller's authentication or authorization."""

    def __init__(self, status_code: int, endpoint: str, request_id: str | None = None) -> None:
        super().__init__(status_code, endpoint, request_id)
        details = _render_details(
            status_code=self.status_code,
            endpoint=self.endpoint,
            request_id=self.request_id,
        )
        self.args = (f"GitHub permission error ({details}).",)


class GitHubRateLimitError(GitHubApiError):
    """GitHub denied a request because a primary or secondary limit was reached."""

    def __init__(
        self,
        status_code: int,
        endpoint: str,
        request_id: str | None = None,
        retry_at: datetime | None = None,
    ) -> None:
        self.retry_at = retry_at
        super().__init__(status_code, endpoint, request_id)
        details = _render_details(
            status_code=self.status_code,
            endpoint=self.endpoint,
            request_id=self.request_id,
            retry_at=self.retry_at,
        )
        self.args = (f"GitHub rate limit error ({details}).",)


class GitHubNotFound(GitHubApiError):
    """The requested GitHub resource was not found."""

    def __init__(self, status_code: int, endpoint: str, request_id: str | None = None) -> None:
        super().__init__(status_code, endpoint, request_id)
        details = _render_details(
            status_code=self.status_code,
            endpoint=self.endpoint,
            request_id=self.request_id,
        )
        self.args = (f"GitHub resource not found ({details}).",)


class GitHubTransientError(RuntimeError):
    """An exhausted idempotent request represented without request/response retention."""

    def __init__(
        self,
        endpoint: str,
        attempt_count: int,
        status_code: int | None = None,
        request_id: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.endpoint = sanitize_endpoint(endpoint)
        self.request_id = sanitize_request_id(request_id)
        self.attempt_count = attempt_count
        details = _render_details(
            status_code=self.status_code,
            endpoint=self.endpoint,
            request_id=self.request_id,
            attempt_count=self.attempt_count,
        )
        super().__init__(f"GitHub transient error ({details}).")


class GitHubProtocolError(RuntimeError):
    """A malformed response or unsafe pagination transition."""

    def __init__(self, endpoint: str) -> None:
        self.endpoint = sanitize_endpoint(endpoint)
        super().__init__(f"GitHub response protocol error (endpoint {self.endpoint}).")
