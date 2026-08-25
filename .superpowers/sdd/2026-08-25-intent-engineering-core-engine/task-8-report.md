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
  `EvidenceRecord` objects. ACL filtering denies access when a known record has an ACL and no
  matching actor is supplied; requested scope similarly denies a known conflicting scope.
- Categories use the existing graph node vocabulary: intent, requirements/capabilities, decisions,
  constraints/policy, acceptance criteria, code references, and tests. Context packs expose the
  new models through `intent_engineering.core.models`.
- A `context_limits` key only caps the matching category. The existing configuration has no
  `evidence_refs` default, so evidence is unlimited unless that existing mapping supplies an
  `evidence_refs` key.
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
- Scope and ACL filtering can only enforce metadata supplied in `EvidenceRecord`; unknown evidence
  references carry no ACL/scope data and are therefore not rejected on that absent metadata alone.
- Output filenames (`graph.md`, `graph.mmd`) are the generated-view convention introduced by
  `GraphRenderer`; direct callers can also use the pure renderer functions.
