**Re: Compress markdown to caveman format**

<!-- Step 1 in SKILL.md dispatches to this mode file. Steps here continue from Step 2. -->

## Mode: routing

> **Codex integration: disabled.** Problem generation and scoring Claude-only. Routing tests orchestrator dispatch logic — scoring deterministic binary match (`selected == expected`). Codex lacks agent system internals context for realistic routing problems.

Routing accuracy test: measures how accurately `general-purpose` orchestrator picks correct `subagent_type` for synthetic task prompts. Not per-agent quality benchmark; included in `all`. Use explicit `routing` target for isolation.

Thresholds (from SKILL.md constants): `ROUTING_ACCURACY_THRESHOLD=0.90`, `ROUTING_HARD_THRESHOLD=0.80`.

### Step 2: Spawn routing pipeline subagent

Mark "Calibrate routing" in_progress. Read `.claude/skills/calibrate/templates/routing-pipeline-prompt.md`. Substitute `<N>` (5 fast, 10 full), `<TIMESTAMP>`, `<MODE>`. Spawn **single** `general-purpose` pipeline subagent with substituted template — handles all phases internally. Proceed to Step 3 after spawn.

Run dir: `.reports/calibrate/<TIMESTAMP>/routing/`

### Report format (Step 3 output)

When target is `routing`, replace standard combined report table with:

```markdown
## Routing Calibration — <date> — <MODE>

| Metric           | Value      | Status |
|------------------|------------|--------|
| Routing accuracy | N/M (XX%)  | ≥90% ✓ / 80–90% ~ / <80% ⚠ |
| Hard accuracy    | N/M (XX%)  | ≥80% ✓ / <80% ⚠ |
| Confusion errors | N          | 0 ✓ / >0 list pairs |
```

Flag routing accuracy < 0.90 or hard accuracy < 0.80 with ⚠. Print confused pair details from routing report's Confused Pairs section. Mark "Calibrate routing" completed.

### Follow-up chain

Routing accuracy < 0.90 or hard accuracy < 0.80 → update descriptions for confused pairs → `/calibrate routing` to verify. Max 3 re-run cycles; still below threshold after third → surface persistent confusion pairs to user for manual review.

Proposals written to: `.reports/calibrate/<TIMESTAMP>/routing/report.md` — Proposals section has targeted wording per confused pair.
