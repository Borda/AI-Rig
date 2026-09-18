# Bounded self-healing

> Write-capable `implement` calls are reported with their partial transcript and workspace delta after a timeout; they are never automatically retried.

> A bridge call performs at most one remedy. It writes one incident record for every fault and appends one health.jsonl line for every completed call, including unsuccessful calls.

> `turn.failed` with `reasoning.effort` or an equivalent structured Claude error may trigger one supported-effort substitution. A missing or unauthenticated CLI, a permission refusal, an unknown fault, or a repeated substitution is reported without retry. Timeout retry is limited to read-only verbs; implement is reported with its partial transcript and workspace delta. An unavailable model and a stale resume session are also reported without remedy: bridge does not maintain a model fallback ladder, and no verified structured signal distinguishes a stale session from other resume faults.

> An `output-limit` incident is terminal and never automatically retried. When the envelope's `incident` path is present, open its JSON file and inspect the `fault`. For read-only work, inspect bounded transcript, make fresh requests split by explicit files, modules, symbols, or independent questions with bounded tool output, and reconcile all scopes before accepting a result. For `implement`, inspect worktree delta and changed files first, then request only verified remaining work in a separate bounded task. Never treat a truncated transcript as a completed result.

> Incident records contain classified fault and reason, model, effort, verb, duration budget, transcript path, and any killed-implementation workspace delta. They deliberately exclude child arguments and environment data because task text and credentials may be present there. Bounded transcript omits completed Codex command-output payloads but records their UTF-8 byte counts and SHA-256 digest; digest does not reconstruct discarded output, and capture savings do not prove provider token savings. Stderr is never the primary fault classifier.
