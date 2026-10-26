<!-- file: efficiency-measured.md — consumers: efficiency.md -->

**Phase B3 — Measured evidence for inline `model:` pins.** Runs only with `--measured`; zero model tokens; installed and `--local` alike, over every root Phase B scanned. Without the flag nothing reads a transcript or writes a ledger. Skills often run in repositories other than the one audited, so the projects are chosen, not assumed.

> loads: bin/efficiency_measured.py — reads transcripts under `~/.claude/projects` read-only; the report carries ids, counts and dollars, never message text.

B3a — list the projects whose sessions ran a flagged skill in the last 30 days (tab-separated `project`, `sessions`, `last_active`, `current`; a project's Claude worktrees count under it):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r RUN_DIR < "${TMPDIR:-/tmp}/audit-state-${CSID}/run-dir" 2>/dev/null || RUN_DIR=""
IFS= read -r MEASURED < "${TMPDIR:-/tmp}/audit-state-${CSID}/measured" 2>/dev/null || MEASURED="false"
[ "$MEASURED" = "true" ] && [ -n "$RUN_DIR" ] || { echo "Phase B3 skipped — no --measured"; exit 0; }
echo "RUN_DIR=$RUN_DIR"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/efficiency_measured.py" --list-projects --scan-file "$RUN_DIR/efficiency-scan.txt" --since 30d || echo "Phase B3: no project has a session that ran a flagged skill in the window, or the listing failed (see stderr) — measured evidence absent"  # timeout: 120000
```

B3b — skip when B3a printed `Phase B3 skipped` or listed no project (a `skipped <id>: <Error>` stderr line only names an unreadable transcript and does not skip). Otherwise ask once: `AskUserQuestion`, `multiSelect: true`, header `B3 projects`, question `Measure which projects' transcripts for model-switch cost?` (plain lines, no table; session counts come from the B3a listing). At most 4 options: the current project (`current` = `yes`) second, the rest by session count; more than 4 listed → last option `All listed projects`; exactly one listed → add `None`. Label = the project name exactly as listed, nothing added (it is written to the file, so never `(Recommended)`); description = `N sessions, last <date>`, plus `current project, recommended` for the current one. Write the chosen names, one per line and nothing else, to `<RUN_DIR>/measured-projects.txt` (`RUN_DIR` as B3a printed it; `All listed projects` writes every listed name; an "Other" answer is written as typed). `None` or nothing chosen → write an empty file, so an earlier pass's choice is never reused; B3c then skips. The collector exits 2 on a name with no transcript directory, stopping the pass rather than measuring another set.

B3c — measure the chosen projects and record the run:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r RUN_DIR < "${TMPDIR:-/tmp}/audit-state-${CSID}/run-dir" 2>/dev/null || RUN_DIR=""
IFS= read -r MEASURED < "${TMPDIR:-/tmp}/audit-state-${CSID}/measured" 2>/dev/null || MEASURED="false"
[ "$MEASURED" = "true" ] && [ -s "$RUN_DIR/measured-projects.txt" ] || { echo "Phase B3 skipped — no projects chosen"; exit 0; }
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/efficiency_measured.py" --scan-file "$RUN_DIR/efficiency-scan.txt" --projects-file "$RUN_DIR/measured-projects.txt" --since 30d --record --output "$RUN_DIR/measured-efficiency.md"  # timeout: 120000
_RC=$?
case "$_RC" in 0) ;; 1) echo "Phase B3: no session in the chosen projects ran a flagged skill in the window — measured evidence absent" ;; *) echo "⚠ Phase B3 failed (exit $_RC) — measured evidence missing; say so in the report" ;; esac
```

Measures every skill the scan flags `INLINE_MODEL` plus every label already in `.notes/efficiency/baselines.jsonl`, so a de-pinned skill keeps its pre-fix comparison; each skill from each typed invocation to the next different command. `--record` appends one record per skill with the chosen projects; the first is the pinned baseline, and each run prints the change against it and the previous run, with how many of its sessions the baseline already held, warning when projects, window or border-rewrite counting differ. The rolling 30-day window still holds pre-fix sessions after a pin is removed; they lack border rewrites and read as a saving no new run produced, so report a de-pinned skill's delta as provisional until the window holds only post-fix runs. Report: `measured-efficiency.md` in the run directory. Covers inline `model:` pins and previously recorded skills only, not spawn tiers or briefs.
