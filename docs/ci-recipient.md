# Dedicated CI recipient for encrypted intent state

GitHub setup currently supports an **organization-owned repository** with a separately
provisioned **self-hosted** runner recipient and an organization runner group restricted
to the exact two reviewed state-validation and code-check workflows. Personal
repositories and unrestricted/shared runner groups are not supported by this setup path.
Developer private keys never leave their OS keyring. Runner private keys also stay
in that runner service account's OS keyring: do not put them in GitHub Secrets,
Actions variables, Git, `.intent`, artifacts, command arguments, or logs.

## Provision and review

1. Before staging anything, protect the default branch with enforced admins,
   required PR approval, stale-review dismissal, no bypass allowances, and disabled
   force pushes and deletions. Setup verifies this baseline before allowing local
   suggestion writes. After the reviewed tooling merge, it additionally verifies
   code-owner enforcement and the exact installed workflow/CODEOWNERS bytes.

   Local setup requires a classic GitHub token with `repo` and `admin:org` scopes,
   repository admin/read/write permission, and access to inspect the organization's
   runner group. Supply it through the existing `GH_TOKEN` or `gh auth login` path;
   never put it in a workflow, descriptor, or public trust file. These setup-write
   privileges are distinct from the generated workflow's read-only `GITHUB_TOKEN`.

   Configure the organization's non-default runner group named `intent-state` with
   selected-repository visibility for this repository and restricted workflow access
   to exactly `acme/project/.github/workflows/intent-state.yml@refs/heads/main` and
   `acme/project/.github/workflows/intent-check.yml@refs/heads/main`
   (substitute the real repository and default branch). Register the runner in that
   group with the exact name `release-01` used below and labels `self-hosted` and
   `intent-state`. The descriptor's `--runner` value must match the registered runner
   name, not merely a label. Setup checks the actual group's runner membership.
   Do not execute candidate code on the host or admit other workflows to this group.
   Code checks use only the existing disposable, secret-free immutable sandbox;
   state validation never executes candidate code. Both protected launchers use the
   same independent CI recipient, without exporting a developer key or provisioning
   another recipient. The runner must meet the Linux/Docker isolation requirements in
   [required check setup](intent-aware-agent.md#required-github-check-setup).
   Configure a working OS keyring backend accessible to the runner service
   account (including after restart); a plaintext/file keyring backend is not
   an acceptable deployment. Run these commands **as that service account**:

   ```sh
   intent team ci provision --project-id project --repository acme/project --runner release-01
   intent team ci provision --project-id project --repository acme/project --runner release-01 --confirm <preview_digest>
   ```

   The first command exits 4 with a network/keyring-free scope preview. Exact
   confirmation creates or reuses an independent X25519 key and prints only its
   public JSON descriptor. Save the second command's stdout as `ci-recipient.json`.
   Repeating the confirmed command reuses the same key; there is no private export.

2. Transfer that **public** descriptor to the developer through a reviewed channel.
   Check project, repository, runner ID, key ID and public key with the operator.

   ```sh
   intent team enable github --repository acme/project --ci-recipient ci-recipient.json --format json
   intent team enable github --repository acme/project --ci-recipient ci-recipient.json --confirm-preview <preview_digest>
   ```

   Setup binds the complete CI descriptor into its exact preview and subsequent
   WebAuthn-authorized publication recipient set. The machine has no fabricated
   GitHub account or WebAuthn credential and cannot authorize human decisions.
   Both the developer and CI receive separate wrapped content keys in the same
   encrypted release. Changing the descriptor invalidates prior confirmation.

   The first WebAuthn setup decision only stages exact CODEOWNERS/workflow
   suggestions locally. It does not create `intent-state`, configure its protection,
   or create signing keys. Commit and merge those files through default-branch review.
   Configure the required workflow rule described below, then return to “Preview
   branch protection changes.” A fresh, distinct WebAuthn decision is required after
   setup verifies the exact remote tooling, default protection, runner group and rule.

3. Review and merge all three staged files—CODEOWNERS, the state workflow, and the
   code-check workflow—on the protected default branch before
   configuring state-branch protection or opening a state publication PR. Configure
   an active required-workflow ruleset that targets **`refs/heads/intent-state`**,
   with no bypass actors, and requires exactly this repository's
   `.github/workflows/intent-state.yml` from **`refs/heads/main`** (or the actual default
   branch). The optional pinned source SHA must match that reviewed default-branch
   commit. Set `do_not_enforce_on_create: true`: this narrowly permits creation of
   the WebAuthn-authorized empty orphan anchor; every later state update remains
   governed. A rule that instead targets only the default branch does not protect
   state publications. Setup checks the effective rule for `intent-state` and its
   active source ruleset; a status name or Actions app ID alone is insufficient.

   Configure a separate active, no-bypass required-workflow ruleset for the protected
   default branch, sourcing exactly this repository's `.github/workflows/intent-check.yml`
   at the same default-branch ref, with `do_not_enforce_on_create: false`. Default
   branch protection must also require the strict `Intent Engineering / check` status
   bound to GitHub Actions app ID `15368`. Setup verifies both effective workflow-source
   rules; a matching status name alone does not prove which tooling ran.

   The generated job is exactly `Intent Engineering / state`, with:

   ```yaml
   runs-on:
     group: intent-state
     labels: [self-hosted, intent-state]
   ```

   It runs on
   `pull_request_target` targeting `intent-state`, and executes only tooling from
   `github.workflow_sha`. Its fetch phase reads head/base as inert Git objects;
   validation never checks out or runs state-candidate code. The state branch
   contains only the three signed/encrypted release artifacts, not workflow YAML.
   State-branch protection requires generic human PR approval, strict required
   checks, linear history, enforced admins, and disabled force pushes/deletions.
   Its required status is additionally bound to GitHub Actions app ID `15368`, but
   this is supplementary to the exact required-workflow rule and restricted runner
   group. Default-branch CODEOWNERS protects the tooling; it does not govern the
   artifact-only state branch, which intentionally has no CODEOWNERS file.

4. After protection succeeds and before opening the first publication PR, copy
   the publication preview's `ci_trust` object to a public JSON file. Independently
   review its signing public keys and machine descriptor with the setup approver.
   Install its canonical JSON under a protected runner-local path **outside every
   checkout**, owned by the runner service account. Its parent directory and file
   must not be writable by another user/group, and neither may be a symlink. A
   mode-700 directory and mode-600 file are recommended. Canonicalize this public
   document with the installed trusted package before the operator's atomic installation:

   ```sh
   python -c 'from pathlib import Path; from intent_engineering.team_state.ci import CiTrustConfig; import sys; c=CiTrustConfig.model_validate_json(Path(sys.argv[1]).read_bytes()); sys.stdout.buffer.write(c.canonical_bytes())' reviewed-ci-trust.json
   ```

   The output has no trailing newline. Set `INTENT_CI_TRUST_PATH` to the installed
   absolute public-config path in the runner service environment, then restart the
   service. Do not point it at a file supplied by a PR or checked out repository.
   This configuration contains only public keys; the validator verifies that its
   recipient matches the private key already present in the local OS keyring.

   The protected launcher executes the same validation as:

   ```sh
   intent team validate-state --project <trusted-checkout> --base <reviewed-base-sha> --head <candidate-head-sha>
   ```

   A missing key, wrong repository/key, malformed or writable public config,
   invalid signature, extra artifact, or non-linear candidate fails closed.
   `INTENT_CI_SHARED_STATE_TRUST` private-key JSON is explicitly rejected by this
   production validator and protected code-check launcher, even when a public config
   path is also supplied. Neither workflow uploads decrypted state, test results or
   logs as artifacts, uses an Actions cache, comments on PRs, or writes/merges branches.
   The code check retains only its strict canonical result at runner-local
   `.intent-trusted/.intent-ci/test-results.json` for in-job audit. Its final
   `always()` step sweeps only bounded, exact Docker IDs carrying this repository,
   Actions run ID, and run attempt in the runtime's `intent.ephemeral-ci` label,
   removing containers before images. A separate nonce label scopes in-process
   cleanup; neither path prunes unrelated or concurrently owned runner resources.

## Limits and lifecycle

This is a trusted self-hosted-runner deployment, not GitHub-hosted key provisioning.
Protect runner group access, its service account, public config, dependencies, and
default-branch workflows together. A process allowed to run as that account can
access its keyring. Candidate builds must remain inside the existing disposable
immutable sandbox, never as host processes or other workflows on that account.

Setup records a bounded public bootstrap receipt before attempting state-ref
creation. If the response is lost, restart and preview protection again; recovery
accepts only that exact receipt-bound orphan and empty tree. It never adopts an
arbitrary incompatible branch. The receipt clears after verified protection.

Before any external publication attempt, Cancel may remove the exact local request
and encrypted draft. After an ambiguous publication write, a recorded PR, or a
bootstrap receipt, cancellation cannot discard recovery authority. Reconcile the
provider state using the displayed retry/refresh action. A pending PR shows its URL;
merge it in GitHub, then refresh. Local trust is installed only after verifying the
merged PR, sole reviewed parent, and exact artifact bytes, including when GitHub
rewrites the commit SHA during a linear-history merge.

For rotation, provision a new runner ID/key and review a recipient-set publication
using the normal WebAuthn-authorized publication path. Never silently overwrite an
existing key or edit a reviewed descriptor to stand in for rotation. Bootstrap setup
only creates the first release; the normal publication lifecycle handles later
releases. Backups and loss recovery remain the operator's secure keyring policy.

GitHub-hosted runners require an independently operated OIDC/external key broker
that releases or unwraps only for an authorized repository/workflow/revision. That
extension is not implemented here. The legacy environment-trust adapter remains
for explicit compatibility/test callers, not as hosted production guidance.
