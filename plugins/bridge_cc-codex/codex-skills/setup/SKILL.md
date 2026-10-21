---
name: setup
description: Configure, authenticate, repair, and verify the Codex to Claude Code Bridge; safe stages run by default and sensitive stages require separate approval.
---

> Before asking, read [User Questions](../../rules/codex-user-questions.md).

# Set Up the Bridge

## Bootstrap and plan

- Treat a plain invocation as `action=all target=peer scope=auto live=prompt`.
- Loaded Codex plugin, Codex trust, Codex authentication, and current session are external bootstrap prerequisites. Never replace or restart current invocation surface.
- Reject a model-controlled workspace override. The deterministic setup CLI uses its trusted host-selected workspace; installed MCP execution separately requires native user-confirmed binding and a current `binding_id`.
- Resolve the active installed plugin root and substitute it for `PLUGIN_ROOT`. Setup helpers use the packaged launcher; on native Windows use `bin/python.cmd` with host-shell quoting. MCP startup still requires Python 3.10+ as `python` on the host's PATH; the helper launcher does not satisfy or change that separate prerequisite.
- Parse only `action=all|check|configure|authenticate|repair|verify-live`, `target=peer|codex|claude`, `scope=auto|user|project|local`, `live=prompt|skip|required`, and the optional `--approve-claude-consent` flag, plus one-release compatibility forms `--live` and `--direction codex|claude`; reject ambiguous or unknown arguments.
- Accept `--approve-claude-consent` only when explicitly supplied in the current user invocation with `action=all|configure|repair`. Reject it with `check`, `authenticate`, or `verify-live` before any setup operation. Consume this skill-only argument here; do not pass it to `bridge_setup.py` or infer it from generic setup approval. It may be accompanied by `--consent-token <token>`, the one-time token the operator minted in their own terminal (see Provider consent); reject the token without the flag and never echo it.

For one resolved target, invoke:

```bash
"PLUGIN_ROOT/bin/python" "PLUGIN_ROOT/bin/bridge_setup.py" --current-host codex --workspace "<launch-workspace>" --action "<action>" --target "<target>" --scope "<scope>" --live "<live>"
```

Credential-free planner returns setup result JSON defined by `schemas/setup-result.schema.json`.

> Route configuration/repair, authentication, and paid live-verification decisions separately through User Questions. Bind each answer to its own current action digest after displaying effects; generic approval never carries to another stage. Preserve digest confirmation syntax, operator-terminal login, cost disclosure, and denial/replay/expiry stops. Host-side answers are not guest authorization or runtime permission.

## Check and approved changes

For `check`, also run the read-only Provider consent check below, report both results and stop. For `all`, `configure`, or `repair`:

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
3. After approval, give the operator the identical command plus `--approve "<approval_digest>"` to run in their own terminal. Then re-plan with `--action check` to report the outcome.

> Host-held HMAC makes digest tamper-evident but does not grant consent; explicit operator or host approval remains required. The digest is action-bound, expires, and is consumed by its first execution attempt; it lives 300 seconds. Never substitute `--approve` without its digest. Denial, changed or expired digest, replay, unsupported capability, failed probe, or failed operation stops without retry.

> Operator proof: `--approve` executes only when stdin and stderr are the operator's terminal; otherwise the helper exits 2 with `operator-terminal-required` before inspecting, locking or mutating anything. Never run `--approve` through a model-controlled shell, a pipe, or an allocated TTY; run only the planning command (no `--approve`) there. The digest the model planned is not operator approval.

## Provider consent

Read and apply [Claude provider consent](../../rules/provider-consent.md). Check standing transfer consent during setup, including when no install/repair operation remains; `action=check` stays read-only. Installation does not approve transfer. For an absent grant, an explicit user-supplied `--approve-claude-consent` authorizes the packaged helper's `grant --approve` path without another consent question, but only together with the user-supplied one-time `--consent-token <token>`, which the skill passes to the helper solely as `BRIDGE_OPERATOR_TOKEN`; without that token, `grant --approve` is refused outside the operator terminal; otherwise use the operator-terminal approval described there. Follow either grant with a read-only recheck. Never synthesize consent, edit the policy directly, or use the flag to bypass a host denial. Report stored consent separately from active host effectiveness. This stage does not replace the current invocation surface or alter its permission mode.

## Authentication and session evidence

When authentication remains:

1. Re-plan the provider-owned interactive login with identical host, workspace, target, scope, and live values but `--action authenticate`.
2. Obtain separate approval for that action's digest and state exact `authentication_argv` may open a browser and use network and account state.
3. Give operator identical authenticate command plus `--approve "<authentication_digest>"` to run in their own terminal.

> Sensitive phase: never run through a model-controlled shell or another captured tool stream. Bridge then launches only native login command with terminal inherited. Never accept, request, pipe, echo, inspect, or store a token, API key, browser code, device code, email, or raw login output. Process exit means `auth-flow-launched`; only a later redacted status probe may establish `host-authenticated`.

Use `bridge_status` from loaded MCP inventory before claiming current session or workspace readiness. `binding_status=unbound` means the installed server is loaded but no project is selected; never call that a stale installation or a workspace mismatch. Invoke `bridge_bind_workspace` with the known absolute project folder as untrusted `proposed_workspace`; require exact canonical native confirmation. Empty arguments retain manual selection and confirmation. Require `binding_status=bound`, a nonempty current `binding_id`, and the intended canonical workspace before executable calls; pass that ID with each `task`. Expected inventory includes `bridge_bind_workspace` plus status and the three executable tools. Rebinding invalidates old IDs immediately, including cancelled rebinds. Binding selects project identity; it grants no editing approval, runtime permission, authentication, or paid-call consent. Static planner cannot establish this evidence.

## Live verification

When `live=prompt` or `live=required` reaches `inference-unverified`:

1. Precondition: a fresh plan with identical host, workspace, target, scope, and live values but `--action all` must show an empty `operations` list and `remaining` listing neither `configuration` nor `authentication`. Otherwise the approved call would be refused (`configuration-needed` or `authentication-needed`, exit 2) after the user already paid attention to it: report the missing stage and stop. A configuration result classified `fresh-session-required` needs a fresh session first.
2. Re-plan with identical host, workspace, target, and scope but `--action verify-live`. Exit 2 with `status=manual` and `classification=live-verification-requires-separate-approval` is this plan's normal result, not a failure; its `approval_digest` is the live digest.
3. Obtain a third approval for that action's digest and one paid provider call.
4. State network, installed-CLI-managed credential, quota/cost, workspace, and point-in-time semantics.
5. Give the operator that same verify-live command plus `--approve "<live_digest>"` to run in their own terminal.

> Never invoke the lower-level live doctor outside this approval path. Successful live CLI result remains partial until applicable loaded-session/workspace evidence also present. `live=skip` remains `inference-unverified`; never call it ready. Denial under `live=required` is non-ready.

## Completion boundary

- Setup always owns one resolved peer target.
- To prepare both integrations, finish peer lifecycle from Codex, start any required fresh session, then run `/bridge:setup` from Claude for its peer; no digest, state claim, or readiness result is shared between hosts.
- Loaded-host branch is check/bootstrap-only, never mutates its current invocation surface.
- Report strongest verified level, exact remaining action, confidence, and limits.

> Never equate static readiness, process exit, host authentication, session readiness, workspace readiness, or live verification.
