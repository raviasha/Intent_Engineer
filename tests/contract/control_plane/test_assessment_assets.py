"""Contracts for the packaged synchronized assessment experience."""

from __future__ import annotations

import re
from importlib.resources import files


def _assets() -> tuple[str, str, str]:
    directory = files("intent_engineering.control_plane").joinpath("assets")
    return (
        directory.joinpath("index.html").read_text(encoding="utf-8"),
        directory.joinpath("app.js").read_text(encoding="utf-8"),
        directory.joinpath("styles.css").read_text(encoding="utf-8"),
    )


def _rgb(value: str) -> tuple[float, float, float]:
    channels = tuple(int(value[index : index + 2], 16) / 255 for index in (1, 3, 5))
    return tuple(
        channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4
        for channel in channels
    )


def _contrast(foreground: str, background: str) -> float:
    foreground_luminance = sum(
        weight * channel for weight, channel in zip((0.2126, 0.7152, 0.0722), _rgb(foreground))
    )
    background_luminance = sum(
        weight * channel for weight, channel in zip((0.2126, 0.7152, 0.0722), _rgb(background))
    )
    lighter = max(foreground_luminance, background_luminance)
    darker = min(foreground_luminance, background_luminance)
    return (lighter + 0.05) / (darker + 0.05)


def test_packaged_assessment_uses_one_sealed_server_scorecard_state() -> None:
    """Catches graph, table, or detail recomputing or copying assessment scores."""
    html, javascript, _css = _assets()

    assert "Open graph assessment" in javascript
    assert 'assessment: "/api/v1/assessment"' in javascript
    assert "const assessmentState = Object.seal({" in javascript
    assert "selectedNodeId: null" in javascript
    assert 'overlay: "approved"' in javascript
    assert "pageRows: []" in javascript
    assert "assessmentState.report.nodes" in javascript
    assert "assessmentState.pageRows" in javascript
    assert "assessmentNode(assessmentState.selectedNodeId)" in javascript
    assert "assessment-node-${index}" in javascript
    assert "assessment-row-${index}" in javascript
    assert "focusAssessmentRepresentation" in javascript
    assert "?focus=${encodeURIComponent(focus)}" in javascript
    assert "acceptAssessmentResponse(await fetchJson(path), focus)" in javascript
    assert "payload.focus.reference !== expectedFocus" in javascript
    assert '"Projected health unavailable"' in javascript
    assert "Worst dimension" in javascript
    assert "Duplicate assessment page row." in javascript
    assert 'item.addEventListener("keydown"' not in javascript
    assert (
        'row.addEventListener("keydown", assessmentSelectionHandler(node, "table"))' in javascript
    )
    assert "innerHTML" not in javascript
    assert "localStorage" not in javascript
    assert "sessionStorage" not in javascript
    assert "console." not in javascript
    assert '<script src="app.js" defer></script>' in html


def test_assessment_health_styles_have_accessible_text_contrast_and_focus() -> None:
    """Catches color-only status or health palettes that become unreadable."""
    _html, javascript, css = _assets()

    variables = dict(re.findall(r"(--health-[a-z-]+):\s*(#[0-9a-fA-F]{6})", css))
    for health in ("green", "orange", "red", "unassessed"):
        foreground = variables[f"--health-{health}-text"]
        background = variables[f"--health-{health}-background"]
        assert _contrast(foreground, background) >= 4.5

    for label, icon in (
        ("Green", "✓"),
        ("Orange", "!"),
        ("Red", "×"),
        ("Unassessed", "?"),
    ):
        assert f'label: "{label}"' in javascript
        assert f'icon: "{icon}"' in javascript
    assert ".assessment-graph-node:focus-visible" in css
    assert ".assessment-table-row:focus-visible" in css
    assert "text-decoration" in css


def test_assessment_client_declares_server_bounds_and_no_authority_state() -> None:
    """Catches unbounded rendering or assessment state retaining mutation authority."""
    _html, javascript, _css = _assets()

    assert "const MAX_ASSESSMENT_NODES = 2000;" in javascript
    assert "const MAX_ASSESSMENT_ROWS = 100;" in javascript
    assert "const MAX_ASSESSMENT_DIMENSIONS = 7;" in javascript
    assert "const MAX_ASSESSMENT_CHECKS = 64;" in javascript
    assert "boundedAssessmentArray" in javascript
    state_literal = javascript.split("const assessmentState = Object.seal({", 1)[1].split("});", 1)[
        0
    ]
    assert "token" not in state_literal.casefold()
    assert "authorization" not in state_literal.casefold()
    assert "capability" not in state_literal.casefold()
