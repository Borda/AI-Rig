# Upgrade Mode — foundry:audit

Triggered by `/audit --upgrade`. Read+executed by `/audit` when `--upgrade` flag present.

## Mode: upgrade

**Trigger**: `/audit --upgrade`

**Purpose**: apply documented Claude Code improvements that passed genuine-value filter. Config changes applied + correctness-checked immediately. Capability changes A/B tested via mini calibrate pipeline against source files (`--local` only) — accepted only if neither delta is negative and at least one clears the noise floor (Phase 4 Step d).

**Task tracking**: TaskCreate "Fetch upgrade proposals", "Apply config proposals", "A/B test capability proposals". Mark in_progress/completed throughout.

### Phase 1: Gate check

Verify no unresolved blocking findings before applying anything.

Critical/high issues from recent `/audit` run: stop, print "⚠ Resolve critical/high findings first (run `/audit` and pick fix level from gate), then re-run `/audit --upgrade`."

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

### Phase 3: Apply config proposals

Mark "Apply config proposals" in_progress. Each **config** proposal, in sequence:

1. Apply change (Edit/Write tool) (inline — exempt from fix.md §No inline fixes, which scopes to Steps 8–10)
2. Correctness check:
   ```bash
   jq empty .claude/settings.json && echo "✓ valid JSON" || echo "✗ invalid JSON" # timeout: 5000
   for f in .claude/hooks/*.js; do [ -e "$f" ] || continue; node --check "$f" && echo "✓ $f" || echo "✗ $f"; done # timeout: 5000
   ```
   `node --check` parses only its first file argument, hence one call per hook.
3. Accept (✓) if every check line is ✓; any ✗ line → revert + mark rejected (✗) with reason

Mark "Apply config proposals" completed.

### Phase 4: A/B test capability proposals

Mark "A/B test capability proposals" in_progress.

**Source-only gate**: `LOCAL_MODE` not `true` → skip this phase; report each capability proposal as `skipped — re-run /audit --upgrade --local to A/B test against plugins/*/ source`. Without `--local` the target agent file is the installed cache copy — an edit there is overwritten by the next plugin update and never reaches source.

Each **capability** proposal (max 3), in sequence:

**Step a — Baseline calibration**: Load the calibrate pipeline template via Bash (not the Read tool — Bash grant is version-proof):

```bash
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/load_mode.py" calibrate templates pipeline-prompt.md --fallback-source  # timeout: 5000
```

Spawn `general-purpose` subagent with that template, target agent name, domain, N=3, MODE=fast, AB_MODE=false. Capture `recall_before` and `f1_before` from returned JSON.

**Step b — Apply change**: Edit target agent source file (`plugins/<plugin>/agents/<name>.md`) per proposal spec.

**Step c — Post calibration**: Spawn same pipeline subagent, identical params. Capture `recall_after` and `f1_after`.

**Step d — Decision**:

- `Δrecall = recall_after − recall_before`
- `ΔF1 = f1_after − f1_before`
- Noise floor 0.05 — calibrate's `none` band (`templates/pipeline-prompt.md` A/B verdict); one N=3 baseline cannot separate a smaller delta from run-to-run noise
- **Accept** (✓) if Δrecall ≥ 0 AND ΔF1 ≥ 0 AND (Δrecall ≥ 0.05 OR ΔF1 ≥ 0.05) → keep change
- **Revert** (✗) if either delta negative, or both below the noise floor (`no measurable lift`) → restore file, record deltas

Mark "A/B test capability proposals" completed.

### Phase 5: Report and sync

```markdown
## Upgrade Complete — <date>

### Gate
[clean / issues found and stopped]

### Config Changes
| # | Feature | Target | Result | Notes |
|---|---------|--------|--------|-------|
| 1 | ... | hooks/task-log.js | ✓ accepted | jq valid |

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
