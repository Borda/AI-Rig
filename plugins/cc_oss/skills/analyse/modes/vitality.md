<!-- loads: vitality-report.md -->

<workflow>

**Task hygiene**: call TaskList first; close orphaned tasks from prior runs. **Task tracking**: TaskCreate tasks for each major phase before starting: "Step 1 Data Fetch", "Step 2 Axis Scoring (3 parallel)", "Step 3 Assemble Scores", "Step 4 Report", "Step 5 Codex Review", "Step 6 Adversarial Rework Loop", "Step 7 Terminal Output"; mark each in_progress/completed as you go. **In `--quick` mode** (QUICK_MODE=true): omit the "Step 5 Codex Review" and "Step 6 Adversarial Rework Loop" tasks — they are skipped.

## Step 1 — Data Fetch

**Task tracking**: mark "Step 1 Data Fetch" in_progress before spawning.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# reload GH_OWNER/GH_REPO (Check 41, set by analyse/SKILL.md)
IFS= read -r GH_OWNER < "${TMPDIR:-/tmp}/analyse-gh-owner-${CSID}" 2>/dev/null || GH_OWNER=""
IFS= read -r GH_REPO < "${TMPDIR:-/tmp}/analyse-gh-repo-${CSID}" 2>/dev/null || GH_REPO=""
mkdir -p .reports/analyse/vitality  # timeout: 5000
TODAY=$(TZ=UTC date +%Y-%m-%d)  # timeout: 5000
RUN_TS=$(TZ=UTC date +%Y-%m-%dT%H-%M-%SZ)  # timeout: 5000
DATA_FILE=".reports/analyse/vitality/raw-data-${GH_OWNER}-${GH_REPO}-${TODAY}.jsonl"
PARTIAL_A=".reports/analyse/vitality/partial-A-${GH_OWNER}-${GH_REPO}-${RUN_TS}.json"
PARTIAL_B=".reports/analyse/vitality/partial-B-${GH_OWNER}-${GH_REPO}-${RUN_TS}.json"
PARTIAL_C=".reports/analyse/vitality/partial-C-${GH_OWNER}-${GH_REPO}-${RUN_TS}.json"
SCORES_FILE=".reports/analyse/vitality/scores-${GH_OWNER}-${GH_REPO}-${RUN_TS}.json"
# persist (Check 41)
echo "$GH_OWNER" > "${TMPDIR:-/tmp}/vitality-gh-owner-${CSID}"
echo "$GH_REPO" > "${TMPDIR:-/tmp}/vitality-gh-repo-${CSID}"
echo "$DATA_FILE" > "${TMPDIR:-/tmp}/vitality-data-file-${CSID}"
echo "$PARTIAL_A" > "${TMPDIR:-/tmp}/vitality-partial-a-${CSID}"
echo "$PARTIAL_B" > "${TMPDIR:-/tmp}/vitality-partial-b-${CSID}"
echo "$PARTIAL_C" > "${TMPDIR:-/tmp}/vitality-partial-c-${CSID}"
echo "$SCORES_FILE" > "${TMPDIR:-/tmp}/vitality-scores-file-${CSID}"
echo "$RUN_TS" > "${TMPDIR:-/tmp}/vitality-run-ts-${CSID}"
```

**Spawn**:

> **Agent budget** — each spawn costs ~120,851 tok of fixed overhead (~73 tool-calls' worth) plus ~12.0 s/call, so work under ~73 calls is cheaper done inline: spawn nothing. Keep each agent near ~55 tool-calls; past ~60 they stall without returning an envelope, forcing reconstruction from disk. Every spawn prompt must require an envelope even on exhaustion — `partial: true` plus what was finished.

> `Agent(subagent_type="oss:gh-scraper", prompt="GH_OWNER=$GH_OWNER GH_REPO=$GH_REPO DATA_FILE=$DATA_FILE")`

Wait for completion. Verify `$DATA_FILE` exists and non-empty. TaskUpdate "Step 1 Data Fetch" completed.

## Step 2 — Parallel Axis Scoring

**Task tracking**: mark "Step 2 Axis Scoring (3 parallel)" in_progress.

Spawn all 3 `oss:repo-warden` agents simultaneously in single response:

> `Agent(subagent_type="oss:repo-warden", prompt="GH_OWNER=$GH_OWNER GH_REPO=$GH_REPO DATA_FILE=$DATA_FILE PARTIAL_FILE=$PARTIAL_A AXIS_GROUP=A")`
>
> `Agent(subagent_type="oss:repo-warden", prompt="GH_OWNER=$GH_OWNER GH_REPO=$GH_REPO DATA_FILE=$DATA_FILE PARTIAL_FILE=$PARTIAL_B AXIS_GROUP=B")`
>
> `Agent(subagent_type="oss:repo-warden", prompt="GH_OWNER=$GH_OWNER GH_REPO=$GH_REPO DATA_FILE=$DATA_FILE PARTIAL_FILE=$PARTIAL_C AXIS_GROUP=C")`

**Health monitoring** (CLAUDE.md §6): before spawning, create checkpoint:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# reload vars (Check 41)
IFS= read -r GH_OWNER < "${TMPDIR:-/tmp}/vitality-gh-owner-${CSID}" 2>/dev/null || GH_OWNER=""
IFS= read -r GH_REPO < "${TMPDIR:-/tmp}/vitality-gh-repo-${CSID}" 2>/dev/null || GH_REPO=""
IFS= read -r RUN_TS < "${TMPDIR:-/tmp}/vitality-run-ts-${CSID}" 2>/dev/null || RUN_TS=""
SCORE_CHECKPOINT_FILE="/tmp/vitality-score-check-${GH_OWNER}-${GH_REPO}-${RUN_TS}-${CSID}"  # tmpdir-exempt: pre-existing hardcoded path, not a sentinel-suffix rename site
touch "$SCORE_CHECKPOINT_FILE"  # timeout: 5000
```

Every 5 min while waiting: `find .reports/analyse/vitality -newer "$SCORE_CHECKPOINT_FILE" -name "partial-*.json" | wc -l` — new files = alive; zero = stalled. Hard cutoff: 15 min no file activity → timed out. One extension (+5 min) if partial file tail explains delay — second unexplained stall = cutoff. On timeout: read tail of any partial output; surface with ⏱ marker.

Wait for all 3 agents. Verify all 3 partial files exist: `$PARTIAL_A`, `$PARTIAL_B`, `$PARTIAL_C`.

TaskUpdate "Step 2 Axis Scoring (3 parallel)" completed.

## Step 3 — Assemble Scores

**Task tracking**: mark "Step 3 Assemble Scores" in_progress.

Read all 3 partial files using Read tool. Merge into unified `$SCORES_FILE`:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_OSS_SHARED=$(ls -d ~/.claude/plugins/cache/borda-ai-rig/oss/*/skills/_shared 2>/dev/null | sort -V | tail -1)  # timeout: 5000
[ -z "$_OSS_SHARED" ] && _OSS_SHARED="plugins/cc_oss/skills/_shared"
SCORING_FILE="$_OSS_SHARED/vitality-scoring.md"
echo "$SCORING_FILE" > "${TMPDIR:-/tmp}/vitality-scoring-file-${CSID}"  # persist (Check 41)
```

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# reload vars (Check 41)
IFS= read -r PARTIAL_A < "${TMPDIR:-/tmp}/vitality-partial-a-${CSID}" 2>/dev/null || PARTIAL_A=""
IFS= read -r PARTIAL_B < "${TMPDIR:-/tmp}/vitality-partial-b-${CSID}" 2>/dev/null || PARTIAL_B=""
IFS= read -r PARTIAL_C < "${TMPDIR:-/tmp}/vitality-partial-c-${CSID}" 2>/dev/null || PARTIAL_C=""
IFS= read -r SCORING_FILE < "${TMPDIR:-/tmp}/vitality-scoring-file-${CSID}" 2>/dev/null || SCORING_FILE=""
IFS= read -r SCORES_FILE < "${TMPDIR:-/tmp}/vitality-scores-file-${CSID}" 2>/dev/null || SCORES_FILE=""
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/assemble_vitality_scores.py" \
    "$PARTIAL_A" "$PARTIAL_B" "$PARTIAL_C" "$SCORING_FILE" "$SCORES_FILE"  # timeout: 15000
```

Extract variables from `$SCORES_FILE` for use in Steps 4–7:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# reload SCORES_FILE (Check 41)
IFS= read -r SCORES_FILE < "${TMPDIR:-/tmp}/vitality-scores-file-${CSID}" 2>/dev/null || SCORES_FILE=""
eval "$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/extract_vitality_vars.py" "$SCORES_FILE")"  # timeout: 5000
echo "[vitality] scorer complete: health=${HEALTH_SCORE_PCT}% conf=${OVERALL_CONFIDENCE} passes=${TOTAL_PASSES}"
```

TaskUpdate "Step 3 Assemble Scores" completed.

## Step 4 — Report Generation

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# reload GH_OWNER/GH_REPO (Check 41) — empty pair names an unfindable report
IFS= read -r GH_OWNER < "${TMPDIR:-/tmp}/vitality-gh-owner-${CSID}" 2>/dev/null || GH_OWNER=""
IFS= read -r GH_REPO < "${TMPDIR:-/tmp}/vitality-gh-repo-${CSID}" 2>/dev/null || GH_REPO=""
# --quick: reload from Step1 flag parse (Check 41) — skips Steps 5+6 (codex review, adversarial loop)
IFS= read -r QUICK_MODE < "${TMPDIR:-/tmp}/analyse-quick-mode-${CSID}" 2>/dev/null || QUICK_MODE="false"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/build_vitality_paths.py" --owner "$GH_OWNER" --repo "$GH_REPO" --quick "$QUICK_MODE"  # timeout: 10000
```

> `--quick` note: in quick mode the single-pass scorecard is un-reviewed — confidence is capped lower and the report's Adversarial Review section records "skipped (--quick)". Rerun without `--quick` for a merge/release-grade assessment.

Resolve template path (installed cache first, source tree fallback):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# reload _OSS_ANALYSE (Check 41; cold-resolved once by parent analyse/SKILL.md)
IFS= read -r _OSS_ANALYSE < "${TMPDIR:-/tmp}/analyse-oss-analyse-${CSID}" 2>/dev/null || _OSS_ANALYSE="$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/resolve_shared_path.py" oss skills/analyse 2>/dev/null)"
[ -z "$_OSS_ANALYSE" ] && _OSS_ANALYSE="plugins/cc_oss/skills/analyse"
REPORT_TPL="$_OSS_ANALYSE/templates/vitality-report.md"
```

**Structural signals (optional, codemap)** — populate the template's `### Structural Constraints` block from index-wide data:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# reload _OSS_ANALYSE (Check 41)
IFS= read -r _OSS_ANALYSE < "${TMPDIR:-/tmp}/analyse-oss-analyse-${CSID}" 2>/dev/null || _OSS_ANALYSE="$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/resolve_shared_path.py" oss skills/analyse 2>/dev/null)"
[ -z "$_OSS_ANALYSE" ] && _OSS_ANALYSE="plugins/cc_oss/skills/analyse"
cat "$_OSS_ANALYSE/modes/codemap-signals.md"  # timeout: 5000
```

Run its **Detect** block (loaded above), then **Signal B** (open-PR conflict/duplicate candidates) and **Signal C** (Structural Constraints). Signal B fetches changed-file lists for open PRs (bounded by `PR_FILES_CAP`), does pairwise file overlap, and — when `CM_ENABLED=true` — adds `coupled`-based hidden-conflict pairs; surface `PRSET_CANDIDATES` pairs in report. Signal C uses parsed `central[]` / `collision_count` / `degraded` / `stale` to fill template's `### Structural Constraints` bullets. `CM_ENABLED=false`: Signal B still runs direct file-overlap layer (no codemap needed); Signal C fills Structural Constraints block with single "structural index unavailable" bullet — never leave empty, never block.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
mkdir -p .reports/analyse/vitality  # timeout: 5000
# reload _OSS_ANALYSE/REPORT_TPL (Check 41)
IFS= read -r _OSS_ANALYSE < "${TMPDIR:-/tmp}/analyse-oss-analyse-${CSID}" 2>/dev/null || _OSS_ANALYSE="$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/resolve_shared_path.py" oss skills/analyse 2>/dev/null)"
[ -z "$_OSS_ANALYSE" ] && _OSS_ANALYSE="plugins/cc_oss/skills/analyse"
REPORT_TPL="$_OSS_ANALYSE/templates/vitality-report.md"
cat "$REPORT_TPL"  # timeout: 5000
```

Full report structure (loaded above). Write `$REPORT_FILE` using that structure as scaffold — substitute every `{VARIABLE}` placeholder. Values come from three places, none of them interchangeable:

- `REPORT_FILE`, `REPORT_TIMESTAMP`, `SKILL_VERSION`, `REPORT_COMMIT`, `CODEX_AVAILABLE`, `REPORT_AGENTS_YAML` — read off `build_vitality_paths.py` stdout in Step 4 as `KEY=value` lines. `REPORT_AGENTS_YAML` is the block between `REPORT_AGENTS_YAML<<END` and the closing `END`, inserted verbatim. These are **not** shell variables; `$SKILL_VERSION` expands to nothing.
- `GH_OWNER`, `GH_REPO` — shell variables, re-read from sentinels at the top of Step 4.
- `TOTAL_PASSES`, `CONFIDENCE_HISTORY` — filled from the Step 6 loop as prose.

The header line `**Skill:** oss:analyse v{SKILL_VERSION} · **Commit:** {REPORT_COMMIT} · **Generated:** {REPORT_TIMESTAMP}` is gated by `hooks/enforce-analyse-header.js` and ships to the user — a blank value fails the gate. Do not print full analysis to terminal.

## Step 5 — Codex Independent Repo Review · Step 6 — Adversarial Rework Loop

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r QUICK_MODE < "${TMPDIR:-/tmp}/analyse-quick-mode-${CSID}" 2>/dev/null || QUICK_MODE="false"
```

**When `$QUICK_MODE` = `true`**: skip both steps entirely — do NOT read either mode file below. Set the report's Independent Codex Review section to "skipped (--quick)" and replace the `## Adversarial Review` placeholder with "skipped (--quick) — single-pass scorecard, un-reviewed; rerun without --quick for a reviewed assessment". Proceed directly to Step 7.

**Otherwise**:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# reload _OSS_ANALYSE (Check 41)
IFS= read -r _OSS_ANALYSE < "${TMPDIR:-/tmp}/analyse-oss-analyse-${CSID}" 2>/dev/null || _OSS_ANALYSE="$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/resolve_shared_path.py" oss skills/analyse 2>/dev/null)"
[ -z "$_OSS_ANALYSE" ] && _OSS_ANALYSE="plugins/cc_oss/skills/analyse"
REVIEW_DIR=".reports/analyse/vitality/$(date +%Y-%m-%d)-review"
REWORK_ITER=0
REWORK_MAX=2
CODEX_STATUS=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/check_bridge.py" --status 2>/dev/null || echo "absent")  # timeout: 5000
[ "$CODEX_STATUS" = "available" ] && CODEX_AVAILABLE=1 || CODEX_AVAILABLE=0
echo "$REVIEW_DIR" > "${TMPDIR:-/tmp}/vitality-review-dir-${CSID}"
echo "$REWORK_ITER" > "${TMPDIR:-/tmp}/vitality-rework-iter-${CSID}"
echo "$REWORK_MAX" > "${TMPDIR:-/tmp}/vitality-rework-max-${CSID}"
echo "$CODEX_AVAILABLE" > "${TMPDIR:-/tmp}/vitality-codex-available-${CSID}"
cat "$_OSS_ANALYSE/modes/vitality-codex-review.md"  # timeout: 5000
```

Execute its steps (Step 5), then:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# reload _OSS_ANALYSE (Check 41)
IFS= read -r _OSS_ANALYSE < "${TMPDIR:-/tmp}/analyse-oss-analyse-${CSID}" 2>/dev/null || _OSS_ANALYSE="$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/resolve_shared_path.py" oss skills/analyse 2>/dev/null)"
[ -z "$_OSS_ANALYSE" ] && _OSS_ANALYSE="plugins/cc_oss/skills/analyse"
cat "$_OSS_ANALYSE/modes/vitality-adversarial-rework.md"  # timeout: 5000
```

Execute its steps (Step 6) — each returns here to the next in sequence.

> loads: vitality-codex-review.md
>
> loads: vitality-adversarial-rework.md

## Step 7 — Terminal Summary Output

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# reload _OSS_SHARED (Check 41; set by parent analyse/SKILL.md)
IFS= read -r _OSS_SHARED < "${TMPDIR:-/tmp}/analyse-oss-shared-${CSID}" 2>/dev/null || _OSS_SHARED=""
cat "$_OSS_SHARED/terminal-summaries.md"  # timeout: 5000
```

Compact block format (loaded above). File absent → warn "run /foundry:setup — printing plain terminal output instead."

**Hook-enforced**: `hooks/enforce-analyse-header.js` (PreToolUse on `AskUserQuestion`) denies SKILL.md Step 6a's follow-up question while the `$REPORT_FILE` written in Step 4 is missing or empty. A denial reading `oss:analyse report gate` means Step 4 never produced the report — write it, print the block below, then re-issue the question. The hook sees only whether the report exists, not whether the block was printed; this step remains the check for that.

Print compact block to terminal. Three sections: header, exec summary, simplified scorecard. Axis rows must appear in numeric order 1–9; never reorder by score, weight, or status:

```markdown
# Repo Vitality — {GH_OWNER}/{GH_REPO}
**Skill:** oss:analyse v{SKILL_VERSION} · **Commit:** {REPORT_COMMIT} · **Generated:** {REPORT_TIMESTAMP}
**Passes:** {TOTAL_PASSES}/5 · confidence: {OVERALL_CONFIDENCE} (history: {CONFIDENCE_HISTORY colons→commas})
```

_(Omit Passes line when TOTAL_PASSES=1 — no retry loop needed.)_

```markdown
---

## Executive Summary

{2–3 sentences: overall health verdict, single top strength, single top risk. Example: "Project is in healthy condition (72%) with strong CI/CD and responsive maintainers. Contributor bus factor of 1 is the primary risk — a single maintainer departure could stall development. Dependency update automation is absent, leaving security hygiene dependent on manual effort."}

**Health Score:** {XX}% {🟢/🟡/🔴} · {N} healthy · {N} warning · {N} critical · {N} unavailable (⚪)
_(When OVERALL_CONFIDENCE < 0.7 prefix this line with: `⚠ LOW CONFIDENCE ({OVERALL_CONFIDENCE:.2f}) — directional only`)_
**Aggregate:**   {XX}% (mean main + Codex) — omit line when CODEX_AVAILABLE=0
**Rework:**      {REWORK_ITER} iteration(s) — omit line when REWORK_ITER=0
**Top Risk:**    {single most urgent finding, one line}
→ {REPORT_FILE}

---

| # | Axis                 | Score | Status   | Key Signal |
|---|----------------------|-------|----------|------------|
| 1 | Responsiveness       | N.N   | 🟢/🟡/🔴 | median issue Xd, PR Xd; X% ≤7d |
| 2 | Maintenance activity | N.N   | 🟢/🟡/🔴 | last commit Xd, X commits/30d |
| 3 | Contributor health   | N.N   | 🟢/🟡/🔴 | bus factor N, retention X% |
| 4 | Issue & PR health    | N.N   | 🟢/🟡/🔴 | stale X%, close X, review cov X% |
| 5 | CI/CD & code quality | N.N   | 🟢/🟡/🔴 | N/5 checks, CI pass X% |
| 6 | Documentation        | N.N   | 🟢/🟡/🔴 | N/9 checkpoints |
| 7 | Governance           | N.N   | 🟢/🟡/🔴 | N/7 files, active maint X/Y |
| 8 | Security posture     | N.N   | 🟢/🟡/🔴 | dep-config: yes/no, alerts: N or 403 |
| 9 | Trajectory           | N.N   | 🟢/🟡/🔴 | pool ±N%, TTM Xd→Yd, P90 Zd, dep-bump X% |
|   | **Total Score**      | **XX%** | 🟢/🟡/🔴 | — |

---
```

For ⚪ axes: show `--` in Score/Status columns; append below closing `---`:

```text
⚠ Axis {N} ({name}, wt {X}%) unavailable — score normalized over {M}/9 axes
```

If Axis 3 specifically ⚪: `⚠ Axis 3 (contributor health, wt 10%) unavailable — rerun in 5–10 min for full score`.

**Post-table validation (mandatory)**: after printing the scorecard, verify: (a) exactly 9 data rows appear with axis numbers 1–9, (b) no axis number repeated. Any duplicate or omission = immediately reprint the corrected full table before any other output. Never omit an axis row — even when data missing, show `--` in Score/Status.

Block must begin with `# Repo Vitality — {GH_OWNER}/{GH_REPO}` title and close with `---` on own line. Do not print full analysis to terminal. Full Conf/Weight columns and per-axis detail in report file only.

</workflow>

<notes>

- **Parallel scoring**: Group A (Axes 1,2,5,6), Group B (Axes 4,7,8), Group C (Axes 3→9) run simultaneously. Each reads DATA_FILE independently — no shared state between scorer agents. Assembler merges after all 3 complete.
- **Rework loop**: max 2 iterations. Rework agents get MINIMAL context — only section content + raw data + rubric + reviewer issue. No full report history passed. Prevents anchoring on prior flawed reasoning.
- **Fresh adversarial agents each iteration**: spawn new Agent() each rework cycle — prior iteration's reviewer findings must NOT be in new reviewer's context. Independent assessment is the point.
- **Adversarial review mandatory in full mode** — Step 6 always runs; `foundry:challenger` always spawned; the complete repository, report path, nine-axis review criteria, output path, permission boundary, and return contract from `vitality-adversarial-rework.md` are passed to `bridge:review` when `CODEX_AVAILABLE=1`. Only skip path is `--quick` (QUICK_MODE=true), which bypasses Steps 5 and 6 for fast daily scorecard at lower confidence.
- **Parallel group discipline**: Group 2 data fetches in gh-scraper only after Group 1 resolves (needs root file list and default_branch); scoring Groups A/B/C have no such dependency — all read from DATA_FILE independently
- **Data reuse**: root-contents fetch shared by Axes 6 and 7; releases fetch shared by Axis 2 and security signals; contributor stats weeks[] shared by Axis 3 and sub-signal 9A — all written to DATA_FILE, each scorer reads what it needs
- **--limit caps and truncation detection**: all limits set to target+1 (e.g. `--limit 501` for open issues targeting 500); if response length == limit, truncation occurred — JSONL record has `"partial": true`; scorer degrades confidence accordingly
- **Duplicate clustering**: flag DUPLICATE only when root = same problem (identical error/feature ask/root cause); flag RELATED when same component, distinct problems — do not conflate
- **Discussions API**: GraphQL `discussions(first:100)` sufficient for health snapshot; full pagination not needed
- **Stats 202 retry**: contributor stats endpoint returns 202 on first call for large repos — gh-scraper retries up to 6× with 10s sleep; if still 202, writes partial record; scorer Group C handles fallback from `commits_50`
- **403 on security APIs**: Dependabot and secret scanning require push access; 403 = expected; scorer Group B applies partial scoring for Axis 8; confidence 0.4; never ⚪ solely from Dependabot 403
- **Axis 1 response time**: responses by issue/PR author do not count — only first non-author comment/review contributes to response time computation
- **Code-review coverage (Axis 4)**: bot-submitted PRs (Dependabot, Renovate) excluded from both numerator and denominator — bot PRs cannot be "reviewed" in human sense and distort coverage rate
- **Star velocity**: advisory only — excluded from numeric score; page loop stops at 180d boundary via `$CUTOFF_180D`; if coverage < 30 days of stars when loop ends, mark 8B ⚪; partial data (≥30d coverage but \<180d) → note truncation and use available window for trend
- **Package registry 404**: skip sub-signal C silently — not all repos publish to PyPI/npm
- **Axis independence**: failure of one axis (API unavailable, access denied, computing) → ⚪ row in scorecard, continue with remaining axes; never block report on single axis failure
- **Codex independent review (Step 5)**: runs before adversarial review — codex assesses raw data independently, not main report; produces parallel scorecard and divergence notes; aggregate health score = mean(main, codex); when CODEX_AVAILABLE=0, note "codex unavailable — single-pass analysis only" in report section
- **bridge availability check**: `bin/check_bridge.py --status` — run before spawn; only `available` qualifies, `disabled` and `absent` both mean no bridge, and never assume the bridge is present
- **Health Score footer row**: Score column shows weighted %; Weight column shows "100%"; Status/Key Signal/Risk left blank
- **Rework loop exit conditions**: exits when `$REWORK_VERDICT = "pass"` OR `$REWORK_ITER >= $REWORK_MAX`; always exits after 2 iterations max regardless of verdict
- **SCORES_FILE**: assembled in Step 3 by orchestrator from 3 partial files — not written by gh-scraper; gh-scraper prompt in Step 1 does NOT include SCORES_FILE
- **CI pass-rate denominator**: always `success / total` (full denominator); never trim to success/conclusive; report `action_required` runs as separate "workflow auth failures" note — never exclude from denominator; inconsistent denominators break cross-repo Health Score comparison
- **Dependabot manifest_path classification**: before classifying alert as runtime user exposure, check `manifest_path` — `*_test.txt`, `**/test*.txt`, `**/dev*.txt`, `**/ci*.txt` = test/CI deps, not user-facing; GitHub `scope=runtime` field is unreliable for extras classification; actual runtime exposure = alert in file referenced by published extras_require; always split security finding: "N user-facing (extras/*.txt)" vs "M dev/CI-facing (*\_test.txt)"
- **CODEOWNERS activity verification**: compute CODEOWNERS active-maintainer count programmatically from commit-author data (`commits.json`/stats); never enumerate by name recognition; inactive = 0 commits in window; still counts as CODEOWNERS member but labelled "nominal"; correct list must include all commit authors who appear in CODEOWNERS, exclude those with 0 commits
- **Issue truncation framing**: when `open_issues.json` size == (`open_issues_count` − open PRs count), sample IS full population — state "all N open issues sampled"; reserve "N-cap window" framing for genuinely truncated samples only (response size == fetch limit); check: `sample_size = len(open_issues.json)`, `population = repo_meta.open_issues_count - open_prs_count`, if `sample_size >= population` → no truncation
- **Workflow count reconciliation**: when API `total_count` differs from count of YAML files in `.github/workflows/`, reconcile — API includes archived/disabled; filesystem is active only; report as "N active (M registered)" never a single ambiguous number; SAST/security claims must use filesystem count (active workflows only)
- **Bus factor confidence discounting**: when Axis 3 conf < 0.70 (e.g. stats 202 fallback), surface explicitly — add "X% of Health Score sits on low-confidence Axis 3" note; when conf ≤ 0.50, treat as partial contribution (effective weight × conf); never carry full axis weight at conf 0.50
- **TTFR primary metric**: headline responsiveness metric is % of issues/PRs with zero response (silence rate); TTFR secondary — characterises responded sub-sample only; always append "(responded only, N=X)" to TTFR figure; in scorecard row show silence rate first, TTFR second; for non-responded issues TTFR = ∞, not omitted
- **Commit denominator discipline**: always use non-bot count as denominator for per-author concentration (bus factor, contributor share); phrase as "X% of non-bot commits"; never "X% of last N commits" when bots inflate N; applies to: Axis 3 bus factor, committer counts, all contributor-share metrics
- **Skill version in Health Score**: record `SKILL_VERSION` in report header; when comparing Health Scores across two reports, warn if skill versions differ — axis weights may have changed; format: "Health Score 57.7% (v0.7.1)" not bare "57.7%"; never compare bare scores across runs with different skill versions

</notes>
