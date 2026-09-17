---
name: setup
description: Configure, authenticate, repair, and verify the Codex to Claude Code Bridge; safe stages run by default and sensitive stages require separate approval.
---

> Before asking, read [User Questions](../../rules/codex-user-questions.md).

# Set Up the Bridge

## Bootstrap and plan

- Treat a plain invocation as `action=all target=peer scope=auto live=prompt`.
- The loaded Codex plugin, Codex trust, Codex authentication, and current session are external bootstrap prerequisites. Never try to replace or restart the current invocation surface.
- Reject a model-controlled workspace override; use the host-selected launch workspace.
- Require `python --version` >= 3.10.
- Parse only `action=all|check|configure|authenticate|repair|verify-live`, `target=peer|codex|claude`, `scope=auto|user|project|local`, and `live=prompt|skip|required`, plus the one-release compatibility forms `--live` and `--direction codex|claude`; reject ambiguous or unknown arguments.

For one resolved target, invoke:

```bash
python "${PLUGIN_ROOT}/bin/bridge_setup.py" --current-host codex --workspace "<launch-workspace>" --action "<action>" --target "<target>" --scope "<scope>" --live "<live>"
```

The credential-free planner returns the setup result JSON defined by `schemas/setup-result.schema.json`.

> Route configuration/repair, authentication, and paid live-verification decisions separately through User Questions. Bind each answer to its own current action digest after displaying effects; a generic approval never carries to another stage. Preserve digest confirmation syntax, operator-terminal login, cost disclosure, and denial/replay/expiry stops. Host-side answers are not guest authorization or runtime permission.

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

> The host-held HMAC makes the digest tamper-evident but does not grant consent; explicit operator or host approval remains required. The digest is action-bound, expires, and is consumed by its first execution attempt. Never substitute `--approve` without its digest. Denial, changed or expired digest, replay, unsupported capability, failed probe, or failed operation stops without retry.

## Authentication and session evidence

When authentication remains:

1. Re-plan the provider-owned interactive login with the identical host, workspace, target, scope, and live values but `--action authenticate`.
2. Obtain separate approval for that action's digest and state that the exact `authentication_argv` may open a browser and use network and account state.
3. Give the operator the identical authenticate command plus `--approve "<authentication_digest>"` to run in their own terminal.

> This is the sensitive phase: never run it through a model-controlled shell or another captured tool stream. Bridge then launches only the native login command with that terminal inherited. Never accept, request, pipe, echo, inspect, or store a token, API key, browser code, device code, email, or raw login output. Process exit means `auth-flow-launched`; only a later redacted status probe may establish `host-authenticated`.

Use `bridge_status` from the loaded MCP inventory before claiming current session or workspace readiness. Its canonical workspace must equal the host-selected launch workspace and its expected tool inventory must match the current Bridge contract. The static planner cannot establish this evidence.

## Live verification

When `live=prompt` or `live=required` reaches `inference-unverified`:

1. Re-plan with the identical host, workspace, target, and scope but `--action verify-live`.
2. Obtain a third approval for that action's digest and one paid provider call.
3. State network, installed-CLI-managed credential, quota/cost, workspace, and point-in-time semantics.
4. Rerun that same verify-live command with `--approve "<live_digest>"`.

> Never invoke the lower-level live doctor outside this approval path. A successful live CLI result remains partial until applicable loaded-session/workspace evidence is also present. `live=skip` remains `inference-unverified`; never call it ready. Denial under `live=required` is non-ready.

## Completion boundary

- Setup always owns one resolved peer target.
- To prepare both integrations, finish the peer lifecycle from Codex, start any required fresh session, then run `/bridge:setup` from Claude for its peer; no digest, state claim, or readiness result is shared between hosts.
- The loaded-host branch is check/bootstrap-only and never mutates its current invocation surface.
- Report the strongest verified level, exact remaining action, confidence, and limits.

> Never equate static readiness, process exit, host authentication, session readiness, workspace readiness, or live verification.
