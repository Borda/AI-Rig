# Preflight Helpers

Shared preflight protocols for develop skills. Read + run relevant section(s) based on active flags.

## Codemap + Semble Preflight

Run when `SEMBLE_ENABLED=true`. Codemap availability already validated by `codemap_resolve.py` in flag-parsing phase — no additional check needed when `CODEMAP_ENABLED=true`.

**If `CODEMAP_ENABLED=true`**: no-op — `codemap_resolve.py` confirmed `codemap-py query` on PATH and index present before setting `CODEMAP_ENABLED=true`.

**If `SEMBLE_ENABLED=true`**: verify `mcp__semble__search` in available tools. If not: print `! --semble requested but semble MCP server not configured. Configure: claude mcp add semble -s user -- uvx --from "semble[mcp]" semble` and stop.

## --plan Path Extraction

Run when skill accepts `--plan <path>` flag. Sets `$PLAN_FILE`.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
eval "$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/parse-skill-flags.py" --flags worktree --value-flags plan "$ARGUMENTS")"  # timeout: 5000 — both --plan spellings
PLAN_FILE="$VALUE_PLAN"
if [ -n "$PLAN_FILE" ] && [ ! -f "$PLAN_FILE" ]; then
  echo "! BREAKING — plan file not found: $PLAN_FILE"
  echo "Fix: pass an existing plan path via --plan <path> or --plan=<path>"
  exit 1
fi
# persist so later cross-Bash-call reads (compaction-contract boundaries in feature/fix/refactor) resolve it — shell var is lost between Bash() calls
echo "$PLAN_FILE" > "${TMPDIR:-/tmp}/dev-plan-file-${CSID}"
```

## Team Spawn Template

Spawn prompt template for foundry:sw-engineer teammate spawns. Replace `[ROLE_PHRASE]` and `[FILE_SLUG]` with skill-specific values before inserting.

Output filenames are per-skill contracts — the consumer skill's spawn prompts and monitor/gate expressions are the source of truth; this table mirrors them. Never invent a different shape from the generic `[FILE_SLUG]-[N]-[timestamp]` pattern in the template below — feature's Wave-1 gate and each monitor glob key on these exact names:

| skill | `[ROLE_PHRASE]` | output file(s) |
| -- | -- | -- |
| debug | `[symptom]` | `.temp/develop/[TS]/debug-hypothesis-[N]-[TS].md` (N = 1..3) |
| feature | `[feature description]` | `.temp/develop/[TS]/feature-[agent-name]-[TS].md` (agent-name = sw-engineer, qa-specialist, doc-scribe) |
| fix | `[bug description]` | `[RUN_DIR]/fix-hypothesis-[N]-[TS].md` (N = A, B — hypothesis letters serve as `[N]`) |
| refactor | `[refactor goal]` | `[RUN_DIR]/refactor-[agent-name].md` (agent-name = qa-specialist, sw-engineer; no timestamp — monitor globs `-type f`) |

```
You are a foundry:sw-engineer teammate working on: [ROLE_PHRASE].
Read ${HOME}/.claude/TEAM_PROTOCOL.md — use AgentSpeak v2 for inter-agent messages.
Your hypothesis: [hypothesis N]. Investigate ONLY this root cause.
Report findings to @lead using deltaT# or epsilonT# codes.
Compact Instructions: preserve file paths, errors, line numbers. Discard verbose tool output.
Task tracking: do NOT call TaskCreate or TaskUpdate — the lead owns all task state. Signal your completion in your final delta message: "Status: complete | blocked — <reason>".
Write your full analysis to .temp/develop/[timestamp]/[FILE_SLUG]-[N]-[timestamp].md using the Write tool (use the run-dir timestamp provided in your spawn context, not a new timestamp). Return ONLY compact JSON: {"status":"done","file":"<path>","findings":N,"confidence":0.N,"summary":"<one-line description of what found/done>"}.
```
