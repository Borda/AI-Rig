---
name: agent-shims
description: 'Safely manage Codex Rig role-agent shims: doctor, status, install, or remove; one action only.'
---

# Agent Shims

After user intervention or repeated invocation, follow [Resume And Re-entry](../../shared/helper-cli-contract.md#resume-and-re-entry): complete selected action's verification and diagnostic instructions below. This manager keeps its non-artifact lifecycle; it does not skip its closing checks or replace final explanation with raw JSON.

This is experimental lifecycle tool. Installing authenticated standalone agent TOML does not prove that active collaboration interface can select that custom profile. A task name or child path matching role name is not proof. Use installed shims only when runtime exposes explicit custom-agent selector and observed child metadata plus verifier output prove selection; otherwise use blank-agent role-card injection.

Accept exactly one action: `doctor`, `status`, `install`, or `remove`. Reject missing, extra, or unknown arguments without writes.

Any follow-up question follows [User Questions](../../shared/native-skill-contract.md#user-questions): show accepted action names or required input format. Preserve the manager's exact-digest removal confirmation syntax; never substitute a generic yes/no response for that digest-bound input. The blocked `install` action still requires no approval prompt.

Locate `../../scripts/manage_role_agents.py` relative to this installed `SKILL.md`. Run it with current Python 3.10+ interpreter and selected action. Do not copy manager, resolve it through source checkout, or edit generated agent files directly.

- `doctor`: run read-only live prerequisite check and render its JSON result for person.
- `status`: run read-only health and installed-roster summary and render its JSON result for person.
- `install`: report stable platform block. Do not request approval; no new shim plan or write is allowed.
- `remove`: use same exact-digest flow to remove every intact managed shim. Never delete by filename prefix or marker alone.

Preserve manager's exit contract: `0` success/converged, `2` usage, `3` cancelled, `4` drift/conflict, `5` prerequisite blocked, `6` untrusted state, `7` internal/recovery failure. Do not retry mutating action after codes `4`, `6`, or `7`; report evidence and keep files untouched.

For `doctor` and `status`, do not return only raw JSON or generic safety label:

1. Start with plain-English explanation of whether shims are usable and what needs attention. Then give `Healthy`, `Degraded`, or `Blocked`, followed by first non-pass check and its exact detail; never expose raw diagnostic JSON as whole answer.
2. List every other non-pass check once, then summarize `state`, `targets`, `recovery`, and namespace candidates.
3. State `No files changed.`
4. Give narrow safe next step. For package or active-package failures, refresh or reinstall Codex Rig and start fresh session. For executable failures, report selected path and rerun from fresh session with stable Python and Codex selection. For permission, owner, type, or link failures, inspect only named path and verify its metadata before changing anything. For corrupt, inconsistent, modified, or foreign evidence, back it up and do not adopt, edit, or delete it automatically. For recognized recovery residue, use `remove` and review its authenticated approval digest.

Never recommend recursive `chmod`, `chown`, deletion, or link replacement from diagnostic alone. A protected agent target may be readable by other users, but it must be owned by current user and have no group/world write or special permission bits. Private lifecycle state remains exact mode `0700`.

After successful install, update, or removal, tell user to start fresh Codex session. Thin shims intentionally depend on installed plugin cache; uninstalling plugin makes remaining shims unavailable until safely removed or reinstalled.

## Output Contract

This manager has no canonical `.reports` result lifecycle and therefore remains explicit exception in `../../shared/final-handoff-contract.md`: its final-response structure is advisory, not executable. Do not claim digest-bound final-response validation until manager gains canonical run/result artifact.

Final chat follows ordered frame from `../../shared/quality-gates.md`:

- `Outcome` includes action, `Healthy|Degraded|Blocked|Removed|Rejected`, exit code, and whether files changed.
- `Results` has one action per row and exactly `Action | Outcome | Verification | Remaining limit`; `doctor` and `status` include every non-pass check once.
- Apply shared `Verification`, `Remaining`, `Next steps`, and `Confidence`, retaining exact-digest/read-only evidence, safe recovery only, and no inferred custom-profile selection. `Artifact` links retained manager JSON or states `None — this lifecycle helper created no durable result artifact`. Raw JSON alone never substitutes for the outcome.

Keep usage rejection and exit codes `2|3|4|5|6|7` explicit. Do not present blocked install, cancelled remove, drift/conflict, untrusted state, or recovery failure as success.
