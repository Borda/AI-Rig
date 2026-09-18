# Upgrade Mode — foundry:audit

Triggered by `/audit --upgrade`. Read+executed by `/audit` when `--upgrade` flag present.

## Mode: upgrade

**Trigger**: `/audit --upgrade`

**Purpose**: apply documented Claude Code improvements that passed genuine-value filter. Config changes applied + correctness-checked immediately. Capability changes A/B tested via mini calibrate pipeline — accepted only if Δrecall ≥ 0 and ΔF1 ≥ 0.

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
   node --check .claude/hooks/*.js 2>&1 | grep -v '^$' || true # timeout: 5000
   ```
3. Accept (✓) if check passes; revert + mark rejected (✗) with reason if fails

Mark "Apply config proposals" completed.

### Phase 4: A/B test capability proposals

Mark "A/B test capability proposals" in_progress. Each **capability** proposal (max 3), in sequence:

**Step a — Baseline calibration**: Load the calibrate pipeline template via `cat` (not the Read tool — `Bash(cat:*)` grant is version-proof):

```bash
CALIB_TPL=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/resolve_skill_subdir.py" calibrate templates 2>/dev/null || echo "plugins/cc_foundry/skills/calibrate/templates")  # timeout: 5000
cat "$CALIB_TPL/pipeline-prompt.md"  # timeout: 5000
```

Spawn `general-purpose` subagent with that template, target agent name, domain, N=3, MODE=fast, AB_MODE=false. Capture `recall_before` and `f1_before` from returned JSON.

**Step b — Apply change**: Edit target agent file per proposal spec.

**Step c — Post calibration**: Spawn same pipeline subagent, identical params. Capture `recall_after` and `f1_after`.

**Step d — Decision**:

- `Δrecall = recall_after − recall_before`
- `ΔF1 = f1_after − f1_before`
- **Accept** (✓) if Δrecall ≥ 0 AND ΔF1 ≥ 0 → keep change
- **Revert** (✗) if either delta negative → restore file, record deltas

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
- `/foundry:setup` — propagate accepted changes to ~/.claude/
- `/audit` — confirm clean baseline after upgrades
- Reverted items: run `/calibrate <agent> full` for deeper A/B signal (N=10 vs N=3 used here)
```

Propose `/foundry:setup` after upgrade completes — no auto-execute. Print: `` → Run `/foundry:setup` to propagate accepted changes to ~/.claude/ ``
