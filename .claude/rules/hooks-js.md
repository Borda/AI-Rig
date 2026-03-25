---
description: Standards for .claude/hooks/*.js files
paths:
  - .claude/hooks/*.js
---

## File Header Structure

Every hook file must start with:

```js
#!/usr/bin/env node
// <filename>.js — <HookType> hook
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

Subsection order: `PURPOSE` → `HOW IT WORKS` → `EXIT CODES` (add others like `HOOK EVENT RESPONSIBILITIES` as needed).

## Exit Code Rules

- **Always exit 0 on unexpected errors** — hooks must never crash or block Claude due to a bug in the hook itself
- **Exit 2 to surface feedback** — Claude Code shows exit-2 output to Claude, which then acts on it
- **Exit 2 only when Claude caused the condition and can fix it** (e.g. a file it wrote failed linting). Use exit 0 for all environmental conditions: missing tools, missing config files, unexpected input formats.
- Exit 1 is not used; treat it the same as exit 2 for pre-commit compatibility

## Implementation Pattern

- CommonJS: `require()` imports, stdin JSON parse, `process.exit()`
- Parse stdin: `JSON.parse(require('fs').readFileSync('/dev/stdin', 'utf8'))`
- Wrap all logic in try/catch; catch → **always** write `process.stderr.write(String(e))` then `process.exit(0)` — never crash, never swallow silently
- Use `execFileSync` (not `execSync` with shell strings) for subprocess calls — avoids shell injection
