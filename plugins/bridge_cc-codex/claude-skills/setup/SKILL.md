---
name: setup
description: Configure, authenticate, repair, and verify the Claude Code to Codex Bridge; safe stages run by default and sensitive stages require separate approval.
argument-hint: '[action=all|check|configure|authenticate|repair|verify-live] [target=peer|codex|claude] [scope=auto|user|project|local] [live=prompt|skip|required]'
allowed-tools: Bash
---

# Set Up the Bridge

## Bootstrap and plan

- Treat a plain invocation as `action=all target=peer scope=auto live=prompt`.
- Loaded Claude plugin, Claude trust, Claude authentication, and current session are external bootstrap prerequisites. Never replace or restart current invocation surface.
- Reject a model-supplied workspace; use Claude's launch workspace.
- Run `python --version` first; stop if unavailable or older than Python 3.10.
- Parse only documented `key=value` grammar plus one-release compatibility forms `--live` and `--direction codex|claude`; reject ambiguous or unknown arguments.

For one resolved target, invoke:

```bash
python "${CLAUDE_PLUGIN_ROOT}/bin/bridge_setup.py" --current-host claude --workspace "<launch-workspace>" --action "<action>" --target "<target>" --scope "<scope>" --live "<live>"
```

Deterministic planner performs credential-free inspection, returns setup result JSON defined by `schemas/setup-result.schema.json`.

## Check and approved changes

For `check`, report the result and stop. For `all`, `configure`, or `repair`:

1. Show every planned `operations` entry and the approval digest before mutation.
2. Obtain explicit approval for exactly that digest. State:
   - action and purpose;
   - exact native argv and resolved target/scope;
   - external capability;
   - credential behavior;
   - filesystem and host-configuration effects;
   - rollback evidence;
   - retry policy; and
   - safe denial outcome.
3. After approval, rerun the identical command with `--approve "<approval_digest>"`.

> Host-held HMAC makes digest tamper-evident but does not grant consent; explicit operator or host approval remains required. The digest is action-bound, expires, and is consumed by its first execution attempt. Never substitute `--approve` without its digest. Denial, changed or expired digest, replay, unsupported capability, failed probe, or failed native operation stops without retry.

## Authentication

When authentication remains:

1. Re-plan the provider-owned interactive login with identical host, workspace, target, scope, and live values but `--action authenticate`.
2. Obtain separate approval for that action's digest and state exact `authentication_argv` may open a browser and use network and account state.
3. Give operator identical authenticate command plus `--approve "<authentication_digest>"` to run in their own terminal.

> Sensitive phase: never run through model-controlled Bash or another captured tool stream. Bridge then launches only native login command with terminal inherited. Never accept, request, pipe, echo, inspect, or store a token, API key, browser code, device code, email, or raw login output. Process exit means `auth-flow-launched`; only a later redacted status probe may establish `host-authenticated`.

## Live verification

When `live=prompt` or `live=required` reaches `inference-unverified`:

1. Re-plan with identical host, workspace, target, and scope but `--action verify-live`.
2. Obtain a third approval for that action's digest and one paid provider call.
3. State network, installed-CLI-managed credential, quota/cost, workspace, and point-in-time semantics.
4. Rerun that same verify-live command with `--approve "<live_digest>"`.

> Never invoke the lower-level live doctor outside this approval path. Successful live CLI result remains partial until applicable loaded-session/workspace evidence also present. `live=skip` remains `inference-unverified`; never call it ready. Denial under `live=required` is non-ready.

## Completion boundary

- Setup always owns one resolved peer target.
- To prepare both integrations, finish peer lifecycle from Claude, start any required fresh session, then run `$bridge:setup` from Codex for its peer; no digest, state claim, or readiness result is shared between hosts.
- Loaded-host branch is check/bootstrap-only, never mutates its current invocation surface.
- `bridge_status` evidence belongs to fresh Codex session, required before claiming reverse MCP session/workspace ready.
- Report strongest verified level, exact remaining action, confidence, and limits.

> Never equate static readiness, process exit, host authentication, session readiness, workspace readiness, or live verification.
