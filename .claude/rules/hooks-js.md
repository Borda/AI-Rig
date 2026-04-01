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

- **Always exit 0 on unexpected errors** — hooks must never crash or block Claude due to a bug in the hook itself
- **Exit 2 to surface feedback** — Claude Code shows exit-2 output to Claude, which then acts on it
- **Exit 2 only when Claude caused the condition and can fix it** (e.g. a file it wrote failed linting). Use exit 0 for all environmental conditions: missing tools, missing config files, unexpected input formats.
- Exit 1 is not used; treat it the same as exit 2 for pre-commit compatibility

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
- **Deprecated**: top-level `"decision"` and `"reason"` fields — these still work but will be removed; migrate to `hookSpecificOutput`
- Most hooks do not need to emit a decision at all — only emit when the hook is specifically acting as a gatekeeper (e.g. blocking destructive commands)
