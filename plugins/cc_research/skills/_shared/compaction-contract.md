<!-- file: compaction-contract.md — consumers: foundry:{audit,brainstorm,calibrate,distill,investigate}, oss:{analyse,resolve,review}, develop:{debug,feature,fix,refactor,review}, research:{fortify,judge,kaggle,run,sweep,topic,verify} -->

# Compaction Contract Protocol

## What

`.temp/state/skill-contract.md` — terse block skill (re)writes at expanding-phase boundaries. PreCompact hook (`task-log.js`) appends it **verbatim** under `## Skill Compaction Contract` in `session-context.md`. Post-compaction re-read restores losslessly.

## Block format (emit this template exactly)

```markdown
## Active Skill Contract
- skill: <plugin:skill> · phase: <name> (after <expanding phase>)
- run-dir: <path or n/a>
- preserve: <task IDs, key decisions, report/artifact paths, file list next phase needs>
- next: <what next phase does with the above>
```

## Placement rule

Refresh at boundary **after** expanding phase (parallel fan-out / iteration loop / large gather), **before** next phase begins.

- Only at phase boundaries — not every step
- `preserve:` = only what next phase consumes; drop raw expanded material
- `--keep "<items>"` at invocation → append user string to `preserve:` at Step 0

## Acceptance criteria

- `preserve:` names concrete inputs (paths, IDs, report filenames) — not vague prose
- Block ≤ ~12 lines

## Lifecycle

| Step | Action |
| -- | -- |
| Boundary reached | Write tool → `.temp/state/skill-contract.md` |
| Auto-compact fires | PreCompact hook appends block verbatim to `session-context.md` |
| Post-compaction | Re-read `session-context.md` — contract section restored into context |
| Skill completes | Delete `.temp/state/skill-contract.md` — prevents stale leak into later compactions |
