---
name: release
description: "Prepare release communication and check readiness. Main mode: notes with optional flags --changelog, --summary, --migration, --append (incremental: reruns the full pipeline scoped to newly-landed commits, integrating results into existing DRAFT.md/CHANGELOG.md/SUMMARY.md/MIGRATION.md instead of full regenerate — non-destructive except revert/pivot); range as v1->v2. Other modes: prepare (full pipeline: audit → all artifacts), audit (pre-release readiness: blockers, docs alignment, version consistency, CVEs), demo (story-telling release notebook in jupytext # %% format). TRIGGER when: user requests release notes, CHANGELOG entry, migration guide, internal summary, release readiness audit, release demo, or an incremental update to an already-drafted release; phrases: 'draft release notes', 'prepare release', 'audit release readiness', 'generate CHANGELOG for v1->v2', 'release demo notebook', 'update release notes with the latest commits'. SKIP: actual git tagging or PyPI/registry upload (use git tag, gh release create, twine upload directly); release communication for a non-Python project where this skill's pytest-centric audit assumptions do not apply; PR-level review (use /oss:review); thread/issue analysis (use /oss:analyse)."
argument-hint: '[notes] [v1->v2] [--changelog] [--summary] [--migration] [--append] | prepare <version> | audit [version] | demo [range]'
allowed-tools: Read, Write, Edit, Bash, TaskList, TaskCreate, TaskUpdate, Agent, AskUserQuestion, WebFetch
model: sonnet
effort: high
---

<objective>

Prepare release communication from changes. Output adapts to audience — user-facing notes, CHANGELOG entry, internal summary, migration guide.

**All outputs = documentation artifacts** (CHANGELOG.md, DRAFT.md, MIGRATION.md, SUMMARY.md, demo.py). Released product = code/package published separately via project tooling (`git tag`, `gh release create`, PyPI upload). Skill prepares communication; doesn't perform release.

NOT for ecosystem impact without release (use oss:analyse (requires `oss` plugin)). NOT for contributor communication or post-release announcements (use oss:shepherd (requires `oss` plugin)). NOT for retrospective analysis — historical review → oss:analyse (requires `oss` plugin).

</objective>

<inputs>

Mode comes **first**; range or flags follow:

| Invocation | Arguments | Writes to disk |
| -- | -- | -- |
| `/release [notes] [range]` | optional range (default: last-tag..HEAD); use `v1->v2` for explicit range | `DRAFT.md` |
| `/release notes [range] --changelog` | optional range + flag | `DRAFT.md` + prepends `CHANGELOG.md` |
| `/release notes [range] --summary` | optional range + flag | `DRAFT.md` + `.temp/output-release-summary-<branch>-<date>.md` |
| `/release notes [range] --migration` | optional range + flag | `DRAFT.md` + `.temp/output-release-migration-<branch>-<date>.md` |
| `/release notes [range] --changelog --summary --migration` | all flags | All four outputs |
| `/release notes --append` | no range (derived from last-processed marker); compose with `--changelog`/`--summary`/`--migration` | Runs the full pipeline scoped to the incremental range, integrating results into every existing artifact **in place** (`DRAFT.md` always; `CHANGELOG.md`/`SUMMARY.md`/`MIGRATION.md` when their flag is set) instead of regenerating from scratch |
| `/release prepare <version>` | version to stamp, e.g. `v1.3.0` | All artifacts in `releases/<version>/`: `DRAFT.md` + `CHANGELOG.md` + `SUMMARY.md` + `MIGRATION.md` + `demo.py` |
| `/release audit [version]` | optional target version | Terminal readiness report; emits `verdict: READY \| NEEDS_ATTENTION \| BLOCKED` as final line for orchestrator consumption |
| `/release demo [range]` | optional range (default: last-tag..HEAD) | `releases/<version>/demo.py` or `.temp/release-demo-<branch>-<date>.py` |

Range notation: `v1->v2` (e.g. `v1.2->v2.0`) — converted internally to git range. No mode → defaults to `notes`. `prepare` = full pipeline — runs audit first, then all artifacts; use when cutting release, not drafting.

`--append`: assumes an earlier `notes` run already produced `DRAFT.md` (and, when their flags were used, `CHANGELOG.md`/`SUMMARY.md`/`MIGRATION.md`) and reruns the **full pipeline** — Gather changes through Draft executive summary, unchanged — scoped to only the commits landed since then, via a per-branch marker at `.temp/release-last-processed-<branch>` (see `bin/release_append_marker.py`). No marker found (first use, or history rewritten by rebase/force-push) → falls back to the default `$LAST_TAG..HEAD` range and full-overwrite write, same as plain `notes` — establishing the baseline for the next `--append` run. Every successful `notes`-mode write (append or full) refreshes the marker to current `HEAD`.

**Non-destructive except revert/pivot**: integration is purely additive — new bullets/blocks/paragraphs join existing artifacts without touching untouched content — *unless* this cycle's Classify/Truth-check phases detect that a new commit reverts or materially changes something a PRIOR cycle already wrote (see Gather changes' "Cross-cycle revert/pivot detection"); that stale entry is struck or superseded, never left stale alongside a contradicting new one. After merge, a **Post-merge re-validation** pass (see `modes/release-draft-template.md`) re-runs Truth check, Identify highlights re-ranking, Validate migration docs, and Validate docs against the FINAL merged content — catches prior-cycle content that went stale from THIS cycle's changes without being a clean detected revert/pivot (e.g. a Spotlight built on a commit a later cycle reverts). </inputs>

<workflow>

**Task hygiene**: Call `TaskList`; triage found tasks (`completed` / `deleted` / `in_progress`).

**Task tracking** — create ALL tasks upfront, execute sequentially; mark completed as each phase finishes. After mode detection, mark inapplicable tasks `deleted`:

- `demo` mode: mark deleted — Classify each change, Classify breaking changes, Validate migration docs, Audit changelog, Extract contributors, Draft migration guide, Draft executive summary, Write release draft, Post-merge re-validation
- bug-fix-only release (no 🚀 Added items): mark deleted — Generate release demo
- not `--append`, or `--append` with no valid marker (`$MARKER_VALID` computed false during Write release draft — see release-draft-template.md): mark deleted — Post-merge re-validation

Tasks:

- Gather changes (git log + find common base tag)
- Explore codebase (changed files, impl detail)
- Validate docs alignment
- Classify each change
- Classify breaking changes (codemap-gated; skip without index)
- Validate migration docs (skip when no migration doc found)
- Audit changelog
- Extract contributors
- Identify highlights
- Draft migration guide
- Generate release demo (feature releases only)
- Draft executive summary
- Write release draft
- Post-merge re-validation (`--append` merge only — re-runs Truth check, Identify highlights, Validate migration docs, Validate docs against the final merged DRAFT.md; see release-draft-template.md "Post-merge re-validation")

**Sequential enforcement**: never begin phase until prior marked `completed`. On failure (empty range, git error, demo fail), stop and report — no downstream phases.

## Delegation strategy

In `prepare` and `audit` modes, delegate gather/explore/validate to subagent via file-based handoff (CLAUDE.md §2) — these phases produce large output, bloat main context:

1. Pre-compute gather file path and create dir:
   ```bash
   # BRANCH, DATE from Shared setup below
   GATHER_FILE=".temp/release-gather-$BRANCH-$DATE.md"
   mkdir -p .temp  # timeout: 5000
   ```
2. Assert variables before spawning:
   ```bash
   [ -n "$GATHER_FILE" ] && [ -n "$REPO_ROOT" ] && [ -n "$RANGE" ] || { echo "Error: GATHER_FILE, REPO_ROOT, or RANGE is empty — verify Shared setup and Gather changes completed"; exit 1; }  # timeout: 5000
   export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
   # reload SKILL_DIR (Check 41)
   IFS= read -r SKILL_DIR < "${TMPDIR:-/tmp}/release-setup-${CSID}/SKILL_DIR" 2>/dev/null || SKILL_DIR=""
   cat "$SKILL_DIR/templates/gather-prompt.md"  # timeout: 5000
   ```

> **Agent budget** — each spawn costs ~120,851 tok of fixed overhead (~73 tool-calls' worth) plus ~12.0 s/call, so work under ~73 calls is cheaper done inline: spawn nothing. Keep each agent near ~55 tool-calls; past ~60 they stall without returning an envelope, forcing reconstruction from disk. Every spawn prompt must require an envelope even on exhaustion — `partial: true` plus what was finished.

Template (loaded above). Substitute `<REPO_ROOT>`, `<RANGE>`, `<GATHER_FILE>` with literal values. Spawn:

> loads: gather-prompt.md `Agent(subagent_type="foundry:sw-engineer", prompt=<substituted gather-prompt.md content>)`

3. Validate envelope; every "abort" is a hard `exit 1`:
   ```bash
   STATUS=$(echo "$ENVELOPE" | jq -r '.status' 2>/dev/null)
   GATHER_FILE=$(echo "$ENVELOPE" | jq -r '.file' 2>/dev/null)
   BREAKING=$(echo "$ENVELOPE" | jq -r '.breaking // 0' 2>/dev/null)  # default 0 — never skip migration guide on missing field
   UNCONFIRMED=$(echo "$ENVELOPE" | jq -r '.unconfirmed // 0' 2>/dev/null)
   UNCONFIRMED_BREAKING=$(echo "$ENVELOPE" | jq -r '.unconfirmed_breaking // 0' 2>/dev/null)
   if [ "$STATUS" != "done" ] || [ -z "$GATHER_FILE" ] || [ "$GATHER_FILE" = "null" ] || [ ! -f "$GATHER_FILE" ]; then
       echo "Error: delegation validation failed — status=$STATUS, file=$GATHER_FILE" >&2
       exit 1
   fi
   ```

When `unconfirmed > 0`, surface removed items as notification (not a gate — already removed). Read REMOVED log from `$GATHER_FILE`:

```bash
if [ "${UNCONFIRMED:-0}" -gt 0 ] 2>/dev/null; then
    REMOVED_ITEMS=$(grep '^REMOVED:' "$GATHER_FILE" | head -20)  # timeout: 3000
    echo "Truth check removed ${UNCONFIRMED} unverified claim(s) from release notes (not found in HEAD):"
    echo "$REMOVED_ITEMS"
fi
```

Pass `$GATHER_FILE` path to artifact phase — do NOT read gather file into main context; REMOVED log grep above = sole sanctioned exception.

**Phases 5–6 parallel delegation** (`prepare`/`audit` modes, after phases 1–4 complete): Audit changelog and Extract contributors are independent — delegate concurrently to reclaim tokens.

Pre-compute output paths and persist for downstream reload (fresh shell, Check 41):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# fresh shell loses vars between blocks (Check 41)
IFS= read -r BRANCH < "${TMPDIR:-/tmp}/release-setup-${CSID}/BRANCH" 2>/dev/null || BRANCH=""
IFS= read -r DATE < "${TMPDIR:-/tmp}/release-setup-${CSID}/DATE" 2>/dev/null || DATE=""
IFS= read -r RANGE < "${TMPDIR:-/tmp}/release-range-${CSID}" 2>/dev/null || RANGE=""
IFS= read -r REPO_ROOT < "${TMPDIR:-/tmp}/release-setup-${CSID}/REPO_ROOT" 2>/dev/null || REPO_ROOT=""
GATHER_FILE=".temp/release-gather-$BRANCH-$DATE.md"
[ -f "$GATHER_FILE" ] || { echo "Error: GATHER_FILE missing — phases 1–4 must complete first"; exit 1; }  # timeout: 5000
CHANGELOG_AUDIT_FILE=".temp/release-changelog-audit-$BRANCH-$DATE.md"
CONTRIBUTORS_FILE=".temp/release-contributors-$BRANCH-$DATE.md"
mkdir -p .temp  # timeout: 5000
echo "${CHANGELOG_AUDIT_FILE:-}" > "${TMPDIR:-/tmp}/release-changelog-audit-${CSID}"
echo "${CONTRIBUTORS_FILE:-}" > "${TMPDIR:-/tmp}/release-contributors-${CSID}"
# reload SKILL_DIR (Check 41)
IFS= read -r SKILL_DIR < "${TMPDIR:-/tmp}/release-setup-${CSID}/SKILL_DIR" 2>/dev/null || SKILL_DIR=""
cat "$SKILL_DIR/modes/changelog-audit-prompt.md"  # timeout: 5000
```

<!-- loads: modes/changelog-audit-prompt.md -->

Prompt (loaded above) — execute (spawn Agent A + Agent B per instructions in that file).

Validate both envelopes:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/parse_release_envelopes.py" --envelope-a "$ENVELOPE_A" --envelope-b "$ENVELOPE_B"  # timeout: 5000
```

`$SCOPE_FLAGGED` > 0 → read the "Scope check" section of `$CHANGELOG_AUDIT_FILE`; every row there (critical = already-released commit landed a second time, medium = unreviewed branch merge) must reach the Findings summary table in `audit`/`prepare` modes — see `templates/audit-checks.md` Output, "Changelog scope" row — never left inside the audit file only. This is the check the reviewer needs surfaced before approving the PR, not something to bury in a report nobody opens.

`notes` and `demo` modes: skip delegation — single-pass; run gather/explore/validate inline. **Size guard**: estimate commit count with `git rev-list --count ${RANGE:-${LAST_TAG:-HEAD~20}..HEAD} 2>/dev/null`. If >50, delegate to `foundry:sw-engineer` subagent same as prepare mode — inline gather with >50 commits causes context flood. Define `GATHER_FILE` before spawning so envelope-validation block above can resolve the path:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# reload BRANCH/DATE (Check 41)
IFS= read -r BRANCH < "${TMPDIR:-/tmp}/release-setup-${CSID}/BRANCH" 2>/dev/null || BRANCH=""
IFS= read -r DATE < "${TMPDIR:-/tmp}/release-setup-${CSID}/DATE" 2>/dev/null || DATE=""
GATHER_FILE=".temp/release-gather-$BRANCH-$DATE.md"
mkdir -p .temp  # timeout: 5000
```

## Mode Detection

| First token | MODE | Routing |
| -- | -- | -- |
| `prepare` | prepare | **Shared setup** first, then **Mode: prepare** |
| `audit` | audit | **Shared setup** first, then **Mode: audit** |
| `demo` | demo | **Shared setup** first, then **Mode: demo** |
| `notes` | notes | Strip `notes` token; parse flags and range from remainder |
| *(bare range or flag)* | notes | Parse flags and range from full string |
| *(none)* | notes | `RANGE=""`, no flags; run all phases |

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
DO_CHANGELOG=false; DO_SUMMARY=false; DO_MIGRATION=false; DO_APPEND=false
FIRST=$(echo "$ARGUMENTS" | awk '{print $1}')
# empty at single-word ARGUMENTS (cut -d' ' -f2- echoes whole line, no delimiter)
REST=""; case "$ARGUMENTS" in *" "*) REST="${ARGUMENTS#* }";; esac
echo "${REST:-}" > "${TMPDIR:-/tmp}/release-rest-${CSID}"
# strip mode token — else leaks into RANGE
_PARSE_INPUT="$ARGUMENTS"; case "$FIRST" in notes|prepare|audit|demo) _PARSE_INPUT="$REST";; esac
RANGE=$(echo "$_PARSE_INPUT" | grep -oE '[^ ]+([[:space:]]*->[[:space:]]*|\.\.)[^ ]+' | head -1 | tr -d '[:space:]')
for _a in $_PARSE_INPUT; do case "$_a" in --changelog) DO_CHANGELOG=true;; --summary) DO_SUMMARY=true;; --migration) DO_MIGRATION=true;; --append) DO_APPEND=true;; --*) echo "⚠ unknown flag: $_a";; *) [ -z "$RANGE" ] && RANGE="$_a";; esac; done
RANGE="${RANGE/->/..}"
# persist (Check 41) — Gather-changes needs this for marker-based RANGE resolution
echo "${DO_APPEND}" > "${TMPDIR:-/tmp}/release-do-append-${CSID}"
```

<!-- branch: unsupported-flags — isolated; ≤1 call; fires only when unknown flags present -->

**Unknown flags**: if any `⚠ unknown flag:` lines printed above, invoke `AskUserQuestion` — (a) **Abort** (stop, re-invoke) · (b) **Continue ignoring**. On Abort: stop.

## Shared setup

Run this first — cold-start fallback (sets `$_OSS_SHARED`):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_OSS_SHARED=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/resolve_shared_path.py" oss skills/_shared 2>/dev/null)  # timeout: 5000
# persist (Check 41)
echo "${_OSS_SHARED:-}" > "${TMPDIR:-/tmp}/release-oss-shared-${CSID}"
# loads: oss-shared-resolver.md
```

Extracted to `bin/release_setup.py` — resolves `SKILL_DIR`, `REPO_ROOT`, `BRANCH`, `DATE`, `LAST_TAG`, `CHERRY_PICK_SUBJECTS`, `SOURCE_TAG_REF`. Writes each var under `${TMPDIR:-/tmp}/release-setup-${CSID}/`; stable-branch banner and "no stable tag" warnings go to stderr.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/release_setup.py"  # timeout: 10000
IFS= read -r SKILL_DIR < "${TMPDIR:-/tmp}/release-setup-${CSID}/SKILL_DIR" 2>/dev/null || SKILL_DIR=""
IFS= read -r REPO_ROOT < "${TMPDIR:-/tmp}/release-setup-${CSID}/REPO_ROOT" 2>/dev/null || REPO_ROOT=""
IFS= read -r BRANCH < "${TMPDIR:-/tmp}/release-setup-${CSID}/BRANCH" 2>/dev/null || BRANCH=""
IFS= read -r DATE < "${TMPDIR:-/tmp}/release-setup-${CSID}/DATE" 2>/dev/null || DATE=""
IFS= read -r LAST_TAG < "${TMPDIR:-/tmp}/release-setup-${CSID}/LAST_TAG" 2>/dev/null || LAST_TAG=""
IFS= read -r CHERRY_PICK_SUBJECTS < "${TMPDIR:-/tmp}/release-setup-${CSID}/CHERRY_PICK_SUBJECTS" 2>/dev/null || CHERRY_PICK_SUBJECTS=""
IFS= read -r SOURCE_TAG_REF < "${TMPDIR:-/tmp}/release-setup-${CSID}/SOURCE_TAG_REF" 2>/dev/null || SOURCE_TAG_REF=""
[ -z "$REPO_ROOT" ] && { echo "Error: release_setup.py failed — REPO_ROOT empty; verify oss plugin installation"; exit 1; }
```

<!-- branch: no-stable-tags — isolated; ≤1 call; fires only when repo has no stable git tags -->

When no stable tags exist, `LAST_TAG` resolves to initial commit — surface via `AskUserQuestion` ("No stable tags found. Range base is initial commit — proceed?"). Options: (a) Proceed with initial commit as base · (b) Abort — stop release process. On (b): stop, print "Release aborted — no stable tags found; create a tag first with `git tag v0.1.0`" and exit.

## Gather changes

Find common base tag across ALL branches via `git tag --list` sorted by version, then `git merge-base HEAD <tag-commit>`. Use as range lower bound when current branch has no direct tag ancestry.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# reload Shared-setup vars (Check 41)
IFS= read -r LAST_TAG < "${TMPDIR:-/tmp}/release-setup-${CSID}/LAST_TAG" 2>/dev/null || LAST_TAG=""
IFS= read -r CHERRY_PICK_SUBJECTS < "${TMPDIR:-/tmp}/release-setup-${CSID}/CHERRY_PICK_SUBJECTS" 2>/dev/null || CHERRY_PICK_SUBJECTS=""
IFS= read -r BRANCH < "${TMPDIR:-/tmp}/release-setup-${CSID}/BRANCH" 2>/dev/null || BRANCH=""
IFS= read -r DO_APPEND < "${TMPDIR:-/tmp}/release-do-append-${CSID}" 2>/dev/null || DO_APPEND="false"
if [ -z "$RANGE" ] && [ "$DO_APPEND" = "true" ]; then
    # marker present+valid -> "<sha>..HEAD"; else "$LAST_TAG..HEAD" (non-append default)
    RANGE=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/release_append_marker.py" resolve --branch "$BRANCH" --last-tag "$LAST_TAG")  # timeout: 5000
else
    RANGE="${RANGE:-$LAST_TAG..HEAD}"
fi
[ -z "$RANGE" ] && echo "Error: could not determine commit range" && exit 1
# persist (Check 41)
echo "${RANGE:-}" > "${TMPDIR:-/tmp}/release-range-${CSID}"

# quote RANGE — tags may carry unusual chars (e.g. v1.2-rc.1+build.42)
git log "$RANGE" --oneline --no-merges # timeout: 3000
git log "$RANGE" --no-merges --format="--- %H%n%B" # timeout: 3000
git diff --stat "$(echo "$RANGE" | sed 's/\.\.\./\ /;s/\.\./\ /')" # timeout: 3000

# prefer gh; fallback git remote show origin; never hardcode main
TRUNK=$(gh repo view --json defaultBranchRef --jq '.defaultBranchRef.name' 2>/dev/null)  # timeout: 6000
if [ -z "$TRUNK" ]; then
    TRUNK=$(git remote show origin 2>/dev/null | grep 'HEAD branch' | { read -r _ _ val; echo "$val"; })  # timeout: 5000
fi
if [ -n "$TRUNK" ]; then
    gh pr list --state merged --base "$TRUNK" --paginate \
        --json number,title,body,labels,mergedAt,author 2>/dev/null  # timeout: 15000
else
    echo "⚠ Could not detect default branch — listing all merged PRs"
    gh pr list --state merged --paginate \
        --json number,title,body,labels,mergedAt,author 2>/dev/null  # timeout: 15000
fi
```

Cross-reference commit bodies against PR descriptions — canonical source of truth for *why* change made. `BREAKING CHANGE:` footer = breaking change regardless of PR label.

**Detect revert pairs**: scan `git log $RANGE --no-merges --format="%H %s"` for subjects beginning with `Revert "`. For each: extract original subject, search range for matching commit. Both found → `REVERT_SET` pair (net effect zero).

Record all `REVERT_SET` pairs before Classify. Commits in `REVERT_SET` excluded from standard sections; collected for 🔄 Reverted. If only revert is in range (original predates range) → classify as ✗ Removed (or ⚠ Breaking Changes if API surface changed without prior deprecation) — NOT 🔄 Reverted; net user effect is non-zero.

**Cross-cycle revert/pivot detection** (`--append` only, when a prior `DRAFT.md`/`$CHANGELOG_FILE` exists — extends the "non-destructive except revert/pivot" rule across append cycles, not just within one range).

**Two detection paths — patch-id provenance (deterministic, content-stable) for literal reverts, semantic judgment (best-effort) for pivots.** Every prior cycle's write is recorded in `.temp/release-provenance-$BRANCH.json` (see `<notes>` "Provenance store (patch-id keyed)"): one entry per `(patch-id, artifact, exact written text)` tuple, keyed on `git patch-id --stable` output rather than raw commit sha — a bare sha changes on `--amend`, `rebase`, or `cherry-pick` even when the diff itself is untouched, so a raw-sha key would silently miss a revert of a commit that has since been reworded or cherry-picked; `patch-id` is a normalized hash of the diff content and survives all three. A genuine `git revert` commit always carries git's own auto-generated `This reverts commit <sha>.` trailer in its body — that sha identifies the reverted commit's *current* form, which is turned into its patch-id and looked up in the store (no text-matching involved). A **pivot** (a symbol's classification changes without a literal revert commit — e.g. deprecated this cycle after being added in a prior one) has no such trailer to key off; it still needs the model's semantic judgment, same as before. A revert commit *without* a usable trailer (manual revert, not made via `git revert`) also falls back to the semantic path — as does a trailer sha whose diff produces no stable patch-id (see step 2 below).

1. **Read** the current `DRAFT.md` Notable-changes bullets and `$CHANGELOG_FILE`'s Unreleased section into context (Read tool — actual content, not just piped grep output).
2. **Revert case — patch-id lookup first**: for each revert whose original predates `$RANGE` (the "only revert in range" case above), extract the original sha from the revert commit's own trailer, then convert it to a patch-id — the sha itself is never the lookup key, since it may point at a commit reworded/rebased/cherry-picked since it was recorded, but its diff content (hence patch-id) is unaffected by any of those:
   ```bash
   ORIGINAL_SHA=$(git log -1 --format=%B "<revert-commit-sha>" | grep -oE 'This reverts commit [0-9a-f]{40}' | grep -oE '[0-9a-f]{40}')  # timeout: 3000
   [ -n "$ORIGINAL_SHA" ] && ORIGINAL_PID=$(git show "$ORIGINAL_SHA" | git patch-id --stable | awk '{print $1}')  # timeout: 3000
   # ORIGINAL_PID empty despite found sha → merge commit w/o -m, or empty commit — rare, treat as "no trailer" below
   ```
   Trailer found and patch-id non-empty → look it up:
   ```bash
   export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
   IFS= read -r BRANCH < "${TMPDIR:-/tmp}/release-setup-${CSID}/BRANCH" 2>/dev/null || BRANCH=""
   PROVENANCE_FILE=".temp/release-provenance-$BRANCH.json"
   [ -f "$PROVENANCE_FILE" ] && jq --arg pid "$ORIGINAL_PID" '[.[] | select(.patch_id == $pid)]' "$PROVENANCE_FILE"  # timeout: 3000
   ```
   One or more matches → each is a confirmed `CROSS_CYCLE_MATCH` directly (step 4) — a patch-id match is definitive, no semantic confirmation needed, even when the matched record's stored `sha` differs from `$ORIGINAL_SHA` (expected whenever the original commit was reworded/rebased/cherry-picked since it was recorded — the diff, not the sha, is what's being matched). No matches → the reverted commit predates any drafted artifact (already published, or never drafted), or its diff has genuinely changed since — falls through to normal ✗ Removed/⚠ Breaking classification, same as today. No trailer found (manual/non-`git revert` revert commit), or trailer found but patch-id computation produced empty output → fall through to step 3's grep-narrow-then-confirm path for this revert, same as a pivot.
3. **Pivot case (and any revert without a usable trailer) — semantic judgment, best-effort not a guarantee**: grep is only a *narrowing hint*, never the decision. Grep for the de-`Revert`-wrapped subject/PR title (revert-without-trailer) or the changed symbol name (pivot: a newly-classified ⚠ Breaking Changes / 🌱 Changed / 🗑️ Deprecated / ❌ Removed item). A grep hit is a candidate to inspect — never an automatic match; a bare substring (e.g. `run`, `Config`) can hit unrelated bullets, treat every hit as "maybe." **Confirm semantically**: does the candidate line genuinely describe the same feature/symbol this new commit reverts or supersedes? Only a positive judgment call proceeds to step 4. Known limitation, accepted: a prior cycle's bullet reworded into human prose by `oss:shepherd` can defeat this grep hint — e.g. raw subject `Revert "feat: add ConfigLoaderV2"` vs. shepherded bullet "dropped the legacy config loader." Don't chase this with fuzzier matching, which only trades false-negatives for false-positives — the patch-id path above already removes this failure mode for the common case of a real `git revert`.
4. **Record** `CROSS_CYCLE_MATCH: {artifact: <file>, matched_text: <exact current line/bullet, copied verbatim>, via: "patch_id"|"semantic"}` — matched_text must always be a full existing line/bullet, **never a bare symbol/subject substring**, so the Edit-tool strike (see `modes/release-draft-template.md` "Append merge") stays scoped to the one confirmed entry instead of risking a match on every bullet that happens to contain the token. Treat as **net-state removal** — same Net-state principle as within-range `REVERT_SET`, just spanning cycles: the reader never saw the reverted/superseded content ship in a published release, so it vanishes from both artifacts entirely, no redundant ✗ Removed/⚠ Breaking bullet added.

No patch-id match, no candidate found, or a candidate inspected but not confirmed → proceed as a normal, purely-additive item (default — never manufacture a match speculatively). A stale bullet surviving the semantic path is an accepted best-effort gap, not silent data loss — the Semantic consistency review pass (`modes/release-draft-template.md`, runs before every write) is the last line of defense that can still catch a surviving contradiction. Collect all `CROSS_CYCLE_MATCH` entries for Audit changelog and Write release draft to consume.

## Explore codebase

For top 3–5 significant changes (features, breaking, major behavior), read actual diff or changed files:

```bash
git diff "$RANGE" -- <file>    # timeout: 3000
git show <commit>:<file>       # timeout: 3000
```

Goal: understand new APIs, parameters, behavior — notes describe real functionality, not just commit subjects. Skip trivial changes (typos, dep bumps, CI config).

## Validate docs

Check public API surface in docs/ (or README) matches diff. Flag public symbol added/renamed/removed in Gather changes but absent from docs. Report: `- [MISSING/STALE] <symbol> in <doc-file>`. Empty list = docs aligned.

**Doc weight check** — for each 🚀 Added change identifying significant new entity (new public skill, new command, new agent, new submodule, new mode): compute **doc weight** for that feature and 2–3 comparable existing features of same nature in relevant README or docs file.

Doc weight = `header_score + coverage_score + example_score`:

- `header_score`: H2 = 3, H3 = 2, H4/deeper = 1, no heading = 0
- `coverage_score`: `min(non_blank_lines_in_section / 5, 5)` — lines from feature heading to next same-or-higher heading
- `example_score`: fenced code blocks in section, capped at 3

Weight ratio = `new_feature_weight / mean(comparable_weights)`. Flag UNDERTREATED when ratio < 0.5. Report: `- [UNDERTREATED] <feature> in <doc-file> — weight N vs peers M1/M2 (ratio R)`. Collect as `doc_proportionality` list in findings.

## Classify each change

<!-- loads: modes/classify-truth-check.md (single load — Classify/Truth check/Breaking-change classification below run sequentially in this same path, no branch skips an earlier phase while running a later one; file content stays in context for the two "Follow above" refs below) -->

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# reload SKILL_DIR (Check 41)
IFS= read -r SKILL_DIR < "${TMPDIR:-/tmp}/release-setup-${CSID}/SKILL_DIR" 2>/dev/null || SKILL_DIR=""
cat "$SKILL_DIR/modes/classify-truth-check.md"  # timeout: 5000
```

Follow above and execute. Contains: category table, PR accumulation rules, dedup rules, OMIT-INTERNAL body-signal override, cherry-pick annotation.

## Truth check

Follow `modes/classify-truth-check.md` (Truth check section, loaded above) and execute. Gate: runs after Classify, before Audit changelog. Verifies 🚀 Added / ⚠ Breaking Changes / 🌱 Changed symbols exist in HEAD via codemap or grep fallback. Max 3 loop iterations.

## Breaking-change classification

Follow `modes/classify-truth-check.md` (Breaking-change classification section, loaded above) and execute. Codemap-gated (skips without a v3 index). For each diff-derived public symbol, `fn-rdeps --exclude-tests` labels it Breaking (caller outside its own package) or internal; Breaking symbols move to ⚠ Breaking Changes with caller evidence, and `migration_lines` feed the Draft migration guide as `breaking_callers` findings.

## Validate migration docs

Gate — runs after Truth check. Only when project has migration docs page.

**Detect** — migration doc OR any alternative describing API changes between versions:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# reload REPO_ROOT (Check 41)
IFS= read -r REPO_ROOT < "${TMPDIR:-/tmp}/release-setup-${CSID}/REPO_ROOT" 2>/dev/null || REPO_ROOT=""
MIGRATION_DOC=$(find "$REPO_ROOT" -maxdepth 3 \( \
  -iname "MIGRATION*" -o -iname "UPGRADING*" -o \
  -iname "migration.md" -o -iname "upgrading.md" -o \
  -iname "CHANGELOG*" -o -iname "BREAKING*" -o \
  -iname "api-changes*" -o -iname "release-notes*" \
\) -not -path "*/node_modules/*" -not -path "*/.venv/*" -not -path "*/.git/*" \
| head -1)  # timeout: 5000
[ -z "$MIGRATION_DOC" ] && MIGRATION_DOC=$(find "$REPO_ROOT/docs" -maxdepth 2 \( \
  -iname "migration*" -o -iname "upgrade*" -o -iname "breaking*" -o -iname "api-changes*" \
\) 2>/dev/null | head -1)  # timeout: 5000
```

Skip entirely when `$MIGRATION_DOC` empty — no migration/upgrade docs exist in project.

**When found**: for every classified item in ⚠ Breaking Changes, 🗑️ Deprecated, and ✗ Removed — verify present and described in `$MIGRATION_DOC`. "Present" = migration doc contains symbol name or semantically equivalent reference with upgrade instructions.

Check each item:

```bash
grep -i "<symbol_or_key>" "$MIGRATION_DOC" 2>/dev/null  # timeout: 3000
```

**Outcomes**:

- Found with upgrade path → `✓ <symbol> covered`
- Found but no upgrade path → `[SHALLOW] <symbol> in <doc> — present but missing upgrade instructions`
- Not found → `[MISSING-MIGRATION] <symbol> — ⚠ Breaking/🗑️ Deprecated but absent from <doc>`

Collect all findings as `migration_gaps` list. Zero findings → migration doc complete. Report before proceeding.

**Do not block** on `[SHALLOW]` findings — flag and continue. `[MISSING-MIGRATION]` findings surface as warnings; Draft migration guide phase must fill the gaps.

## Audit changelog

**`prepare`/`audit` modes**: delegated in parallel (see Delegation strategy). Reload paths:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r CHANGELOG_AUDIT_FILE < "${TMPDIR:-/tmp}/release-changelog-audit-${CSID}" 2>/dev/null || CHANGELOG_AUDIT_FILE=""
IFS= read -r CHANGELOG_FILE < "${TMPDIR:-/tmp}/release-changelog-file-${CSID}" 2>/dev/null || CHANGELOG_FILE=""
```

Read `$CHANGELOG_AUDIT_FILE` for audit findings; report added/flagged counts from delegation envelope. If file missing (delegation skipped), fall back to inline below.

**`notes` mode or delegation fallback**:

Search order: `CHANGELOG.md` at repo root, `docs/CHANGELOG.md`, any `CHANGELOG*` one level deep (excluding `node_modules/`, `.venv/`, `vendor/`). Store as `$CHANGELOG_FILE`.

If exists: cross-check against unreleased section. Items absent → add (same emoji format). Items in CHANGELOG not matching classified → flag for review (no auto-delete). For each REVERT_SET pair: add `🔄 Reverted: <original change description> (introduced and reverted in this release)`. If original already in CHANGELOG before revert, strike/remove from main section — unshipped change must not appear shipped. Reverted items never in highlights or migration guide.

For each `CROSS_CYCLE_MATCH` targeting `$CHANGELOG_FILE` (from Gather changes' cross-cycle detection — original predates `$RANGE`, matched text found in Unreleased from a prior `--append` cycle): strike/remove the matched entry the same way — do not add a redundant `🔄 Reverted` bullet for something the reader never saw shipped in this visible cycle.

If missing: create `CHANGELOG.md`; populate with `# Changelog` header and `## [Unreleased]` from Classify.

**Scope check** (same rule as delegated Agent A — see `modes/changelog-audit-prompt.md`): `git log $RANGE --merges --pretty='%H %P %s'`, keep rows whose subject doesn't match `merge pull request #[0-9]+|\(#[0-9]+\)` — a raw branch merge landed inside `$RANGE`, not a real PR merge. Diff each such commit's two parents to list what it pulled in; any pulled-in commit already shipped in a prior CHANGELOG section is `critical` — it's about to appear a second time in the wrong release under this PR by accident. Report as its own line, never folded into the add/flag count silently.

Always report: "N items added, M flagged for review, K scope-flagged (non-PR branch merge)." This phase owns CHANGELOG-format classification; Write release draft reads from it — does NOT copy. DRAFT.md uses different format.

## Extract contributors

**`prepare`/`audit` modes**: delegated in parallel (see Delegation strategy). Reload path:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r CONTRIBUTORS_FILE < "${TMPDIR:-/tmp}/release-contributors-${CSID}" 2>/dev/null || CONTRIBUTORS_FILE=""
```

Read `$CONTRIBUTORS_FILE` for formatted contributors list. If file missing (delegation skipped), fall back to inline below.

**`notes` mode or delegation fallback**:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# reload $RANGE (Check 41)
IFS= read -r RANGE < "${TMPDIR:-/tmp}/release-range-${CSID}" 2>/dev/null || RANGE=""
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/extract_contributors.py" --range "$RANGE"  # timeout: 5000
```

`extract_contributors.py` emits one `Name <email>` line per contributor — already deduplicated by email and bot-filtered (`[bot]`, `noreply@`). Every commit counts, including docs and typo fixes.

For each contributor, inspect commits in range (`git log "$RANGE" --no-merges --author="<email>" --oneline`) and pick up to 3 most significant contributions. Rank: new public API > major UX improvement > significant fix > internal change > docs/typo. No PR numbers, no issue links, no `(#N)` references.

Resolve GitHub handle from PR author data (`author.login` field). Match on name or email. If no PR found, omit handle.

For each resolved handle, find a LinkedIn link via this ordered chain — stop at first hit, never guess/infer/search-by-name at any step. Contributor **name is never a matching key anywhere in this chain** — only the resolved GitHub handle (steps 1, 2, 4 below) or an anchor href read directly from fetched page content (step 3):

1. **Primary — Social Accounts API** (strongest signal: explicitly added by the person to their own GitHub profile):

   ```bash
   gh api "/users/<login>/social_accounts" --jq '.[] | select(.provider=="linkedin") | .url' 2>/dev/null  # timeout: 6000
   ```

   Non-empty output → use directly, done.

2. **Fallback A — `.blog` field**:

   ```bash
   gh api /users/<login> --jq '{blog: .blog, twitter: .twitter_username}' 2>/dev/null  # timeout: 6000
   ```

   `.blog` contains `linkedin.com` → use directly, done.

3. **Fallback B — personal-page exception**: `.blog` is a non-empty URL that is NOT a `linkedin.com` URL → `WebFetch` that page and scan its actual returned content for `linkedin.com/in/...` anchor links (real hrefs read from the page, never inferred from surrounding text). Exactly one distinct such link found → use it, done. Zero or multiple distinct links found → do not guess; omit LinkedIn for this contributor.

4. **Fallback C — past releases**: search for a Contributors entry, keyed by this EXACT GitHub handle, that a prior release already resolved and credited with a `[LinkedIn](...)` link:

   ```bash
   grep -rn "@<login>" CHANGELOG.md docs/CHANGELOG.md releases/*/SUMMARY.md releases/*/DRAFT.md 2>/dev/null | grep -m1 '\[LinkedIn\]('  # timeout: 5000
   # only when the above finds nothing — scan published release bodies (stop at first match):
   for tag in $(gh release list --limit 100 --json tagName --jq '.[].tagName' 2>/dev/null); do  # timeout: 15000
       gh release view "$tag" --json body --jq '.body' 2>/dev/null | grep -m1 "@<login>.*\[LinkedIn\]("  # timeout: 6000
   done | head -1
   ```

   Found → reuse that URL verbatim, done. Not found → omit.

5. No step produced a link → omit LinkedIn, same as today's no-match behavior.

Format unchanged: `- **Name** (@github_handle, [LinkedIn](https://linkedin.com/in/handle)) — <brief what they did>`. Omit `@handle` when unresolvable. `<brief what they did>` picks up to 3 most significant contributions per line 440's ranking and describes the contribution itself (feature/fix/area) — never a PR number, issue link, or `(#N)` reference.

## Identify highlights

Pick top 3–5 most significant changes from Classify. Ranking: breaking changes > new public API > major UX improvements > notable fixes. Pull concrete code example from explore-codebase diff for each. Drives Summary paragraph and Spotlights section.

## Draft migration guide

Always produce. No breaking changes → single line "No breaking changes in this release." Deprecations/removals → show before→after code examples. State in preamble: API deprecated in prior release and now removed → ✗ Removed (not Breaking).

If `migration_gaps` non-empty (from Validate migration docs): for each `[MISSING-MIGRATION]` item, add dedicated section covering that symbol with before→after example. For each `[SHALLOW]` item, expand existing coverage to add concrete upgrade instructions.

If `breaking_callers` non-empty (from Breaking-change classification): for each Breaking symbol, add a before→after section citing its external call sites (the `migration_lines`) so downstream consumers see exactly which of their call sites must change. When that phase reported `query_complete:false`, prefix the section "Affected call sites (possibly-incomplete — codemap coverage partial)".

## Generate release demo

**Only for feature releases** (≥1 🚀 Added items). Skip bug-fix-only releases.

Self-contained Python script in jupytext percent (`# %%`) format. Full story: install → setup → demonstrate each highlight → verify output.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# reload Shared-setup vars (Check 41)
IFS= read -r BRANCH < "${TMPDIR:-/tmp}/release-setup-${CSID}/BRANCH" 2>/dev/null || BRANCH=""
IFS= read -r DATE < "${TMPDIR:-/tmp}/release-setup-${CSID}/DATE" 2>/dev/null || DATE=""
DEMO_OUT=".temp/release-demo-$BRANCH-$DATE.py"
mkdir -p .temp  # timeout: 5000
echo "${DEMO_OUT:-}" > "${TMPDIR:-/tmp}/release-demo-out-${CSID}"
```

Write demo to `$DEMO_OUT`. (`prepare` mode: `releases/$VERSION/demo.py` — see Phase 4.)

**Gate: demo must execute to completion before proceeding to Draft executive summary.**

<!-- branch: demo-approval — only in demo/prepare mode; isolated from notes/changelog path; ≤1 call -->

Invoke `AskUserQuestion` — "Ready to run demo script `$DEMO_OUT`?" Options: (a) Run now · (b) Review first · (c) Skip and **exclude from release artifacts**.

On option (c): mark demo excluded, skip to Draft executive summary — do NOT invoke the failure-path AskUserQuestion below.

On (a) or (b) confirmed:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r DEMO_OUT < "${TMPDIR:-/tmp}/release-demo-out-${CSID}" 2>/dev/null || DEMO_OUT=""
python "$DEMO_OUT"  # timeout: 600000
DEMO_EXIT=$?
echo "${DEMO_EXIT}" > "${TMPDIR:-/tmp}/release-demo-exit-${CSID}"
```

<!-- policy-sibling: plugins/cc_oss/skills/release/modes/prepare.md §Phase 4a execution gate (demo retry bound) -->

**Guard**: only proceed to failure handling when `$DEMO_EXIT -ne 0` after attempting a fix. Success (`$DEMO_EXIT = 0`) → proceed directly to Draft executive summary — no AskUserQuestion. Failure → fix and re-run (max 3 iterations total). Only after 3 failed attempts invoke `AskUserQuestion` ("Demo still failing after 3 attempts. Exclude from release and continue, or abort?"). Self-contained: package installed in current env; no live API calls or network deps; deterministic synthetic data; `# !pip install` lines are Python comments — interpreter skips.

## Draft executive summary

1–2 paragraph executive summary: what release is, why it matters, who benefits. Based on Identify highlights. Save `.temp/output-release-summary-$BRANCH-$DATE.md`.

## Write release draft

Pre-flight — verify all templates present before proceeding:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# reload Shared-setup vars (Check 41)
IFS= read -r SKILL_DIR < "${TMPDIR:-/tmp}/release-setup-${CSID}/SKILL_DIR" 2>/dev/null || SKILL_DIR=""
IFS= read -r BRANCH < "${TMPDIR:-/tmp}/release-setup-${CSID}/BRANCH" 2>/dev/null || BRANCH=""
IFS= read -r DATE < "${TMPDIR:-/tmp}/release-setup-${CSID}/DATE" 2>/dev/null || DATE=""
IFS= read -r DO_APPEND < "${TMPDIR:-/tmp}/release-do-append-${CSID}" 2>/dev/null || DO_APPEND="false"
[ -z "$SKILL_DIR" ] && echo "Error: could not locate release skill directory" && exit 1
for tmpl in release-draft.md audit-checks.md gather-prompt.md; do # timeout: 5000
    [ -f "$SKILL_DIR/templates/$tmpl" ] || {
        echo "Missing template: $tmpl — aborting"
        exit 1
    }
done
```

Before writing, fetch last 2–3 releases to check formatting conventions:

```bash
gh release list --limit 5                                                  # timeout: 30000
LATEST_TAG=$(gh release list --limit 100 --json tagName --jq '[.[] | select(.tagName | test("rc|dev|alpha|beta"; "i") | not)] | .[0].tagName // empty') # timeout: 30000
[ -z "$LATEST_TAG" ] || [ "$LATEST_TAG" = "null" ] && echo "No releases found — using template defaults" || gh release view "$LATEST_TAG"  # timeout: 15000
```

Existing releases deviate from templates → match tone and prose style only. **Never** use `# Changelog` structure for DRAFT.md — always use `release-draft.md` structure. `gh release list` empty → use template defaults.

Fetch origin URL for full changelog link:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
ORIGIN_URL=$(git remote get-url origin 2>/dev/null || echo "")  # timeout: 3000
# reload SKILL_DIR (Check 41)
IFS= read -r SKILL_DIR < "${TMPDIR:-/tmp}/release-setup-${CSID}/SKILL_DIR" 2>/dev/null || SKILL_DIR=""
cat "$SKILL_DIR/templates/release-draft.md"  # timeout: 5000
```

**DRAFT.md format guard**: must NOT start with `# Changelog`, must NOT use CHANGELOG section structure. CHANGELOG-format classification = internal working doc only — derive sections from it, don't copy verbatim.

Template (loaded above). Replace `[org]/[repo]` with actual values from `$ORIGIN_URL`. Omit empty sections.

Key difference from `prepare`: phases run inline (no subagent delegation); output to `DRAFT.md` and root `CHANGELOG.md`.

### Adversarial review

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# reload SKILL_DIR (Check 41)
IFS= read -r SKILL_DIR < "${TMPDIR:-/tmp}/release-setup-${CSID}/SKILL_DIR" 2>/dev/null || SKILL_DIR=""
cat "$SKILL_DIR/modes/adversarial-review.md"  # timeout: 5000
```

Follow above and execute.

<!-- loads: modes/release-draft-template.md -->

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# reload SKILL_DIR (Check 41)
IFS= read -r SKILL_DIR < "${TMPDIR:-/tmp}/release-setup-${CSID}/SKILL_DIR" 2>/dev/null || SKILL_DIR=""
cat "$SKILL_DIR/modes/release-draft-template.md"  # timeout: 5000
```

Follow above and execute (format templates, semantic consistency review, polish, shepherd spawn, write to disk).

## Mode: prepare

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# reload SKILL_DIR (Check 41)
IFS= read -r SKILL_DIR < "${TMPDIR:-/tmp}/release-setup-${CSID}/SKILL_DIR" 2>/dev/null || SKILL_DIR=""
[ -f "$SKILL_DIR/modes/prepare.md" ] || { echo "Error: modes/prepare.md not found at $SKILL_DIR/modes/prepare.md — verify oss plugin installation"; exit 1; }
cat "$SKILL_DIR/modes/prepare.md"  # timeout: 5000
```

Follow above and execute.

> Confidence block — prepare mode: end response with `## Confidence` block per CLAUDE.md output standards after prepare.md completes.

## Mode: audit

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# reload SKILL_DIR (Check 41)
IFS= read -r SKILL_DIR < "${TMPDIR:-/tmp}/release-setup-${CSID}/SKILL_DIR" 2>/dev/null || SKILL_DIR=""
[ -f "$SKILL_DIR/modes/audit.md" ] || { echo "Error: modes/audit.md not found at $SKILL_DIR/modes/audit.md — verify oss plugin installation"; exit 1; }
# refuse to audit already-published release
IFS= read -r _REST_LINE < "${TMPDIR:-/tmp}/release-rest-${CSID}" 2>/dev/null || _REST_LINE=""; _AUDIT_VERSION=${_REST_LINE%% *}
if [ -n "$_AUDIT_VERSION" ]; then
    if gh release view "$_AUDIT_VERSION" --json tagName --jq .tagName >/dev/null 2>&1; then  # timeout: 15000
        echo "! BLOCKED — $_AUDIT_VERSION is already a published release on GitHub. /release audit checks FORWARD readiness only."
        echo "  For retrospective analysis use: /oss:analyse  (requires oss plugin)"
        exit 1
    fi
fi
cat "$SKILL_DIR/modes/audit.md"  # timeout: 5000
```

Follow above and execute.

> Confidence block — audit mode: end response with `## Confidence` block per CLAUDE.md output standards after audit.md completes.

## Mode: demo

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# reload SKILL_DIR (Check 41)
IFS= read -r SKILL_DIR < "${TMPDIR:-/tmp}/release-setup-${CSID}/SKILL_DIR" 2>/dev/null || SKILL_DIR=""
[ -f "$SKILL_DIR/modes/demo.md" ] || { echo "Error: modes/demo.md not found at $SKILL_DIR/modes/demo.md — verify oss plugin installation"; exit 1; }
cat "$SKILL_DIR/modes/demo.md"  # timeout: 5000
```

Follow above and execute.

> Confidence block — demo mode: end response with `## Confidence` block per CLAUDE.md output standards after demo.md completes.

</workflow>

<notes>

- **Doc artifacts ≠ released product**: CHANGELOG.md, DRAFT.md, MIGRATION.md, SUMMARY.md, demo.py = communication artifacts; released product published separately via `git tag`, `gh release create`, PyPI upload.
- **AskUserQuestion usage**: spread across `notes`/`prepare`/`audit`/`demo` modes; each call in distinct branch-path (no single path has >4 sequential calls); compliant with sequential-call limit.
- **Numbers reference**: numeric limits documented with rationale in `guidelines/numbers-reference.md`; update whenever limits change
- Filter noise (CI config, dep bumps, typos) unless user-impacting
- **Public-facing content policy**: user-visible changes only. Never include: internal staff names, internal maintenance, CI/tooling, internal dep bumps, housekeeping with no user impact.
- **Contributor email privacy**: `.temp/` must be in `.gitignore` — emails from `git log --format="%aN <%aE>"` must not leak into repo.
- Public-facing output co-authored with `oss:shepherd` (requires `oss` plugin) — follow shepherd voice protocol:
  ```bash
  export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
  IFS= read -r _OSS_SHARED < "${TMPDIR:-/tmp}/release-oss-shared-${CSID}" 2>/dev/null || _OSS_SHARED=""
  cat "$_OSS_SHARED/shepherd-voice.md"  # timeout: 5000
  ```
- **Demo mode output**: jupytext percent format — convert with `jupytext --to notebook <file>.py`; replace placeholder URLs before publishing; Colab badge URL must point to actual notebook after upload

<!-- branch: demo-synthetic-fallback — only when real data unavailable; isolated deep in demo path -->

- **Demo real-world-only policy**: use actual project data/fixtures/API — synthetic requires explicit user approval; fallback: (1) document each failed attempt in `## Demo attempts`, (2) ask Codex if available, (3) ask user via `AskUserQuestion`, (4) synthetic only on explicit approval
- **Changelog audit non-destructive**: adds missing entries, flags extras, never removes automatically
- **`--append` marker**: `.temp/release-last-processed-<branch>` — not date-stamped like sibling `.temp/release-*` artifacts (must survive across days/sessions); losing it (TTL cleanup, gitignore) degrades safely to the existing full-range/full-overwrite behavior, never to corruption — see `bin/release_append_marker.py` docstring for the full rationale.
- **Provenance store (patch-id keyed)**: `.temp/release-provenance-<branch>.json` — same cross-session lifecycle reasoning as the marker (per-branch, `.temp/`, gitignored, not date-stamped). Array of `{patch_id, sha, subject, artifact, anchor_text, written_at}` records, one per (contributing commit, artifact, exact-written-text) tuple; every `notes`-mode write (full regenerate or `--append` merge) appends a record for each newly-written bullet/entry that traces to specific commit(s) — see "Post-write bookkeeping" → "Provenance record" in `release-draft-template.md`. Consumed by Gather changes' cross-cycle revert detection: a genuine `git revert` commit's own `This reverts commit <sha>.` trailer is converted to its `git patch-id --stable` and looked up here — a hit means an exact, deterministic `{artifact, anchor_text}` strike, no text-matching guesswork. **`patch_id` is the only lookup key**: a raw commit sha changes on `--amend`, `rebase`, or `cherry-pick` even when the diff is untouched, so a sha-keyed store would silently miss a revert of a commit reworded or cherry-picked since it was recorded; `git patch-id --stable` is a normalized hash of the diff content and survives all three (verified empirically: identical patch-id across `--amend` and cherry-pick onto another branch, while the sha changed each time). `sha` and `subject` are carried for human debugging only, never used as a matching key. A commit whose diff produces no stable patch-id (a merge commit shown without `-m`, or a genuinely empty commit) is recorded with `patch_id: null` and can only ever be struck via the semantic path — a documented gap, not a bug. Recorded artifacts in practice: DRAFT.md (Notable-changes, Spotlights, Migration guide), `$CHANGELOG_FILE`, standalone `MIGRATION.md` — never Summary (DRAFT.md's own section or standalone `SUMMARY.md`, both additive-only prose with no removal path) or Contributors (per-person, not per-commit-revertible). Losing the store (TTL cleanup) degrades the revert path to the same semantic-grep fallback already used for pivots — never to corruption or a silently-missed strike (Semantic consistency review is still the backstop).
- **`--append` integration scope**: covers every DRAFT.md section (Summary, Spotlights, Migration guide, Notable-changes subsections, Contributors), plus root-level `SUMMARY.md`/`MIGRATION.md` when their flags are set — all merged via Read + Edit tool (see `release-draft-template.md` "Append merge"), not a parsing script. Purely additive except a detected cross-cycle revert/pivot (Gather changes' `CROSS_CYCLE_MATCH`), which strikes the specific stale entry instead of leaving a contradicting pair.
- **Post-merge re-validation gates only the merge path**: Truth check / Identify highlights / Validate migration docs / Validate docs re-run against the final merged DRAFT.md only when `$MARKER_VALID == true` (see release-draft-template.md). A full regenerate (no marker, or `prepare`) is already single-pass-valid — nothing accumulated from a prior cycle to re-check.
- **Collapse guard**: `SUMMARY.md`/`MIGRATION.md` merges (whole-file artifacts, no section structure to sanity-check against) carry a mechanical byte-count trip-wire — content collapsing from substantial to near-empty during a merge cycle is refused and restored from the pre-merge Read, never silently written (see `release-draft-template.md` "Collapse guard"). DRAFT.md's own sections can legitimately empty down to a dropped header (all items struck, nothing added) — that's intended, not guarded against; the guard is scoped to the two headerless artifacts where a full-file wipe was the actual historical bug.
- **Every `notes`-mode write refreshes the marker** — including plain (non-`--append`) runs. New side effect for users who've never used `--append`: a `.temp/release-last-processed-<branch>` file now appears. Harmless (gitignored, seeds a correct baseline for later `--append` adoption) and does not change `DRAFT.md`/`CHANGELOG.md` output — noted here so it's not a surprise.
- **"### Since last draft" accumulates, never reconciles**: each `--append` cycle's Summary paragraph piles up under this subheading (DRAFT.md's `📋 Summary` section and standalone `SUMMARY.md` both) with no `remove` path across cycles — cosmetic drift after many cycles, not a correctness or data-loss issue. Reconciling into the main paragraph (or trimming) happens, if at all, at the next full regenerate (no marker / `prepare`).
- Follow-up chains:
  - Readiness check → `/oss:release prepare <version>` runs built-in audit first; use standalone `/oss:release audit [version]` only for readiness check without cutting release
  - Breaking changes → `/oss:analyse` (requires `oss` plugin) for ecosystem impact
  - Notes/changelog written → `gh release create` must be user-run via project tooling
  - `migration` content written → add to project docs, link from CHANGELOG entry

</notes>

<calibration>

Registered: `notes` mode — classification accuracy (change type, section assignment, noise filtering).

Future candidates (not yet registered): `prepare` (pipeline completeness), `audit` (verdict accuracy: READY/NEEDS_ATTENTION/BLOCKED), `demo` (headline feature selection, narrative quality, code cell correctness).

</calibration>
