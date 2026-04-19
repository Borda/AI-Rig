**Re: Compress worktree protocol markdown to caveman format**

# Worktree Protocol

Conventions for skills/agents running commands inside git worktree.

## Bash command pattern — two calls, not one

Claude Code permission matcher checks **first token** of Bash command. Compound command like:

```bash
# BAD — first token is "cd"; "uv run:*" permission never fires
cd /path/to/worktree && uv run python -c "..."
```

…not match `Bash(uv run:*)` even if pattern in allowlist. Causes unexpected permission prompt.

Use **two separate Bash calls**. Shell CWD persists between calls:

```bash
# Call 1 — sets CWD; matches Bash(cd:*)
cd /path/to/worktree

# Call 2 — first token is now the real command; matches its own pattern
uv run python -c "..."
```

Applies to every command run "in" worktree from lead's context: `uv run`, `python`, `pytest`, `git`, etc.

## Running commands from inside a worktree agent

Cleanest alternative: spawn agent with `isolation: "worktree"`. Agent CWD = worktree root. All Bash calls use clean first-token patterns, no `cd` prefix needed.

```
Agent(subagent_type="foundry:sw-engineer", isolation="worktree", prompt="...")
```

Reserve `cd /worktree && cmd` (split across two calls) for lead running quick one-off check without spawning full agent.

## Settings in worktrees

Worktrees via `isolation: "worktree"` land under `.claude/worktrees/<id>/`. Worktree has full project checkout (including `.claude/`), so Claude Code finds `settings.local.json` at `worktree/.claude/settings.local.json`. **Snapshot from worktree-creation time** — permissions added to main project after worktree created not reflected. Worktree agent hits unexpected permission prompts → check if main project's `settings.local.json` updated since worktree created.
