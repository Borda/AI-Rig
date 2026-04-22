# Hypothesis Pipeline — run/SKILL.md sidecar

Loaded by Step R0 when `--researcher` or `--architect` is active.
Contains oracle agent orchestration, feasibility annotation, queue filtering, and checkpoint resume.

> **Research run directory**: outputs (`hypotheses.jsonl`, `checkpoint.json`, `journal.md`) go to `.experiments/<run-id>/` — timestamped dir created at R0 start, distinct from `.experiments/state/<run-id>/`. Called `<RUN_DIR>` throughout. See `protocol.md` (companion file, same skill dir) for layout.

1. **Build hypothesis queue** — if `--hypothesis <path>` provided, read as pre-built queue (skip oracle phase). Otherwise, spawn oracle agents per active flags — parallel if both set:

   **If `--researcher` is set** — spawn `research:scientist` (`maxTurns: 15`):

   ```text
   Read the program file and the project codebase. Generate 5–10 ML experiment hypotheses grounded in SOTA literature and the specific metric goal. Write to `<RUN_DIR>/hypotheses.jsonl` — one JSON object per line, each with fields: hypothesis, rationale, confidence (float 0–1), expected_delta, priority (int, 1=highest), source: "oracle". Write your full analysis, reasoning, and Confidence block to `<RUN_DIR>/oracle-researcher.md` using the Write tool. Return ONLY: {"status":"done","file":"<path>","count":N,"confidence":0.N}
   ```

   **If `--architect` is set** — spawn `foundry:solution-architect` (`maxTurns: 15`) as hypothesis generator (not just feasibility annotator):

   ```text
   Read the program file and the project codebase. Analyze the architecture, coupling, and structural design. Generate 5–10 architectural optimization hypotheses (refactoring opportunities, coupling reductions, abstraction improvements) that could improve the metric. Write to `<RUN_DIR>/hypotheses-arch.jsonl` — one JSON object per line with the same schema as the research oracle (hypothesis, rationale, confidence, expected_delta, priority, source: "architect"). Write your full analysis, reasoning, and Confidence block to `<RUN_DIR>/oracle-solution-architect.md` using the Write tool. Return ONLY: {"status":"done","file":"<path>","count":N,"confidence":0.N}
   ```

   **If both `--researcher` and `--architect` set**: run both oracle agents in parallel. After both complete, merge JSONL files into `<RUN_DIR>/hypotheses.jsonl`, interleaving by priority (lower number = higher priority, round-robin on ties). Update priorities to reflect interleaved order.

   After oracle phase(s), run feasibility annotation pass — spawn `foundry:solution-architect` (`maxTurns: 10`):

   ```text
   Read `<RUN_DIR>/hypotheses.jsonl` and the project codebase. For each hypothesis, annotate with: feasible (bool), blocker (str|null, required if feasible=false), codebase_mapping (str). Write the annotated queue back to the same file preserving order. Write your full analysis, reasoning, and Confidence block to `<RUN_DIR>/oracle-feasibility.md` using the Write tool. Return ONLY: {"status":"done","file":"<path>","feasible":N,"infeasible":N,"confidence":0.N}
   ```

   Note: when `--architect` only (no `--researcher`), skip feasibility annotation — architect already validated feasibility. Set `feasible: true` implicitly.

   Both agents follow handoff envelope protocol (CLAUDE.md §2). Schema: `protocol.md` (companion file, same skill dir).

2. **Filter and sort** — load annotated queue. Infeasible (`feasible: false`) stay for audit, excluded from execution. Sort by `priority` ascending (1 = first).

3. **Resume skip** — if `<RUN_DIR>/checkpoint.json` exists (resuming crashed run), read it. Skip any hypothesis whose 0-indexed position matches `hypothesis_id` in checkpoint.

4. Store active queue in memory as `RESEARCH_QUEUE`.
