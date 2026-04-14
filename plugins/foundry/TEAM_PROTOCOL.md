# Agent Teams Protocol — Borda.ai-home

AgentSpeak v2 compressed inter-agent messaging for Claude Code Agent Teams. Achieves ~60% token savings vs natural language on inter-agent messages. Adapted from [github.com/yuvalsuede/claude-teams-language-protocol](https://github.com/yuvalsuede/claude-teams-language-protocol) (MIT) <!-- attribution-only; not a runtime dependency -->.

- **Rule 1**: Teammate↔teammate uses this protocol. Lead↔human uses normal English.
- **Rule 2**: Declare version at spawn: `alpha PROTO:v2.0 @lead ready`
- **Rule 3**: Two teammates must NEVER edit the same file simultaneously — use file locking.

______________________________________________________________________

## Status Codes

| Code      | Meaning           | Use when                         |
| --------- | ----------------- | -------------------------------- |
| `alpha`   | Starting          | Beginning a task                 |
| `beta`    | In progress       | Working (append %: `beta75`)     |
| `gamma`   | Blocked           | Waiting on another agent or task |
| `delta`   | Done              | Task completed                   |
| `epsilon` | Bug/error         | Reporting an issue               |
| `omega`   | Shutdown          | Wrapping up, final message       |
| `theta`   | Protocol feedback | Proposing a protocol improvement |

## Action Symbols

| Symbol  | Meaning                    |
| ------- | -------------------------- |
| `+`     | Added                      |
| `-`     | Removed                    |
| `~`     | Changed                    |
| `!`     | Broken                     |
| `?`     | Requesting                 |
| `->`    | Depends on / transforms to |
| `>>`    | Unblocks                   |
| `<<`    | Blocked by                 |
| `@`     | Route to / assign          |
| `+lock` | Claim file ownership       |
| `-lock` | Release file ownership     |

## Priority Prefix

| Prefix | Meaning                          |
| ------ | -------------------------------- |
| `!!`   | Urgent — requires lead attention |
| (none) | Normal                           |
| `..`   | FYI only                         |

## Bug Severity

| Code | Meaning                                               |
| ---- | ----------------------------------------------------- |
| `P0` | Critical (security, data loss, crash, blocks release) |
| `P1` | High (broken feature, bad UX)                         |
| `P2` | Medium (logic bug, type mismatch)                     |
| `P3` | Low (style, minor quality)                            |

## Message Format

```
[priority?][status][task?][file?][action][detail]
```

Status code is always first. Task ID (`T#`) always precedes file shortcode.

## Agent Shortcodes

| Code | Agent              | Code | Agent          |
| ---- | ------------------ | ---- | -------------- |
| `SW` | sw-engineer        | `PO` | perf-optimizer |
| `SA` | solution-architect | `AR` | researcher     |
| `QA` | qa-specialist      | `DS` | doc-scribe     |
| `LE` | linting-expert     | `CG` | ci-guardian    |
| `DT` | data-steward       | `WE` | web-explorer   |
| `OM` | shepherd           | `SM` | self-mentor    |

## Project File Shortcodes (`.claude/` config)

| Code | Path                       |
| ---- | -------------------------- |
| `AG` | `.claude/agents/`          |
| `SK` | `.claude/skills/`          |
| `HK` | `.claude/hooks/`           |
| `ST` | `.claude/settings.json`    |
| `CM` | `.claude/CLAUDE.md`        |
| `TP` | `.claude/TEAM_PROTOCOL.md` |
| `MM` | `MEMORY.md` ¹              |
| `RM` | `README.md`                |

¹ `MM` — session-injected from `~/.claude/projects/<slug>/memory/MEMORY.md`; read-only in teammate context (Write tool cannot address it).

For source files, define shortcodes per-team in the spawn prompt (e.g., `SRC=src/, TST=tests/`).

## Examples

```
alpha PROTO:v2.0 @lead ready                      # spawn announcement
alphaT3 +lockSRC starting root cause analysis     # claim task + file
beta50T3 SW ~auth.py narrowing to L140-166        # progress update
deltaT3 SW -lockSRC >>T4 HOOK:verify              # done, unblocks T4
gammaT4 QA <<T3 need root cause before tests      # blocked
!!epsilonP0 auth.py !bypass L140 ?lead            # critical finding
..epsilonP3 TST duplicate fixtures L55,L77        # low-priority finding
epsilonBATCH 3items: P0:1 [auth!bypass L140] P1:1 [api!validate L55] P3:1 [utils!style L12]
omega @lead idle ?nextT                           # done, want more work
omega @lead idle DONE                             # done, wrapping up
theta:compress >>T4-6 shorter than >>T4,T5,T6    # protocol improvement
```

## File Locking

```
+lockSRC,TST    # claim src/ and tests/
-lockSRC        # release
!lockSRC @lead  # conflict — lead resolves
```

## Hook Integration

```
deltaT3 +feature HOOK:verify    # triggers TaskCompleted hook
omega @lead idle ?nextT         # triggers TeammateIdle hook
```

## Task List Updates

Teammates assigned a task via TaskUpdate(owner) **must** update the shared task list:

1. `TaskUpdate(status: "in_progress")` — when starting the assigned task
2. `TaskUpdate(status: "completed")` — when work is done, **before** sending the delta message to @lead
3. `TaskUpdate(status: "completed")` — **before** sending `omega` on shutdown, even if work was cut short; incomplete tasks use status `"cancelled"` instead.

This keeps the task list as a live progress feed for the user. AgentSpeak delta messages alone do not update task status.

## Error Recovery

```
epsilon!retry auth attempt:2/3               # retrying
epsilon!fail auth attempt:3/3 ?lead          # giving up, reassign
@QA T3:reassign (from @SW epsilon!fail)     # lead reassigns
```

## Security in QA

`qa-specialist` automatically includes OWASP Top 10 security checks when the task touches:

- Authentication or authorization code
- Payment flows or financial data
- User PII or sensitive data (storage, transmission, access control)

Report security findings as `P0` (auth bypass, injection, secrets in code) or `P1` (broken access control, missing input validation). No separate security agent is needed — QA owns this scope.

## Result Return Protocol

When a teammate completes an analysis task (review, audit, research):

- Write full findings to the canonical output path — see `.claude/skills/_shared/file-handoff-protocol.md` for the path convention
- Send lead a summary message: `DONE <task-id> | findings=N sev=C/H/M | → <file-path>`
- Lead reads the file only when consolidating the final report — not for every task completion

This keeps inter-agent traffic compact while preserving full findings for the consolidation phase.

## Anti-Patterns (Do NOT do)

- No greetings ("Hi team!"), acknowledgments ("Great work!"), or sign-offs
- No prose descriptions of file changes — use shortcode + action symbol
- No multi-sentence explanations of blocking — use `gammaT4 <<T1 [reason]`
- No separate routing message per agent — batch into `@agent [items]`
- No full conversation history in spawn prompts — include only task-relevant context; unrelated history wastes tokens and pollutes reasoning
