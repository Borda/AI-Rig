---
name: setup
description: Configure, authenticate, repair, and verify the Claude Code to Codex Bridge; safe stages run by default and sensitive stages require separate approval.
argument-hint: '[action=all|check|configure|authenticate|repair|verify-live] [target=peer|codex|claude] [scope=auto|user|project|local] [live=prompt|skip|required]'
allowed-tools: Bash, Write, AskUserQuestion
effort: medium
---

# Set Up the Bridge

## Bootstrap and plan

- Treat a plain invocation as `action=all target=peer scope=auto live=prompt`.
- Loaded Claude plugin, Claude trust, Claude authentication, and current session are external bootstrap prerequisites. Never replace or restart current invocation surface.
- Reject a model-supplied workspace; use Claude's launch workspace — the working directory of the current Claude Code session, never a path named in conversation or task text.
- Run `python --version` first; stop if unavailable or older than Python 3.10; report the interpreter found, or its absence, and the 3.10 minimum.
- Parse only documented `key=value` grammar plus one-release compatibility forms `--live` (= `action=verify-live live=required`) and `--direction codex|claude` (= `target=codex|claude`); reject ambiguous or unknown arguments.

For one resolved target, invoke:

```bash
python "${CLAUDE_PLUGIN_ROOT}/bin/bridge_setup.py" --current-host claude --workspace "<launch-workspace>" --action "<action>" --target "<target>" --scope "<scope>" --live "<live>"
```

Deterministic planner performs credential-free inspection, returns setup result JSON defined by `schemas/setup-result.schema.json`.

## Check and approved changes

For `check`, report the result and stop. For `all`, `configure`, or `repair`:

1. If the planned `operations` list is empty, report nothing to configure and stop under `configure` or `repair`; under `all`, continue to the stages below without requesting approval.

2. Before mutation, Write every planned `operations` entry and the approval digest to `.temp/bridge-setup/operations-<first 12 digest chars>.md`, together with:

   - action and purpose;
   - exact native argv and resolved target/scope;
   - external capability;
   - credential behavior;
   - filesystem and host-configuration effects;
   - rollback evidence;
   - retry policy; and
   - safe denial outcome.

   Print that file once as the last reply text before the step-3 question. Never put operations or the digest in an option `preview`.

3. Obtain explicit approval for exactly that digest via the `AskUserQuestion` tool — never a plain-text question. Question text: at most 6 plain lines (target 600 characters, hard cap 1,500), no table, pipe layout or code fence, carrying the action, operation count, full approval digest and `→ <operations file path>`. Reply text before a tool call can arrive as an empty progress update; the question text is always rendered, so it alone must identify what is approved — never ask approval for operations the user could not see. Options: a label plus a one-line effect each, no previews, recommended option second.

4. After approval, give the operator the identical command plus `--approve "<approval_digest>"` to run in their own terminal, with `${CLAUDE_PLUGIN_ROOT}` expanded to its absolute path (it is unset there; read it with `echo "${CLAUDE_PLUGIN_ROOT}"`). Then re-plan with `--action check` to report the outcome.

> Host-held HMAC makes digest tamper-evident but does not grant consent; explicit operator or host approval remains required. The digest is action-bound, expires, and is consumed by its first execution attempt; it lives 300 seconds. Never substitute `--approve` without its digest. Denial, changed or expired digest, replay, unsupported capability, failed probe, or failed native operation stops without retry.

> Operator proof: `--approve` executes only when stdin and stderr are the operator's terminal; otherwise the helper exits 2 with `operator-terminal-required` before inspecting, locking or mutating anything. Never run `--approve` through model-controlled Bash, a pipe, or an allocated TTY; run only the planning command (no `--approve`) there. The digest the model planned is not operator approval.

## Authentication

When `action=all` or `action=authenticate` and authentication remains (`configure` and `repair` stop after their result, reporting `remaining`):

1. Re-plan the provider-owned interactive login with identical host, workspace, target, scope, and live values but `--action authenticate`.
2. Obtain separate approval for that action's digest via the `AskUserQuestion` tool, with the same question-text contract as step 3 above: the question text names the full authentication digest and the exact `authentication_argv`, and states it may open a browser and use network and account state. No previews.
3. Give operator identical authenticate command plus `--approve "<authentication_digest>"` to run in their own terminal, with the absolute plugin path as in step 4 above.

> Sensitive phase: never run through model-controlled Bash or another captured tool stream. Bridge then launches only native login command with terminal inherited. Never accept, request, pipe, echo, inspect, or store a token, API key, browser code, device code, email, or raw login output. Process exit means `auth-flow-launched`; only a later redacted status probe may establish `host-authenticated`.

## Live verification

When `action=all` or `action=verify-live`, `live=prompt` or `live=required`, and `remaining` still lists `live-verification`:

1. Precondition: a fresh plan with identical host, workspace, target, scope, and live values but `--action all` must show an empty `operations` list and `remaining` listing neither `configuration` nor `authentication`. Otherwise the approved call would be refused (`configuration-needed` or `authentication-needed`, exit 2) after the user already paid attention to it: report the missing stage and stop. A configuration result classified `fresh-session-required` needs a fresh session first.
2. Re-plan with identical host, workspace, target, and scope but `--action verify-live`. Exit 2 with `status=manual` and `classification=live-verification-requires-separate-approval` is this plan's normal result, not a failure; its `approval_digest` is the live digest.
3. Obtain a third approval for that action's digest via the `AskUserQuestion` tool and one paid provider call, with the same question-text contract as step 3 above: the question text names the full live digest and the one paid call. No previews.
4. State network, installed-CLI-managed credential, quota/cost, workspace, and point-in-time semantics in that question text, or, when they do not fit its 6 lines, in a file printed once as the last reply text before the question, which the question text names as `→ <path>`.
5. Give the operator that same verify-live command plus `--approve "<live_digest>"` to run in their own terminal, with the absolute plugin path as in step 4 above.

> Never invoke the lower-level live doctor outside this approval path. Successful live CLI result remains partial until the applicable `bridge_status` session/workspace evidence named under Completion boundary is also present. `live=skip` remains `inference-unverified`; never call it ready. Denial under `live=required` is non-ready.

## Completion boundary

For Codex-to-Claude repository transfer consent, continue in a fresh Codex session with `$bridge:setup`; its [provider-consent stage](../../rules/provider-consent.md) records an explicit operator-approved policy. Installing either half does not grant that consent. Do not write Codex policy from this Claude setup flow or reuse a configuration/authentication/live-check approval for it. This stage governs the reverse Codex-to-Claude route, not Claude-to-Codex authorization.

- Setup always owns one resolved peer target.
- To prepare both integrations, finish peer lifecycle from Claude, start any required fresh session, then run `$bridge:setup` from Codex for its peer; no digest, state claim, or readiness result is shared between hosts.
- Loaded-host branch is check/bootstrap-only, never mutates its current invocation surface.
- `bridge_status` evidence belongs to a fresh Codex session. An unbound reverse backend is loaded but has no selected project; use `bridge_bind_workspace` there with the known absolute folder as untrusted `proposed_workspace` and exact canonical native confirmation; empty arguments retain manual selection plus confirmation. Require its current `binding_id` and intended workspace before claiming reverse MCP session/workspace ready or passing executable calls; binding grants no runtime permission or paid-call consent.
- Report strongest verified level, exact remaining action, confidence, and limits.

> Never equate static readiness, process exit, host authentication, session readiness, workspace readiness, or live verification.
