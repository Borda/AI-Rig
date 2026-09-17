#!/usr/bin/env node
// task-log.js — multi-event lifecycle hook
//
// PURPOSE
//   Central nervous system for session state tracking.  It handles seven distinct
//   Claude Code hook events and maintains four categories of runtime state that
//   other hooks (statusline.js) read to render the live status line:
//
//     agents/   — which subagents are currently running
//     codex/    — which codex plugin sessions are active
//     skills/   — which Skill() calls are currently in flight
//     tools/    — which tool types fired in the current turn (for the 🔧 line)
//     timings/  — in-flight start markers for per-tool wall-clock timing
//
//   It also appends to append-only audit logs so you have a full history of every
//   agent launch, skill invocation, and context compaction across sessions.
//
// HOW IT WORKS
//   1. Parse stdin JSON for hook_event_name, tool_name, tool_input, agent_id, agent_type, session_id
//   2. Resolve per-session temp dir at /tmp/claude-state-<session_id>/ for ephemeral state
//   3. PreToolUse: log Task/Agent/Skill invocations to invocations.jsonl; open a codex session
//      file in state/codex/ for codex:* skills; increment per-tool-type counter in state/tools/;
//      write a timing start marker (tool name, args summary, timestamp) to state/timings/
//   4. PostToolUse: close the codex session file when Skill(codex:*) completes; read the
//      timing start marker, compute duration_ms, append to timings.jsonl, delete marker
//   5. PostToolUseFailure: same as PostToolUse timing path but records status "error"
//   6. SubagentStart: write agent metadata (type, model, color, timestamp) to state/agents/<id>.json
//   7. SubagentStop: delete the per-agent file; append completion entry (with last_assistant_message)
//      to invocations.jsonl; also clean up codex tracking if the agent was a codex:* type
//   8. PreCompact: scan transcript tail for Write/Edit tool_use blocks and write modified
//      file paths to state/session-context.md as a compaction breadcrumb; append the verbatim
//      .temp/state/skill-contract.md (if present) so a per-skill compaction contract survives
//   9. UserPromptSubmit: write a queue marker to state/queue/ (deduplicated via 500ms lock)
//  10. Stop: clear state/tools/ and queue markers (deduplicated); agents left intact;
//      delete orphaned timing start markers and accumulated lock-Pre-*.lock dedup files
//  11. SessionEnd: delete own /tmp/claude-state-<session_id>/ subtree; clean other sessions'
//      stale dirs older than 24h (orphaned by crashes); prune stale git worktrees
//
// HOOK EVENT RESPONSIBILITIES
//
//   PreToolUse
//     • Logs Task/Agent and Skill invocations to invocations.jsonl.
//     • Opens a codex session file when Skill(codex:*) or Agent(codex:*) starts
//       (keyed by tool_use_id so concurrent sessions don't collide).
//     • Writes/increments a per-tool-type file in state/tools/ for the 🔧 display.
//       Uses a fixed 30s window (since = window start, not last call) so count resets
//       after a >30s gap. Agent and Task calls excluded — tracked via SubagentStart/Stop.
//     • For Agent() calls: writes state/pending/<tool_use_id>.json with subagent_type so
//       SubagentStart can resolve agent_type when its payload omits it (Agent() vs Task()).
//     • Refreshes last_active on a worktree subagent's own tool events (join via cwd →
//       agent_id), so a still-working long-running agent isn't reaped by the staleness filter.
//
//   PostToolUse
//     • Closes the codex session file when Skill(codex:*) or Agent(codex:*) completes,
//       so the 🤖 counter drops back to zero immediately after the run finishes.
//     • Reads the timing start marker written by PreToolUse, computes wall-clock duration,
//       appends a record to timings.jsonl (status "ok"), and deletes the marker.
//
//   PostToolUseFailure
//     • Same timing path as PostToolUse but records status "error" so failed tool calls
//       are distinguishable in timing analysis without blocking normal flow.
//
//   SubagentStart
//     • Resolves agent_type from the state/pending/<tool_use_id>.json cache written by
//       PreToolUse when the SubagentStart payload omits agent_type (Agent() tool spawns).
//     • Creates state/agents/<id>.json with the resolved type, model, color (read from
//       the agent's frontmatter), and start timestamp.  One file per agent ID means
//       concurrent agents never overwrite each other (no read-modify-write race).
//
//   SubagentStop
//     • Deletes the per-agent file so the 🕵 counter decrements correctly.
//     • Appends a completion entry (with last assistant message) to invocations.jsonl
//       for post-mortem debugging.
//
//   PreCompact
//     • Scans the tail of the transcript for Write/Edit tool_use blocks, extracts
//       modified file paths, and writes state/session-context.md — a lightweight
//       breadcrumb that survives context compaction and is re-read at session resume.
//     • If .temp/state/skill-contract.md exists, appends its content verbatim under a
//       "## Skill Compaction Contract" section so a skill's per-compaction contract
//       survives unsummarized. Observational — a missing/unreadable contract is
//       swallowed and never blocks compaction.
//
//   UserPromptSubmit
//     • Clears state/tools/ so the 🔧 line resets immediately at new-prompt start —
//       display resets without waiting for the first PostToolUse of the new turn.
//       (Stop also clears; this is a backup and ensures visible reset at prompt time.)
//     • Writes a marker file to state/queue/ so statusline.js shows 💬 on Line 1
//       while Claude is processing the current turn. (UserPromptSubmit fires when Claude
//       begins handling the message — not when the user presses Enter — so the marker
//       represents "currently processing", not a queued-but-unstarted message.)
//     • Detects user-invoked slash commands (data.prompt starting with /) and writes a
//       skills/<id>.json entry so statusline.js shows ⚡ while the skill executes.
//       User slash commands are system-injected (not Skill() tool calls), so
//       PreToolUse(Skill) never fires for them — this is the only tracking path.
//
//   Stop  (end of Claude's turn)
//     • Clears state/tools/ so the 🔧 line resets between turns.
//     • Removes ALL processing markers from state/queue/ so the 💬 badge disappears.
//       Clears all (not just oldest) to handle interrupted turns where Stop didn't fire
//       and stale markers accumulated. Agents are intentionally NOT cleared here.
//     • Deletes any remaining files in state/timings/ (orphaned start markers from tool
//       calls that never received a PostToolUse/PostToolUseFailure event).
//     • Deletes lock-Pre-*.lock dedup files from tmpDir root (accumulate across turns;
//       functionally inert after 500ms TTL but never otherwise cleaned).
//     • Deletes remaining state/skills/ entries written by UserPromptSubmit (slash cmds).
//       PostToolUse already deleted Skill() tool call entries; Stop cleans up the rest.
//
//   SessionEnd  (full session teardown)
//     • Clears state/agents/, state/tools/, state/codex/, and state/queue/ completely.
//     • Scans /tmp and removes claude-state-* dirs from OTHER sessions older than 24h
//       (orphaned by crashed sessions that never fired their own SessionEnd).
//     • Runs `git worktree prune` to remove stale worktree refs.
//     • Removes any worktrees under .claude/worktrees/ older than 2 hours
//       (orphaned by crashed agents or interrupted sessions).
//
// STATE FILES
//   ~/.claude/logs/invocations.jsonl    — append-only audit log (agents + skills); global across all projects; includes project field
//   ~/.claude/logs/timings.jsonl        — append-only per-tool timing log {ts, project, tool, args, tool_use_id, session_id, duration_ms, status, model}
//   /tmp/claude-state-<session_id>/agents/<id>.json     — one file per active subagent
//   /tmp/claude-state-<session_id>/codex/<id>.json      — one file per active codex plugin session
//   /tmp/claude-state-<session_id>/skills/<id>.json     — one file per in-flight Skill() call
//   /tmp/claude-state-<session_id>/tools/<tool>.json    — one file per tool type, current turn only
//   /tmp/claude-state-<session_id>/queue/<ts>.json      — one file per pending user input (cleared on Stop)
//   /tmp/claude-state-<session_id>/pending/<id>.json    — one file per in-flight Agent() call (consumed by SubagentStart; cleaned at SessionEnd)
//   /tmp/claude-state-<session_id>/timings/<id>.json    — in-flight timing start marker (written by PreToolUse, consumed by PostToolUse/Failure; orphans cleared at Stop)
//   .claude/state/session-context.md    — modified-files breadcrumb for compaction
//
// SESSION ISOLATION
//   Ephemeral state (agents, tools, codex, queue, dedup locks) lives in a per-session temp
//   directory keyed by the session_id from the hook payload.  This prevents cross-session
//   contamination when multiple Claude Code instances run simultaneously — each session reads
//   and writes only its own /tmp/claude-state-<session_id>/ subtree.
//   Persistent state (audit logs, session-context.md) remains in .claude/ (project-scoped).
//
// EXIT CODES
//   0  Always — logging hook; must never block or crash Claude.

const fs = require("fs");
const os = require("os");
const path = require("path");
const { execFileSync, execSync } = require("child_process");

function getSentinelDir() {
  return process.platform === "win32" ? os.tmpdir() : "/tmp";
}

let raw = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (d) => (raw += d));
process.stdin.on("end", () => {
  try {
    const data = JSON.parse(raw);
    const { hook_event_name, tool_name, tool_input, agent_id, agent_type, session_id } = data;

    // Resolve workspace root from CWD (hooks run with CWD = project root)
    const root = process.cwd();
    const stateDir = path.join(root, ".claude", "state");
    // Per-skill compaction contract lives under .temp/state (not .claude/) so the active skill
    // can write it with the Write tool without tripping Claude Code's sensitive-file gate on
    // tool-call writes under .claude/. This hook only READS it; session-context.md stays in
    // stateDir (.claude/state) since it's written here via Node fs, which bypasses that gate.
    const contractDir = path.join(root, ".temp", "state");
    // Global logs dir — audit logs accumulate across all projects and sessions
    const globalLogsDir = path.join(os.homedir(), ".claude", "logs");
    const logFile = path.join(globalLogsDir, "invocations.jsonl");
    const timingsFile = path.join(globalLogsDir, "timings.jsonl");
    // Project slug used to tag log entries so they can be filtered by project
    const projectSlug = root.replace(/[/.]/g, "-");

    // Ephemeral per-session state lives in /tmp scoped by session_id.
    // This prevents cross-session contamination when multiple Claude Code instances run
    // concurrently — each session owns its own subtree and cannot see another session's state.
    // Fallback to 'default' if session_id is missing (older Claude Code versions).
    const sid = (session_id || "default").replace(/[^a-zA-Z0-9_-]/g, "_");
    const tmpDir = path.join(getSentinelDir(), `claude-state-${sid}`);
    const agentsDir = path.join(tmpDir, "agents");
    const toolsDir = path.join(tmpDir, "tools");
    const codexDir = path.join(tmpDir, "codex");
    const queueDir = path.join(tmpDir, "queue");
    const pendingDir = path.join(tmpDir, "pending");
    const timingsDir = path.join(tmpDir, "timings");
    const skillsDir = path.join(tmpDir, "skills");

    const ts = new Date().toISOString();

    if (hook_event_name === "PreToolUse") {
      if (tool_name === "Task" || tool_name === "Agent") {
        const agentType = tool_input?.subagent_type || "unknown";
        const desc = tool_input?.description || "";
        const prompt = (tool_input?.prompt || "").slice(0, 200);
        appendLog(logFile, globalLogsDir, {
          ts,
          project: projectSlug,
          event: "started",
          tool: "Task",
          agent: agentType,
          desc,
          prompt,
        });
        // Track Agent() calls directly via PreToolUse — SubagentStart fires sporadically and cannot be
        // relied on to appear in the statusline. Write to agents/ immediately; PostToolUse deletes it.
        // Also keep pending/ cache so SubagentStart (when it does fire) can consume it without double-writing.
        if (tool_name === "Agent" && tool_input?.subagent_type && data.tool_use_id) {
          try {
            const agentType = tool_input.subagent_type;
            const info = readAgentInfo(root, agentType);
            fs.mkdirSync(agentsDir, { recursive: true });
            fs.writeFileSync(
              path.join(agentsDir, `${data.tool_use_id}.json`),
              JSON.stringify({
                id: data.tool_use_id,
                type: agentType,
                model: info.model,
                color: info.color,
                since: ts,
              }),
            );
            fs.mkdirSync(pendingDir, { recursive: true });
            // Persist run_in_background here, where tool_input reliably carries it. PostToolUse reads
            // this flag to decide whether the Agent() call returned at dispatch (background) or at
            // completion (foreground) — its own tool_input may omit run_in_background.
            // Persist name too: a named Agent()'s SubagentStart payload carries the *name* in its
            // agent_type field, not subagent_type (confirmed live: dispatched as
            // type="foundry:challenger", SubagentStart's agent_type arrived as the assigned name) — the
            // tool_use_id-less matching fallback below needs to match on either.
            fs.writeFileSync(
              path.join(pendingDir, `${data.tool_use_id}.json`),
              JSON.stringify({
                type: agentType,
                name: tool_input?.name || null,
                ts,
                bg: tool_input?.run_in_background === true,
              }),
            );
          } catch (_) {}
        }
      } else if (tool_name === "Skill") {
        const skill = tool_input?.skill || "unknown";
        const args = tool_input?.args || "";
        appendLog(logFile, globalLogsDir, { ts, project: projectSlug, event: "invoked", tool: "Skill", skill, args });
        // Track bridge-backed Codex skills for statusline display (tool_use_id is the stable key).
        if (skill && skill.startsWith("bridge:") && data.tool_use_id) {
          try {
            fs.mkdirSync(codexDir, { recursive: true });
            const shortName = skill.slice("bridge:".length);
            fs.writeFileSync(
              path.join(codexDir, `${data.tool_use_id}.json`),
              JSON.stringify({ id: data.tool_use_id, since: ts, type: shortName }),
            );
          } catch (_) {}
        }
        // Track active skills for statusline display (keyed by tool_use_id)
        if (data.tool_use_id) {
          try {
            fs.mkdirSync(skillsDir, { recursive: true });
            fs.writeFileSync(
              path.join(skillsDir, `${data.tool_use_id}.json`),
              JSON.stringify({ id: data.tool_use_id, skill, since: ts }),
            );
          } catch (_) {}
        }
      }
      // Track all tool calls for statusline tool-activity line (count per type within 30s fixed window).
      // Fixed window: since = start of window (not last call) → count resets after 30s gap.
      // Exclude Agent/Task — those are tracked separately via PreToolUse/PostToolUse → state/agents/
      if (tool_name && tool_name !== "Agent" && tool_name !== "Task") {
        try {
          fs.mkdirSync(toolsDir, { recursive: true });
          const toolFile = path.join(toolsDir, `${tool_name}.json`);
          let count = 1;
          let windowStart = ts; // fixed window: since = start of current 30s window, not last call
          try {
            const existing = JSON.parse(fs.readFileSync(toolFile, "utf8"));
            const windowAge = Date.now() - new Date(existing.since || 0).getTime();
            if (windowAge <= 30000) {
              count = (existing.count || 0) + 1;
              windowStart = existing.since; // preserve original window start so window is fixed, not sliding
            }
            // windowAge > 30s: reset — count stays 1, windowStart = now (new window)
          } catch (_) {}
          fs.writeFileSync(toolFile, JSON.stringify({ tool: tool_name, since: windowStart, count }));
        } catch (_) {}
      }
      // Refresh a running subagent's liveness on its own tool activity, so a still-working
      // long-running agent is never reaped by the statusline's staleness filter. An agent file's
      // `since` is dispatch time and is never updated once running, so after its cutoff the agent
      // vanishes even while working. Tool events carry no agent_id (CC hook schema: only
      // session_id/tool_name/tool_input/tool_result/tool_use_id/cwd), so the one reliable
      // per-agent join is `cwd` for worktree-isolated agents: the worktree dir is
      // .claude/worktrees/agent-<agent_id>/ and the agent state file is keyed by that same
      // agent_id — renameAgentFile (SubagentStart) re-keys the PreToolUse-time tool_use_id record to
      // agent_id before the worktree agent's own tool calls land here, so this lookup actually finds
      // it. Non-worktree agents share the parent's cwd (indistinguishable from the orchestrator's
      // own calls) and stay covered by the (longer) staleness backstop instead.
      touchAgentLastActive(data.cwd, agentsDir);
      // Write timing start marker. PostToolUse reads it to compute wall-clock duration.
      // isDuplicateEvent dedup prevents double-write when both project and home settings.json fire.
      if (data.tool_use_id && !isDuplicateEvent(`Pre-${data.tool_use_id}`, tmpDir)) {
        try {
          fs.mkdirSync(timingsDir, { recursive: true });
          fs.writeFileSync(
            path.join(timingsDir, `${data.tool_use_id}.json`),
            JSON.stringify({
              tool: tool_name,
              start: Date.now(),
              model: data.model || null,
              args: summarizeArgs(tool_name, tool_input),
            }),
          );
        } catch (_) {}
      }
    } else if (hook_event_name === "PostToolUse") {
      // Remove bridge-backed Codex session tracking when the Skill call completes.
      if (data.tool_use_id) {
        const isCodexSkill = tool_name === "Skill" && tool_input?.skill?.startsWith("bridge:");
        if (isCodexSkill) {
          try {
            fs.unlinkSync(path.join(codexDir, `${data.tool_use_id}.json`));
          } catch (_) {}
        }
        // Foreground Agent() calls complete when PostToolUse fires → delete the PreToolUse entry so
        // the 🤖 counter drops promptly. Background agents (run_in_background) return at *dispatch*,
        // so PostToolUse fires while the agent is still running — keep agents/ + pending/ so the agent
        // stays visible (and a later SubagentStart, if it fires, consumes pending/ without writing a
        // duplicate; renameAgentFile re-keys it from tool_use_id to agent_id there). Background-ness
        // is read from pending/ (captured at PreToolUse) since this event's tool_input may omit
        // run_in_background.
        // A finished background agent is reaped by SubagentStop as soon as it fires — its unlink
        // (agentsDir/<agent_id>.json) now matches because SubagentStart already re-keyed the record
        // to agent_id. The statusline's staleness filter is a backstop for the case SubagentStop
        // never fires at all (crash, session drop), not the primary reaper. A *still-running*
        // worktree agent keeps its last_active fresh via touchAgentLastActive (PreToolUse).
        if (tool_name === "Agent") {
          let isBackground = tool_input?.run_in_background === true;
          try {
            const p = JSON.parse(fs.readFileSync(path.join(pendingDir, `${data.tool_use_id}.json`), "utf8"));
            if (p.bg === true) isBackground = true;
          } catch (_) {}
          if (!isBackground) {
            try {
              fs.unlinkSync(path.join(agentsDir, `${data.tool_use_id}.json`));
            } catch (_) {}
            try {
              fs.unlinkSync(path.join(pendingDir, `${data.tool_use_id}.json`));
            } catch (_) {}
          }
        }
        // Remove skill tracking entry when Skill() call completes
        if (tool_name === "Skill") {
          try {
            fs.unlinkSync(path.join(skillsDir, `${data.tool_use_id}.json`));
          } catch (_) {}
        }
        // Complete timing: read start marker, compute duration, append to timings.jsonl, delete marker.
        // Natural dedup: first fire reads+deletes the marker; second fire finds it gone and exits silently.
        recordTiming(data.tool_use_id, tool_name, session_id, "ok", timingsDir, timingsFile, globalLogsDir, data.model);
      }
    } else if (hook_event_name === "PostToolUseFailure") {
      // Same as PostToolUse timing but marks status "error".
      if (data.tool_use_id) {
        // Remove skill tracking entry on failure too
        if (tool_name === "Skill") {
          try {
            fs.unlinkSync(path.join(skillsDir, `${data.tool_use_id}.json`));
          } catch (_) {}
        }
        recordTiming(
          data.tool_use_id,
          tool_name,
          session_id,
          "error",
          timingsDir,
          timingsFile,
          globalLogsDir,
          data.model,
        );
      }
    } else if (hook_event_name === "SubagentStart") {
      // PreToolUse already wrote to agents/ for Agent() calls. SubagentStart fires sporadically —
      // consume the pending entry (if present) so it doesn't linger, but skip writing agents/ again.
      // For agents without a pending entry (e.g. team-mode agents), write as before.
      try {
        const id = agent_id || ts;
        let resolvedType = agent_type;
        let preToolUseTracked = false;
        if (data.tool_use_id) {
          const pendingFile = path.join(pendingDir, `${data.tool_use_id}.json`);
          try {
            const p = JSON.parse(fs.readFileSync(pendingFile, "utf8"));
            resolvedType = resolvedType || p.type;
            fs.unlinkSync(pendingFile); // consume — PreToolUse already wrote to agents/
            preToolUseTracked = true;
            renameAgentFile(agentsDir, data.tool_use_id, id);
          } catch (_) {}
        } else if (agent_type) {
          // SubagentStart payload omits tool_use_id — scan pending/ for most-recent entry
          // matching this agent_type, consume it. PreToolUse already wrote agents/<tool_use_id>.json,
          // so skipping the second write below avoids double-counting in statusline.
          // Match on type OR name: a *named* Agent()'s SubagentStart carries the assigned name in
          // agent_type, not its subagent_type — matching only on p.type would miss it, leaving
          // agents/<tool_use_id>.json unrenamed (SubagentStop's later unlink then never finds it,
          // reopening the exact leak this function exists to close).
          try {
            const files = fs
              .readdirSync(pendingDir)
              .map((f) => {
                try {
                  return { f, p: JSON.parse(fs.readFileSync(path.join(pendingDir, f), "utf8")) };
                } catch (_) {
                  return null;
                }
              })
              .filter((x) => x && x.p && (x.p.type === agent_type || (x.p.name && x.p.name === agent_type)))
              .sort((a, b) => (b.p.ts > a.p.ts ? 1 : -1));
            if (files.length > 0) {
              fs.unlinkSync(path.join(pendingDir, files[0].f)); // consume most-recent match
              preToolUseTracked = true;
              renameAgentFile(agentsDir, files[0].f.replace(/\.json$/, ""), id);
            }
          } catch (_) {}
        }
        if (!preToolUseTracked) {
          // No PreToolUse entry — write agents/ entry for this agent (e.g. team-mode agent)
          fs.mkdirSync(agentsDir, { recursive: true });
          const info = readAgentInfo(root, resolvedType);
          const model = data.model || info.model;
          const color = info.color;
          fs.writeFileSync(
            path.join(agentsDir, `${id}.json`),
            JSON.stringify({ id, type: resolvedType || "unknown", model, color, since: ts }),
          );
        }
        // Bridge-backed Codex execution uses Skill calls, not Agent events.
      } catch (_) {}
    } else if (hook_event_name === "SubagentStop") {
      // Delete the per-agent file; read stored type first for accurate completion logging
      const id = agent_id || ts;
      let loggedType = agent_type;
      try {
        const agentFile = path.join(agentsDir, `${id}.json`);
        const stored = JSON.parse(fs.readFileSync(agentFile, "utf8"));
        if (stored.type && stored.type !== "unknown") loggedType = stored.type;
      } catch (_) {}
      try {
        fs.unlinkSync(path.join(agentsDir, `${id}.json`));
        // Also clean up any bridge skill entry keyed by this lifecycle id.
        try {
          fs.unlinkSync(path.join(codexDir, `${id}.json`));
        } catch (_) {}
      } catch (_) {}
      // Capture last assistant message (up to 500 chars) for post-mortem debugging
      const lastMsg = (data.last_assistant_message || "").slice(0, 500) || undefined;
      appendLog(logFile, globalLogsDir, {
        ts,
        project: projectSlug,
        event: "completed",
        tool: "Task",
        agent: loggedType || "unknown",
        ...(lastMsg && { last_msg: lastMsg }),
      });
    } else if (hook_event_name === "PreCompact") {
      // Extract modified files from transcript and write context snapshot
      const transcriptPath = data.transcript_path;
      if (transcriptPath) {
        try {
          // Read last 50KB of transcript to find Write/Edit tool_use blocks
          const stats = fs.statSync(transcriptPath);
          const readSize = Math.min(50 * 1024, stats.size);
          const buf = Buffer.alloc(readSize);
          const fd = fs.openSync(transcriptPath, "r");
          try {
            fs.readSync(fd, buf, 0, readSize, stats.size - readSize);
          } finally {
            fs.closeSync(fd);
          }
          const transcriptTail = buf.toString("utf8");
          // Extract file paths from Write and Edit tool_use blocks
          const filePattern = /"file_path"\s*:\s*"([^"]+)"/g;
          const toolPattern = /"name"\s*:\s*"(Write|Edit)"/g;
          const files = new Set();
          // Find tool_use blocks for Write/Edit and extract file_path values
          const blocks = transcriptTail.split(/"type"\s*:\s*"tool_use"/);
          for (const block of blocks) {
            if (toolPattern.test(block)) {
              let m;
              while ((m = filePattern.exec(block)) !== null) {
                files.add(m[1]);
              }
            }
            // Reset regex state
            toolPattern.lastIndex = 0;
            filePattern.lastIndex = 0;
          }
          // Write session-context.md
          fs.mkdirSync(stateDir, { recursive: true });
          const lines = ["# Session Context (auto-generated)", "## Files Modified This Session"];
          if (files.size > 0) {
            for (const f of files) lines.push(`- ${f}`);
          } else {
            lines.push("- (none detected)");
          }
          // Preserve a per-skill compaction contract verbatim, if one was staged.
          // Observational only — a missing/unreadable contract must never block compaction.
          try {
            const contract = fs.readFileSync(path.join(contractDir, "skill-contract.md"), "utf8").trim();
            if (contract) {
              lines.push("");
              lines.push("## Skill Compaction Contract");
              lines.push(contract);
            }
          } catch (_) {}
          fs.writeFileSync(path.join(stateDir, "session-context.md"), lines.join("\n") + "\n");
        } catch (_) {}
      }
    } else if (hook_event_name === "UserPromptSubmit") {
      // Deduplication lock — project and home settings.json both register this hook.
      // Guard: if a lock for UserPromptSubmit exists and is < 500ms old, skip (duplicate fire).
      if (isDuplicateEvent("UserPromptSubmit", tmpDir)) process.exit(0);
      // Clear tool activity from previous turn — resets display immediately at new prompt
      // without waiting for the first PostToolUse of the new turn. Stop also clears; this is backup.
      try {
        const tFiles = fs.readdirSync(toolsDir);
        for (const f of tFiles) {
          try {
            fs.unlinkSync(path.join(toolsDir, f));
          } catch (_) {}
        }
      } catch (_) {}
      // Write a processing marker so statusline shows 💬 while Claude handles this turn.
      // Cleared on Stop (turn complete). SessionEnd removes any crash remnants via tmpDir wipe.
      try {
        fs.mkdirSync(queueDir, { recursive: true });
        const id = ts.replace(/[:.]/g, "-");
        fs.writeFileSync(path.join(queueDir, `${id}.json`), JSON.stringify({ since: ts }));
      } catch (_) {}
      // Detect user-invoked slash command — write current-skill.json for statusline ⚡ display.
      // User slash commands are system injections; PreToolUse(Skill) fires too but skills/*.json
      // is wiped by Stop at every turn end. current-skill.json persists across turns so ⚡
      // stays visible during multi-turn skill execution (e.g. /oss:resolve, /develop:feature).
      // Cleared by the next UserPromptSubmit (new command) or SessionEnd (full teardown).
      // Claude Code wraps slash commands in XML: <command-name>/foo</command-name>.
      const BUILTIN_CMDS = new Set([
        "clear",
        "exit",
        "help",
        "fast",
        "slow",
        "compact",
        "memory",
        "config",
        "mcp",
        "status",
        "permissions",
        "cost",
        "init",
        "login",
        "logout",
        "resume",
        "reset",
        "load",
        "doctor",
        "update",
        "vim",
        "pr_comments",
        "approve",
        "bug",
      ]);
      const rawPrompt = (data.prompt || data.user_message || "").trim();
      // Match XML-wrapped form (newer CC injects <command-name>/foo</command-name> into hook payload)
      // or raw /foo form (current CC sends raw user text in data.prompt at UserPromptSubmit time).
      const slashMatch = rawPrompt.match(/<command-name>\s*\/([^\s<]+)/) || rawPrompt.match(/^\/([^\s]+)/);
      if (slashMatch && !BUILTIN_CMDS.has(slashMatch[1])) {
        try {
          fs.mkdirSync(tmpDir, { recursive: true });
          fs.writeFileSync(
            path.join(tmpDir, "current-skill.json"),
            JSON.stringify({ skill: slashMatch[1], since: ts }),
          );
        } catch (_) {}
      } else {
        // Non-skill message — clear current-skill so ⚡ resets to "none" for regular prompts
        try {
          fs.unlinkSync(path.join(tmpDir, "current-skill.json"));
        } catch (_) {}
      }
    } else if (hook_event_name === "Stop") {
      // Deduplication lock — project and home settings.json both register this hook.
      // Guard: if a lock for Stop exists and is < 500ms old, skip (duplicate fire).
      // Without this, double-fire deletes two markers per turn — incorrectly consuming
      // genuinely queued messages when the user sends while Claude is processing.
      if (isDuplicateEvent("Stop", tmpDir)) process.exit(0);
      // End of turn — clear tool activity and queue markers (both are per-turn)
      // Agents intentionally NOT cleared — subagents can still be running across turns
      try {
        const files = fs.readdirSync(toolsDir);
        for (const f of files) {
          try {
            fs.unlinkSync(path.join(toolsDir, f));
          } catch (_) {}
        }
      } catch (_) {}
      try {
        // Delete ALL processing markers on Stop — ensures the badge always clears when
        // Claude goes idle. Deleting only the oldest left stale files when turns were
        // interrupted (no Stop fired) or when rapid messages accumulated multiple markers.
        const qFiles = fs.readdirSync(queueDir);
        for (const f of qFiles) {
          try {
            fs.unlinkSync(path.join(queueDir, f));
          } catch (_) {}
        }
      } catch (_) {}
      // Clear orphaned timing start markers — any marker left at Stop means PostToolUse never
      // fired for that tool call (crashed/interrupted). Prevents stale markers from corrupting
      // future timing if tool_use_ids were ever reused.
      try {
        const tFiles = fs.readdirSync(timingsDir);
        for (const f of tFiles) {
          try {
            fs.unlinkSync(path.join(timingsDir, f));
          } catch (_) {}
        }
      } catch (_) {}
      // Clean dedup lock files that accumulate in tmpDir root across turns.
      try {
        for (const f of fs.readdirSync(tmpDir)) {
          if (f.startsWith("lock-Pre-") && f.endsWith(".lock")) {
            try {
              fs.unlinkSync(path.join(tmpDir, f));
            } catch (_) {}
          }
        }
      } catch (_) {}
      // Clean orphaned skill entries — PostToolUse handles normal Skill() completions;
      // Stop catches any left over from interrupted/crashed turns (tool_use_id entries only).
      // current-skill.json is intentionally NOT cleared here — it persists across turns.
      try {
        const sFiles = fs.readdirSync(skillsDir);
        for (const f of sFiles) {
          try {
            fs.unlinkSync(path.join(skillsDir, f));
          } catch (_) {}
        }
      } catch (_) {}
    } else if (hook_event_name === "TaskCreated") {
      // Log task creation for audit trail. Payload fields vary by Claude Code version — safe fallbacks.
      const subject = data.subject || data.task?.subject || "";
      const taskId = data.task_id || data.id || data.task?.id || "";
      appendLog(logFile, globalLogsDir, { ts, project: projectSlug, event: "task_created", task_id: taskId, subject });
    } else if (hook_event_name === "SessionEnd") {
      // Full session teardown — delete the entire session-scoped temp directory.
      // All ephemeral state (agents, tools, codex, queue, dedup locks) lives there.
      try {
        fs.rmSync(tmpDir, { recursive: true, force: true });
      } catch (_) {}
      // Remove stale tmpDirs from other sessions that crashed without firing SessionEnd.
      // Only delete dirs older than 24h; skip current session (already removed above).
      // Guard assumption: session_id in SessionEnd payload is consistent with other events.
      // The ownDirName is precomputed from tmpDir (not re-derived from sid) so guard and
      // the rmSync target above are always in sync.
      const ownDirName = path.basename(tmpDir); // "claude-state-<sid>"
      try {
        const cutoff = Date.now() - 24 * 60 * 60 * 1000;
        for (const entry of fs.readdirSync(getSentinelDir())) {
          if (!entry.startsWith("claude-state-") || entry === ownDirName) continue;
          const p = path.join(getSentinelDir(), entry);
          try {
            const stat = fs.statSync(p);
            if (!stat.isDirectory()) continue;
            // Use lock-Stop.lock mtime as last-activity indicator — written by isDuplicateEvent
            // on every Stop event (end of each user turn). More reliable than dir mtime, which
            // only updates when files are added/removed at top-level (not inside subdirs).
            // Fallback to dir mtime if lock file absent (new session or crashed before first turn).
            let lastActivity = stat.mtimeMs;
            try {
              lastActivity = fs.statSync(path.join(p, "lock-Stop.lock")).mtimeMs;
            } catch (_) {}
            if (lastActivity < cutoff) {
              fs.rmSync(p, { recursive: true, force: true });
            }
          } catch (_) {}
        }
      } catch (_) {}
      // Prune stale worktrees (orphaned by crashed agents or interrupted sessions)
      try {
        execFileSync("git", ["worktree", "prune"], { cwd: root, timeout: 5000, stdio: "ignore" });
      } catch (_) {}
      // Clean stale worktrees from .claude/worktrees/ (older than 2h)
      const worktreesDir = path.join(root, ".claude", "worktrees");
      try {
        const entries = fs.readdirSync(worktreesDir);
        const cutoff = Date.now() - 2 * 60 * 60 * 1000;
        for (const entry of entries) {
          const p = path.join(worktreesDir, entry);
          const stat = fs.statSync(p);
          if (stat.isDirectory() && stat.mtimeMs < cutoff) {
            execFileSync("git", ["worktree", "remove", "--force", p], {
              cwd: root,
              timeout: 10000,
              stdio: "ignore",
            });
          }
        }
      } catch (_) {}
      // Artifact TTL: prune skill run dirs / temp files older than 30 days (rules/foundry-config.md
      // §Cleanup Hook). Best-effort shell find; portable BSD/GNU flags. Completed runs are keyed on
      // result.jsonl mtime (incomplete runs lack it and are preserved for post-mortem); loose
      // temp/cache/blueprint files are keyed on their own mtime. Review dirs: two shapes coexist —
      // legacy flat .reports/review/<timestamp>/ (still produced by /develop:review) ages by its own
      // mtime; .reports/review/pr-<N>/run-<NNN>/ (oss lineage) ages per run-<NNN>, not per pr-<N>, so
      // an active PR's older runs still expire even while new ones keep landing; the last empty
      // pr-<N> parent is swept once its final run has aged out.
      try {
        execSync(
          [
            "find .reports/calibrate .reports/resolve .reports/audit .reports/analyse .experiments .developments -maxdepth 2 -name result.jsonl -mtime +30 2>/dev/null | xargs -r dirname 2>/dev/null | xargs -r rm -rf 2>/dev/null",
            "find .reports/review -mindepth 1 -maxdepth 1 -type d ! -name 'pr-*' -mtime +30 2>/dev/null | xargs -r rm -rf 2>/dev/null",
            "find .reports/review -mindepth 2 -maxdepth 2 -type d -name 'run-*' -mtime +30 2>/dev/null | xargs -r rm -rf 2>/dev/null",
            "find .reports/review -mindepth 1 -maxdepth 1 -type d -name 'pr-*' -empty -delete 2>/dev/null",
            "find .plans/blueprint .cache .temp -type f -mtime +30 2>/dev/null | xargs -r rm -f 2>/dev/null",
            "find .temp -mindepth 2 -maxdepth 2 -type d -empty -delete 2>/dev/null",
          ].join("; "),
          { cwd: root, timeout: 15000, stdio: "ignore", shell: "/bin/sh" },
        );
      } catch (_) {}
    }
  } catch (err) {
    // Hook must never crash or block Claude — but a fully silent catch here means a
    // thrown exception (e.g. an unexpected payload shape for a teammate-originated
    // event, which carries an extra agent_id field per Agent Teams hook docs) leaves
    // zero trace: no agents/ write, no lock file, statusline silently reads "none"
    // forever with no way to diagnose why. Best-effort diagnostic breadcrumb instead.
    try {
      const errDir = path.join(process.cwd(), ".claude", "state");
      fs.mkdirSync(errDir, { recursive: true });
      const line =
        JSON.stringify({
          ts: new Date().toISOString(),
          event: (() => {
            try {
              return JSON.parse(raw).hook_event_name;
            } catch (_) {
              return null;
            }
          })(),
          error: err && err.message,
          stack: err && err.stack,
        }) + "\n";
      const errFile = path.join(errDir, "hook-errors.jsonl");
      fs.appendFileSync(errFile, line);
      // Cap growth — keep last 200 lines, best-effort.
      const existing = fs.readFileSync(errFile, "utf8").split("\n").filter(Boolean);
      if (existing.length > 200) fs.writeFileSync(errFile, existing.slice(-200).join("\n") + "\n");
    } catch (_) {}
  }
  process.exit(0);
});

// isDuplicateEvent — deduplication guard for events that fire from both project and home
// settings.json (e.g. UserPromptSubmit, Stop). Uses a per-event lock file with a 500ms TTL.
// The first instance creates the lock and proceeds; the second finds a fresh lock and exits
// 0 silently. Genuine subsequent events (seconds later) are unaffected.
// Lock lives inside the session-scoped tmpDir so it cannot suppress events from OTHER sessions.
function isDuplicateEvent(eventName, tmpDir) {
  const lockFile = path.join(tmpDir, `lock-${eventName}.lock`);
  try {
    fs.mkdirSync(tmpDir, { recursive: true });
  } catch (_) {}
  try {
    const lockStat = fs.statSync(lockFile);
    if (Date.now() - lockStat.mtimeMs < 500) return true; // duplicate — skip
  } catch (_) {} // lock absent — first instance, proceed
  try {
    fs.writeFileSync(lockFile, String(process.pid));
  } catch (_) {}
  return false;
}

// renameAgentFile — re-key an agents/ record from its PreToolUse-time tool_use_id to its
// SubagentStart-time agent_id. PreToolUse only ever has tool_use_id (agent_id doesn't exist yet),
// so agents/<tool_use_id>.json is the only record until SubagentStart resolves the real agent_id.
// SubagentStop and touchAgentLastActive both key by agent_id — without this rename those lookups
// permanently miss: SubagentStop's unlink silently no-ops (a finished agent's record leaks in
// agents/ until the statusline's staleness backstop reaps it, not on actual completion) and a
// worktree agent's own tool events never find their file to refresh last_active.
function renameAgentFile(agentsDir, fromId, toId) {
  if (!fromId || !toId || fromId === toId) return;
  const fromFile = path.join(agentsDir, `${fromId}.json`);
  const toFile = path.join(agentsDir, `${toId}.json`);
  try {
    const rec = JSON.parse(fs.readFileSync(fromFile, "utf8"));
    rec.id = toId;
    fs.writeFileSync(toFile, JSON.stringify(rec));
    fs.unlinkSync(fromFile);
  } catch (_) {} // source file gone/unreadable — nothing to re-key
}

// touchAgentLastActive — refresh a worktree-isolated subagent's liveness on its own tool activity.
// A subagent's tool events run with cwd = its worktree (.claude/worktrees/agent-<agent_id>/) and are
// delivered under the parent session_id, so agentsDir resolves to the same dir holding the agent file
// keyed by <agent_id>. Extract that id from the cwd basename and stamp last_active; the statusline
// filters on (last_active ?? since), so an actively-working agent never ages out. No-op when cwd is
// absent or not a worktree path (non-worktree agents share the parent cwd and can't be attributed).
// Per-file read-modify-write is race-free: a subagent runs one tool at a time (single writer per file).
function touchAgentLastActive(cwd, agentsDir) {
  if (!cwd || typeof cwd !== "string") return;
  const m = path.basename(cwd).match(/^agent-([a-zA-Z0-9]+)$/);
  if (!m) return; // not a worktree cwd — cannot attribute activity to a specific agent
  const agentFile = path.join(agentsDir, `${m[1]}.json`);
  try {
    const rec = JSON.parse(fs.readFileSync(agentFile, "utf8"));
    rec.last_active = new Date().toISOString();
    fs.writeFileSync(agentFile, JSON.stringify(rec));
  } catch (_) {} // agent file gone (agent finished) or unreadable — nothing to refresh
}

function readAgentInfo(root, agentType) {
  if (!agentType || agentType === "unknown") return { model: "inherit", color: null };
  // Strip plugin namespace prefix: "foundry:sw-engineer" → "sw-engineer"
  const shortName = agentType.includes(":") ? agentType.split(":").pop() : agentType;
  // Search order: project-local full name, project-local short name, home-installed short name
  const candidates = [
    path.join(root, ".claude", "agents", `${agentType}.md`),
    path.join(root, ".claude", "agents", `${shortName}.md`),
    path.join(os.homedir(), ".claude", "agents", `${shortName}.md`),
  ];
  for (const f of candidates) {
    try {
      const content = fs.readFileSync(f, "utf8");
      // Extract frontmatter block (between first and second ---)
      const fm = content.match(/^---\n([\s\S]*?)\n---/);
      const block = fm ? fm[1] : "";
      const modelMatch = block.match(/^model:\s*(\S+)/m);
      const colorMatch = block.match(/^color:\s*(\S+)/m);
      return {
        model: modelMatch ? modelMatch[1] : "inherit",
        color: colorMatch ? colorMatch[1] : null,
      };
    } catch (_) {}
  }
  return { model: "inherit", color: null }; // built-in types (general-purpose) or missing file
}

function appendLog(logFile, dir, entry) {
  try {
    fs.mkdirSync(dir, { recursive: true });
    fs.appendFileSync(logFile, JSON.stringify(entry) + "\n");
  } catch (_) {}
}

// summarizeArgs — extract a compact, safe summary of tool_input for timings.jsonl.
// Omits large content fields (Write/Edit body) to keep log lines short.
function summarizeArgs(toolName, toolInput) {
  if (!toolInput) return null;
  try {
    switch (toolName) {
      case "Read": {
        let s = "file_path=" + (toolInput.file_path || "?");
        if (toolInput.offset) s += " offset=" + toolInput.offset;
        if (toolInput.limit) s += " limit=" + toolInput.limit;
        return s;
      }
      case "Write":
      case "Edit":
        return "file_path=" + (toolInput.file_path || "?");
      case "Bash":
        return "command=" + (toolInput.command || "").slice(0, 200);
      case "Grep":
        return "pattern=" + (toolInput.pattern || "") + " path=" + (toolInput.path || ".");
      case "Glob":
        return "pattern=" + (toolInput.pattern || "");
      case "Agent":
      case "Task":
        return "type=" + (toolInput.subagent_type || "?") + " desc=" + (toolInput.description || "");
      case "Skill":
        return "skill=" + (toolInput.skill || "") + " args=" + (toolInput.args || "");
      default:
        return JSON.stringify(toolInput).slice(0, 200);
    }
  } catch (_) {
    return null;
  }
}

// recordTiming — read start marker, compute wall-clock duration, append to timings.jsonl, delete marker.
// Natural dedup: first PostToolUse fire reads+deletes the marker; second fire finds it gone and skips.
function recordTiming(toolUseId, toolName, sid, status, timingsDir, timingsFile, logsDir, model) {
  try {
    const f = path.join(timingsDir, toolUseId + ".json");
    const d = JSON.parse(fs.readFileSync(f, "utf8"));
    const duration_ms = Date.now() - d.start;
    appendLog(timingsFile, logsDir, {
      ts: new Date().toISOString(),
      tool: toolName || d.tool,
      args: d.args || null,
      tool_use_id: toolUseId,
      session_id: sid,
      duration_ms,
      status,
      model: model || d.model || null,
    });
    fs.unlinkSync(f);
  } catch (_) {}
}
