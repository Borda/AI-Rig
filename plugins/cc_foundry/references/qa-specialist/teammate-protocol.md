## Operating as Teammate (Agent Teams)

When spawned as Agent Teams teammate (e.g., via `/develop:fix --team`, `/develop:feature --team` — requires `develop` plugin):

Follow AgentSpeak v2 protocol: run `cat ~/.claude/TEAM_PROTOCOL.md` via Bash (symlinked by `/foundry:setup` — requires `foundry` plugin). If absent, run `cat "${CLAUDE_PLUGIN_ROOT}/TEAM_PROTOCOL.md"`; if the file is unavailable, ask the orchestrator to provide TEAM_PROTOCOL content directly.

**Challenging sw-engineer's API design (in `/develop:feature --team` — requires `develop` plugin)**: when qa-specialist spawned alongside sw-engineer, review proposed API BEFORE implementation starts. Challenge:

- Missing input validation or error cases
- Auth/permission assumptions not explicit in type signature
- Type safety gaps that generate flaky test noise
- Missing edge cases in proposed interface

Report design challenges to lead with epsilon + specific concern. SW adjusts design; QA then writes tests against finalized API.
