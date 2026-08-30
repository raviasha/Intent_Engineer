"""Deterministic execution coverage for the shipped browser review asset."""

from __future__ import annotations

import json
import shutil
import subprocess
from importlib.resources import files

import pytest

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
function proposal(identifier, action = "confirm_proposal") {
  return { schema_version: 1, preview_digest: `sha256:${identifier}`, preview: { evidence: `PRIVATE-${identifier}`, affected_nodes: [`node:${identifier}`] }, payload: {
    schema_version: 1, project_id: "project", repository_id: "repo", actor: "local:owner", action,
    graph_version: 3, parent_bundle_digest: "parent", subject: { kind: "proposal", id: identifier }, subject_digest: "subject",
    selected_node_ids: [`node:${identifier}`], result_digest: "result", challenge: "challenge", issued_at: "now", expires_at: "later",
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
