(() => {
  "use strict";

  const api = Object.freeze({
    status: "/api/v1/status",
    inbox: "/api/v1/inbox",
    proposal: "/api/v1/proposals/",
    clarificationAnswerPreview: "/api/v1/clarifications/answers/preview",
    clarificationAnswerDiscard: "/api/v1/clarifications/answers/discard",
    registrationOptions: "/api/v1/webauthn/register/options",
    registrationVerify: "/api/v1/webauthn/register/verify",
    teamEnrollment: "/api/v1/team/enrollment",
    teamEnrollmentOptions: "/api/v1/team/enrollment/options",
    teamEnrollmentVerify: "/api/v1/team/enrollment/verify",
    teamEnrollmentCancel: "/api/v1/team/enrollment/cancel",
    teamPublicationPreview: "/api/v1/team/publication/preview",
    decisionOptions: "/api/v1/decisions/options",
    decisionVerify: "/api/v1/decisions/verify",
    developmentObservation: "/api/v1/development/observation",
    reviewedTests: "/api/v1/development/tests/run",
    browserBootstrap: "/_intent/browser/bootstrap",
  });
  const viewNames = new Set(["home", "onboarding", "inbox", "proposal", "team_state"]);
  const app = document.getElementById("app");
  const statusRegion = document.getElementById("status");
  const navigation = Array.from(document.querySelectorAll("[data-view]"));
  const state = {
    view: "home",
    status: null,
    inbox: null,
    proposalId: null,
    preview: null,
    selectedNodeIds: [],
    proposalGeneration: 0,
    developmentObservation: null,
    teamEnrollment: null,
    teamPublication: null,
  };
  let csrfToken = readCsrfBootstrap();

  function readCsrfBootstrap() {
    const fragment = new URLSearchParams(window.location.hash.slice(1));
    const candidate = fragment.get("csrf");
    history.replaceState(null, "", window.location.pathname);
    return typeof candidate === "string" && /^[A-Za-z0-9_-]{1,512}$/.test(candidate)
      ? candidate
      : "";
  }

  function announce(message) {
    statusRegion.textContent = message;
  }

  async function exchangeCsrfBootstrap() {
    if (!csrfToken) {
      return;
    }
    const response = await fetch(api.browserBootstrap, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ token: csrfToken }),
      credentials: "same-origin",
      cache: "no-store",
      redirect: "error",
    });
    if (!response.ok) {
      csrfToken = "";
      throw new Error("Local review bootstrap is unavailable. Restart intent dev.");
    }
  }

  function isWrite(method) {
    return method !== "GET";
  }

  async function fetchJson(path, options = {}) {
    const method = options.method || "GET";
    const headers = new Headers(options.headers || {});
    headers.set("accept", "application/json");
    if (isWrite(method)) {
      headers.set("content-type", "application/json");
      if (!csrfToken) {
        throw new Error("Local review bootstrap is unavailable. Restart intent dev.");
      }
      headers.set("x-intent-csrf", csrfToken);
    }
    const response = await fetch(path, {
      method,
      headers,
      body: options.body,
      credentials: "same-origin",
      cache: "no-store",
      redirect: "error",
    });
    let body = "";
    try {
      body = await response.text();
      if (!response.ok) {
        throw new Error(response.status === 503 ? "challenge_unavailable" : "request_rejected");
      }
      return JSON.parse(body);
    } finally {
      body = "";
    }
  }

  function addText(parent, tag, value) {
    const element = document.createElement(tag);
    element.textContent = String(value);
    parent.append(element);
    return element;
  }

  function addValue(parent, label, value) {
    const term = document.createElement("dt");
    term.textContent = label;
    const description = document.createElement("dd");
    if (Array.isArray(value)) {
      const list = document.createElement("ul");
      for (const item of value) {
        const entry = document.createElement("li");
        if (item !== null && typeof item === "object") {
          const nested = document.createElement("dl");
          for (const [key, nestedValue] of Object.entries(item)) {
            addValue(nested, key, nestedValue);
          }
          entry.append(nested);
        } else {
          entry.textContent = String(item);
        }
        list.append(entry);
      }
      description.append(list);
    } else if (value !== null && typeof value === "object") {
      const nested = document.createElement("dl");
      for (const [key, nestedValue] of Object.entries(value)) {
        addValue(nested, key, nestedValue);
      }
      description.append(nested);
    } else {
      description.textContent = String(value);
    }
    parent.append(term, description);
  }

  function addProjection(panel, projection) {
    const details = document.createElement("dl");
    for (const [key, value] of Object.entries(projection || {})) {
      addValue(details, key, value);
    }
    panel.append(details);
  }

  function actionButton(label, handler, className = "") {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = label;
    if (className) {
      button.className = className;
    }
    button.addEventListener("click", handler);
    return button;
  }

  function panel(title) {
    const section = document.createElement("section");
    section.className = "panel";
    section.setAttribute("aria-labelledby", "view-title");
    addText(section, "h2", title).id = "view-title";
    return section;
  }

  function updateNavigation() {
    for (const button of navigation) {
      const selected = button.dataset.view === state.view;
      if (selected) {
        button.setAttribute("aria-current", "page");
      } else {
        button.removeAttribute("aria-current");
      }
    }
  }

  function renderHome() {
    const section = panel("Home");
    if (state.status) {
      addProjection(section, state.status);
    } else {
      addText(section, "p", "Loading local review status.");
    }
    section.append(actionButton("Refresh readiness", refreshStatus));
    if (state.developmentObservation) {
      addText(section, "h3", "Development evidence");
      addProjection(section, state.developmentObservation);
      for (const commandId of state.developmentObservation.command_ids || []) {
        section.append(
          actionButton("Run reviewed tests", () => runReviewedTests(commandId))
        );
      }
    }
    section.append(
      actionButton("Refresh development evidence", () => refreshDevelopmentObservation(false))
    );
    return section;
  }

  function renderOnboarding() {
    const section = panel("Onboarding");
    addText(
      section,
      "p",
      "Review submitted onboarding proposals in Inbox, then enroll this device before signing a decision."
    );
    section.append(actionButton("Register this device with WebAuthn", registerDevice));
    return section;
  }

  function renderInbox() {
    const section = panel("Inbox");
    const inbox = state.inbox;
    if (!inbox) {
      addText(section, "p", "Loading review items.");
      return section;
    }
    const items = [
      ...(inbox.pending_proposal_ids || []),
      ...(inbox.open_case_ids || []),
    ];
    const clarificationSessions = Array.isArray(inbox.clarification_sessions)
      ? inbox.clarification_sessions
      : [];
    if (items.length > 0) {
      const list = document.createElement("ul");
      for (const identifier of items) {
        const entry = document.createElement("li");
        entry.append(
          actionButton(`Review ${identifier}`, () => {
            selectProposal(identifier);
            showView("proposal");
          })
        );
        list.append(entry);
      }
      section.append(list);
    }
    for (const session of clarificationSessions) {
      if (!session || typeof session !== "object" || !Array.isArray(session.questions)) {
        continue;
      }
      const clarification = document.createElement("article");
      addText(clarification, "h3", `Clarification ${session.id}`);
      addText(clarification, "p", `Task: ${session.task_id}`);
      for (const question of session.questions) {
        if (!question || typeof question !== "object") {
          continue;
        }
        const fieldset = document.createElement("fieldset");
        addText(fieldset, "legend", question.prompt);
        const label = document.createElement("label");
        label.textContent = `Answer ${question.id}${question.required ? " (required)" : ""}`;
        const answer = document.createElement("textarea");
        answer.maxLength = 16384;
        answer.required = Boolean(question.required);
        label.append(answer);
        fieldset.append(
          label,
          actionButton(`Preview answer for ${question.id}`, () =>
            previewClarificationAnswer(session.id, question.id, answer)
          )
        );
        clarification.append(fieldset);
      }
      section.append(clarification);
    }
    if (items.length === 0 && clarificationSessions.length === 0) {
      addText(section, "p", "No visible proposals, cases, or clarification questions need review.");
    }
    section.append(actionButton("Refresh inbox", loadInbox));
    return section;
  }

  function selectionRequired(payload) {
    return Boolean(
      payload &&
      (payload.action === "confirm_baseline" || payload.action === "confirm_proposal")
    );
  }

  function selectionSatisfied(payload) {
    return !selectionRequired(payload) || state.selectedNodeIds.length > 0;
  }

  function renderSelectedNodes(payload) {
    const fieldset = document.createElement("fieldset");
    const legend = document.createElement("legend");
    legend.textContent = "Selected nodes bound to this decision";
    fieldset.append(legend);
    if (state.selectedNodeIds.length === 0) {
      addText(
        fieldset,
        "p",
        selectionRequired(payload)
          ? "This decision requires at least one server-bound selected node."
          : "This action is explicitly bound to an empty node selection."
      );
      return fieldset;
    }
    for (const nodeId of state.selectedNodeIds) {
      const label = document.createElement("label");
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = true;
      checkbox.disabled = true;
      label.append(checkbox, document.createTextNode(` Selected node: ${nodeId}`));
      fieldset.append(label);
    }
    addText(fieldset, "p", "The server-generated decision payload fixes this selection.");
    return fieldset;
  }

  function renderDecisionSummary(section, payload) {
    const summary = document.createElement("dl");
    summary.className = "decision-summary";
    const subject = payload && payload.subject;
    addValue(summary, "Action", payload && payload.action);
    addValue(
      summary,
      "Subject",
      subject && typeof subject === "object" ? `${subject.kind}: ${subject.id}` : "unavailable"
    );
    addValue(summary, "Result digest", payload && payload.result_digest);
    section.append(summary);
  }

  function renderProposal() {
    const section = panel("Proposal review");
    if (!state.preview) {
      if (state.proposalId) {
        addText(section, "p", "Loading the complete proposal and evidence preview.");
      } else {
        addText(section, "p", "Choose a proposal or case in Inbox to review its complete preview.");
        section.append(actionButton("Open inbox", () => showView("inbox")));
      }
      return section;
    }
    addText(section, "h3", "Exact preview");
    addProjection(section, state.preview.preview);
    addText(section, "h3", "Decision binding");
    addProjection(section, state.preview.payload);
    const payload = state.preview.payload;
    renderDecisionSummary(section, payload);
    section.append(renderSelectedNodes(payload));
    const actions = document.createElement("div");
    actions.className = "actions";
    const authorize = actionButton(
      `Authorize ${decisionLabel(payload)} with WebAuthn`,
      authorizeDecision,
      "danger"
    );
    authorize.disabled = !selectionSatisfied(payload);
    actions.append(authorize, actionButton("Cancel review without applying a decision", cancelReview));
    section.append(actions);
    return section;
  }

  function renderTeamState() {
    const section = panel("Team state");
    addText(
      section,
      "p",
      "This local projection shows the available shared-state readiness only; it does not disclose credentials."
    );
    if (state.status) {
      addProjection(section, state.status);
    }
    if (state.teamEnrollment) {
      addText(section, "h3", "Recipient enrollment");
      addProjection(section, state.teamEnrollment);
    }
    if (state.teamPublication) {
      addText(section, "h3", "Pending publication preview");
      addProjection(section, state.teamPublication.preview);
    }
    const proofLabel = document.createElement("label");
    proofLabel.textContent = "GitHub device authorization proof";
    const proof = document.createElement("input");
    proof.type = "password";
    proof.autocomplete = "off";
    proof.maxLength = 16384;
    proofLabel.append(proof);
    section.append(
      proofLabel,
      actionButton("Verify GitHub identity and enroll this device", () => enrollTeamRecipient(proof)),
      actionButton("Preview reviewed team-state publication", loadTeamPublicationPreview),
      actionButton("Refresh team-state readiness", refreshTeamEnrollment)
    );
    return section;
  }

  function render() {
    updateNavigation();
    let next = null;
    if (state.view === "onboarding") {
      next = renderOnboarding();
    } else if (state.view === "inbox") {
      next = renderInbox();
    } else if (state.view === "proposal") {
      next = renderProposal();
    } else if (state.view === "team_state") {
      next = renderTeamState();
    } else {
      next = renderHome();
    }
    app.replaceChildren(next);
  }

  function clearPreview() {
    state.preview = null;
    state.selectedNodeIds = [];
    state.proposalId = null;
  }

  function discardProposalReview() {
    state.proposalGeneration += 1;
    clearPreview();
  }

  function selectProposal(proposalId) {
    discardProposalReview();
    state.proposalId = proposalId;
  }

  function payloadMatchesProposal(payload, proposalId) {
    return (
      payload &&
      typeof payload === "object" &&
      payload.subject &&
      typeof payload.subject === "object" &&
      payload.subject.id === proposalId
    );
  }

  function selectionMatchesPayload(payload, proposalId) {
    return (
      payloadMatchesProposal(payload, proposalId) &&
      Array.isArray(payload.selected_node_ids) &&
      payload.selected_node_ids.length === state.selectedNodeIds.length &&
      payload.selected_node_ids.every(
        (nodeId, index) => nodeId === state.selectedNodeIds[index]
      )
    );
  }

  function currentReviewMatches(proposalId, generation, preview) {
    return (
      state.proposalId === proposalId &&
      state.proposalGeneration === generation &&
      state.preview === preview &&
      selectionMatchesPayload(preview && preview.payload, proposalId)
    );
  }

  function decisionLabel(payload) {
    const subject = payload && payload.subject;
    const subjectId = subject && typeof subject.id === "string" ? subject.id : "unavailable subject";
    const actions = {
      confirm_baseline: "confirm baseline",
      answer_clarification: "answer clarification",
      confirm_proposal: "confirm proposal",
      resolve_conflict: "resolve conflict",
      publish_state: "publish state",
      approve_external_write: "approve external write",
    };
    const action = payload && typeof payload.action === "string" ? payload.action : "authorize";
    return `${actions[action] || action} ${subjectId}`;
  }

  function pendingAnswerId(payload) {
    const subject = payload && payload.subject;
    return payload &&
      payload.action === "answer_clarification" &&
      subject &&
      subject.kind === "answer" &&
      typeof subject.id === "string" &&
      /^answer:[0-9a-f]{64}$/.test(subject.id)
      ? subject.id
      : null;
  }

  async function discardPendingAnswer(payload) {
    const answerId = pendingAnswerId(payload);
    if (!answerId) {
      return;
    }
    await fetchJson(api.clarificationAnswerDiscard, {
      method: "POST",
      body: JSON.stringify({ answer_id: answerId }),
    });
  }

  async function cancelReview() {
    const payload = state.preview && state.preview.payload;
    discardProposalReview();
    state.view = "inbox";
    render();
    announce("Review cancelled. No decision was sent.");
    try {
      await discardPendingAnswer(payload);
    } catch (_error) {
      announce("Review cancelled. The private answer will expire without authority.");
    }
  }

  async function previewClarificationAnswer(sessionId, questionId, field) {
    let answer = typeof field.value === "string" ? field.value : "";
    const inbox = state.inbox;
    if (!answer) {
      announce("Enter an answer before requesting an authoritative preview.");
      return;
    }
    try {
      const preview = await fetchJson(api.clarificationAnswerPreview, {
        method: "POST",
        body: JSON.stringify({ session_id: sessionId, question_id: questionId, answer }),
      });
      const payload = preview && preview.payload;
      if (
        state.view !== "inbox" ||
        state.inbox !== inbox ||
        !payload ||
        payload.action !== "answer_clarification" ||
        !Array.isArray(payload.selected_node_ids) ||
        payload.selected_node_ids.length !== 0 ||
        !pendingAnswerId(payload)
      ) {
        throw new Error("Clarification answer preview binding changed.");
      }
      discardProposalReview();
      state.proposalId = payload.subject.id;
      state.preview = preview;
      state.selectedNodeIds = [];
      state.view = "proposal";
      render();
      announce("Answer preview loaded. Confirm its digest with WebAuthn to submit it.");
    } catch (_error) {
      announce("Answer preview is unavailable. No answer was submitted.");
    } finally {
      field.value = "";
      answer = "";
    }
  }

  async function refreshStatus() {
    try {
      state.status = await fetchJson(api.status);
      if (state.view === "home" || state.view === "team_state") {
        render();
      }
      announce("Local review status updated.");
    } catch (_error) {
      announce("Local review status is unavailable. No decision was applied.");
    }
  }

  async function refreshTeamEnrollment(quiet = false) {
    try {
      state.teamEnrollment = await fetchJson(api.teamEnrollment);
      if (state.view === "team_state") {
        render();
      }
      if (!quiet) {
        announce("Team recipient status updated.");
      }
    } catch (_error) {
      announce("Team recipient status is unavailable.");
    }
  }

  async function loadTeamPublicationPreview() {
    try {
      state.teamPublication = await fetchJson(api.teamPublicationPreview);
      if (state.view === "team_state") {
        render();
      }
      announce("Exact team-state publication preview loaded for review.");
    } catch (_error) {
      state.teamPublication = null;
      announce("Team-state publication is unavailable. Nothing was published.");
    }
  }

  async function loadInbox() {
    try {
      state.inbox = await fetchJson(api.inbox);
      if (state.view === "inbox") {
        render();
      }
      announce("Inbox updated.");
    } catch (_error) {
      announce("Inbox is unavailable. No decision was applied.");
    }
  }

  async function refreshDevelopmentObservation(quiet = false) {
    try {
      state.developmentObservation = await fetchJson(api.developmentObservation);
      if (state.view === "home") {
        render();
      }
      if (!quiet) {
        announce("Development evidence updated.");
      }
    } catch (_error) {
      if (!quiet) {
        announce("Development evidence is temporarily unavailable.");
      }
    }
  }

  async function runReviewedTests(commandId) {
    if (typeof commandId !== "string" || !/^test:sha256:[0-9a-f]{64}$/.test(commandId)) {
      announce("Reviewed test selection is unavailable.");
      return;
    }
    try {
      const result = await fetchJson(api.reviewedTests, {
        method: "POST",
        body: JSON.stringify({ command_id: commandId }),
      });
      announce(`Reviewed tests finished with status ${result.status}.`);
      await refreshDevelopmentObservation(true);
    } catch (_error) {
      announce("Reviewed tests could not run. No completion was asserted.");
    }
  }

  async function loadPreview() {
    if (!state.proposalId) {
      return;
    }
    const proposalId = state.proposalId;
    const generation = state.proposalGeneration;
    try {
      const preview = await fetchJson(`${api.proposal}${encodeURIComponent(proposalId)}`);
      if (state.proposalId !== proposalId || state.proposalGeneration !== generation) {
        return;
      }
      const payload = preview && preview.payload;
      const selectedNodeIds = Array.isArray(payload && payload.selected_node_ids)
        ? payload.selected_node_ids.filter((value) => typeof value === "string")
        : [];
      if (!payloadMatchesProposal(payload, proposalId)) {
        throw new Error("Preview binding does not match the selected review item.");
      }
      state.preview = preview;
      state.selectedNodeIds = selectedNodeIds;
      if (state.view === "proposal") {
        render();
      }
      announce("Complete proposal preview loaded. Review the evidence and selected nodes.");
    } catch (_error) {
      if (state.proposalId === proposalId && state.proposalGeneration === generation) {
        discardProposalReview();
        render();
        announce("Proposal preview is unavailable. No decision was applied.");
      }
    }
  }

  async function showView(view) {
    if (!viewNames.has(view)) {
      return;
    }
    if (view !== "proposal" && state.view === "proposal") {
      discardProposalReview();
    }
    state.view = view;
    render();
    app.focus();
    if (view === "inbox") {
      await loadInbox();
    } else if (view === "proposal") {
      await loadPreview();
    } else if (view === "team_state") {
      await refreshTeamEnrollment();
    }
  }

  function base64urlToBuffer(value) {
    if (typeof value !== "string" || !/^[A-Za-z0-9_-]+$/.test(value)) {
      throw new Error("Invalid WebAuthn base64url value.");
    }
    const padded = value.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(value.length / 4) * 4, "=");
    const binary = atob(padded);
    const bytes = new Uint8Array(binary.length);
    for (let index = 0; index < binary.length; index += 1) {
      bytes[index] = binary.charCodeAt(index);
    }
    return bytes.buffer;
  }

  function bufferToBase64url(buffer) {
    const bytes = new Uint8Array(buffer);
    let binary = "";
    for (const byte of bytes) {
      binary += String.fromCharCode(byte);
    }
    return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
  }

  function clearBuffer(value) {
    if (value instanceof ArrayBuffer) {
      new Uint8Array(value).fill(0);
    }
  }

  function clearOptionBuffers(options) {
    const publicKey = options && options.publicKey;
    if (!publicKey || typeof publicKey !== "object") {
      return;
    }
    clearBuffer(publicKey.challenge);
    if (publicKey.user) {
      clearBuffer(publicKey.user.id);
    }
    for (const credential of publicKey.excludeCredentials || []) {
      clearBuffer(credential.id);
    }
    for (const credential of publicKey.allowCredentials || []) {
      clearBuffer(credential.id);
    }
  }

  function convertCredentialDescriptors(descriptors) {
    if (!Array.isArray(descriptors)) {
      return;
    }
    for (const descriptor of descriptors) {
      if (!descriptor || typeof descriptor !== "object") {
        throw new Error("Invalid WebAuthn credential descriptor.");
      }
      descriptor.id = base64urlToBuffer(descriptor.id);
    }
  }

  function creationOptions(options) {
    const publicKey = options && options.publicKey;
    if (!publicKey || typeof publicKey !== "object" || !publicKey.user || typeof publicKey.user !== "object") {
      throw new Error("Invalid registration options.");
    }
    publicKey.challenge = base64urlToBuffer(publicKey.challenge);
    publicKey.user.id = base64urlToBuffer(publicKey.user.id);
    convertCredentialDescriptors(publicKey.excludeCredentials);
    publicKey.authenticatorSelection = publicKey.authenticatorSelection || {};
    publicKey.authenticatorSelection.userVerification = "required";
    return options;
  }

  function requestOptions(options) {
    const publicKey = options && options.publicKey;
    if (!publicKey || typeof publicKey !== "object") {
      throw new Error("Invalid authentication options.");
    }
    publicKey.challenge = base64urlToBuffer(publicKey.challenge);
    convertCredentialDescriptors(publicKey.allowCredentials);
    publicKey.userVerification = "required";
    return options;
  }

  function serializeCredential(credential) {
    if (!credential || credential.type !== "public-key" || !credential.response) {
      throw new Error("Invalid WebAuthn credential.");
    }
    const response = credential.response;
    const result = {
      id: credential.id,
      rawId: bufferToBase64url(credential.rawId),
      type: credential.type,
      response: {
        clientDataJSON: bufferToBase64url(response.clientDataJSON),
      },
    };
    if (response.attestationObject instanceof ArrayBuffer) {
      result.response.attestationObject = bufferToBase64url(response.attestationObject);
    } else {
      result.response.authenticatorData = bufferToBase64url(response.authenticatorData);
      result.response.signature = bufferToBase64url(response.signature);
      result.response.userHandle = response.userHandle ? bufferToBase64url(response.userHandle) : null;
    }
    return result;
  }

  function clearSerializedCredential(response) {
    if (!response || typeof response !== "object") {
      return;
    }
    response.id = "";
    response.rawId = "";
    if (response.response) {
      for (const key of Object.keys(response.response)) {
        response.response[key] = null;
      }
    }
  }

  function cancellation(error) {
    return error && (error.name === "AbortError" || error.name === "NotAllowedError");
  }

  async function registerDevice() {
    let options = null;
    let credential = null;
    let response = null;
    let result = null;
    try {
      options = await fetchJson(api.registrationOptions, { method: "POST", body: "{}" });
      credential = await navigator.credentials.create({ publicKey: creationOptions(options).publicKey });
      response = serializeCredential(credential);
      result = await fetchJson(api.registrationVerify, {
        method: "POST",
        body: JSON.stringify({ response }),
      });
      announce("Device registration completed. Credential details are not shown or retained.");
    } catch (error) {
      announce(
        cancellation(error)
          ? "Device registration cancelled. No credential response was sent."
          : "Device registration could not complete. No decision was applied."
      );
    } finally {
      clearOptionBuffers(options);
      clearSerializedCredential(response);
      result = null;
      response = null;
      credential = null;
      options = null;
    }
  }

  async function enrollTeamRecipient(field) {
    let identityProof = typeof field.value === "string" ? field.value : "";
    let options = null;
    let credential = null;
    let response = null;
    let result = null;
    let completed = false;
    if (!identityProof) {
      announce("Complete GitHub device authorization before team enrollment.");
      return;
    }
    try {
      options = await fetchJson(api.teamEnrollmentOptions, {
        method: "POST",
        body: JSON.stringify({ identity_proof: identityProof }),
      });
      field.value = "";
      identityProof = "";
      credential = await navigator.credentials.create({ publicKey: creationOptions(options).publicKey });
      response = serializeCredential(credential);
      result = await fetchJson(api.teamEnrollmentVerify, {
        method: "POST",
        body: JSON.stringify({ response }),
      });
      completed = true;
      announce("Team recipient enrolled after GitHub identity and WebAuthn verification.");
      await refreshTeamEnrollment(true);
    } catch (error) {
      if (!completed) {
        try {
          await fetchJson(api.teamEnrollmentCancel, { method: "POST", body: "{}" });
        } catch (_cancelError) {
          // The server also expires abandoned enrollment authority.
        }
      }
      announce(
        cancellation(error)
          ? "Team enrollment cancelled. No recipient key was created."
          : "Team enrollment could not complete. The project remains local-only."
      );
    } finally {
      field.value = "";
      identityProof = "";
      clearOptionBuffers(options);
      clearSerializedCredential(response);
      result = null;
      response = null;
      credential = null;
      options = null;
    }
  }

  async function authorizeDecision() {
    const proposalId = state.proposalId;
    const generation = state.proposalGeneration;
    const preview = state.preview;
    if (
      !proposalId ||
      !preview ||
      !selectionSatisfied(preview.payload) ||
      !currentReviewMatches(proposalId, generation, preview)
    ) {
      announce("Review the exact server-bound decision before authorizing it.");
      return;
    }
    let payload = preview.payload;
    let options = null;
    let credential = null;
    let response = null;
    let result = null;
    let applied = false;
    try {
      options = await fetchJson(api.decisionOptions, {
        method: "POST",
        body: JSON.stringify({ payload }),
      });
      credential = await navigator.credentials.get({ publicKey: requestOptions(options).publicKey });
      if (!currentReviewMatches(proposalId, generation, preview)) {
        throw new Error("Review selection changed before verification.");
      }
      response = serializeCredential(credential);
      result = await fetchJson(api.decisionVerify, {
        method: "POST",
        body: JSON.stringify({ response, payload }),
      });
      applied = true;
      announce("Decision applied after user-verifying WebAuthn confirmation.");
    } catch (error) {
      announce(
        cancellation(error)
          ? "WebAuthn review cancelled. No decision was sent."
          : "The challenge may have expired or changed. Review the proposal again; no decision was applied."
      );
    } finally {
      if (!applied) {
        try {
          await discardPendingAnswer(payload);
        } catch (_error) {
          // The server also expires abandoned private previews at the authority deadline.
        }
      }
      clearOptionBuffers(options);
      clearSerializedCredential(response);
      result = null;
      response = null;
      credential = null;
      options = null;
      payload = null;
      if (currentReviewMatches(proposalId, generation, preview)) {
        discardProposalReview();
        if (state.view === "proposal") {
          state.view = "inbox";
        }
        render();
      }
    }
    if (applied) {
      void refreshStatus();
    }
  }

  for (const button of navigation) {
    button.addEventListener("click", () => showView(button.dataset.view));
  }

  async function start() {
    try {
      await exchangeCsrfBootstrap();
      render();
      await refreshStatus();
      await refreshDevelopmentObservation(true);
    } catch (_error) {
      render();
      announce("Local review bootstrap is unavailable. Restart intent dev.");
    }
  }

  void start();
})();
