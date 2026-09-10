---
name: agent-shims
description: 'Safely manage Codex Rig role-agent shims: doctor, status, install, or remove; one action only.'
---

# Agent Shims

After user intervention or a repeated invocation, follow [Resume And Re-entry](../../shared/helper-cli-contract.md#resume-and-re-entry): complete the selected action's verification and the diagnostic instructions below. This manager keeps its non-artifact lifecycle; it does not skip its closing checks or replace the final explanation with raw JSON.

This is an experimental lifecycle tool. Installing authenticated standalone agent TOML does not prove that the active collaboration interface can select that custom profile. A task name or child path matching the role name is not proof. Use installed shims only when the runtime exposes an explicit custom-agent selector and observed child metadata plus verifier output prove selection; otherwise use blank-agent role-card injection.

Accept exactly one action: `doctor`, `status`, `install`, or `remove`. Reject missing, extra, or unknown arguments without writes.

Locate `../../scripts/manage_role_agents.py` relative to this installed `SKILL.md`. Run it with the current Python 3.10+ interpreter and the selected action. Do not copy the manager, resolve it through a source checkout, or edit generated agent files directly.

- `doctor`: run the read-only live prerequisite check and render its JSON result for a person.
- `status`: run the read-only health and installed-roster summary and render its JSON result for a person.
- `install`: report the stable platform block. Do not request approval; no new shim plan or write is allowed.
- `remove`: use the same exact-digest flow to remove every intact managed shim. Never delete by filename prefix or marker alone.

Preserve the manager's exit contract: `0` success/converged, `2` usage, `3` cancelled, `4` drift/conflict, `5` prerequisite blocked, `6` untrusted state, `7` internal/recovery failure. Do not retry a mutating action after codes `4`, `6`, or `7`; report the evidence and keep files untouched.

For `doctor` and `status`, do not return only the raw JSON or a generic safety label:

1. Start with a plain-English explanation of whether the shims are usable and what needs attention. Then give `Healthy`, `Degraded`, or `Blocked`, followed by the first non-pass check and its exact detail; never expose raw diagnostic JSON as the whole answer.
2. List every other non-pass check once, then summarize `state`, `targets`, `recovery`, and namespace candidates.
3. State `No files changed.`
4. Give the narrow safe next step. For package or active-package failures, refresh or reinstall Codex Rig and start a fresh session. For executable failures, report the selected path and rerun from a fresh session with stable Python and Codex selection. For permission, owner, type, or link failures, inspect only the named path and verify its metadata before changing anything. For corrupt, inconsistent, modified, or foreign evidence, back it up and do not adopt, edit, or delete it automatically. For recognized recovery residue, use `remove` and review its authenticated approval digest.

Never recommend recursive `chmod`, `chown`, deletion, or link replacement from a diagnostic alone. A protected agent target may be readable by other users, but it must be owned by the current user and have no group/world write or special permission bits. Private lifecycle state remains exact mode `0700`.

After successful install, update, or removal, tell the user to start a fresh Codex session. Thin shims intentionally depend on the installed plugin cache; uninstalling the plugin makes remaining shims unavailable until safely removed or reinstalled.

## Output Contract

This manager has no canonical `.reports` result lifecycle and therefore remains the explicit exception in `../../shared/final-handoff-contract.md`: its final-response structure is advisory, not executable. Do not claim digest-bound final-response validation until the manager gains a canonical run/result artifact.

Final chat follows the ordered frame from `../../shared/quality-gates.md`:

- `Outcome` includes the action, `Healthy|Degraded|Blocked|Removed|Rejected`, exit code, and whether files changed.
- `Results` has one action per row and exactly `Action | Outcome | Verification | Remaining limit`; `doctor` and `status` include every non-pass check once.
- Apply shared `Verification`, `Remaining`, `Next steps`, and `Confidence`, retaining exact-digest/read-only evidence, safe recovery only, and no inferred custom-profile selection. `Artifact` links retained manager JSON or states `None — this lifecycle helper created no durable result artifact`. Raw JSON alone never substitutes for the outcome.

Keep usage rejection and exit codes `2|3|4|5|6|7` explicit. Do not present a blocked install, cancelled remove, drift/conflict, untrusted state, or recovery failure as success.
