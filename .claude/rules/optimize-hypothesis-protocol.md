---
description: JSONL schema for hypotheses.jsonl, checkpoint.json, and journal.md entry format used by /optimize run --researcher and --architect
paths:
  - .experiments/**
  - .claude/skills/optimize/**
---

## Run Directory Layout

Every `--researcher` run writes to `.experiments/<run-id>/`:

```
.experiments/<run-id>/
  hypotheses.jsonl   ← annotated hypothesis queue (oracle + feasibility)
  checkpoint.json    ← per-iteration state for --resume
  journal.md         ← structured learning log, appended after every iteration (when --journal is set)
```

## hypotheses.jsonl Schema

One JSON object per line. Field groups are written by separate agents in two passes:

**Pass 1 — ai-researcher (oracle):**

| Field            | Type    | Description                                                                                                                       |
| ---------------- | ------- | --------------------------------------------------------------------------------------------------------------------------------- |
| `hypothesis`     | `str`   | What to test — concrete, implementable change                                                                                     |
| `rationale`      | `str`   | Literature or experiment grounding for the hypothesis                                                                             |
| `confidence`     | `float` | Oracle confidence [0–1]; entries < 0.7 are deprioritized to end of queue                                                          |
| `expected_delta` | `str`   | Expected metric change (e.g. `"+1–3% val_loss"`)                                                                                  |
| `priority`       | `int`   | Execution order (1 = highest); journal-sourced entries use lower values than oracle entries                                       |
| `source`         | `str`   | `"oracle"` for ai-researcher entries; `"journal"` for journal-sourced entries; `"team"` for team-mode hypothesis agents (Phase A) |

**Pass 2 — solution-architect (feasibility filter):** annotates in place, preserving order

| Field              | Type          | Description                                                             |
| ------------------ | ------------- | ----------------------------------------------------------------------- |
| `feasible`         | `bool`        | `true` if the codebase supports the change with reasonable effort       |
| `blocker`          | `str \| null` | Required if `feasible: false`; names the specific architectural blocker |
| `codebase_mapping` | `str`         | Files, classes, or functions that would need to change                  |

**Minimal valid oracle entry (before feasibility pass):**

```json
{
  "hypothesis": "...",
  "rationale": "...",
  "confidence": 0.85,
  "expected_delta": "+2% val_acc",
  "priority": 1,
  "source": "oracle"
}
```

**After feasibility annotation** (3 fields added by solution-architect — see Pass 2 table above):

```json
{
  "feasible": true,
  "blocker": null,
  "codebase_mapping": "src/model.py:Encoder.forward"
}
```

## Feasibility Filter Rules

- `feasible: false` entries are skipped in campaign execution; they remain in the file for audit purposes
- `confidence < 0.7` entries are moved to the end of the queue — not removed
- Solution-architect must **preserve hypothesis order** when annotating; do not re-rank
- `blocker` is required when `feasible: false` — a blank or null blocker on a false entry is a schema violation

## checkpoint.json Schema

Written after every iteration; used by `--resume` to skip completed iterations:

```json
{
  "iteration": 3,
  "hypothesis_id": 2,
  "metric_before": 0.842,
  "metric_after": 0.861,
  "status": "passed"
}
```

| Field           | Type    | Values                                      |
| --------------- | ------- | ------------------------------------------- |
| `iteration`     | `int`   | 1-indexed; monotonically increasing         |
| `hypothesis_id` | `int`   | 0-indexed position in `hypotheses.jsonl`    |
| `metric_before` | `float` | Metric value before applying the hypothesis |
| `metric_after`  | `float` | Metric value after applying the hypothesis  |
| `status`        | `str`   | `"passed"` or `"rolled_back"`               |

- A completed iteration already in `checkpoint.json` is idempotent — skip it, do not re-run
- A `status: "rolled_back"` entry must still be written — partial results are still audit data

## journal.md Entry Format

Active when `--journal` flag is set. Appended after EVERY iteration (kept and reverted). Location: `<RUN_DIR>/journal.md`. Never overwrite — always append.

Each entry follows this exact structure:

```markdown
## Iteration N — YYYY-MM-DD

**Approach**: <agent's description from Phase 2 JSON — the proposed change>
**Outcome**: <kept | reverted | rework | no-op | hook-blocked | timeout>
**Metric delta**: <metric_before> → <metric_after> (<+/->X.X%) — or "n/a" if no metric was measured
**Why kept / why reverted**: <one sentence — e.g. "Metric improved 1.2% with guard passing" or "Reverted: guard failed after 2 rework attempts; test_model.py broke" or "No files changed">
**Avoid repeating**: <yes | no> — yes if outcome was reverted/blocked/no-op AND approach was not a transient failure (e.g. hook issue); no if kept or if the failure was infrastructure (timeout, hook), not the approach itself
**Pattern**: <cross-iteration observation if ≥3 journal entries exist, otherwise "n/a">

---
```

Rules:

- `Avoid repeating: yes` signals the ideation agent in Phase 2 to skip similar approaches (same file, same technique, same abstraction)
- `Pattern` emerges after 3+ entries — synthesize what is / isn't working across the run
- Do NOT use threshold filtering — all iterations are recorded regardless of delta magnitude
- `Why kept / why reverted` must be substantive — not "it worked" or "it failed"; name the specific mechanism or failure mode

> **Note**: The per-run `diary.md` (`.experiments/state/<run-id>/diary.md`) written by Phase 7a of the campaign loop is a separate, always-active record of all iterations. The `journal.md` written by `--journal` is a structured learning log in the research/architect run directory (`.experiments/<run-id>/`), distinct from the state diary.

## Journal-Sourced Hypothesis Rules

- Never execute a journal-sourced hypothesis without a feasibility annotation — `feasible` must be present before it enters the campaign loop
- Journal hypotheses inherit the same JSONL schema as oracle hypotheses; `source: "journal"` is the only distinguishing field
- Priority assignment: `priority` must be numerically higher (lower priority) than all existing oracle entries so journal hypotheses run after the original queue is exhausted

## Team Mode Extensions

When `--team` is active, hypothesis agents in Phase A produce entries with `source: "team"` and three additional optional fields that are absent from oracle/journal entries:

| Field          | Type  | Description                                                                                                                                                |
| -------------- | ----- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `axis`         | `str` | The optimization axis the hypothesis belongs to (e.g., `"model architecture"`)                                                                             |
| `agent_type`   | `str` | Specialist agent type to use for implementation (e.g., `"perf-optimizer"`, `"ai-researcher"`)                                                              |
| `change_scope` | `str` | Estimated blast radius: `"small"` (1–2 files), `"medium"` (3–5 files), `"large"` (6+ files or architectural) (primary Phase B sort key — small runs first) |

**Backfill rule** (for R0 `--researcher`/`--architect` entries merged into a team queue): see Phase A Step 5 in `.claude/skills/optimize/modes/team.md` for the full backfill logic.

**Team-mode output files** (in `<RUN_DIR>/`, alongside `hypotheses.jsonl`):

| File                                | Written by     | Description                                                          |
| ----------------------------------- | -------------- | -------------------------------------------------------------------- |
| `hypotheses-<axis-slug>.jsonl`      | Phase A agents | Per-axis raw hypothesis output; merged into queue in Phase B         |
| `hypothesis-analyst-<axis-slug>.md` | Phase A agents | Full analysis and Confidence block (file-handoff protocol)           |
| `team-queue.jsonl`                  | Phase B lead   | Final ordered execution queue (post-sort, post-user-gate)            |
| `team-results.jsonl`                | Phase C lead   | Per-hypothesis outcome log (kept/reverted, metric delta, commit SHA) |
