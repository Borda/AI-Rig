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
```

<!-- same branch/date pattern as Step 2a block -->

### Step P2: Codebase analysis

> **Agent budget** — each spawn costs ~120,851 tok of fixed overhead (~73 tool-calls' worth) plus ~12.0 s/call, so work under ~73 calls is cheaper done inline: spawn nothing. Keep each agent near ~55 tool-calls; past ~60 they stall without returning an envelope, forcing reconstruction from disk. Every spawn prompt must require an envelope even on exhaustion — `partial: true` plus what was finished.

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

Print compact terminal summary — MANDATORY, same turn as the write above; TaskUpdate "Print report header" → `completed` only once it has actually appeared in this response. **Hook-enforced**: `hooks/enforce-topic-header.js` (PreToolUse on `AskUserQuestion`) denies SKILL.md's Follow-up gate call while `$PLAN_OUT` (sentinel path from P1) is missing or empty. The hook sees only whether the plan file exists, not whether the print happened; the task above remains the check for the print itself.

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
