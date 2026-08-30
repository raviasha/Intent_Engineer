# SDD ledger — plan: docs/superpowers/plans/2026-08-30-intent-dev-control-plane.md

## Preflight scan

| Scope | Producer / consumer relationship | Finding |
|---|---|---|
| Task 1 | Defines canonical decision, credential, challenge, status, and attention models consumed by Tasks 2–7. | Internally consistent: tests cover the canonical representation later services sign and verify. |
| Task 2 | Persists Task 1 credential/challenge models and extends Runtime targets consumed by Tasks 3, 4, and 7. | Internally consistent: append-only transition semantics and legacy recovery compatibility agree. |
| Task 3 | Consumes Tasks 1–2 and produces verified human decisions consumed by Task 4. | Internally consistent: deterministic verifier port preserves offline tests while production requires user verification. |
| Task 4 | Consumes Task 3 verification and existing workflow services; produces previews and authenticated actions for Task 5. | Internally consistent: handlers remain outside semantic mutation and transaction-bound drift checks are explicit. |
| Task 5 | Consumes Task 4 and exposes the only browser API consumed by Task 6 and hosted by Task 7. | Internally consistent: strict raw boundary precedes handler behavior and responses are detached. |
| Task 6 | Consumes Task 5 routes and Task 1/3 WebAuthn representations; adds packaged static assets. | Internally consistent: no Node toolchain and wheel packaging changes match the files specified. |
| Task 7 | Composes Tasks 1–6 into one Runtime/process lifecycle and documents the Milestone 1 journey. | Internally consistent: lifecycle tests, CLI surface, and release proof cover the stated implementation. |
| Tasks 1 ↔ 3 | Task 1 canonical payload is the sole signed representation; Task 3 verifies it. | Clean; Task 3 must not introduce another signed payload encoding. |
| Tasks 1 ↔ 6 | Both modify `pyproject.toml`: dependencies first, packaged static assets later. | Clean and ordered; Task 6 must preserve Task 1 direct dependency bounds. |
| Tasks 2 ↔ 3 | Task 2 store issue/consume and credential counters back Task 3 ceremonies. | Clean; atomic counter/challenge mutation remains one transaction concern. |
| Tasks 2 ↔ 4 | Runtime targets and exact authority preimages feed Task 4 authenticated operations. | Clean; Task 4 must include credential/challenge state in drift protection. |
| Tasks 2 ↔ 7 | Runtime initialization and recovery targets are consumed by the `intent dev` lifecycle. | Clean; Task 7 must reuse, not recreate, store setup. |
| Tasks 3 ↔ 4 | Verified decision output authorizes Task 4 service operations. | Clean; verification is necessary but Task 4 must still reconstruct and compare live previews. |
| Tasks 4 ↔ 5 | Control-plane service is the sole semantic port behind HTTP. | Clean; handlers must not bypass it. |
| Tasks 5 ↔ 6 | Browser assets consume only the strict `/api/v1` routes. | Clean; UI tests must not use private service calls. |
| Tasks 5 ↔ 7 | Task 7 hosts the Task 5 app at an exact loopback origin and publishes lifecycle metadata. | Clean; origin selection must be established before WebAuthn/HTTP construction. |
| Tasks 6 ↔ 7 | Task 7 opens and documents the packaged Task 6 UI. | Clean; unguessable fragment bootstrap data must not become authority. |

Preflight result: no task contradiction or spec conflict found. The spec remains binding if an implementation detail later conflicts with plan prose.

Task 1: fix round 1/5 (1 addressed, 0 open — exact integer schema versions reject booleans; commits 0d7ef56..d2ea988)
Task 1: complete (commits 60609ea..d2ea988, review clean)
Task 2: fix round 1/5 (2 addressed, 0 open — open challenge lifetime and fixed hostile-lock failures; commits 5293c3a..d726fd2)
Task 2: complete (commits d2ea988..d726fd2, review clean)
Task 3: minor (deferred): production verifier bridge coverage is narrower than the fake-verifier security matrix; final review should triage direct forwarding and substitution/replay cases.
Task 3: fix round 1/5 (1 addressed, 0 open — production WebAuthn response traceback scrubbing; commits e498e71..9c3a992)
Task 3: complete (commits d726fd2..9c3a992, review clean; 1 deferred minor)
Task 4: Ruling: `APPROVE_EXTERNAL_WRITE` persists an authenticated ApprovalRecord but does not execute the provider mutation or append a receipt — the approved design requires WebAuthn for approval, while existing execution remains a separate semantic operation and Task 4/5 expose no execute route — if wrong, a later UI/API task will need an explicit authenticated execute route and receipt-stage rollback coverage.
Task 4: minor (deferred): abandoned private answer previews remain in memory until successful apply or service shutdown; final review should triage bounded expiry cleanup.
Task 4: fix round 1/5 (4 addressed, 0 open — approval atomicity, connector membership drift, production confirmation preview authority, case alias parity; commits a73f3c2..4854f1d)
Task 4: complete (commits 9c3a992..4854f1d, review clean; 1 deferred minor)
Task 5: Ruling: add narrow public `ControlPlaneService` registration option/verification wrappers using the service-owned actor, origin, clock, and WebAuthn service — Task 5 requires handlers to call public service methods only, so private-field access would violate layering — if wrong, the wrappers can later move to a dedicated enrollment application port without changing the HTTP contract.
Task 5: fix round 1/5 (2 addressed, 0 open — fixed-error cancellation secrecy and exact configured origin; commits 2d0e831..417d2f9)
Task 5: complete (commits 4854f1d..417d2f9, review clean)
Task 6: fix round 1/5 (5 addressed, 0 open — stale preview binding, immediate cleanup, destructive labels, executable manual probe, actual shipped-JS tests; commits a68c488..9ee01a7)
Task 6: fix round 2/5 (1 addressed, 0 open — authoritative success reporting during navigation race; commits 9ee01a7..711154b)
Task 6: complete (commits 417d2f9..711154b, review clean)
Task 7: minor (deferred): the complete semantic journey drives the captured service directly rather than traversing the freshly launched HTTP/UI surface; final review should triage a full launched-browser journey.
Task 7: fix round 1/5 (3 addressed, 1 open — process/listener/instance attestation, post-publish cancellation cleanup, Runtime closure addressed; shutdown overlap remained; commits de7bda6..57e1768)
Task 7: fix round 2/5 (1 addressed, 0 open — retained lifecycle lease removes pre-cleanup Runtime overlap; commits 57e1768..09c3cd5)
Task 7: complete (commits 711154b..09c3cd5, review clean; 1 deferred minor)
Final review: Ruling: add a strict clarification-answer preview route even though the Task 5 route list named exactly seven routes — the approved spec and Milestone 1 acceptance require browser-based authoritative answers, so the route list was incomplete — if wrong, the public API gains one additive endpoint that can be deprecated without changing stored state.
Final review: Ruling: permit an absent `Origin` only on exact loopback same-origin read-only GET routes while requiring any present Origin to match and retaining exact Origin+CSRF on all mutations — browsers commonly omit Origin on same-origin GET, and otherwise the shipped UI cannot function — if wrong, read endpoints rely on exact Host plus loopback binding rather than a redundant Origin signal.
Final review: exception approval: after final re-review, the user explicitly authorized one narrow corrective pass for `ControlPlaneService.answer_preview()` cap-failure traceback retention, despite the prior single-fix-wave/single-commit boundary. No other finding or scope was reopened.
Final review: exceptional corrective pass RED: the real fixed-time 65th-answer cap regression retained `PRIVATE-CAP-ANSWER-MARKER-43127` through the local `EvidenceRecord` (`1 failed in 1.75s`) while the fixed `ControlPlaneError` type/cause/context and 64-item table semantics were correct.
Final review: exceptional corrective pass GREEN: initializing and clearing only the answer-preview coordinator/evidence/pending/payload/material intermediates removed the marker from every repository traceback-frame local (`1 passed in 2.15s`); focused service/browser coverage was `113 passed, 1 skipped in 2.76s`; Ruff/format/mypy/diff passed; the final exact offline suite was `1923 passed, 1 skipped in 108.24s`.
