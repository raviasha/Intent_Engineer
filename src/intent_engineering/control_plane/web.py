"""Strict loopback-only Starlette API for local human review."""

from __future__ import annotations

import json
import re
import secrets
import traceback
from collections.abc import Awaitable, Callable
from typing import Any, cast
from urllib.parse import quote, unquote_to_bytes, urlsplit

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from intent_engineering.control_plane.http_models import (
    MAX_HTTP_BODY_BYTES,
    AssessmentNodeResponse,
    AssessmentResponse,
    ClarificationAnswerDiscardRequest,
    ClarificationAnswerDiscardResponse,
    ClarificationAnswerPreviewRequest,
    ClarificationAnswerPreviewResponse,
    DecisionOptionsRequest,
    DecisionVerifyRequest,
    EnrichmentAnswerRequest,
    EnrichmentGapRequest,
    EnrichmentProposalRequest,
    EnrichmentProposalResponse,
    EnrichmentSessionRequest,
    EnrichmentSessionResponse,
    EnrichmentStartRequest,
    InboxResponse,
    RegistrationOptionsRequest,
    RegistrationVerifyRequest,
    ReviewedTestRunRequest,
    StatusResponse,
    TeamEnrollmentCancelRequest,
    TeamEnrollmentOptionsRequest,
    TeamEnrollmentVerifyRequest,
    canonical_json_object,
    detach_response_mapping,
    parse_request_bytes,
    parse_response_bytes,
)
from intent_engineering.control_plane.models import CredentialRecord
from intent_engineering.control_plane.service import ControlPlaneService
from intent_engineering.team_state.models import RecipientRecord as TeamRecipientRecord

_BODY_STATE_KEY = "intent.control-plane.raw-body"
_QUERY_STATE_KEY = "intent.control-plane.query"
_CSRF_COOKIE = "intent_csrf"
_CSRF_HEADER = b"x-intent-csrf"
_FIXED_ERROR = {
    "schema_version": 1,
    "status": "rejected",
    "reason": "request_unavailable",
}
_FIXED_ERROR_BYTES = json.dumps(
    _FIXED_ERROR,
    allow_nan=False,
    ensure_ascii=False,
    separators=(",", ":"),
    sort_keys=True,
).encode("utf-8")
_CSP = (
    "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
    "form-action 'self'; object-src 'none'; script-src 'self'; "
    "style-src 'self'; connect-src 'self'"
)
_STATIC_METHODS = {
    "/api/v1/assessment": "GET",
    "/api/v1/status": "GET",
    "/api/v1/inbox": "GET",
    "/api/v1/development/observation": "GET",
    "/api/v1/development/tests/run": "POST",
    "/api/v1/enrichment/start": "POST",
    "/api/v1/enrichment/current": "POST",
    "/api/v1/enrichment/answer": "POST",
    "/api/v1/enrichment/skip": "POST",
    "/api/v1/enrichment/pause": "POST",
    "/api/v1/enrichment/resume": "POST",
    "/api/v1/enrichment/propose": "POST",
    "/api/v1/clarifications/answers/preview": "POST",
    "/api/v1/clarifications/answers/discard": "POST",
    "/api/v1/webauthn/register/options": "POST",
    "/api/v1/webauthn/register/verify": "POST",
    "/api/v1/team/enrollment": "GET",
    "/api/v1/team/enrollment/options": "POST",
    "/api/v1/team/enrollment/verify": "POST",
    "/api/v1/team/enrollment/cancel": "POST",
    "/api/v1/team/publication/preview": "GET",
    "/api/v1/team/setup": "GET",
    **{
        f"/api/v1/team/setup/{action}": "POST"
        for action in (
            "inspect",
            "enroll",
            "protection-preview",
            "publication-preview",
            "options",
            "verify",
            "cancel",
        )
    },
    "/api/v1/decisions/options": "POST",
    "/api/v1/decisions/verify": "POST",
}
_PROPOSAL_PATH = re.compile(r"^/api/v1/proposals/([A-Za-z0-9:._-]{1,512})$")
_PROPOSAL_PREFIX = "/api/v1/proposals/"
_ASSESSMENT_NODE_PATH = re.compile(r"^/api/v1/assessment/nodes/([A-Za-z0-9:._-]{1,512})$")
_ASSESSMENT_NODE_PREFIX = "/api/v1/assessment/nodes/"
_ASSESSMENT_REFERENCE = re.compile(r"^[A-Za-z0-9:._-]{1,512}$")
_ASSESSMENT_CURSOR = re.compile(r"^page:[0-9a-f]{64}:[1-9][0-9]{0,3}$")
_MAX_RAW_TARGET_BYTES = 4_096
_CSRF_VALUE = re.compile(r"^[A-Za-z0-9_-]{1,512}$")


class _HttpBoundaryError(Exception):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _HandlerRequestError(Exception):
    pass


def _valid_configuration(origin: object, csrf_secret: object) -> tuple[str, int, str]:
    result: tuple[str, int, str] | None = None
    parsed: Any = None
    port: int | None = None
    try:
        if type(origin) is not str or type(csrf_secret) is not str:
            raise ValueError("invalid control plane HTTP configuration")
        parsed = urlsplit(origin)
        port = parsed.port
        if (
            origin != f"http://localhost:{port}"
            or parsed.scheme != "http"
            or parsed.hostname != "localhost"
            or port is None
            or not 1 <= port <= 65535
            or parsed.username is not None
            or parsed.password is not None
            or parsed.netloc != f"localhost:{port}"
            or parsed.path != ""
            or parsed.query != ""
            or parsed.fragment != ""
            or _CSRF_VALUE.fullmatch(csrf_secret) is None
        ):
            raise ValueError("invalid control plane HTTP configuration")
        result = (parsed.netloc, port, csrf_secret)
    except (TypeError, ValueError):
        result = None
    finally:
        origin = None
        csrf_secret = None
        parsed = None
        port = None
    if result is None:
        raise ValueError("invalid control plane HTTP configuration")
    return result


def _header_values(headers: list[tuple[bytes, bytes]], name: bytes) -> list[bytes]:
    return [value for candidate, value in headers if candidate.lower() == name]


def _single_header(headers: list[tuple[bytes, bytes]], name: bytes, *, status_code: int) -> bytes:
    values = _header_values(headers, name)
    if len(values) != 1 or type(values[0]) is not bytes:
        raise _HttpBoundaryError(status_code)
    return values[0]


def _content_length(headers: list[tuple[bytes, bytes]]) -> int | None:
    values = _header_values(headers, b"content-length")
    if not values:
        return None
    if len(values) != 1:
        raise _HttpBoundaryError(400)
    raw = values[0]
    if not raw or not raw.isdigit() or (len(raw) > 1 and raw.startswith(b"0")):
        raise _HttpBoundaryError(400)
    length = int(raw)
    if length > MAX_HTTP_BODY_BYTES:
        raise _HttpBoundaryError(413)
    return length


def _csrf_cookie(value: bytes, expected: bytes) -> bool:
    parts: list[bytes] = []
    cookies: dict[bytes, bytes] = {}
    part = b""
    name = b""
    cookie_value = b""
    try:
        if not value:
            return False
        parts = value.split(b"; ")
        if b";".join(parts) != value.replace(b"; ", b";"):
            return False
        for part in parts:
            name, separator, cookie_value = part.partition(b"=")
            if (
                separator != b"="
                or not name
                or name in cookies
                or not re.fullmatch(rb"[A-Za-z0-9_-]+", name)
            ):
                return False
            cookies[name] = cookie_value
        candidate = cookies.get(_CSRF_COOKIE.encode("ascii"))
        return candidate is not None and secrets.compare_digest(candidate, expected)
    finally:
        value = expected = part = name = cookie_value = b""
        parts.clear()
        cookies.clear()


def _expected_method(path: str) -> str:
    method = _STATIC_METHODS.get(path)
    if method is not None:
        return method
    if _PROPOSAL_PATH.fullmatch(path) is not None:
        return "GET"
    if _ASSESSMENT_NODE_PATH.fullmatch(path) is not None:
        return "GET"
    raise _HttpBoundaryError(404)


def _canonical_raw_path(path: str) -> bytes:
    proposal = _PROPOSAL_PATH.fullmatch(path)
    assessment_node = _ASSESSMENT_NODE_PATH.fullmatch(path)
    if proposal is None and assessment_node is None:
        try:
            return path.encode("ascii")
        except UnicodeError:
            raise _HttpBoundaryError(404) from None
    if proposal is not None:
        identifier = proposal.group(1)
        prefix = _PROPOSAL_PREFIX
    else:
        if assessment_node is None:  # pragma: no cover - narrowed above
            raise _HttpBoundaryError(404)
        identifier = assessment_node.group(1)
        prefix = _ASSESSMENT_NODE_PREFIX
    return f"{prefix}{quote(identifier, safe='._-')}".encode("ascii")


def _assessment_query(path: str, query: bytes) -> dict[str, str]:
    if path != "/api/v1/assessment":
        if query:
            raise _HttpBoundaryError(400)
        return {}
    if not query:
        return {}
    if len(query) > _MAX_RAW_TARGET_BYTES:
        raise _HttpBoundaryError(414)
    result: dict[str, str] = {}
    parts = query.split(b"&")
    if len(parts) > 2:
        raise _HttpBoundaryError(400)
    for part in parts:
        name, separator, raw_value = part.partition(b"=")
        if separator != b"=" or name not in {b"focus", b"cursor"}:
            raise _HttpBoundaryError(400)
        key = name.decode("ascii")
        if key in result or not raw_value:
            raise _HttpBoundaryError(400)
        try:
            decoded_bytes = unquote_to_bytes(raw_value)
            decoded = decoded_bytes.decode("ascii", errors="strict")
        except (UnicodeError, ValueError):
            raise _HttpBoundaryError(400) from None
        if raw_value != quote(decoded, safe="._-").encode("ascii"):
            raise _HttpBoundaryError(400)
        if key == "focus":
            if (
                _ASSESSMENT_REFERENCE.fullmatch(decoded) is None
                or len(decoded.encode("utf-8")) > 512
            ):
                raise _HttpBoundaryError(400)
        elif _ASSESSMENT_CURSOR.fullmatch(decoded) is None:
            raise _HttpBoundaryError(400)
        result[key] = decoded
    expected = b"&".join(
        name.encode("ascii") + b"=" + quote(result[name], safe="._-").encode("ascii")
        for name in ("focus", "cursor")
        if name in result
    )
    if query != expected:
        raise _HttpBoundaryError(400)
    return result


def _validate_scope_and_headers(
    scope: Scope,
    *,
    expected_host: bytes,
    expected_origin: bytes,
    csrf_secret: bytes,
) -> tuple[list[tuple[bytes, bytes]], int | None, dict[str, str]]:
    headers: list[tuple[bytes, bytes]] = []
    try:
        if type(scope) is not dict or scope.get("type") != "http":
            raise _HttpBoundaryError(400)
        method = scope.get("method")
        path = scope.get("path")
        raw_path = scope.get("raw_path")
        query = scope.get("query_string")
        scheme = scope.get("scheme")
        if (
            type(method) is not str
            or type(path) is not str
            or type(raw_path) is not bytes
            or type(query) is not bytes
        ):
            raise _HttpBoundaryError(400)
        if len(raw_path) + len(query) + (1 if query else 0) > _MAX_RAW_TARGET_BYTES:
            raise _HttpBoundaryError(414)
        if type(scheme) is not str or scheme != "http":
            raise _HttpBoundaryError(403)
        required_method = _expected_method(path)
        if raw_path != _canonical_raw_path(path):
            raise _HttpBoundaryError(404)
        query_values = _assessment_query(path, query)
        if method != required_method:
            raise _HttpBoundaryError(405)
        raw_headers = scope.get("headers")
        if type(raw_headers) is not list:
            raise _HttpBoundaryError(400)
        for pair in list.__iter__(raw_headers):
            if (
                type(pair) is not tuple
                or len(pair) != 2
                or type(pair[0]) is not bytes
                or type(pair[1]) is not bytes
            ):
                raise _HttpBoundaryError(400)
            headers.append((pair[0], pair[1]))
        host = _single_header(headers, b"host", status_code=403)
        if not secrets.compare_digest(host, expected_host):
            raise _HttpBoundaryError(403)
        origins = _header_values(headers, b"origin")
        if required_method == "GET":
            if len(origins) > 1 or (
                origins and not secrets.compare_digest(origins[0], expected_origin)
            ):
                raise _HttpBoundaryError(403)
        else:
            origin = _single_header(headers, b"origin", status_code=403)
            if not secrets.compare_digest(origin, expected_origin):
                raise _HttpBoundaryError(403)
        if _header_values(headers, b"transfer-encoding"):
            raise _HttpBoundaryError(400)
        length = _content_length(headers)
        content_types = _header_values(headers, b"content-type")
        if required_method == "POST":
            if content_types != [b"application/json"]:
                raise _HttpBoundaryError(415)
            csrf_headers = _header_values(headers, _CSRF_HEADER)
            cookie_headers = _header_values(headers, b"cookie")
            if (
                len(csrf_headers) != 1
                or len(cookie_headers) != 1
                or not secrets.compare_digest(csrf_headers[0], csrf_secret)
                or not _csrf_cookie(cookie_headers[0], csrf_secret)
            ):
                raise _HttpBoundaryError(403)
        elif content_types or (length is not None and length != 0):
            raise _HttpBoundaryError(400)
        return headers, length, query_values
    except _HttpBoundaryError:
        raise
    except (KeyError, TypeError, UnicodeError, ValueError):
        raise _HttpBoundaryError(400) from None


async def _read_body(receive: Receive, expected_length: int | None) -> bytearray:
    body = bytearray()
    message: Message = {}
    chunk = b""
    more = True
    try:
        while more:
            message = await receive()
            if type(message) is not dict or message.get("type") != "http.request":
                raise _HttpBoundaryError(400)
            chunk = message.get("body", b"")
            more_value = message.get("more_body", False)
            if type(chunk) is not bytes or type(more_value) is not bool:
                raise _HttpBoundaryError(400)
            if len(body) + len(chunk) > MAX_HTTP_BODY_BYTES:
                raise _HttpBoundaryError(413)
            body.extend(chunk)
            more = more_value
        if expected_length is not None and len(body) != expected_length:
            raise _HttpBoundaryError(400)
        return body
    finally:
        receive = cast(Receive, None)
        expected_length = None
        message = {}
        chunk = b""
        more = False


async def _empty_receive() -> Message:
    return {"type": "http.request", "body": b"", "more_body": False}


def _security_headers(csrf_secret: str, *, set_cookie: bool) -> list[tuple[bytes, bytes]]:
    headers = [
        (b"cache-control", b"no-store"),
        (b"x-content-type-options", b"nosniff"),
        (b"content-security-policy", _CSP.encode("ascii")),
        (b"referrer-policy", b"no-referrer"),
        (b"cross-origin-opener-policy", b"same-origin"),
        (b"cross-origin-resource-policy", b"same-origin"),
    ]
    if set_cookie:
        headers.append(
            (
                b"set-cookie",
                (f"{_CSRF_COOKIE}={csrf_secret}; Path=/; HttpOnly; SameSite=Strict").encode(
                    "ascii"
                ),
            )
        )
    return headers


def _secure_sender(send: Send, csrf_secret: str, *, set_cookie: bool) -> Send:
    security_names = {
        b"cache-control",
        b"x-content-type-options",
        b"content-security-policy",
        b"referrer-policy",
        b"cross-origin-opener-policy",
        b"cross-origin-resource-policy",
        b"set-cookie",
    }

    async def secured_send(message: Message) -> None:
        outgoing: Message | None = None
        original_headers: list[tuple[bytes, bytes]] = []
        retained: list[tuple[bytes, bytes]] = []
        signal: BaseException | None = None
        try:
            if type(message) is not dict:
                raise RuntimeError("invalid ASGI response")
            if message.get("type") == "http.response.start":
                raw_headers = message.get("headers", [])
                if type(raw_headers) is not list:
                    raise RuntimeError("invalid ASGI response")
                for pair in list.__iter__(raw_headers):
                    if (
                        type(pair) is not tuple
                        or len(pair) != 2
                        or type(pair[0]) is not bytes
                        or type(pair[1]) is not bytes
                    ):
                        raise RuntimeError("invalid ASGI response")
                    original_headers.append((pair[0], pair[1]))
                retained = [
                    pair for pair in original_headers if pair[0].lower() not in security_names
                ]
                outgoing = {
                    **message,
                    "headers": [
                        *retained,
                        *_security_headers(csrf_secret, set_cookie=set_cookie),
                    ],
                }
            else:
                outgoing = dict(message)
            await send(outgoing)
        except BaseException as caught:  # noqa: BLE001 - scrub before exact propagation
            caught.__traceback__ = None
            caught.__cause__ = None
            caught.__context__ = None
            signal = caught
        finally:
            message = {}
            outgoing = None
            original_headers.clear()
            retained.clear()
        if signal is not None:
            caught_signal = signal
            signal = None
            raise caught_signal.with_traceback(None)

    return secured_send


async def _send_fixed(send: Send, status_code: int) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status_code,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(_FIXED_ERROR_BYTES)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": _FIXED_ERROR_BYTES})


class _StrictLoopbackMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        expected_host: str,
        expected_origin: str,
        csrf_secret: str,
    ) -> None:
        self._app = app
        self._expected_host = expected_host.encode("ascii")
        self._expected_origin = expected_origin.encode("ascii")
        self._csrf_secret = csrf_secret
        self._csrf_bytes = csrf_secret.encode("ascii")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if type(scope) is dict and scope.get("type") == "lifespan":
            await self._app(scope, receive, send)
            return
        downstream_send = send
        body = bytearray()
        headers: list[tuple[bytes, bytes]] = []
        raw_state: object = None
        state: dict[str, object] | None = None
        secured_send: Send | None = None
        signal: BaseException | None = None
        failure_status: int | None = None
        try:
            headers, length, query_values = _validate_scope_and_headers(
                scope,
                expected_host=self._expected_host,
                expected_origin=self._expected_origin,
                csrf_secret=self._csrf_bytes,
            )
            body = await _read_body(receive, length)
            if scope["method"] == "GET" and body:
                raise _HttpBoundaryError(400)
            raw_state = scope.get("state")
            if raw_state is None:
                state = {}
                scope["state"] = state
            elif type(raw_state) is dict:
                state = raw_state
            else:
                raise _HttpBoundaryError(400)
            if _BODY_STATE_KEY in state or _QUERY_STATE_KEY in state:
                raise _HttpBoundaryError(400)
            state[_BODY_STATE_KEY] = body
            state[_QUERY_STATE_KEY] = query_values
            secured_send = _secure_sender(send, self._csrf_secret, set_cookie=False)
            await self._app(scope, _empty_receive, secured_send)
        except _HttpBoundaryError as error:
            failure_status = error.status_code
        except BaseException as caught:  # noqa: BLE001 - scrub exact cancellation path
            caught.__traceback__ = None
            caught.__cause__ = None
            caught.__context__ = None
            signal = caught
        finally:
            if body:
                body[:] = b"\x00" * len(body)
            body.clear()
            if state is not None:
                state.pop(_BODY_STATE_KEY, None)
                state.pop(_QUERY_STATE_KEY, None)
            headers.clear()
            scope = {}
            receive = cast(Receive, None)
            send = cast(Send, None)
            secured_send = None
            raw_state = None
            state = None
        if signal is not None:
            caught_signal = signal
            signal = None
            raise caught_signal.with_traceback(None)
        if failure_status is not None:
            error_send = _secure_sender(
                downstream_send,
                self._csrf_secret,
                set_cookie=False,
            )
            error_signal: BaseException | None = None
            try:
                await _send_fixed(error_send, failure_status)
            except BaseException as caught:  # noqa: BLE001 - scrub fixed-error cancellation
                caught.__traceback__ = None
                caught.__cause__ = None
                caught.__context__ = None
                error_signal = caught
            finally:
                error_send = cast(Send, None)
                downstream_send = cast(Send, None)
            if error_signal is not None:
                caught_signal = error_signal
                error_signal = None
                raise caught_signal.with_traceback(None)


def _request_bytes(request: Request) -> bytes:
    state = request.scope.get("state")
    if type(state) is not dict:
        raise _HandlerRequestError()
    value = state.get(_BODY_STATE_KEY)
    if type(value) is not bytearray:
        raise _HandlerRequestError()
    return bytes(value)


def _request_query(request: Request) -> dict[str, str]:
    state = request.scope.get("state")
    if type(state) is not dict:
        raise _HandlerRequestError()
    value = state.get(_QUERY_STATE_KEY)
    if type(value) is not dict or any(
        type(key) is not str or type(item) is not str for key, item in value.items()
    ):
        raise _HandlerRequestError()
    return cast(dict[str, str], value)


def _scrub_request(request: Request | None) -> None:
    if request is None:
        return
    state = request.scope.get("state")
    if type(state) is dict:
        body = state.pop(_BODY_STATE_KEY, None)
        if type(body) is bytearray:
            if body:
                body[:] = b"\x00" * len(body)
            body.clear()
        query = state.pop(_QUERY_STATE_KEY, None)
        if type(query) is dict:
            query.clear()
    if hasattr(request, "_body"):
        request._body = b""


def _fixed_response(status_code: int) -> Response:
    return Response(_FIXED_ERROR_BYTES, status_code=status_code, media_type="application/json")


def _json_response(value: object) -> Response:
    detached = detach_response_mapping(value)
    content = json.dumps(
        detached,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return Response(content, media_type="application/json")


def _parse_body(request: Request, model: type[Any]) -> Any:
    raw = b""
    try:
        raw = _request_bytes(request)
        return parse_request_bytes(raw, model)
    except (TypeError, ValueError, _HandlerRequestError):
        raise _HandlerRequestError() from None
    finally:
        raw = b""


def _visible_status(service: ControlPlaneService) -> dict[str, object]:
    raw: object = None
    status: StatusResponse | None = None
    visible_proposals: list[str] = []
    visible_cases: list[str] = []
    inbox: InboxResponse | None = None
    try:
        raw = service.status()
        status = StatusResponse.model_validate(raw)
        inbox = InboxResponse.model_validate(service.inbox())
        visible_proposals.extend(inbox.pending_proposal_ids)
        visible_cases.extend(inbox.open_case_ids)
        projected = status.model_dump(mode="json")
        projected["pending_proposal_ids"] = visible_proposals
        projected["open_case_ids"] = visible_cases
        if (
            status.status == "human_attention_required"
            and status.attention_route == "inbox"
            and not visible_proposals
            and not visible_cases
            and not inbox.clarification_sessions
        ):
            projected["status"] = "local_only"
            projected["attention_route"] = "home"
        return StatusResponse.model_validate(projected).model_dump(mode="json")
    finally:
        raw = None
        status = None
        inbox = None
        visible_proposals.clear()
        visible_cases.clear()


def _raise_signal(signal: BaseException | None) -> None:
    if signal is not None:
        raise signal.with_traceback(None)


def _scrub_enrichment_signal(error: BaseException) -> BaseException:
    old_traceback = error.__traceback__
    error.args = ()
    error.__dict__.clear()
    error.__traceback__ = None
    error.__cause__ = None
    error.__context__ = None
    if old_traceback is not None:
        traceback.clear_frames(old_traceback)
    old_traceback = None
    return error


def _proposal_id(request: Request) -> str:
    value = request.path_params.get("proposal_id")
    if type(value) is not str or _PROPOSAL_PATH.fullmatch(f"/api/v1/proposals/{value}") is None:
        raise _HandlerRequestError()
    return value


def _assessment_node_id(request: Request) -> str:
    value = request.path_params.get("node_id")
    if (
        type(value) is not str
        or _ASSESSMENT_NODE_PATH.fullmatch(f"{_ASSESSMENT_NODE_PREFIX}{value}") is None
        or len(value.encode("utf-8")) > 512
    ):
        raise _HandlerRequestError()
    return value


def _end_handler(
    request: Request | None,
    signal: BaseException | None,
    response: Response | None,
) -> Response:
    _scrub_request(request)
    request = None
    _raise_signal(signal)
    if response is None:
        return _fixed_response(503)
    return response


def _make_handlers(
    service: ControlPlaneService,
) -> dict[str, Callable[[Request], Awaitable[Response]]]:
    async def status_endpoint(request: Request) -> Response:
        response: Response | None = None
        signal: BaseException | None = None
        result: object = None
        try:
            result = _visible_status(service)
            response = _json_response(result)
        except Exception:  # noqa: BLE001 - fixed browser boundary
            response = _fixed_response(503)
        except BaseException as caught:  # noqa: BLE001 - scrub exact cancellation path
            caught.__traceback__ = None
            caught.__cause__ = None
            caught.__context__ = None
            signal = caught
        finally:
            result = None
        return _end_handler(request, signal, response)

    async def assessment_endpoint(request: Request) -> Response:
        response: Response | None = None
        signal: BaseException | None = None
        result: dict[str, object] | None = None
        detached: AssessmentResponse | None = None
        query: dict[str, str] = {}
        try:
            query = _request_query(request)
            result = service.assessment(
                query.get("focus"),
                page_cursor=query.get("cursor"),
            )
            detached = AssessmentResponse.model_validate(result)
            response = _json_response(detached.model_dump(mode="json"))
        except _HandlerRequestError:
            response = _fixed_response(400)
        except Exception:  # noqa: BLE001 - fixed browser boundary
            response = _fixed_response(503)
        except BaseException as caught:  # noqa: BLE001 - scrub exact cancellation path
            caught.__traceback__ = None
            caught.__cause__ = None
            caught.__context__ = None
            signal = caught
        finally:
            query.clear()
            result = None
            detached = None
        return _end_handler(request, signal, response)

    async def assessment_node_endpoint(request: Request) -> Response:
        response: Response | None = None
        signal: BaseException | None = None
        result: dict[str, object] | None = None
        detached: AssessmentNodeResponse | None = None
        node_id = ""
        try:
            node_id = _assessment_node_id(request)
            result = service.assessment_node(node_id)
            detached = AssessmentNodeResponse.model_validate(result)
            response = _json_response(detached.model_dump(mode="json"))
        except (_HandlerRequestError, LookupError):
            response = _fixed_response(404)
        except Exception:  # noqa: BLE001 - fixed browser boundary
            response = _fixed_response(503)
        except BaseException as caught:  # noqa: BLE001 - scrub exact cancellation path
            caught.__traceback__ = None
            caught.__cause__ = None
            caught.__context__ = None
            signal = caught
        finally:
            node_id = ""
            result = None
            detached = None
        return _end_handler(request, signal, response)

    async def enrichment_endpoint(request: Request) -> Response:
        response: Response | None = None
        signal: BaseException | None = None
        model: object = None
        result: dict[str, object] | None = None
        detached: object = None
        action = request.url.path.rpartition("/")[2]
        try:
            if action == "start":
                model = _parse_body(request, EnrichmentStartRequest)
                start = cast(EnrichmentStartRequest, model)
                result = service.enrichment_start(start.minutes, start.focus)
                detached = EnrichmentSessionResponse.model_validate(result)
            elif action == "current":
                model = _parse_body(request, EnrichmentSessionRequest)
                current = cast(EnrichmentSessionRequest, model)
                result = service.enrichment_current(current.session_id)
                detached = EnrichmentSessionResponse.model_validate(result)
            elif action == "answer":
                model = _parse_body(request, EnrichmentAnswerRequest)
                answered = cast(EnrichmentAnswerRequest, model)
                result = service.enrichment_answer(
                    answered.session_id,
                    answered.gap_id,
                    answered.answer,
                )
                detached = EnrichmentSessionResponse.model_validate(result)
            elif action == "skip":
                model = _parse_body(request, EnrichmentGapRequest)
                skipped = cast(EnrichmentGapRequest, model)
                result = service.enrichment_skip(skipped.session_id, skipped.gap_id)
                detached = EnrichmentSessionResponse.model_validate(result)
            elif action == "pause":
                model = _parse_body(request, EnrichmentSessionRequest)
                paused = cast(EnrichmentSessionRequest, model)
                result = service.enrichment_pause(paused.session_id)
                detached = EnrichmentSessionResponse.model_validate(result)
            elif action == "resume":
                model = _parse_body(request, EnrichmentSessionRequest)
                resumed = cast(EnrichmentSessionRequest, model)
                result = service.enrichment_resume(resumed.session_id)
                detached = EnrichmentSessionResponse.model_validate(result)
            elif action == "propose":
                model = _parse_body(request, EnrichmentProposalRequest)
                proposed = cast(EnrichmentProposalRequest, model)
                result = service.enrichment_propose(proposed.session_id, proposed.submission)
                detached = EnrichmentProposalResponse.model_validate(result)
            else:  # pragma: no cover - exact static routes only
                raise _HandlerRequestError()
            response = _json_response(cast(Any, detached).model_dump(mode="json"))
        except _HandlerRequestError:
            response = _fixed_response(400)
        except Exception:  # noqa: BLE001 - fixed browser boundary
            response = _fixed_response(503)
        except BaseException as caught:  # noqa: BLE001 - preserve cancellation identity
            signal = _scrub_enrichment_signal(caught)
        finally:
            action = ""
            if "answered" in locals():
                answered = cast(EnrichmentAnswerRequest, None)
            if "start" in locals():
                start = cast(EnrichmentStartRequest, None)
            if "current" in locals():
                current = cast(EnrichmentSessionRequest, None)
            if "skipped" in locals():
                skipped = cast(EnrichmentGapRequest, None)
            if "paused" in locals():
                paused = cast(EnrichmentSessionRequest, None)
            if "resumed" in locals():
                resumed = cast(EnrichmentSessionRequest, None)
            if "proposed" in locals():
                proposed = cast(EnrichmentProposalRequest, None)
            model = None
            result = None
            detached = None
        return _end_handler(request, signal, response)

    async def inbox_endpoint(request: Request) -> Response:
        response: Response | None = None
        signal: BaseException | None = None
        result: dict[str, object] | None = None
        try:
            result = InboxResponse.model_validate(service.inbox()).model_dump(mode="json")
            response = _json_response(result)
        except Exception:  # noqa: BLE001 - fixed browser boundary
            response = _fixed_response(503)
        except BaseException as caught:  # noqa: BLE001 - scrub exact cancellation path
            caught.__traceback__ = None
            caught.__cause__ = None
            caught.__context__ = None
            signal = caught
        finally:
            result = None
        return _end_handler(request, signal, response)

    async def development_observation_endpoint(request: Request) -> Response:
        response: Response | None = None
        signal: BaseException | None = None
        result: object = None
        try:
            result = service.development_observation()
            response = _json_response(result.model_dump(mode="json"))
        except Exception:  # noqa: BLE001 - fixed browser boundary
            response = _fixed_response(503)
        except BaseException as caught:  # noqa: BLE001 - scrub exact cancellation path
            caught.__traceback__ = None
            caught.__cause__ = None
            caught.__context__ = None
            signal = caught
        finally:
            result = None
        return _end_handler(request, signal, response)

    async def reviewed_tests_endpoint(request: Request) -> Response:
        response: Response | None = None
        signal: BaseException | None = None
        model: ReviewedTestRunRequest | None = None
        result: object = None
        try:
            model = cast(ReviewedTestRunRequest, _parse_body(request, ReviewedTestRunRequest))
            result = await service.run_reviewed_tests(model.command_id)
            response = _json_response(result.model_dump(mode="json"))
        except _HandlerRequestError:
            response = _fixed_response(400)
        except Exception:  # noqa: BLE001 - fixed browser boundary
            response = _fixed_response(503)
        except BaseException as caught:  # noqa: BLE001 - scrub exact cancellation path
            caught.__traceback__ = None
            caught.__cause__ = None
            caught.__context__ = None
            signal = caught
        finally:
            model = None
            result = None
        return _end_handler(request, signal, response)

    async def proposal_endpoint(request: Request) -> Response:
        response: Response | None = None
        signal: BaseException | None = None
        result: object = None
        proposal_id = ""
        try:
            proposal_id = _proposal_id(request)
            result = service.proposal_preview(proposal_id)
            response = _json_response(result)
        except _HandlerRequestError:
            response = _fixed_response(404)
        except Exception:  # noqa: BLE001 - hidden and unavailable remain identical
            response = _fixed_response(503)
        except BaseException as caught:  # noqa: BLE001 - scrub exact cancellation path
            caught.__traceback__ = None
            caught.__cause__ = None
            caught.__context__ = None
            signal = caught
        finally:
            proposal_id = ""
            result = None
        return _end_handler(request, signal, response)

    async def clarification_answer_preview_endpoint(request: Request) -> Response:
        response: Response | None = None
        signal: BaseException | None = None
        model: ClarificationAnswerPreviewRequest | None = None
        result: object = None
        detached: ClarificationAnswerPreviewResponse | None = None
        try:
            model = cast(
                ClarificationAnswerPreviewRequest,
                _parse_body(request, ClarificationAnswerPreviewRequest),
            )
            result = service.answer_preview(model.session_id, model.question_id, model.answer)
            detached = ClarificationAnswerPreviewResponse.model_validate(result)
            response = _json_response(detached.model_dump(mode="json"))
        except _HandlerRequestError:
            response = _fixed_response(400)
        except Exception:  # noqa: BLE001 - fixed browser boundary
            response = _fixed_response(503)
        except BaseException as caught:  # noqa: BLE001 - scrub exact cancellation path
            caught.__traceback__ = None
            caught.__cause__ = None
            caught.__context__ = None
            signal = caught
        finally:
            model = None
            result = None
            detached = None
        return _end_handler(request, signal, response)

    async def clarification_answer_discard_endpoint(request: Request) -> Response:
        response: Response | None = None
        signal: BaseException | None = None
        model: ClarificationAnswerDiscardRequest | None = None
        result: object = None
        detached: ClarificationAnswerDiscardResponse | None = None
        try:
            model = cast(
                ClarificationAnswerDiscardRequest,
                _parse_body(request, ClarificationAnswerDiscardRequest),
            )
            result = service.discard_answer_preview(model.answer_id)
            detached = ClarificationAnswerDiscardResponse.model_validate(result)
            response = _json_response(detached.model_dump(mode="json"))
        except _HandlerRequestError:
            response = _fixed_response(400)
        except Exception:  # noqa: BLE001 - fixed browser boundary
            response = _fixed_response(503)
        except BaseException as caught:  # noqa: BLE001 - scrub exact cancellation path
            caught.__traceback__ = None
            caught.__cause__ = None
            caught.__context__ = None
            signal = caught
        finally:
            model = None
            result = None
            detached = None
        return _end_handler(request, signal, response)

    async def registration_options_endpoint(request: Request) -> Response:
        response: Response | None = None
        signal: BaseException | None = None
        options = b""
        try:
            _parse_body(request, RegistrationOptionsRequest)
            options = service.registration_options()
            response = _json_response(parse_response_bytes(options))
        except _HandlerRequestError:
            response = _fixed_response(400)
        except Exception:  # noqa: BLE001 - fixed browser boundary
            response = _fixed_response(503)
        except BaseException as caught:  # noqa: BLE001 - scrub exact cancellation path
            caught.__traceback__ = None
            caught.__cause__ = None
            caught.__context__ = None
            signal = caught
        finally:
            options = b""
        return _end_handler(request, signal, response)

    async def registration_verify_endpoint(request: Request) -> Response:
        response: Response | None = None
        signal: BaseException | None = None
        model: RegistrationVerifyRequest | None = None
        encoded_response = b""
        credential: CredentialRecord | None = None
        try:
            model = cast(
                RegistrationVerifyRequest,
                _parse_body(request, RegistrationVerifyRequest),
            )
            encoded_response = canonical_json_object(model.response)
            credential = service.register(encoded_response)
            if type(credential) is not CredentialRecord:
                raise ValueError("invalid registration result")
            detached = CredentialRecord.model_validate_json(credential.model_dump_json())
            response = _json_response(detached.model_dump(mode="json"))
        except _HandlerRequestError:
            response = _fixed_response(400)
        except Exception:  # noqa: BLE001 - fixed browser boundary
            response = _fixed_response(503)
        except BaseException as caught:  # noqa: BLE001 - scrub exact cancellation path
            caught.__traceback__ = None
            caught.__cause__ = None
            caught.__context__ = None
            signal = caught
        finally:
            model = None
            encoded_response = b""
            credential = None
            if "detached" in locals():
                detached = cast(CredentialRecord, None)
        return _end_handler(request, signal, response)

    async def github_setup_endpoint(request: Request) -> Response:
        response: Response | None = None
        signal: BaseException | None = None
        encoded = b""
        model: (
            RegistrationOptionsRequest | DecisionOptionsRequest | DecisionVerifyRequest | None
        ) = None
        result: dict[str, object] | None = None
        try:
            if request.url.path == "/api/v1/team/setup":
                result = service.github_setup_status()
            else:
                action = request.url.path.rsplit("/", 1)[-1]
                if action == "options":
                    model = cast(
                        DecisionOptionsRequest, _parse_body(request, DecisionOptionsRequest)
                    )
                    result = await service.github_setup_action(action, payload=model.payload)
                elif action == "verify":
                    model = cast(DecisionVerifyRequest, _parse_body(request, DecisionVerifyRequest))
                    encoded = canonical_json_object(model.response)
                    result = await service.github_setup_action(
                        action, payload=model.payload, response=encoded
                    )
                else:
                    model = cast(
                        RegistrationOptionsRequest, _parse_body(request, RegistrationOptionsRequest)
                    )
                    result = await service.github_setup_action(action)
            response = _json_response(result)
        except _HandlerRequestError:
            response = _fixed_response(400)
        except Exception:  # noqa: BLE001 - fixed browser boundary
            response = _fixed_response(503)
        except BaseException as caught:  # noqa: BLE001 - preserve scrubbed cancellation
            caught.__traceback__ = None
            caught.__cause__ = None
            caught.__context__ = None
            signal = caught
        finally:
            encoded = b""
            model = None
            result = None
        return _end_handler(request, signal, response)

    async def team_enrollment_status_endpoint(request: Request) -> Response:
        response: Response | None = None
        signal: BaseException | None = None
        result: dict[str, object] | None = None
        try:
            result = service.team_enrollment_status()
            response = _json_response(result)
        except Exception:  # noqa: BLE001 - fixed browser boundary
            response = _fixed_response(503)
        except BaseException as caught:  # noqa: BLE001 - scrub exact cancellation path
            caught.__traceback__ = None
            caught.__cause__ = None
            caught.__context__ = None
            signal = caught
        finally:
            result = None
        return _end_handler(request, signal, response)

    async def team_publication_preview_endpoint(request: Request) -> Response:
        response: Response | None = None
        signal: BaseException | None = None
        result: dict[str, object] | None = None
        try:
            result = service.team_publication_preview()
            response = _json_response(result)
        except Exception:  # noqa: BLE001 - fixed browser boundary
            response = _fixed_response(503)
        except BaseException as caught:  # noqa: BLE001 - preserve cancellation identity
            caught.__traceback__ = None
            caught.__cause__ = None
            caught.__context__ = None
            signal = caught
        finally:
            result = None
        return _end_handler(request, signal, response)

    async def team_enrollment_options_endpoint(request: Request) -> Response:
        response: Response | None = None
        signal: BaseException | None = None
        model: TeamEnrollmentOptionsRequest | None = None
        proof = b""
        options = b""
        try:
            model = cast(
                TeamEnrollmentOptionsRequest,
                _parse_body(request, TeamEnrollmentOptionsRequest),
            )
            proof = model.identity_proof.encode("utf-8")
            options = service.team_enrollment_options(proof)
            response = _json_response(parse_response_bytes(options))
        except _HandlerRequestError:
            response = _fixed_response(400)
        except Exception:  # noqa: BLE001 - fixed browser boundary
            response = _fixed_response(503)
        except BaseException as caught:  # noqa: BLE001 - scrub exact cancellation path
            caught.__traceback__ = None
            caught.__cause__ = None
            caught.__context__ = None
            signal = caught
        finally:
            model = None
            proof = b""
            options = b""
        return _end_handler(request, signal, response)

    async def team_enrollment_verify_endpoint(request: Request) -> Response:
        response: Response | None = None
        signal: BaseException | None = None
        model: TeamEnrollmentVerifyRequest | None = None
        encoded_response = b""
        recipient: TeamRecipientRecord | None = None
        try:
            model = cast(
                TeamEnrollmentVerifyRequest,
                _parse_body(request, TeamEnrollmentVerifyRequest),
            )
            encoded_response = canonical_json_object(model.response)
            enrolled = service.complete_team_enrollment(encoded_response)
            recipient = TeamRecipientRecord.model_validate(enrolled.model_dump(mode="python"))
            response = _json_response(recipient.model_dump(mode="json"))
        except _HandlerRequestError:
            response = _fixed_response(400)
        except Exception:  # noqa: BLE001 - fixed browser boundary
            response = _fixed_response(503)
        except BaseException as caught:  # noqa: BLE001 - scrub exact cancellation path
            caught.__traceback__ = None
            caught.__cause__ = None
            caught.__context__ = None
            signal = caught
        finally:
            model = None
            encoded_response = b""
            recipient = None
            if "enrolled" in locals():
                enrolled = cast(TeamRecipientRecord, None)
        return _end_handler(request, signal, response)

    async def team_enrollment_cancel_endpoint(request: Request) -> Response:
        response: Response | None = None
        signal: BaseException | None = None
        result: dict[str, object] | None = None
        try:
            _parse_body(request, TeamEnrollmentCancelRequest)
            result = service.cancel_team_enrollment()
            response = _json_response(result)
        except _HandlerRequestError:
            response = _fixed_response(400)
        except Exception:  # noqa: BLE001 - fixed browser boundary
            response = _fixed_response(503)
        except BaseException as caught:  # noqa: BLE001 - scrub exact cancellation path
            caught.__traceback__ = None
            caught.__cause__ = None
            caught.__context__ = None
            signal = caught
        finally:
            result = None
        return _end_handler(request, signal, response)

    async def decision_options_endpoint(request: Request) -> Response:
        response: Response | None = None
        signal: BaseException | None = None
        model: DecisionOptionsRequest | None = None
        options = b""
        try:
            model = cast(DecisionOptionsRequest, _parse_body(request, DecisionOptionsRequest))
            options = service.decision_options(model.payload)
            response = _json_response(parse_response_bytes(options))
        except _HandlerRequestError:
            response = _fixed_response(400)
        except Exception:  # noqa: BLE001 - fixed browser boundary
            response = _fixed_response(503)
        except BaseException as caught:  # noqa: BLE001 - scrub exact cancellation path
            caught.__traceback__ = None
            caught.__cause__ = None
            caught.__context__ = None
            signal = caught
        finally:
            model = None
            options = b""
        return _end_handler(request, signal, response)

    async def decision_verify_endpoint(request: Request) -> Response:
        response: Response | None = None
        signal: BaseException | None = None
        model: DecisionVerifyRequest | None = None
        encoded_response = b""
        result: object = None
        try:
            model = cast(DecisionVerifyRequest, _parse_body(request, DecisionVerifyRequest))
            encoded_response = canonical_json_object(model.response)
            result = service.apply_decision(encoded_response, model.payload)
            response = _json_response(result)
        except _HandlerRequestError:
            response = _fixed_response(400)
        except Exception:  # noqa: BLE001 - fixed browser boundary
            response = _fixed_response(503)
        except BaseException as caught:  # noqa: BLE001 - scrub exact cancellation path
            caught.__traceback__ = None
            caught.__cause__ = None
            caught.__context__ = None
            signal = caught
        finally:
            model = None
            encoded_response = b""
            result = None
        return _end_handler(request, signal, response)

    return {
        "assessment": assessment_endpoint,
        "assessment_node": assessment_node_endpoint,
        "enrichment": enrichment_endpoint,
        "status": status_endpoint,
        "inbox": inbox_endpoint,
        "development_observation": development_observation_endpoint,
        "reviewed_tests": reviewed_tests_endpoint,
        "proposal": proposal_endpoint,
        "clarification_answer_preview": clarification_answer_preview_endpoint,
        "clarification_answer_discard": clarification_answer_discard_endpoint,
        "registration_options": registration_options_endpoint,
        "registration_verify": registration_verify_endpoint,
        "team_enrollment_status": team_enrollment_status_endpoint,
        "github_setup": github_setup_endpoint,
        "team_enrollment_options": team_enrollment_options_endpoint,
        "team_enrollment_verify": team_enrollment_verify_endpoint,
        "team_enrollment_cancel": team_enrollment_cancel_endpoint,
        "team_publication_preview": team_publication_preview_endpoint,
        "decision_options": decision_options_endpoint,
        "decision_verify": decision_verify_endpoint,
    }


def build_control_plane_app(
    service: ControlPlaneService,
    *,
    origin: str,
    csrf_secret: str,
) -> Starlette:
    """Build the only browser-facing API over one held control-plane service."""
    configuration: tuple[str, int, str] | None = None
    invalid = False
    try:
        configuration = _valid_configuration(origin, csrf_secret)
    except Exception:  # noqa: BLE001 - one fixed configuration boundary
        invalid = True
    if invalid or configuration is None:
        service = cast(ControlPlaneService, None)
        origin = csrf_secret = ""
        raise ValueError("invalid control plane HTTP configuration") from None
    expected_host, port, validated_csrf = configuration
    handlers = _make_handlers(service)
    app = Starlette(
        debug=False,
        routes=[
            Route("/api/v1/assessment", handlers["assessment"], methods=["GET"]),
            Route(
                "/api/v1/assessment/nodes/{node_id}",
                handlers["assessment_node"],
                methods=["GET"],
            ),
            *[
                Route(
                    f"/api/v1/enrichment/{action}",
                    handlers["enrichment"],
                    methods=["POST"],
                )
                for action in ("start", "current", "answer", "skip", "pause", "resume", "propose")
            ],
            Route("/api/v1/team/setup", handlers["github_setup"], methods=["GET"]),
            *[
                Route(f"/api/v1/team/setup/{action}", handlers["github_setup"], methods=["POST"])
                for action in (
                    "inspect",
                    "enroll",
                    "protection-preview",
                    "publication-preview",
                    "options",
                    "verify",
                    "cancel",
                )
            ],
            Route("/api/v1/status", handlers["status"], methods=["GET"]),
            Route("/api/v1/inbox", handlers["inbox"], methods=["GET"]),
            Route(
                "/api/v1/development/observation",
                handlers["development_observation"],
                methods=["GET"],
            ),
            Route(
                "/api/v1/development/tests/run",
                handlers["reviewed_tests"],
                methods=["POST"],
            ),
            Route(
                "/api/v1/proposals/{proposal_id}",
                handlers["proposal"],
                methods=["GET"],
            ),
            Route(
                "/api/v1/clarifications/answers/preview",
                handlers["clarification_answer_preview"],
                methods=["POST"],
            ),
            Route(
                "/api/v1/clarifications/answers/discard",
                handlers["clarification_answer_discard"],
                methods=["POST"],
            ),
            Route(
                "/api/v1/webauthn/register/options",
                handlers["registration_options"],
                methods=["POST"],
            ),
            Route(
                "/api/v1/webauthn/register/verify",
                handlers["registration_verify"],
                methods=["POST"],
            ),
            Route(
                "/api/v1/team/enrollment",
                handlers["team_enrollment_status"],
                methods=["GET"],
            ),
            Route(
                "/api/v1/team/enrollment/options",
                handlers["team_enrollment_options"],
                methods=["POST"],
            ),
            Route(
                "/api/v1/team/enrollment/verify",
                handlers["team_enrollment_verify"],
                methods=["POST"],
            ),
            Route(
                "/api/v1/team/enrollment/cancel",
                handlers["team_enrollment_cancel"],
                methods=["POST"],
            ),
            Route(
                "/api/v1/team/publication/preview",
                handlers["team_publication_preview"],
                methods=["GET"],
            ),
            Route(
                "/api/v1/decisions/options",
                handlers["decision_options"],
                methods=["POST"],
            ),
            Route(
                "/api/v1/decisions/verify",
                handlers["decision_verify"],
                methods=["POST"],
            ),
        ],
        middleware=[
            Middleware(
                _StrictLoopbackMiddleware,
                expected_host=expected_host,
                expected_origin=origin,
                csrf_secret=validated_csrf,
            )
        ],
    )
    app.state.bind_host = "127.0.0.1"
    app.state.bind_port = port
    app.state.origin = origin
    return app


__all__ = ["build_control_plane_app"]
