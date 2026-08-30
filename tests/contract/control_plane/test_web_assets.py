"""Contract coverage for the packaged, local-only review UI assets."""

from __future__ import annotations

from html.parser import HTMLParser
from importlib.resources import files


class _Document(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.attributes: list[dict[str, str | None]] = []

    def handle_starttag(self, _tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.attributes.append(dict(attrs))


def _assets() -> tuple[str, str, str]:
    directory = files("intent_engineering.control_plane").joinpath("assets")
    return (
        directory.joinpath("index.html").read_text(encoding="utf-8"),
        directory.joinpath("app.js").read_text(encoding="utf-8"),
        directory.joinpath("styles.css").read_text(encoding="utf-8"),
    )


def test_packaged_ui_exposes_five_keyboard_navigable_accessible_views() -> None:
    """Catches an asset omission or a UI that leaves review locations unreachable."""
    html, _javascript, css = _assets()
    document = _Document()
    document.feed(html)

    navigation = {
        attributes["data-view"]
        for attributes in document.attributes
        if attributes.get("data-view") is not None
    }

    assert navigation == {"home", "onboarding", "inbox", "proposal", "team_state"}
    assert any(attributes.get("aria-live") == "polite" for attributes in document.attributes)
    assert any(attributes.get("id") == "app" for attributes in document.attributes)
    assert "focus-visible" in css


def test_browser_bundle_uses_the_public_api_and_never_persists_sensitive_material() -> None:
    """Catches a browser boundary bypass or a credential/evidence persistence regression."""
    _html, javascript, _css = _assets()

    for route in (
        "/api/v1/status",
        "/api/v1/inbox",
        "/api/v1/proposals/",
        "/api/v1/clarifications/answers/preview",
        "/api/v1/clarifications/answers/discard",
        "/api/v1/webauthn/register/options",
        "/api/v1/webauthn/register/verify",
        "/api/v1/decisions/options",
        "/api/v1/decisions/verify",
    ):
        assert route in javascript
    assert "function fetchJson" in javascript
    assert "navigator.credentials.create" in javascript
    assert "navigator.credentials.get" in javascript
    assert 'userVerification = "required"' in javascript
    assert "textContent" in javascript
    assert "innerHTML" not in javascript
    assert "localStorage" not in javascript
    assert "sessionStorage" not in javascript
    assert "console." not in javascript
    assert "history.replaceState" in javascript
    assert "encodeURIComponent" in javascript
    assert 'headers.set("origin"' not in javascript.lower()


def test_browser_bundle_clears_sensitive_ceremony_references_in_finally_blocks() -> None:
    """Catches an abandoned or completed WebAuthn ceremony retaining browser-side material."""
    _html, javascript, _css = _assets()

    assert "function clearBuffer" in javascript
    assert javascript.count("finally {") >= 2
    assert "credential = null" in javascript
    assert "options = null" in javascript
    assert "payload = null" in javascript
    assert "app.replaceChildren(next)" in javascript


def test_authorization_is_explicitly_destructive_and_cancel_is_safe() -> None:
    """Catches an ambiguous authority-grant label or a cancel button styled as destructive."""
    _html, javascript, css = _assets()

    assert "Authorize ${decisionLabel(payload)} with WebAuthn" in javascript
    assert 'actionButton("Cancel review without applying a decision", cancelReview)' in javascript
    assert "const authorize = actionButton(" in javascript
    assert "authorizeDecision," in javascript
    assert '      "danger"' in javascript
    assert ".danger" in css
