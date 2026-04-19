**Re: Compress markdown to caveman format**

Delegate only small, bounded tasks needing code read — not single-command tasks. Good fits:

- **Small coding**: 1–3 functions, self-contained, no arch decisions
- **Small tests**: 1–3 test cases for specific, well-specified function/behaviour
- **Complex linting**: ruff or mypy violations needing non-trivial code changes (not auto-fixable with `--fix`)
- **Typing/mypy resolution**: type annotation fixes needing function contract understanding

For each qualifying task, read target code, form accurate brief, spawn:

```
Agent(
  subagent_type="codex:codex-rescue",
  prompt="<specific task with accurate description of what the code does>. Target: <file>."
)
```

Plugin agent writes direct to working tree. Inspect via `git diff HEAD` after return. If plugin unavailable it reports gracefully — don't block.

**Don't delegate to Codex:**

- Task where precise description requires guessing
- Anything executable as single shell command (e.g. `ruff check --fix`, `pytest tests/foo.py`) — run direct
- Formatting-only changes (black, isort, trailing whitespace) handled by `pre-commit` — run `pre-commit` instead
