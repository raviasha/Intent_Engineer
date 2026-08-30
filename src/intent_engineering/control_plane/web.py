"""Strict loopback-only Starlette API for local human review."""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Awaitable, Callable
from typing import Any, cast
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from intent_engineering.control_plane.http_models import (
    MAX_HTTP_BODY_BYTES,
    DecisionOptionsRequest,
    DecisionVerifyRequest,
    InboxResponse,
    RegistrationOptionsRequest,
    RegistrationVerifyRequest,
    StatusResponse,
    canonical_json_object,
    detach_response_mapping,
    parse_request_bytes,
    parse_response_bytes,
)
from intent_engineering.control_plane.models import CredentialRecord
from intent_engineering.control_plane.service import ControlPlaneService

_BODY_STATE_KEY = "intent.control-plane.raw-body"
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
    "/api/v1/status": "GET",
    "/api/v1/inbox": "GET",
    "/api/v1/webauthn/register/options": "POST",
    "/api/v1/webauthn/register/verify": "POST",
    "/api/v1/decisions/options": "POST",
    "/api/v1/decisions/verify": "POST",
}
_PROPOSAL_PATH = re.compile(r"^/api/v1/proposals/([A-Za-z0-9:._-]{1,512})$")
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
    raise _HttpBoundaryError(404)


def _validate_scope_and_headers(
    scope: Scope,
    *,
    expected_host: bytes,
    expected_origin: bytes,
    csrf_secret: bytes,
) -> tuple[list[tuple[bytes, bytes]], int | None]:
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
            or query
        ):
            raise _HttpBoundaryError(400)
        if type(scheme) is not str or scheme != "http":
            raise _HttpBoundaryError(403)
        try:
            if raw_path != path.encode("ascii"):
                raise _HttpBoundaryError(404)
        except UnicodeError:
            raise _HttpBoundaryError(404) from None
        required_method = _expected_method(path)
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
        origin = _single_header(headers, b"origin", status_code=403)
        if not secrets.compare_digest(host, expected_host) or not secrets.compare_digest(
            origin, expected_origin
        ):
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
        return headers, length
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
        trusted = False
        failure_status: int | None = None
        try:
            headers, length = _validate_scope_and_headers(
                scope,
                expected_host=self._expected_host,
                expected_origin=self._expected_origin,
                csrf_secret=self._csrf_bytes,
            )
            trusted = True
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
            if _BODY_STATE_KEY in state:
                raise _HttpBoundaryError(400)
            state[_BODY_STATE_KEY] = body
            secured_send = _secure_sender(send, self._csrf_secret, set_cookie=True)
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
                set_cookie=trusted,
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
    preview: object = None
    try:
        raw = service.status()
        status = StatusResponse.model_validate(raw)
        for identifier, destination in (
            *((item, visible_proposals) for item in status.pending_proposal_ids),
            *((item, visible_cases) for item in status.open_case_ids),
        ):
            try:
                preview = service.proposal_preview(identifier)
                detach_response_mapping(preview)
            except Exception:  # noqa: BLE001, S112 - hidden and unavailable are identical
                continue
            destination.append(identifier)
            preview = None
        projected = status.model_dump(mode="json")
        projected["pending_proposal_ids"] = visible_proposals
        projected["open_case_ids"] = visible_cases
        if (
            status.status == "human_attention_required"
            and status.attention_route == "inbox"
            and not visible_proposals
            and not visible_cases
        ):
            projected["status"] = "local_only"
            projected["attention_route"] = "home"
        return StatusResponse.model_validate(projected).model_dump(mode="json")
    finally:
        raw = preview = None
        status = None
        visible_proposals.clear()
        visible_cases.clear()


def _raise_signal(signal: BaseException | None) -> None:
    if signal is not None:
        raise signal.with_traceback(None)


def _proposal_id(request: Request) -> str:
    value = request.path_params.get("proposal_id")
    if type(value) is not str or _PROPOSAL_PATH.fullmatch(f"/api/v1/proposals/{value}") is None:
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

    async def inbox_endpoint(request: Request) -> Response:
        response: Response | None = None
        signal: BaseException | None = None
        result: dict[str, object] | None = None
        try:
            status = StatusResponse.model_validate(_visible_status(service))
            result = InboxResponse(
                pending_proposal_ids=status.pending_proposal_ids,
                open_case_ids=status.open_case_ids,
            ).model_dump(mode="json")
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
            if "status" in locals():
                status = cast(StatusResponse, None)
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
        "status": status_endpoint,
        "inbox": inbox_endpoint,
        "proposal": proposal_endpoint,
        "registration_options": registration_options_endpoint,
        "registration_verify": registration_verify_endpoint,
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
            Route("/api/v1/status", handlers["status"], methods=["GET"]),
            Route("/api/v1/inbox", handlers["inbox"], methods=["GET"]),
            Route(
                "/api/v1/proposals/{proposal_id}",
                handlers["proposal"],
                methods=["GET"],
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
