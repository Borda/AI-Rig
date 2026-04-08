---
description: Standards for .claude/hooks/*.js files
paths:
  - .claude/hooks/*.js
---

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
//   0  <success case>
//   2  <feedback case — Claude Code shows output and Claude acts on it>
```

Subsection order: `PURPOSE` → `HOW IT WORKS` → `EXIT CODES` (add others like `HOOK EVENT RESPONSIBILITIES` as needed). `HOW IT WORKS` may not be omitted even for simple hooks — use at least one numbered step.

## Exit Code Rules

<!-- Behavioral semantics for the exit codes declared in the file header template above. -->

- **Always exit 0 on unexpected errors** — hooks must never crash or block Claude due to a bug in the hook itself
- **Exit 2 to surface feedback** — Claude Code shows exit-2 output to Claude, which then acts on it
- **Exit 2 only when Claude caused the condition and can fix it** (e.g. a file it wrote failed linting). Use exit 0 for all environmental conditions: missing tools, missing config files, unexpected input formats.
- Exit 1 is not used; Claude Code maps it to exit 2 behavior (these hooks are not wired to git pre-commit)

## Implementation Pattern

- CommonJS: `require()` imports, stdin JSON parse, `process.exit()`
- **Only permitted stdin pattern** — use event-based accumulation; do not use
  `fs.readFileSync("/dev/stdin")` or any synchronous stdin read:
  ```js
  let raw = "";
  process.stdin.setEncoding("utf8");
  process.stdin.on("data", (d) => (raw += d));
  process.stdin.on("end", () => {
    const data = JSON.parse(raw);
    // ... handler logic
  });
  ```
- Wrap all logic in try/catch; catch → **always** `process.exit(0)` — hooks must never crash or block Claude; silent-swallow is acceptable for top-level catches (logging hooks must not interfere with Claude's execution)
- Use `execFileSync` or `spawnSync` (not `execSync` with shell strings) for subprocess calls — both take an args array, avoiding shell injection. Use `execFileSync` when the command MUST succeed (throws on non-zero exit, use in try/catch). Use `spawnSync` when you need to inspect the result code (returns `{status, stdout, stderr}`, does not throw).

## PreToolUse Decision Output

When a `PreToolUse` hook needs to approve or block a tool call, use `hookSpecificOutput` (current format):

```json
{
  "hookSpecificOutput": {
    "permissionDecision": "allow",
    "permissionDecisionReason": "optional explanation shown to user"
  }
}
```

- `permissionDecision`: `"allow"` or `"block"` — use `"block"` to prevent the tool call
- **Deprecated**: top-level `"decision"` and `"reason"` fields — these still work but may be removed in a future Claude Code release; check release notes for removal timeline; migrate to `hookSpecificOutput` <!-- verified: 2026-04-06; re-check quarterly against Claude Code CHANGELOG for removal -->
- Most hooks do not need to emit a decision at all — only emit when the hook is specifically acting as a gatekeeper (e.g. blocking destructive commands)

## PostToolUse and SubagentStop Hooks

Logging hooks (timing, file-writes, audit trails) need no output — exit 0 silently. Never emit to stdout from a logging hook; unexpected output can interfere with Claude's tool result handling.

- `PostToolUse` receives the tool result payload on stdin — use it for timing deltas, logging tool output size, or writing audit records
- `SubagentStop` fires when a spawned agent completes — use it to clean up per-agent state files (e.g. `/tmp/claude-state-<session>/agents/<id>.json`)
- Both hook types: wrap all logic in try/catch; catch → `process.exit(0)` always

## Assigned Agent

Hook files are JavaScript — editing or creating them is delegated to **`sw-engineer`** (not `self-mentor`). `self-mentor` handles `.md` config files; `sw-engineer` owns implementation code.

- `/manage update <hook-name> "change"` → dispatches to `sw-engineer`
- Direct edits during an audit fix → also use `sw-engineer`

## Anti-patterns

> **Prohibited**: `execSync` with a shell string — shell injection risk; takes a raw string parsed by `/bin/sh`.
> Use `execFileSync(cmd, argsArray)` or `spawnSync(cmd, argsArray)` instead (both take an args array, no shell involved).
