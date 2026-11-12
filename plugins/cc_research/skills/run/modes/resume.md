# Resume Mode — run/SKILL.md sidecar

Loaded by `run/SKILL.md` when `--resume` flag is set.

## Resume Mode

**Locating the run**:

Both forms consider only runs with `status: running` — `initializing` (failed init), `completed`, and `goal-achieved` runs are never resumed.

- `resume` (no argument): scan `.experiments/state/`, select run with latest `started_at` and `status: running`.
- `resume <file.md>`: resolve path to absolute. Scan all run dirs, filter by `status: running` and `"program_file"` matching. Pick latest `started_at`. If no match: stop with `! BLOCKED — no running run for <file.md>; finished runs cannot resume — start a new /research:run`.

**Restore session identity** — R1 never ran in this session, so its sentinels are missing (Phase 8 flush and `codex-copilot.md` exit BLOCKED without the run-id). Substitute the selected run's directory name for `<run-id>`:

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name research) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
RESUME_RUN_ID="<run-id>"
case "$RESUME_RUN_ID" in *'<'*'>'*|""|*/*) echo "! BLOCKED — resume: run-id not substituted"; exit 1;; esac
[ -f ".experiments/state/$RESUME_RUN_ID/state.json" ] || { echo "! BLOCKED — resume: .experiments/state/$RESUME_RUN_ID/state.json not found"; exit 1; }
mkdir -p ".experiments/$RESUME_RUN_ID"  # timeout: 5000 — RUN_DIR: hypothesis queue, checkpoint, journal
echo "$RESUME_RUN_ID" > "${TMPDIR:-/tmp}/research-run-id-${CSID}"
python "$PLUGIN_ROOT/bin/extract-keep-flag.py" research-run "$ARGUMENTS"  # timeout: 5000 — keep-items sentinel for Phase 8
python "$PLUGIN_ROOT/bin/codemap-flag.py" research-run "$ARGUMENTS" || exit 1  # timeout: 5000 — codemap-enabled sentinel; prints CODEMAP_RAW
echo "RUN_ID=$RESUME_RUN_ID RUN_DIR=.experiments/$RESUME_RUN_ID"
```

`RUN_DIR` = `.experiments/<run-id>` (the R1 convention). Codemap printed ≠ `off` → follow R1's codemap-gates block.

1. Read `state.json`. Restore `clarification_prompt`, `colab_hw`, `sandbox_mode`, and the `flags` object R1 persisted (`--team`, `--compute`, `--codex`, `--researcher`, `--architect`, `--journal`, `--hypothesis`, `--scientist`). `flags` absent (run started before R1 persisted it) → no stored flags. Precedence: flags passed with this `--resume` invocation override stored ones. `team_mode` present → team run: restore `RUN_DIR` = `team_mode.run_dir`, but defer dispatch until the recovery and precondition steps below finish.

2. **Re-parse program file**: `program_file` null or program file missing → `! BLOCKED — an existing program file is required`; stop before interrupted-iteration recovery. Otherwise re-read/re-parse (R1 rules) and re-write `state.json` `config` in R1's shape (fortify and retro read it) — applies edits made between runs. Re-run R1's `sandbox_network` sentinel block for the re-parsed value (the sentinel is per session). Edits during active loop take effect only on next `resume`.

3. **Validate `experiments.jsonl`**: read last line, parse as JSON. If truncated or invalid: invoke `AskUserQuestion` tool — question: "experiments.jsonl last line appears corrupt (truncated or invalid JSON). How to proceed?", (a) label: `truncate corrupt entry and resume`, (b) label: `abort — fix manually`. If (a), remove last line; if (b), stop.

4. Validate git ancestry: `best_commit` null → skip. Else `git merge-base --is-ancestor <best_commit> HEAD` — exit 0 is normal (revert commits stack on top of `best_commit`). Non-zero (history rewritten, other branch) → invoke `AskUserQuestion` tool — question: "HEAD no longer descends from best_commit in state.json. Continue anyway?", (a) label: `yes, continue from current HEAD`, (b) label: `no, abort`. If (b), stop.

5. **Recover interrupted iteration** — before R2 measures the current tree or any team Phase C dispatch, require R2's clean-tree and attached-branch checks, then inspect `git log -1 --format=%s` and the validated `experiments.jsonl` rows. For this preliminary clean-tree check only, exclude the program input without comparing its hash: step 2 deliberately accepts program edits between runs. Keep the old snapshot unchanged through recovery; step 6 binds the next active iteration to the re-parsed input. Let `<N>` = `state.json.iteration + 1`.

   - **Logged outcome (any mode)**: if iteration `<N>` already has an `experiments.jsonl` row, run the Phase 8 flush block once to append only records still staged; never delete these `.rec` files and never stage a second outcome for `<N>`. After the flush succeeds, set `state.json.iteration = <N>`, then do the team reconciliation below if applicable. Never revert, re-measure or rerun `<N>`, regardless of HEAD's subject.

   - **Unlogged outcome (any mode)**: HEAD subject starting with `experiment(optimize/i<N>):` and no row for iteration `<N>` means Phase 4 committed an unmeasured or unlogged change. Do not adopt it as the next iteration's starting tree. HEAD subject starting with `Revert "experiment(optimize/i<N>):` and no experiment row still needs the interrupted log recovery in any mode; skip the revert command and never revert it again.

     Before staging recovery records in this no-row branch, delete only stale `.rec` files for this interrupted iteration: `.experiments/state/<run-id>/experiments.jsonl.rec`, `.experiments/state/<run-id>/diary.md.rec`, `.experiments/state/<run-id>/metric-runs.jsonl.rec`, `<RUN_DIR>/checkpoint.jsonl.rec`, `<RUN_DIR>/journal.md.rec`, and `<RUN_DIR>/team-results.jsonl.rec`. Never flush these uncommitted outcomes or delete their ledgers.

     Reload the run-id and derive the interrupted iteration from persisted state, delete those six staged files, then use the existing guarded revert only for an unreverted experiment commit and verify the resulting HEAD:

     ```bash
     PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name research) || exit $?; export PLUGIN_ROOT
     export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
     command -v jq >/dev/null 2>&1 || { echo "! MISSING — jq not on PATH; install jq and retry"; exit 1; }
     IFS= read -r RUN_ID < "${TMPDIR:-/tmp}/research-run-id-${CSID}" 2>/dev/null || RUN_ID=""
     [ -n "$RUN_ID" ] || { echo "! BLOCKED — resume: run-id sentinel missing"; exit 1; }
     DONE_ITER=$(jq -b -r '.iteration // 0' ".experiments/state/$RUN_ID/state.json") || { echo "! BLOCKED — resume: state.json unreadable"; exit 1; }
     RUN_DIR=$(jq -b -r --arg run_id "$RUN_ID" '.team_mode.run_dir // (".experiments/" + $run_id)' ".experiments/state/$RUN_ID/state.json") || { echo "! BLOCKED — resume: state.json unreadable"; exit 1; }
     I=$((DONE_ITER + 1))
     echo "$I" > "${TMPDIR:-/tmp}/research-run-iter-${CSID}"
     HEAD_SUBJECT=$(git log -1 --format=%s) || { echo "! BLOCKED — resume: HEAD unreadable"; exit 1; }
     case "$HEAD_SUBJECT" in
         "experiment(optimize/i$I):"*|"Revert \"experiment(optimize/i$I):"*) ;;
         *) echo "! BLOCKED — resume: HEAD does not match interrupted iteration $I"; exit 1;;
     esac
     rm -f ".experiments/state/$RUN_ID/experiments.jsonl.rec" ".experiments/state/$RUN_ID/diary.md.rec" ".experiments/state/$RUN_ID/metric-runs.jsonl.rec" "$RUN_DIR/checkpoint.jsonl.rec" "$RUN_DIR/journal.md.rec" "$RUN_DIR/team-results.jsonl.rec" || exit 1
     case "$HEAD_SUBJECT" in
         "experiment(optimize/i$I):"*)
             python "$PLUGIN_ROOT/bin/revert_iteration_commit.py" --iteration "$I" --phase 5 || exit 1  # timeout: 15000
             ;;
     esac
     HEAD_SUBJECT=$(git log -1 --format=%s) || { echo "! BLOCKED — resume: HEAD unreadable"; exit 1; }
     case "$HEAD_SUBJECT" in
         "Revert \"experiment(optimize/i$I):"*) ;;
         *) echo "! BLOCKED — resume: interrupted iteration $I was not reverted"; exit 1;;
     esac
     ```

     Require revert success and verify HEAD changed from the interrupted experiment commit (or was already its revert); failure or conflict → stop, preserve evidence. Restore `best_metric`, `best_commit`, and repeated-run best statistics from the last logged baseline/kept result, excluding the interrupted iteration. Run Phase 7a and Phase 8 for `<N>` with `status: reverted`, `metric: null`, decision `interrupted before log — reverted`; append through Phase 8's `append_ledger.py` flush, then advance `state.json.iteration`. Do not invent a measurement or checkpoint an untested hypothesis as passed.

   - For team Phase C, reconcile `team_mode.active_iteration` / `active_queue_position` before deriving the next iteration, even when Phase 8 already advanced `state.json.iteration`. If that iteration has an `experiments.jsonl` row, use its recorded outcome to append the missing `team-results.jsonl` entry at the saved queue position through Phase C step 4; an existing row at that position is not appended again. Then advance `current_hypothesis` and clear the active association together as in Phase C step 6. Never re-measure, revert, or dispatch a hypothesis whose outcome is already logged. Older runs without the association must reconcile against their queue, existing team results, and iteration ideation artifacts first; an ambiguous mapping → `! BLOCKED — cannot identify interrupted team hypothesis`, preserve artifacts rather than replaying a potentially completed change.

6. **Re-run R2** — after interrupted recovery is complete, capture the re-parsed program's SHA256 with R1's `find_judge_verdict.py --hash-program` command and persist `program_sha256`. This refresh applies deliberate edits between runs and initializes legacy states lacking the field; never refresh the snapshot during an active iteration or before recovering one. For step 5's preliminary clean-tree check, exclude the program input from porcelain but keep the old snapshot for recovery scope checks. Then run precondition checks for the restored flags (clean git, not detached, metric numeric, guard passes, `--colab` / `--codex` / docker / `--journal` / `jq` checks, flag conflicts) and the sandbox + timeout variable init block. Status stays `running` — skip R2's status write. Then R3; skip R4 (baseline already in `state.json`).

7. `--researcher` / `--architect` / `--hypothesis` restored → rebuild `RESEARCH_QUEUE` via `hypothesis-pipeline.md`: step 1 reuses `<RUN_DIR>/hypotheses.jsonl` for oracle runs or the stored path in `flags.hypothesis` for `--hypothesis` (a path passed with this resume overrides it), read-only; step 3 skips checkpointed ids in `<RUN_DIR>/checkpoint.jsonl` (legacy `checkpoint.json` fallback unchanged). Never spawn oracles on resume.

8. **Continue restored mode** — team run: continue per `team.md` from `team_mode.phase` / `current_hypothesis`; otherwise continue the loop from `state.json.iteration + 1`. `diary.md` NOT re-initialized — entries append to existing file.
