<!-- file: team.md — consumers: topic/SKILL.md -->

## Team Mode (`--team`)

Use when topic warrants exploring multiple competing method families with adversarial cross-evaluation.

Trigger when: 3+ distinct method families exist AND field has no clear leading method (benchmark spread \<5% between top methods, or no SOTA consensus past 12 months). Skip for topics with clear dominant approach — default single researcher sufficient.

**Workflow:**

1. Lead completes Step 1 (codebase context) as normal

> **Agent budget** — each teammate costs ~120,851 tok of fixed overhead (~73 tool-calls' worth) plus ~12.0 s/call, so work under ~73 calls is cheaper done inline: spawn nothing. Keep each teammate near ~55 tool-calls; past ~60 they stall without returning an envelope, forcing reconstruction from disk. Every spawn prompt must require an envelope even on exhaustion — `partial: true` plus what was finished.

2. Spawn 2–3 **researcher** teammates, each assigned distinct method cluster
3. Broadcast constraints to all: `broadcast {topic: <topic>, constraints: <framework/compute/dataset from Step 1>}`
4. Each teammate researches independently, reports with `deltaT# HOOK:verify` (AgentSpeak v2 completion signal — see TEAM_PROTOCOL.md) and compressed comparison table
5. Lead routes key findings from one researcher to others for cross-challenge: `@AR2: AR1 found [finding] — does it hold under [condition]?`
6. Lead synthesizes into Step 3 report, noting where researchers agreed or diverged — written to `$REPORT_OUT` (resolved in the bash block below), whether the lead writes it directly (2 teammates) or the consolidator does (3 teammates)

**Note on CLAUDE.md §6 (background agent monitoring)**: Team mode spawns in-process teammates via TeamCreate — not background agents writing to run directory. In-process teammates send TeammateIdle notifications on completion — synchronous completion signals. File-activity polling protocol (§8) doesn't apply; TeammateIdle equivalent liveness signal.

Pre-compute before spawning:

```bash
TEAM_PROTOCOL_PATH="$HOME/.claude/TEAM_PROTOCOL.md"
```

**Spawn prompt template:**

```markdown
# Substitute pre-computed values — do not pass raw $(date) expressions or shell vars into spawn prompts
You are an researcher teammate researching: [topic].
Read ~/.claude/TEAM_PROTOCOL.md — use AgentSpeak v2 for inter-agent messages.
Your cluster: [method family N] (e.g., "attention-free architectures" vs "linear attention variants").
Research the top 3 methods in your cluster: comparison table + recommendation given constraints.
Write your full findings (comparison table, analysis, Confidence block) to `.temp/output-research-<teammate-name>-<SPAWN_BRANCH>-<SPAWN_DATE>.md` (substitute pre-computed values from bash block below) using the Write tool.
Report completion with deltaT# HOOK:verify and include: papers=N recommendation="<method>" confidence=0.N file=.temp/output-research-<teammate-name>-<date>.md
Compact Instructions: preserve paper titles, benchmarks, code links. Discard protocol handshakes.
Task tracking: call TaskUpdate(in_progress) when you start your assigned task; call TaskUpdate(completed) when done, before sending your delta message.
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
```

For 3 teammates, spawn consolidator researcher agent: "Read research files at [paths from deltas]. Synthesize into Step 3 unified report structure. Write to `<$REPORT_OUT>` (substitute resolved path from bash block above — not template variable). Return ONLY compact JSON: `{"status":"done","papers":N,"best_method":"<name>","confidence":0.N,"file":"<path>"}`"

TaskUpdate "Print report header" → `in_progress`.

**MANDATORY, not optional narration** — the consolidator's returned JSON is a routing signal only; it is never printed to the user and never satisfies this step. Consolidator wrote full report to `<file>` (from its envelope) but printed nothing itself. Before returning control to SKILL.md's `## Follow-up gate`: (1) Read `<file>` (Read tool); (2) render its `---` header fields as a two-column Markdown table (`Field | Value`, one row per key, file order) per quality-gates.md §Report File Format's Universal terminal-print rule — never print the raw `---`-delimited block; (3) append `→ saved to <file>`; (4) TaskUpdate "Print report header" → `completed` — only after the table has actually appeared in this response, never before. SKILL.md's Follow-up gate must not fire while this task is `pending`/`in_progress`.

**Hook-enforced**: `hooks/enforce-topic-header.js` (PreToolUse on `AskUserQuestion`) denies the Follow-up gate call while `$REPORT_OUT` (sentinel path above) is missing or empty — a consolidator that never wrote its report cannot be papered over with an ad-hoc summary. The hook sees only whether the report exists, not whether the print happened; steps (1)–(4) above remain the check for the print itself.
