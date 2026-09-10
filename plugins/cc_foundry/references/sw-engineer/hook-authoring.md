<!-- Loaded by foundry:sw-engineer (opus + xhigh) -->

# Hook Authoring (foundry:sw-engineer specialized guidance)

Read only for hook code: JavaScript under `.claude/hooks/`, hook registrations in `settings.json`, or `PostToolUse`/`PreToolUse`/`SubagentStop` handlers. Skip Python implementation tasks.

`foundry:sw-engineer` exclusively owns hook authoring and editing; the curator NOT-for boundary prohibits curator edits to hook files. `foundry:curator` reviews only hook-adjacent Markdown config. For any hook creation or modification, `foundry:sw-engineer` owns the work end-to-end.

## File Header Structure

Every hook file must start with:

```js
#!/usr/bin/env node
// <filename>.js — <HookType> hook  ← the word `hook` is literal, not a placeholder
//
// PURPOSE
//   <one-paragraph description of what this hook does and why>
//
// HOW IT WORKS
//   1. <step>
//   2. <step>
//   ...
//
// EXIT CODES
//   0  <success case — stdout is read for structured control>
//   1  <error case — logged as a non-blocking error; execution proceeds>
//   2  <blocking case — on an event that can block, blocks and shows output to Claude>
```

Subsection order: `PURPOSE` → `HOW IT WORKS` → `EXIT CODES` (add others like `HOOK EVENT RESPONSIBILITIES` as needed). `HOW IT WORKS` may not be omitted even for simple hooks — use at least one numbered step.

### Minimal exit-code template (always pair success + error paths)

```bash
# success path
exit 0

# error path — non-blocking; the message is logged, execution continues
echo "Error: <message>" >&2
exit 1
```

Every hook must explicitly handle its error path — never leave it implicit.

## Exit Code Rules

**Exit 1 never blocks.** It is a non-blocking error on every event, `PreToolUse` included: Claude Code logs it and proceeds with the action. A gate that exits 1 on failure has failed open. **Exit 2 is the only exit code that blocks through the code alone**, and only on the events that support blocking.

| Exit | Meaning |
| -- | -- |
| **0** | Success. Claude Code reads stdout for structured control (see [PreToolUse Decision Output](#pretooluse-decision-output)); a parsed object that passes schema validation takes effect. |
| **1** | Non-blocking error. Logged; the action proceeds. |
| **2** | Blocking error on an event that can block — **regardless of any JSON on stdout**, so a `permissionDecision` of `"allow"` cannot override it. On an event that cannot block, it is a non-blocking error. |

Events where exit 2 blocks, and what it blocks: `PreToolUse` (the tool call), `UserPromptSubmit` (prompt processing; the prompt is erased), `UserPromptExpansion` (the expansion), `Stop` (Claude stopping), `SubagentStop` (the subagent stopping), `TeammateIdle` (the teammate going idle), `TaskCreated` (rolls the creation back), `TaskCompleted` (the completion), `ConfigChange` (the change taking effect). `PermissionRequest` does not honour exit 2 — use its `decision` object. `PostToolUse`, `SubagentStart` and `PreCompact` do not block.

- **Gatekeeper hooks that can emit `deny`**: exit 2 to block; exit 0 to let the call proceed to normal permission handling.
- **Allow-only hooks** (they can emit `allow` and nothing else) and **logging hooks**: exit 0 on every path, including unexpected errors — see [Error handling by hook class](#implementation-pattern).
- **Exit 2 only when Claude caused the condition and can fix it** (e.g. a file it wrote failed linting). Use exit 0 for environmental conditions: missing tools, missing config files, unexpected input formats.
- **A timed-out hook has its output discarded** and does not block; the tool call continues through the normal permission flow. Never rely on a stalled hook to act as a gate.

## Implementation Pattern

- CommonJS: `require()` imports, stdin JSON parse, `process.exit()`
- **Only permitted stdin pattern** — use event-based accumulation; do not use `fs.readFileSync("/dev/stdin")` or any synchronous stdin read:
  ```js
  let raw = "";
  process.stdin.setEncoding("utf8");
  process.stdin.on("data", (d) => (raw += d));
  process.stdin.on("end", () => {
      const data = JSON.parse(raw);
      // ... handler logic
  });
  ```
- Wrap all logic in try/catch; catch behaviour depends on what the hook is able to decide:
  - **PreToolUse gatekeepers that can emit `deny`**: catch → `process.exit(2)` — an erroring gatekeeper must **block**, not allow, and exit 2 is the only code that does. Letting a tool call through when the gate logic crashed is a security bypass.
  - **Allow-only PreToolUse hooks** — those whose sole possible output is `permissionDecision: "allow"`: catch → `process.exit(0)`. A crashed allow-only hook grants nothing, so the call falls back to normal permission handling, which is the safe direction; exiting 2 would block a call the hook was never empowered to refuse. Establish this at review time from the source, not from intent: if any code path can emit `deny`, the hook is a gatekeeper.
  - **Logging hooks** (PostToolUse, SubagentStop, observational PreToolUse without decision output): catch → `process.exit(0)` — silent-swallow acceptable; logging hooks must not interfere with Claude's execution.
- Use `execFileSync` or `spawnSync` (not `execSync` with shell strings) for subprocess calls — both take args array, avoiding shell injection. Use `execFileSync` when command MUST succeed (throws on non-zero exit, use in try/catch). Use `spawnSync` when need to inspect result code (returns `{status, stdout, stderr}`, does not throw). **Always pass `{ maxBuffer: 10 * 1024 * 1024 }` (10 MB) to `execFileSync`** — default buffer is 1 MB; subprocess producing unbounded output (e.g. large lint run) will hang or crash Claude Code session without this cap.

## PreToolUse Decision Output

When `PreToolUse` hook needs to approve or block tool call, use `hookSpecificOutput` (current format):

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "allow",
    "permissionDecisionReason": "optional explanation shown to user"
  }
}
```

- `permissionDecision` — `"deny"` blocks the tool call, `"allow"` lets it run without a prompt. There is no `"block"`; that value is not part of the schema and is silently ineffective.
- `"ask"` forces a confirmation prompt instead of deciding. `write-guard.js` ships it, so it is a working value here, but the published reference documents only `allow` and `deny` — treat `ask` as supported-in-practice and verify it still prompts when a hook depends on it.
- `hookEventName` belongs in the object; omitting it is a schema risk, not a shorthand.
- **Deprecated**: top-level `"decision"` and `"reason"` fields — still work but may be removed in a future Claude Code release; migrate to `hookSpecificOutput`.
- Most hooks need no decision output — emit only when the hook acts as a gate.
- **All matching hooks for one event run in parallel**; completion order is not registration order, and no published rule states how conflicting decisions from several hooks are prioritised. Never write a hook whose correctness depends on running before or after another.

<!-- Verified against the published hooks reference and Claude Code 2.1.236 on 2026-09-09. -->

<!-- Re-check on a host version bump: exit-code table, blocking-event list, permissionDecision vocabulary, timeout behaviour. -->

<!-- The reference does not document `defer`; do not describe it here until it does. -->

## PostToolUse and SubagentStop Hooks

Logging hooks (timing, file writes, audit trails) emit no output and exit 0 silently. Never write to stdout; it can interfere with Claude's tool-result handling.

- `PostToolUse` receives tool result payload on stdin — use for timing deltas, logging tool output size, or writing audit records
- `SubagentStop` fires when a spawned agent completes — use it to clean up per-agent state files, e.g. `<state dir>/agents/<id>.json`. **`claude-state-<session-id>` is a convention this rig's own hooks create, not a path the harness manages or guarantees**: `task-log.js` derives its base itself, and any hook reading or writing there is depending on a sibling hook having created it. Derive the base the same way rather than hardcoding one, treat a missing directory as normal, and never assume another hook's file is present. <!-- tmpdir-exempt: names the rig's own convention, not a plugin-authored literal -->
- Both hook types: wrap all logic in try/catch; catch → `process.exit(0)` always

## Anti-patterns

- **Prohibited**: `execSync` with shell string — shell injection risk; takes raw string parsed by `/bin/sh`. Use `execFileSync(cmd, argsArray)` or `spawnSync(cmd, argsArray)` instead.
