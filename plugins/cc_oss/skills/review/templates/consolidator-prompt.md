<!-- file: consolidator-prompt.md — consumers: plugins/cc_oss/skills/review/SKILL.md -->

**Task:** Read all finding files in `$RUN_DIR/` (agent files: `foundry--sw-engineer.md`, `foundry--qa-specialist.md`, `foundry--perf-optimizer.md`, `foundry--doc-scribe.md`, `foundry--linting-expert.md`, `foundry--solution-architect.md`, `foundry--challenger.md` if present, `foundry--codex.md` if present — skip missing). Load `<REVIEW_SKILL_DIR>/checklist.md` via `cat` (not Read tool — version-pinned cache path), apply consolidation rules (signal-to-noise filter, annotation completeness, section caps). Load `<_OSS_SHARED>/review-section-taxonomy.md` via `cat` for canonical section header strings, agent-to-section ownership. Include only findings passing Step 4 cross-validation (verdict=CONFIRMED or un-cross-validated medium/low). For `foundry--challenger.md`: map severity keys Blockers → critical/high, Concerns → medium, Nitpicks → low when aggregating counts.

**Filtering rules:**

- Precision gate: only include findings with concrete, actionable location (function, line range, or variable name).
- Finding density: modules under 100 lines → aim ≤10 total findings.
- Ranking: within each section, order by impact (blocking > critical > high > medium > low).
- Codex deduplication: include `foundry--codex.md` unique findings under `### Codex Co-Review`; same file:line raised by both agent and Codex → keep agent version, mark 'also flagged by Codex'.

**Issue alignment (when `issue-*.md` files exist in `$RUN_DIR`):** Include `### Issue Root Cause Alignment` section placed immediately after `### [blocking] Critical`. Per linked issue: state root cause hypothesis, whether PR addresses it (yes / partially / no), whether PR description diverges from issue's stated problem, whether reproduction scenario tested. Any `root cause misalignment` or `scope divergence` finding at least HIGH severity. **PR description drift**: Before flagging `scope divergence`, cross-check PR thread and review comments to determine what was actually agreed; description diverging from *thread consensus* is signal worth flagging.

**File head — MANDATORY format:** file MUST begin with a `---`-delimited YAML metadata block exactly as in `<REVIEW_SKILL_DIR>/templates/review-report.md` — opening `---` on line 1, then 14 fields in order (`Title:`, `PR:`, `Date:`, `PR Type:`, `Scope:`, `Focus:`, `Agents:`, `CI:`, `Gate:`, `Outcome:`, `Summary:`, `Confidence:`, `Next steps:`, `Path:`), then closing `---`, then report body. Do NOT encode head as HTML comments (`<!-- ... -->`) or any other form — orchestrator reads `---` block verbatim as reply header; no `---` head = broken terminal output. `Title:` `oss-review — [PR #N title]` · `PR:` `#<PR_NUMBER>` (omit field entirely when `<PR_NUMBER>` is empty — direct-path mode has no PR) · `Confidence:` aggregate score — key gaps · `Path:` `→ <REPORT_DIR>/review-report.md`.

**Header fields** (orchestrator must expand all shell vars to literal values before spawning):

- `PR:` `#<PR_NUMBER>` — omit field entirely when `<PR_NUMBER>` is empty (direct-path mode) · `Date:` `<DATE>` · `PR Type:` classify from diff INTENT (not title/file-count): `fix` / `feat` / `refactor` / `perf` / `docs` / `ci` / `chore` / `test` / `mixed` · `Scope:` key changed files from `<CHANGED_FILES>` (skip test files if >3 source; cap ~5) · `Focus:` `<SCOPE>` — one-line description from diff + PR body · `Agents:` short names of agents with output files in `$RUN_DIR/` · `CI:` `failing — [<CI_FAILING_CHECKS>]` when that value is non-empty, else `passing (<CI_COUNTS>)` — `<CI_COUNTS>` empty too (no checks reported): write `pending` · `Gate:` literal `<GATE>` value (`PASS` or `BLOCK` — reject-gate reports never reach the consolidator, that value is always one of these two here; a `BLOCK` gate does not change how you write `Outcome:` below, it's already carried in `CI:`/the findings) · `Outcome:` `APPROVE` / `NEEDS_WORK` / `REQUEST_CHANGES` from your own findings · `Summary:` 1–2 sentences · `Next steps:` blockers first, max 5

**Severity tiers:** Every finding must carry an explicit inline severity label: `[cosmetic]`, `[low]`, `[medium]`, `[high]`, or `[critical]`. Cosmetic findings go in the dedicated `### Cosmetic / Style` section — never interleaved with behavioural findings.

**Confidence parsing:** Parse each agent's `confidence` from JSON envelope. Assign `codex` fixed confidence 0.75 (moderate — static analysis, no runtime context).

**Write to:** `<REPORT_DIR>/review-report.md` using Write tool.

**Source Files footnote**: after the `## Confidence` block, append `## Source Files` section. Use `Glob(pattern="*.md", path="$RUN_DIR")` to list every handover file present — lets reviewers locate raw subagent outputs without knowing the run timestamp. `$RUN_DIR` is consolidator's self-resolved run-dir (from run-dir preamble: `cat "${TMPDIR:-/tmp}/oss-review-run-dir-${CSID}"`) — not orchestrator-substituted.

**Return ONLY** one-liner summary: `verdict=<APPROVE|REQUEST_CHANGES|NEEDS_WORK> | findings=N | critical=N | high=N | file=<REPORT_DIR>/review-report.md`
