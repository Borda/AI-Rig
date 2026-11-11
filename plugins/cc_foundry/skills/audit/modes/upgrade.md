# Upgrade Mode — foundry:audit

Triggered by `/audit --upgrade`. Read+executed by `/audit` when `--upgrade` flag present.

## Mode: upgrade

**Trigger**: `/audit --upgrade`

**Purpose**: apply documented Claude Code improvements that passed genuine-value filter. Config changes applied + correctness-checked immediately. Capability changes A/B tested via mini calibrate pipeline against source files (`--local` only) — accepted only if neither delta is negative and at least one clears the noise floor (Phase 4 Step d).

**Task tracking**: TaskCreate "Fetch upgrade proposals", "Apply config proposals", "A/B test capability proposals". Mark in_progress/completed throughout.

### Phase 1: Gate check

Verify no unresolved blocking findings before applying anything.

Use the most recent completed same-project audit's `summary.jsonl` and re-audit/fix results as the gate source; record its exact run path. It is fresh only within 4 hours and when none of its audited sources changed after that verification. Missing, incomplete or stale evidence → run a fresh scoped `/audit` before applying proposals. Any unresolved security/critical/high issues: stop, print "⚠ Resolve blocking findings first (run `/audit` and pick fix level from gate), then re-run `/audit --upgrade`." Never treat historical ledger rows already verified resolved as unresolved findings.

Select the latest completed audit in the current project's report root with this block. Its printed path is a candidate, not gate approval: verify the audited-source inventory and freshness, including zero-finding sources, and current unresolved findings against its report and fix evidence. Missing inventory/provenance → run a fresh scoped audit and retain its inventory and verification evidence; never infer unchanged sources from an empty ledger. Upgrade completion markers are not audit evidence.

```bash
python - <<'PY'
import json
import time
from pathlib import Path

root = (Path.cwd() / ".reports/audit").resolve()
candidates = []
for summary in root.glob("*/summary.jsonl"):
    run = summary.parent
    result = run / "result.jsonl"
    if run.resolve().parent != root or (run / "upgrade").exists():
        continue
    if not all((run / name).is_file() for name in ("result.jsonl", "aggregate.md", "report.md")):
        continue
    try:
        rows = [json.loads(line) for line in result.read_text(encoding="utf-8").splitlines() if line.strip()]
        valid_summary = all(isinstance(json.loads(line), dict) for line in summary.read_text(encoding="utf-8").splitlines() if line.strip())
        complete = rows and all(isinstance(row, dict) and row.get("status", "complete") == "complete" and row.get("mode") != "upgrade" for row in rows)
        age = time.time() - result.stat().st_mtime
        if complete and valid_summary and 0 <= age <= 4 * 60 * 60:
            candidates.append((result.stat().st_mtime, str(run)))
    except (OSError, ValueError):
        continue
if not candidates:
    raise SystemExit("Fresh scoped audit required: no fresh completed same-project audit")
print("BASELINE_AUDIT_DIR=" + max(candidates)[1])
PY
```

After the gate passes, allocate a fresh upgrade run for snapshots and proposal evidence; do not replace the audit's `run-dir` sentinel. Before later shell calls, re-read `RUN_DIR` from `upgrade-run-dir`; substitute its printed literal path into sub-agent prompts and Write destinations.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
RUN_DIR=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/make_run_dir.py" .reports/audit)  # timeout: 5000
[ -n "$RUN_DIR" ] || { echo "! BLOCKED — upgrade run directory unavailable"; exit 1; }
mkdir -p "$RUN_DIR/upgrade"
echo "$RUN_DIR" > "${TMPDIR:-/tmp}/audit-state-${CSID}/upgrade-run-dir"
printf 'UPGRADE_RUN_DIR=%s\n' "$RUN_DIR"
```

### Phase 2: Fetch and classify proposals

**Always spawn fresh foundry:web-explorer** — no context from prior audit runs, cached docs, or memory. Every upgrade run fetches live docs.

Run **Claude Code docs freshness** check from Step 4 of main audit workflow: spawn foundry:web-explorer, validate current config against latest docs, apply genuine-value filter, produce Upgrade Proposals table. Cap 5 total (max 3 capability, any number config).

**RTK hook alignment** — also run Check 10 from main audit workflow (inline, no subagent):

- `rtk` not installed or `.claude/hooks/rtk-rewrite.js` absent: skip silently.
- Otherwise: run `rtk --help`, extract `RTK_PREFIXES` from hook, compare, add findings as **config proposals**:
  - Invalid prefix (not valid RTK subcommand) → config proposal: remove from `RTK_PREFIXES`; severity **high**
  - Filterable RTK command absent from hook → config proposal: add to `RTK_PREFIXES`; severity **medium**

Include alongside docs-based proposals in same Upgrade Proposals table.

No proposals pass filter: print "✓ No upgrade proposals — current setup is current." and stop.

The no-proposal path still writes the Phase 5 completion marker before returning.

### Proposal snapshots

For each config or capability proposal, Write `<RUN_DIR>/upgrade/proposal-state.json` with `kind` (`config` or `capability`), a unique safe `proposal` slug, `action` (`snapshot`, `record-edited`, or `restore`) and `targets` (each with the literal absolute `path` and a unique safe `slug`). Use the same target set for all three actions. Run this block with `snapshot` before editing and verify success; immediately after editing run it with `record-edited` before validators or calibration. Rejection runs it with `restore`. Keep earlier proposal state alongside its evidence before starting another proposal. Snapshots are exclusive writes; missing/corrupt snapshots or concurrent changes stop restoration and require reconciliation, never a Write-tool reconstruction or Git restore.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r RUN_DIR < "${TMPDIR:-/tmp}/audit-state-${CSID}/upgrade-run-dir" || exit 1
python - "$RUN_DIR" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

run = Path(sys.argv[1])
state = json.loads((run / "upgrade/proposal-state.json").read_text(encoding="utf-8"))

def current(path: Path) -> bytes | None:
    """Read a regular target's exact bytes or record its absence."""
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError(f"Unsupported snapshot target: {path}")
    return path.read_bytes() if path.exists() else None

def capture(path: Path, snapshot: Path) -> None:
    """Save immutable bytes and a digest, including an explicit absence marker."""
    data = current(path)
    encoded = {"exists": data is not None, "sha256": hashlib.sha256(data or b"").hexdigest()}
    with snapshot.open("xb") as handle:
        handle.write(data or b"")
    with snapshot.with_suffix(snapshot.suffix + ".json").open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(encoded, handle)
    if load(snapshot) != data:
        raise ValueError(f"Snapshot verification failed: {path}")

def load(snapshot: Path) -> bytes | None:
    """Verify saved bytes against their original digest and existence marker."""
    data = snapshot.read_bytes()
    meta = json.loads(snapshot.with_suffix(snapshot.suffix + ".json").read_text(encoding="utf-8"))
    if hashlib.sha256(data).hexdigest() != meta["sha256"]:
        raise ValueError(f"Corrupt snapshot: {snapshot}")
    return data if meta["exists"] else None

targets = [(Path(item["path"]), run / "upgrade" / f'{state["kind"]}-{state["proposal"]}-{item["slug"]}') for item in state["targets"]]
action = state["action"]
if action in ("snapshot", "record-edited"):
    suffix = ".before" if action == "snapshot" else ".after"
    for target, prefix in targets:
        capture(target, Path(str(prefix) + suffix))
elif action == "restore":
    restored = [(target, load(Path(str(prefix) + ".before")), load(Path(str(prefix) + ".after"))) for target, prefix in targets]
    if any(current(target) != after for target, before, after in restored):
        raise SystemExit("Concurrent changes: restoration stopped for reconciliation")
    for target, before, after in restored:
        if current(target) != after:
            raise SystemExit(f"Concurrent change: restoration stopped for {target}")
        if before is None:
            target.unlink(missing_ok=True)
        else:
            target.write_bytes(before)
        if current(target) != before:
            raise SystemExit(f"Restoration verification failed: {target}")
else:
    raise SystemExit("Unknown snapshot action")
print(f"Snapshot action complete: {action}")
PY
```

### Phase 3: Apply config proposals

Mark "Apply config proposals" in_progress. Each **config** proposal, in sequence:

1. Before each proposal, use the Proposal snapshots block with `kind=config`, `action=snapshot` to save every target's exact current bytes and existence to `<RUN_DIR>/upgrade/config-<proposal>-<target-slug>.before` plus its metadata. Verify success before editing; never use Git restore. Apply change (Edit/Write tool) (inline — exempt from fix.md §No inline fixes, which scopes to Steps 8–10), then run `record-edited` immediately.
2. Correctness check only files changed by this proposal. For each changed JSON or JavaScript target, Write its literal absolute path to `${TMPDIR:-/tmp}/audit-state-${CSID}/upgrade-target` and run this block. Do not check an absent/unmodified `.claude/settings.json`, and do not require `jq`:
   ```bash
   export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
   IFS= read -r TARGET_FILE < "${TMPDIR:-/tmp}/audit-state-${CSID}/upgrade-target" || exit 1
   case "$TARGET_FILE" in
     *.json) python -m json.tool "$TARGET_FILE" > /dev/null && echo "✓ valid JSON" || echo "✗ invalid JSON" ;; # timeout: 5000
     *.js) node --check "$TARGET_FILE" && echo "✓ valid JavaScript" || echo "✗ invalid JavaScript" ;; # timeout: 5000
   esac
   ```
   `node --check` parses only its first file argument, hence one call per hook.
3. Accept (✓) if every applicable check line is ✓; any ✗ line or unavailable validator → run the Proposal snapshots block with `action=restore`, then mark rejected (✗) with reason. It confirms targets still equal this proposal's post-edit bytes before restoring; concurrent changes stop restoration and are reported for reconciliation, never overwritten.

Mark "Apply config proposals" completed.

### Phase 4: A/B test capability proposals

Mark "A/B test capability proposals" in_progress.

**Source-only gate**: `LOCAL_MODE` not `true` → skip this phase; report each capability proposal as `skipped — re-run /audit --upgrade --local to A/B test against plugins/*/ source`. Without `--local` the target agent file is the installed cache copy — an edit there is overwritten by the next plugin update and never reaches source.

Each **capability** proposal (max 3), in sequence:

Resolve `TARGET_FILE` to the existing agent source under `plugins/` before calibration; unresolved target → reject the proposal without editing or falling back to an installed agent. Run the Proposal snapshots block with `kind=capability`, `action=snapshot` before any edit. Allocate a fresh `TIMESTAMP` and distinct before/after run slugs per proposal; never reuse an existing calibration directory.

**Step a — Baseline calibration**: Load the calibrate pipeline template via Bash (not the Read tool — Bash grant is version-proof):

```bash
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/load_mode.py" calibrate templates pipeline-prompt.md --fallback-source  # timeout: 5000
```

Spawn `general-purpose` subagent with that template, target agent name, domain, N=3, MODE=fast, AB_MODE=false, LOCAL_MODE=true, explicit TIMESTAMP, TARGET_LABEL and PROBLEM_SET_VERSION from the current calibrate template, and RUN_SLUG=`upgrade-<proposal>-before`. Require its resolved TARGET_FILE to equal the proposal's source path and record the path and source digest in the run evidence. A missing target, incomplete verdict or nonnumeric recall/F1 rejects the proposal before editing. Capture `recall_before` and `f1_before` from returned JSON.

**Step b — Apply change**: Edit target agent source file (`plugins/<plugin>/agents/<name>.md`) per proposal spec, then run the Proposal snapshots block with `action=record-edited` immediately.

**Step c — Post calibration**: Before dispatch, copy the baseline's fixed problems and ground truth with the block below. Spawn the same pipeline subagent with LOCAL_MODE=true and identical target, domain, N, MODE, AB_MODE, TIMESTAMP, TARGET_LABEL and PROBLEM_SET_VERSION, but RUN_SLUG=`upgrade-<proposal>-after`. After its pre-flight resolves the same edited TARGET_FILE, start at Phase 2, skipping Phases 1a–1c entirely; consume the copied problems.json without regeneration or reselection. Record the edited source digest. An unresolved target, failed copy, incomplete verdict or nonnumeric metric rejects the proposal and restores the snapshot under Step d's restoration rule. Capture `recall_after` and `f1_after`.

Write `<RUN_DIR>/upgrade/calibration-state.json` with literal `before_dir` = `.reports/calibrate/<TIMESTAMP>/upgrade-<proposal>-before/` and `after_dir` = `.reports/calibrate/<TIMESTAMP>/upgrade-<proposal>-after/`, then run:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r RUN_DIR < "${TMPDIR:-/tmp}/audit-state-${CSID}/upgrade-run-dir" || exit 1
python - "$RUN_DIR" <<'PY'
import json
import shutil
import sys
from pathlib import Path

state = json.loads((Path(sys.argv[1]) / "upgrade/calibration-state.json").read_text(encoding="utf-8"))
source = Path(state["before_dir"]) / "problems.json"
expected = source.read_bytes()
json.loads(expected)
after = Path(state["after_dir"])
after.mkdir(parents=True, exist_ok=False)
shutil.copyfile(source, after / "problems.json")
if (after / "problems.json").read_bytes() != expected:
    raise SystemExit("Problem-set copy mismatch: reject proposal")
print("Copied baseline problems; start after arm at Phase 2")
PY
```

**Step d — Decision**:

- `Δrecall = recall_after − recall_before`
- `ΔF1 = f1_after − f1_before`
- Noise floor 0.05 — calibrate's `none` band (`templates/pipeline-prompt.md` A/B verdict); one N=3 baseline cannot separate a smaller delta from run-to-run noise
- **Accept** (✓) if Δrecall ≥ 0 AND ΔF1 ≥ 0 AND (Δrecall ≥ 0.05 OR ΔF1 ≥ 0.05) → keep change
- **Revert** (✗) if either delta negative, or both below the noise floor (`no measurable lift`) → run the Proposal snapshots block with `action=restore`, record deltas. Restore only while the source still equals this proposal's post-edit bytes; concurrent changes require reconciliation, never overwrite them.

Mark "A/B test capability proposals" completed.

### Phase 5: Report and sync

```markdown
## Upgrade Complete — <date>

### Gate
[clean / issues found and stopped]

### Config Changes
| # | Feature | Target | Result | Notes |
|---|---------|--------|--------|-------|
| 1 | ... | hooks/task-log.js | ✓ accepted | syntax valid |

### Capability Changes
| # | Feature | Target | Δrecall | ΔF1 | Result |
|---|---------|--------|---------|-----|--------|
| 1 | ... | agents/curator.md | +0.04 | +0.02 | ✓ accepted |
| 2 | ... | agents/sw-engineer.md | −0.02 | +0.01 | ✗ reverted |

### Next Steps
- Accepted plugin-source edits reach `~/.claude/` only via plugin update/reinstall; `/foundry:setup` afterwards re-merges plugin settings/rules only
- `/audit` — confirm clean baseline after upgrades
- Reverted items: run `/calibrate <agent> full` for deeper A/B signal (N=10 vs N=3 used here)
```

Propose reinstall after upgrade completes — no auto-execute. Print: `` → Update/reinstall the plugin to deliver accepted changes to ~/.claude/, then run `/foundry:setup` to re-merge plugin settings/rules ``

On successful completion, including no proposals, Write `<RUN_DIR>/result.jsonl.upgrade.rec` containing one JSON object with `status:"complete"`, `mode:"upgrade"`, the gate audit path and proposal outcomes; run `python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/append_ledger.py" "<RUN_DIR>/result.jsonl"` to persist the completion marker for TTL cleanup. On abort/error leave result.jsonl absent, preserving incomplete evidence. An upgrade result is never a clean-audit gate source.
