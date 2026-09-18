Delegate only small, bounded tasks needing code read — not single-command tasks. Good fits:

- **Small coding**: 1–3 functions, self-contained, no arch decisions
- **Small tests**: 1–3 test cases for specific, well-specified function/behaviour
- **Complex linting**: ruff or mypy violations needing non-trivial code changes (not auto-fixable with `--fix`)
- **Typing/mypy resolution**: type annotation fixes needing function contract understanding

For each qualifying task: read target code, form accurate self-contained brief, then use the installed bridge (requires `bridge@borda-ai-rig`):

```text
Skill(
  skill="bridge:implement",
  args="Implement this bounded change after reading <file>: <specific task with an accurate description of the current behavior and required outcome>. Verify the focused behavior before returning."
)
```

Bridge implementation writes direct to the working tree. Inspect via `git diff HEAD` after return. Bridge absent or disabled → report status, continue without a legacy fallback.

**Don't delegate to Codex:**

- Task where precise description requires guessing
- Anything executable as single shell command (e.g. `ruff check --fix`, `pytest tests/foo.py`) — run direct
- Formatting-only changes (black, isort, trailing whitespace) handled by `pre-commit` — run `pre-commit` instead
