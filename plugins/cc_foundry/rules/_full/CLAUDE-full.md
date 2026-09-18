## CLAUDE.src.md — worked detail

Full detail behind the `CLAUDE.src.md` stub (`~/.claude/CLAUDE.md` post-install). The rule itself stays in the stub, always loaded — this file is illustration only, fetched at the trigger point named in the stub section.

### Subagent Strategy — subagent_type telemetry

Telemetry shows `general-purpose` gets picked *despite* the spawn prompt's own lead line naming the correct specialist (e.g. prompt says "foundry:sw-engineer — convert X to jsonargparse", dispatch says `general-purpose` anyway). Likely cause: misreading "Agent Teams: user-invoked only" as covering *any* named specialist rather than only the formal multi-agent Team protocol. §Agent Teams is a separate, narrower gate (model tiering + TEAM_PROTOCOL.md + AgentSpeak v2, user-invoked only) — it does NOT restrict picking a specialist for an ordinary single-agent spawn, ad-hoc or background included. Separately: omitting `subagent_type` also defaults to `general-purpose` (tool-level fallback), skips agent-tracking writes, making the spawn invisible in the 🤖 status segment from dispatch.
