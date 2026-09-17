# File-Based Handoff Protocol

## When to apply

- Any skill spawning **2+ agents in parallel** for analysis/review
- Any **single agent** expected to produce >500 tokens of findings/analysis
- Exception: implementation agents (writing code) return inline — output IS deliverable
- Exception: single-agent single-question spawns where output inherently short (\<200 tokens)

## Agent contract

Spawned agent **must**:

1. Write full findings coverage (all findings, analysis, Confidence block — nothing dropped) to `<RUN_DIR>/<agent-name>.md` using Write tool, **prose compressed to ultra-caveman tier** per `quality-gates.md` §Prose Compression (fragments only, zero filler — "full" means no omitted evidence, not verbose prose)
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

Three tiers — pick based on what the dir holds:

- **Ephemeral** (session-scoped): `/tmp/<skill>-<timestamp>/` — OS-managed; short-lived state not needed after session; create once: `mkdir -p /tmp/<skill>-$(date +%s)`
- **Intermediate** (subagent handover, project-scoped): `.temp/<skill>/<timestamp>/` — per-run working dir for subagent output files; 30-day TTL; **NEVER in `.reports/`**
- **Final** (permanent skill output): `.reports/<skill>/<timestamp>/` — consolidated final report only; 30-day TTL when `result.jsonl` present

**Key rule**: `.reports/<skill>/` holds ONLY final consolidated outputs. Intermediate subagent handover files (agent `.md` analysis files) must go to `.temp/<skill>/<timestamp>/`, never to `.reports/`.

**Rollout status**: `oss:review` and `develop:review` implement this fully. `audit`, `calibrate`, `resolve`, `distill` currently mix intermediate + final in `.reports/<skill>/` — pending migration to three-tier.

**Footnote requirement**: final report MUST include `## Source Files` section listing every intermediate agent handover file used (paths relative to repo root, one per line) — lets reviewer locate raw subagent outputs without knowing run timestamp.

## Orchestrator contract

1. **Do NOT read agent files back into main context** — delegate to consolidator agent instead
2. Collect compact envelopes from each spawn (tiny — stay in context)
3. Use envelopes to decide which files need further action (e.g. files with critical findings)
4. Spawn **consolidator agent** to read all `<RUN_DIR>/*.md` and write final report

## Consolidator threshold

- **4+ agent files** → mandatory consolidator; reads all files, writes final report
- **2–3 agent files** → orchestrator may read directly **only if** total expected content \<2K tokens
- Consolidator type: same domain as lead reviewer (e.g. `foundry:sw-engineer` for code review, `foundry:curator` for config audit)

## Consolidator prompt template

```text
Read all finding files in `<RUN_DIR>/`. Apply the consolidation rules from <checklist path>.
Write the consolidated report to `<output path>` using the Write tool.
Return ONLY a compact JSON envelope on your final line — nothing else after it:
{"status":"done","findings":N,"severity":{"critical":N,"high":N,"medium":N,"low":N},"file":"<output path>","confidence":0.N,"summary":"<one-line description of what was found>"}
```

Main context receives only envelope JSON.

## Envelope fields reference

| Field | Required | Description |
| -- | -- | -- |
| `status` | yes | `"done"`, `"done_with_concerns"`, `"needs_context"`, `"timed_out"`, `"error"` |
| `findings` | yes | total finding count (0 if none) |
| `severity` | yes | `{"critical":N,"high":N,"medium":N,"low":N}` |
| `file` | yes | absolute path to written findings file |
| `confidence` | yes | agent self-reported confidence (0–1) |
| `summary` | yes | one-line description of what found/done |

## Status semantics

| Value | When to use |
| -- | -- |
| `"done"` | Completed, full confidence |
| `"done_with_concerns"` | Completed but agent has doubts — low confidence, incomplete coverage, or unverifiable claims; orchestrator surface this, not silently accept |
| `"needs_context"` | No quality output; re-run with specific context named in `summary` unblocks agent |
| `"timed_out"` | Health monitor cut off per §8 protocol |
| `"error"` | Unrecoverable failure |

Orchestrator handling by status:

- `"done"` → accept normally
- `"done_with_concerns"` → include agent `summary` as flagged concern in consolidated report; not clean completion
- `"needs_context"` → consider re-spawn with missing context named in `summary`; if not feasible, record as partial-result gap
- `"timed_out"` / `"error"` → follow §8 health monitoring protocol; surface with ⏱ in report

## Reference implementation

`/oss:review` and `/develop:review` = canonical examples of three-tier convention — intermediate agent handover files in `.temp/review/<timestamp>/`. Final report: `.reports/review/pr-<N>/run-<NNN>/review-report.md` for `/oss:review` (PR-scoped, so `ls .reports/review/pr-<N>/` finds every run for that PR); `.reports/review/<timestamp>/review-report.md` for `/develop:review` (no PR number to key on — local file/diff target).

`/calibrate`, `/audit` predate this convention — they mix intermediate and final in `.reports/<skill>/`. Treat as legacy patterns, not examples to follow. Migration pending.
