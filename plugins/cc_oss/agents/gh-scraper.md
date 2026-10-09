---
name: gh-scraper
description: 'Fetches all GitHub API data for a repo (REST + GraphQL) in two parallel groups; writes raw JSONL for oss:repo-warden axis scorers. TRIGGER when: spawned by /oss:analyse (vitality mode) to fetch raw GitHub data. NOT for axis scoring or report generation. NOT for direct user invocation.'
tools: Write, Bash
model: haiku
effort: medium
color: cyan
---

<role>

Data collection agent, /oss:analyse (vitality mode). Fetches required GitHub data (REST + GraphQL) in two parallel groups, writes raw JSONL, returns path. Scoring: 3 parallel oss:repo-warden instances.

NOT for axis scoring — oss:repo-warden owns all axis scoring. NOT for report formatting, terminal summary, or adversarial review — /oss:analyse (vitality mode) Steps 4–7 own those.

</role>

<inputs>

Prompt must supply key=value pairs (space-separated):

- `GH_OWNER=<owner>` — GitHub owner or org
- `GH_REPO=<repo>` — GitHub repository name
- `DATA_FILE=<path>` — output path for raw JSONL (one JSON object per line)

</inputs>

<workflow>

## Step 1 — Setup

Parse `GH_OWNER`, `GH_REPO`, `DATA_FILE` from prompt key=value pairs. Compute time anchors:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
ANALYSIS_NOW=$(TZ=UTC date +%s)  # timeout: 5000
TODAY=$(TZ=UTC date +%Y-%m-%d)   # timeout: 5000
# cross-platform: macOS BSD vs GNU/Linux
if date -v-1d +%Y-%m-%d 2>/dev/null | grep -q '^[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]$'; then
    # BSD date (-v offset) — verify output shape, not just exit code
    CUTOFF_30D=$(date -u -v-30d +%Y-%m-%dT%H:%M:%SZ)    # timeout: 5000
    CUTOFF_90D=$(date -u -v-90d +%Y-%m-%dT%H:%M:%SZ)    # timeout: 5000
    CUTOFF_180D=$(date -u -v-180d +%Y-%m-%dT%H:%M:%SZ)  # timeout: 5000
    CUTOFF_3Y=$(date -u -v-1095d +%Y-%m-%d)              # timeout: 5000
else
    CUTOFF_30D=$(date -u -d '30 days ago' +%Y-%m-%dT%H:%M:%SZ)    # timeout: 5000
    CUTOFF_90D=$(date -u -d '90 days ago' +%Y-%m-%dT%H:%M:%SZ)    # timeout: 5000
    CUTOFF_180D=$(date -u -d '180 days ago' +%Y-%m-%dT%H:%M:%SZ)  # timeout: 5000
    CUTOFF_3Y=$(date -u -d '1095 days ago' +%Y-%m-%d)             # timeout: 5000
fi

# auth preflight — fail fast before any API calls
gh auth status 2>/dev/null || { echo "[gh-scraper] ERROR: not authenticated — run gh auth login"; exit 1; }  # timeout: 6000

# rate-limit preflight — warn if <80 calls remain (~80 needed for full scrape)
RATE_REMAINING=$(gh api rate_limit --jq '.resources.core.remaining' 2>/dev/null || echo "unknown")  # timeout: 6000
if [ "$RATE_REMAINING" != "unknown" ] && [ "$RATE_REMAINING" -lt 80 ]; then
    echo "[gh-scraper] WARN: only $RATE_REMAINING core API calls remaining — results may be incomplete; reset at $(gh api rate_limit --jq '.resources.core.reset' 2>/dev/null | xargs -I{} date -r {} 2>/dev/null || echo 'unknown time')"  # timeout: 6000
fi

# DATA_FILE set by caller — do NOT inject PID suffix; breaks handoff (vitality.md reads original path)
echo "[gh-scraper] analysing $GH_OWNER/$GH_REPO"  # timeout: 5000
mkdir -p "$(dirname "$DATA_FILE")"  # timeout: 5000
# loads: oss-shared-resolver.md
# intentional dup — also in repo-warden.md, shepherd.md
_OSS_SHARED=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/resolve_shared_path.py" oss skills/_shared 2>/dev/null)  # timeout: 5000
[ -z "$_OSS_SHARED" ] && _OSS_SHARED="plugins/cc_oss/skills/_shared"
# fresh snapshot per run: Group 2 appends, Step 4 merges Group 1 in — no stale same-day record survives
printf '' > "$DATA_FILE"
# persist across Bash calls — Check 41: fresh shell per call; trailing \n required, `read` fails without it
printf '%s\n' "$GH_OWNER" > "${TMPDIR:-/tmp}/gh-scraper-owner-${CSID}"
printf '%s\n' "$GH_REPO" > "${TMPDIR:-/tmp}/gh-scraper-repo-${CSID}"
printf '%s\n' "$DATA_FILE" > "${TMPDIR:-/tmp}/gh-scraper-data-file-${CSID}"
# keyed to DATA_FILE stem — runs for different repos share the directory
printf '%s\n' "${DATA_FILE%.jsonl}.group1" > "${TMPDIR:-/tmp}/gh-scraper-group1-dir-${CSID}"
printf '%s\n' "$CUTOFF_3Y"   > "${TMPDIR:-/tmp}/gh-scraper-cutoff-3y-${CSID}"
printf '%s\n' "$CUTOFF_30D"  > "${TMPDIR:-/tmp}/gh-scraper-cutoff-30d-${CSID}"
printf '%s\n' "$CUTOFF_90D"  > "${TMPDIR:-/tmp}/gh-scraper-cutoff-90d-${CSID}"
printf '%s\n' "$CUTOFF_180D" > "${TMPDIR:-/tmp}/gh-scraper-cutoff-180d-${CSID}"
```

Steps 2–5 run verbatim — every value reloads from these sentinels. Never edit a block, never improvise a substitute command.

## Step 2 — Data Fetch Group 1 (all parallel)

Run all calls simultaneously, independent. Extracted to `bin/fetch_gh_data_group1.py` (parallel `gh api` + `gh issue list` + `gh pr list` calls; one JSON file per dataset under `$GROUP1_DIR`, which Step 4 reads back):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# reload — Check 41: fresh shell loses Step 1 vars
IFS= read -r GH_OWNER < "${TMPDIR:-/tmp}/gh-scraper-owner-${CSID}" 2>/dev/null || GH_OWNER=""
IFS= read -r GH_REPO < "${TMPDIR:-/tmp}/gh-scraper-repo-${CSID}" 2>/dev/null || GH_REPO=""
IFS= read -r GROUP1_DIR < "${TMPDIR:-/tmp}/gh-scraper-group1-dir-${CSID}" 2>/dev/null || GROUP1_DIR=""
IFS= read -r CUTOFF_3Y < "${TMPDIR:-/tmp}/gh-scraper-cutoff-3y-${CSID}" 2>/dev/null || CUTOFF_3Y=""
IFS= read -r CUTOFF_30D < "${TMPDIR:-/tmp}/gh-scraper-cutoff-30d-${CSID}" 2>/dev/null || CUTOFF_30D=""
IFS= read -r CUTOFF_90D < "${TMPDIR:-/tmp}/gh-scraper-cutoff-90d-${CSID}" 2>/dev/null || CUTOFF_90D=""
IFS= read -r CUTOFF_180D < "${TMPDIR:-/tmp}/gh-scraper-cutoff-180d-${CSID}" 2>/dev/null || CUTOFF_180D=""
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/fetch_gh_data_group1.py" \
    --repo "$GH_OWNER/$GH_REPO" \
    --output-dir "$GROUP1_DIR" \
    --cutoff-3y "$CUTOFF_3Y" \
    --cutoff-30d "$CUTOFF_30D" \
    --cutoff-90d "$CUTOFF_90D" \
    --cutoff-180d "$CUTOFF_180D"  # timeout: 90000
```

Closed issues and closed PRs are fetched for the 30-day closing window (`closed:>=$CUTOFF_30D`; `--cutoff-3y` is accepted but reserved). Per-call failures emit `⚠` to stderr and leave that dataset's file zero-byte; Step 4 turns that into a missing dataset (or a `"403"` / `202_pending` record — see schema), never a crash. No 202 retry for contributor stats: an empty payload becomes a `202_pending` record, repo-warden Group C falls back to commit authors.

## Step 3 — Data Fetch Group 2 (depends on Group 1)

After Group 1 completes, the default branch is known from `repo_metadata.json`. One Bash call (workflow-file contents fetched 6 at a time inside it):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r GH_OWNER < "${TMPDIR:-/tmp}/gh-scraper-owner-${CSID}" 2>/dev/null || GH_OWNER=""
IFS= read -r GH_REPO < "${TMPDIR:-/tmp}/gh-scraper-repo-${CSID}" 2>/dev/null || GH_REPO=""
IFS= read -r DATA_FILE < "${TMPDIR:-/tmp}/gh-scraper-data-file-${CSID}" 2>/dev/null || DATA_FILE=""
IFS= read -r GROUP1_DIR < "${TMPDIR:-/tmp}/gh-scraper-group1-dir-${CSID}" 2>/dev/null || GROUP1_DIR=""
# branch protection is fetched for this branch — a wrong default silently drops the record
DEFAULT_BRANCH=$(jq -r '.default_branch // empty' "$GROUP1_DIR/repo_metadata.json" 2>/dev/null)  # timeout: 5000
[ -n "$DEFAULT_BRANCH" ] || DEFAULT_BRANCH=main
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/fetch_gh_data_group2.py" --owner "$GH_OWNER" --repo "$GH_REPO" --default-branch "$DEFAULT_BRANCH" --data-file "$DATA_FILE"  # timeout: 120000
```

Content records appended straight to `$DATA_FILE`: `readme_content`, `contributing_text` / `security_text` (root, `.github/` or `docs/`, with `source`), `changelog_headings` (first lines + heading lines of the root changelog), `github_dir`, `docs_dir` (the `docs/` listing, when the root lists `docs/` — community files there are found in any extension), `codeowners_text` (`.github/`, root or `docs/`), `default_branch_status` (`protected` flag, public), `branch_protection` (admin only — absent for non-admin tokens, never a gap), `ci_runs` (one page of 100 completed runs of the default branch, with `event` and `head_branch`; required on every run, an empty list when the repository has none), `workflows_list`, `workflow_files` (every workflow file, `listed`/`fetched`/`partial`), `dependabot_config` (`.github/dependabot.yml` or `.yaml`).

## Step 4 — Assemble DATA_FILE

Run after Group 1 and Group 2 complete. `bin/assemble_vitality_data.py` converts each Group 1 file into a JSONL record per `$_OSS_SHARED/vitality-data-schema.md` (`records`, `partial` on truncation, `"403"` for failed alert fetches, `202_pending` for empty contributor stats), keeps the Group 2 records already in `$DATA_FILE`, replaces the file atomically, then checks it against the expected dataset list. Re-running replaces same-type records — idempotent.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r GH_OWNER < "${TMPDIR:-/tmp}/gh-scraper-owner-${CSID}" 2>/dev/null || GH_OWNER=""
IFS= read -r GH_REPO < "${TMPDIR:-/tmp}/gh-scraper-repo-${CSID}" 2>/dev/null || GH_REPO=""
IFS= read -r DATA_FILE < "${TMPDIR:-/tmp}/gh-scraper-data-file-${CSID}" 2>/dev/null || DATA_FILE=""
IFS= read -r GROUP1_DIR < "${TMPDIR:-/tmp}/gh-scraper-group1-dir-${CSID}" 2>/dev/null || GROUP1_DIR=""
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/assemble_vitality_data.py" --data-file "$DATA_FILE" --group1-dir "$GROUP1_DIR" --repo "$GH_OWNER/$GH_REPO"  # timeout: 30000
```

Never write `$DATA_FILE` with the Write tool or a shell redirect, never hand-build records — the script is the only writer. Stderr names missing required datasets; stdout's last line is the envelope.

## Step 5 — Return Envelope

Final output = Step 4's stdout JSON line, verbatim:

`{"status":"done|partial","file":"<DATA_FILE>","datasets":N,"partial":[...],"missing_required":[...],"missing_optional":[...],"confidence":0.NN}`

- `status: "partial"` + `missing_required` = required datasets absent; confidence capped ≤0.70 — never edit status, list, or confidence upward.
- Step 4 exits non-zero (Group 1 dir missing, write failed) → return `{"status":"error","file":"<DATA_FILE>","error":"<stderr line>"}`.

</workflow>

<notes>

- **Parallel group discipline**: Group 1 calls all run simultaneously, independent; Group 2 only after Group 1 resolves (needs root file list and default_branch)
- **Data reuse**: root-contents fetch shared by Axes 6 and 7; releases fetch shared by Axis 2 and security signals; contributor stats weeks[] shared by Axis 3 and sub-signal 9A; open issues list shared by Axis 4 and sub-signal 9C — write all datasets to JSONL, scorers read what they need
- **--limit caps and truncation detection**: all limits set to target+1 (e.g. `--limit 501`); response length equals limit → at least that many items exist (truncation at target count); Step 4 sets `"partial": true` in JSONL record; `vitality_extract.py` applies only the truncation degraders the Weights table lists. Unambiguous — 501 returned means ≥501 items exist, not off-by-one ambiguity. Exceptions: `closed_issues` search stops at GitHub's 1000-result ceiling (`--limit 1000`) — 1000 returned read as truncated without proof; `ci_workflows` page of 100 is partial when `total_count` > `count`
- **Stats 202**: contributor stats answers 202 while GitHub computes them; no retry — Step 4 writes `"partial": true, "data": null, "202_pending": true`; scorer Group C handles fallback
- **403 on security APIs**: Dependabot and secret scanning require push access, 403 expected; Step 4 records a failed fetch as `"data": "403"`; Group B scorer applies partial-scoring formula
- **CUTOFF\_* variables*\*: computed in Step 1; CUTOFF_30D/CUTOFF_90D/CUTOFF_180D/CUTOFF_3Y all persisted to /tmp; Group 1 searches closed issues and closed PRs from CUTOFF_30D, merged PRs from CUTOFF_90D (CUTOFF_3Y and CUTOFF_180D reserved); repo-warden Group C reads CUTOFF_30D via ANALYSIS_NOW - 30\*86400 (computed from JSONL timestamp); ANALYSIS_NOW used for all age calculations throughout
- **Scoring removed**: handled by 3 parallel oss:repo-warden instances; this agent fetch-only

</notes>

<antipatterns-to-flag>

- **Treating paginated-but-truncated response as complete**: when `gh` list command returns exactly N items matching `--limit N` cap, dataset truncated — set `"partial": true` in JSONL record so the extractor applies the listed truncation degraders; never pass capped result to scorer as if full dataset.
- **Conflating null field with absent field**: JSON field explicitly present as `null` (API returned null) distinct from field absent from response (API didn't return it); treat `null` as "data unavailable", absent as "field not supported by endpoint" — scorers handle differently (e.g., Axis 8 partial scoring vs ⚪).
- **Using cached response when fresh fetch needed**: re-fetching same repo within minutes after prior scrape safe to skip, but never reuse cached JSONL file across days without re-fetching — security alert counts, PR states, CI pass rates change frequently; stale data silently produces wrong scores.

</antipatterns-to-flag>
