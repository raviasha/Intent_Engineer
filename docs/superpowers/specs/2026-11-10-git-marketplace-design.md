# Git marketplace distribution

Status: proposed design; distribution selected by user, runtime packaging proposed by agent.

## Intent

Users install Intent Advisor and trust its hooks without manually cloning the source
repository or installing Python. Keep the local-first graph and existing read-only,
background reconciliation behavior. Git marketplace distribution was explicitly selected
in the November 10 conversation. Bundling the runtime is the recommended implementation,
not a separately recorded user decision.

## Approaches

Use a Git-backed marketplace with a self-contained runtime (recommended): larger download,
but no dependency installation during hook execution. An alternative downloads a pinned
runtime on first use: smaller package, but adds network failures and setup latency. A hosted
MCP service alone cannot replace local repository access and trusted code-change hooks.

## User journey

1. Add the project's Git marketplace once using Codex's supported marketplace command.
2. Install Intent Advisor from that marketplace and start a new session if required.
3. Review and trust the bundled hooks.
4. Open the existing codebase, select intent sources, and continue normal onboarding.

The marketplace registration is an explicit first-use step; this is not a public-directory
listing. Installation and hook trust remain per computer. Updates can require renewed hook
review. Connected intent sources retain their own authentication flow.

## Package and release

Keep source development on main and publish a generated distribution branch in the same
GitHub repository. Its marketplace retains the current marketplace/plugin identity to avoid
duplicate installations, and points at a complete installed plugin folder. Users register
`raviasha/Intent_Engineer --ref marketplace` rather than checking out the source themselves.

The release package includes platform-specific, relocatable CPython runtimes, pinned
dependencies, application code, existing skills, hooks, and MCP configuration. Build each
runtime on its target platform; record source commit, Python version, dependency versions,
licenses and file hashes. Never ship credentials, local graphs, or developer environment
paths. No Git LFS or user-side package manager is required. Reject a release if repository
file-size limits or native-library relocation checks fail.

A plugin-relative launcher selects the supported runtime, preserves the project working
directory and arguments, and replaces its process so cancellation reaches the worker.
MCP, hooks, subprocesses and documented agent CLI fallbacks must all resolve the packaged
runtime, without depending on a global `intent`, Python, or a developer virtual environment.
Keep an explicitly documented source-development path for contributors.

Initial target proposal: macOS arm64/x86_64 and Linux x86_64, matching existing POSIX process
and locking assumptions. Windows and Linux arm64 require separate portability validation
and must not be advertised as supported by this release. Unsupported platforms receive a
clear setup diagnostic, never a silent claim of readiness.

The existing semantic reviewer also invokes Codex CLI. Validate its availability in desktop
installations and use a supported host-provided executable if available. If it cannot be
resolved without a separate CLI install, record this as a release blocker for the promised
installation experience; do not quietly retain an undocumented prerequisite.

## Publication and updates

Build and test all advertised targets before advancing the distribution branch. Publish
only complete packages using normal commits with concurrency protection; never overwrite
main with generated binaries. Version plugin and runtime together. Preserve the previous
release for rollback. A marketplace refresh must install a coherent version without
changing repository graph state or granting hook trust.

## Validation

Test installation from the Git marketplace in an isolated Codex profile, leaving the user's
installed plugin and trust settings unchanged. Run relocated packages from paths containing
spaces with system Python and global intent absent. Verify CLI startup, MCP handshake,
hook input/output, cancellation and a synthetic reconciliation. Run from an unrelated
working directory to prove project resolution. Test missing/unsupported runtime diagnostics,
upgrade behavior and package integrity. Exercise every advertised architecture in CI.

Success requires a published, tested marketplace package and short user instructions;
changing marketplace metadata alone does not satisfy removal of the Python prerequisite.
No assessed project code, source documents, graph semantics or trust policy changes belong
in this packaging work.
