# Mode: Executables Extraction

<!-- file: executables.md — consumers: distill/SKILL.md -->

Triggered when the first normalized argument token is `executables`. Reads latest `/audit --efficiency` Check 33 reports if present; runs bin/ extraction scan inline otherwise. Then gates, extracts with user confirmation, re-audits changed files.

## Step E1: Locate or run scan

```bash
_FS=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/resolve_shared_path.py" foundry skills/_shared 2>/dev/null || echo "plugins/cc_foundry/skills/_shared")  # timeout: 5000
RUN_DIR=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/make_run_dir.py" .reports/distill 2>/dev/null || echo ".reports/distill/$(date -u +%Y-%m-%dT%H-%M-%SZ)")  # timeout: 5000
mkdir -p "$RUN_DIR"  # timeout: 5000

EXEC_ARGS="${ARGUMENTS#executables}"
EXEC_ARGS="${EXEC_ARGS# }"
if [ -n "$EXEC_ARGS" ]; then
  if [ -d "$EXEC_ARGS" ]; then
    mapfile -t CHECK33_FILES < <(ls "$EXEC_ARGS"/efficiency-check33-*.md 2>/dev/null)
  elif [ -f "$EXEC_ARGS" ]; then
    CHECK33_FILES=("$EXEC_ARGS")
  else
    printf "! MISSING — path not found: %s\n" "$EXEC_ARGS"
    exit 1
  fi
else
  LATEST_RUN=$(find .reports/audit -maxdepth 1 -type d -name "20*" 2>/dev/null \
    | sort -r \
    | while IFS= read -r d; do
        ls "$d"/efficiency-check33-*.md 2>/dev/null | head -1 | grep -q . && echo "$d" && break
      done)  # timeout: 5000
  mapfile -t CHECK33_FILES < <(ls "$LATEST_RUN"/efficiency-check33-*.md 2>/dev/null)
fi
echo "Check 33 files: ${#CHECK33_FILES[@]} (from ${LATEST_RUN:-$EXEC_ARGS})"
```

**If `CHECK33_FILES` non-empty**: proceed to Step E2 using those files.

**If `CHECK33_FILES` empty** (no prior `/audit --efficiency` run): print `[→ No efficiency report found — running bin/ extraction scan]` and execute scan inline:

Determine LOCAL_MODE-aware scan path and extract code blocks:

```bash
[ -d "plugins/" ] && _SCAN_DIR="plugins/" || _SCAN_DIR=".claude/"  # timeout: 3000
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/extract_code_blocks.py" "$_SCAN_DIR" --min-tokens 5 > "$RUN_DIR/blocks.jsonl"  # timeout: 30000
```

Spawn **foundry:curator** per plugin directory found under `$_SCAN_DIR` (one spawn per plugin, all in parallel — issue all in a single response). Pass curator relevant slice of `$RUN_DIR/blocks.jsonl` (filter lines where `"file"` prefix matches plugin dir). Each spawn prompt:

> Use the attached block JSONL (pre-extracted via extract_code_blocks.py — each line is `{"file":..., "lang_marker":..., "line_start":..., "token_estimate":..., "content":...}`). Follow the full Check 33 Phase B2 protocol from `audit/modes/efficiency.md` exactly: assign block IDs, write purpose statements, build purpose clusters, compute syntactic similarity, produce Table 1 (purpose clusters) and Table 2 (extraction scoring). Write to `$RUN_DIR/efficiency-check33-<plugin>.md`. Return ONLY: `{"status":"done","file":"<path>","clusters":N,"findings":N,"severity":{"high":N,"medium":N,"low":N},"confidence":0.N}`

After all spawns complete: update `CHECK33_FILES` to point to the new files in `$RUN_DIR`.

**Health monitoring for scan spawns** (`_shared/agent-spawn-protocol.md`): the curator spawns run in the background. Issue them, end the turn, and resume on each completion notification — never a filler call, a "waiting" line, or a sleep. On each notification read that plugin's `$RUN_DIR/efficiency-check33-<plugin>.md`. Empty or missing → mark that plugin `timed_out`, surface with ⏱, continue with completed plugins' results.

## Step E2: Parse candidates

For each file in `CHECK33_FILES`, read and extract clusters. Default: `Verdict = HIGH` or `Verdict = MEDIUM` only. With `--eager`: also include `Verdict = LOW` clusters. Build candidate list: cluster ID, files affected, language, block purpose, occurrence count, verdict, recommended extraction target, differs-by param slots.

Also include **prose compression candidates** — inline blocks where `tokens(block) > tokens(prose equivalent)` or bin/ call-site descriptions where `tokens(description) >= tokens(prose equivalent)`. Tag as `Verdict = PROSE` in candidate list. Exempt: examples, templates, exact-syntax blocks.

If no qualifying clusters found: print `✓ No extraction candidates at current threshold.` End with `## Confidence` block and stop.

## Step E3: Present candidates and gate

Print candidate summary table:

```text
Bin/ extraction candidates:

| Cluster | Type  | Verdict | Blocks | Language | Purpose | Recommended target |
|---------|-------|---------|--------|----------|---------|--------------------|
| C1      | bin/  | HIGH    | 3      | bash     | resolves _shared/ path | bin/resolve_shared_path.py |
| C2      | prose | PROSE   | 1      | bash     | sets STALE var used only in prose conditions | replace with 2-row table |
```

**Before-numbers** — capture the pre-extraction measurement now, so the summary in Step E6 can quote a real delta rather than an estimate:

```bash
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/extract_code_blocks.py" "$_SCAN_DIR" --min-tokens 5 > "$RUN_DIR/blocks-before.jsonl"  # timeout: 30000
```

Then call `AskUserQuestion` — do NOT write options as plain text first. Map options directly into tool call arguments:

- question: "Extract candidates to bin/ scripts?"
- (a) label: `HIGH only` — description: extract only HIGH-verdict clusters
- (b) label: `HIGH + MEDIUM` — description: extract all HIGH and MEDIUM clusters
- (c) label: `HIGH + MEDIUM + LOW` — description: extract all clusters including LOW verdict; only shown when `--eager` active
- (d) label: `Prose only` — description: compress only PROSE-verdict candidates (replace blocks with prose/table/schema, delete script if bin/ call-site)
- (e) label: `Skip` — description: no extraction; review candidates manually

**If `$EAGER == false`**: omit option (c) — present only (a), (b), (d), (e).

## Step E4: Extract

> **Worktree isolation caveat** — `foundry:sw-engineer` runs with `isolation: worktree`. File writes inside agent (including `$RUN_DIR/extract-<cluster-id>.md` summary) land in agent's worktree under `.claude/worktrees/<id>/`, NOT main working tree. After agent returns JSON envelope, orchestrator must either (a) read summary from returned worktree path declared in agent's stdout, or (b) cherry-pick / merge worktree branch before reading `$RUN_DIR/extract-<cluster-id>.md` from main tree. Path resolution: use absolute main-tree path `$(git rev-parse --show-toplevel)/$RUN_DIR/extract-<cluster-id>.md` in prompt so agent has unambiguous target; agent's worktree shares same path layout, merging worktree branch back deposits summary at same path in main tree.

For each selected cluster, resolve `$_FS` path and spawn **foundry:sw-engineer** (one per cluster, all in parallel — issue all in a single response). Substitute absolute `$RUN_DIR` value into prompt before spawning (resolve via `$(git rev-parse --show-toplevel)/$RUN_DIR`):

> **Agent budget** — each spawn costs ~120,851 tok of fixed overhead (~73 tool-calls' worth) plus ~12.0 s/call, so work under ~73 calls is cheaper done inline: spawn nothing. Keep each agent near ~55 tool-calls; past ~60 they stall without returning an envelope, forcing reconstruction from disk. Every spawn prompt must require an envelope even on exhaustion — `partial: true` plus what was finished.

```text
Agent(subagent_type="foundry:sw-engineer", prompt="
_FS=$(python \"\${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/resolve_shared_path.py\" foundry skills/_shared 2>/dev/null || echo \"plugins/cc_foundry/skills/_shared\")
cat \"$_FS/bin-authoring-guide.md\"
Follow bin/ script conventions from the file loaded above.
Task: extract cluster <cluster-id> to bin/.
Cluster: purpose=<purpose>, language=<lang>, param slots=<differs-by values>.
Source files: <list of source .md files>.
**SURGICAL EDIT CONSTRAINT — mandatory**: modify ONLY the identified target block in each source file. Do NOT edit frontmatter, surrounding prose, other code blocks, check tables, or any content outside the target block. If you notice other issues in the file, record them in the summary — do not fix them.
Steps:
1. Create bin/<recommended-target> as a standalone Python executable following bin-authoring-guide.md: module docstring with Usage and Exit codes, argparse, type hints, __name__ guard. NEVER a .sh file — these plugins run on native Windows, where .sh does not execute (plugins/CLAUDE.md §Installability). Portability: pathlib, temp dir via os.environ.get(\"TMPDIR\") or tempfile.gettempdir(), session token via os.environ.get(\"CSID\") or os.environ.get(\"CLAUDE_CODE_SESSION_ID\") or \"shared\", never os.getppid(). CLI params: one named arg per param slot.
2. In each source .md file replace ONLY the target inline block with a one-line invocation:
   \`\`\`bash
   python \"\${CLAUDE_PLUGIN_ROOT:-plugins/<plugin>}/bin/<script>.py\" --param1 val1 ...  # timeout: <estimated_ms>
   \`\`\`
   Prefer this bare form — every plugin allow-lists Bash(python:*), so it never prompts. A capture form (VAR=$(python ...)) trips the \"Contains expansion\" gate and passes only via an exact-text entry in that plugin's blueprint-manifest.json; when one is unavoidable, name the call site in the summary so the manifest is regenerated in the same commit. Preserve surrounding sentinel reads and variable assignments consuming block output.
3. Diff gate — for each modified source file run: git diff HEAD -- <file> | grep "^[+-]" | grep -v "^[+-][+-][+-]"
   Count non-target changed lines. If any lines outside the target block changed: revert the file (git checkout HEAD -- <file>) and re-apply edit targeting only the block. Report diff line counts in summary.
4. Verify: grep source files to confirm old block body absent; confirm bin/ script exists; run all three gates and confirm each exits 0 — python plugins/cc_foundry/bin/check_orphaned_bin.py, python plugins/cc_foundry/bin/check_cli_flag_drift.py, and python plugins/cc_foundry/bin/check_fence_symmetry.py <changed .md files>.
5. Create test file: write `plugins/<plugin>/tests/test_<script-basename>.py` (or the matching `tests/` dir for the plugin) with at minimum pytest tests covering the public CLI entry point (use monkeypatch/capsys/tmp_path). Follow the test style in `tests/` alongside the bin/ script — check existing tests for fixture and import patterns. Non-empty file required; empty file fails Check R4.
Write extraction summary to $RUN_DIR/extract-<cluster-id>.md. Include: diff line counts per file, any reverts performed, incidental issues noticed but NOT fixed.
Return ONLY: {\"status\":\"done\",\"file\":\"$RUN_DIR/extract-<cluster-id>.md\",\"bin_script\":\"<path>\",\"source_files_updated\":N,\"test_file_created\":bool,\"confidence\":0.N}
")
```

**Health monitoring for extraction spawns** (`_shared/agent-spawn-protocol.md`): the sw-engineer spawns run in the background. Issue them, end the turn, and resume on each completion notification — never a filler call, a "waiting" line, or a sleep. On each notification read that cluster's `$RUN_DIR/extract-<cluster-id>.md`. Empty or missing → mark that cluster `timed_out`, surface with ⏱, continue with completed clusters.

## Step E5: Re-audit changed files

After all E4 agents complete, collect modified .md files from envelopes. Spawn **foundry:curator** per modified file (all parallel — issue all in a single response):

```text
Agent(subagent_type="foundry:curator", prompt="Re-audit <file> after bin/ extraction. Check: (1) no inline block body remains — only bin/ invocation one-liner; (2) timeout annotation present on invocation line; (3) variable assignments consuming block output still correct; (4) no orphaned variable references; (5) run git diff HEAD -- <file> and flag any changed lines outside the target block — these are unauthorized side-edits; surface as high finding if found. Write findings to $RUN_DIR/reaudit-<slug>.md. Return ONLY: {\"status\":\"done\",\"file\":\"$RUN_DIR/reaudit-<slug>.md\",\"issues\":N,\"side_edits_detected\":bool,\"confidence\":0.N}")
```

## Step E6: Measure, review, commit

**After-numbers** — re-run the same measurement and diff it against `blocks-before.jsonl` from Step E3, so the summary quotes a measured delta:

```bash
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/extract_code_blocks.py" "$_SCAN_DIR" --min-tokens 5 > "$RUN_DIR/blocks-after.jsonl"  # timeout: 30000
```

Block count stays roughly flat by design — an extraction replaces a block's body, it does not delete the block. Token estimate and the count of blocks over 25 lines are the numbers that move.

**Adversarial Convergence Loop** — before any commit, run the loop from `quality-gates.md` §Adversarial Convergence Loop over the combined diff: up to 3 `foundry:challenger` passes, findings weighted `security 20 · critical 10 · high 6 · medium 4 · low 2 · nit 1`, stop on `W_n == 0`, on a plateau (`0.5 < r_n < 1.0`), or immediately on `r_n ≥ 1.0`. Any open `security` or `critical` finding blocks the commit whatever the trend. On a stop with findings still open, report the score series and invoke `AskUserQuestion` instead of committing.

**Commit grouping** — two commits, never one. Extraction and policy are different kinds of change and reviewers read them differently:

1. The scripts, their tests, and the replaced call sites.
2. Any change to the extraction rules themselves — this mode file, the Check 33 gate spec, the severity table.

Each touched plugin bumps its own `plugin.json` `Y` in the commit that touches it, baseline read via `git show HEAD:<plugin-path>/.claude-plugin/plugin.json`. Sync each plugin's `README.md` in the same commit when a `bin/` table or a listed invocation changed.

Print:

```text
Extraction complete — <date>
  Extracted: N clusters → bin/ scripts
    <script-path>: <purpose> (<N> call sites updated)
  Source files updated: N
  Tokens: <before> → <after> (Δ −N, −N%)   blocks >25 lines: <before> → <after>
  Re-audit: clean / N issues (see $RUN_DIR/)
  Convergence: W_0 → W_1 → W_2 (<verdict>)
```

Remind: run `/foundry:setup` to propagate bin/ scripts to `~/.claude/` plugin cache. Then run `/audit --efficiency` to confirm `clusters == 0`.

End response with `## Confidence` block per CLAUDE.md output standards.
