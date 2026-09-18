# Neutral task prompting

> Every dispatched task starts with its soft budget, current depth, and run identifier. It asks the callee to return a compact structured result before time expires, to report partial work with `remaining`, and to report inaccessible resources or approvals in `blockers` instead of waiting.

> The prompt is peer-neutral: it names no host as primary or fallback authority. It never asks for model, effort, cost, duration, session, depth, or run metadata because harness owns those facts.

> A partial result is useful. Caller may issue a fresh bounded request containing prior remaining work; read-only calls do not resume a prior session.
