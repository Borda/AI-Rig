**Re: Compress file-based handoff protocol to caveman format**

# File-Based Handoff Protocol

## When to apply

- Any skill spawning **2+ agents in parallel** for analysis/review
- Any **single agent** expected to produce >500 tokens of findings/analysis
- Exception: implementation agents (writing code) return inline — output IS deliverable
- Exception: single-agent single-question spawns where output inherently short (\<200 tokens)

## Agent contract

Spawned agent **must**:

1. Write full output (findings, analysis, Confidence block) to `<RUN_DIR>/<agent-name>.md` using Write tool
2. Return to orchestrator **ONLY** compact JSON envelope on final line — nothing else after it:

```json
{
  "status": "done",
  "findings": 3,
  "severity": {
    "critical": 0,
    "high": 1,
    "medium": 2,
    "low": 0
  },
  "file": "<path>",
  "confidence": 0.88,
  "summary": "1 high (missing tool), 2 medium (unused tools)"
}
```

Add task-specific keys (e.g. `"papers":5` for research, `"verdict":"approve"` for review), keep envelope ≤250 bytes. `summary` = one-line description of what found/done — always include.

## RUN_DIR convention

- **Ephemeral** (per-run): `/tmp/<skill>-<timestamp>/` — create once before spawns: `mkdir -p /tmp/<skill>-$(date +%s)`
- **Persistent** (reports): `.temp/` — for final consolidated reports surviving session

## Orchestrator contract

1. **Do NOT read agent files back into main context** — delegate to consolidator agent instead
2. Collect compact envelopes from each spawn (tiny — stay in context)
3. Use envelopes to decide which files need further action (e.g. files with critical findings)
4. Spawn **consolidator agent** to read all `<RUN_DIR>/*.md` and write final report

## Consolidator threshold

- **4+ agent files** → mandatory consolidator; reads all files, writes final report
- **2–3 agent files** → orchestrator may read directly **only if** total expected content \<2K tokens
- Consolidator type: same domain as lead reviewer (e.g. `sw-engineer` for code review, `self-mentor` for config audit)

## Consolidator prompt template

```
Read all finding files in `<RUN_DIR>/`. Apply the consolidation rules from <checklist path>.
Write the consolidated report to `<output path>` using the Write tool.
Return ONLY a compact JSON envelope on your final line — nothing else after it:
{"status":"done","findings":N,"severity":{"critical":N,"high":N,"medium":N,"low":N},"file":"<output path>","confidence":0.N,"summary":"<one-line description of what was found>"}
```

Main context receives only envelope JSON.

## Envelope fields reference

| Field | Required | Description |
| --- | --- | --- |
| `status` | yes | `"done"`, `"done_with_concerns"`, `"needs_context"`, `"timed_out"`, `"error"` |
| `findings` | yes | total finding count (0 if none) |
| `severity` | yes | `{"critical":N,"high":N,"medium":N,"low":N}` |
| `file` | yes | absolute path to the written findings file |
| `confidence` | yes | agent's self-reported confidence (0–1) |
| `summary` | yes | one-line human-readable description of what was found or done |

## Status semantics

| Value | When to use |
| --- | --- |
| `"done"` | Completed with full confidence |
| `"done_with_concerns"` | Completed but agent has doubts — low confidence, incomplete coverage, or unverifiable claims; orchestrator should surface this, not silently accept it |
| `"needs_context"` | Could not produce quality output; re-running with the specific context named in `summary` would unblock the agent |
| `"timed_out"` | Health monitor cut off the agent per §8 protocol |
| `"error"` | Unrecoverable failure |

Orchestrator handling by status:

- `"done"` → accept normally
- `"done_with_concerns"` → include agent's `summary` as flagged concern in consolidated report; not clean completion
- `"needs_context"` → consider re-spawning with missing context named in `summary`; if not feasible, record as partial-result gap
- `"timed_out"` / `"error"` → follow §8 health monitoring protocol; surface with ⏱ in report

## Reference implementation

`/calibrate` = canonical example of file-based handoff at scale — agents write to `/tmp/calibrate-<id>/`; orchestrator collects one-line summaries; consolidation post-collection without flooding main context.

See also `/audit` Step 3 (`self-mentor` agents per file → `<RUN_DIR>/<file-basename>.md`) and `/oss:review` Step 3–6.
