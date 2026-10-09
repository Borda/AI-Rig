<!-- file: vitality-adversarial-rework.md — consumers: analyse/modes/vitality.md (Step 6 pointer, QUICK_MODE-gated) -->

## Step 6 — Adversarial Rework Loop

After Step 5 aggregation complete — report includes main analysis + Codex independent review + divergence resolution. Adversarial reviewers assess **complete combined report** iteratively; rework applied between iterations

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# REVIEW_DIR/REWORK_ITER/REWORK_MAX/CODEX_AVAILABLE persisted by vitality.md Otherwise block — rehydrate, do not redefine
IFS= read -r REVIEW_DIR < "${TMPDIR:-/tmp}/vitality-review-dir-${CSID}" 2>/dev/null || REVIEW_DIR=".reports/analyse/vitality/$(date +%Y-%m-%d)-review"
IFS= read -r CODEX_AVAILABLE < "${TMPDIR:-/tmp}/vitality-codex-available-${CSID}" 2>/dev/null || CODEX_AVAILABLE="0"
IFS= read -r REWORK_ITER < "${TMPDIR:-/tmp}/vitality-rework-iter-${CSID}" 2>/dev/null || REWORK_ITER="0"
IFS= read -r REWORK_MAX < "${TMPDIR:-/tmp}/vitality-rework-max-${CSID}" 2>/dev/null || REWORK_MAX="2"
_OSS_SHARED=$(ls -d ~/.claude/plugins/cache/borda-ai-rig/oss/*/skills/_shared 2>/dev/null | sort -V | tail -1)  # timeout: 5000
[ -z "$_OSS_SHARED" ] && _OSS_SHARED="plugins/cc_oss/skills/_shared"
REWORK_SECTIONS=""
```

**Iteration loop** (repeat up to `$REWORK_MAX` times):

### 6a — Adversarial Review (fresh spawn each iteration)

Spawn reviewers simultaneously in single response — each writes to own iter-indexed file. No shared input between reviewers.

**When CODEX_AVAILABLE=1**: spawn `foundry:challenger` and call the bridge review simultaneously:

1. `foundry:challenger` — reads `$REPORT_FILE`; stress-tests scoring thresholds, flags weak evidence, challenges causality claims, verifies limit-hit detection, checks coverage gate logic; flags shared blind spots between main analysis and Codex independent review, or unconvincing divergence resolution; assesses all 9 axes including Axis 9 Trajectory. Writes findings to `$REVIEW_DIR/challenger-iter${REWORK_ITER}.md` (Write tool). Writes sentinel `$REVIEW_DIR/challenger-iter${REWORK_ITER}.done`. After narrative findings, writes machine-readable block on own line:

   ```
   REWORK_JSON: {"verdict":"pass"}
   ```

   OR

   ```
   REWORK_JSON: {"verdict":"needs_rework","items":[{"axis":N,"section":"<heading>","issue":"<specific claim that needs correction>","severity":"critical|high|medium"}]}
   ```

   Only flag `verdict=needs_rework` when finding is factually wrong or unsupported by evidence — not style/emphasis differences. Returns compact JSON envelope only.

2. `Skill(skill="bridge:review", args="Read $REPORT_FILE independently for repository $GH_OWNER/$GH_REPO. Do not read challenger output and do not edit source files. Review evidence quality, threshold calibration, data gaps, scoring edges, divergence resolution, and the nine named axes: responsiveness, maintenance activity, contributor health, issue and pull-request health, CI/CD and code quality, documentation, governance, security posture, and trajectory. Write actionable findings to $REVIEW_DIR/codex-iter${REWORK_ITER}.md, then write $REVIEW_DIR/codex-iter${REWORK_ITER}.done. Return a compact envelope naming the output file, finding count, blockers, and confidence.")`

**When CODEX_AVAILABLE=0**: spawn `foundry:challenger` only (step 1 above; skip step 2).

After spawning, verify sentinels:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r REVIEW_DIR < "${TMPDIR:-/tmp}/vitality-review-dir-${CSID}" 2>/dev/null || REVIEW_DIR=".reports/analyse/vitality/$(date +%Y-%m-%d)-review"
IFS= read -r REWORK_ITER < "${TMPDIR:-/tmp}/vitality-rework-iter-${CSID}" 2>/dev/null || REWORK_ITER="0"
IFS= read -r CODEX_AVAILABLE < "${TMPDIR:-/tmp}/vitality-codex-available-${CSID}" 2>/dev/null || CODEX_AVAILABLE="0"
[ -f "$REVIEW_DIR/challenger-iter${REWORK_ITER}.done" ] || { echo "⚠ challenger iter${REWORK_ITER} did not complete"; CHALLENGER_ITER_OUT=""; }
[ "$CODEX_AVAILABLE" = "1" ] && { [ -f "$REVIEW_DIR/codex-iter${REWORK_ITER}.done" ] || CODEX_ITER_OUT=""; }
```

Parse REWORK_JSON from challenger output:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r REVIEW_DIR < "${TMPDIR:-/tmp}/vitality-review-dir-${CSID}" 2>/dev/null || REVIEW_DIR=".reports/analyse/vitality/$(date +%Y-%m-%d)-review"
IFS= read -r REWORK_ITER < "${TMPDIR:-/tmp}/vitality-rework-iter-${CSID}" 2>/dev/null || REWORK_ITER="0"
REWORK_JSON=$(grep "^REWORK_JSON:" "$REVIEW_DIR/challenger-iter${REWORK_ITER}.md" 2>/dev/null | sed 's/^REWORK_JSON: //')
REWORK_VERDICT=$(echo "$REWORK_JSON" | jq -r '.verdict // "pass"' 2>/dev/null || echo "pass")  # timeout: 5000
```

### 6b — Rework (only when REWORK_VERDICT=needs_rework)

If `$REWORK_VERDICT` = `needs_rework` AND `$REWORK_ITER` < `$REWORK_MAX`:

For each item in rework list (parsed from `$REWORK_JSON`):

1. Extract specific section from `$REPORT_FILE` — grep from section heading to next `##` heading
2. Extract relevant axis data from `$DATA_FILE` — `type` record(s) for that axis from JSONL
3. Identify axis rubric section from the axis's group file in `$_OSS_SHARED` — `vitality-scoring-group-a.md` (Axes 1, 2, 5, 6), `vitality-scoring-group-b.md` (4, 7, 8), `vitality-scoring-group-c.md` (3, 9); `vitality-scoring.md` holds only the Weights table and shared rules

Axis `score`, label and `conf` are extractor-owned (`bin/vitality_extract.py`, copied verbatim by oss:repo-warden; SCORES_FILE Health Score assembled from them in Step 3) — rework never changes them, even when a reviewer disputes one. A disputed value stays as computed: the revised section explains the dispute in its notes, and the dispute is reported as an extractor/rubric issue in the Adversarial Review block (6c).

Spawn FRESH rework agent per flagged section with MINIMAL context (no report history, no prior iteration findings):

> **Agent waits** — SKILL.md §Health monitoring (batch `adversarial`, rewritten per iteration); never `ScheduleWakeup`, `ListAgents` or a `Monitor` loop.

```
Agent(subagent_type="foundry:sw-engineer", prompt="""
You are a technical writer revising one section of a vitality analysis report.
REPO: {GH_OWNER}/{GH_REPO}
AXIS: {axis N — axis name}
SECTION TO REVISE (current content):
---
{section_content}
---
REVIEWER ISSUE: {item.issue}
RAW DATA for this axis (from GitHub API):
{axis_specific_data_from_DATA_FILE}
SCORING RUBRIC for this axis:
{axis_N_section_from_its_vitality_scoring_group_file}

Instructions: Rewrite the section to address the reviewer's issue. Use only the raw data provided above — do not introduce claims unsupported by this data. Preserve the existing markdown format (headings, bold labels, evidence/impact/action structure). Never change the axis score, label or confidence — the extractor computed them and the Health Score is assembled from them. If the reviewer disputes one, keep the value and add a note naming the disputed value, the reviewer's reason and the raw data behind it, marked "extractor/rubric issue".

Write the revised section to {REVIEW_DIR}/axis_{axis_N}_iter{REWORK_ITER}.md using Write tool.
Return ONLY: {"status":"done","file":"<path>","axis":N}
""")
```

After all rework agents complete: patch `$REPORT_FILE` in-place — replace each original section with revised version via Edit tool (exact string match on section heading). Track revised sections in `$REWORK_SECTIONS`.

Increment `REWORK_ITER`:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r REWORK_ITER < "${TMPDIR:-/tmp}/vitality-rework-iter-${CSID}" 2>/dev/null || REWORK_ITER="0"
IFS= read -r REWORK_MAX < "${TMPDIR:-/tmp}/vitality-rework-max-${CSID}" 2>/dev/null || REWORK_MAX="2"
REWORK_ITER=$((REWORK_ITER + 1))
echo "$REWORK_ITER" > "${TMPDIR:-/tmp}/vitality-rework-iter-${CSID}"
```

If `$REWORK_ITER >= $REWORK_MAX` OR `$REWORK_VERDICT = "pass"`: exit loop.

Otherwise: return to 6a with new `Agent()` spawns (prior iteration's reviewer findings must NOT be in new reviewer's prompt — each adversarial spawn is fresh blank-context agent).

### 6c — Merge Adversarial Findings into Report

After loop exits (pass or max iterations): update `$REPORT_FILE` — replace placeholder `## Adversarial Review` block from Step 4 with final content via Edit tool:

```markdown
## Adversarial Review

**Rework iterations:** {REWORK_ITER} of {REWORK_MAX} maximum
{If REWORK_ITER > 0: "**Sections revised:** {REWORK_SECTIONS comma-separated}"}
{If a revised section marks an extractor/rubric issue: "**Disputed extractor values (unchanged):** axis N — {value} — {reason}" per dispute}

**Challenger:** {findings from $REVIEW_DIR/challenger-iter{final_iter}.md}

**Codex:** {findings from $REVIEW_DIR/codex-iter{final_iter}.md — or "codex unavailable — single adversarial pass only" when CODEX_AVAILABLE=0}
```

**TaskUpdate**: mark "Step 6 Adversarial Rework Loop" completed. If overall confidence from adversarial findings drops below 0.7 AND re-run of specific axes warranted, create new task "Step 3 re-score: {axis list}" and mark in_progress before re-fetching — keep task list current when confidence-driven reruns happen.

Returns to `vitality.md` Step 7 (terminal summary output).
