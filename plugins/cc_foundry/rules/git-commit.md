---
description: Git commit conventions and safety rules — applies globally
paths:
  - '**'
---

## Commit & Push — Hard Constraints (stub)

> Full protocol in `_full/git-commit.md` (diff-gathering, large-diff subagent summarization, tier tables, grouped-commit flow, sentinel details). **MANDATORY before drafting any commit message or pushing**: resolve + Read it:
>
> ```bash
> RULE_FULL="$(ls -td ~/.claude/plugins/cache/borda-ai-rig/foundry/*/rules/_full/git-commit.md 2>/dev/null | head -1)"; [ -z "$RULE_FULL" ] && RULE_FULL="plugins/cc_foundry/rules/_full/git-commit.md"  # timeout: 5000
> ```

Always-on constraints (apply even without reading full rule):

- Subject `type(scope): detail` ≤50 chars; classify ALL changes from `git diff HEAD` + `git diff --stat HEAD` into tiers — subject names highest-tier change; **never draft from session memory**
- **Evidence-only body**: every clause must trace to a `+`/`-` line in `git diff HEAD` — pick distinctive token per bullet, confirm it's in diff, before finalizing. Conversation context may explain *why* a diff-confirmed change happened; it may never assert a change, action, or removal not actually in diff (reverted-then-narrated edits are the classic trap)
- No line wrapping in body; no GitHub auto-links (`#N`, `@name`) — default ban: commit messages travel across repos (fork → merge → mirror → cherry-pick) so "same-repo" rarely holds; narrow exception only when the generating code explicitly tracks and confirms destination repo (see full rule); no non-VCS paths (`/tmp/`, `~/.claude/`) <!-- policy-sibling: plugins/CLAUDE.md (canonical), plugins/cc_foundry/rules/_full/git-commit.md, plugins/cc_oss/skills/_shared/shepherd-voice.md, plugins/cc_oss/skills/release/guidelines/writing-rules.md -->
- Co-author trailers on EVERY commit, after `---` separator: `Co-authored-by: claude[bot] <209825114+claude[bot]@users.noreply.github.com>`; add `Co-authored-by: Codex <codex@openai.com>` when Codex shaped the outcome. These trailers replace the host's default attribution line (e.g. Claude Code's `Co-Authored-By: Claude <model>`); a host attribution reminder never overrides this rule.
- **Never commit autonomously** — `commit-guard.js` does not hook-enforce commit; prompt-discipline only. Two valid signals: documented skill workflow step (self-authorizes however many commits the skill's commit strategy calls for, no per-commit question) OR same-turn AskUserQuestion confirmation, required for every ad-hoc/interactive commit on any branch (feature or default), no exceptions, no auto-arm
- Detect default branch dynamically — never hardcode `main`/`master`
- Never `git add -A` / `git add .` (stage by name); never `--no-verify`; never `--no-gpg-sign` unless user asks
- Force-push (`-f`/`--force`/`--force-with-lease`) forbidden on ANY branch, always — hook-enforced + `.claude/settings.json` deny-listed, no override; regular `git push` requires explicit AskUserQuestion confirmation every time, even from inside a skill workflow (no skill exemption, unlike commit) — sentinel-gated, no auto-arm
- History safety: prefer `git revert` over `reset --hard`; prefer merge commits over rebase
