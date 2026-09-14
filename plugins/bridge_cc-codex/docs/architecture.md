# Architecture and transport

`bridge_CC-Codex` is a self-contained package with the normalized plugin identifier `bridge` and two host integrations. The Claude Code integration starts the Codex command-line interface through `bin/bridge_call.py`; the Codex integration exposes four tools through the stdio MCP server declared in `.mcp.json` and starts Claude Code through that server. Both integrations expose the same approval-bound setup lifecycle through their host skill.

## Request directions

| Direction | Entry point | Peer process | Transport requirement |
| -- | -- | -- | -- |
| Claude Code to Codex | `claude-skills/implement`, `claude-skills/advise`, `claude-skills/review` and `bin/bridge_call.py` | `codex exec` | Direct local command-line invocation; MCP is not required. |
| Codex to Claude Code | `codex-skills/implement`, `codex-skills/advise`, `codex-skills/review` and `bridge_mcp.py` | `claude --print` | The bridge MCP server is required for this direction. |
| Setup and verification | `claude-skills/setup`, `codex-skills/setup`, `bin/bridge_setup.py`, and (for session evidence) `bridge_status` | Native host CLI operations, provider-owned login, or one live diagnostic | Credential-free inspection is local; configuration, authentication, and live inference use distinct action-bound approvals. |

MCP is therefore complementary to the bridge as a whole, but mandatory for its Codex-to-Claude Code half. A one-way Claude Code-to-Codex installation can operate without MCP. A full bidirectional installation needs the MCP declaration so the Codex host can launch `bin/bridge_mcp.py` outside the model sandbox and use the Claude Code command-line interface through the host's normal authentication context.

The reverse transport is intentionally not implemented as a shell command issued by a sandboxed Codex model turn. The host-launched server owns the process boundary, validates JSON-RPC input, invokes `claude --print`, and returns one compact bridge envelope. The peer/model-to-harness result contains the six decision fields plus bounded verbose `details`; the harness persists `details` in the bounded transcript and strips them from the public envelope, which carries decisions, blockers, and remaining work in its decision-critical fields, along with observed metadata and workspace-relative transcript or incident references. Transcript-only `details` provide additional evidence and never hide required work. This keeps the supported authentication path and the transport boundary in one installed package while keeping routine host context small. Setup does not route through a model: its deterministic planner inspects the host surfaces, emits the setup-specific result, and executes only an exact configuration, authentication, or live-verification phase separately approved for that action and current state.

## MCP surface

The server implements the MCP methods required for startup and tool use: `initialize`, `notifications/initialized`, `tools/list`, and `tools/call`. `tools/list` advertises exactly four tools:

- `bridge_implement` performs one bounded write-capable request.
- `bridge_advise` performs one read-only request.
- `bridge_review` performs one read-only adversarial review.
- `bridge_status` returns sanitized server identity, Bridge/plugin and protocol/schema versions, the canonical host-selected workspace and fingerprint, and the expected tool inventory without invoking a peer, provider, write, repair, or authentication operation.

Every request tool requires a non-empty `task` and accepts optional `model`, `effort`, `timeout_seconds`, `depth`, `run_id`, and `supported_efforts`. The server supplies the trusted workspace selected by the host. `workspace`, `background`, and `session_id` are not accepted as model-controlled MCP arguments, so a tool call cannot select a different workspace or request detached execution through the reverse route. `bridge_status` is the exception to the request-tool argument contract: it accepts no workspace override and no model or task input. Its workspace evidence is session evidence only; static sync cannot claim that a host session has loaded the MCP server or selected the expected workspace.

## Setup lifecycle

The loaded host is the current host and `target=peer` resolves to the other integration. Current-host plugin installation, enablement, trust, authentication, and fresh-session activation are external bootstrap prerequisites. The default `action=all target=peer scope=auto live=prompt` path inspects the relevant surfaces, classifies state, proposes exact operations, obtains an expiring one-use action-bound approval, applies safe configuration or one closed repair, re-inspects host inventory, launches provider-owned authentication only through a separately planned action, and offers one separately approved live verification. One setup run owns one peer target; preparing both integrations requires one run from each loaded host with any fresh-session boundary honored between them.

The setup result is intentionally separate from the model bridge envelope. It reports requested/resolved target and scope, credential-free observed-state and operation fingerprints, state-changed/provider-call booleans, verification level, readiness, remaining manual work, rollback status, confidence, and limits. It never reuses model-run fields such as transcript, token count, cost, model verb, or model-authored decision fields. Approval payloads are authenticated with a host-held per-user HMAC key and tracked beneath the platform user-state root with a per-target/scope lock and regular non-symlink sanitized records so another workspace cannot forge, race, or replay them. The key establishes local payload integrity, not human consent; the host permission surface or operator still owns approval. Plan drift, expiry, or replay invalidates the approval before execution; a failed operation or repeated fault stops without a generic fallback or hidden retry.

Ordinary synchronization calls only the direct static doctor. Sync never invokes a setup skill through a model, passes an approval token, authenticates, repairs, restarts a host, or makes a provider call. This keeps setup's state-changing lifecycle out of unattended propagation.

## Process and result boundaries

`bin/bridge_call.py` is the shared supervisor for both directions. It selects the peer command, applies the verb's permission mode, supplies the structured result schema, enforces the soft-budget hard cutoff, validates the model-authored six-field core, adds observed harness metadata, and writes bridge artifacts under `.temp/bridge/` in the selected workspace. Review requests use a read-only general Codex execution with an explicit adversarial-review prompt and the same structured result contract; they do not depend on Codex's native review subcommand.

The public result is a JSON object with decision-critical `status`, `verdict`, `findings`, `files_touched`, `remaining`, and `blockers`, plus harness-owned metadata such as `model`, `effort`, `duration_seconds`, `depth`, `run_id`, `transcript_path`, `verb`, and `direction`. Decisions, blockers, and remaining work stay in the public envelope; peer `details` are transcript-only additional evidence and cannot hide required work. A fault's `incident` field points to a compact diagnostic record and is not the inline record; open its JSON path before inspecting `fault`. Complete Codex command events in the bounded transcript retain `aggregated_output_bytes` and `aggregated_output_sha256` over decoded output re-encoded as UTF-8 while preserving other event metadata; the digest cannot reconstruct output, and capture-byte savings do not establish provider token savings. Model output cannot claim process timing, cost, lifecycle, identity, or correlation metadata.

The recursion guard starts an outer request at `depth` zero, propagates the trusted value through `CC_CODEX_BRIDGE_DEPTH`, and refuses a peer dispatch at depth one or greater. A new `run_id` correlates records across the two hosts without treating a Codex thread identifier as a Claude session identifier.

## Permission model

`implement` uses `workspace-write` for Codex and `acceptEdits` for Claude Code. `advise` and `review` use read-only modes, and reverse read-only requests also disallow Claude tools named `Edit` and `Write`. Claude-side detached execution and Codex session continuation are available only through the Claude Code-to-Codex command-line route; the reverse MCP surface intentionally exposes neither capability.
