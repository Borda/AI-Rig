# Changelog

## 0.5.0

- Add capability-aware native Codex questions across every Codex skill: prefer permitted synchronous controls for required decisions, asynchronous controls for independent follow-ups or lossless fallback, and plain chat when unsupported.
- Use Approve/Deny for conversational authorization while preserving exact-digest and runtime permission boundaries. Bind delayed answers to immutable scope, reject superseded/duplicate replies, retain all feasible choices, and avoid duplicate live prompts.
- Ship plugin-local question guidance and regression coverage; preserve existing authorization and host-specific behavior without changing installed settings or requiring a Codex upgrade.
- Label one evidence-backed first choice `(Recommended)` in option-based questions; preserve canonical answers, exact confirmation syntax, and explicit consent.

All notable changes to `bridge_CC-Codex` are documented here.

## 0.4.1

- Compact complete Codex command-execution output in bounded transcripts with UTF-8 byte counts and SHA-256 digests, reserving transcript-wrapper space and keeping oversized records or retained-output overflow terminal.
- Guide both host surfaces through output-limit recovery: dereference the incident JSON, split read-only work into explicit bounded packages, reconcile complete coverage, and inspect partial edits before scoping verified remaining write-capable work.
- Clarify that capture-byte savings do not establish provider token savings and that a reported implementation workspace delta is advisory evidence, not proof that no edit occurred.

## 0.4.0

- Reap the child leader after Windows tree termination and POSIX kill fallbacks so timeout, cancellation, and output-limit results retain the actual exit code. Bound the wait and diagnose unavailable status without fabricating success.

- Build the peer child's environment from an allowlist instead of inheriting the caller's: base process keys, provider authentication keys, and on Windows the interpreter start-up keys survive; unrelated API keys, cloud credentials, and the session's inter-agent messaging variables no longer reach a peer process.

- Add `BRIDGE_CHILD_ENV_EXTRA`, a comma-separated list of additional variable names to allow through, for hosts whose proxy or enterprise provider path needs variables the fixed sets cannot anticipate.

- ! BREAKING — pin `sandbox_workspace_write.network_access=false` on every Codex invocation, so the bridge's egress posture no longer depends on the installed Codex version's default. A Codex `implement` that installed a dependency, refreshed a lockfile, or ran a network-touching hook now fails inside the sandbox.

- Document the sandbox mode, egress, environment, and quota posture in `docs/security.md`, including that the egress control is Codex-side only and has no Claude-child equivalent.

## 0.3.2

- Terminate surviving POSIX peer-group descendants after timeout or cancellation even when the leader has already exited.
- Reject symlinked, reparse-point, and pre-existing hard-linked health artifacts; use no-follow member opens plus opened-descriptor validation to contain predictable telemetry writes.
- Bound encoded task input before dispatch and combined peer output during capture; reject oversized requests without launching a peer and classify output-limit termination while preserving capped diagnostics.

## 0.3.1

- Restructure the README's setup, request, artifact, MCP, and safety guidance into concise lists, tables, and blockquotes while preserving every command, parameter, transport boundary, and evidentiary limit.
- Align the Claude and Codex setup skills around explicit lifecycle phases and atomic approval steps without changing host-specific workspace, authentication, fresh-session, paid-live-verification, or readiness semantics.

## 0.3.0

- Make `/bridge:setup` and `$bridge:setup` truthful approval-bound lifecycle entrypoints with default `action=all target=peer scope=auto live=prompt` behavior.
- Add credential-free setup planning, HMAC-authenticated expiring one-use action approvals, separate no-capture provider-owned authentication, approval-bound live verification, post-configuration reinspection, user-scoped mutation locks and sanitized records, bounded rollback metadata, and explicit bootstrap/fresh-session boundaries.
- Add the zero-provider read-only `bridge_status` MCP tool for sanitized session/workspace evidence and keep setup results in a dedicated schema separate from model envelopes.
- Keep ordinary synchronization static-only: no setup skill, model call, approval token, authentication, repair, restart, or provider call.
- Gate host CLIs on a minimum supported version instead of an exact pin, so routine CLI self-updates no longer fail every setup action as `unsupported-version`.
- Report the denial for an empty trailing `--approve` from the actually parsed arguments, keep `verify-live` plans empty under `live=skip`, and reject a zero-exit `Not logged in` Codex status as unauthenticated.
- Give the approved marketplace install a network-sized timeout, expire recorded operation failures after a bounded retry window, and extend the setup-result schema to cover the sensitive-input rejection placeholders.
- Fingerprint the complete installed payload including `.mcp.json` and the CLI baseline, report missing payload members before the baseline loads, derive the MCP-reported version from the plugin manifest, and report the status workspace in canonical POSIX form.
- Update both host manifests and maintainer documentation for the setup contract.

## 0.2.1

- Compress Claude- and Codex-side skill instructions while preserving explicit effort selection, caller-selected arguments, host-bound workspace/session authority, recursion refusal, detached-job lifecycle, compact-envelope/transcript boundaries, and paid live-probe consent.
- Retain Codex-side characterization coverage and validate Claude-side compression through exact executable-literal preservation plus full plugin and packaged-shape gates.

## 0.2.0

- Set the user-facing brand to `bridge_CC-Codex` across both host manifest descriptions and the Codex display metadata, while the installed plugin name, skill namespaces, and marketplace registrations stay `bridge` for consistency with the other plugins.
- Advertise the MCP server under the same `bridge` name the plugin already installs as, so the transport identifier no longer differs from the plugin identifier.
- Accept the task from a file via `--task-file`, mutually exclusive with `--task`, so a caller forwarding text it did not author never has to embed that text in a command line; both a missing and an ambiguous task source fail as the same parseable JSON error envelope every other failure uses.

## 0.1.0

- Add independently installable Claude Code and Codex halves for bounded `implement`, read-only `advise`, and adversarial `review` calls.
- Add caller-selected model and effort, task-tier normalization, per-verb soft budgets, hard cutoffs, bounded read-only retry, and recursion-depth protection.
- Return compact validated envelopes instead of forwarding raw host transcripts into the caller's context.
- Keep decision-critical fields and observed metadata in the public envelope while retaining bounded peer `details` in workspace-relative transcript artifacts and compact incident records.
- Route read-only reviews through a general Codex execution with an explicit adversarial-review prompt rather than relying on the native review subcommand.
- Add detached-job status, result, and cancellation controls, project-local transcript/job artifacts, sanitized incidents, and rolling health/cost records.
- Add static CLI drift diagnostics and an opt-in live setup probe for host availability, authentication, structured-output compatibility, and envelope behavior.
- Add repository marketplace registrations for Claude Code and Codex, disjoint host skill surfaces, and complete architecture, security, operations, and development documentation.
