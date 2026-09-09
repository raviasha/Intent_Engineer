"""Execute shipped browser UI against controlled public enrollment responses."""

from tests.e2e.test_team_setup_browser import _run


def test_join_ui_uses_local_webauthn_and_only_opaque_session_requests():
    result = _run(r"""
respond(take("/api/v1/status"),projection); await settle();
nav.find((item)=>item.dataset.view==="team_state").click(); await settle();
respond(take("/api/v1/team/membership"), {state:"review_required",action:"join",session_id:"opaque-session",can_cancel:true,repository_id:"github.com/acme/project"}); await settle();
button("Enroll this device with WebAuthn").click(); await settle();
respond(take("/api/v1/team/membership/register-options"), {publicKey:{challenge:"Y2hhbGxlbmdl",user:{id:"dXNlcg",name:"bob",displayName:"Bob"},authenticatorSelection:{userVerification:"preferred"}}}); await settle();
const registrationCall=take("/api/v1/team/membership/register-verify");
respond(registrationCall,{state:"review_required"}); await settle();
respond(take("/api/v1/team/membership/preview"),{state:"preview_ready",action:"join",session_id:"opaque-session",can_cancel:true,identity:{account_id:"200",login:"bob"},recipient_key_id:"recipient:key",signature_id:"signer:key",root_key_id:"root:key",preview_digest:"sha256:review"}); await settle();
button("Authorize join with WebAuthn").click(); await settle();
respond(take("/api/v1/team/membership/options"),{publicKey:{challenge:"Y2hhbGxlbmdl",allowCredentials:[],userVerification:"preferred"}}); await settle();
const verification=take("/api/v1/team/membership/verify");
respond(verification,{state:"response-ready",action:"join",session_id:"opaque-session",can_cancel:false}); await settle();
process.stdout.write(JSON.stringify({createCount,getCount,body:JSON.parse(verification.init.body),registration:JSON.parse(registrationCall.init.body),text:app.textContent}));
""")
    assert result["createCount"] == 1
    assert result["getCount"] == 1
    assert set(result["body"]) == {"session_id", "response"}
    assert result["body"]["session_id"] == "opaque-session"
    assert set(result["registration"]) == {"session_id", "response"}
    assert "response-ready" in result["text"]
    assert "Cancel enrollment" not in result["text"]
    assert "assertion-secret" not in result["text"]


def test_pending_and_closed_ui_hide_cancel_and_require_explicit_fresh_restart():
    result = _run(r"""
respond(take("/api/v1/status"),projection); await settle();
nav.find((item)=>item.dataset.view==="team_state").click(); await settle();
respond(take("/api/v1/team/membership"),{state:"publication_recovery_required",action:"approve-join",session_id:"opaque-session",can_cancel:false}); await settle();
const pendingText=app.textContent;
button("Refresh enrollment progress").click(); await settle();
respond(take("/api/v1/team/membership/reconcile"),{state:"closed",action:"approve-join",session_id:"opaque-session",can_cancel:false}); await settle();
const closed=app.textContent;
button("Discard closed enrollment and start fresh").click(); await settle();
const restart=take("/api/v1/team/membership/restart"); respond(restart,{state:"fresh_invitation_required",action:"approve-join",can_cancel:false}); await settle();
process.stdout.write(JSON.stringify({pending:pendingText,closed,text:app.textContent,body:JSON.parse(restart.init.body),getCount}));
""")
    assert "Cancel enrollment" not in result["pending"] + result["closed"]
    assert "Discard closed enrollment and start fresh" in result["closed"]
    assert result["body"] == {"session_id": "opaque-session"}
    assert result["getCount"] == 0
    assert "fresh_invitation_required" in result["text"]


def test_active_member_publishes_through_existing_browser_session():
    result = _run(r"""
respond(take("/api/v1/status"),projection); await settle();
nav.find((item)=>item.dataset.view==="team_state").click(); await settle();
respond(take("/api/v1/team/membership"),{state:"member-active",action:"join",session_id:"opaque-session",can_cancel:false}); await settle();
button("Review team-state publication").click(); await settle();
const preview=take("/api/v1/team/membership/publish-preview");
respond(preview,{state:"publication_preview",action:"join",session_id:"opaque-session",can_cancel:false,preview:{bundle_digest:"sha256:bundle"}}); await settle();
button("Authorize team-state publication with WebAuthn").click(); await settle();
respond(take("/api/v1/team/membership/publish-options"),{publicKey:{challenge:"Y2hhbGxlbmdl",allowCredentials:[],userVerification:"preferred"}}); await settle();
const verification=take("/api/v1/team/membership/publish-verify");
respond(verification,{state:"publication_pending",repository_id:"github.com/acme/project",pull_request_url:"https://github.com/acme/project/pull/9"}); await settle();
process.stdout.write(JSON.stringify({getCount,preview:JSON.parse(preview.init.body),verification:JSON.parse(verification.init.body),text:app.textContent}));
""")
    assert result["getCount"] == 1
    assert result["preview"] == {"session_id": "opaque-session"}
    assert set(result["verification"]) == {"session_id", "response"}
    assert result["verification"]["session_id"] == "opaque-session"
    assert "publication_pending" in result["text"]
    assert "assertion-secret" not in result["text"]
