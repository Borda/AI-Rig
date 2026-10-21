# Hypothesis Pipeline — run/SKILL.md sidecar

Loaded by Step R0 (executes after R2) when `--researcher`, `--architect`, or `--hypothesis` active. Contains oracle agent orchestration, feasibility annotation, queue filtering, checkpoint resume.

> **Research run directory**: outputs (`hypotheses.jsonl`, `checkpoint.jsonl`, `journal.md`) go to `.experiments/<run-id>/` — timestamped dir created at R1 (R0 block prints it), distinct from `.experiments/state/<run-id>/`. Called `<RUN_DIR>` throughout. See `protocol.md` (companion file, same skill dir) for layout.

**Spawn note**: oracle agents run in the background — issue the batch, then end the turn; no filler call, no "waiting" line, no sleep (CLAUDE.md §6). On completion notifications, check each oracle's output (e.g. `<RUN_DIR>/oracle-researcher.md`); missing or empty → surface with ⏱, continue with partial hypotheses or empty queue if none written.

1. **Build hypothesis queue** — if `--hypothesis <path>` provided, read as pre-built JSONL queue (skip oracle phase); file is read-only — backfill and `hypothesis_id` tagging happen in memory, never written back. Resuming crashed run with `<RUN_DIR>/hypotheses.jsonl` already on disk → reuse that file as queue, skip oracle phase: re-spawned oracles write a different queue, so checkpointed ids would name other hypotheses. Otherwise spawn oracle agents per active flags — parallel if both set:

   **If `--researcher` set** — spawn `research:scientist` (`maxTurns: 15`):

   ```text
   Read the program file and the project codebase. Generate 5–10 ML experiment hypotheses grounded in SOTA literature and the specific metric goal. Write to `<RUN_DIR>/hypotheses.jsonl` — one JSON object per line, each with fields: hypothesis, rationale, confidence (float 0–1), expected_delta, priority (int, 1=highest), source: "oracle", feasible (bool — grounded in the codebase you just read), blocker (str|null, required if feasible=false), codebase_mapping (str — files/classes/functions the change touches). Write your full analysis, reasoning, and Confidence block to `<RUN_DIR>/oracle-researcher.md` using the Write tool. Return ONLY: {"status":"done","file":"<path>","count":N,"feasible":N,"confidence":0.N}
   ```

   **If `--architect` set** — spawn `foundry:solution-architect` (`maxTurns: 15`) as hypothesis generator (not just feasibility annotator):

   ```text
   Read the program file and the project codebase. Analyze the architecture, coupling, and structural design. Generate 5–10 architectural optimization hypotheses (refactoring opportunities, coupling reductions, abstraction improvements) that could improve the metric. Write to `<RUN_DIR>/hypotheses-arch.jsonl` — one JSON object per line with the same schema as the research oracle (hypothesis, rationale, confidence, expected_delta, priority, source: "architect", feasible, blocker, codebase_mapping — annotate feasibility yourself from the codebase you just read). Write your full analysis, reasoning, and Confidence block to `<RUN_DIR>/oracle-solution-architect.md` using the Write tool. Return ONLY: {"status":"done","file":"<path>","count":N,"confidence":0.N}
   ```

   **Both `--researcher` and `--architect` set**: run both oracle agents parallel. After both done, merge JSONL files into `<RUN_DIR>/hypotheses.jsonl`, interleaving by priority (lower = higher priority, round-robin on ties). Update priorities to reflect interleaved order.

   No separate feasibility-annotation spawn — each oracle annotates its own hypotheses (`feasible`/`blocker`/`codebase_mapping` are in both prompts above; both oracles already read the codebase). Dedicated `foundry:solution-architect` annotator pass costs ~120,851 tok fixed overhead to re-read same codebase for facts generating oracle already had in hand — annotation is factual codebase mapping, not adversarial review. Entries missing the fields after an oracle returns (older queue files, partial output): backfill `feasible: true`, `blocker: null`, `codebase_mapping: ""` and flag the count in the R0 summary.

   Both agents follow handoff envelope protocol (CLAUDE.md §2). Schema: `protocol.md` (companion file, same skill dir).

2. **Filter and sort** — load annotated queue. Before filtering, tag every entry with `hypothesis_id` = its 0-indexed line in the queue file (file order, never sorted position). Infeasible (`feasible: false`) stay for audit, excluded from execution. Sort by `priority` ascending (1 = first); entries keep their `hypothesis_id`.

3. **Resume skip** — if `<RUN_DIR>/checkpoint.jsonl` exists (resuming crashed run), read it; absent, read legacy `<RUN_DIR>/checkpoint.json` (same JSONL lines, written by runs before the rename) when present. Skip any hypothesis whose `hypothesis_id` (step 2 file-line id, not queue position) appears in checkpoint.

4. Store active queue in memory as `RESEARCH_QUEUE`. Checkpoint line for each popped entry records its `hypothesis_id`.
