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
//   8. PreCompact: log to compactions.jsonl; scan transcript tail for Write/Edit tool_use blocks
//      and write modified file paths to state/session-context.md as a compaction breadcrumb
//   9. UserPromptSubmit: write a queue marker to state/queue/ (deduplicated via 500ms lock)
//  10. Stop: clear state/tools/ and remove oldest queue marker (deduplicated); agents left intact;
//      delete any orphaned timing start markers (tool calls that never got a PostToolUse)
//  11. SessionEnd: delete entire /tmp/claude-state-<session_id>/ subtree; prune stale git worktrees
//
// HOOK EVENT RESPONSIBILITIES
//
//   PreToolUse
//     • Logs Task/Agent and Skill invocations to invocations.jsonl.
//     • Opens a codex session file when Skill(codex:*) or Agent(codex:*) starts
//       (keyed by tool_use_id so concurrent sessions don't collide).
//     • Writes/increments a per-tool-type file in state/tools/ for the 🔧 display.
//       Agent and Task tool calls are excluded here — they are tracked via the
//       dedicated SubagentStart/Stop events to avoid double-counting.
//     • For Agent() calls: writes state/pending/<tool_use_id>.json with subagent_type so
//       SubagentStart can resolve agent_type when its payload omits it (Agent() vs Task()).
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
//     • Appends a compaction event to compactions.jsonl.
//     • Scans the tail of the transcript for Write/Edit tool_use blocks, extracts
//       modified file paths, and writes state/session-context.md — a lightweight
//       breadcrumb that survives context compaction and is re-read at session resume.
//
//   UserPromptSubmit
//     • Writes a marker file to state/queue/ so statusline.js shows 💬 on Line 1
//       while Claude is processing the current turn. (UserPromptSubmit fires when Claude
//       begins handling the message — not when the user presses Enter — so the marker
//       represents "currently processing", not a queued-but-unstarted message.)
//
//   Stop  (end of Claude's turn)
//     • Clears state/tools/ so the 🔧 line resets between turns.
//     • Removes ALL processing markers from state/queue/ so the 💬 badge disappears.
//       Clears all (not just oldest) to handle interrupted turns where Stop didn't fire
//       and stale markers accumulated. Agents are intentionally NOT cleared here.
//     • Deletes any remaining files in state/timings/ (orphaned start markers from tool
//       calls that never received a PostToolUse/PostToolUseFailure event).
//
//   SessionEnd  (full session teardown)
//     • Clears state/agents/, state/tools/, state/codex/, and state/queue/ completely.
//     • Runs `git worktree prune` to remove stale worktree refs.
//     • Removes any worktrees under .claude/worktrees/ older than 2 hours
//       (orphaned by crashed agents or interrupted sessions).
//
// STATE FILES
//   ~/.claude/logs/invocations.jsonl    — append-only audit log (agents + skills); global across all projects; includes project field
//   ~/.claude/logs/compactions.jsonl    — compaction events log; global across all projects; includes project field
//   ~/.claude/logs/timings.jsonl        — append-only per-tool timing log {ts, project, tool, args, tool_use_id, session_id, duration_ms, status, model}
//   /tmp/claude-state-<session_id>/agents/<id>.json     — one file per active subagent
//   /tmp/claude-state-<session_id>/codex/<id>.json      — one file per active codex plugin session
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
const { execFileSync } = require("child_process");

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
    // Global logs dir — audit logs accumulate across all projects and sessions
    const globalLogsDir = path.join(os.homedir(), ".claude", "logs");
    const logFile = path.join(globalLogsDir, "invocations.jsonl");
    const compactFile = path.join(globalLogsDir, "compactions.jsonl");
    const timingsFile = path.join(globalLogsDir, "timings.jsonl");
    // Project slug used to tag log entries so they can be filtered by project
    const projectSlug = root.replace(/[/.]/g, "-");

    // Ephemeral per-session state lives in /tmp scoped by session_id.
    // This prevents cross-session contamination when multiple Claude Code instances run
    // concurrently — each session owns its own subtree and cannot see another session's state.
    // Fallback to 'default' if session_id is missing (older Claude Code versions).
    const sid = (session_id || "default").replace(/[^a-zA-Z0-9_-]/g, "_");
    const tmpDir = path.join("/tmp", `claude-state-${sid}`);
    const agentsDir = path.join(tmpDir, "agents");
    const toolsDir = path.join(tmpDir, "tools");
    const codexDir = path.join(tmpDir, "codex");
    const queueDir = path.join(tmpDir, "queue");
    const pendingDir = path.join(tmpDir, "pending");
    const timingsDir = path.join(tmpDir, "timings");

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
        // Cache subagent_type so SubagentStart can resolve agent_type when its payload omits it.
        // Agent()-spawned agents may have null agent_type in SubagentStart; pending/ bridges the gap.
        if (tool_name === "Agent" && tool_input?.subagent_type && data.tool_use_id) {
          try {
            fs.mkdirSync(pendingDir, { recursive: true });
            fs.writeFileSync(
              path.join(pendingDir, `${data.tool_use_id}.json`),
              JSON.stringify({ type: tool_input.subagent_type, ts }),
            );
          } catch (_) {}
        }
      } else if (tool_name === "Skill") {
        const skill = tool_input?.skill || "unknown";
        const args = tool_input?.args || "";
        appendLog(logFile, globalLogsDir, { ts, project: projectSlug, event: "invoked", tool: "Skill", skill, args });
        // Track codex plugin sessions for statusline display (tool_use_id is the stable key)
        // Matches any codex: plugin command (codex:review, codex:adversarial-review, codex:rescue, etc.)
        if (skill && skill.startsWith("codex:") && data.tool_use_id) {
          try {
            fs.mkdirSync(codexDir, { recursive: true });
            const shortName = skill.slice("codex:".length);
            fs.writeFileSync(
              path.join(codexDir, `${data.tool_use_id}.json`),
              JSON.stringify({ id: data.tool_use_id, since: ts, type: shortName }),
            );
          } catch (_) {}
        }
      }
      // Track all tool calls for statusline tool-activity line (count per type within window)
      // Exclude Agent/Task — those are tracked separately via SubagentStart/Stop → state/agents/
      if (tool_name && tool_name !== "Agent" && tool_name !== "Task") {
        try {
          fs.mkdirSync(toolsDir, { recursive: true });
          const toolFile = path.join(toolsDir, `${tool_name}.json`);
          let count = 1;
          try {
            const existing = JSON.parse(fs.readFileSync(toolFile, "utf8"));
            count = (existing.count || 0) + 1;
          } catch (_) {}
          fs.writeFileSync(toolFile, JSON.stringify({ tool: tool_name, since: ts, count }));
        } catch (_) {}
      }
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
      // Remove codex plugin session tracking when any Skill(codex:*) completes.
      if (data.tool_use_id) {
        const isCodexSkill = tool_name === "Skill" && tool_input?.skill?.startsWith("codex:");
        if (isCodexSkill) {
          try {
            fs.unlinkSync(path.join(codexDir, `${data.tool_use_id}.json`));
          } catch (_) {}
        }
        // Complete timing: read start marker, compute duration, append to timings.jsonl, delete marker.
        // Natural dedup: first fire reads+deletes the marker; second fire finds it gone and exits silently.
        recordTiming(data.tool_use_id, tool_name, session_id, "ok", timingsDir, timingsFile, globalLogsDir, data.model);
      }
    } else if (hook_event_name === "PostToolUseFailure") {
      // Same as PostToolUse timing but marks status "error".
      if (data.tool_use_id) {
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
      // Each agent gets its own file — no read-modify-write race with concurrent agents
      try {
        fs.mkdirSync(agentsDir, { recursive: true });
        const id = agent_id || ts;
        // Resolve agent type — Agent()-spawned agents may have null agent_type in the SubagentStart
        // payload. Look up the pending cache written by PreToolUse to recover the true type.
        let resolvedType = agent_type;
        if (!resolvedType && data.tool_use_id) {
          try {
            const pendingFile = path.join(pendingDir, `${data.tool_use_id}.json`);
            const p = JSON.parse(fs.readFileSync(pendingFile, "utf8"));
            resolvedType = p.type;
            fs.unlinkSync(pendingFile); // consume once — remainder cleaned at SessionEnd
          } catch (_) {}
        }
        // Try hook data first; fall back to reading model+color from agent frontmatter
        const info = readAgentInfo(root, resolvedType);
        const model = data.model || info.model;
        const color = info.color;
        fs.writeFileSync(
          path.join(agentsDir, `${id}.json`),
          JSON.stringify({ id, type: resolvedType || "unknown", model, color, since: ts }),
        );
        // Also track codex:* agents in state/codex/ so statusline shows them in the 🤖 section
        if (resolvedType && resolvedType.startsWith("codex:")) {
          fs.mkdirSync(codexDir, { recursive: true });
          const shortName = resolvedType.slice("codex:".length);
          fs.writeFileSync(path.join(codexDir, `${id}.json`), JSON.stringify({ id, since: ts, type: shortName }));
        }
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
        // Also clean up codex tracking entry if this was a codex:* agent
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
      appendLog(compactFile, globalLogsDir, { ts, project: projectSlug, event: "pre_compact" });
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
          fs.writeFileSync(path.join(stateDir, "session-context.md"), lines.join("\n") + "\n");
        } catch (_) {}
      }
    } else if (hook_event_name === "UserPromptSubmit") {
      // Deduplication lock — project and home settings.json both register this hook.
      // Guard: if a lock for UserPromptSubmit exists and is < 500ms old, skip (duplicate fire).
      if (isDuplicateEvent("UserPromptSubmit", tmpDir)) process.exit(0);
      // Write a processing marker so statusline shows 💬 while Claude handles this turn.
      // Cleared on Stop (turn complete). SessionEnd removes any crash remnants via tmpDir wipe.
      try {
        fs.mkdirSync(queueDir, { recursive: true });
        const id = ts.replace(/[:.]/g, "-");
        fs.writeFileSync(path.join(queueDir, `${id}.json`), JSON.stringify({ since: ts }));
      } catch (_) {}
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
    } else if (hook_event_name === "SessionEnd") {
      // Full session teardown — delete the entire session-scoped temp directory.
      // All ephemeral state (agents, tools, codex, queue, dedup locks) lives there.
      try {
        fs.rmSync(tmpDir, { recursive: true, force: true });
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
    }
  } catch (_) {
    // Silently swallow all errors — hook must never crash or block Claude
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

function readAgentInfo(root, agentType) {
  if (!agentType || agentType === "unknown") return { model: "inherit", color: null };
  try {
    const content = fs.readFileSync(path.join(root, ".claude", "agents", `${agentType}.md`), "utf8");
    // Extract frontmatter block (between first and second ---)
    const fm = content.match(/^---\n([\s\S]*?)\n---/);
    const block = fm ? fm[1] : "";
    const modelMatch = block.match(/^model:\s*(\S+)/m);
    const colorMatch = block.match(/^color:\s*(\S+)/m);
    return {
      model: modelMatch ? modelMatch[1] : "inherit",
      color: colorMatch ? colorMatch[1] : null,
    };
  } catch (_) {
    return { model: "inherit", color: null }; // built-in types (general-purpose) or missing file
  }
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
