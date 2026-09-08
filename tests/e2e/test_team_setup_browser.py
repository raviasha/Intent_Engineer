"""Deterministic execution coverage for the production team-setup browser journey."""

from __future__ import annotations

import json
import shutil
import subprocess
from importlib.resources import files

import pytest

NODE = shutil.which("node")


def _asset() -> str:
    return files("intent_engineering.control_plane").joinpath("assets", "app.js").read_text()


_HARNESS = r"""
class Element {
  constructor(tag) { this.tagName=tag; this.children=[]; this.handlers=new Map(); this.dataset={}; this.attributes=new Map(); this._text=""; this.value=""; this.disabled=false; this.type=""; }
  get textContent() { return this._text + this.children.map((child) => child.textContent || "").join(""); }
  set textContent(value) { this._text=String(value); this.children=[]; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children=children; this._text=""; }
  setAttribute(name,value) { this.attributes.set(name,String(value)); }
  removeAttribute(name) { this.attributes.delete(name); }
  addEventListener(name,handler) { this.handlers.set(name,handler); }
  click() { if (!this.disabled) return this.handlers.get("click")?.(); }
  focus() {}
}
const app=new Element("main"), status=new Element("p");
const nav=["home","onboarding","inbox","proposal","team_state"].map((view)=>{const item=new Element("button");item.dataset.view=view;return item;});
globalThis.document={getElementById(id){return id==="app"?app:status;},querySelectorAll(){return nav;},createElement(tag){return new Element(tag);},createTextNode(value){const item=new Element("text");item.textContent=value;return item;}};
globalThis.window={location:{hash:"#csrf=csrf-token",pathname:"/"}}; globalThis.location=window.location; globalThis.history={replaceState(){}};
globalThis.Headers=class { constructor(){this.values=new Map();} set(key,value){this.values.set(key,value);} };
globalThis.atob=(value)=>Buffer.from(value,"base64").toString("binary"); globalThis.btoa=(value)=>Buffer.from(value,"binary").toString("base64");
const pending=[], calls=[];
globalThis.fetch=(path,init={})=>new Promise((resolve)=>{const request={path,init,resolve};pending.push(request);calls.push(request);});
function take(path){const index=pending.findIndex((item)=>item.path===path);if(index<0)throw new Error(`missing ${path}; pending=${pending.map((item)=>item.path)}`);return pending.splice(index,1)[0];}
function respond(request,value,statusCode=200){request.resolve({ok:statusCode>=200&&statusCode<300,status:statusCode,text:async()=>JSON.stringify(value)});}
async function settle(){for(let index=0;index<12;index+=1)await Promise.resolve();}
function walk(node,items=[]){items.push(node);for(const child of node.children||[])walk(child,items);return items;}
function button(label){const item=walk(app).find((node)=>node.tagName==="button"&&node.textContent===label);if(!item)throw new Error(`missing button ${label}; app=${app.textContent}`);return item;}
function bytes(value){return Uint8Array.from(Buffer.from(value)).buffer;}
function registration(){return {id:"registration-secret",rawId:bytes("registration"),type:"public-key",response:{attestationObject:bytes("attestation"),clientDataJSON:bytes("client")}};}
function assertion(){return {id:"assertion-secret",rawId:bytes("assertion"),type:"public-key",response:{authenticatorData:bytes("auth"),clientDataJSON:bytes("client"),signature:bytes("signature"),userHandle:null}};}
let createCount=0,getCount=0;
Object.defineProperty(globalThis,"navigator",{configurable:true,value:{credentials:{create(){createCount+=1;return Promise.resolve(registration());},get(){getCount+=1;return Promise.resolve(assertion());}}}});
const projection={schema_version:1,status:"ready",attention_route:null,project_id:"alpha",repository_id:"github.com/acme/alpha",graph_version:7,pending_proposal_ids:[],open_case_ids:[]};
const payload=(action,id)=>({schema_version:1,project_id:"alpha",repository_id:"github.com/acme/alpha",actor:"local:asha",action,graph_version:7,parent_bundle_digest:null,subject:{kind:"write-plan",id},subject_digest:"sha256:subject",selected_node_ids:[],result_digest:"sha256:result",challenge:"challenge",issued_at:"2026-09-08T00:00:00Z",expires_at:"2026-09-08T00:05:00Z"});
"""


def _run(scenario: str) -> dict[str, object]:
    if NODE is None:
        pytest.skip("Node runtime unavailable")
    completed = subprocess.run(
        [NODE, "--input-type=module"],
        input=f"{_HARNESS}\n{_asset()}\nawait settle(); respond(take('/_intent/browser/bootstrap'), {{status:'ok'}}); await settle();\n{scenario}",
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_setup_journey_uses_server_identity_and_two_exact_webauthn_authorizations() -> None:
    """Catches proof-token enrollment or either reviewed external write bypassing WebAuthn."""
    result = _run(
        r"""
respond(take("/api/v1/status"),projection); await settle();
nav.find((item)=>item.dataset.view==="team_state").click(); await settle();
const setupStatus=take("/api/v1/team/setup");
respond(setupStatus,{state:"setup_required",project_id:"alpha",repository_id:"github.com/acme/alpha",authority_digest:"sha256:authority"}); await settle();
button("Inspect GitHub identity and repository").click(); await settle();
const inspect=take("/api/v1/team/setup/inspect"); respond(inspect,{state:"identity_verified",github_account_id:"101",github_login:"asha",repository_id:"github.com/acme/alpha",enrollment:"required"}); await settle();
button("Enroll this device with WebAuthn").click(); await settle();
const enroll=take("/api/v1/team/setup/enroll"); respond(enroll,{publicKey:{challenge:"Y2hhbGxlbmdl",user:{id:"YXNoYQ",name:"asha",displayName:"Asha"}}}); await settle();
const enrollmentVerify=take("/api/v1/team/enrollment/verify"); const enrollmentBody=JSON.parse(enrollmentVerify.init.body); respond(enrollmentVerify,{state:"enrolled"}); await settle();
button("Preview branch protection changes").click(); await settle();
const protectionPreview=take("/api/v1/team/setup/protection-preview"); const protectionPayload=payload("approve_external_write","protection"); respond(protectionPreview,{preview:{branch:"intent-state",required_reviews:1},payload:protectionPayload}); await settle();
const protectionText=app.textContent; button("Authorize branch protection with WebAuthn").click(); await settle();
const protectionOptions=take("/api/v1/team/setup/options"); const protectionOptionsBody=JSON.parse(protectionOptions.init.body); respond(protectionOptions,{publicKey:{challenge:"Y2hhbGxlbmdl",allowCredentials:[{id:"Y3JlZGVudGlhbA",type:"public-key"}]}}); await settle();
const protectionVerify=take("/api/v1/team/setup/verify"); const protectionVerifyBody=JSON.parse(protectionVerify.init.body); respond(protectionVerify,{state:"protection_configured"}); await settle();
button("Preview encrypted team-state publication").click(); await settle();
const publicationPreview=take("/api/v1/team/setup/publication-preview"); const publicationPayload=payload("publish_state","publication"); respond(publicationPreview,{preview:{branch:"intent-publication/abc",bundle_digest:"sha256:bundle"},payload:publicationPayload}); await settle();
const publicationText=app.textContent; button("Authorize publication with WebAuthn").click(); await settle();
const publicationOptions=take("/api/v1/team/setup/options"); const publicationOptionsBody=JSON.parse(publicationOptions.init.body); respond(publicationOptions,{publicKey:{challenge:"Y2hhbGxlbmdl",allowCredentials:[{id:"Y3JlZGVudGlhbA",type:"public-key"}]}}); await settle();
const publicationVerify=take("/api/v1/team/setup/verify"); const publicationVerifyBody=JSON.parse(publicationVerify.init.body); respond(publicationVerify,{state:"published",pull_request_url:"https://github.com/acme/alpha/pull/7"}); await settle();
process.stdout.write(JSON.stringify({app:app.textContent,protectionText,publicationText,enrollmentBody,protectionOptionsBody,protectionVerifyBody,publicationOptionsBody,publicationVerifyBody,createCount,getCount,paths:calls.map((item)=>item.path)}));
"""
    )

    assert result["createCount"] == 1
    assert result["getCount"] == 2
    assert result["enrollmentBody"]["response"]["id"] == "registration-secret"
    assert result["protectionOptionsBody"]["payload"]["action"] == "approve_external_write"
    assert result["protectionVerifyBody"]["payload"] == result["protectionOptionsBody"]["payload"]
    assert result["publicationOptionsBody"]["payload"]["action"] == "publish_state"
    assert result["publicationVerifyBody"]["payload"] == result["publicationOptionsBody"]["payload"]
    assert "required_reviews1" in result["protectionText"]
    assert "bundle_digestsha256:bundle" in result["publicationText"]
    assert "https://github.com/acme/alpha/pull/7" in result["app"]
    assert "/api/v1/team/enrollment/options" not in result["paths"]


def test_production_setup_hides_manual_proof_and_cancel_clears_pending() -> None:
    """Catches production setup exposing a raw authority-proof field or abandoning pending state."""
    result = _run(
        r"""
respond(take("/api/v1/status"),projection); await settle(); nav.find((item)=>item.dataset.view==="team_state").click(); await settle();
respond(take("/api/v1/team/setup"),{state:"setup_required",project_id:"alpha",repository_id:"github.com/acme/alpha",authority_digest:"sha256:authority"}); await settle();
const setupText=app.textContent; button("Inspect GitHub identity and repository").click(); await settle();
respond(take("/api/v1/team/setup/inspect"),{state:"identity_verified",github_account_id:"101",github_login:"asha",repository_id:"github.com/acme/alpha",enrollment:"required"}); await settle();
button("Cancel team setup").click(); await settle(); const cancel=take("/api/v1/team/setup/cancel"); respond(cancel,{state:"setup_required"}); await settle();
process.stdout.write(JSON.stringify({setupText,app:app.textContent,cancelBody:cancel.init.body}));
"""
    )

    assert "GitHub device authorization proof" not in result["setupText"]
    assert result["cancelBody"] == "{}"
    assert "Team setup cancelled" in result["app"] or "setup_required" in result["app"]
