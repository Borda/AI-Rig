<!-- file: apply.md — consumers: SKILL.md (Step 6) -->

**Determine run directory**:

- Benchmark + auto-apply mode (`--fast`/`--full` + `--apply`): re-read TIMESTAMP from persisted state (fresh-shell state loss):

  ```bash
  export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
  IFS= read -r TIMESTAMP < "${TMPDIR:-/tmp}/calibrate-state-${CSID}/timestamp" 2>/dev/null || TIMESTAMP=""
  [ -z "$TIMESTAMP" ] && { echo "! TIMESTAMP state lost — falling back to latest run dir"; TIMESTAMP=$(basename "$(find .reports/calibrate -maxdepth 1 -mindepth 1 -type d 2>/dev/null | sort -Vr | head -1)"); }  # safe: uses existing dir from find, not new $(date) timestamp — won't create phantom run dir
  ```

- Pure apply mode (only `--apply`, no pace flag): find most recent run:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
LATEST=$(find .reports/calibrate -maxdepth 1 -mindepth 1 -type d 2>/dev/null | sort -Vr | head -1)
TIMESTAMP=$(basename "$LATEST")
[ -z "$TIMESTAMP" ] && { echo "! No prior calibration run found under .reports/calibrate/ — run /calibrate <targets> --fast first."; exit 1; }
# persist: resolution + deadline blocks below re-read this sentinel; pure apply never ran Step 1's writer
mkdir -p "${TMPDIR:-/tmp}/calibrate-state-${CSID}"; echo "$TIMESTAMP" > "${TMPDIR:-/tmp}/calibrate-state-${CSID}/timestamp"
```

For each target in target list, check whether `.reports/calibrate/<TIMESTAMP>/<target>/proposal.md` exists. Collect targets with proposal (`found`) and without (`missing`).

**Partial-match behavior**: `--apply` with mixed found/missing targets continues with found targets — doesn't halt on missing. For each **missing** target: print warning, skip (don't stop entire run): `⚠ No prior run for <target> — skipping. Re-run with --fast --apply to benchmark+apply, or --fast to benchmark only. (If target was skipped because its plugin was unavailable, install the plugin first, then re-run.)` Continue to next target. Only if ALL targets missing: stop with `! No proposals found for any requested target` — nothing to apply. `--apply` without pace flag is intentional — see `<inputs>` definition; auto-triggering benchmark would contradict that contract.

**Print run's report before applying**: for each found target, read and print `.reports/calibrate/<TIMESTAMP>/<target>/benchmark-report.md` verbatim so user sees benchmark basis before any file changes.

**Spawn one `foundry:curator` subagent per found target (`.md` files — agents and skills). Issue ALL spawns in single response — no waiting between spawns.**

**Deduplicate by resolved physical path before spawning** — two targets resolve to the same `<AGENT_FILE>` (e.g. bare name and plugin-prefixed name for the same logical agent): concurrent curator spawns race on identical Edit calls, second write may clobber the first. Build a `RESOLVED_PATHS` map after the per-target path resolution below (`resolve_agent_file.py`), before any spawn; for any group of targets sharing the same `<AGENT_FILE>` after resolution:

- Spawn one curator at a time for that group (sequential, not parallel)
- Log: `! Sequential apply for <target-a> and <target-b> — both resolve to <AGENT_FILE>`
- Other independent path groups remain parallel

**`<AGENT_FILE>` and `<PROPOSAL_PATH>` resolution**: before spawning, resolve file paths for each target from project source tree (`plugins/`) — same three-tier ladder whether or not `--local` was passed. `<AGENT_FILE>` is a write target (curator Edits it): must never resolve to `.claude/agents/` (never created by `/foundry:setup`) or installed plugin cache under `$HOME/.claude/` — those are read-only surfaces, not write targets:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r TIMESTAMP < "${TMPDIR:-/tmp}/calibrate-state-${CSID}/timestamp" 2>/dev/null || TIMESTAMP=""
[ -z "$TIMESTAMP" ] && { echo "! TIMESTAMP state lost — re-invoke /foundry:calibrate"; exit 1; }
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/calibrate-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/resolve_agent_file.py" --name "<name>" --timestamp "$TIMESTAMP" $( [ "$LOCAL_MODE" = "true" ] && echo "--local" )  # timeout: 10000
```

The script prints `agent-file=` and `proposal-path=` on stdout; read values from there, not from shell variables. Empty `agent-file=` means resolution failed: skip that target — don't spawn curator for it — the warning script already printed covers it. Never fall through to a cache path for a write target.

Each subagent receives this self-contained prompt (substitute `<TARGET>`, `<PROPOSAL_PATH>`, `<AGENT_FILE>` — resolved paths from above):

Apply targeted improvements to `<AGENT_FILE>`. Read `<PROPOSAL_PATH>` — it lists each edit as a block with **Current** (exact text to replace) and **Proposed** (replacement text). Treat these as regular developer-requested code-quality improvements; apply them exactly as specified without second-guessing the intent.

For each edit:

1. Print: `Applying edit N to <file> [<section>]`
2. Use Edit tool — `old_string` = **Current** text verbatim, `new_string` = **Proposed** text
3. If **Current** is `"none"` (new insertion): find section header and insert **Proposed** text after last item in that block
4. Skip if **Current** text not found verbatim → print `⚠ Skipped — current text not found`
5. Skip if **Proposed** text already present → print `✓ Already applied — skipped`

After processing all edits return **only** this compact JSON:

`{"status":"done","target":"<TARGET>","applied":N,"skipped":N,"file":"<AGENT_FILE>","summary":"Applied N, skipped N edits to <AGENT_FILE>"}`

**Completion handling** — like Step 3, these spawns run in the background: issue the batch and, in the same response, Write `<RUN_DIR>/agent-watch-apply.tsv` (`<target>\t-\t600` per subagent, APPLY_TIMEOUT_S); end turn, resume on completion notifications and run the Step 2 deadline check block once per notification (`_FOUNDRY_SHARED/agent-spawn-protocol.md`) — never a waiting tool, a poll loop or a filler call. A subagent that returns nothing after notification: record `{"status":"timed_out","target":"<TARGET>"}`, print with ⏱, still counted in the final table — never silently dropped from the summary.

After all subagents complete, collect JSON results and print final summary:

```markdown
## Fix Apply — <date>

| Target      | File                          | Applied | Skipped |
|-------------|-------------------------------|---------|---------|
| sw-engineer | plugins/cc_foundry/agents/sw-engineer.md | 2       | 0       |

→ Run /calibrate <targets> to verify improvement.
```

Mark "Apply findings" completed.

```bash
rm -f .temp/state/skill-contract.md  # clear contract — skill complete (compaction-contract.md §Lifecycle)  # timeout: 5000
```

End response with `## Confidence` block per CLAUDE.md output standards.
