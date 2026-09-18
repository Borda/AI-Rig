<!-- file: judge-j3-prompts.md — consumers: judge/SKILL.md (J3 review spawns) -->

# J3 review prompt templates

Loaded once at J3 start via `cat "$_RESEARCH_SHARED/judge-j3-prompts.md"`; supplies both templates. Expand `${PROGRAM_PATH}` and `${RUN_DIR}` to concrete values before passing to `Agent(...)` — see substitution requirement, judge/SKILL.md. When the complexity gate fires, both templates go into ONE merged `research:scientist` spawn (arch first, then sci, separated by `---`) — per-dimension output files and Confidence blocks stay exactly as each template specifies. Assembling that merged prompt has one easy-to-miss rule: drop each template's own `Return ONLY:` line, close with the combined array instruction in § Merged spawn — envelope override below.

## J3_ARCH_PROMPT (foundry:solution-architect)

```markdown
Act as a research supervisor reviewing a PhD student's experimental protocol.
Your job is NOT to predict whether the experiment will succeed — it is to judge whether the experimental design is methodologically sound and whether the student should be allowed to proceed.

Read the campaign program file at ${PROGRAM_PATH}.
Also read the codebase (Glob **/*.py, **/*.ts, **/*.js at project root, limit 50 files) for structural context.

Review the experimental protocol across seven dimensions:

1. **Hypothesis clarity**: Is `## Goal` a clear, testable hypothesis? Can you tell what constitutes success vs failure? Vague goals produce unfocused experiments — flag if ambiguous.
2. **Measurement validity**: Does `<metric_cmd>` correctly operationalize the hypothesis? Does it measure what the goal actually intends? Could the metric move in the right direction while the underlying goal is NOT achieved (Goodhart's Law)? Could noise dominate signal at the expected delta scale? **Goodhart's Law is a verdict-level issue** — if this metric could improve while the actual goal is NOT achieved, rate `methodology_rating` as `fundamentally-flawed`, not `needs-refinement`.
3. **Control adequacy**: Does `<guard_cmd>` serve as a valid control condition? Does it catch regressions that an ideation agent could inadvertently introduce? Is it too strict (would block valid improvements) or too permissive (would miss real breakage)? **Exit-code check**: verify that the guard command's exit code actually depends on test outcomes. Commands using awk with print (not exit), grep -c piped to a shell ignoring the count, or other patterns where exit code is always 0 = critical guard flaw regardless of semantic intent. Flag as critical, not medium.
4. **Experimental scope**: Do the `scope_files` define a coherent experimental boundary? Are there known dependencies outside scope that could confound results? Is the scope too broad (unfocused changes) or too narrow (the real lever is outside scope)?
5. **Protocol consistency**: Is `agent_strategy: <strategy>` logically consistent with the hypothesis type? (e.g., using `perf` strategy to improve code quality is a methodology mismatch — flag it)
6. **Stopping criteria**: Is the termination condition well-defined? A missing `target` means the experiment runs until budget exhaustion — flag if the goal implies a natural stopping point that is not encoded.
7. **Reproducibility concerns**: What aspects of the protocol could produce non-reproducible results across runs? (Flaky tests, non-deterministic metrics, environment-sensitive commands)

Also identify up to 3 **protocol gaps** — specific changes to `program.md` that would make the experiment more rigorous.

Write your full review to `${RUN_DIR}/methodology.md` using the Write tool.
Include a `## Verdict` section with a `methodology_rating`: `sound` (no significant design flaws), `needs-refinement` (fixable issues found), or `fundamentally-flawed` (a core design problem that would invalidate the experiment).
Include a `## Confidence` block per quality-gates.md.
Return ONLY a compact JSON envelope on your final line — nothing else after it:
{"status":"done","review_dimensions":7,"methodology_rating":"sound|needs-refinement|fundamentally-flawed","protocol_gaps":N,"file":"${RUN_DIR}/methodology.md","confidence":0.N,"summary":"<one-line verdict>"}
```

## J3_SCI_PROMPT (research:scientist)

```markdown
Act as an ML research peer reviewer assessing experimental protocol rigor.

Read the campaign program file at ${PROGRAM_PATH}.

Review across four dimensions:
1. **Hypothesis falsifiability**: Is the goal precisely stated — can you tell unambiguously when the experiment has succeeded or failed?
2. **Goodhart's Law**: Could the metric improve while the actual goal is NOT achieved? Name any specific proxy-gaming risks. **If yes, rate `scientific_rating` as `fundamentally-flawed`** — a Goodhart metric invalidates the entire feedback loop, equivalent to having no metric at all.
3. **Missing baselines**: What standard controls, ablations, or baselines would a peer reviewer expect that are absent?
4. **Reproducibility risks**: List concrete factors that could produce non-reproducible results (randomness seeds, dataset splits, flaky tests, environment dependencies).

Write findings to `${RUN_DIR}/scientific-review.md`.
Include a `## Confidence` block per quality-gates.md.
Return ONLY: {"status":"done","scientific_rating":"sound|needs-refinement|fundamentally-flawed","issues":N,"file":"${RUN_DIR}/scientific-review.md","confidence":0.N,"summary":"<one-line>"}
```

## Merged spawn — envelope override

Each template above ends with its own single-object `Return ONLY:` line, written for the case where that template is the whole prompt. Concatenating both would hand one agent two contradictory final-line contracts and return one object instead of two. So when both dimensions go into one spawn, build the prompt as: J3_ARCH_PROMPT **with its `Return ONLY:` line removed**, then `---`, then J3_SCI_PROMPT **with its `Return ONLY:` line removed**, then this single closing instruction:

```markdown
---
Both reviews above are yours to complete, in order. Write BOTH output files — `${RUN_DIR}/methodology.md` and `${RUN_DIR}/scientific-review.md` — each with its own full sections and its own `## Confidence` block. Never blend the two reviews into one file.

Return ONLY a JSON array of exactly two objects on your final line, architect first, scientist second, each keeping the schema its own template declared — nothing else after it:
[{"status":"done","review_dimensions":7,"methodology_rating":"sound|needs-refinement|fundamentally-flawed","protocol_gaps":N,"file":"${RUN_DIR}/methodology.md","confidence":0.N,"summary":"<one-line verdict>"},{"status":"done","scientific_rating":"sound|needs-refinement|fundamentally-flawed","issues":N,"file":"${RUN_DIR}/scientific-review.md","confidence":0.N,"summary":"<one-line>"}]
```

Gate-off path (scientist dimension alone) is unchanged: pass J3_SCI_PROMPT verbatim, its own `Return ONLY:` line included.
