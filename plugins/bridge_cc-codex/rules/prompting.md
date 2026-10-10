# Neutral task prompting

> Every dispatched task starts with its soft budget, current depth, and run identifier. It asks the callee to return a compact structured result before time expires, to report partial work with `remaining`, and to report inaccessible resources or approvals in `blockers` instead of waiting.

> An `implement` task also tells the callee to leave every change unstaged and uncommitted and to run no Git write, push included. The peer still loads its own project instructions, where a local Git approval grant in the checkout would make a completion commit its default. A bridge child gains nothing from any grant or push token: they are the authority records of the calling session, which reviews the diff and owns every commit and push. No bridge step assigns a commit to the child.

> The prompt is peer-neutral: it names no host as primary or fallback authority. It never asks for model, effort, cost, duration, session, depth, or run metadata because harness owns those facts.

> A partial result is useful. Caller may issue a fresh bounded request containing prior remaining work; read-only calls do not resume a prior session.
