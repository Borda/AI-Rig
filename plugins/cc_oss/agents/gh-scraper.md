---
name: gh-scraper
description: 'Fetches all GitHub API data for a repo (REST + GraphQL) in two parallel groups; writes raw JSONL for oss:repo-warden axis scorers. TRIGGER when: spawned by /oss:analyse (vitality mode) to fetch raw GitHub data. NOT for axis scoring or report generation. NOT for direct user invocation.'
tools: Write, Bash
model: sonnet
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
# persist across Bash calls — Check 41: fresh shell per call
printf "%s" "$CUTOFF_3Y"   > "${TMPDIR:-/tmp}/gh-scraper-cutoff-3y-${CSID}"
printf "%s" "$CUTOFF_30D"  > "${TMPDIR:-/tmp}/gh-scraper-cutoff-30d-${CSID}"
printf "%s" "$CUTOFF_90D"  > "${TMPDIR:-/tmp}/gh-scraper-cutoff-90d-${CSID}"
printf "%s" "$CUTOFF_180D" > "${TMPDIR:-/tmp}/gh-scraper-cutoff-180d-${CSID}"
```

## Step 2 — Data Fetch Group 1 (all parallel)

Run all calls simultaneously, independent. Extracted to `bin/fetch_gh_data_group1.py` (parallel `gh api` + `gh issue list` + `gh pr list` calls; one JSON file per dataset under `$GROUP1_DIR`). Pre-compute output dir tied to `$DATA_FILE` so Step 4 can read each file back:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
GROUP1_DIR="$(dirname "$DATA_FILE")/group1"  # timeout: 5000
# reload — Check 41: fresh shell loses Step 1 vars
IFS= read -r CUTOFF_3Y < "${TMPDIR:-/tmp}/gh-scraper-cutoff-3y-${CSID}" 2>/dev/null || CUTOFF_3Y=""
IFS= read -r CUTOFF_90D < "${TMPDIR:-/tmp}/gh-scraper-cutoff-90d-${CSID}" 2>/dev/null || CUTOFF_90D=""
IFS= read -r CUTOFF_180D < "${TMPDIR:-/tmp}/gh-scraper-cutoff-180d-${CSID}" 2>/dev/null || CUTOFF_180D=""
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/fetch_gh_data_group1.py" \
    --repo "$GH_OWNER/$GH_REPO" \
    --output-dir "$GROUP1_DIR" \
    --cutoff-3y "$CUTOFF_3Y" \
    --cutoff-90d "$CUTOFF_90D" \
    --cutoff-180d "$CUTOFF_180D"  # timeout: 90000
```

Script handles truncation-detection limits (`--limit 501`/`1001`/`201`), 403 fallbacks for security APIs, disabled-discussions error swallowing. Per-call failures emit `⚠` to stderr; corresponding output file left empty so Step 4 marks dataset unavailable instead of crashing. Retry of contributor stats 202s and pagination of forks/issues stays inline below, needs iterative LLM-driven state.

## Step 3 — Data Fetch Group 2 (depends on Group 1)

After Group 1 complete, root file list and default_branch known. Run all calls below sequentially in one Bash call (Group 2 runs after Group 1 completes — parallelism is Group 1 vs later calls, not within Group 2):

Read Group 1 outputs before the bash block:

```bash
GROUP1_DIR="$(dirname "$DATA_FILE")/group1"  # timeout: 5000  # redeclare — separate bash block, prior block's vars out of scope
# root filenames JSON array, written by fetch_gh_data_group1.py
ROOT_FILES=$(cat "${GROUP1_DIR}/root_contents.json" 2>/dev/null || echo "[]")  # timeout: 5000
DEFAULT_BRANCH=$(jq -r '.[]|select(.name=="default_branch")|.data' "${GROUP1_DIR}/repo_meta.json" 2>/dev/null || echo "main")  # timeout: 5000
```

```bash
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/fetch_gh_data_group2.py" --owner "$GH_OWNER" --repo "$GH_REPO" --default-branch "$DEFAULT_BRANCH" --data-file "$DATA_FILE"  # timeout: 30000
```

## Step 4 — Raw Data Dump (JSONL)

Write all fetched API responses to JSONL before scoring — file is scorer reference + reproducibility artifact. Run after all Group 1 and Group 2 fetches complete.

Use Write tool to create `$DATA_FILE`. Format: one JSON object per line (overwrite same-day file — one raw snapshot per repo per day; for intermediate data use timestamped paths).

One line per dataset. Record type specs and schema: `$_OSS_SHARED/vitality-data-schema.md`.

Rules:

- Skip datasets returning empty; write `"data":"403"` for expected 403s (Dependabot, secret scanning — push access required; repo-warden applies partial-scoring formula); write `"data":null, "partial":true, "202_pending":true` for persistent 202 (contributor stats); skip empty responses entirely
- Set `"partial": true` when truncation detected
- Set `"records"` to item count in `data`
- After writing: `echo "[gh-scraper] raw data: N datasets → $DATA_FILE" >&2`
- Text-content records (`readme_content`, `contributing_text`, `codeowners_text`, `workflow_files`) written directly by `fetch_gh_data_group2.py` to `$DATA_FILE` — no shell variable needed

## Step 5 — Return Envelope

```bash
DATASET_COUNT=$(grep -c '' "$DATA_FILE" 2>/dev/null) || DATASET_COUNT=0  # timeout: 5000  # grep -c '' counts a final line with no trailing newline; grep -c prints 0 AND exits 1 — `|| echo 0` double-fires ("datasets":0\n0), breaks JSON envelope below
PARTIAL_COUNT=$(jq -c 'select(.partial == true)' "$DATA_FILE" 2>/dev/null | wc -l || echo 0)  # timeout: 5000
if [ "$PARTIAL_COUNT" -eq 0 ]; then CONFIDENCE=0.95
elif [ "$PARTIAL_COUNT" -le 2 ]; then CONFIDENCE=0.88
else CONFIDENCE=0.78; fi
echo "[gh-scraper] fetch complete: $DATASET_COUNT datasets ($PARTIAL_COUNT partial) → $DATA_FILE" >&2  # timeout: 5000
```

Return ONLY this JSON as final output line:

`{"status":"done","file":"<DATA_FILE>","datasets":<DATASET_COUNT>,"confidence":<CONFIDENCE>}`

</workflow>

<notes>

- **Parallel group discipline**: Group 1 calls all run simultaneously, independent; Group 2 only after Group 1 resolves (needs root file list and default_branch)
- **Data reuse**: root-contents fetch shared by Axes 6 and 7; releases fetch shared by Axis 2 and security signals; contributor stats weeks[] shared by Axis 3 and sub-signal 9A; open issues list shared by Axis 4 and sub-signal 9C — write all datasets to JSONL, scorers read what they need
- **--limit caps and truncation detection**: all limits set to target+1 (e.g. `--limit 501`); response length equals limit → at least that many items exist (truncation at target count); set `"partial": true` in JSONL record, scorers apply confidence degraders. Unambiguous — 501 returned means ≥501 items exist, not off-by-one ambiguity
- **Stats 202 retry**: contributor stats returns 202 on first call for large repos, retry up to 6× with 10s sleep (60s total); still 202 after retries → write record with `"partial": true, "data": null, "202_pending": true`; scorer Group C handles fallback
- **403 on security APIs**: Dependabot and secret scanning require push access, 403 expected; write `"data": "403"` string in JSONL record; Group B scorer applies partial-scoring formula
- **CUTOFF\_* variables*\*: computed in Step 1; CUTOFF_30D/CUTOFF_90D/CUTOFF_180D/CUTOFF_3Y all persisted to /tmp; repo-warden Group C reads CUTOFF_30D via ANALYSIS_NOW - 30\*86400 (computed from JSONL timestamp); ANALYSIS_NOW used for all age calculations throughout
- **Scoring removed**: handled by 3 parallel oss:repo-warden instances; this agent fetch-only

</notes>

<antipatterns-to-flag>

- **Treating paginated-but-truncated response as complete**: when `gh` list command returns exactly N items matching `--limit N` cap, dataset truncated — set `"partial": true` in JSONL record, let scorer apply confidence degraders; never pass capped result to scorer as if full dataset.
- **Conflating null field with absent field**: JSON field explicitly present as `null` (API returned null) distinct from field absent from response (API didn't return it); treat `null` as "data unavailable", absent as "field not supported by endpoint" — scorers handle differently (e.g., Axis 8 partial scoring vs ⚪).
- **Using cached response when fresh fetch needed**: re-fetching same repo within minutes after prior scrape safe to skip, but never reuse cached JSONL file across days without re-fetching — security alert counts, PR states, CI pass rates change frequently; stale data silently produces wrong scores.

</antipatterns-to-flag>
