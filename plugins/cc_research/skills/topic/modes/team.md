<!-- file: team.md — consumers: topic/SKILL.md -->

## Team Mode (`--team`)

Use when topic warrants exploring multiple competing method families with adversarial cross-evaluation.

Trigger when: 3+ distinct method families exist AND field has no clear leading method (benchmark spread \<5% between top methods, or no SOTA consensus past 12 months). Skip for topics with clear dominant approach — default single researcher sufficient.

**Workflow:**

1. Lead completes Step 1 (codebase context) as normal

<!-- policy-sibling: plugins/cc_research/skills/judge/SKILL.md, plugins/cc_research/skills/fortify/SKILL.md, plugins/cc_research/skills/verify/SKILL.md, plugins/cc_research/skills/plan/SKILL.md, plugins/cc_research/skills/topic/SKILL.md, plugins/cc_research/skills/retro/SKILL.md, plugins/cc_research/skills/topic/modes/plan.md — Agent-budget spawn-envelope rule restated in each; keep in sync (plugins/CLAUDE.md §Policy Duplication Marker). -->

> **Agent budget** — every spawn prompt must require an envelope even on exhaustion — `partial: true` plus what was finished.

2. Spawn 2–3 **researcher** teammates in one response, each assigned a distinct method cluster: background `Agent(subagent_type="foundry:web-explorer", name="researcher-<cluster-slug>", description="<cluster> survey", prompt=...)` (requires `foundry` plugin). The topic string and Step 1 constraints (framework, compute, dataset) go into each spawn prompt — this skill grants no `SendMessage`/`TeamCreate`, so nothing is broadcast. End the turn; resume on the completion notifications
3. Each teammate researches independently, writes its findings to its own `.temp/output-research-*` file and returns the compact envelope (completion signal — see TEAM_PROTOCOL.md) with a compressed comparison table
4. Cross-challenge runs through the files, not mid-run messages: once every teammate has returned, the lead (2 teammates) or the consolidator (3 teammates) reads all findings files and tests each cluster's key finding against the other clusters' conditions — `does AR1's [finding] hold under AR2's [condition]?` — recording agreement and divergence in the report
5. Lead synthesizes into Step 3 report, noting where researchers agreed or diverged — written to `$REPORT_OUT` (resolved in the bash block below), whether the lead writes it directly (2 teammates) or the consolidator does (3 teammates)

**Note on CLAUDE.md §6 (background agent monitoring)**: teammates are ordinary background `Agent()` spawns writing to `.temp/`. Resume on each completion notification; a notification whose findings file is missing or empty marks that teammate ⏱ `timed_out` at once — never wait on it, poll it, or probe its liveness (`agent-resolution.md` §Agent waits — no polling). Surface the ⏱ in the report and synthesize from the teammates that returned.

Pre-compute before spawning:

```bash
TEAM_PROTOCOL_PATH="$HOME/.claude/TEAM_PROTOCOL.md"
```

**Spawn prompt template:**

```markdown
# Substitute pre-computed values — do not pass raw $(date) expressions or shell vars into spawn prompts
You are a researcher teammate researching: [topic].
Read ~/.claude/TEAM_PROTOCOL.md — use AgentSpeak v2 for inter-agent messages.
Your cluster: [method family N] (e.g., "attention-free architectures" vs "linear attention variants").
Research the top 3 methods in your cluster: comparison table + recommendation given constraints.
Write your full findings (comparison table, analysis, Confidence block) to `.temp/output-research-<teammate-name>-<SPAWN_BRANCH>-<SPAWN_DATE>.md` (substitute pre-computed values from bash block below) using the Write tool.
Return ONLY a compact JSON envelope on your final line, even when out of budget (`partial: true` plus what was finished): {"status":"done","papers":N,"recommendation":"<method>","confidence":0.N,"file":".temp/output-research-<teammate-name>-<date>.md"}
Compact Instructions: preserve paper titles, benchmarks, code links. Discard protocol handshakes.
Task tracking: do NOT call TaskCreate or TaskUpdate — lead owns all task state.
```

Lead synthesizes by reading teammate file paths from delta messages. Pre-compute:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
SPAWN_BRANCH="$(git branch --show-current 2>/dev/null | tr "/" "-" || echo "main")"  # timeout: 3000
SPAWN_DATE="$(date -u +%Y-%m-%d)"  # timeout: 3000
mkdir -p .temp .reports/research  # timeout: 3000
# Anti-overwrite per teammate: run once per teammate before its spawn, with TNAME set to
# that teammate's name; substitute resolved path (not template) into its prompt:
#   _TOUT=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/resolve-anti-overwrite-path.py" .temp "output-research-$TNAME-$SPAWN_BRANCH-$SPAWN_DATE")  # timeout: 5000
# Consolidator report path — same anti-overwrite rule as SKILL.md Step 3 (quality-gates.md)
REPORT_OUT=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/resolve-anti-overwrite-path.py" .reports/research "topic-$SPAWN_BRANCH-$SPAWN_DATE")  # timeout: 5000
# Absolute path — hooks/enforce-topic-header.js reads this to gate the follow-up question
echo "$PWD/$REPORT_OUT" > "${TMPDIR:-/tmp}/research-topic-report-file-${CSID}"
echo "SPAWN_BRANCH=$SPAWN_BRANCH SPAWN_DATE=$SPAWN_DATE REPORT_OUT=$REPORT_OUT"
```

For 3 teammates, spawn consolidator researcher agent: "Read research files at [paths from deltas]. Synthesize into Step 3 unified report structure. Write to `<$REPORT_OUT>` (substitute resolved path from bash block above — not template variable). Return ONLY compact JSON: `{"status":"done","papers":N,"best_method":"<name>","confidence":0.N,"file":"<path>"}`. Task tracking: do NOT call TaskCreate or TaskUpdate — lead owns all task state."

TaskUpdate "Print report header" → `in_progress`.

**MANDATORY, not optional narration** — the consolidator's returned JSON is a routing signal only; it is never printed to the user and never satisfies this step. Consolidator wrote full report to `<file>` (from its envelope) but printed nothing itself. Before returning control to SKILL.md's `## Follow-up gate`:

- (1) Read `<file>` (Read tool).
- (2) Render its `---` header fields as a two-column Markdown table (`Field | Value`, one row per key, file order) per quality-gates.md §Report File Format — never the raw `---`-delimited block.
- (3) Append `→ saved to <file>`. Prepare a plain summary for SKILL.md's Follow-up gate question text from `<file>`: report path, `Title`, `Outcome`, and brief confidence/gap facts, at most 6 plain lines (target 600 characters, hard cap 1,500), no table, pipe layout, or code fence. Options have one-line descriptions and no previews. Print the full header table and compact summary once in the final reply after the answer, never before the question or in an option preview (SKILL.md Step 3).
- (4) TaskUpdate "Print report header" → `completed` once the block is built, never before; preparation does not discharge final delivery.

SKILL.md's Follow-up gate must not fire while this task is `pending`/`in_progress`.

**Hook-enforced**: `hooks/enforce-topic-header.js` (PreToolUse on `AskUserQuestion`) denies the Follow-up gate call while `$REPORT_OUT` (sentinel path above) is missing or empty — a consolidator that never wrote its report cannot be papered over with an ad-hoc summary. The hook also checks that the first question text carries the saved report path, `Title`, and `Outcome` as a plain summary; steps (1)–(4) above prepare it and the full header table required in the final reply after the answer.
