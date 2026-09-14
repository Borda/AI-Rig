# Operations and troubleshooting

This document covers local setup, routine invocation, detached-job lifecycle, and the failure signals emitted by `bridge_CC-Codex`. The normalized plugin identifier is `bridge`. The default setup path performs safe inspection, configuration, and one closed repair when the operator approves the exact plan; provider-owned authentication, fresh-session activation, and live inference remain separate human-approved gates.

## Prerequisites

For Claude Code to Codex calls, make `codex` available on `PATH` and authenticate it for the requested model. For Codex to Claude Code calls, make `claude` available on `PATH` and authenticate it for the requested model. Both directions require a `python` executable on `PATH` that reports Python 3.10 or newer. The selected workspace must permit creation of `.temp/bridge/`.

The selected workspace does not need to be a Git repository. The bridge uses Codex's supported `--skip-git-repo-check` option when needed; that option only permits execution outside Git and never chooses or widens the workspace. The caller and host remain responsible for selecting and trusting the intended directory. Setup uses the host-selected launch workspace and rejects a model-controlled override.

The loaded current host must already have the Bridge plugin, trust, authentication, and fresh session needed to invoke setup. Current-host bootstrap is external and human-owned. From either host, invoke the canonical setup lifecycle:

```text
/bridge:setup action=all target=peer scope=auto live=prompt
$bridge:setup action=all target=peer scope=auto live=prompt
```

Plain setup uses `action=all target=peer scope=auto live=prompt`. It first runs credential-free inspection and displays every proposed operation, exact native argv, resolved target/scope, external capability, credential behavior, filesystem and host effects, rollback evidence, retry policy, and safe denial outcome. Safe configuration or repair runs only after one expiring, one-use approval bound to the action, workspace, plan, and observed state, then re-inspects host inventory before reporting whether the configuration is visible or a fresh session is required. Authentication and live verification are separately planned actions with distinct digests; the operator runs authentication in their own terminal outside model-captured tool streams, while live verification permits one paid provider call through the setup executor. A denial, changed or expired digest, replay, failed probe, or failed operation stops without a hidden retry.

```text
/bridge:setup action=check target=peer scope=auto live=skip
$bridge:setup action=check target=peer scope=auto live=skip
```

Use `action=check` for inspection only. The setup result is defined by `schemas/setup-result.schema.json` and reports the strongest evidence reached: `static`, `host-authenticated`, `session-ready`, `workspace-ready`, or `live-verified`. Static readiness, authentication, session/workspace binding, and inference are separate claims. `live=skip` remains `inference-unverified`; `live=required` is non-ready when approval or verification fails. A successful setup-CLI live probe is point-in-time evidence and remains partial until the host skill has applicable loaded-session/workspace evidence. The deterministic planner records only credential-free state and operation fingerprints; it never stores provider tokens, API keys, browser/device codes, account secrets, or raw login output. It does create one random per-user HMAC key solely to authenticate approval payloads.

Ordinary `make sync-*` does not invoke setup. It calls only the direct static doctor with explicit read-only semantics, bounded timeout, and structured output. Sync does not invoke a model, pass an approval token, authenticate, configure, repair, restart a host, claim an active session/workspace, or make a provider call. A direct doctor result proves only the named machine/static probes.

## Foreground calls

Claude Code skills use `/bridge:implement`, `/bridge:advise`, and `/bridge:review`. Codex skills use `$bridge:implement`, `$bridge:advise`, and `$bridge:review`. Each call requires a concrete task and supports explicit `--model`, `--effort`, and `--timeout-seconds` values. Omitted budgets default to 600 seconds for `implement`, 120 seconds for `advise`, and 300 seconds for `review`; the hard cutoff is 1.2 times the soft budget. Reverse implementations accept at most 700 seconds; reverse advice and review accept at most 350 seconds so their optional second attempt, per-attempt termination and drain overhead, and the response margin remain inside the 900-second host deadline.

For direct local diagnostics or automation, the Claude-facing supervisor accepts the following shape:

```bash
python "${CLAUDE_PLUGIN_ROOT}/bin/bridge_call.py" advise --task "Summarize the current diff without editing files."
python "${CLAUDE_PLUGIN_ROOT}/bin/bridge_call.py" review --task "Review the current diff for regressions and missing tests."
python "${CLAUDE_PLUGIN_ROOT}/bin/bridge_call.py" implement --task "Run the focused test and fix the smallest verified defect." --timeout-seconds 600
```

The Codex-facing MCP tools are the supported reverse entry point; do not invoke `claude --print` directly from a sandboxed Codex model turn. Implement is write-capable under the host's normal permission mode, advice is read-only, and review is routed through a read-only general Codex execution with an explicit adversarial-review prompt rather than Codex's native review subcommand.

On native Windows PowerShell, use the same `python` launcher after confirming it reports Python 3.10 or newer; use the installed plugin-root variable with backslash or slash separators.

## Detached jobs

Only Claude Code-to-Codex `implement` supports detached execution. Start it with `--background`; the command returns a job identifier. Inspect or stop that identifier with:

```bash
python "${CLAUDE_PLUGIN_ROOT}/bin/bridge_call.py" status --job-id JOB_ID
python "${CLAUDE_PLUGIN_ROOT}/bin/bridge_call.py" result --job-id JOB_ID
python "${CLAUDE_PLUGIN_ROOT}/bin/bridge_call.py" cancel --job-id JOB_ID
```

Use the same workspace for lifecycle commands. While a job is running, do not edit paths named by its task. `status` reports lifecycle metadata; `result` returns the completed envelope; `cancel` writes a durable, identity-checked cancellation marker for the live supervisor and returns `cancel_requested`. Poll `status` or `result` for the final `cancelled` state; the supervisor, never the lifecycle caller, terminates its verified child process tree and records the observed worktree delta when available.

A recorded `running` job whose supervisor process no longer exists — or a `queued` record older than two minutes that no supervisor ever claimed — is reported as `stalled` instead of `running` or `queued`, so a supervisor killed without writing its final record cannot keep a caller polling forever. A supervisor that fails with an internal error before producing an envelope records the terminal `failed` state with its error text. Treat `stalled` and `failed` as terminal: cancellation is refused for a stalled job because no supervisor remains to consume the marker, and you should inspect the job record, the bounded transcript, and the worktree before re-dispatching, because a killed write-capable child may have landed partial edits.

Finalization freezes the bounded capture under its lock; a failed drain returns a non-retrying blocked drain failure rather than timeout or cancellation unless output-limit was already observed, and late unseen bytes are not reclassified as output-limit.

## Common failures

| Signal | Meaning | Operator action |
| -- | -- | -- |
| `blocked` | The request was rejected or a required CLI, authentication, permission, model, or input contract was unavailable. | Read `blockers`, run static diagnosis, and resolve the host prerequisite or narrow the request. |
| `blocked` with `output-limit` incident | A pending stdout record or retained stdout/stderr exceeded its 256 KiB bound, including during final flush; this classification wins over timeout or cancellation. | Use the bounded-work steps below; no automatic retry occurs. |
| `timeout` | The hard cutoff elapsed before a valid result returned. | Read the transcript and workspace state; narrow the task or increase the explicit budget. Do not assume no edits occurred. |
| `refused` with `recursion-depth` | The trusted call chain already contains a peer dispatch. | Start a fresh outer call rather than attempting another cross-host hop. |
| `effort_substituted` | The requested effort was valid but unsupported by the target capability declaration. | Review the recorded requested and applied levels; repeat only with an intentional supported choice. |
| Missing command or changed flags | A host CLI is absent or no longer matches `rules/cli-baseline.json`. | Install or upgrade the host CLI, or use a bridge version that supports it; never edit the baseline at runtime. |
| `bootstrap-required` | The current host cannot prove the loaded Bridge skill, trust, authentication, or fresh session needed to invoke setup. | Complete the external host bootstrap and start a fresh session; setup cannot repair its own invocation surface. |
| `configuration-needed` or `repairable` | The peer host has a supported native operation that can be applied safely. | Review the exact plan and approve its digest; do not substitute a generic CLI command. |
| `authentication-needed` or `auth-flow-launched` | The peer provider login is separate from Bridge configuration and may require a browser or device flow. | Approve the no-capture provider flow; only a separate redacted status probe can establish `host-authenticated`. |
| `fresh-session-required` | A host restart or newly loaded MCP/plugin surface is needed. | Restart the relevant host manually and invoke setup again; setup never terminates or restarts a host. |
| `trust-required` | The host requires an operator trust decision before loading the plugin or MCP server. | Complete the host trust prompt outside setup, then use a fresh session. |
| `workspace mismatch` | The canonical workspace reported by the loaded session differs from the host-selected launch workspace. | Stop and reopen the host in the intended workspace; never accept a model-selected relocation. |

## Output-limit recovery

1. Read `blockers` and the bounded `transcript_path`. When `incident` is present, open the JSON file at that workspace-relative path and inspect its `fault` member. Treat the call as incomplete even if the transcript contains useful work. For `implement`, inspect `workspace_delta`, changed files, and relevant tests before another request; the delta is advisory and an empty or unavailable-Git delta does not prove that no edits landed.
2. For `advise` or `review`, divide the original scope into explicit, independently checkable packages by file, module, symbol, or question. Name the covered paths and expected output in each request so no part of the original scope is silently dropped. Ask the peer to bound command output with focused searches or path-limited diffs rather than dumping whole files or suites.
3. Make fresh read-only calls for those packages and reconcile their results against the original scope. If any package still blocks, report its uncovered scope; do not promote partial transcript text to a verdict. For `implement`, request only verified remaining work as a separate bounded task after checking what already changed. Never replay the whole write-capable task automatically.

## Artifact inspection

The bridge stores bounded transcripts at `.temp/bridge/raw-<timestamp>.txt`, detached-job records at `.temp/bridge/jobs/<job-id>.json`, durable cancellation markers at `.temp/bridge/jobs/<job-id>.cancel.json`, health records at `.temp/bridge/health.jsonl`, and sanitized fault records at `.temp/bridge/incidents/<timestamp>-<fault>.json`. Approved setup execution uses the platform user-state root under `bridge-setup/`: one owner-only HMAC key authenticates approval payloads, per-target/scope locks prevent concurrent user-scoped mutation across workspaces, and regular non-symlink JSONL records enforce one-use approvals and recurrence. These credential-free records contain only allowlisted identities, fingerprints, timestamps, classifications, outcomes, and rollback metadata; they never contain native output, authentication output, provider credentials, or raw native argv. Planning and checks do not create state. Setup retains this state by default so replay and repeated-fault evidence survives across workspaces; a journal larger than the bounded reader limit fails closed. Cleanup is operator-owned: archive evidence first and remove the complete `bridge-setup` state only when no setup operation or unexpired approval remains, accepting that removal invalidates approvals and recurrence history. The compact envelope's public fields carry decisions, blockers, and remaining work; its `transcript_path` points to transcript-only `details` and retained child output for additional evidence. Completed Codex command-output payloads are replaced by `aggregated_output_bytes` and `aggregated_output_sha256`, computed from the decoded output re-encoded as UTF-8; the digest supports equality checks against separately retained output but does not reconstruct it or add a raw archive. Compaction reduces Bridge capture bytes only and does not establish provider token savings. Details never hide required work, and `incident` is a workspace-relative path to a compact diagnostic file whose `fault` must be inspected after opening it. Preserve incident and review evidence before applying local retention or cleanup.
