# Dedicated CI recipient provisioning

The production setup must encrypt each initial state release for both its enrolled human recipient and an explicitly reviewed CI recipient. Developer private keys are never exported. No recipient private key is stored in GitHub, `.intent`, command arguments, logs, or workflow secrets.

## Deployment and authority

A dedicated self-hosted runner service account provisions one X25519 key in its OS keyring. A preview-first `intent team ci provision` command binds project ID, canonical repository and runner ID; exact confirmation creates/reuses the key and emits only a public machine descriptor. The machine recipient has its own strict model and never claims GitHub identity or WebAuthn authority.

The developer imports that public descriptor with `intent team enable github --ci-recipient <path>`. Import is read-only and bounded; the complete descriptor enters the CLI preview and persisted non-secret setup request. Missing CI configuration is an actionable setup error before provider side effects. Existing publication WebAuthn authorization binds the complete sorted human+machine recipient set; stale/substituted descriptors cannot preserve authority. Only human recipients may satisfy a publication decision credential.

Publication preview emits a public CI trust document containing the exact machine descriptor and reviewed signing public keys. An operator installs this public document in a protected runner-local path outside the checkout. The validator loads that document and reconstructs trust from the dedicated CI keyring entry, checks exact public/private binding, and requires repository/project/signer/candidate lineage matches. Missing CI key or mismatched public trust fails closed. No private key export command exists.

## Workflow

Generated production workflow targets dedicated `[self-hosted, intent-state]` runners. Only protected default-branch tooling executes; candidates remain inert Git objects. A runner environment variable names the protected local public trust document. Production guidance does not use `INTENT_CI_SHARED_STATE_TRUST` or GitHub-hosted private-key JSON secrets. Existing explicit environment trust remains a compatibility/test API, not generated production guidance.

The runner keyring and public trust file are provisioned by a trusted operator outside workflows, before the first required check. This is intentionally a deployment prerequisite, not an automatic GitHub secret upload. The runner must be isolated from arbitrary candidate code. An external secret manager/OIDC broker is a future extension for hosted runners; it is not implemented or implied here.

## Verification and lifecycle

Tests prove independent CI/developer decryption of one real production setup bundle, validator success with only CI keyring material, no secret in setup/descriptor/workflow output, strict wrong-project/repository/recipient/signer rejection, missing-key failure, and publication authority rejection after recipient substitution. Key generation is exact-confirmation-only and idempotent; public descriptor import does not generate keys. Existing durable draft/replay/cancellation protections continue to apply.

Key rotation requires provisioning a distinct runner ID/key and a new reviewed recipient-set publication. Provisioning never replaces an existing key silently. Private-key deletion/rotation and arbitrary multi-machine membership management are not added by this slice.
