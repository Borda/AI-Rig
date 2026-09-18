---
description: JSONL schema for hypotheses.jsonl, checkpoint.json, and journal.md entry format used by research:run --researcher and --architect
paths:
  - .experiments/**
  - plugins/cc_research/skills/run/**
---

## Run Directory Layout

Canonical layout — other run/mode files point here rather than re-explaining it. A run uses two sibling dirs under `.experiments/`, keyed by same `<run-id>`:

```text
.experiments/state/<run-id>/     ← per-iteration state (all modes)
  state.json         ← iteration count, best metric, status (resume anchor scanned by find_run_id.py)
  experiments.jsonl  ← one line per iteration
  diary.md           ← human-readable research diary

.experiments/<run-id>/           ← hypothesis-pipeline artifacts (--researcher / --architect / --journal)
  hypotheses.jsonl   ← annotated hypothesis queue (oracle + feasibility)
  checkpoint.json    ← per-iteration state for --resume
  journal.md         ← structured learning log, appended after every iteration (when --journal is set)
```

> **Planned unification (code alignment needed — not yet migrated)**: two run-scoped dirs are known duplication (re-explained across several files). Target = one dir per run — merge pipeline artifacts under `.experiments/state/<run-id>/`, leaving `state.json` in place so `find_run_id.py` / `read_state_field.py` resume discovery untouched. Blocked on coordinated change: producers (`run/SKILL.md` `RUN_DIR`, `hypothesis-pipeline.md`, `team.md`), every `<RUN_DIR>` substitution, README/protocol docs must move together; team mode also derives its own `.experiments/run-team-<ts>/` via `make_run_dir`, stores that `run_dir` path in `state.json` — migration must not break team-mode resume.

## hypotheses.jsonl Schema

One JSON obj per line. Single-pass write — each oracle annotates its own entries (a separate solution-architect annotation spawn was removed: generating oracle already read codebase; second agent pass costs full spawn overhead to re-derive same facts):

**Core fields (every oracle):**

| Field | Type | Description |
| -- | -- | -- |
| `hypothesis` | `str` | What to test — concrete, implementable change |
| `rationale` | `str` | Literature or experiment grounding |
| `confidence` | `float` | Oracle confidence [0–1]; entries < 0.7 deprioritized to queue end |
| `expected_delta` | `str` | Expected metric change (e.g. `"+1–3% val_loss"`) |
| `priority` | `int` | Execution order (1 = highest); journal-sourced entries use lower values than oracle entries |
| `source` | `str` | `"oracle"` for researcher; `"journal"` for journal-sourced; `"team"` for team-mode hypothesis agents (Phase A); `"retro"` for `/research:retro` output (feasibility fields absent — treated as `feasible: true`); `"architect"` for architect-only entries |

**Feasibility fields (written by the same oracle in the same pass):**

| Field | Type | Description |
| -- | -- | -- |
| `feasible` | `bool` | `true` if codebase supports change with reasonable effort |
| `blocker` | `str \| null` | Required if `feasible: false`; names specific architectural blocker |
| `codebase_mapping` | `str` | Files, classes, or functions needing change |

Entries missing the feasibility fields (retro output, older queue files) are treated as `feasible: true`, `blocker: null`, `codebase_mapping: ""`.

**Complete valid oracle entry:**

```json
{
  "hypothesis": "...",
  "rationale": "...",
  "confidence": 0.85,
  "expected_delta": "+2% val_acc",
  "priority": 1,
  "source": "oracle",
  "feasible": true,
  "blocker": null,
  "codebase_mapping": "src/model.py:Encoder.forward"
}
```

## Feasibility Filter Rules

- `feasible: false` entries skipped in execution; remain for audit
- `confidence < 0.7` → end of queue, not removed
- Move low-confidence: assign `priority` > max in queue; don't reorder lines (preserve JSONL append order for audit)
- Solution-architect must **preserve hypothesis order** when annotating; no re-rank
- `blocker` required when `feasible: false` — blank/null blocker on false entry = schema violation
- `source: "architect"` and `source: "retro"` entries may omit `feasible`/`blocker`/`codebase_mapping`; absent fields treated as `feasible: true` by all consumers

## checkpoint.json Schema

Written after every iteration; `--resume` uses to skip completed:

```json
{
  "iteration": 3,
  "hypothesis_id": 2,
  "metric_before": 0.842,
  "metric_after": 0.861,
  "status": "passed"
}
```

| Field | Type | Values |
| -- | -- | -- |
| `iteration` | `int` | 1-indexed; monotonically increasing |
| `hypothesis_id` | `int` | 0-indexed position in `hypotheses.jsonl` |
| `metric_before` | `float` | Metric value before applying hypothesis |
| `metric_after` | `float` | Metric value after applying hypothesis |
| `status` | `str` | `"passed"` or `"rolled_back"` |

- Completed iteration in `checkpoint.json` = idempotent — skip, don't re-run
- `status: "rolled_back"` must still write — partial results = audit data
- `status: "rolled_back"` = idempotent on `--resume` same as `passed`; only hypotheses with no checkpoint entry execute

## journal.md Entry Format

Active with `--journal`. Appended after EVERY iteration (kept and reverted). Location: `<RUN_DIR>/journal.md`. Never overwrite — always append.

Each entry:

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

- `Avoid repeating: yes` → Phase 2 ideation skips similar approaches (same file, technique, abstraction)
- `Pattern` emerges after 3+ entries — synthesize what works/doesn't across run
- At iteration 3, Pattern required if trend observable. Write "n/a" only if \<3 entries — not placeholder when entries exist but no trend; if no trend at 3+ entries, write "insufficient signal — no consistent pattern across N iterations"
- No threshold filtering — all iterations recorded regardless of delta
- `Why kept / why reverted` must be substantive — not "it worked"/"it failed"; name mechanism or failure mode

> **Note**: `diary.md` (`.experiments/state/<run-id>/diary.md`) = Phase 7a always-active iteration record. `journal.md` via `--journal` = structured learning log in `.experiments/<run-id>/`, distinct from state diary.

## Journal-Sourced Hypothesis Rules

- Never execute journal hypothesis without feasibility annotation — `feasible` required before campaign loop; the lead annotates journal entries itself when queuing them (it has full run context — no annotator spawn)
- Journal hypotheses inherit oracle JSONL schema; `source: "journal"` only distinguishing field
- `priority` must be numerically higher than all oracle entries — journal hypotheses run after queue exhausted

## Team Mode Extensions

`--team` active: Phase A hypothesis agents produce `source: "team"` entries with 3 **required** additional fields:

| Field | Type | Description |
| -- | -- | -- |
| `axis` | `str` | Optimization axis (e.g., `"model architecture"`) |
| `agent_type` | `str` | Specialist agent for implementation (e.g., `"perf-optimizer"`, `"researcher"`) |
| `change_scope` | `str` | Estimated blast radius: `"small"` (1–2 files), `"medium"` (3–5 files), `"large"` (6+ files or architectural) (primary Phase B sort key — small runs first) |

Team entry missing any of 3 fields = schema violation (like missing `blocker` on infeasible entry).

**Backfill rule** (R0 `--researcher`/`--architect` entries merged into team queue): see Phase A Step 5 in `./modes/team.md`.

**Team-mode output files** (in `<RUN_DIR>/`, alongside `hypotheses.jsonl`):

| File | Written by | Description |
| -- | -- | -- |
| `hypotheses-<axis-slug>.jsonl` | Phase A agents | Per-axis raw hypothesis output; merged into queue in Phase B |
| `hypothesis-analyst-<axis-slug>.md` | Phase A agents | Full analysis and Confidence block (file-handoff protocol) |
| `team-queue.jsonl` | Phase B lead | Final ordered execution queue (post-sort, post-user-gate) |
| `team-results.jsonl` | Phase C lead | Per-hypothesis outcome log (kept/reverted, metric delta, commit SHA) |
