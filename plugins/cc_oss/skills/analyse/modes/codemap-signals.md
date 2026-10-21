# Codemap structural signals (analyse)

> Optional. Gated on codemap plugin + fresh index. Absent → skip; caller flags "structural context unavailable", never blocks. codemap separate opt-in plugin (`requires codemap plugin`).

Consumers: `modes/thread.md` (issue triage — stale-symbol check), `modes/vitality.md` (Structural Constraints block). Both read `$_OSS_ANALYSE` (installed analyse skill dir, set by caller) and this file.

## Detect — sets `CM_ENABLED`

Reuse oss shared detector — same helper review/resolve use. codemap-py query on PATH AND index for this project both required.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# Resolve analyse skill dir if caller did not (sentinel cold-resolved by analyse/SKILL.md, then source fallback)
[ -n "$_OSS_ANALYSE" ] || IFS= read -r _OSS_ANALYSE < "${TMPDIR:-/tmp}/analyse-oss-analyse-${CSID}" 2>/dev/null || _OSS_ANALYSE=""
[ -z "$_OSS_ANALYSE" ] && _OSS_ANALYSE="plugins/cc_oss/skills/analyse"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/detect_codemap.py" --prefix analyse 2>&1  # timeout: 5000
IFS= read -r CM_ENABLED < "${TMPDIR:-/tmp}/analyse-codemap-enabled-${CSID}" 2>/dev/null || CM_ENABLED="false"
IFS= read -r CM_CURRENCY < "${TMPDIR:-/tmp}/analyse-codemap-currency-${CSID}" 2>/dev/null || CM_CURRENCY="no_index"
```

`CM_ENABLED=false` → caller emits inline flag (see below) and skips every codemap-py query block. No AskUserQuestion — analyse is read-only triage; degrade silently-but-flagged per accept criterion. `CM_ENABLED=true` + `CM_CURRENCY=stale` → detector already printed a stale warning; proceed with stale data, caller notes "index stale — signals may miss recent code".

**Inline flag when disabled** (caller prints once, in report + terminal): `> structural signals unavailable — codemap index absent (build via /codemap-py:scan-codebase, requires codemap plugin) or codemap-py query not installed`

## Signal A — stale-issue check (thread mode, issues/discussions only)

Thread names symbols/modules that may no longer exist. Extract candidate identifiers from thread body + comments, then existence-check via one `batch` process.

**Extract candidates** — dotted module paths (`a.b.c`) and CamelCase/snake symbol names appearing in backticks, tracebacks, or `import`/`from` lines. Cap at 8 candidates (highest-signal: those in code fences or import lines first). Skip stdlib/third-party names (`os`, `numpy`, `torch`, …) — only project-internal identifiers matter for staleness

**Existence-check** — write the extracted candidates one-per-line to `${TMPDIR:-/tmp}/analyse-triage-candidates-${CSID}.txt` (bash arrays do not survive a fresh shell — use a file), then build a batch of one query per candidate: module-shaped (`a.b.c`, has a dot) → `rdeps`; bare symbol → `find-symbol`. Run once:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# Caller wrote candidates (one identifier per line) to this file before this fence.
IFS= read -r CM_ENABLED < "${TMPDIR:-/tmp}/analyse-codemap-enabled-${CSID}" 2>/dev/null || CM_ENABLED="false"
_CAND="${TMPDIR:-/tmp}/analyse-triage-candidates-${CSID}.txt"
_BATCH="${TMPDIR:-/tmp}/analyse-triage-batch-${CSID}.json"
if [ "$CM_ENABLED" = "true" ] && [ -s "$_CAND" ]; then
    _BATCH_LEN=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/build_triage_batch.py" "$_CAND" "$_BATCH" 2>/dev/null || echo 0)  # timeout: 5000
    [ "${_BATCH_LEN:-0}" -gt 0 ] && codemap-py query batch "$_BATCH" 2>/dev/null  # timeout: 15000
fi
```

> `build_triage_batch.py` maps each candidate to `rdeps` (module) or `find-symbol` (symbol) — extracted to `bin/` per plugin authoring policy (no inline heredoc in skill bodies). `CM_ENABLED=false` or no candidates → whole block no-ops; empty batch (`[]`) skips the codemap-py query call.

**Interpret** each `batch[]` entry:

- `rdeps` result with `"error": "module not indexed"` → module named in thread no longer indexed → **stale-issue candidate**. `suggestions[]` = likely rename target; surface as "module `X` not found — possibly renamed to `Y`".
- `find-symbol` result with `"count": 0` (empty `matches`) → symbol gone → **stale-issue candidate**.
- Non-empty result → identifier still live; not stale. `rdeps` `count: 0` (indexed, zero importers) is NOT stale — module exists but unused; note only if thread claims it is used.

Set `STALE_ISSUE=true` when ≥1 candidate is a stale-issue hit; list the missing identifiers + suggested renames in the report's Analysis section and add `stale-symbols` to Suggested Labels. `query_complete: false` in the batch coverage block → append caveat "(codemap coverage partial — absence not conclusive)".

## Signal B — PR-set conflict/duplicate candidates (vitality mode)

Open PRs touching overlapping code are merge-conflict or duplicate-effort risks. Two layers

1. **Direct overlap** (no codemap needed) — pairwise intersection of changed-file lists. Shared file(s) between two open PRs → conflict/duplicate candidate.
2. **Structural overlap** (codemap-py) — two PRs touch *different* files that map to *tightly coupled* modules → hidden-conflict candidate the file-name intersection misses.

**Bounded fetch** — `open_prs` from gh-scraper carries no `files` field; fetch changed-file name-lists here, but only when the open-PR count is small enough to stay within rate limits:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# Cap: skip when > PR_FILES_CAP open PRs (file lists grow with PR count).
IFS= read -r GH_OWNER < "${TMPDIR:-/tmp}/analyse-gh-owner-${CSID}" 2>/dev/null || GH_OWNER=""
IFS= read -r GH_REPO < "${TMPDIR:-/tmp}/analyse-gh-repo-${CSID}" 2>/dev/null || GH_REPO=""
[ -n "$GH_OWNER" ] && [ -n "$GH_REPO" ] || { echo "⚠ PR-set overlap skipped — GH_OWNER/GH_REPO sentinels missing"; exit 0; }
PR_FILES_CAP=25
_OPEN_PR_NUMS=$(gh pr list -R "$GH_OWNER/$GH_REPO" --state open --json number --jq '.[].number' --limit 201 2>/dev/null)  # timeout: 15000
_OPEN_PR_COUNT=$(printf '%s\n' "$_OPEN_PR_NUMS" | grep -c . 2>/dev/null) || _OPEN_PR_COUNT=0  # `|| echo 0` appends a second 0: grep -c prints 0 *and* exits 1 on zero matches
_PRSET="${TMPDIR:-/tmp}/analyse-prset-files-${CSID}.jsonl"
: > "$_PRSET"
if [ "$_OPEN_PR_COUNT" -gt 0 ] && [ "$_OPEN_PR_COUNT" -le "$PR_FILES_CAP" ]; then
    python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/gh_batch_query.py" pr-files --repo "$GH_OWNER/$GH_REPO" $_OPEN_PR_NUMS > "$_PRSET" || { echo "⚠ PR-set overlap skipped — batched changed-file fetch failed"; : > "$_PRSET"; }  # timeout: 30000
fi
```

One batched GraphQL request replaces one `gh pr view` per PR. A record with `coverage_gap` (PR vanished, or more than 100 changed files) is a partial file list: still intersect it, and append "(file list partial for #N)" to any candidate pair it supports.

> `> PR_FILES_CAP` open PRs → skip the whole signal; report notes "PR-set overlap skipped — {N} open PRs exceeds cap {PR_FILES_CAP} (per-PR file fetch too costly)". Keeps vitality within GitHub rate limits on high-traffic repos.

**Direct overlap** — for each unordered PR pair in `$_PRSET`, intersect `files`; non-empty intersection → candidate. Report: "PRs #A and #B both touch `path` — conflict/duplicate candidate."

**Structural overlap** (only when `CM_ENABLED=true`) — derive each PR's touched modules from its `.py` file paths, then check whether any module pair across two PRs appears together in `coupled --top 30`:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r CM_ENABLED < "${TMPDIR:-/tmp}/analyse-codemap-enabled-${CSID}" 2>/dev/null || CM_ENABLED="false"
[ "$CM_ENABLED" = "true" ] && codemap-py query coupled --top 30 --exclude-tests 2>/dev/null > "${TMPDIR:-/tmp}/analyse-cm-coupled-${CSID}.json"  # timeout: 10000
```

Parse `coupled[]` (each has `name`, `dep_count`). Two PRs whose modules are both high-coupling (`dep_count` ≥ 20) but share no files → "PRs #A and #B touch tightly-coupled modules (`m1`/`m2`) — review together even though file sets differ." `query_complete: false` → append "(coupling scan partial)".

Set `PRSET_CANDIDATES` = count of direct + structural candidate pairs; surface the pairs in the vitality report's Structural Constraints or a dedicated "Open-PR conflicts" note. Zero pairs → "no overlapping open PRs detected."

## Signal C — Structural Constraints (vitality mode)

Populate the report template's `### Structural Constraints` block from index-wide signals — not per-thread. Run once during vitality assembly:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r CM_ENABLED < "${TMPDIR:-/tmp}/analyse-codemap-enabled-${CSID}" 2>/dev/null || CM_ENABLED="false"
[ "$CM_ENABLED" = "true" ] || { echo "cm_central=[] cm_collision=0 cm_degraded=0"; }
if [ "$CM_ENABLED" = "true" ]; then
    codemap-py query central --top 5 2>/dev/null > "${TMPDIR:-/tmp}/analyse-cm-central-${CSID}.json"  # timeout: 10000
fi
```

Parse `${TMPDIR:-/tmp}/analyse-cm-central-${CSID}.json`:

- `central[]` → top-5 highest-blast-radius modules (name + rdep_count). Emit a bullet: "Highest blast radius: `mod` (N reverse-deps) — changes here ripple widest; weight review effort accordingly."
- coverage block `collision_count` (>0) → bullet: "N symbol-name collisions in the index — `find-symbol`/rename precision reduced for those names."
- coverage block `degraded: true` → bullet: "index built in degraded mode — some structural signals approximate."
- `stale: true` or `CM_CURRENCY=stale` → bullet: "index stale relative to working tree — structural figures lag recent commits."

`CM_ENABLED=false`: emit a single bullet "structural index unavailable — blast-radius / collision signals not computed (codemap plugin absent or no index)" and omit the rest. These are permanent-for-this-run constraints, matching the template's "will not resolve by re-running" framing.
