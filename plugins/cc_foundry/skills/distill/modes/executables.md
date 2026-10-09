# Mode: Executables Extraction

<!-- file: executables.md — consumers: distill/SKILL.md -->

Triggered when first normalized argument token is `executables`. Reads latest `/audit --efficiency` Check 33 reports if present; runs bin/ extraction scan inline otherwise. Then gates, extracts with user confirmation, re-audits changed files.

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

**If `CHECK33_FILES` empty** (no prior `/audit --efficiency` run): print `[→ No efficiency report found — running bin/ extraction scan]`, execute scan inline:

Determine LOCAL_MODE-aware scan path, extract code blocks:

```bash
[ -d "plugins/" ] && _SCAN_DIR="plugins/" || _SCAN_DIR=".claude/"  # timeout: 3000
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/extract_code_blocks.py" "$_SCAN_DIR" --min-tokens 5 > "$RUN_DIR/blocks.jsonl"  # timeout: 30000
```

Spawn **foundry:curator** per plugin directory found under `$_SCAN_DIR` (one spawn per plugin, all parallel — issue all in a single response). Pass curator relevant slice of `$RUN_DIR/blocks.jsonl` (filter lines where `"file"` prefix matches plugin dir). Each spawn prompt:

> Use the attached block JSONL (pre-extracted via extract_code_blocks.py — each line is `{"file":..., "lang_marker":..., "line_start":..., "token_estimate":..., "content":...}`). Follow the full Check 33 Phase B2 protocol from `audit/modes/efficiency.md` exactly: assign block IDs, write purpose statements, build purpose clusters, compute syntactic similarity, produce Table 1 (purpose clusters) and Table 2 (extraction scoring). Write to `$RUN_DIR/efficiency-check33-<plugin>.md`. Return ONLY: `{"status":"done","file":"<path>","clusters":N,"findings":N,"severity":{"high":N,"medium":N,"low":N},"confidence":0.N}`

After all spawns complete: update `CHECK33_FILES` to point to new files in `$RUN_DIR`.

**Health monitoring for scan spawns** (`_shared/agent-spawn-protocol.md`): curator spawns run in background. Issue them together with `$RUN_DIR/agent-watch-scan.tsv` (one row per plugin: `<plugin>\t$RUN_DIR/efficiency-check33-<plugin>.md\t900`) in one response, end turn, resume on each completion notification and run `python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/agent_watch.py" --state-dir "$RUN_DIR"` once — never `ScheduleWakeup`, `ListAgents`, `Monitor`, a filler call, a "waiting" line, or a sleep. On each notification read that plugin's `$RUN_DIR/efficiency-check33-<plugin>.md`. Empty or missing: mark that plugin `timed_out`, surface with ⏱, continue with completed plugins' results.

## Step E2: Parse candidates

For each file in `CHECK33_FILES`, read, extract clusters. Default: `Verdict = HIGH` or `Verdict = MEDIUM` only. With `--eager`: also include `Verdict = LOW` clusters. Build candidate list: cluster ID, files affected, language, block purpose, occurrence count, verdict, recommended extraction target, differs-by param slots.

Also include **prose compression candidates** — inline blocks where `tokens(block) > tokens(prose equivalent)` or bin/ call-site descriptions where `tokens(description) >= tokens(prose equivalent)`. Tag as `Verdict = PROSE` in candidate list. Exempt: examples, templates, exact-syntax blocks.

No qualifying clusters found: print `✓ No extraction candidates at current threshold.` End with `## Confidence` block, stop.

## Step E3: Present candidates and gate

Candidate summary table — the `preview` of every option of the question below, not reply text (text written before a tool call can arrive as an empty progress update). Preview cap: ≤2000 chars and ≤12 lines per preview, every line counted (Claude Code withholds a longer preview and clips a taller one, no scroll) — over it, Write the full table to `$RUN_DIR/candidates.md` first, make every option's `preview` a compact summary ending `→ full table: $RUN_DIR/candidates.md`, and name that path in the question text:

```text
Bin/ extraction candidates:

| Cluster | Type  | Verdict | Blocks | Language | Purpose | Recommended target |
|---------|-------|---------|--------|----------|---------|--------------------|
| C1      | bin/  | HIGH    | 3      | bash     | resolves _shared/ path | bin/resolve_shared_path.py |
| C2      | prose | PROSE   | 1      | bash     | sets STALE var used only in prose conditions | replace with 2-row table |
```

**Before-numbers** — capture pre-extraction measurement now, so Step E6 summary quotes a real delta rather than an estimate:

```bash
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/extract_code_blocks.py" "$_SCAN_DIR" --min-tokens 5 > "$RUN_DIR/blocks-before.jsonl"  # timeout: 30000
```

Then call `AskUserQuestion`, the candidate table as every option's `preview` — do NOT write options as plain text first. Map options directly into tool call arguments:

- question: "Extract candidates to bin/ scripts?"
- (a) label: `HIGH only` — description: extract only HIGH-verdict clusters
- (b) label: `HIGH + MEDIUM` — description: extract all HIGH and MEDIUM clusters
- (c) label: `HIGH + MEDIUM + LOW` — description: extract all clusters including LOW verdict; only shown when `--eager` active
- (d) label: `Prose only` — description: compress only PROSE-verdict candidates (replace blocks with prose/table/schema, delete script if bin/ call-site)
- (e) label: `Skip` — description: no extraction; review candidates manually

**If `$EAGER == false`**: omit option (c) — present only (a), (b), (d), (e).

## Step E4: Extract

> **Explicit worktree isolation** — each E4 spawn passes `isolation="worktree"` itself (`agents/sw-engineer.md` §Worktree isolation): clusters run in parallel, two may share one source `.md`, and the diff gate below reverts with `git checkout HEAD -- <file>` — in one shared tree that revert wipes a sibling cluster's edit or the user's uncommitted work. An isolated agent cannot write into the main tree, so its summary rides in the envelope, never a file; its edits stay in its worktree until §Transplant below.

For each selected cluster, resolve `$_FS` path, spawn **foundry:sw-engineer** (one per cluster, all parallel — issue all in a single response). Substitute `<BASE_SHA>` = main-tree `git rev-parse HEAD` into every prompt before spawning; HEAD stays fixed until E6 commits, so §Transplant reuses the same value:

> **Agent budget** — each spawn costs ~120,851 tok of fixed overhead (~73 tool-calls' worth) plus ~12.0 s/call, so work under ~73 calls is cheaper done inline: spawn nothing — work-displacement only; an isolation-motivated spawn (adversarial reviewer, distinct specialist role, model tier, worktree) runs regardless of size. Keep each agent near ~55 tool-calls; past ~60 they stall without returning an envelope, forcing reconstruction from disk. Every spawn prompt must require an envelope even on exhaustion — `partial: true` plus what was finished.

```text
Agent(subagent_type="foundry:sw-engineer", isolation="worktree", name="extract-<cluster-id>", description="<cluster purpose, 3-5 words>", prompt="Extract cluster <cluster-id> to bin/ — <purpose>.
_FS=$(python \"\${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/resolve_shared_path.py\" foundry skills/_shared 2>/dev/null || echo \"plugins/cc_foundry/skills/_shared\")
cat \"$_FS/bin-authoring-guide.md\"
Follow bin/ script conventions from the file loaded above.
Cluster: purpose=<purpose>, language=<lang>, param slots=<differs-by values>.
Source files: <list of source .md files>.
BASE — step 0, before any edit: git status --porcelain must print nothing; then git branch --show-current, then git checkout -B <that branch> <BASE_SHA> (no branch printed → git checkout --detach <BASE_SHA>), then git merge-base --is-ancestor <BASE_SHA> HEAD. Any step fails → change nothing more, return status blocked with reason \"base mismatch\".
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
Put the extraction summary in the envelope summary field, never in a file — your worktree drops untracked and ignored files when removed, and main-tree paths are refused. Include: diff line counts per file, any reverts performed, incidental issues noticed but NOT fixed.
Return ONLY: {\"status\":\"done\",\"changed\":[\"<every path you created or modified>\"],\"bin_script\":\"<path>\",\"source_files_updated\":N,\"test_file_created\":bool,\"summary\":\"<at most 5 lines>\",\"confidence\":0.N}
")
```

**Health monitoring for extraction spawns** (`_shared/agent-spawn-protocol.md`): sw-engineer spawns run in background. Issue them together with `$RUN_DIR/agent-watch-extract.tsv` (one row per cluster: `<cluster-id>\t-\t900`, envelope-only) in one response, end turn, resume on each completion notification and run `python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/agent_watch.py" --state-dir "$RUN_DIR"` once — never `ScheduleWakeup`, `ListAgents`, `Monitor`, a filler call, a "waiting" line, or a sleep. On each notification Write that cluster's envelope `summary` to `$RUN_DIR/extract-<cluster-id>.md` (orchestrator's own tree). No envelope: mark that cluster `timed_out`, surface with ⏱, continue with completed clusters.

**Transplant — after the last E4 envelope, before E5; one cluster at a time, selection order.** Per `done` cluster, take `<wt>` and `<branch>` from its agent result:

1. `git -C <wt> rev-parse HEAD` must print `<BASE_SHA>`; otherwise stale base → keep `<wt>`, surface `⚠ <cluster-id> stale base — not transplanted`, next cluster.
2. `git -C <wt> add -- <changed paths from envelope>`, then `git -C <wt> diff --cached --binary <BASE_SHA> > "$RUN_DIR/extract-<cluster-id>.patch"`.
3. `git apply "$RUN_DIR/extract-<cluster-id>.patch"` in the main tree. Fails (sibling cluster or uncommitted edit touched the same lines) → change nothing more, keep `<wt>`, surface `⚠ <cluster-id> not transplanted — merge by hand from <wt>`.
4. Applied → assert `<wt>` lies under `<main-tree root>/.claude/worktrees/`, then `git worktree remove --force <wt>` and `git branch -D <branch>`. Assertion or removal fails → change nothing, list `<wt>` as leftover (`git worktree remove`, then `git worktree prune`). Never remove a path outside `.claude/worktrees/`.

E5 then sees every transplanted change in the main tree; an untransplanted cluster's files are absent from the E5 list.

## Step E5: Re-audit changed files

After all E4 agents complete, collect modified .md files from envelopes. Spawn **foundry:curator** per modified file (all parallel — issue all in one response):

```text
Agent(subagent_type="foundry:curator", prompt="Re-audit <file> after bin/ extraction. Check: (1) no inline block body remains — only bin/ invocation one-liner; (2) timeout annotation present on invocation line; (3) variable assignments consuming block output still correct; (4) no orphaned variable references; (5) run git diff HEAD -- <file> and flag any changed lines outside the target block — these are unauthorized side-edits; surface as high finding if found. Write findings to $RUN_DIR/reaudit-<slug>.md. Return ONLY: {\"status\":\"done\",\"file\":\"$RUN_DIR/reaudit-<slug>.md\",\"issues\":N,\"side_edits_detected\":bool,\"confidence\":0.N}")
```

## Step E6: Measure, review, commit

**After-numbers** — re-run same measurement, diff against `blocks-before.jsonl` from Step E3, so summary quotes a measured delta:

```bash
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/extract_code_blocks.py" "$_SCAN_DIR" --min-tokens 5 > "$RUN_DIR/blocks-after.jsonl"  # timeout: 30000
```

Block count stays roughly flat by design — an extraction replaces a block's body, doesn't delete the block. Token estimate and count of blocks over 25 lines are the numbers that move.

**Adversarial Convergence Loop** — before any commit, read and follow `quality-gates.md` §Adversarial Convergence Loop over the combined diff: an independent `foundry:challenger` baseline `W_0`, then further passes only while the unchanged-scope nonnegative integer total score strictly decreases, findings weighted `security 20 · critical 10 · high 6 · medium 4 · low 2 · nit 1`. Resolve feasible authorized findings and investigate the shared root cause of repeated signatures; a structural flag alone does not stop remediation. Any decrease (`0 < r_n < 1`) converges; equal scores plateau, and increases do not converge. Stop on an independently reviewed current `W_n == 0`, plateau/non-convergence with open findings, a stricter caller-budget stop, or missing independence/authorization. Any open `security` or `critical` finding blocks completion and commit; unresolved `high` requires escalation. On a stop with findings open, report completed scores, per-tier residue, evidence, owner and next action; invoke `AskUserQuestion` only for the concrete missing decision or authorization. A clean loop does not authorize a commit.

**Commit grouping** — two commits, never one. Extraction and policy are different kinds of change; reviewers read them differently:

1. The scripts, their tests, and the replaced call sites.
2. Any change to the extraction rules themselves — this mode file, Check 33 gate spec, severity table.

Each touched plugin bumps its own `plugin.json` `Y` in the commit that touches it, baseline read via `git show HEAD:<plugin-path>/.claude-plugin/plugin.json`. Sync each plugin's `README.md` in same commit when a `bin/` table or listed invocation changed.

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

Remind: run `/foundry:setup` to propagate bin/ scripts to `~/.claude/` plugin cache, then `/audit --efficiency` to confirm `clusters == 0`.

End response with `## Confidence` block per CLAUDE.md output standards.
