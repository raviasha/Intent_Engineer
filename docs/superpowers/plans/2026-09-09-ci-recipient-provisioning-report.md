# CI recipient provisioning follow-up report

## Implemented scope

The setup-generated encrypted release now includes an independent CI X25519
recipient. It is a machine record, not a fabricated human/GitHub/WebAuthn identity.
The self-hosted runner service account provisions/reuses its key in its OS keyring;
only a public descriptor is exported. Developer setup imports that descriptor into
the exact preview and WebAuthn-authorized publication authority. Publication review
exposes public CI trust containing the machine descriptor and reviewed Ed25519 keys.
The state validator reads a protected runner-local public config outside the checkout,
checks ownership/modes/canonical bytes and exact keyring/public-key binding, and never
falls back to environment private-key JSON. Missing key/config errors are actionable.

No commit, push, live GitHub writes, or real keyring provisioning was performed.

## Files owned by this follow-up

New source: `src/intent_engineering/team_state/ci.py`, `cli/team_ci.py`.
Changed source: `team_state/models.py`, `crypto.py`, `publication.py`, `candidate.py`,
`setup.py`, `cli/team.py`, `integrations/protected_ci.py` (all under
`src/intent_engineering/`). `setup.py` and `cli/team.py` also contain separate
adapter-worker permission/CODEOWNERS/pending-merge edits, preserved in place.
The final parent-requested follow-up also changes
`control_plane/assets/app.js` for the pending publication browser state.

Tests: new `tests/unit/team_state/test_ci_recipient.py`; changed
`tests/unit/team_state/test_crypto.py`,
`tests/integration/team_state/test_publication.py`,
`tests/integration/team_state/test_candidate_validation.py`,
`tests/e2e/test_cli_team_github.py`, `tests/e2e/test_team_setup_bridge.py`.
The final pending browser regression is in `tests/e2e/test_team_setup_browser.py`.

Docs: `docs/ci-recipient.md`, `docs/github.md`, `docs/intent-aware-agent.md`,
the approved `docs/superpowers/specs/2026-09-09-ci-recipient-provisioning-design.md`
and `docs/superpowers/plans/2026-09-09-ci-recipient-provisioning.md`.

## TDD and evidence

Observed RED before each behavior change: absent machine model/wrapping; publication
rejecting machine records; missing CI CLI/provision store; wrong hosted runner and
missing setup descriptor guard; production setup manifest omitting CI key; candidate
validator ignoring runner-local trust; missing-key diagnostic being swallowed;
boolean schema accepted; lexical `..` checkout-path bypass; provider secret exception
remaining in public traceback locals. Each was followed by focused GREEN.

Fresh CI-only gate:

```sh
PYTHONPATH=src PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest --import-mode=importlib -p anyio.pytest_plugin tests/unit/team_state/test_ci_recipient.py tests/unit/team_state/test_crypto.py tests/integration/team_state/test_publication.py tests/e2e/test_cli_team_github.py tests/integration/team_state/test_candidate_validation.py -q --tb=short
```

Result: **67 passed**. The real setup normal/lost-response/cancel journeys also passed
with actual recorded encrypted bytes decrypted using the independent runner key;
the same journey was extended with the separate adapter worker's pre-merge no-trust
and post-merge receipt expectations. The final bridge gate passed **14 tests**, with
a distinct rewritten merged SHA, sole reviewed parent, and exact tree/blob verification.

Combined changed-file Ruff and `ruff format --check`: clean (19 Python files,
including the concurrent adapter changes). `git diff --check`: clean. Fresh strict
mypy: **160 source files clean**.

Combined Task 6 gate:

```sh
PYTHONPATH=src PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest --import-mode=importlib -p anyio.pytest_plugin tests/unit/team_state tests/integration/team_state tests/e2e/test_cli_team_github.py tests/e2e/test_team_setup_bridge.py tests/e2e/test_team_setup_browser.py tests/e2e/test_team_enrollment.py tests/e2e/test_protected_ci.py tests/unit/capture/github/test_client.py -q --tb=short
```

Result: **520 passed, 1 failed** in 72.17s. The sole failure was the known sandbox
offline `uv build` wheel-cache permission failure in
`test_installed_wheel_contains_the_protected_dependency_lock`; no behavior test failed.

After that gate, parent requested pending browser handling. Observed RED for absent
automatic inspect and missing pending PR URL; implemented server-derived URL, explicit
merge/refresh guidance, automatic restart inspect and a refresh button that finalizes
without WebAuthn reenrollment. Affected gate:

```sh
PYTHONPATH=src PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest --import-mode=importlib -p anyio.pytest_plugin tests/e2e/test_team_setup_browser.py tests/e2e/test_team_setup_bridge.py -q --tb=short
```

Result: **21 passed**. Final Ruff (20 changed Python files), browser JS `node --check`,
strict mypy (160 source files), and `git diff --check` are clean. No command remains
running; no commit or push was made.

## Deployment tradeoffs and boundaries

- This is deliberately self-hosted only: generated workflow targets
  `[self-hosted, intent-state]`, never a hosted runner or GitHub-secret private JSON.
- The operator installs reviewed public trust outside all checkouts and configures
  `INTENT_CI_TRUST_PATH` in the runner service environment. Directory/file integrity,
  keyring availability after restart, and exclusive trusted workflow access are
  deployment prerequisites. A process running as that service account can access
  its keyring; candidate builds must not share it.
- Existing environment-trust APIs remain for explicit compatibility/test callers.
  Existing code-check/sync workflows need a separate migration; docs no longer
  recommend uploading private recipient keys to GitHub.
- Hosted execution requires an independently operated OIDC/external broker,
  explicitly documented as an unimplemented extension.
- Rotation means a new independent runner scope and a reviewed recipient-set
  publication, not silent key overwrite. Setup remains first-baseline-only.
- This follow-up does not prove live GitHub or host-keyring deployment. All external
  transports/backends in tests are deterministic offline boundaries; crypto,
  publication, config validation and inert Git candidate validation run for real.

## Reviewed tooling and recovery integration follow-up

Updated `tests/e2e/test_team_setup_bridge.py` with complete `repo, admin:org`
credential headers; protected default-branch baseline and exact tooling tree/raw
blob responses; workflow-restricted organization runner group with exact registered
runner membership; and effective no-bypass workflow ruleset targeting `intent-state`.
The creation-only exception is `do_not_enforce_on_create: true`; subsequent updates
remain governed by the exact default-ref workflow rule. The generated runner selector
is an explicit group plus labels object.

Real production journey now verifies local code staging is a separate WebAuthn phase,
with no provider/signing mutations; remote tooling must be merged and independently
verified before protection authorization. Added durable bootstrap lost-response
recovery, exact one-time orphan creation, cancellation refusal for provider receipts,
and conservative canonical legacy-draft migration (both pre-network and PR-receipt
cases). All journeys retain independent CI decryption and exact post-merge trust.

Updated `tests/e2e/test_team_setup_browser.py` and the shipped browser asset for
server-provided prerequisite/staged guidance without stale authorization buttons,
retry after default-branch prerequisites are met, recovery-only actions, and truthful
cancel-refusal messaging. Observed RED for invisible remote-tooling guidance,
stranded prerequisite state and incorrect expiring-authority cancellation text;
each corresponding browser fix was followed by GREEN.

Updated `docs/ci-recipient.md`, `docs/github.md`, and `docs/intent-aware-agent.md` for
organization-only restricted runner groups, exact runner names, separate setup-token
and CI permissions, prerequisite baseline protection, staged/remote/protection order,
the state-target/default-source ruleset distinction, no bypass, supplementary app ID,
and durable recovery semantics. Default-branch code-check and state-check rules are
explicitly distinguished; no recipient secrets are recommended for GitHub storage.

Fresh combined integration/docs verification:

```sh
PYTHONPATH=src PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest --import-mode=importlib -p anyio.pytest_plugin tests/e2e/test_team_setup_bridge.py tests/e2e/test_team_setup_browser.py tests/e2e/test_cli_team_github.py tests/integration/github/test_github_docs.py tests/e2e/test_public_alpha.py::test_public_alpha_docs_and_bindings_match_the_shipped_operating_model -q --tb=short
```

Result: **46 passed in 14.36s**. Fresh scoped Ruff/format, browser JavaScript syntax,
strict mypy (**160 source files**), and `git diff --check` are clean. Adapter/CI source
was preserved; the adapter worker independently fixed the source regressions exposed
by these integration tests. No commit or push was performed.
