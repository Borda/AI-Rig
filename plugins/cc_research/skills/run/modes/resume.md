# Resume Mode — run/SKILL.md sidecar

Loaded by `run/SKILL.md` when `--resume` flag is set.

## Resume Mode

Triggered by `--resume` flag (with optional `<file.md>` argument).

**Locating the run**:

Both forms consider only runs with `status: running` — `initializing` (failed init), `completed`, and `goal-achieved` runs are never resumed.

- `resume` (no argument): scan `.experiments/state/`, select run with latest `started_at` and `status: running`.
- `resume <file.md>`: resolve path to absolute. Scan all run dirs, filter by `status: running` and `"program_file"` matching. Pick latest `started_at`. If no match: stop with `! BLOCKED — no running run for <file.md>; finished runs cannot resume — start a new /research:run`.

**Restore session identity** — R1 never ran in this session, so its sentinels are missing (Phase 8 flush and `codex-copilot.md` exit BLOCKED without the run-id). Substitute the selected run's directory name for `<run-id>`:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
RESUME_RUN_ID="<run-id>"
case "$RESUME_RUN_ID" in *'<'*'>'*|""|*/*) echo "! BLOCKED — resume: run-id not substituted"; exit 1;; esac
[ -f ".experiments/state/$RESUME_RUN_ID/state.json" ] || { echo "! BLOCKED — resume: .experiments/state/$RESUME_RUN_ID/state.json not found"; exit 1; }
mkdir -p ".experiments/$RESUME_RUN_ID"  # timeout: 5000 — RUN_DIR: hypothesis queue, checkpoint, journal
echo "$RESUME_RUN_ID" > "${TMPDIR:-/tmp}/research-run-id-${CSID}"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/extract-keep-flag.py" research-run "$ARGUMENTS"  # timeout: 5000 — keep-items sentinel for Phase 8
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/codemap-flag.py" research-run "$ARGUMENTS" || exit 1  # timeout: 5000 — codemap-enabled sentinel; prints CODEMAP_RAW
echo "RUN_ID=$RESUME_RUN_ID RUN_DIR=.experiments/$RESUME_RUN_ID"
```

`RUN_DIR` = `.experiments/<run-id>` (the R1 convention). Codemap printed ≠ `off` → follow R1's codemap-gates block.

1. Read `state.json`. Restore `clarification_prompt`, `colab_hw`, `sandbox_mode`, and the `flags` object R1 persisted (`--team`, `--compute`, `--codex`, `--researcher`, `--architect`, `--journal`, `--hypothesis`, `--scientist`). `flags` absent (run started before R1 persisted it) → no stored flags. Precedence: flags passed with this `--resume` invocation override stored ones. `team_mode` present → team run: continue per `team.md` from `team_mode.phase` / `current_hypothesis`, with `RUN_DIR` = `team_mode.run_dir`.
2. **Re-parse program file**: if `program_file` non-null, re-read/re-parse (R1 rules) and re-write `state.json` `config` in R1's shape (fortify and retro read it) — applies edits made between runs. `program_file` null (text goal) → keep the stored `config`. Re-run R1's `sandbox_network` sentinel block for the re-parsed value (the sentinel is per session). Edits during active loop take effect only on next `resume`.
3. **Validate `experiments.jsonl`**: read last line, parse as JSON. If truncated or invalid: invoke `AskUserQuestion` tool — question: "experiments.jsonl last line appears corrupt (truncated or invalid JSON). How to proceed?", (a) label: `truncate corrupt entry and resume`, (b) label: `abort — fix manually`. If (a), remove last line; if (b), stop.
4. Validate git ancestry: `best_commit` null → skip. Else `git merge-base --is-ancestor <best_commit> HEAD` — exit 0 is normal (revert commits stack on top of `best_commit`). Non-zero (history rewritten, other branch) → invoke `AskUserQuestion` tool — question: "HEAD no longer descends from best_commit in state.json. Continue anyway?", (a) label: `yes, continue from current HEAD`, (b) label: `no, abort`. If (b), stop.
5. **Re-run R2** — precondition checks for the restored flags (clean git, not detached, metric numeric, guard passes, `--colab` / `--codex` / docker / `--journal` / `jq` checks, flag conflicts) and the sandbox + timeout variable init block. Status stays `running` — skip R2's status write. Then R3; skip R4 (baseline already in `state.json`).
6. `--researcher` / `--architect` restored → rebuild `RESEARCH_QUEUE` via `hypothesis-pipeline.md`: step 1 reuses `<RUN_DIR>/hypotheses.jsonl`, step 3 skips ids already in `<RUN_DIR>/checkpoint.jsonl`.
7. Continue loop from `state.json.iteration + 1`. `diary.md` NOT re-initialized — entries append to existing file.
