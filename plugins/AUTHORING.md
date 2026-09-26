<!-- scope: project-local plugin authoring detail — full text for sections `plugins/CLAUDE.md` keeps compressed. Load on demand via `cat`; not auto-injected. -->

# Plugin Authoring — Full Detail

Companion to `plugins/CLAUDE.md`. Each section here expands a compressed `plugins/CLAUDE.md` section named in its "Full ... : AUTHORING.md §X" pointer. Read the relevant `§` only when the narrow trigger it names applies — not needed for routine edits.

## Markdown No-Wrap

Full rule in `plugins/CLAUDE.md` §Markdown No-Wrap.

Self-documenting comments to remove from procedural code (WHAT/HOW, not WHY): `# Create directory`, `# Check if exists`, `# Parse flags`, and similar restatements of the line beneath them.

## GitHub Reference Scoping — Full Catalogue

Full one-line rule in `plugins/CLAUDE.md` §GitHub Reference Scoping.

**Forbidden**: bare `#N` for local ordinals — numbered-list items, loop/step indices, rank/leaderboard position, internal check/rule IDs (e.g. "resolve #2 from review list", "checks-index #42", "ranked #1"). Write the bare number, an ordinal (`1st`), or a qualifying word (`step 7`) instead.

**Forbidden**: bare `@word` for non-GitHub-user tokens written in plain prose — Python decorators (`@pytest.fixture`, `@staticmethod`), internal role handles (`@lead`), placeholder mentions. Either drop the `@` (`lead`, not `@lead`) or keep it inside backticks/code fence (`` `@pytest.fixture` ``) so it renders as code, not a live mention.

**`#N` is same-repo only** — GitHub resolves bare `#N` against the repo the comment/commit lives in. Referencing an issue/PR/discussion in a *different* repo needs the full URL (`https://github.com/<owner>/<repo>/issues/N`), never `#N` or `owner/repo#N` shorthand — wrong repo context silently cross-links to the wrong item.

**`@name` requires certainty of intent** — before writing a real `@handle`, confirm the goal is actually to notify/ping that person now. Backticks are for code-shaped tokens only (previous bullet) — never used to defang a real handle; that reads as claiming a person's name is a code identifier. If intent is genuinely uncertain (naming someone in passing inside an internal note not addressed to them), drop the `@` entirely (`octocat`, not `@octocat`) instead. Release-note and CHANGELOG contributor credit is standard, *deliberate* ping-intent — the notification is the point (crediting them) — leave the live `@handle` as-is; do not neutralize it.

**OK**: `#N`/`@name` inside a fenced code block or inline backticks (rendered as code, not live) — for actual code tokens, not for disguising a person's handle; genuine `gh` CLI examples (`gh pr view 123`); actual same-repo GH issue/PR references in commit-message or release-note guidance; real GitHub usernames in reply-drafting, CODEOWNERS templates, or release-note/CHANGELOG contributor credit — all deliberate live mentions.

## Writing Style — Compression Tiers

Full rule in `plugins/CLAUDE.md` §Writing Style — Compression Tiers.

| Content | Tier | Rule |
| -- | -- | -- |
| READMEs, `docs/`, user-facing guides | Verbose | Full sentences, rationale, examples |
| Final reports (`.reports/`), human-read output | Normal caveman | Drop articles/filler/hedging; full sentences where clarity needs |
| Agent source (`<workflow>`, `<role>`, `<notes>`, skills, rules, modes), handover files (`.temp/`), inter-agent prose | Ultra caveman | Max compression — fragments OK, zero filler, shortest synonyms |

Verbatim always (no compression): code blocks, bash commands, tool citations, file:line refs, JSON keys, structured field labels, compact JSON envelopes.

Unsure which tier applies: human reads the artifact directly → normal; only agents read it → ultra.

## Comment Compression

Rule in `plugins/CLAUDE.md` §Writing Style. Comments inside `.md` bash blocks load into context on **every** run of that skill — a 6-line rationale comment is paid every invocation, forever.

**Before** (real, 2026-08):

```bash
# 2026-08 audit (codemap_battery_rebalance): mock-rdeps/uncovered/undocumented/xrefs together
# were 57% of query volume with no benchmarked win of their own — gated behind the one
# dimension agent that actually reads each, instead of running unconditionally for every PR.
# Gate only on unambiguous full-skip modes already computed above (never on the CHORE+non-deps
# partial skip — too easy to mis-encode into a false skip, so left ungated; extra queries there
# cost tokens, never coverage).
```

**After**:

```bash
# 57% of query volume, no benchmarked win — gate per consuming dimension, not per PR.
# Full-skip modes only; CHORE+non-deps partial left ungated (mis-encode risk > token saved).
```

Survived: measured fact (57%, no win) · mechanism (gate per dimension) · deliberate exclusion + its reason. Died: date, basket name, "already computed above", every narration clause. ~6 lines → 2.

**A trap needs one line, not a paragraph:**

```bash
# grep -c prints 0 AND exits 1 — || echo 0 double-fires, captures "0\n0"
```

**Test before deleting**: could removing this comment let someone reintroduce a bug, or "simplify" something load-bearing? Yes → keep the content, compress the wording. No → delete.

**Recurring casualties of a compression pass** — an independent challenger over 318 plugin files found these lost most often; keep each verbatim:

- Modal words: `must`, `never`, `always`, `only`, `MUST`. "Never X" → "avoid X" and "must get" → "gets" weaken an obligation to a description.
- Conjunctions carrying logic: `but` (exception), `or` (any-one vs all), `then` (sequence), `if`/`when` (condition), `which ran` (tense). "X but no Y" ≠ "X, no Y"; an asyndetic list reads conjunctively.
- Paragraph boundaries that scope a condition — merging "Skip if …" with the next step makes the skip cover the step.
- Actors and qualifiers: "text you did not author" ≠ "unauthored text"; "verbose evidence" ≠ "evidence"; "for declared domain" is the test, not filler.
- `# tmpdir-exempt:` / `# timeout:` / `# noqa:` / `# shellcheck` — the whole comment or parenthetical carrying the marker, reason text included.
- Invented shorthand (`w/`, `+` for "and", new `→` chains) — same tokens, one more reading; pre-existing `→` chains stay as they are.
- Sentences pinned by tests (`test_contracts.py`, `test_codex_user_questions.py`, `test_skill_parity.py`, codex-rig `tests/`) — run the owning plugin's suite in the editing pass; a pin failure means restore the whole file, never edit the test.

Origin: agents writing inline rationale drift to essay length unless told otherwise — the example above was generated by an agent earlier the same day it was trimmed. The casualty list comes from the 2026-09 repo-wide pass, which also measured the corpus as already near the ultra tier (light pass −0.6%, hand-measured ceiling ≈ −6%) — a further repo-wide pass is low-yield.

## Length Unit Convention

Full rule in `plugins/CLAUDE.md` §Length Unit Convention.

Never lines-only or chars-only as the sole unit — line length is unbounded and char counts are opaque to humans, so tokens stay the primary measure with a line estimate for human scanning. Applies to: per-file limits, per-turn budgets, envelope size caps, output size constraints, consolidator thresholds.

## bin/ Language Policy

Applies when creating or editing a `plugins/<name>/bin/` script.

- **Python scripts**: type hints, module docstring, `if __name__ == "__main__"` guard; ruff-format 120-char line length (pre-commit enforced); aggregate related print output into a single `print()` using `\n`/`\t`; pure functions (no I/O, no subprocess, no env-var reads) → `doctest` in the docstring; anything with I/O/subprocess/argv → `pytest` with `capsys`/`monkeypatch` in `tests/` alongside `bin/`
- **bin/ scope**: deterministic transforms only (parse args, resolve paths, compute one value); decision flow, branching prompts, agent-dispatch logic stays in SKILL.md prose
- **Reference design**: `plugins/codemap-py/bin/` (typed, docstrings, `__name__` guards, dataclass serialization boundaries)

## references/<agent> Nesting Mechanism

Applies when adding an agent sidecar fragment under `plugins/<name>/references/<agent>/`.

Claude Code scans `agents/` recursively and turns each subdirectory into part of the scoped identifier, so a fragment parked at `agents/<parent>/<fragment>.md` registers as a real dispatchable agent named `<plugin>:<parent>:<fragment>` — with `Tools: All tools`, since a prose fragment carries no `tools:` frontmatter. There is no ignore/exclude mechanism (`plugin.json`'s `agents` field selects where to look, not what to skip), so keeping fragments out of `agents/` is the only fix. Also keep them out of `bin/`, whose contents Claude Code adds to the Bash tool's `PATH`; other reserved component dirs (see the plugins-reference docs for the current list) are likewise unsuitable. `skills/<name>/` is fine — that scan matches `<name>/SKILL.md` only, which is why `_shared/`, `modes/`, and `templates/` work there.

## Shared File Authoring Rule — Why + Precedent

Full rule in `plugins/CLAUDE.md` §Shared File Authoring Rule.

**Why**: a grep-based orphan check finds zero hits on an unmarked file → an agent concludes the file is dead → deletes it. Happened to `adversarial.md`, `upgrade.md`, `vitality-calibration.md`. A single comment line prevents deletion.

Consumer in a different plugin than the file → add the `<!-- file: <basename> — consumers: ... -->` header to the file itself instead of relying on a same-plugin `# loads:` mention.

## Policy Duplication Marker — Precedent

Full rule in `plugins/CLAUDE.md` §Policy Duplication Marker.

GitHub `#`/`@` reference-scoping policy (`plugins/CLAUDE.md` §GitHub Reference Scoping) shipped a refinement to itself and to `shepherd-voice.md` but missed `plugins/cc_foundry/rules/git-commit.md` — caught only because the user noticed by hand. The marker was added retroactively to all four copies (`plugins/CLAUDE.md`, `git-commit.md` stub + `_full`, `shepherd-voice.md`). Use this as the reference example when adding a marker to a newly duplicated policy.

## Fallback / Resilience Infrastructure

Full rule in `plugins/CLAUDE.md` §Fallback / Resilience Infrastructure.

**Examples**: fallback for missing `foundry` agents cannot live in `foundry`; fallback for missing `oss` agents cannot live in `oss` — same for any plugin pair.

**Intentionally not manifested** in `propagate_shared.py`: files that legitimately vary per plugin, e.g. `agent-resolution.md` fallback tables and per-plugin `rules/quality-gates.md`. Do not add them to the MANIFEST.

## Self-Contained `_shared`

Full rule in `plugins/CLAUDE.md` §Self-Contained `_shared`.

**Incident**: `$HOME/.claude/skills/_shared/...` no longer exists as a valid path — `/foundry:setup` symlinks only `rules/*.md` + `TEAM_PROTOCOL.md`, and purges any leftover `~/.claude/skills/` link. A directory carrying `SKILL.md` there registers as a **user-level skill** and silently shadows Claude Code's bundled skill of the same name — that is what broke bare `/review` (it reached Claude Code's bundled reviewer instead of `oss:review`).

**Precedent**: `codex-delegation.md`, canonical in `foundry`, was copied to `cc_research` after research's Check R7 lost the file on a research-only install (no `foundry` present to reach into).

**Counter-precedent (optional-provider exception)**: codemap-py's `claude-skills/_shared/codemap-{gates,context}.md` were manifested into five consumer copies, and each copy froze at the consumer's last release while the installed `codemap-py` kept moving — the consumer drove a newer CLI with stale contract prose, and no test could see it because tests only read the source tree. The R7 incident does not apply: without `codemap-py` the whole feature is already a no-op (every wrapper gates on `command -v codemap-py`), so the contract has no value to lose. Consumers now resolve the active install (`resolve_shared_path.py codemap-py claude-skills/_shared`) and print their own fallback line when it is absent. The exception is narrow — see `plugins/CLAUDE.md` §Self-Contained `_shared` for the three conditions; degraded-mode content stays duplicated.

## Versioning — Pre-Bump Checklist + Worked Example

Full rule (trigger, decision table, one-bump-per-commit) in `plugins/CLAUDE.md` §Versioning.

Serialized artifact schemas are separate version families under the repository Version Continuity rule. For each changed schema, compare the current writer and template with the same family's value in committed `HEAD`; start a new family at 1 and advance a committed family by exactly one. Historical readers and uncommitted intermediate designs do not change that baseline. Record the comparison beside the affected verification.

**Example**: start `0.2.0`, session has both a wording fix and a feature add → commit as `0.3.0` (not `0.2.1`) — `X` absorbs any pending `Y`.

**Pre-bump checklist** — all steps mandatory; skipping any step = violation:

0. **Test-only guard**: run `git diff HEAD --name-only -- plugins/<name>/`, check if every changed path is under `plugins/<name>/tests/`. Yes → **STOP; no bump needed** — test-only commits never touch version.
1. Read HEAD baseline: `git show HEAD:<plugin-path>/.claude-plugin/plugin.json | grep version`
2. **Read on-disk version**: `grep version <plugin-path>/.claude-plugin/plugin.json` — on-disk ≠ HEAD → a session bump was already applied → **STOP; do not proceed**
3. Classify the highest-magnitude change in the session (`X` or `Y`)
4. Calculate the new version from the HEAD baseline: `X` → bump minor, reset patch to `0`; `Y` → bump patch only; max +1 on the bumped component
5. Write the calculated version — must be exactly HEAD + a single bump; anything higher is a double-bump violation

**One bump per commit — never per session.** Scope is the commit, not the working session. A session producing N commits that each touch a plugin bumps that plugin N times, once per commit. Step 2's on-disk-vs-HEAD check only suppresses a *second* bump for the *same* pending commit — once you commit, HEAD moves, that check clears, and the next commit touching the same plugin bumps again from the new HEAD. Never treat an on-disk bumped value as a new baseline to increment from *within* one commit's staging.

> **Incident (2026-08-08)**: one session's work was split into two commits. The first (`refine(plugins): split rules into stub plus _full`, foundry-only) shipped with no bump because "one bump per session" was read as covering both; that also invalidated the second commit's number, which had been computed against the un-bumped baseline. One missed bump, two wrong commits. Both were rebuilt via `commit-tree` + compare-and-swap `update-ref` — foundry 0.40.1 → 0.41.0 → 0.42.0. If a split is decided after bumping, re-derive each commit's version from the commit that precedes it, not from the session baseline.

When a plugin ships additional runtime manifests such as `.codex-plugin/plugin.json`, keep every shipped manifest on the same bumped version and update CHANGELOG or release metadata when that plugin's convention requires it.
