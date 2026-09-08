"""Deterministic execution coverage for the shipped browser review asset."""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from importlib.resources import files
from typing import Any, cast

import pytest
import uvicorn

from intent_engineering.cli.dev import _ControlPlaneSite
from intent_engineering.control_plane import build_control_plane_app

NODE = shutil.which("node")


def _asset() -> str:
    return (
        files("intent_engineering.control_plane")
        .joinpath("assets", "app.js")
        .read_text(encoding="utf-8")
    )


_HARNESS = r"""
class Element {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.attributes = new Map();
    this.handlers = new Map();
    this.dataset = {};
    this.className = "";
    this.id = "";
    this.type = "";
    this.checked = false;
    this.disabled = false;
    this.value = "";
    this.maxLength = 0;
    this.required = false;
    this._text = "";
  }
  get textContent() {
    return this._text + this.children.map((child) => child.textContent || "").join("");
  }
  set textContent(value) {
    this._text = String(value);
    this.children = [];
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; this._text = ""; }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  removeAttribute(name) { this.attributes.delete(name); }
  addEventListener(name, handler) { this.handlers.set(name, handler); }
  click() { return this.handlers.get("click")?.(); }
  focus() { globalThis.focused = this; }
}
class TextNode { constructor(value) { this.textContent = String(value); } }
const app = new Element("main");
const status = new Element("p");
const nav = ["home", "onboarding", "inbox", "proposal", "team_state"].map((view) => {
  const button = new Element("button"); button.dataset.view = view; return button;
});
globalThis.document = {
  getElementById(id) { return id === "app" ? app : status; },
  querySelectorAll() { return nav; },
  createElement(tag) { return new Element(tag); },
  createTextNode(value) { return new TextNode(value); },
};
globalThis.window = { location: { hash: "#csrf=csrf-token", pathname: "/" } };
globalThis.location = globalThis.window.location;
globalThis.history = { replaceState() {} };
globalThis.Headers = class { constructor() { this.values = new Map(); } set(k, v) { this.values.set(k, v); } };
globalThis.atob = (value) => Buffer.from(value, "base64").toString("binary");
globalThis.btoa = (value) => Buffer.from(value, "binary").toString("base64");
const pending = [];
const calls = [];
globalThis.fetch = (path, init = {}) => new Promise((resolve) => {
  const request = { path, init, resolve };
  pending.push(request); calls.push(request);
});
function respond(request, value, status = 200) {
  request.resolve({ ok: status >= 200 && status < 300, status, text: async () => JSON.stringify(value) });
}
function take(path) {
  const index = pending.findIndex((request) => request.path === path);
  if (index < 0) throw new Error(`No pending request for ${path}; got ${pending.map((request) => request.path)}`);
  return pending.splice(index, 1)[0];
}
async function settle() { for (let index = 0; index < 8; index += 1) await Promise.resolve(); }
function walk(node, items = []) { items.push(node); for (const child of node.children || []) walk(child, items); return items; }
function button(label) {
  const result = walk(app).find((node) => node.tagName === "button" && node.textContent === label);
  if (!result) throw new Error(`Missing button: ${label}; app=${app.textContent}`);
  return result;
}
function credential(id) {
  const bytes = (value) => Uint8Array.from(Buffer.from(value)).buffer;
  return { id, rawId: bytes(id), type: "public-key", response: {
    authenticatorData: bytes("auth"), clientDataJSON: bytes("client"), signature: bytes("sig"), userHandle: null,
  }};
}
let lastCreateOptions = null;
let lastGetOptions = null;
let getCredential = () => Promise.resolve(credential("assertion"));
let createCredential = () => Promise.resolve(credential("registration"));
Object.defineProperty(globalThis, "navigator", { configurable: true, value: { credentials: {
  get(options) { lastGetOptions = options; return getCredential(options); },
  create(options) { lastCreateOptions = options; return createCredential(options); },
}}});
const statusProjection = { schema_version: 1, status: "human_attention_required", attention_route: "inbox", project_id: "project", repository_id: "repo", graph_version: 3, pending_proposal_ids: ["proposal:a", "proposal:b"], open_case_ids: [] };
function proposal(identifier, action = "confirm_proposal", selectedNodeIds = [`node:${identifier}`]) {
  const kind = action === "resolve_conflict" ? "case" : action === "approve_external_write" ? "write-plan" : action === "answer_clarification" ? "answer" : "proposal";
  return { schema_version: 1, preview_digest: `sha256:${identifier}`, preview: { evidence: `PRIVATE-${identifier}`, affected_nodes: [`node:${identifier}`] }, payload: {
    schema_version: 1, project_id: "project", repository_id: "repo", actor: "local:owner", action,
    graph_version: 3, parent_bundle_digest: "parent", subject: { kind, id: identifier }, subject_digest: "subject",
    selected_node_ids: selectedNodeIds, result_digest: "sha256:result", challenge: "challenge", issued_at: "now", expires_at: "later",
  }};
}
"""


def _run(scenario: str) -> dict[str, object]:
    """Execute the shipped browser bundle with a deterministic, test-only DOM/runtime."""
    if NODE is None:
        pytest.skip("Node runtime is unavailable for deterministic browser-asset execution")
    completed = subprocess.run(
        [NODE, "--input-type=module"],
        input=f"{_HARNESS}\n{_asset()}\n{scenario}",
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_shipped_browser_ignores_out_of_order_proposal_responses_and_binds_authorization() -> None:
    """Catches an A preview overwriting B or an authorization sending A after B was selected."""
    result = _run(
        r"""
await settle(); respond(take("/api/v1/status"), statusProjection); await settle();
nav.find((item) => item.dataset.view === "inbox").click(); await settle();
respond(take("/api/v1/inbox"), { schema_version: 1, pending_proposal_ids: ["proposal:a", "proposal:b"], open_case_ids: [] }); await settle();
button("Review proposal:a").click(); await settle();
const a = take("/api/v1/proposals/proposal%3Aa");
nav.find((item) => item.dataset.view === "inbox").click(); await settle();
respond(take("/api/v1/inbox"), { schema_version: 1, pending_proposal_ids: ["proposal:a", "proposal:b"], open_case_ids: [] }); await settle();
button("Review proposal:b").click(); await settle();
const b = take("/api/v1/proposals/proposal%3Ab");
respond(b, proposal("proposal:b")); await settle();
respond(a, proposal("proposal:a")); await settle();
const previewAfterA = app.textContent;
const authorize = button("Authorize confirm proposal proposal:b with WebAuthn");
const cancel = button("Cancel review without applying a decision");
authorize.click(); await settle();
const options = take("/api/v1/decisions/options");
const boundPayload = JSON.parse(options.init.body).payload;
respond(options, { publicKey: { challenge: "Y2hhbGxlbmdl", allowCredentials: [{ id: "Y3JlZGVudGlhbA", type: "public-key" }] } }); await settle();
const verify = take("/api/v1/decisions/verify");
const verifyPayload = JSON.parse(verify.init.body).payload;
process.stdout.write(JSON.stringify({
  previewAfterA, boundPayload, verifyPayload, authorizeClass: authorize.className,
  cancelClass: cancel.className, cancelLabel: cancel.textContent,
}));
"""
    )

    assert "PRIVATE-proposal:b" in result["previewAfterA"]
    assert "PRIVATE-proposal:a" not in result["previewAfterA"]
    assert result["boundPayload"]["subject"]["id"] == "proposal:b"
    assert result["verifyPayload"]["subject"]["id"] == "proposal:b"
    assert result["authorizeClass"] == "danger"
    assert result["cancelClass"] == ""
    assert result["cancelLabel"] == "Cancel review without applying a decision"


def test_shipped_browser_registers_with_converted_options_and_clears_credential_material() -> None:
    """Catches a registration flow that bypasses WebAuthn conversion or renders credential material."""
    result = _run(
        r"""
createCredential = () => {
  const bytes = (value) => Uint8Array.from(Buffer.from(value)).buffer;
  return { id: "registration", rawId: bytes("registration"), type: "public-key", response: {
    attestationObject: bytes("attestation"), clientDataJSON: bytes("client"),
  }};
};
await settle(); respond(take("/api/v1/status"), statusProjection); await settle();
nav.find((item) => item.dataset.view === "onboarding").click(); await settle();
button("Register this device with WebAuthn").click(); await settle();
const options = take("/api/v1/webauthn/register/options");
respond(options, { publicKey: {
  challenge: "Y2hhbGxlbmdl", user: { id: "bG9jYWwtb3duZXI", name: "local:owner", displayName: "Local owner" },
  authenticatorSelection: { userVerification: "preferred" },
} }); await settle();
const verify = take("/api/v1/webauthn/register/verify");
const registration = JSON.parse(verify.init.body).response;
respond(verify, { schema_version: 1, credential_id: "credential" }); await settle();
process.stdout.write(JSON.stringify({
  convertedChallenge: lastCreateOptions.publicKey.challenge instanceof ArrayBuffer,
  userVerification: lastCreateOptions.publicKey.authenticatorSelection.userVerification,
  registration,
  app: app.textContent,
  status: status.textContent,
}));
"""
    )

    assert result["convertedChallenge"] is True
    assert result["userVerification"] == "required"
    assert result["registration"]["id"] == "registration"
    assert "registration" not in result["app"]
    assert "Credential details are not shown or retained" in result["status"]


def test_shipped_browser_scrubs_review_before_an_unresolved_status_refresh() -> None:
    """Catches credential/evidence DOM retention while a post-success status request hangs."""
    result = _run(
        r"""
await settle(); respond(take("/api/v1/status"), statusProjection); await settle();
nav.find((item) => item.dataset.view === "inbox").click(); await settle();
respond(take("/api/v1/inbox"), { schema_version: 1, pending_proposal_ids: ["proposal:b"], open_case_ids: [] }); await settle();
button("Review proposal:b").click(); await settle(); respond(take("/api/v1/proposals/proposal%3Ab"), proposal("proposal:b")); await settle();
button("Authorize confirm proposal proposal:b with WebAuthn").click(); await settle();
const options = take("/api/v1/decisions/options");
respond(options, { publicKey: { challenge: "Y2hhbGxlbmdl", allowCredentials: [{ id: "Y3JlZGVudGlhbA", type: "public-key" }] } }); await settle();
const verify = take("/api/v1/decisions/verify");
respond(verify, { schema_version: 1, status: "resolved" }); await settle();
const statusRequest = take("/api/v1/status");
process.stdout.write(JSON.stringify({ app: app.textContent, status: status.textContent, pendingStatus: statusRequest.path }));
"""
    )

    assert "PRIVATE-proposal:b" not in result["app"]
    assert "Decision binding" not in result["app"]
    assert "node:proposal:b" not in result["app"]
    assert result["pendingStatus"] == "/api/v1/status"


def test_successful_verify_after_switching_to_b_reports_applied_and_preserves_b_review() -> None:
    """Catches a successful A mutation being reported as unapplied after B becomes current."""
    result = _run(
        r"""
await settle(); respond(take("/api/v1/status"), statusProjection); await settle();
nav.find((item) => item.dataset.view === "inbox").click(); await settle();
respond(take("/api/v1/inbox"), { schema_version: 1, pending_proposal_ids: ["proposal:a", "proposal:b"], open_case_ids: [] }); await settle();
button("Review proposal:a").click(); await settle(); respond(take("/api/v1/proposals/proposal%3Aa"), proposal("proposal:a")); await settle();
button("Authorize confirm proposal proposal:a with WebAuthn").click(); await settle();
const options = take("/api/v1/decisions/options");
respond(options, { publicKey: { challenge: "Y2hhbGxlbmdl", allowCredentials: [{ id: "Y3JlZGVudGlhbA", type: "public-key" }] } }); await settle();
const verifyA = take("/api/v1/decisions/verify");
nav.find((item) => item.dataset.view === "inbox").click(); await settle();
respond(take("/api/v1/inbox"), { schema_version: 1, pending_proposal_ids: ["proposal:b"], open_case_ids: [] }); await settle();
button("Review proposal:b").click(); await settle(); respond(take("/api/v1/proposals/proposal%3Ab"), proposal("proposal:b")); await settle();
respond(verifyA, { schema_version: 1, status: "resolved" }); await settle();
const statusRequest = take("/api/v1/status");
process.stdout.write(JSON.stringify({ app: app.textContent, status: status.textContent, statusPath: statusRequest.path }));
"""
    )

    assert "PRIVATE-proposal:b" in result["app"]
    assert "PRIVATE-proposal:a" not in result["app"]
    assert "Decision applied after user-verifying WebAuthn confirmation" in result["status"]
    assert result["statusPath"] == "/api/v1/status"


def test_shipped_browser_cancellation_and_expiry_clear_the_review_without_verify_replay() -> None:
    """Catches a cancelled ceremony verifying anyway or an expired ceremony retaining its preview."""
    result = _run(
        r"""
await settle(); respond(take("/api/v1/status"), statusProjection); await settle();
nav.find((item) => item.dataset.view === "inbox").click(); await settle();
respond(take("/api/v1/inbox"), { schema_version: 1, pending_proposal_ids: ["proposal:b"], open_case_ids: [] }); await settle();
button("Review proposal:b").click(); await settle(); respond(take("/api/v1/proposals/proposal%3Ab"), proposal("proposal:b")); await settle();
getCredential = () => Promise.reject(Object.assign(new Error("cancelled"), { name: "AbortError" }));
button("Authorize confirm proposal proposal:b with WebAuthn").click(); await settle();
const options = take("/api/v1/decisions/options");
respond(options, { publicKey: { challenge: "Y2hhbGxlbmdl", allowCredentials: [{ id: "Y3JlZGVudGlhbA", type: "public-key" }] } }); await settle();
const verifyAfterCancel = calls.filter((request) => request.path === "/api/v1/decisions/verify").length;
nav.find((item) => item.dataset.view === "inbox").click(); await settle();
respond(take("/api/v1/inbox"), { schema_version: 1, pending_proposal_ids: ["proposal:b"], open_case_ids: [] }); await settle();
button("Review proposal:b").click(); await settle(); respond(take("/api/v1/proposals/proposal%3Ab"), proposal("proposal:b")); await settle();
getCredential = () => Promise.resolve(credential("assertion"));
button("Authorize confirm proposal proposal:b with WebAuthn").click(); await settle();
const retryOptions = take("/api/v1/decisions/options");
respond(retryOptions, { publicKey: { challenge: "Y2hhbGxlbmdl", allowCredentials: [{ id: "Y3JlZGVudGlhbA", type: "public-key" }] } }); await settle();
const expired = take("/api/v1/decisions/verify"); respond(expired, { status: "rejected" }, 503); await settle();
process.stdout.write(JSON.stringify({ verifyAfterCancel, app: app.textContent, status: status.textContent }));
"""
    )

    assert result["verifyAfterCancel"] == 0
    assert "PRIVATE-proposal:b" not in result["app"]
    assert "Decision binding" not in result["app"]
    assert "node:proposal:b" not in result["app"]
    assert "challenge may have expired" in result["status"]


def test_shipped_browser_applies_selection_rules_by_decision_action() -> None:
    """Catches a global selection guard blocking cases/answers/writes or weakening proposals."""
    result = _run(
        r"""
await settle(); respond(take("/api/v1/status"), statusProjection); await settle();
async function openReview(identifier, action, selected) {
  nav.find((item) => item.dataset.view === "inbox").click(); await settle();
  respond(take("/api/v1/inbox"), { schema_version: 1, pending_proposal_ids: [identifier], open_case_ids: [], clarification_sessions: [] }); await settle();
  button(`Review ${identifier}`).click(); await settle();
  respond(take(`/api/v1/proposals/${encodeURIComponent(identifier)}`), proposal(identifier, action, selected)); await settle();
  return button(`Authorize ${({confirm_baseline: "confirm baseline", confirm_proposal: "confirm proposal", answer_clarification: "answer clarification", resolve_conflict: "resolve conflict", approve_external_write: "approve external write"})[action]} ${identifier} with WebAuthn`);
}
const baseline = await openReview("proposal:baseline", "confirm_baseline", []);
const baselineSummary = app.textContent;
nav.find((item) => item.dataset.view === "inbox").click(); await settle(); respond(take("/api/v1/inbox"), { schema_version: 1, pending_proposal_ids: [], open_case_ids: [], clarification_sessions: [] }); await settle();
const proposalButton = await openReview("proposal:requirement", "confirm_proposal", []);
const proposalSummary = app.textContent;
const outcomes = [];
for (const [identifier, action] of [["case:conflict", "resolve_conflict"], ["answer:" + "a".repeat(64), "answer_clarification"], ["write-plan:" + "b".repeat(64), "approve_external_write"]]) {
  nav.find((item) => item.dataset.view === "inbox").click(); await settle(); respond(take("/api/v1/inbox"), { schema_version: 1, pending_proposal_ids: [], open_case_ids: [], clarification_sessions: [] }); await settle();
  const authorize = await openReview(identifier, action, []);
  const summary = app.textContent;
  authorize.click(); await settle();
  const options = take("/api/v1/decisions/options"); respond(options, { publicKey: { challenge: "Y2hhbGxlbmdl", allowCredentials: [{ id: "Y3JlZGVudGlhbA", type: "public-key" }] } }); await settle();
  const verify = take("/api/v1/decisions/verify"); respond(verify, { schema_version: 1, status: "applied" }); await settle();
  const refresh = take("/api/v1/status"); respond(refresh, statusProjection); await settle();
  outcomes.push({ identifier, action, disabled: authorize.disabled, summary, payload: JSON.parse(options.init.body).payload });
}
process.stdout.write(JSON.stringify({ baselineDisabled: baseline.disabled, proposalDisabled: proposalButton.disabled, baselineSummary, proposalSummary, outcomes }));
"""
    )

    assert result["baselineDisabled"] is True
    assert result["proposalDisabled"] is True
    assert "requires at least one server-bound selected node" in result["baselineSummary"]
    assert "requires at least one server-bound selected node" in result["proposalSummary"]
    assert [item["action"] for item in result["outcomes"]] == [
        "resolve_conflict",
        "answer_clarification",
        "approve_external_write",
    ]
    for outcome in result["outcomes"]:
        assert outcome["disabled"] is False
        assert outcome["payload"]["selected_node_ids"] == []
        assert "Action" in outcome["summary"]
        assert "Subject" in outcome["summary"]
        assert "Result digest" in outcome["summary"]


def test_shipped_browser_answers_a_visible_question_and_discards_on_cancel() -> None:
    """Catches missing answer UI, unsafe rendering, or cancel leaving server-held plaintext."""
    result = _run(
        r"""
await settle(); respond(take("/api/v1/status"), statusProjection); await settle();
nav.find((item) => item.dataset.view === "inbox").click(); await settle();
const sessionId = "clarification:" + "c".repeat(64);
respond(take("/api/v1/inbox"), { schema_version: 1, pending_proposal_ids: [], open_case_ids: [], clarification_sessions: [{ id: sessionId, task_id: "task:share", questions: [{ id: "audience", prompt: "Who may share <reports>?", required: true }] }] }); await settle();
const textarea = walk(app).find((node) => node.tagName === "textarea");
textarea.value = "PRIVATE answer <owners>";
button("Preview answer for audience").click(); await settle();
const answerRequest = take("/api/v1/clarifications/answers/preview");
const submitted = JSON.parse(answerRequest.init.body);
const answerId = "answer:" + "d".repeat(64);
respond(answerRequest, proposal(answerId, "answer_clarification", [])); await settle();
const review = app.textContent;
button("Cancel review without applying a decision").click(); await settle();
const discard = take("/api/v1/clarifications/answers/discard");
const discarded = JSON.parse(discard.init.body); respond(discard, { schema_version: 1, status: "discarded", answer_id: answerId }); await settle();
process.stdout.write(JSON.stringify({ submitted, discarded, review, app: app.textContent, decisionCalls: calls.filter((request) => request.path.startsWith("/api/v1/decisions/")).length }));
"""
    )

    assert result["submitted"] == {
        "session_id": "clarification:" + "c" * 64,
        "question_id": "audience",
        "answer": "PRIVATE answer <owners>",
    }
    assert result["discarded"] == {"answer_id": "answer:" + "d" * 64}
    assert (
        "Who may share <reports>?" in result["review"] or "answer clarification" in result["review"]
    )
    assert "PRIVATE answer <owners>" not in result["review"]
    assert "PRIVATE answer <owners>" not in result["app"]
    assert result["decisionCalls"] == 0


def test_shipped_browser_routes_answer_preview_through_existing_decision_verification() -> None:
    """Catches an answer preview bypassing WebAuthn decision options or verification."""
    result = _run(
        r"""
await settle(); respond(take("/api/v1/status"), statusProjection); await settle();
nav.find((item) => item.dataset.view === "inbox").click(); await settle();
const sessionId = "clarification:" + "e".repeat(64);
respond(take("/api/v1/inbox"), { schema_version: 1, pending_proposal_ids: [], open_case_ids: [], clarification_sessions: [{ id: sessionId, task_id: "task:share", questions: [{ id: "audience", prompt: "Who may share reports?", required: true }] }] }); await settle();
const textarea = walk(app).find((node) => node.tagName === "textarea"); textarea.value = "Workspace owners";
button("Preview answer for audience").click(); await settle();
const answerRequest = take("/api/v1/clarifications/answers/preview");
const answerId = "answer:" + "f".repeat(64); respond(answerRequest, proposal(answerId, "answer_clarification", [])); await settle();
button(`Authorize answer clarification ${answerId} with WebAuthn`).click(); await settle();
const options = take("/api/v1/decisions/options"); const optionPayload = JSON.parse(options.init.body).payload;
respond(options, { publicKey: { challenge: "Y2hhbGxlbmdl", allowCredentials: [{ id: "Y3JlZGVudGlhbA", type: "public-key" }] } }); await settle();
const verify = take("/api/v1/decisions/verify"); const verifyPayload = JSON.parse(verify.init.body).payload;
respond(verify, { schema_version: 1, status: "open", session_id: sessionId, evidence_ref: "evidence:answer" }); await settle();
const refresh = take("/api/v1/status");
process.stdout.write(JSON.stringify({ optionPayload, verifyPayload, refresh: refresh.path, status: status.textContent }));
"""
    )

    assert result["optionPayload"] == result["verifyPayload"]
    assert result["optionPayload"]["action"] == "answer_clarification"
    assert result["optionPayload"]["selected_node_ids"] == []
    assert result["refresh"] == "/api/v1/status"
    assert "Decision applied" in result["status"]


def _live_payload(action: str, subject_id: str) -> dict[str, object]:
    kind = "case" if action == "resolve_conflict" else "answer"
    return {
        "schema_version": 1,
        "project_id": "project",
        "repository_id": "repo:sha256:" + "a" * 64,
        "actor": "local:owner",
        "action": action,
        "graph_version": 3,
        "parent_bundle_digest": "sha256:" + "b" * 64,
        "subject": {"kind": kind, "id": subject_id},
        "subject_digest": "sha256:" + "c" * 64,
        "selected_node_ids": [],
        "result_digest": "sha256:" + "d" * 64,
        "challenge": "challenge:" + "e" * 64,
        "issued_at": "2026-08-30T12:00:00Z",
        "expires_at": "2026-08-30T12:05:00Z",
    }


@dataclass
class _LaunchedJourneyService:
    calls: list[tuple[str, object]] = field(default_factory=list)

    def status(self) -> dict[str, object]:
        self.calls.append(("status", None))
        return {
            "schema_version": 1,
            "status": "human_attention_required",
            "attention_route": "inbox",
            "project_id": "project",
            "repository_id": "repo:sha256:" + "a" * 64,
            "graph_version": 3,
            "pending_proposal_ids": [],
            "open_case_ids": ["case:encoded"],
        }

    def inbox(self) -> dict[str, object]:
        self.calls.append(("inbox", None))
        return {
            "schema_version": 1,
            "pending_proposal_ids": [],
            "open_case_ids": ["case:encoded"],
            "clarification_sessions": [
                {
                    "id": "clarification:" + "f" * 64,
                    "task_id": "task:sharing",
                    "questions": [
                        {
                            "id": "audience",
                            "prompt": "Who may share reports?",
                            "required": True,
                        }
                    ],
                }
            ],
        }

    def proposal_preview(self, proposal_id: str) -> dict[str, object]:
        self.calls.append(("proposal_preview", proposal_id))
        return {
            "schema_version": 1,
            "preview_digest": "sha256:" + "1" * 64,
            "preview": {
                "kind": "reconciliation_case",
                "result_changeset_digest": "sha256:" + "d" * 64,
            },
            "payload": _live_payload("resolve_conflict", proposal_id),
        }

    def answer_preview(self, session_id: str, question_id: str, answer: str) -> dict[str, object]:
        self.calls.append(("answer_preview", (session_id, question_id, answer)))
        answer_id = "answer:" + "9" * 64
        return {
            "schema_version": 1,
            "preview_digest": "sha256:" + "2" * 64,
            "preview": {
                "kind": "clarification_answer",
                "session_id": session_id,
                "question_id": question_id,
                "answer_digest": "sha256:" + "3" * 64,
                "result_evidence_id": "evidence:answer",
                "result_evidence_digest": "sha256:" + "4" * 64,
            },
            "payload": _live_payload("answer_clarification", answer_id),
        }

    def discard_answer_preview(self, answer_id: str) -> dict[str, object]:
        self.calls.append(("discard_answer_preview", answer_id))
        return {"schema_version": 1, "status": "discarded", "answer_id": answer_id}

    def registration_options(self) -> bytes:
        raise AssertionError("registration is outside this journey")

    def register(self, _response: bytes) -> object:
        raise AssertionError("registration is outside this journey")

    def decision_options(self, payload: object) -> bytes:
        self.calls.append(("decision_options", payload))
        return b'{"publicKey":{"challenge":"Y2hhbGxlbmdl","allowCredentials":[{"id":"Y3JlZGVudGlhbA","type":"public-key"}]}}'

    def apply_decision(self, response: bytes, payload: object) -> dict[str, object]:
        self.calls.append(("apply_decision", (response, payload)))
        return {"schema_version": 1, "status": "applied"}


@contextmanager
def _launched_site(service: _LaunchedJourneyService) -> Iterator[str]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    port = cast(tuple[str, int], listener.getsockname())[1]
    origin = f"http://localhost:{port}"
    csrf = "launched-browser-csrf"
    api = build_control_plane_app(cast(Any, service), origin=origin, csrf_secret=csrf)
    site = _ControlPlaneSite(
        api,
        origin=origin,
        csrf_secret=csrf,
        instance_id="instance:" + "8" * 64,
        project_id="project",
        repository_id="repo:sha256:" + "4" * 64,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            site,
            host="127.0.0.1",
            port=port,
            loop="asyncio",
            lifespan="on",
            log_config=None,
            log_level="critical",
            access_log=False,
            proxy_headers=False,
            server_header=False,
            date_header=False,
        )
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started
    try:
        yield origin
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        site.close()
        assert not thread.is_alive()


_LIVE_BROWSER = r"""
import http from "node:http";
class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.attributes = new Map(); this.handlers = new Map(); this.dataset = {}; this.className = ""; this.id = ""; this.type = ""; this.checked = false; this.disabled = false; this.value = ""; this._text = ""; }
  get textContent() { return this._text + this.children.map((child) => child.textContent || "").join(""); }
  set textContent(value) { this._text = String(value); this.children = []; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; this._text = ""; }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  removeAttribute(name) { this.attributes.delete(name); }
  addEventListener(name, handler) { this.handlers.set(name, handler); }
  click() { if (!this.disabled) return this.handlers.get("click")?.(); }
  focus() {}
}
class TextNode { constructor(value) { this.textContent = String(value); } }
const app = new Element("main"); const status = new Element("p");
const nav = ["home", "onboarding", "inbox", "proposal", "team_state"].map((view) => { const item = new Element("button"); item.dataset.view = view; return item; });
globalThis.document = { getElementById(id) { return id === "app" ? app : status; }, querySelectorAll() { return nav; }, createElement(tag) { return new Element(tag); }, createTextNode(value) { return new TextNode(value); } };
globalThis.window = { location: { hash: "#csrf=launched-browser-csrf", pathname: "/" } }; globalThis.location = window.location;
globalThis.history = { replaceState() {} };
globalThis.Headers = class { constructor() { this.values = new Map(); } set(key, value) { this.values.set(key.toLowerCase(), String(value)); } };
globalThis.atob = (value) => Buffer.from(value, "base64").toString("binary"); globalThis.btoa = (value) => Buffer.from(value, "binary").toString("base64");
const bytes = (value) => Uint8Array.from(Buffer.from(value)).buffer;
Object.defineProperty(globalThis, "navigator", { configurable: true, value: { credentials: { get() { return Promise.resolve({ id: "assertion", rawId: bytes("assertion"), type: "public-key", response: { authenticatorData: bytes("auth"), clientDataJSON: bytes("client"), signature: bytes("sig"), userHandle: null } }); } } } });
const origin = __ORIGIN__; const parsedOrigin = new URL(origin); let cookie = ""; const network = [];
function raw(path, init = {}) { return new Promise((resolve, reject) => { const method = init.method || "GET"; const headers = { Host: parsedOrigin.host }; for (const [key, value] of (init.headers?.values || new Map())) headers[key] = value; if (cookie) headers.Cookie = cookie; if (method !== "GET") headers.Origin = origin; const body = init.body || ""; if (body) headers["Content-Length"] = String(Buffer.byteLength(body)); network.push({ path, method, headers: { ...headers } }); const request = http.request({ host: "127.0.0.1", port: parsedOrigin.port, path, method, headers }, (response) => { let content = ""; response.setEncoding("utf8"); response.on("data", (chunk) => { content += chunk; }); response.on("end", () => resolve({ status: response.statusCode, headers: response.headers, text: content })); }); request.on("error", reject); if (body) request.write(body); request.end(); }); }
const root = await raw("/"); cookie = root.headers["set-cookie"][0].split(";", 1)[0]; const asset = await raw("/app.js");
globalThis.fetch = async (path, init = {}) => { const response = await raw(path, init); return { ok: response.status >= 200 && response.status < 300, status: response.status, text: async () => response.text }; };
function walk(node, items = []) { items.push(node); for (const child of node.children || []) walk(child, items); return items; }
function findButton(label) { return walk(app).find((node) => node.tagName === "button" && node.textContent === label); }
async function waitFor(predicate) { for (let index = 0; index < 300; index += 1) { const value = predicate(); if (value) return value; await new Promise((resolve) => setTimeout(resolve, 10)); } throw new Error(`timed out: ${app.textContent} / ${status.textContent}`); }
(0, eval)(asset.text);
await waitFor(() => status.textContent.includes("updated")); nav.find((item) => item.dataset.view === "inbox").click();
await waitFor(() => findButton("Review case:encoded")); findButton("Review case:encoded").click();
const caseAuthorize = await waitFor(() => findButton("Authorize resolve conflict case:encoded with WebAuthn")); const caseSummary = app.textContent; caseAuthorize.click();
await waitFor(() => !app.textContent.includes("Decision binding")); nav.find((item) => item.dataset.view === "inbox").click();
await waitFor(() => status.textContent.includes("Inbox updated"));
await waitFor(() => findButton("Preview answer for audience")); const textarea = walk(app).find((node) => node.tagName === "textarea"); textarea.value = "Workspace owners"; findButton("Preview answer for audience").click();
const answerAuthorize = await waitFor(() => findButton(`Authorize answer clarification ${"answer:" + "9".repeat(64)} with WebAuthn`)); const answerSummary = app.textContent; answerAuthorize.click();
await waitFor(() => !app.textContent.includes("Decision binding"));
process.stdout.write(JSON.stringify({ caseSummary, answerSummary, network }));
"""


def test_launched_http_and_shipped_asset_complete_encoded_case_and_answer_journey() -> None:
    """Catches the real listener/asset/API seams for Origin, encoded IDs, empty selection, and answers."""
    if NODE is None:
        pytest.skip("Node runtime is unavailable for launched shipped-asset execution")
    service = _LaunchedJourneyService()
    with _launched_site(service) as origin:
        completed = subprocess.run(
            [NODE, "--input-type=module"],
            input=_LIVE_BROWSER.replace("__ORIGIN__", json.dumps(origin)),
            text=True,
            capture_output=True,
            check=False,
            timeout=15,
        )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    paths = [item["path"] for item in result["network"]]
    assert "/app.js" in paths
    assert "/api/v1/proposals/case%3Aencoded" in paths
    assert "/api/v1/clarifications/answers/preview" in paths
    assert paths.count("/api/v1/decisions/options") == 2
    assert paths.count("/api/v1/decisions/verify") == 2
    get_requests = [item for item in result["network"] if item["method"] == "GET"]
    assert all(
        "Origin" not in item["headers"] and "origin" not in item["headers"] for item in get_requests
    )
    assert "Actionresolve_conflict" in result["caseSummary"]
    assert "Result digestsha256:" in result["caseSummary"]
    assert "Actionanswer_clarification" in result["answerSummary"]
    assert ("proposal_preview", "case:encoded") in service.calls
    assert (
        "answer_preview",
        ("clarification:" + "f" * 64, "audience", "Workspace owners"),
    ) in service.calls
