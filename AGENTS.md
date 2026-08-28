# Agent instructions

Before modifying this repository, read these files in order:

1. [INTENT_ENGINEERING.md](INTENT_ENGINEERING.md) — foundational specification.
2. [schemas/intent-meta-model.yaml](schemas/intent-meta-model.yaml) — semantic contract.
3. [graph/framework-intent-graph.yaml](graph/framework-intent-graph.yaml) — dogfood graph.
4. [ROADMAP.md](ROADMAP.md) — scope and sequencing.
5. [CODEX_IMPLEMENTATION.md](CODEX_IMPLEMENTATION.md) — implementation instructions.

Preserve local-first operation, versioned evidence, source provenance, stable IDs,
semantic ChangeSet history, explicit reconciliation, and the distinction between
intent, requirements, implementation, and tests. A mismatch is evidence for
review, never an automatic statement that one source is true.

Use tests first for production behavior. Keep deterministic fixtures fixed in time
and offline; do not track nested fixture Git metadata. Validate graph mutations
through the public models and stores, and keep generated renders non-canonical.
