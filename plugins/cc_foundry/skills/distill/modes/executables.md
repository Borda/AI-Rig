# Mode: Executables Extraction

<!-- file: executables.md — consumers: distill/SKILL.md -->

Triggered when first normalized argument token is `executables`. Reads latest `/foundry:audit --efficiency` Check 33 reports if present; runs bin/ extraction scan inline otherwise. Then gates, extracts with user confirmation, re-audits changed files.

## Step E1: Locate or run scan

Substitute `<EXEC_PATH>` with the path after `executables` in `ARGUMENTS_FINAL` (SKILL.md flag-parse stdout), or with nothing for auto-detect — this file is loader-rendered, so `$ARGUMENTS` is not set in its blocks.

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name foundry) || exit $?; export PLUGIN_ROOT
EXEC_ARGS='<EXEC_PATH>'
case "$EXEC_ARGS" in *'<'*'>'*) echo "! BLOCKED — <EXEC_PATH> not substituted (empty = auto-detect)"; exit 1;; esac
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
RUN_DIR=$(python "$PLUGIN_ROOT/bin/make_run_dir.py" .reports/distill 2>/dev/null || echo ".reports/distill/$(date -u +%Y-%m-%dT%H-%M-%SZ)")  # timeout: 5000
mkdir -p "$RUN_DIR"  # timeout: 5000
[ -d "plugins/" ] && _SCAN_DIR="plugins/" || _SCAN_DIR=".claude/"
echo "$RUN_DIR" > "${TMPDIR:-/tmp}/distill-exec-run-dir-${CSID}"; echo "$_SCAN_DIR" > "${TMPDIR:-/tmp}/distill-exec-scan-dir-${CSID}"
echo "RUN_DIR=$RUN_DIR SCAN_DIR=$_SCAN_DIR"
if [ -n "$EXEC_ARGS" ]; then
  # newline-separated scalar, never mapfile — absent in zsh, would leave the list always empty
  if [ -d "$EXEC_ARGS" ]; then
    CHECK33_FILES=$(find "$EXEC_ARGS" -maxdepth 2 -type f -name 'efficiency-check33-*.md' 2>/dev/null | sort)
  elif [ -f "$EXEC_ARGS" ]; then
    CHECK33_FILES="$EXEC_ARGS"
  else
    printf "! MISSING — path not found: %s\n" "$EXEC_ARGS"
    exit 1
  fi
else
  LATEST_RUN=$(find .reports/audit -maxdepth 1 -type d -name "20*" 2>/dev/null \
    | sort -r \
    | while IFS= read -r d; do
        find "$d" -maxdepth 2 -type f -name 'efficiency-check33-*.md' 2>/dev/null | head -1 | grep -q . && echo "$d" && break
      done)  # timeout: 5000
  [ -n "$LATEST_RUN" ] && CHECK33_FILES=$(find "$LATEST_RUN" -maxdepth 2 -type f -name 'efficiency-check33-*.md' 2>/dev/null | sort)
fi
echo "Check 33 files (from ${LATEST_RUN:-$EXEC_ARGS}), one per line — none listed = empty:"
printf '%s\n' "$CHECK33_FILES"
```

**If `CHECK33_FILES` non-empty**: proceed to Step E2 using those files.

**If `CHECK33_FILES` empty** (no prior `/audit --efficiency` run): print `[→ No efficiency report found — running bin/ extraction scan]`, execute scan inline:

Reload E1 run dir + scan path (bash state does not persist across calls), extract code blocks:

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name foundry) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r RUN_DIR < "${TMPDIR:-/tmp}/distill-exec-run-dir-${CSID}" 2>/dev/null || RUN_DIR=""
IFS= read -r _SCAN_DIR < "${TMPDIR:-/tmp}/distill-exec-scan-dir-${CSID}" 2>/dev/null || _SCAN_DIR=""
[ -n "$RUN_DIR" ] && [ -n "$_SCAN_DIR" ] || { echo "! MISSING — E1 run-dir/scan-dir sentinel; re-run Step E1"; exit 1; }
python "$PLUGIN_ROOT/bin/extract_code_blocks.py" "$_SCAN_DIR" --min-tokens 5 > "$RUN_DIR/blocks.jsonl"  # timeout: 30000
```

Use Glob to enumerate plugin directories when `$_SCAN_DIR` is `plugins/`; `.claude/` is one scan unit named `project-config`, not a container of plugins. Read `$RUN_DIR/blocks.jsonl` and Write one `$RUN_DIR/blocks-<plugin>.jsonl` per scan unit, copying only complete JSONL rows whose `file` path is inside that unit's directory (match path components, not a partial directory-name prefix). An empty slice is valid and still receives a scan report. Spawn **foundry:curator** per scan unit (one spawn per unit, all parallel — issue all in a single response). Substitute `$RUN_DIR`, `<plugin>` and the resolved foundry plugin root in every prompt with their actual paths before spawning; spawned agents receive text, never shell state or attached data. Each spawn prompt:

> Read `$RUN_DIR/blocks-<plugin>.jsonl` (pre-extracted via extract_code_blocks.py — each line is `{"file":..., "lang_marker":..., "line_start":..., "token_estimate":..., "content":...}`). Run `python "<foundry-plugin-root>/bin/load_mode.py" audit modes efficiency.md --fallback-source` (`# timeout: 5000`) and follow only its full Check 33 Phase B2 protocol exactly: assign block IDs, write purpose statements, build purpose clusters, compute syntactic similarity, produce Table 1 (purpose clusters) and Table 2 (extraction scoring). An empty slice produces a no-candidates report. Write to `$RUN_DIR/efficiency-check33-<plugin>.md`. Return ONLY: `{"status":"done","file":"<path>","clusters":N,"findings":N,"severity":{"high":N,"medium":N,"low":N},"confidence":0.N}`

After all spawns complete: update `CHECK33_FILES` to point to new files in `$RUN_DIR`.

**Health monitoring for scan spawns** (`_shared/agent-spawn-protocol.md`): curator spawns run in background. Issue them together with `$RUN_DIR/agent-watch-scan.tsv` (one row per scan unit: `<plugin>\t$RUN_DIR/efficiency-check33-<plugin>.md\t900`) in one response, end turn, resume on each completion notification and run `PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name foundry) || exit $?; export PLUGIN_ROOT; export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"; IFS= read -r RUN_DIR < "${TMPDIR:-/tmp}/distill-exec-run-dir-${CSID}" 2>/dev/null || RUN_DIR=""; [ -n "$RUN_DIR" ] && python "$PLUGIN_ROOT/bin/agent_watch.py" --state-dir "$RUN_DIR"` once — never `ScheduleWakeup`, `ListAgents`, `Monitor`, a filler call, a "waiting" line, or a sleep. Missing sentinel → stop with `! BLOCKED — re-run E1`. On each notification read that plugin's `$RUN_DIR/efficiency-check33-<plugin>.md`. Empty or missing: mark that plugin `timed_out`, surface with ⏱, continue with completed plugins' results; if no scans completed, report the scan incomplete and stop, never print `No extraction candidates`.

## Step E2: Parse candidates

For each file in `CHECK33_FILES`, read, extract clusters. Default: `Verdict = HIGH` or `Verdict = MEDIUM` only. With `--eager`: also include `Verdict = LOW` clusters. Build candidate list: cluster ID, files affected, language, block purpose, occurrence count, verdict, recommended extraction target, differs-by param slots.

Also include **prose compression candidates** — inline blocks where `tokens(block) > tokens(prose equivalent)` or bin/ call-site descriptions where `tokens(description) >= tokens(prose equivalent)`. Tag as `Verdict = PROSE` in candidate list. Exempt: examples, templates, exact-syntax blocks.

No qualifying clusters found: print `✓ No extraction candidates at current threshold.` End with `## Confidence` block, stop.

## Step E3: Present candidates and gate

Build the candidate summary table below and Write it to `$RUN_DIR/candidates.md` first. After capturing the before-numbers, it is printed once, rendered as normal Markdown, as the last reply text before the question call. The question text carries the extraction headline, counts by verdict and `→ $RUN_DIR/candidates.md`. Keep the question text to at most 6 plain lines (target 600 characters, hard cap 1,500), with no table, no pipe layout and no code fence. Options carry labels and one-line action descriptions, no `preview`; place the recommended option second without changing labels or answer mapping.

```text
Bin/ extraction candidates:

| Cluster | Type  | Verdict | Blocks | Language | Purpose | Recommended target |
|---------|-------|---------|--------|----------|---------|--------------------|
| C1      | bin/  | HIGH    | 3      | bash     | resolves _shared/ path | bin/resolve_shared_path.py |
| C2      | prose | PROSE   | 1      | bash     | sets STALE var used only in prose conditions | replace with 2-row table |
```

**Before-numbers** — capture pre-extraction measurement now, so Step E6 summary quotes a real delta rather than an estimate:

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name foundry) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r RUN_DIR < "${TMPDIR:-/tmp}/distill-exec-run-dir-${CSID}" 2>/dev/null || RUN_DIR=""
IFS= read -r _SCAN_DIR < "${TMPDIR:-/tmp}/distill-exec-scan-dir-${CSID}" 2>/dev/null || _SCAN_DIR=""
[ -n "$RUN_DIR" ] && [ -n "$_SCAN_DIR" ] || { echo "! MISSING — E1 run-dir/scan-dir sentinel; re-run Step E1"; exit 1; }
python "$PLUGIN_ROOT/bin/extract_code_blocks.py" "$_SCAN_DIR" --min-tokens 5 > "$RUN_DIR/blocks-before.jsonl"  # timeout: 30000
```

Then call `AskUserQuestion`, with the plain candidate summary in the question text and no `preview` — do NOT write options as plain text first. Map options directly into tool call arguments:

- question: "Extract candidates to bin/ scripts?"
- (a) label: `HIGH only` — description: extract only HIGH-verdict clusters
- (b) label: `HIGH + MEDIUM` — description: extract all HIGH and MEDIUM clusters
- (c) label: `HIGH + MEDIUM + LOW` — description: extract all clusters including LOW verdict; only shown when `--eager` active
- (d) label: `Prose only` — description: compress only PROSE-verdict candidates (replace blocks with prose/table/schema, delete script if bin/ call-site)
- (e) label: `Skip` — description: no extraction; review candidates manually

**If `$EAGER == false`**: omit option (c) — present only (a), (b), (d), (e).

## Step E4: Extract

**`Prose only` branch** — select only PROSE candidates and have the parent or **foundry:curator** compress their exact approved blocks; do not spawn extraction engineers. Before editing, Read each target, record its approved block boundaries and snapshot its full bytes with `cp -p -- "<target>" "<RUN_DIR>/prose-pre/<target-id>"` after creating the snapshot directory; substitute actual paths before each command. A failed snapshot stops that target's writes. Replace only the selected block with equivalent prose/table/schema, preserving modal strength, literals, exceptions and step order; apply the instruction compression gate for instruction files. Run `diff -u "<RUN_DIR>/prose-pre/<target-id>" "<target>"` after editing, show the diff, and reject changes outside the approved block or any semantic loss. For a bin/ call-site candidate, first replace every approved consumer, verify no remaining script references across the project, and request confirmation for the exact script path before deleting it; remaining consumers or denied deletion keep the script and are reported. Record every changed/deleted path and the snapshot comparison for E5, skip the engineer spawns and Transplant below, then continue to E5 and E6. `Skip` exits without changes. Other selections use only bin/ clusters at the selected thresholds.

> **Explicit worktree isolation** — each E4 spawn passes `isolation="worktree"` itself (`agents/sw-engineer.md` §Worktree isolation): clusters run in parallel, two may share one source `.md`, and the diff gate below reverts with `git checkout HEAD -- <file>` — in one shared tree that revert wipes a sibling cluster's edit or the user's uncommitted work. An isolated agent cannot write into the main tree, so its summary rides in the envelope, never a file; its edits stay in its worktree until §Transplant below.

For each selected cluster, resolve `$_FS` path, spawn **foundry:sw-engineer** (one per cluster, all parallel — issue all in a single response). Substitute `<BASE_SHA>` = main-tree `git rev-parse HEAD` into every prompt before spawning; HEAD stays fixed until E6 commits, so §Transplant reuses the same value:

> **Agent budget** — each spawn costs ~120,851 tok of fixed overhead (~73 tool-calls' worth) plus ~12.0 s/call, so work under ~73 calls is cheaper done inline: spawn nothing — work-displacement only; an isolation-motivated spawn (adversarial reviewer, distinct specialist role, model tier, worktree) runs regardless of size. Keep each agent near ~55 tool-calls; past ~60 they stall without returning an envelope, forcing reconstruction from disk. Every spawn prompt must require an envelope even on exhaustion — `partial: true` plus what was finished.

```text
Agent(subagent_type="foundry:sw-engineer", isolation="worktree", name="extract-<cluster-id>", description="<cluster purpose, 3-5 words>", prompt="Extract cluster <cluster-id> to bin/ — <purpose>.
PLUGIN_ROOT=$(python '${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py' --plugin-name foundry) || exit $?; export PLUGIN_ROOT
python \"$PLUGIN_ROOT/bin/load_shared_doc.py\" foundry skills/_shared bin-authoring-guide.md
Follow bin/ script conventions from the file loaded above.
Cluster: purpose=<purpose>, language=<lang>, param slots=<differs-by values>.
Source files: <list of source .md files>.
BASE — step 0, before any edit: git status --porcelain must print nothing; then git branch --show-current, then git checkout -B <that branch> <BASE_SHA> (no branch printed → git checkout --detach <BASE_SHA>), then git merge-base --is-ancestor <BASE_SHA> HEAD. Any step fails → change nothing more, return status blocked with reason \"base mismatch\".
Do not commit, stage, merge, rebase, or move HEAD after the BASE step. Leave every created or modified file unstaged; the parent transplants the work and owns any later commit. Before returning, verify git rev-parse HEAD still equals <BASE_SHA>; mismatch → return blocked and preserve the worktree.
**SURGICAL EDIT CONSTRAINT — mandatory**: modify ONLY the identified target block in each source file. Do NOT edit frontmatter, surrounding prose, other code blocks, check tables, or any content outside the target block. If you notice other issues in the file, record them in the summary — do not fix them.
Steps:
1. Create bin/<recommended-target> as a standalone Python executable following bin-authoring-guide.md: module docstring with Usage and Exit codes, argparse, type hints, __name__ guard. NEVER a .sh file — these plugins run on native Windows, where .sh does not execute (plugins/CLAUDE.md §Installability). Portability: pathlib, temp dir via os.environ.get(\"TMPDIR\") or (tempfile.gettempdir() if os.name == \"nt\" else \"/tmp\") to match the shell ${TMPDIR:-/tmp}, session token via os.environ.get(\"CSID\") or os.environ.get(\"CLAUDE_CODE_SESSION_ID\") or \"shared\", never os.getppid(). CLI params: one named arg per param slot.
2. In each source .md file replace ONLY the target inline block with the consuming plugin's guarded resolver prologue followed by its helper invocation. Write the plain bootstrap token in that source prologue, not the absolute foundry path rendered in this prompt; use the target plugin's manifest name for --plugin-name:
   \`\`\`bash
   python \"$PLUGIN_ROOT/bin/<script>.py\" --param1 val1 ...  # timeout: <estimated_ms>
   \`\`\`
   Prefer the bare helper invocation — every plugin allow-lists Bash(python:*); the guarded resolver capture still requires exact manifest coverage. A capture form (VAR=$(python ...)) trips the \"Contains expansion\" gate and passes only via an exact-text entry in that plugin's blueprint-manifest.json; when one is unavoidable, name the call site in the summary so the manifest is regenerated in the same commit. Preserve surrounding sentinel reads and variable assignments consuming block output.
3. Diff gate — for each modified source file run: git diff HEAD -- <file> | grep "^[+-]" | grep -v "^[+-][+-][+-]"
   Count non-target changed lines. If any lines outside the target block changed: revert the file (git checkout HEAD -- <file>) and re-apply edit targeting only the block. Report diff line counts in summary.
4. Verify: grep source files to confirm old block body absent; confirm bin/ script exists; start each gate block with the foundry guarded resolver prologue above, then run all three gates and confirm each exits 0 — python \"$PLUGIN_ROOT/bin/check_orphaned_bin.py\", python \"$PLUGIN_ROOT/bin/check_cli_flag_drift.py\", and python \"$PLUGIN_ROOT/bin/check_fence_symmetry.py\" <changed .md files>.
5. Create test file: write `plugins/<plugin>/tests/test_<script-basename>.py` (or the matching `tests/` dir for the plugin) with at minimum pytest tests covering the public CLI entry point (use monkeypatch/capsys/tmp_path). Follow the test style in `tests/` alongside the bin/ script — check existing tests for fixture and import patterns. Non-empty file required; empty file fails Check R4.
Put the extraction summary in the envelope summary field, never in a file — your worktree drops untracked and ignored files when removed, and main-tree paths are refused. Include: diff line counts per file, any reverts performed, incidental issues noticed but NOT fixed.
Return ONLY: {\"status\":\"done\",\"changed\":[\"<every path you created or modified>\"],\"bin_script\":\"<path>\",\"source_files_updated\":N,\"test_file_created\":bool,\"summary\":\"<at most 5 lines>\",\"confidence\":0.N}
")
```

**Health monitoring for extraction spawns** (`_shared/agent-spawn-protocol.md`): sw-engineer spawns run in background. Issue them together with `$RUN_DIR/agent-watch-extract.tsv` (one row per cluster: `<cluster-id>\t-\t900`, envelope-only) in one response, end turn, resume on each completion notification and run `PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name foundry) || exit $?; export PLUGIN_ROOT; export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"; IFS= read -r RUN_DIR < "${TMPDIR:-/tmp}/distill-exec-run-dir-${CSID}" 2>/dev/null || RUN_DIR=""; [ -n "$RUN_DIR" ] && python "$PLUGIN_ROOT/bin/agent_watch.py" --state-dir "$RUN_DIR"` once — never `ScheduleWakeup`, `ListAgents`, `Monitor`, a filler call, a "waiting" line, or a sleep. Missing sentinel → stop with `! BLOCKED — re-run E1`. On each notification Write that cluster's envelope `summary` to `$RUN_DIR/extract-<cluster-id>.md` (orchestrator's own tree). No envelope: mark that cluster `timed_out`, surface with ⏱, continue with completed clusters.

**Transplant — after the last E4 envelope, before E5; one cluster at a time, selection order.** Per `done` cluster, take `<wt>` and `<branch>` from its agent result:

1. `git -C <wt> rev-parse HEAD` must print `<BASE_SHA>`; otherwise stale base → keep `<wt>`, surface `⚠ <cluster-id> stale base — not transplanted`, next cluster.
2. `git -C <wt> add -- <changed paths from envelope>`, then `git -C <wt> diff --cached --binary <BASE_SHA> > "$RUN_DIR/extract-<cluster-id>.patch"`.
3. `git apply "$RUN_DIR/extract-<cluster-id>.patch"` in the main tree. Fails (sibling cluster or uncommitted edit touched the same lines) → change nothing more, keep `<wt>`, surface `⚠ <cluster-id> not transplanted — merge by hand from <wt>`.
4. Applied → assert `<wt>` lies under `<main-tree root>/.claude/worktrees/`, then `git worktree remove --force <wt>` and `git branch -D <branch>`. Assertion or removal fails → change nothing, list `<wt>` as leftover (`git worktree remove`, then `git worktree prune`). Never remove a path outside `.claude/worktrees/`.

E5 then sees every transplanted change in the main tree; an untransplanted cluster's files are absent from the E5 list.

## Step E5: Re-audit changed files

After all E4 agents complete, collect modified .md files from envelopes. Spawn **foundry:curator** per modified file (all parallel — issue all in one response):

For the Prose only branch, use the parent's changed-file list and pre-edit snapshots instead of envelopes; check semantic equivalence and changes confined to the approved block, allow removal of the code fence and timeout when prose replaces execution, and verify any deleted script has no remaining consumers. For bin/ extraction, use the checks in the prompt below. Substitute `$RUN_DIR` in every prompt with its computed path before spawning, including the returned `file` field.

```text
Agent(subagent_type="foundry:curator", name="reaudit-<slug>", description="<slug> extraction re-audit", prompt="Re-audit <file> after bin/ extraction. Check: (1) no inline block body remains — only bin/ invocation one-liner; (2) timeout annotation present on invocation line; (3) variable assignments consuming block output still correct; (4) no orphaned variable references; (5) run git diff HEAD -- <file> and flag any changed lines outside the target block — these are unauthorized side-edits; surface as high finding if found. Write findings to $RUN_DIR/reaudit-<slug>.md. Return ONLY: {\"status\":\"done\",\"file\":\"$RUN_DIR/reaudit-<slug>.md\",\"issues\":N,\"side_edits_detected\":bool,\"confidence\":0.N}")
```

**Health monitoring for re-audit spawns** (`_shared/agent-spawn-protocol.md`): issue the curator spawns together with `$RUN_DIR/agent-watch-reaudit.tsv` (one row per file: `<slug>\t$RUN_DIR/reaudit-<slug>.md\t900`) in one response, end turn, resume on each completion notification and run `PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name foundry) || exit $?; export PLUGIN_ROOT; export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"; IFS= read -r RUN_DIR < "${TMPDIR:-/tmp}/distill-exec-run-dir-${CSID}" 2>/dev/null || RUN_DIR=""; [ -n "$RUN_DIR" ] && python "$PLUGIN_ROOT/bin/agent_watch.py" --state-dir "$RUN_DIR"` once — never `ScheduleWakeup`, `ListAgents`, `Monitor`, a filler call, a "waiting" line, or a sleep. Missing sentinel → stop with `! BLOCKED — re-run E1`. Empty or missing `reaudit-<slug>.md`: mark that file `timed_out`, surface with ⏱ in the E6 summary as not re-audited.

## Step E6: Measure, review, commit

**After-numbers** — re-run same measurement, diff against `blocks-before.jsonl` from Step E3, so summary quotes a measured delta:

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name foundry) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r RUN_DIR < "${TMPDIR:-/tmp}/distill-exec-run-dir-${CSID}" 2>/dev/null || RUN_DIR=""
IFS= read -r _SCAN_DIR < "${TMPDIR:-/tmp}/distill-exec-scan-dir-${CSID}" 2>/dev/null || _SCAN_DIR=""
[ -n "$RUN_DIR" ] && [ -n "$_SCAN_DIR" ] || { echo "! MISSING — E1 run-dir/scan-dir sentinel; re-run Step E1"; exit 1; }
python "$PLUGIN_ROOT/bin/extract_code_blocks.py" "$_SCAN_DIR" --min-tokens 5 > "$RUN_DIR/blocks-after.jsonl"  # timeout: 30000
```

For bin/ extraction, block count stays roughly flat by design — an extraction replaces a block's body, doesn't delete the block. Token estimate and count of blocks over 25 lines are the numbers that move. Prose compression may remove blocks entirely; report its measured block-count delta separately and count retained scripts when deletion was not approved.

**Adversarial Convergence Loop** — before any commit, read and follow `quality-gates.md` §Adversarial Convergence Loop over the combined diff: an independent `foundry:challenger` baseline `W_0`, then further passes only while the unchanged-scope nonnegative integer total score strictly decreases, findings weighted `security 20 · critical 10 · high 6 · medium 4 · low 2 · nit 1`. Resolve feasible authorized findings and investigate the shared root cause of repeated signatures; a structural flag alone does not stop remediation. Any decrease (`0 < r_n < 1`) converges; equal scores plateau, and increases do not converge. Stop on an independently reviewed current `W_n == 0`, plateau/non-convergence with open findings, a stricter caller-budget stop, or missing independence/authorization. Any open `security` or `critical` finding blocks completion and commit; unresolved `high` requires escalation. On a stop with findings open, report completed scores, per-tier residue, evidence, owner and next action; invoke `AskUserQuestion` only for the concrete missing decision or authorization. A clean loop does not authorize a commit.

**Commit grouping** — two commits, never one. Extraction and policy are different kinds of change; reviewers read them differently:

1. The scripts, their tests, and the replaced call sites.
2. Any change to the extraction rules themselves — this mode file, Check 33 gate spec, severity table.

Only for a target with a verified `.claude-plugin/plugin.json` layout: each touched plugin bumps its own `plugin.json` `Y` in the commit that touches it, baseline read via `git show HEAD:<plugin-path>/.claude-plugin/plugin.json`. For another plugin layout, read and follow that target's own version/release instructions; never assume a manifest path or version field. Project-local `.claude/` extraction has no plugin version bump. Sync each plugin's `README.md` in same commit when a `bin/` table or listed invocation changed.

Print:

```text
Extraction complete — <date>
  Extracted: N clusters → bin/ scripts
    <script-path>: <purpose> (<N> call sites updated)
  Compressed: N PROSE candidates; scripts deleted: N / retained: N
  Source files updated: N
  Tokens: <before> → <after> (Δ −N, −N%)   blocks >25 lines: <before> → <after>
  Re-audit: clean / N issues (see $RUN_DIR/)
  Convergence: W_0 → W_1 → W_2 (<verdict>)
```

Remind: plugin `bin/` scripts reach the `~/.claude/` plugin cache only via plugin update/reinstall (`/foundry:setup` does not copy them); then `/audit --efficiency` to confirm `extract_count == 0`.

End response with `## Confidence` block per CLAUDE.md output standards.
