# 🌉 bridge_CC-Codex — Claude Code ↔ Codex

Codex questions use synchronous `request_user_input` for required or flow-changing decisions only when the active host permits that purpose and every feasible choice fits. Otherwise invoke permitted `request_user_input_async`, including required decisions without independent work, and keep dependent actions pending. Optional questions prefer permitted async, falling back to permitted sync when async is unavailable or unsuitable. Plain chat is reserved for questions neither native control can support. Option-based questions put one evidence-backed choice first with `(Recommended)` in its label; the suffix maps to the unchanged canonical answer and never grants consent. Conversational approvals use Approve/Deny; exact-digest protocols and runtime permissions remain separate. Silence, preselection, stale or duplicate replies never authorize action. All Codex entrypoints load their plugin-local guidance; no sibling plugin or global setup is required. [Codex CLI 0.154.0](https://learn.chatgpt.com/docs/changelog) introduced inline selectable asynchronous TUI questions, but version alone does not establish tool availability. Older/headless hosts retain plain-chat or unresolved-input fallback; no plugin-wide minimum or automatic upgrade is added. Claude-specific question behavior is unchanged.

Rejected question calls resume at the pending decision without replaying delivered report context. A synchronous mode error does not establish async unavailability; explicit higher-priority host requirements for plain text remain binding and are reported as policy restrictions, not missing tools.

`bridge_CC-Codex` lets Claude Code and OpenAI Codex hand one another bounded implementation, advice, and review requests. Its normalized plugin identifier is `bridge`. It is one repository with two independently installable host integrations: the Claude Code half calls the `codex` CLI, and the Codex half calls Claude through the bridge's host-launched MCP server.

The bridge is useful with either host integration installed and has no dependency on another plugin from this repository. Existing-plugin replacement and consumer migration are deliberately outside this standalone package.

> Release: `0.5.1`. Claude- and Codex-side setup skills provide an approval-bound lifecycle for safe configuration and repair while retaining full caller-input, workspace/session authority, recursion, asynchronous lifecycle, envelope/transcript, and approval boundaries.

______________________________________________________________________

<details markdown="1">
<summary><strong>📋 Contents</strong></summary>

- [What it provides](#-what-it-provides)
- [Requirements](#-requirements)
- [Set up the bridge](#-set-up-the-bridge)
- [Is MCP required?](#-is-mcp-required)
- [Install for Claude Code](#-install-for-claude-code)
- [Use from Claude Code](#-use-from-claude-code)
- [Install for Codex](#-install-for-codex)
- [Use from Codex](#-use-from-codex)
- [Model, effort, budget, and depth](#-model-effort-budget-and-depth)
- [Results and artifacts](#-results-and-artifacts)
- [Privacy and security boundaries](#-privacy-and-security-boundaries)
- [Updating, uninstalling, and human-owned gates](#-updating-uninstalling-and-human-owned-gates)
- [Maintainer documentation](#-maintainer-documentation)
- [License and attribution](#-license-and-attribution)

</details>

______________________________________________________________________

<a id="-what-it-provides"></a>

## 🎯 What it provides

The two hosts expose the same three bridge request operations and an approval-bound setup lifecycle:

| Operation   | Purpose                                                                    | Default access                                                                                         |
| ----------- | -------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| `implement` | Complete a bounded change and report the resulting files and limits.       | Write-capable (`workspace-write` for Codex, `acceptEdits` for Claude).                                 |
| `advise`    | Answer a focused question without editing files.                           | Read-only.                                                                                             |
| `review`    | Perform an adversarial review of the current diff or supplied artifact.    | Read-only.                                                                                             |
| `setup`     | Inspect, configure, repair, authenticate, and verify one bridge direction. | Safe inspection/configuration by default; authentication and live inference require separate approval. |

Every bridge request call carries a model and reasoning-effort selection:

- An omitted `model` uses the target CLI's configured host default; the envelope records `model: "host-default"`.
- An omitted skill effort is classified from the complete task using the shipped effort policy; direct CLI or MCP calls that bypass a skill use `medium`.
- Supported effort aliases are normalized before dispatch, invalid values are blocked, and an explicitly supplied supported-effort list may cause one recorded downgrade.

Every call also carries a soft wall-clock budget and a recursion `depth`:

- The budget is announced to the callee, which is asked to return a useful partial result with `remaining` and `blockers` instead of waiting indefinitely.
- The bridge enforces a hard cutoff at 1.2 times the soft budget.
- A depth of one or greater is refused, so a Claude → Codex → Claude loop cannot continue.

Results are compact envelopes rather than transcripts:

- The public envelope carries decision-critical `status`, `verdict`, `findings`, `files_touched`, `remaining`, and `blockers`, plus observed harness metadata such as model, effort, cost, duration, depth, `run_id`, and a session identifier when applicable.
- Incomplete work and blockers must stay in those public fields; transcript-only `details` hold additional evidence and never hide required work.
- The envelope carries workspace-relative `transcript_path` and, for faults, an `incident` path; open the referenced JSON file and inspect its `fault` member only when the compact result needs investigation.
- Detached calls return their job identifier separately.

> The model-authored core is validated separately from harness metadata; model output cannot claim observed cost, timing, process, or correlation fields.

<a id="-requirements"></a>

## ✅ Requirements

- Claude Code with plugin support for the Claude half.
- OpenAI Codex CLI for Claude → Codex calls, available as `codex` on `PATH` and authenticated for the requested model.
- Claude Code CLI for Codex → Claude calls, available as `claude` on `PATH` and authenticated for the requested model.
- A `python` executable on `PATH` that reports Python 3.10 or newer for the bridge's Python entry points; the same launcher is used on POSIX and Windows.
- A writable project-local `.temp/bridge/` directory for bridge state and transcripts.
- A writable platform user-state directory for the host-held approval-integrity key, one-use approval receipts, per-target mutation locks, and sanitized setup records; setup never stores provider credentials or raw login output there.

The setup skill can orchestrate verified native plugin/configuration operations and one closed repair per fault after an exact plan approval.

> It never installs runtimes, replaces the current host invocation surface, reads credentials, or grants permissions. Provider-owned authentication and live inference retain separate approvals and terminal/network boundaries. The Codex-side MCP server is host-launched outside the model's sandbox so it can reach the Claude CLI's normal authentication path; a Claude CLI that is not logged in remains a reported setup/authentication failure.

<a id="-set-up-the-bridge"></a>

## 🧭 Set up the bridge

The loaded host is the current host and `target=peer` resolves to the other integration.

> A current-host plugin, trust, authentication, and fresh session are external bootstrap prerequisites; a setup skill cannot repair the invocation surface from which it was loaded.

After bootstrap, run the same command on either host:

```text
/bridge:setup action=all target=peer scope=auto live=prompt
$bridge:setup action=all target=peer scope=auto live=prompt
```

Plain `/bridge:setup` and `$bridge:setup` invocations use those defaults. `all`:

1. Inspects, proposes, and reports every supported stage.
2. Asks for one expiring, one-use approval before safe configuration or repair, bound to the exact action, target, resolved scope, workspace, observed-state fingerprint, native argv, external capability, rollback record, and stop condition.

> The digest is authenticated with a host-held per-user HMAC key so a caller cannot forge or alter a plan, but the digest is not consent: the operator or host permission surface must still approve the displayed operation. A denial, changed or expired digest, replay, failed probe, or failed operation stops without an equivalent retry.

The remaining setup stages have distinct boundaries:

| Stage                                  | Behavior and boundary                                                                                                                                                                                                                                                 |
| -------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `action=check`                         | Credential-free inspection only.                                                                                                                                                                                                                                      |
| `action=configure` and `action=repair` | Apply one approved native operation, then re-inspect the host inventory before reporting whether a fresh session is required.                                                                                                                                         |
| `action=authenticate`                  | Launches only the peer's official no-capture login through a separately planned provider-owned interactive flow that the operator runs in their own terminal, outside model-captured tool streams, so Bridge can inherit the terminal without receiving login output. |
| `action=verify-live`                   | Performs one separately approved live probe through the same planner/executor; live verification uses a third action-bound approval for one paid provider call.                                                                                                       |
| `live=skip`                            | Finishes at `inference-unverified`.                                                                                                                                                                                                                                   |
| `live=required`                        | Is non-ready when the live approval or probe fails.                                                                                                                                                                                                                   |

One setup run owns one peer target. To prepare both integrations, complete the peer lifecycle from one host, honor any fresh-session boundary, then run the other host's setup skill; no approval or readiness claim is shared across them.

The setup result is defined by `schemas/setup-result.schema.json`, separate from the model bridge envelope:

- It reports the strongest evidence level reached: `static`, `host-authenticated`, `session-ready`, `workspace-ready`, or `live-verified`.
- It never treats process exit, authentication, or static checks as proof of inference.
- The deterministic setup CLI cannot prove the loaded session/workspace and therefore remains non-ready even after a successful point-in-time live probe; the host skill may claim a stronger lifecycle result only after applicable loaded-session evidence is also present.
- The read-only MCP tool `bridge_status` returns sanitized server identity, version, schema/protocol version, host-selected canonical workspace, workspace fingerprint, and expected tool inventory without calling a provider or writing state.

<a id="-is-mcp-required"></a>

## 🔌 Is MCP required?

MCP is complementary to the bridge as a whole but mandatory for the Codex → Claude Code direction. Claude Code → Codex calls launch `codex exec` directly and do not need MCP. Codex → Claude Code calls must use the packaged MCP server because a `claude --print` process started from a sandboxed Codex model turn cannot rely on the normal Claude authentication context, while the Codex host launches the MCP server outside that model sandbox.

If you install only the Claude Code half to call Codex, MCP is not required. If you install only the Codex half or want the complete bidirectional bridge, the `.mcp.json` declaration and `bin/bridge_mcp.py` are required transport components, not optional enhancements. The MCP boundary provides the three request tools plus the read-only status tool and prevents model-controlled workspace, background, or session selection.

<a id="-install-for-claude-code"></a>

## 📦 Install for Claude Code

The Codex-facing MCP surface has four tools: `bridge_implement`, `bridge_advise`, `bridge_review`, and the zero-provider read-only `bridge_status`. MCP is required for Codex → Claude Code and full bidirectional use, but not for Claude Code → Codex-only use.

Add the AI-Rig marketplace and install `bridge_CC-Codex`:

```bash
claude plugin marketplace add Borda/AI-Rig
claude plugin install bridge@borda-ai-rig
```

Start a fresh Claude Code session after installation. Run the static local CLI check with:

```text
/bridge:setup
```

The default setup path is end-to-end and approval-bound:

1. Check the installed payload and native CLI capabilities.
2. Propose safe configuration or repair.
3. Apply only the approved exact operations.
4. Separately offer provider-owned authentication when needed.
5. Verify each applicable evidence level.

> It never captures sensitive login material or claims readiness beyond the evidence returned. Use the canonical syntax above; the legacy `--live` and `--direction` forms are accepted only for one release and are normalized to the same approval boundaries:

```text
/bridge:setup action=verify-live target=peer live=required
```

<a id="-use-from-claude-code"></a>

## ⚡ Use from Claude Code

Invoke the namespaced skills directly. Pass a concrete task or question and select the model and effort when the host default is not appropriate:

```text
/bridge:implement --model <codex-model> --effort <effort> --timeout-seconds 600 "Run the focused test and fix the smallest verified defect."
/bridge:advise --model <codex-model> --effort <effort> --timeout-seconds 120 "Which files own this behavior, and what evidence should I collect first?"
/bridge:review --model <codex-model> --effort <effort> --timeout-seconds 300 "Review the current diff for correctness, regressions, and missing tests."
```

The verb skills accept the bridge's task text and these caller-selected fields:

| Applies to            | Fields and behavior                                                                                                                                                                                                            |
| --------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| All verbs             | `--timeout-seconds`, `--depth`, `--run-id`, and `--workspace`. Omitted budget uses the per-verb default (`advise` 120 seconds, `review` 300 seconds, `implement` 600 seconds); omitted depth starts an outermost call at zero. |
| `implement`           | `--background` and `--session-id`. A write-capable implementation is never automatically retried after timeout because its process may already have changed the worktree.                                                      |
| `advise` and `review` | May receive one bounded retry at the next lower supported effort tier, so the retry can finish within the same budget.                                                                                                         |

Long-running implementation work may be detached. Use the job identifier printed by the bridge to inspect or stop it:

```text
/bridge:status <job-id>
/bridge:result <job-id>
/bridge:cancel <job-id>
```

> Background execution and lifecycle controls are available only for Claude Code → Codex `implement`. `advise` and `review` are foreground, ephemeral calls; the Codex → Claude MCP transport does not expose reverse background requests or separate status/result/cancel skills.

After an implementation returns:

1. Re-read every path in `files_touched[]` before making another edit.
2. Do not edit paths named by a still-running implementation task; the job record is the coordination boundary.

Session continuation is also limited to Claude Code → Codex `implement`:

- Pass the explicit session identifier returned by a prior implementation.
- Keep the same workspace.
- Never use a “most recent session” selector.
- Advice and review always start fresh ephemeral Codex runs.
- The reverse MCP path does not resume Claude sessions.

<a id="-install-for-codex"></a>

## 📦 Install for Codex

Register the repository marketplace and add the Codex plugin:

```bash
codex plugin marketplace add Borda/AI-Rig
codex plugin add bridge@borda-ai-rig
```

The Codex manifest declares the bridge MCP server, and the installed `.mcp.json` starts `bin/bridge_mcp.py` from the plugin root.

> The server treats the current directory selected by the Codex host as its trusted workspace; open the Codex session in the intended project and do not use write-capable calls if the installed host launches the MCP server from a different directory. If Codex asks you to trust or enable the installed MCP content, review the displayed command and approve it according to your local policy.

Start a fresh Codex session after installation, then run the Codex-side setup skill:

```text
$bridge:setup
```

The Codex half degrades cleanly when `claude` is absent or unauthenticated: setup reports the prerequisite and bridge calls return a structured blocked result.

> The static planner does not establish that the current MCP session is loaded or workspace-bound; use `bridge_status` from a fresh Codex session for that evidence. The bridge does not install Claude, read credentials from files, or fall back to a shell call inside the Codex sandbox.

<a id="-use-from-codex"></a>

## ⚡ Use from Codex

Use the Codex skills for the same three operations:

```text
$bridge:implement --model <claude-model> --effort <effort> --timeout-seconds 600 "Implement the bounded change and report the files touched."
$bridge:advise --model <claude-model> --effort <effort> --timeout-seconds 120 "Explain the smallest safe next step without editing files."
$bridge:review --model <claude-model> --effort <effort> --timeout-seconds 300 "Review the current diff and list actionable findings."
```

The skills invoke the bridge MCP tools `bridge_implement`, `bridge_advise`, `bridge_review`, and `bridge_status`.

| Tool group           | Contract                                                                                                                                                                                                                                            |
| -------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Bridge request tools | Accept `task`, optional `model` and `effort`, and optional `timeout_seconds`, `depth`, `run_id`, and supported-effort capability data. Omitted model, effort, depth, and run ID use the host-default, `medium`, zero, and a new UUID respectively.  |
| `bridge_status`      | Accepts no workspace override and performs no peer, provider, write, repair, or authentication operation.                                                                                                                                           |
| Reverse timeouts     | Implementations accept at most 700 seconds; advice and review accept at most 350 seconds because their one allowed timeout retry, including per-attempt termination and drain overhead, must also finish within the MCP host's 900-second deadline. |
| Host boundary        | The host-launched server binds the request to its launch workspace and rejects model-supplied workspace, background, and session fields, so a tool call cannot widen filesystem authority.                                                          |

The bridge supplies the budget preamble, invokes `claude -p` with the narrowest permission mode for the verb, and returns the same compact envelope used by the Claude half. The peer's bounded verbose `details` remain in the transcript referenced by the envelope; they are not copied into the caller's context.

> Do not invoke `claude -p` directly from a sandboxed Codex model turn: the bridge MCP server is the supported transport because it runs in the host context where the normal Claude authentication path is available.

<a id="-model-effort-budget-and-depth"></a>

## 🎚️ Model, effort, budget, and depth

Model and effort selection:

- The caller's explicit model is passed to the target CLI; the bridge does not discover or validate model availability before dispatch.
- An omitted model uses the target host default and is represented as `host-default` in the envelope.
- The caller's explicit effort is normalized before dispatch. Supported values are `minimal`, `low`, `medium`, `high`, `xhigh`, and `max`, with `trivial` and `none` accepted as aliases for `minimal`.
- An invalid value returns `blocked`; when a caller supplies supported-effort capabilities, one lower supported value may be recorded in `effort_substituted`.

Budget and result states:

| Item            | Contract                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| --------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Soft budget     | Two minutes for `advise`, five minutes for `review`, and ten minutes for `implement`. A callee receives the budget in-band and is expected to scope its work to fit.                                                                                                                                                                                                                                                                                          |
| Hard cutoff     | 1.2× the soft budget.                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| Transport input | MCP accepts at most 16,384 task characters and Bridge rejects task UTF-8 over 16 KiB or a constructed prompt over 20 KiB before creating artifacts or launching a peer. Bridge resolves each peer executable and measures the full argv with Windows `list2cmdline` UTF-16 semantics; `.cmd`/`.bat` shims use a conservative 8,000-unit ceiling below Cmd.exe's 8,191-character limit.                                                                        |
| Child output    | Bridge bounds each pending stdout record independently and retains at most 256 KiB combined stdout/stderr, reserving transcript wrapper space so the transcript remains at most 256 KiB. Only complete valid Codex command events replace `aggregated_output` with `aggregated_output_bytes` and `aggregated_output_sha256`; overflow during capture or final flush still terminates the peer tree with `output-limit`, including timeout/cancellation paths. |
| `partial`       | Valid work with explicit `remaining`.                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `blocked`       | Names the permission, authentication, or input blocker.                                                                                                                                                                                                                                                                                                                                                                                                       |
| `timeout`       | Identifies a cutoff.                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `refused`       | Identifies recursion protection or another deliberate refusal.                                                                                                                                                                                                                                                                                                                                                                                                |

Depth and correlation:

- `depth` starts at zero for a caller-originated request.
- Before launching the peer, the transport increments it in `CC_CODEX_BRIDGE_DEPTH`; the peer CLI and MCP process inherit that trusted value, and caller input cannot lower it.
- Negative depth is rejected, and a call received at trusted depth one or greater returns `refused: recursion-depth`.
- `run_id` is minted once at the outermost call and echoed through the call chain, so health records from both hosts can be correlated without conflating a Codex thread identifier with a Claude session identifier.

<a id="-results-and-artifacts"></a>

## 📊 Results and artifacts

Each child attempt writes its bounded host transcript once. Complete Codex command-output payloads are summarized by their UTF-8 byte counts and SHA-256 digest; other retained events and Claude output remain unchanged. The digest can compare separately retained output but cannot reconstruct discarded content, and capture-byte savings do not establish provider token savings.

> The public envelope returns the compact decision core, including decisions, blockers, and remaining work, with observed metadata and workspace-relative transcript or incident references; verbose peer `details` stay in the transcript as additional evidence and never substitute for required public fields.

Bridge state is project-local:

```text
.temp/bridge/raw-<timestamp>.txt                    bounded transcript, referenced by the envelope
.temp/bridge/jobs/<job-id>.json                     detached-job metadata and lifecycle state
.temp/bridge/jobs/<job-id>.cancel.json              durable cooperative-cancellation request
.temp/bridge/health.jsonl                           one health/cost record per completed bridge call
.temp/bridge/incidents/<timestamp>-<fault>.json     sanitized fault and recovery evidence
```

Artifact handling:

- Incident records do not persist child command arguments or environment data. They preserve the classified fault, reason, model, effort, verb, budget, transcript path, and—when a write-capable process is killed—the observed worktree delta. That delta is advisory: unchanged dirty status or unavailable Git can produce an empty delta despite edits, so inspect the actual worktree independently.
- The health log records direction, verb, model, effort, cost/tokens when reported by the host, duration, status, depth, and `run_id`.
- The predictable `health.jsonl` member is opened without following links or reparse points, then its opened descriptor must be a regular single-link file. A prepared symlink, reparse point, or hard link returns a contained blocked result and does not write its target. This does not provide universal containment if an attacker creates a new hard link after the descriptor check.
- Setup summarizes blocked/timeout/refused counts, their latest timestamps, and reported cost by direction, verb, and model; static CLI findings are reported separately.

> Artifacts are evidence, not authority. Read the envelope, source changes, tests, permissions, and remaining limits before accepting consequential work. Delete `.temp/bridge/` only under your project's normal retention policy and only after preserving any incident or review evidence you still need.

For an `output-limit` incident, use the [bounded-work recovery steps](https://github.com/Borda/AI-Rig/blob/main/plugins/bridge_cc-codex/docs/operations.md#output-limit-recovery). A compact final answer alone does not cap tool output, and the Bridge does not automatically retry this fault.

<a id="-privacy-and-security-boundaries"></a>

## 🔒 Privacy and security boundaries

The bridge sends the task text and the selected project context to the provider CLI named by the direction of the call. Provider billing, retention, account access, and model availability remain governed by the provider and your host configuration. The bridge does not upload artifacts to a separate service or persist credentials.

Safety boundaries:

- Use `advise` and `review` for read-only work.
- `implement` can modify the current worktree under the host's normal permission policy.
- Setup configuration and repair are state-changing operations bound to an exact approval digest.
- Authentication failures, permission denials, unsupported models, and unknown faults are surfaced as structured results or incidents.
- Timeout and cancellation give POSIX peer groups a two-second cooperative grace, then force termination even when the group leader already exited. Windows invokes `taskkill /T /F` before a leader can exit and orphan its tree; native Windows reparse and tree behavior still require native-host verification.
- Provider login output is never captured, and rollback never touches credentials.
- The peer child receives an allowlisted environment, not the caller's: base process keys, provider authentication keys, and on Windows the interpreter start-up keys. Unrelated API keys, cloud credentials, and the session's inter-agent messaging variables are dropped. A host whose proxy or enterprise provider path needs more names them in `BRIDGE_CHILD_ENV_EXTRA` (comma-separated).
- ! BREAKING — every Codex invocation pins `sandbox_workspace_write.network_access=false`, so an `implement` task that installed a dependency, refreshed a lockfile, or ran a network-touching hook now fails inside the sandbox. The control is Codex-side only; the Claude child is constrained by permission mode instead.

Termination reaps the child leader after native tree cleanup, including Windows `taskkill` and direct-kill fallbacks. The final wait is bounded to two seconds; unavailable exit status is diagnosed rather than fabricated. Existing timeout, cancellation, and output-limit classifications stay intact.

Finalization freezes the bounded capture under its lock; a failed drain returns a non-retrying blocked drain failure rather than timeout or cancellation unless output-limit was already observed, and late unseen bytes are not reclassified as output-limit.

> The bridge never bypasses host permission prompts, invents a credential, retries a write-capable timeout, or silently replaces a requested effort tier.

<a id="-updating-uninstalling-and-human-owned-gates"></a>

## ⬆️ Updating, uninstalling, and human-owned gates

Update the marketplace snapshot through the host's normal plugin manager, then restart the host session. Uninstalling one half does not remove the other half or alter host credentials. Remote publication, marketplace refresh, provider login, MCP trust approval, and any permission escalation remain human-owned operations.

The bridge's checked-in manifests and host baseline describe the CLI surface it was verified against. If setup reports missing or changed flags, upgrade the host or use a bridge release that supports the installed host; do not patch the baseline at runtime. Ordinary `make sync-*` runs invoke only the direct static doctor with explicit read-only semantics; they never invoke a setup skill, model, approval token, authentication, repair, restart, or provider call. Run the local verification commands below after source changes. On Linux or macOS:

```bash
python -m pytest -q plugins/bridge_cc-codex
git diff --check -- plugins/bridge_cc-codex
disposable_parent_directory="$(mktemp -d)"
python plugins/bridge_cc-codex/scripts/build_package.py --output "$disposable_parent_directory/bridge"
python plugins/bridge_cc-codex/scripts/validate_package.py "$disposable_parent_directory/bridge"
```

On native Windows PowerShell:

```powershell
& python -m pytest -q plugins/bridge_cc-codex
git diff --check -- plugins/bridge_cc-codex
$disposableParentDirectory = Join-Path ([System.IO.Path]::GetTempPath()) ("bridge-" + [System.Guid]::NewGuid())
New-Item -ItemType Directory -Path $disposableParentDirectory | Out-Null
$disposablePackageDirectory = Join-Path $disposableParentDirectory "bridge"
& python plugins/bridge_cc-codex/scripts/build_package.py --output $disposablePackageDirectory
& python plugins/bridge_cc-codex/scripts/validate_package.py $disposablePackageDirectory
```

Run `action=verify-live ... live=required` only after explicitly accepting the separate provider call and its cost. A live setup probe verifies one selected path at that moment; it is diagnostic evidence, not proof that a future task will succeed. This documentation does not claim that either host is currently authenticated or live-verified.

<a id="-maintainer-documentation"></a>

## 📚 Maintainer documentation

- [Architecture and transport](https://github.com/Borda/AI-Rig/blob/main/plugins/bridge_cc-codex/docs/architecture.md) explains both request directions, process boundaries, permissions, and exactly where MCP is required.
- [Security and privacy](https://github.com/Borda/AI-Rig/blob/main/plugins/bridge_cc-codex/docs/security.md) defines authority, data flow, artifacts, result integrity, recovery, and cancellation boundaries.
- [Operations and troubleshooting](https://github.com/Borda/AI-Rig/blob/main/plugins/bridge_cc-codex/docs/operations.md) covers prerequisites, diagnosis, foreground calls, detached jobs, common failures, and artifact inspection.
- [Development and release verification](https://github.com/Borda/AI-Rig/blob/main/plugins/bridge_cc-codex/docs/development.md) records the package layout, local gates, installed-shape validation, and release responsibilities.

<a id="-license-and-attribution"></a>

## 📄 License and attribution

This plugin is distributed under the Apache License, Version 2.0; see [LICENSE](https://github.com/Borda/AI-Rig/blob/main/plugins/bridge_cc-codex/LICENSE). Repository attribution is in [NOTICE](https://github.com/Borda/AI-Rig/blob/main/plugins/bridge_cc-codex/NOTICE). The bridge's implementation is maintained as a self-contained package and does not require a sibling plugin.
