# Task 8 report — concise context packs and graph views

## Summary

Implemented immutable public `ContextItem` and `ContextPack` models, plus a deterministic
`ContextProvider` for task and symbol queries. The provider tokenizes lowercase Unicode words,
scores label overlap, traverses active edges in either direction for at most two hops, applies
configured limits, includes linked open cases and evidence, and emits deterministic review
warnings. It filters known evidence records fail-closed when an ACL or requested repository scope
does not allow access.

Implemented pure Markdown and Mermaid render functions and `GraphRenderer`. Generated Markdown
groups semantic layers, evidence, and open cases. Mermaid safely escapes labels, uses `flowchart
LR`, and sorts nodes and edges by stable ID. `GraphRenderer` only writes `graph.md` and
`graph.mmd` below its explicitly supplied output directory; it reads canonical graph data through
the production YAML store.

## Files

- `src/intent_engineering/core/models/context.py`
- `src/intent_engineering/core/models/__init__.py`
- `src/intent_engineering/context/__init__.py`
- `src/intent_engineering/context/provider.py`
- `src/intent_engineering/render/__init__.py`
- `src/intent_engineering/render/markdown.py`
- `src/intent_engineering/render/mermaid.py`
- `src/intent_engineering/render/renderer.py`
- `tests/unit/context/__init__.py`
- `tests/unit/context/conftest.py`
- `tests/unit/context/test_provider.py`
- `tests/unit/render/__init__.py`
- `tests/unit/render/conftest.py`
- `tests/unit/render/test_renderers.py`

## Decisions

- `ContextProvider` receives immutable graph/case/config inputs and an optional sequence of
  `EvidenceRecord` objects. Referenced evidence must resolve; ACL filtering requires an explicit
  matching actor, and a requested scope requires an exact matching evidence scope.
- Categories use the existing graph node vocabulary: intent, requirements/capabilities, decisions,
  constraints/policy, acceptance criteria, code references, and tests. Context packs expose the
  new models through `intent_engineering.core.models`.
- A `context_limits` key caps its matching category. The default configuration caps
  `evidence_refs` at 20.
- Markdown and Mermaid functions are pure. Stable Mermaid identifiers are SHA-256-derived from
  stable node IDs so arbitrary IDs cannot alter Mermaid syntax.

## TDD and verification evidence

RED (before production modules existed):

```text
.venv/bin/pytest tests/unit/context tests/unit/render -v
exit 1
ModuleNotFoundError: No module named 'intent_engineering.context'
```

GREEN after minimal implementation:

```text
.venv/bin/pytest tests/unit/context tests/unit/render -v
7 passed in 0.03s
```

Final focused verification:

```text
.venv/bin/pytest tests/unit/context tests/unit/render -v
7 passed in 0.02s
```

Final static verification:

```text
.venv/bin/ruff format src/intent_engineering/core/models/context.py src/intent_engineering/core/models/__init__.py src/intent_engineering/context src/intent_engineering/render tests/unit/context tests/unit/render
14 files left unchanged

.venv/bin/ruff check src/intent_engineering/core/models/context.py src/intent_engineering/core/models/__init__.py src/intent_engineering/context src/intent_engineering/render tests/unit/context tests/unit/render
All checks passed!

.venv/bin/mypy --strict src/intent_engineering/core/models/context.py src/intent_engineering/context/provider.py src/intent_engineering/render/markdown.py src/intent_engineering/render/mermaid.py src/intent_engineering/render/renderer.py
Success: no issues found in 5 source files
```

Full verification:

```text
.venv/bin/pytest -v
158 passed in 1.25s
```

## Commits

- Product and tests: `a2f3ba0427cb56807ea76d35a6dc96965efed6d3` (`feat: generate task context and graph views`)
- Report: recorded in the follow-up documentation commit.

## Risks and deviations

- No deviations from the task requirements.
- Scope and ACL filtering depend on supplied `EvidenceRecord` metadata; an unresolved referenced
  evidence ID is conservatively excluded from context.
- Output filenames (`graph.md`, `graph.mmd`) are the generated-view convention introduced by
  `GraphRenderer`; direct callers can also use the pure renderer functions.

## Fix round 1 — review hardening

Addressed all six independent-review findings in product commit
`cf7df5c86000cdeb5cba5c4496b2fadd239ea3fe` (`fix: harden task context and graph views`).

- Evidence access is now conservative: non-empty referenced sets resolve every ID, ACL-protected
  records require an explicit matching actor, and a requested repository scope requires an exact
  scope on every resolved record. Evidence-less nodes remain visible with the required warning.
- Symbol context seeds only an exact active stable node ID; empty and unknown references produce
  deterministic empty packs.
- `is_nonterminal_case_status()` centralizes visibility of `OPEN`, `PROPOSED`, and `NEEDS_HUMAN`
  cases for both context and Markdown.
- Markdown replaces all CR/LF, Unicode line separators, and control characters before escaping.
- Generated views reject symlinked output directories and targets, then use no-follow file opens.
- The default `ProjectConfig.context_limits` now caps `evidence_refs` at 20.

### RED

```text
.venv/bin/pytest tests/unit/context tests/unit/render tests/unit/core/models/test_evidence.py -v
13 failed, 23 passed in 0.25s
```

The failures reproduced exact-ID symbol selection, unresolved/unscoped evidence leakage,
`PROPOSED`/`NEEDS_HUMAN` omission, Markdown line injection, renderer symlink following, and the
unbounded default evidence collection.

### GREEN

```text
.venv/bin/pytest tests/unit/context tests/unit/render tests/unit/core/models/test_evidence.py -v
39 passed in 0.11s

.venv/bin/ruff format src/intent_engineering/core/models/project.py src/intent_engineering/core/models/reconciliation.py src/intent_engineering/core/models/__init__.py src/intent_engineering/context/provider.py src/intent_engineering/render/markdown.py src/intent_engineering/render/renderer.py tests/unit/core/models/test_evidence.py tests/unit/context tests/unit/render
13 files left unchanged

.venv/bin/ruff check src/intent_engineering/core/models/project.py src/intent_engineering/core/models/reconciliation.py src/intent_engineering/core/models/__init__.py src/intent_engineering/context/provider.py src/intent_engineering/render/markdown.py src/intent_engineering/render/renderer.py tests/unit/core/models/test_evidence.py tests/unit/context tests/unit/render
All checks passed!

.venv/bin/mypy --strict src/intent_engineering/core/models/project.py src/intent_engineering/core/models/reconciliation.py src/intent_engineering/context/provider.py src/intent_engineering/render/markdown.py src/intent_engineering/render/renderer.py
Success: no issues found in 5 source files
```

### Full verification

```text
.venv/bin/pytest -v
183 passed in 2.37s
```
