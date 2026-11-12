---
name: release
description: "Prepare release communication and check readiness. Main mode: notes with optional flags --changelog, --summary, --migration, --append (incremental: reruns the full pipeline scoped to newly-landed commits, integrating results into existing DRAFT.md/CHANGELOG.md/SUMMARY.md/MIGRATION.md instead of full regenerate — non-destructive except revert/pivot and the re-folded DRAFT.md Summary); range as v1->v2. Other modes: prepare (full pipeline: audit → all artifacts), audit (pre-release readiness: blockers, docs alignment, version consistency, CVEs), demo (story-telling release notebook in jupytext # %% format). TRIGGER when: user requests release notes, CHANGELOG entry, migration guide, internal summary, release readiness audit, release demo, or an incremental update to an already-drafted release; phrases: 'draft release notes', 'prepare release', 'audit release readiness', 'generate CHANGELOG for v1->v2', 'release demo notebook', 'update release notes with the latest commits'. SKIP: actual git tagging or PyPI/registry upload (use git tag, gh release create, twine upload directly); release communication for a non-Python project where this skill's pytest-centric audit assumptions do not apply; PR-level review (use /oss:review); thread/issue analysis (use /oss:analyse)."
argument-hint: '[notes] [v1->v2] [--changelog] [--summary] [--migration] [--append] | prepare <version> | audit [version] | demo [range]'
allowed-tools: Read, Write, Edit, Bash, TaskList, TaskCreate, TaskUpdate, Agent, Skill, AskUserQuestion, WebFetch
effort: high
---

<objective>

Prepare release communication from changes. Output adapts to audience — user-facing notes, CHANGELOG entry, internal summary, migration guide.

**All outputs = documentation artifacts** (CHANGELOG.md, DRAFT.md, MIGRATION.md, SUMMARY.md, demo.py). Released product = code/package published separately via project tooling (`git tag`, `gh release create`, PyPI upload). Skill prepares communication; doesn't perform release.

NOT for ecosystem impact without release (use oss:analyse). NOT for contributor communication or post-release announcements (use oss:shepherd). NOT for retrospective analysis — historical review → oss:analyse. A request matching any NOT-for or SKIP case gets the redirect only: print `! UNSUPPORTED` naming the matching skill, then stop. Never do any part of that skill's work (thread summary, repro steps, duplicate check, or similar), before or after the redirect.

</objective>

<inputs>

Mode comes **first**; range or flags follow:

| Invocation | Arguments | Writes to disk |
| -- | -- | -- |
| `/oss:release [notes] [range]` | optional range (default: last-tag..HEAD); use `v1->v2` for explicit range | `DRAFT.md` |
| `/oss:release notes [range] --changelog` | optional range + flag | `DRAFT.md` + prepends `CHANGELOG.md` |
| `/oss:release notes [range] --summary` | optional range + flag | `DRAFT.md` + `.temp/output-release-summary-<branch>-<date>.md` |
| `/oss:release notes [range] --migration` | optional range + flag | `DRAFT.md` + `.temp/output-release-migration-<branch>-<date>.md` |
| `/oss:release notes [range] --changelog --summary --migration` | all flags | All four outputs |
| `/oss:release notes --append` | no range (derived from last-processed marker); compose with `--changelog`/`--summary`/`--migration` | Runs the full pipeline scoped to the incremental range, integrating results into every existing artifact **in place** (`DRAFT.md` always; `CHANGELOG.md`/`SUMMARY.md`/`MIGRATION.md` when their flag is set) instead of regenerating from scratch |
| `/oss:release prepare <version>` | version to stamp, e.g. `v1.3.0` | All artifacts in `releases/<version>/`: `DRAFT.md` + `CHANGELOG.md` + `SUMMARY.md` + `MIGRATION.md` + `demo.py` + `waived-changes.md` |
| `/oss:release audit [version]` | optional target version | Terminal readiness report; emits `verdict: READY \| NEEDS_ATTENTION \| BLOCKED` as final line for orchestrator consumption |
| `/oss:release demo [range]` | optional range (default: last-tag..HEAD) | `releases/<version>/demo.py` or `.temp/release-demo-<branch>-<date>.py` (glob `.temp/release-demo-*.py`) |

Range notation: `v1->v2` (e.g. `v1.2->v2.0`) — converted internally to git range. No mode → defaults to `notes`. `prepare` = full pipeline — runs audit first, then all artifacts; use when cutting release, not drafting.

`--append`: assumes an earlier `notes` run already produced `DRAFT.md` (and, when their flags were used, `CHANGELOG.md`/`SUMMARY.md`/`MIGRATION.md`) and reruns the **full pipeline** — Gather changes through Draft executive summary, unchanged — scoped to only the commits landed since then, via a per-branch marker at `.temp/release-state-v2/<branch-key>/marker` (see `bin/release_append_marker.py`). No marker found (first use, or history rewritten by rebase/force-push) → falls back to the default `$LAST_TAG..HEAD` range and full-overwrite write, same as plain `notes` — establishing the baseline for the next `--append` run. Every successful `notes`-mode write (append or full) refreshes the marker to the completed range endpoint; an explicit plain-notes range ending before `HEAD` leaves later commits for a future append.

**Non-destructive except revert/pivot**: integration is purely additive — new bullets/blocks/paragraphs join existing artifacts without touching untouched content — except DRAFT.md's Summary, which every cycle re-folds (top-5 wins, re-derived upgrade call, one `New since last draft` pointer line), and *unless* this cycle's Classify/Truth-check phases detect that a new commit reverts or materially changes something a PRIOR cycle already wrote (see Gather changes' "Cross-cycle revert/pivot detection"); that stale entry is struck or superseded, never left stale alongside a contradicting new one.

- After merge, a **Post-merge re-validation** pass (see `modes/release-draft-template.md`) re-runs Truth check, Identify highlights re-ranking, Validate migration docs, and Validate docs against the FINAL merged content — catches prior-cycle content that went stale from THIS cycle's changes without being a clean detected revert/pivot (e.g. a Spotlight built on a commit a later cycle reverts).

</inputs>

<workflow>

**Task hygiene** — follow `agent-resolution.md` §Task hygiene (loaded above); keep this workflow's task names and timing below.

**Task tracking** — create ALL tasks upfront, execute sequentially; mark completed as each phase finishes. After mode detection, mark inapplicable tasks `deleted`. **Zero bookkeeping-only turns**: issue every `TaskCreate` in one response together with the first real tool call, every mode-detection `deleted` in one response together with the next real call, and each phase's `completed` plus the next phase's `in_progress` in the response that carries that next phase's first call — never a response of task calls alone, except a `completed` right before a long output block:

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
- Post-merge re-validation (`--append` merge only — re-runs Truth check, Identify highlights, Validate migration docs, Validate docs, and the Summary shape/duplication check against the final merged DRAFT.md; see release-draft-template.md "Post-merge re-validation")

**Sequential enforcement**: never begin phase until prior marked `completed`. On failure (empty range, git error, demo fail), stop and report — no downstream phases.

**Agent wait discipline — no polling, per-agent deadlines.** Every spawn (gather, changelog audit, adversarial review, shepherd) runs in the background: spawn, end the turn, resume on its completion notification. Never `ScheduleWakeup`, `ListAgents`, a `Monitor` loop, a `sleep` or a no-op call to wait. In each spawn response write `${TMPDIR:-/tmp}/release-setup-${CSID}/agent-watch-<batch>.tsv` with the Write tool (`<batch>` = `gather`, `changelog-audit`, `adversarial`, `shepherd`) — one row per agent, `<name><TAB><deliverable file, or - when it returns only an envelope><TAB>1800` — and at every wake-up run the check below once before reading agent output: `done` → consume; `timed_out`, or a notification that arrived without the deliverable → ⏱ `timed_out` now and follow that step's failure path. A ⏱ only informs; it never answers or skips a user question.

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
python "$PLUGIN_ROOT/bin/agent_watch.py" --state-dir "${TMPDIR:-/tmp}/release-setup-${CSID}"  # timeout: 5000
```

**State checks — one call.** Ad-hoc repository state (branch, HEAD, upstream, status lists, remotes, worktrees, merge-base against a ref) is one `python "${CLAUDE_PLUGIN_ROOT}/bin/git_state_snapshot.py"` call, never a run of separate `git status` / `branch` / `log -1` / `remote` calls; tag listing and range logs keep their own commands.

<!-- ../../assets/images/release-arch.svg diagrams the runs, gates and parallel fan-out. Documentation only, never loaded — update it in the same commit as any change to step order, gate placement, or agent fan-out. -->

## Delegation strategy

<!-- loads: modes/delegation-strategy.md -->

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
_OSS_RELEASE="$PLUGIN_ROOT/skills/release"
python "$PLUGIN_ROOT/bin/render_plugin_root.py" "$_OSS_RELEASE/modes/delegation-strategy.md"  # timeout: 5000
```

Follow the loaded section in order, then continue below.

## Mode Detection

| First token | MODE | Routing |
| -- | -- | -- |
| `prepare` | prepare | **Shared setup** first, then **Mode: prepare** |
| `audit` | audit | **Shared setup** first, then **Mode: audit** |
| `demo` | demo | **Shared setup** first, then **Mode: demo** |
| `notes` | notes | Strip `notes` token; parse flags and range from remainder (block below), then **Shared setup** |
| *(bare range or flag)* | notes | Parse flags and range from full string (block below), then **Shared setup** |
| *(none)* | notes | Parse with `RANGE=""`, no flags (block below), then **Shared setup**; run all phases |

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# --keep-state: no eval, writes release-{changelog,summary,migration,append,clean-args}-${CSID}; --inlined: release keeps no compaction contract, so it clears none
python "$PLUGIN_ROOT/bin/parse-skill-flags.py" --flags changelog,summary,migration,append --keep-state release --inlined -- "$ARGUMENTS" >/dev/null  # timeout: 5000
IFS= read -r DO_CHANGELOG < "${TMPDIR:-/tmp}/release-changelog-${CSID}" 2>/dev/null || DO_CHANGELOG=false
IFS= read -r DO_SUMMARY < "${TMPDIR:-/tmp}/release-summary-${CSID}" 2>/dev/null || DO_SUMMARY=false
IFS= read -r DO_MIGRATION < "${TMPDIR:-/tmp}/release-migration-${CSID}" 2>/dev/null || DO_MIGRATION=false
IFS= read -r DO_APPEND < "${TMPDIR:-/tmp}/release-append-${CSID}" 2>/dev/null || DO_APPEND=false
IFS= read -r CLEAN_ARGS < "${TMPDIR:-/tmp}/release-clean-args-${CSID}" 2>/dev/null || CLEAN_ARGS=""
FIRST=$(echo "$CLEAN_ARGS" | awk '{print $1}')
# REST keeps the parsed tail — flags remain available in their own sentinels for downstream readers
REST=""; case "$CLEAN_ARGS" in *" "*) REST="${CLEAN_ARGS#* }";; esac
echo "${REST:-}" > "${TMPDIR:-/tmp}/release-rest-${CSID}"
# strip mode token — else leaks into RANGE
_PARSE_INPUT="$CLEAN_ARGS"; case "$FIRST" in notes|prepare|audit|demo) case "$CLEAN_ARGS" in *" "*) _PARSE_INPUT="${CLEAN_ARGS#* }";; *) _PARSE_INPUT="";; esac;; esac
RANGE=$(echo "$_PARSE_INPUT" | grep -oE '[^ ]+([[:space:]]*->[[:space:]]*|\.\.)[^ ]+' | head -1 | tr -d '[:space:]')
for _a in $_PARSE_INPUT; do case "$_a" in --*) echo "⚠ unknown flag: $_a";; *) [ -z "$RANGE" ] && RANGE="$_a";; esac; done
RANGE="${RANGE/->/..}"
# persist (Check 41) — Gather-changes needs this for marker-based RANGE resolution
echo "${DO_APPEND}" > "${TMPDIR:-/tmp}/release-do-append-${CSID}"
echo "${DO_CHANGELOG}" > "${TMPDIR:-/tmp}/release-do-changelog-${CSID}"
echo "${DO_SUMMARY}" > "${TMPDIR:-/tmp}/release-do-summary-${CSID}"
echo "${DO_MIGRATION}" > "${TMPDIR:-/tmp}/release-do-migration-${CSID}"
# explicit range dies with this shell; only v1..v2 persists (lone token = prepare/audit version, not a range); always written so a prior run's range never leaks in
case "$RANGE" in *..*) printf '%s\n' "$RANGE";; *) printf '\n';; esac > "${TMPDIR:-/tmp}/release-range-${CSID}"
case "$FIRST" in prepare|audit|demo) RELEASE_MODE="$FIRST";; *) RELEASE_MODE="notes";; esac
printf '%s\n' "$RELEASE_MODE" > "${TMPDIR:-/tmp}/release-mode-${CSID}"
```

<!-- branch: unsupported-flags — isolated; ≤1 call; fires only when unknown flags present -->

**Unknown flags**: if any `⚠ unknown flag:` lines printed above, invoke `AskUserQuestion` naming those flags in its question text (tool output is not reply text the user reads) — (a) **Abort** (stop, re-invoke) · (b) **Continue ignoring**. On Abort: stop.

## Shared setup

Run this first. Extracted to `bin/release_setup.py` — resolves `SKILL_DIR`, `REPO_ROOT`, portable scratch `BRANCH`, raw `BRANCH_REF`, persistent `BRANCH_KEY`, `DATE`, `LAST_TAG`, `CHERRY_PICK_SUBJECTS`, `SOURCE_TAG_REF`, `SHIPPED_ELSEWHERE_TAGS`. It refuses detached HEAD before publishing branch state. Writes each var under `${TMPDIR:-/tmp}/release-setup-${CSID}/`; stable-branch banner and "no stable tag" warnings go to stderr. One block first runs the cold-start fallback (sets `$_OSS_SHARED`) and loads `agent-resolution.md`, then the setup script:

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_OSS_SHARED=$(python "$PLUGIN_ROOT/bin/resolve_shared_path.py" oss skills/_shared 2>/dev/null)  # timeout: 5000
# persist (Check 41)
echo "${_OSS_SHARED:-}" > "${TMPDIR:-/tmp}/release-oss-shared-${CSID}"
# loads: oss-shared-resolver.md
[ -d "$_OSS_SHARED" ] && cat "$_OSS_SHARED/agent-resolution.md"  # timeout: 5000
python "$PLUGIN_ROOT/bin/release_setup.py" || exit 1  # timeout: 10000
IFS= read -r SKILL_DIR < "${TMPDIR:-/tmp}/release-setup-${CSID}/SKILL_DIR" 2>/dev/null || SKILL_DIR=""
IFS= read -r REPO_ROOT < "${TMPDIR:-/tmp}/release-setup-${CSID}/REPO_ROOT" 2>/dev/null || REPO_ROOT=""
IFS= read -r BRANCH < "${TMPDIR:-/tmp}/release-setup-${CSID}/BRANCH" 2>/dev/null || BRANCH=""
IFS= read -r BRANCH_REF < "${TMPDIR:-/tmp}/release-setup-${CSID}/BRANCH_REF" 2>/dev/null || BRANCH_REF=""
IFS= read -r BRANCH_KEY < "${TMPDIR:-/tmp}/release-setup-${CSID}/BRANCH_KEY" 2>/dev/null || BRANCH_KEY=""
IFS= read -r RELEASE_MODE < "${TMPDIR:-/tmp}/release-mode-${CSID}" 2>/dev/null || RELEASE_MODE=""
IFS= read -r DATE < "${TMPDIR:-/tmp}/release-setup-${CSID}/DATE" 2>/dev/null || DATE=""
IFS= read -r LAST_TAG < "${TMPDIR:-/tmp}/release-setup-${CSID}/LAST_TAG" 2>/dev/null || LAST_TAG=""
IFS= read -r CHERRY_PICK_SUBJECTS < "${TMPDIR:-/tmp}/release-setup-${CSID}/CHERRY_PICK_SUBJECTS" 2>/dev/null || CHERRY_PICK_SUBJECTS=""
IFS= read -r SOURCE_TAG_REF < "${TMPDIR:-/tmp}/release-setup-${CSID}/SOURCE_TAG_REF" 2>/dev/null || SOURCE_TAG_REF=""
[ -z "$REPO_ROOT" ] && { echo "Error: release_setup.py failed — REPO_ROOT empty; verify oss plugin installation"; exit 1; }
[ -n "$BRANCH_REF" ] && [ -n "$BRANCH_KEY" ] && [ -n "$RELEASE_MODE" ] || { echo "Error: release branch identity or mode missing" >&2; exit 1; }
if [ "$RELEASE_MODE" = notes ]; then
    IFS= read -r DO_APPEND < "${TMPDIR:-/tmp}/release-do-append-${CSID}" 2>/dev/null || DO_APPEND="false"
    if [ -f ".temp/release-state-v2/$BRANCH_KEY/journal.json" ]; then
        python "$PLUGIN_ROOT/bin/release_append_publish.py" recover --branch "$BRANCH" || exit 1
        echo "Reconciled interrupted append publication; inspect marker and artifacts, then stop this invocation before gathering again."
        exit 0
    fi
    python "$PLUGIN_ROOT/bin/release_append_marker.py" guard --branch "$BRANCH_REF" || exit 1
fi
```

**Agent Resolution** — `agent-resolution.md` (loaded above): foundry check + fallback table. Foundry not installed → at every spawn substitute `foundry:X` with `general-purpose` + the table's role prefix and model. Agents this skill dispatches: `foundry:sw-engineer` (gather, size-guard gather, adversarial review), `foundry:doc-scribe` (changelog audit, contributors), `oss:shepherd` (public-facing voice review). The `foundry:humanizer` pass in `modes/release-draft-template.md` keeps its own availability rule. <!-- Inline fallback (if agent-resolution.md unreadable): foundry:sw-engineer → general-purpose, foundry:doc-scribe → general-purpose. -->

<!-- branch: no-stable-tags — isolated; ≤1 call; fires only when repo has no stable git tags -->

When no stable tags exist, `LAST_TAG` resolves to initial commit — surface via `AskUserQuestion` ("No stable tags found. Range base is initial commit — proceed?"). Options: (a) Proceed with initial commit as base · (b) Abort — stop release process. On (b): stop, print "Release aborted — no stable tags found; create a tag first with `git tag v0.1.0`" and exit.

## Gather changes

<!-- loads: modes/gather-changes.md -->

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
_OSS_RELEASE="$PLUGIN_ROOT/skills/release"
python "$PLUGIN_ROOT/bin/render_plugin_root.py" "$_OSS_RELEASE/modes/gather-changes.md"  # timeout: 5000
```

Follow the loaded section in order, then continue below.

## Explore codebase

<!-- loads: modes/scope-validation.md -->

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
_OSS_RELEASE="$PLUGIN_ROOT/skills/release"
python "$PLUGIN_ROOT/bin/render_plugin_root.py" "$_OSS_RELEASE/modes/scope-validation.md"  # timeout: 5000
```

Follow the loaded section in order, then continue below.

## Audit changelog

<!-- loads: modes/changelog-contributors.md -->

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
_OSS_RELEASE="$PLUGIN_ROOT/skills/release"
python "$PLUGIN_ROOT/bin/render_plugin_root.py" "$_OSS_RELEASE/modes/changelog-contributors.md"  # timeout: 5000
```

Follow the loaded section in order, then continue below.

## Identify highlights

Pick top 3–5 most significant changes from Classify. Ranking: breaking changes > new public API > major UX improvements > notable fixes. Pull concrete code example from explore-codebase diff for each. Drives Summary pitch and Spotlights section.

## Draft migration guide

Always produce. No breaking changes → single line "No breaking changes in this release." Deprecations/removals → show before→after code examples. State in preamble: API deprecated in prior release and now removed → ❌ Removed (not Breaking).

If `migration_gaps` non-empty (from Validate migration docs): for each `[MISSING-MIGRATION]` item, add dedicated section covering that symbol with before→after example. For each `[SHALLOW]` item, expand existing coverage to add concrete upgrade instructions.

If `breaking_callers` non-empty (from Breaking-change classification): for each Breaking symbol, add a before→after section citing its external call sites (the `migration_lines`) so downstream consumers see exactly which of their call sites must change. When that phase reported `query_complete:false`, prefix the section "Affected call sites (possibly-incomplete — codemap coverage partial)".

## Generate release demo

<!-- loads: modes/notes-draft.md -->

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
_OSS_RELEASE="$PLUGIN_ROOT/skills/release"
python "$PLUGIN_ROOT/bin/render_plugin_root.py" "$_OSS_RELEASE/modes/notes-draft.md"  # timeout: 5000
```

Follow the loaded section in order, then continue below.

## Mode: prepare

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# reload SKILL_DIR (Check 41)
IFS= read -r SKILL_DIR < "${TMPDIR:-/tmp}/release-setup-${CSID}/SKILL_DIR" 2>/dev/null || SKILL_DIR=""
[ -f "$SKILL_DIR/modes/prepare.md" ] || { echo "Error: modes/prepare.md not found at $SKILL_DIR/modes/prepare.md — verify oss plugin installation"; exit 1; }
python "$PLUGIN_ROOT/bin/render_plugin_root.py" "$SKILL_DIR/modes/prepare.md"  # timeout: 5000
```

Follow above and execute.

> Confidence block — prepare mode: end response with `## Confidence` block per CLAUDE.md output standards after prepare.md completes.

## Mode: audit

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# reload SKILL_DIR (Check 41)
IFS= read -r SKILL_DIR < "${TMPDIR:-/tmp}/release-setup-${CSID}/SKILL_DIR" 2>/dev/null || SKILL_DIR=""
[ -f "$SKILL_DIR/modes/audit.md" ] || { echo "Error: modes/audit.md not found at $SKILL_DIR/modes/audit.md — verify oss plugin installation"; exit 1; }
# refuse to audit already-published release
IFS= read -r _REST_LINE < "${TMPDIR:-/tmp}/release-rest-${CSID}" 2>/dev/null || _REST_LINE=""; _AUDIT_VERSION=${_REST_LINE%% *}
if [ -n "$_AUDIT_VERSION" ]; then
    if gh release view "$_AUDIT_VERSION" --json tagName --jq .tagName >/dev/null 2>&1; then  # timeout: 15000
        echo "! BLOCKED — $_AUDIT_VERSION is already a published release on GitHub. /oss:release audit checks FORWARD readiness only."
        echo "  For retrospective analysis use: /oss:analyse "
        exit 1
    fi
fi
python "$PLUGIN_ROOT/bin/render_plugin_root.py" "$SKILL_DIR/modes/audit.md"  # timeout: 5000
```

Follow above and execute.

> Confidence block — audit mode: end response with `## Confidence` block per CLAUDE.md output standards after audit.md completes.

## Mode: demo

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# reload SKILL_DIR (Check 41)
IFS= read -r SKILL_DIR < "${TMPDIR:-/tmp}/release-setup-${CSID}/SKILL_DIR" 2>/dev/null || SKILL_DIR=""
[ -f "$SKILL_DIR/modes/demo.md" ] || { echo "Error: modes/demo.md not found at $SKILL_DIR/modes/demo.md — verify oss plugin installation"; exit 1; }
python "$PLUGIN_ROOT/bin/render_plugin_root.py" "$SKILL_DIR/modes/demo.md"  # timeout: 5000
```

Follow above and execute.

> Confidence block — demo mode: end response with `## Confidence` block per CLAUDE.md output standards after demo.md completes.

</workflow>

<notes>

- **Doc artifacts ≠ released product**: CHANGELOG.md, DRAFT.md, MIGRATION.md, SUMMARY.md, demo.py = communication artifacts; released product published separately via `git tag`, `gh release create`, PyPI upload.
- **Numbers reference**: numeric limits documented with rationale in `guidelines/numbers-reference.md`; update whenever limits change
- Filter noise (CI config, dep bumps, typos) unless user-impacting
- **Public-facing content policy**: user-visible changes only. Never include: internal staff names, internal maintenance, CI/tooling, internal dep bumps, housekeeping with no user impact.
- **Contributor email privacy**: `.temp/` must be in `.gitignore` — emails from `git log --format="%aN <%aE>"` must not leak into repo.
- Public-facing output co-authored with `oss:shepherd` — follow shepherd voice protocol:
  ```bash
  export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
  IFS= read -r _OSS_SHARED < "${TMPDIR:-/tmp}/release-oss-shared-${CSID}" 2>/dev/null || _OSS_SHARED=""
  cat "$_OSS_SHARED/shepherd-voice.md"  # timeout: 5000
  ```
- **Demo mode output**: jupytext percent format — convert with `jupytext --to notebook <file>.py`; replace placeholder URLs before publishing; Colab badge URL must point to actual notebook after upload

<!-- branch: demo-synthetic-fallback — only when real data unavailable; isolated deep in demo path -->

- **Demo real-world-only policy**: use actual project data/fixtures/API — synthetic requires explicit user approval; fallback: (1) document each failed attempt in `## Demo attempts`, (2) ask Codex if available, (3) ask user via `AskUserQuestion`, (4) synthetic only on explicit approval
- **Changelog audit preserves history**: add missing entries and flag unrelated or uncertain extras. Remove only the exact confirmed stale Unreleased claim for an unshipped revert/pivot; preserve historical entries and every unrelated item.
- **`--append` notes** (marker location, patch-id provenance store, integration scope, post-merge re-validation gate, collapse guard, Summary folding): `modes/append-cross-cycle.md` § Append notes, loaded with the cross-cycle procedure for an `--append` run.
- **Every completed `notes`-mode write refreshes the marker** — including plain (non-`--append`) runs. Marker refresh uses the completed range endpoint after the final truth gate; an explicit range ending before `HEAD` never certifies later commits. The workflow then records a receipt bound to branch, source `HEAD`, range endpoint, and exact nonempty draft bytes, and `write` refuses missing or stale receipts. This is a workflow gate against accidental direct writes, not an attestation against a local caller who forges workflow inputs. New side effect for users who've never used `--append`: a `.temp/release-state-v2/<branch-key>/marker` file now appears. It is gitignored and seeds the baseline for later `--append` adoption without changing `DRAFT.md`/`CHANGELOG.md` output.
- **Confidence scores classification accuracy, not artifact completeness** (`notes` and `prepare` classification output; `audit` and `demo` keep the generic quality-gates Confidence rule): base the score on the evidence classification used — commit bodies, `BREAKING CHANGE:` footers, revert trailers, PR descriptions. Skipped Truth check / baseline / codemap verification and unresolved URLs or handles are listed as `(-0.00)` Gaps only when no specific classification depends on them. Deduct for a skipped Truth check with any ❌ Removed / ⚠️ Breaking Changes item, or when a specific classification hinges on other repo state that went unverified: prior deprecation (❌ Removed vs ⚠️ Breaking Changes), a revert whose original may predate `$RANGE`, or a Truth-check removal or reclassification.
- Follow-up chains:
  - Readiness check → `/oss:release prepare <version>` runs built-in audit first; use standalone `/oss:release audit [version]` only for readiness check without cutting release
  - Breaking changes → `/oss:analyse` for ecosystem impact
  - Notes/changelog written → `gh release create` must be user-run via project tooling
  - `migration` content written → add to project docs, link from CHANGELOG entry

</notes>

<calibration>

Registered: `notes` mode — classification accuracy (change type, section assignment, noise filtering).

Future candidates (not yet registered): `prepare` (pipeline completeness), `audit` (verdict accuracy: READY/NEEDS_ATTENTION/BLOCKED), `demo` (headline feature selection, narrative quality, code cell correctness).

</calibration>
