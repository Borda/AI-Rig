---
description: Git commit conventions and safety rules — applies globally
paths:
  - '**'
---

## Commit Message Format

Subject line format: `type(scope): detail` — ≤50 chars total; name up to 3 most significant changes only.

**type** — pick lowest that fits:

| type | when |
| -- | -- |
| `fix` | Bug fix, correctness repair |
| `feat` | New user-visible capability |
| `refactor` | Internal restructure, no behaviour change |
| `perf` | Performance improvement |
| `test` | Test-only changes |
| `docs` | Documentation only |
| `ci` | CI/CD pipeline |
| `chore` | Tooling, config, deps, build |
| `refine` | Improvement to existing behaviour (not pure fix, not new feature) |
| `compress` | Compression / caveman reformatting pass |

**scope** — affected area: `plugins`, `oss`, `foundry`, `docs`, `cli`, `<module_name>`, etc. Omit only when change cross-cutting.

**Subject priority — classify before drafting**

Enumerate ALL changes from diff, assign each a tier, pick subject from highest tier present:

| Tier | Change type | Example |
| -- | -- | -- |
| 1 | New capability — new file, new agent/skill, new flag, new user-visible behaviour | `efficiency.md` added |
| 2 | Changed behaviour — existing feature works differently, routing/trigger updated | TRIGGER/SKIP added to agent |
| 3 | Fix or removal — correctness repair, deleted dead code | audit findings fixed |
| 4 | Maintenance — quoting, README sync, version bump, formatting, refactor/extract | frontmatter `"..."` wrap, extract to `_shared/` |

Rules:

- **Never draft subject from session memory** — always enumerate from diff first, classify each change, then write subject

- Session recency bias must not dominate: last task worked on ≠ most significant change

- Line count ≠ tier: 200-line maintenance diff < 20-line new capability

- Multiple tiers present → subject names tier-1 change; lower tiers appear in bullet list only

- Tie-breaker: prefer user-visible impact when type/significance comparable

- Blank line, then bullet list — one bullet per logical change; extended description of top changes plus all other notable changes

  - Skip: typos, linting, whitespace-only edits
  - All changes skip-worthy → omit bullet list; subject-only commit — still include co-author block separated by blank line and `---`:

  ```markdown
  Fix typo in config key name

  ---
  Co-authored-by: claude[bot] <209825114+claude[bot]@users.noreply.github.com>
  ```

- **No line wrapping** — bullets and prose single continuous lines; never hard-break at any column width. Overrides any skill-level `Wrap at N chars` instruction (e.g. caveman-commit).

- **No GitHub auto-links** — never use `#N`, `@name`, or `@org` in commit messages; GitHub renders these as issue/PR links and user/org mentions, creating unintended cross-references in any repo that picks up the commit. Stricter than the general `#N`/`@name` scoping rule (`plugins/CLAUDE.md` — same-repo genuine refs OK elsewhere): a PR/issue comment lives in exactly one repo forever, but a commit message travels with the code — fork → merge into upstream → mirror → cherry-pick — so "same-repo" at authoring time gives no guarantee at read time. Default is an unconditional ban. <!-- policy-sibling: plugins/CLAUDE.md (canonical), plugins/cc_foundry/rules/git-commit.md (stub), plugins/cc_oss/skills/_shared/shepherd-voice.md -->

  - **Narrow exception**: a script that explicitly tracks its own destination repo per commit (e.g. `isCrossRepository` from `gh pr view`, checked before every commit) may embed `#N` for same-repo pushes, must substitute a full URL (`https://github.com/<owner>/<repo>/pull/N`) for cross-repo pushes (e.g. committing to a contributor's fork while referencing the upstream PR number). Reference implementation: `oss:resolve`'s `PR_REF` (`plugins/cc_oss/skills/resolve/SKILL.md` Step 4, consumed by `bin/commit_action_item.py` / `bin/commit_all_items.py`). `@name` in commit messages stays banned outright even under this exception — a commit notifying someone is essentially never the intent.

- **No non-VCS paths** — never reference files or paths not tracked in repo (e.g. `/tmp/`, `~/.claude/`, local cache dirs, machine-specific paths); commit message must be meaningful on any machine that clones the repo

## Gathering Diff Context

Before writing commit, run three in parallel:

- `git status` — identify staged new files (`A` prefix) and unstaged changes
- `git diff HEAD` — **not** bare `git diff`; bare shows only unstaged, misses staged new files; `git diff HEAD` captures staged and unstaged vs HEAD
- `git log --oneline -5` — reference repo's existing commit style

**Truncated diff — mandatory follow-up**: `git diff HEAD` output large, Bash tool saves to file (shows only 2 KB preview) — read the saved file completely before writing commit. Don't write from preview alone — most significant changes often sit past the truncation point. Also run `git diff --stat HEAD` (always fits context) for the complete file-by-file change map; use it to find which files changed most and whether any were missed in preview. Saved diff file exceeds ~2000 lines → escalate to subagent summarization, see Large diff rule below. **Large diff — subagent summarization**: diff file exceeds ~2000 lines OR `git diff --stat HEAD` shows >10 files spanning >2 plugins/concerns → spawn one Agent task per logical file group — one agent per top-level directory in stat output (e.g. one per `plugins/<name>/`, one collective for everything outside `plugins/`); max 5 agents, group smallest partitions until ≤5. Each task runs inline (not background); **use `model: haiku`** — diff summarisation is bounded, low-complexity output; receives `git diff HEAD -- <file> [<file> ...]`, returns compact bullet summary: what changed, highest tier classification. Orchestrator aggregates summaries, writes commit from aggregated evidence only — never session memory. After aggregation, cross-check every file in `git diff --stat HEAD` appears in ≥1 summary; missing file → spawn one additional Agent task for it before drafting. On agent failure/timeout: fall back to direct `git diff HEAD -- <files>` read for that group; surface unread group as a gap in commit message.

**Grouped commit — resolve/verify flow**: committing grouped changes, any post-commit verification step (`/oss:resolve`, lint gate, test run) is also batched — one delegated agent covers the entire grouped commit, not one agent per change. Agent writes full findings to `.temp/`; returns compact JSON envelope to orchestrator. Orchestrator reads envelope verdict; reads full file only on FAIL. Never spawn N resolve agents for N grouped changes in the same commit.

**Large diff — agent handover format**: before spawning, create run dir: `RUN_TS=".temp/commit-diff/$(date -u +%Y-%m-%dT%H-%M-%SZ)"; mkdir -p "$RUN_TS"`. Each agent task writes to `$RUN_TS/group-<dir-slug>.md` using this fixed structure:

```markdown
## Group: <top-level-dir>
Files in scope: <comma-separated list>

| File | Change | Tier | Type |
|------|--------|------|------|
| `path/to/file` | one-line what changed | 1 | feat |

Highest tier: <N>
Tier-1 items: <file — specific new capability>
Tier-2 items: <file — specific behaviour change>
Recurring theme: <pattern visible across ≥2 files in this group, e.g. "python→python3 migration" or "none">
```

Agent returns ONLY this JSON envelope (no prose after it):

```json
{"status":"done","group":"<dir>","files_covered":["a","b"],"highest_tier":1,"theme":"<cross-file pattern or null>","file":"<path>","summary":"<file>: <change> T<N>; <file>: <change> T<N>"}
```

`theme` — one-phrase pattern visible across ≥2 files in this group (e.g. `"python→python3 migration"`, `"TRIGGER/SKIP added to all agents"`); `null` when no pattern.

Orchestrator: collect envelopes, verify coverage (every file in `git diff --stat HEAD` in ≥1 `files_covered`), then read `.md` files directly — ≤5 small files within direct-read threshold (file-handoff-protocol.md). Draft commit from `.md` file content only — never from envelope `summary` strings (too lossy). `status: "done_with_concerns"` → flag that group as uncertain in commit message.

**Compound synthesis step** (mandatory before drafting): after reading all group `.md` files, scan across all groups for repeated themes — same concept changed in N **codebase** files across different groups each classified T3–T4 individually. ≥3 codebase files share a theme (same pattern replaced, same flag added everywhere, same agent property updated system-wide) → elevate aggregate to T2 minimum, name the cross-cutting change in commit subject. Per-group tiers are local signal only — aggregate tier governs subject line. Exclude docs/supplementary files (README, CHANGELOG, comments, docstrings) from the ≥3 threshold count — they don't compound.

**High-churn files — mandatory diff read**: any file with >50 lines changed in `git diff --stat` NOT already in planned bullets — read the actual diff before writing message. Don't assume from session memory or prior context; post-compaction sessions have no reliable recall. User/developer-facing changes (command syntax, CLI argument names, invocation patterns, API surface, usage examples) must be identified and prioritised regardless of earlier discussion — outrank internal restructuring of equal line count. **Ranking rule — diff first, recency last**: classify all changes into tiers (see Subject priority table above) before writing title.

- Conversational recency bias must not dominate — last task in session ≠ most significant
- Title must reflect highest-tier change in diff, not most recent one

**Same-tier tie-breaking — session work over bundled pre-existing**: multiple T1 items exist in diff → item explicitly produced in current session takes subject priority. One T1 item was the explicit focus of conversation, design, and iteration this session → it leads, even if a different T1 item appears first in subagent output or has more lines. This does NOT override "never draft from session memory" — still classify from diff; use session context only to rank among same-tier items, not to skip diff analysis. Ask: "which T1 item did this session set out to produce?" — that one leads.

**New files — classify by content, not by `A` marker**: any file marked `A` in `git status` must be explicitly mentioned in the commit bullet list. Tier depends on content origin:

- Content is genuinely new capability/behaviour → tier 1
- Content extracted/refactored from existing file → tier 4 (maintenance); mention as "extracted from X", not "added"
- Test-only new file (adds tests, no source change) → tier 4; `test` type; not tier 1 even though content is new
- New file + new content = tier 1. New file + moved content = tier 3. New file + tests only = tier 4.

**Semantic novelty beats diff verbosity**: new capability/interface/script outranks a verbose-but-routine config edit even if the config diff has more lines. Ask "what would the reviewer need to know first?" — that's the most significant change.

**Compound change detection**: ≥3 **codebase** files share a common theme in their changes (same concept replaced, same flag added, same pattern adopted everywhere) → treat aggregate as potentially higher tier than any individual file suggests. Signals: same function/string replaced across N files → migration pattern; same trigger/description updated in N agents → routing change (T2 minimum); same convention adopted across all plugins → new standard. Rule: after reading all per-file diffs (or all subagent `.md` summaries), ask "do these individually small changes form a coordinated pattern?" — if yes, classify the whole at aggregate tier, not per-file tier. Name the pattern in commit subject, not individual files. **Docs/supplementary exempt**: README, CHANGELOG, inline comments, docstrings, and other documentation-only files are standalone entities — repeated small doc tweaks don't compound into a higher tier regardless of count.

**Evidence-only body — mandatory, not situational**: the diff is the only evidence. Conversation/session context may be used **exclusively** to explain the *why* behind a change already confirmed present in `git diff HEAD` — never to assert that a change, action, or removal happened. A sentence describing something not backed by a `+`/`-` line doesn't go in the message, no matter how confidently conversation discussed it as done. Covers two failure modes, both forbidden equally:

- **Reverted-change leak**: content introduced then rolled back before commit (e.g. via `git checkout HEAD -- <file>` or a later overwrite) — visible in chat history, absent from `git diff HEAD`. Distinct from content removed BY this commit, which appears as `-` lines and IS valid to mention.
- **Narrated-but-unlanded change**: conversation describes an edit as accomplished (e.g. "removed the X section") but the actual diff hunk shows different content than narrated (e.g. it removed Y, not X — because X was reverted earlier and the removal script actually matched Y). Session narrative describing *what* was done isn't evidence of *what the diff contains* — only the diff is.

**Verification step — required before finalizing every commit body, no exceptions**: for each bullet or clause, pick one distinctive token from it (identifier, section heading, filename, error string, config key), confirm it appears in actual `git diff HEAD` output for that file — not in a subagent summary, not in session memory of an earlier tool result. A claim with no matching `+`/`-` line gets rewritten to match what the diff actually shows, or dropped. If subagent summarization was used instead of a full inline read, re-run `git diff HEAD -- <file>` for that specific file before including the change in any bullet — the subagent's prose summary isn't itself sufficient evidence for final wording.

## Co-authors

Separate co-author block from bullet list with `---`:

```markdown
- last bullet

---
Co-authored-by: claude[bot] <209825114+claude[bot]@users.noreply.github.com>
```

- Claude: `Co-authored-by: claude[bot] <209825114+claude[bot]@users.noreply.github.com>`
- Codex (if contributed anything — code, review, diagnosis, analysis, architectural guidance, or "here's what needs fixing and why"): `Co-authored-by: OpenAI Codex <codex@openai.com>`

**Codex intellectual contributions count**: Codex earns the trailer whenever it shaped the outcome — even if Claude wrote the final code.

- Examples: Codex identified root cause, Codex suggested approach, Codex returned a review comment that led to the change
- Test: "would this commit exist in current form without Codex's input?" — if yes, include trailer

Co-author trailer on every Claude Code commit — not conditional on user mentioning involvement.

**Skill commit templates — trailers not optional**: when a skill or workflow step provides a `git commit -m "..."` template (heredoc or one-liner), the template is **message body scaffold only**. `---` separator and co-author block must always be appended regardless of whether the template shows them:

- **Heredoc** (`cat <<'EOF' ... EOF`): insert `---` block and trailers before closing `EOF`
- **One-liner `-m "string"`**: convert to heredoc — one-liners cannot carry multi-line trailers

Never skip trailers because skill template omits them.

## Branch Safety

Default branch is repo-specific — do NOT hardcode `main` or `master`. Detect dynamically via `git symbolic-ref refs/remotes/origin/HEAD`, `gh repo view`, or `git remote show origin`.

Before any `git commit`, check current branch: `git branch --show-current`

## Commit Authorization

`commit-guard.js` doesn't intercept `git commit` at all — commit authorization is prompt-discipline only, no runtime/hook check. Two valid signals, no others:

| Source | Authorization | Claude action |
| -- | -- | -- |
| **Skill workflow** — a skill documented to commit as part of its flow (e.g. `/oss:resolve`, `/research:run`) | The user invoking that skill IS the authorization — covers however many commits the skill's documented commit strategy calls for (one-per-item, batched, etc.) | Run `git commit` inside the skill workflow; no `AskUserQuestion` per individual commit — asking N times for one already-approved skill run is pure friction, not a security signal |
| **Ad-hoc / interactive** — anything else: user said "commit this", Claude decides a commit is warranted, no active skill workflow | `AskUserQuestion` confirmation, every time, any branch (feature or default) — no exceptions, no auto-arm shortcut | Show branch + diff size + draft subject in the question; only commit after explicit confirmation |

**Never commit autonomously**: no commit without an active skill workflow step or an in-turn `AskUserQuestion` confirmation.

- Each message is independent — prior messages in session containing "commit" don't authorize commits in later messages; pattern-matching on session history isn't a valid signal source.
- A skill's own documented workflow is the only exemption from per-commit `AskUserQuestion` — don't extend this exemption to ad-hoc commits by rationalizing them as "workflow-like."

## Staging and Hooks

- Never `git add -A` or `git add .` — always stage specific files by name
- Never `--no-verify` — if pre-commit blocks, fix underlying issue
- Never `--no-gpg-sign` unless user explicitly requests it

## Push Authorization

Two-tier design: force-push is an unconditional hard block; regular push is sentinel-gated per-invocation.

**Force-push** (`-f`, `--force`, `--force-with-lease`): forbidden on any branch, always — no override, even with explicit user instruction. Enforced twice (defense-in-depth): `commit-guard.js` checks this unconditionally, before any sentinel lookup; `.claude/settings.json` `permissions.deny` also deny-lists all three flag forms. Claude never runs a force-push variant regardless of signal.

**Regular push**: gated by a dedicated push sentinel — `/tmp/claude-push-auth-<repo-slug>-<branch-slug>`, TTL 15 min. Claude invokes `AskUserQuestion` (state branch + what's being pushed) → user runs `! touch $PUSH_SENTINEL` from own shell → Claude runs `git push` → user runs `! rm -f $PUSH_SENTINEL` after.

**Deliberate asymmetry vs commit**: push has **no skill-workflow exemption**. Commit allows a documented skill workflow to self-authorize multiple commits per its own commit strategy, no `AskUserQuestion` per commit; push always requires a fresh in-turn `AskUserQuestion` confirmation, even from inside a skill workflow, even if the user said "push this" in the same message. Intentional, not oversight: push is repo-visible, harder to undo than a local commit.

Standard interactive Bash permission dialog still applies on top of the hook gate, unchanged — `.claude/settings.json` `permissions.allow` not modified for push; no allow-list shortcut.

## History Safety

- Prefer `git revert` over `git reset --hard` (preserves history)
- Prefer merge commits for conflict resolution over rebase (preserves SHAs)
