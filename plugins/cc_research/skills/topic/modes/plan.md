<!-- file: plan.md — consumers: topic/SKILL.md -->

## Plan Mode

Only when first token of `$ARGUMENTS` is exactly `plan` (not prefix match — "planning algorithms" must NOT trigger this mode).

Produce sequenced, dependency-ordered implementation plan from SOTA research findings, mapped against current codebase. Use after research run identified recommended method — needed before `/develop:feature` (requires `develop` plugin).

**Input detection** (parse argument after `plan`):

- No argument → **auto-detect**: use Glob (pattern `topic-*.md`, path `.reports/research/`) to find recent research outputs; exclude paths containing `-plan-` or `-codebase-`; sort by modification time descending; pick most recent. Print `→ Using: <path>` before proceeding. No file found → stop: "No recent research output found — run `/research:topic <topic>` first."
- Ends in `.md` → treat as path to existing research output file; skip to Step P1-B

### Step P1: Gather research findings

**P1-A — Auto-detected report**: when the no-argument auto-detect above found a recent `.reports/research/topic-*.md`, read that file. (Plan mode dispatches BEFORE Steps 2–3, skips them — "fresh" report never exists in same run; input always prior run's report.) Extract: Recommendation section, Implementation Plan, Key Hyperparameters, Gotchas, Integration with Current Codebase.

**P1-B — From existing output**: Read file at given path directly. Extract same sections.

**Validation**: file must contain clear **Recommendation** section naming specific method. Missing or ambiguous → stop: "Research output does not contain a clear method recommendation — run `/research:topic <topic>` first, then pass the output path."

Before spawning in Steps P2–P3, pre-compute output path components:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
BRANCH=$(git branch --show-current 2>/dev/null | tr '/' '-' || echo 'main')  # timeout: 3000
DATE=$(date +%Y-%m-%d)  # timeout: 3000
mkdir -p .temp .reports/research  # timeout: 3000
# Anti-overwrite counter-suffix (quality-gates.md §Output Routing) — same rule as SKILL.md Step 3
CODEBASE_OUT=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/resolve-anti-overwrite-path.py" .temp "output-research-codebase-$BRANCH-$DATE")  # timeout: 5000
PLAN_OUT=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/resolve-anti-overwrite-path.py" .reports/research "topic-plan-$BRANCH-$DATE")  # timeout: 5000
# Absolute path — hooks/enforce-topic-header.js reads this to gate the follow-up question
echo "$PWD/$PLAN_OUT" > "${TMPDIR:-/tmp}/research-topic-report-file-${CSID}"
echo "CODEBASE_OUT=$CODEBASE_OUT PLAN_OUT=$PLAN_OUT"
```

<!-- same branch/date pattern as Step 2a block -->

### Step P2: Codebase analysis

<!-- policy-sibling: plugins/cc_research/skills/judge/SKILL.md, plugins/cc_research/skills/fortify/SKILL.md, plugins/cc_research/skills/verify/SKILL.md, plugins/cc_research/skills/plan/SKILL.md, plugins/cc_research/skills/topic/SKILL.md, plugins/cc_research/skills/retro/SKILL.md, plugins/cc_research/skills/topic/modes/team.md — Agent-budget spawn-envelope rule restated in each; keep in sync (plugins/CLAUDE.md §Policy Duplication Marker). -->

> **Agent budget** — every spawn prompt must require an envelope even on exhaustion — `partial: true` plus what was finished.

**Agent wait**: the spawn runs in the background — end the turn and resume on its completion notification, never a `sleep`, `ScheduleWakeup`, `ListAgents`, or `Monitor` loop. A notification without the expected output marks the agent ⏱ `timed_out` at once; surface it in the report (`agent-resolution.md` §Agent waits — no polling).

Call `Agent(subagent_type="foundry:solution-architect", prompt=...)`:

```markdown
Read research findings file at <path from P1>.
Analyze current codebase to map recommended method against existing code:
1. Identify all files/modules relevant to recommended method's domain
2. Map existing abstractions: interfaces, base classes, patterns codebase already uses
3. Identify integration points: where does the new method plug in?
4. Flag conflicts: existing patterns that would need to change
5. Estimate complexity per integration point (low/medium/high)

Write full analysis to `<$CODEBASE_OUT>` via Write tool. (Substitute resolved path — not template variable.)
Return ONLY a compact JSON envelope on your final line — nothing else after it:
{"status":"done","integration_points":N,"conflicts":N,"file":"<$CODEBASE_OUT>","confidence":0.N,"summary":"N integration points, N conflicts"}
```

### Step P3: Synthesize plan

Read both files (research findings from P1 + codebase analysis from P2). Produce phased plan, write to `$PLAN_OUT` (resolved path from P1 bash block):

```markdown
---
Title: Research Plan — [method name]
Date: [YYYY-MM-DD]
Scope: [original arguments]
Focus: implementation roadmap mapped to codebase
Agents: foundry:solution-architect
Outcome: [ready / needs work]
Summary: [phase count, task count, top risk]
Confidence: [score] — [key gaps]
Next steps: /develop:feature <first Phase 1 task> (requires `develop` plugin)
Path: [resolved PLAN_OUT]
---

## Implementation Roadmap: [method name]
Topic: [original $ARGUMENTS]

### Prerequisites
- [dependency, environment requirement, or data prerequisite]

### Phase 1: Foundation — [description]
**Goal**: [what this phase achieves and why it must come first]
| Task | Files | Depends On | Complexity | Verification |
|------|-------|------------|------------|--------------|
| ...  | ...   | —          | low/med/hi | [how to verify done] |

### Phase 2: Core Implementation — [description]
**Goal**: [what this phase achieves]
| Task | Files | Depends On | Complexity | Verification |
|------|-------|------------|------------|--------------|
| ...  | ...   | Phase 1    | ...        | ...          |

### Phase 3: Integration & Validation — [description]
**Goal**: wire into existing pipeline, validate end-to-end
[same table format]

### Risks
| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| ...  | low/med/hi | ...    | ...        |

### Estimated Effort
| Phase | Tasks | Complexity Mix | Estimate |
|-------|-------|----------------|----------|
| 1     | N     | N low, M med   | X days   |

### Next Steps
- Phase 1 ready → `/develop:feature <first task from Phase 1>` (requires `develop` plugin)
- Full plan approved → create `.plans/active/todo_<method>.md` with phases as task groups
```

TaskUpdate "Print report header" → `in_progress`.

Read `$PLAN_OUT` and render every `---` header field as a two-column `Field | Value` table in file order, followed by the compact summary below, in the same turn. Prepare the plain summary for SKILL.md's Follow-up gate question text from `$PLAN_OUT`: report path, `Title`, `Outcome`, phase/task counts, and brief confidence/gap facts, at most 6 plain lines (target 600 characters, hard cap 1,500), no table, pipe layout, or code fence. Options have one-line descriptions and no previews. Print the full header table and compact summary once in the final reply after the answer, never before the question or in an option preview. Then TaskUpdate "Print report header" → `completed` once the block is built; preparation does not discharge final delivery. The topic hook blocks only the workflow follow-up until the saved report exists and the first question text contains its matching path, `Title`, and `Outcome`. Diagnostic/recovery questions remain available; unreadable delivery evidence does not authorize the follow-up.

```text
---
Research Plan — [method name]
Phases:      [N] phases, [M] tasks total
Complexity:  [N low / M medium / K high]
Top risk:    [one-line from risks table]
Confidence:  [score] — [key gaps]
→ saved to .reports/research/topic-plan-[date].md
---
```
