For each task, read the target code, form an accurate brief, then spawn:

```
Task(
  subagent_type="general-purpose",
  prompt="Read .claude/skills/codex/SKILL.md and follow its workflow exactly.
Task: use the <agent> to <specific task with accurate description of what the code does>.
Target: <file>."
)
```

The subagent handles pre-flight, dispatch, validation, and patch capture. If Codex is unavailable it reports gracefully — do not block on this step.
