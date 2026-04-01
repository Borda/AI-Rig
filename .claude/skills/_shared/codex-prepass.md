Before cycle 1 of the review loop, run a Codex pre-pass if the diff is meaningful:

```bash
claude plugin list 2>/dev/null | grep -q 'codex@openai-codex' || echo "codex (openai-codex) not installed — skipping pre-pass"
git diff HEAD --stat
```

**Skip** this step if:

- `codex@openai-codex` plugin is not installed
- `git diff HEAD --stat` shows only 1–3 lines changed, or changes are formatting, comments, whitespace, or variable renames only

**Run** when changes include new logic, functions, conditionals, error paths, or restructured code:

```
Agent(subagent_type="codex:codex-rescue", prompt="Review the current working-tree changes for bugs, missed edge cases, and inconsistencies. Read-only: do not apply fixes.")
```

Treat any Codex findings as pre-flagged issues entering cycle 1. If Codex found nothing or was skipped, start cycle 1 from scratch.
