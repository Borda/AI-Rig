#!/usr/bin/env node
// PreToolUse hook  — logs Task/Skill invocations to the audit log.
// SubagentStart hook — adds the subagent to the active-agents state.
// SubagentStop hook  — removes the subagent from state and logs completion.
// PreCompact hook  — logs context compaction start to compactions.jsonl.
// Stop hook        — clears active-agents state when the main agent finishes.
// SessionEnd hook  — clears active-agents state when the session terminates.
//
// Writes to:
//   .claude/logs/invocations.jsonl      — append-only audit log
//   .claude/logs/compactions.jsonl      — compaction events log
//   .claude/state/agents/<id>.json      — one file per active subagent (replaces agents.json)
//   .claude/state/tools/<tool>.json     — one file per tool type, updated on each PreToolUse
//   .claude/state/session-context.md    — files modified + compact summary (written on compaction)
//
// Exit 0 always — logging must never block Claude.

const fs = require("fs");
const path = require("path");

let raw = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (d) => (raw += d));
process.stdin.on("end", () => {
  try {
    const data = JSON.parse(raw);
    const { hook_event_name, tool_name, tool_input, agent_id, agent_type } = data;

    // Resolve workspace root from CWD (hooks run with CWD = project root)
    const root = process.cwd();
    const logsDir = path.join(root, ".claude", "logs");
    const stateDir = path.join(root, ".claude", "state");
    const agentsDir = path.join(stateDir, "agents");
    const toolsDir = path.join(stateDir, "tools");
    const logFile = path.join(logsDir, "invocations.jsonl");
    const compactFile = path.join(logsDir, "compactions.jsonl");

    const ts = new Date().toISOString();

    if (hook_event_name === "PreToolUse") {
      if (tool_name === "Task" || tool_name === "Agent") {
        const agentType = tool_input?.subagent_type || "unknown";
        const desc = tool_input?.description || "";
        const prompt = (tool_input?.prompt || "").slice(0, 200);
        appendLog(logFile, logsDir, { ts, event: "started", tool: "Task", agent: agentType, desc, prompt });
      } else if (tool_name === "Skill") {
        const skill = tool_input?.skill || "unknown";
        const args = tool_input?.args || "";
        appendLog(logFile, logsDir, { ts, event: "invoked", tool: "Skill", skill, args });
      }
      // Track all tool calls for statusline tool-activity line (count per type within window)
      if (tool_name) {
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
    } else if (hook_event_name === "SubagentStart") {
      // Each agent gets its own file — no read-modify-write race with concurrent agents
      try {
        fs.mkdirSync(agentsDir, { recursive: true });
        const id = agent_id || ts;
        // Try hook data first; fall back to reading model+color from agent frontmatter
        const info = readAgentInfo(root, agent_type);
        const model = data.model || info.model;
        const color = info.color;
        fs.writeFileSync(
          path.join(agentsDir, `${id}.json`),
          JSON.stringify({ id, type: agent_type || "unknown", model, color, since: ts }),
        );
      } catch (_) {}
    } else if (hook_event_name === "SubagentStop") {
      // Delete the per-agent file
      try {
        const id = agent_id || ts;
        fs.unlinkSync(path.join(agentsDir, `${id}.json`));
      } catch (_) {}
      // Capture last assistant message (up to 500 chars) for post-mortem debugging
      const lastMsg = (data.last_assistant_message || "").slice(0, 500) || undefined;
      appendLog(logFile, logsDir, {
        ts,
        event: "completed",
        tool: "Task",
        agent: agent_type || "unknown",
        ...(lastMsg && { last_msg: lastMsg }),
      });
    } else if (hook_event_name === "PreCompact") {
      appendLog(compactFile, logsDir, { ts, event: "pre_compact" });
      // Extract modified files from transcript and write context snapshot
      const transcriptPath = data.transcript_path;
      if (transcriptPath) {
        try {
          // Read last 50KB of transcript to find Write/Edit tool_use blocks
          const stats = fs.statSync(transcriptPath);
          const readSize = Math.min(50 * 1024, stats.size);
          const buf = Buffer.alloc(readSize);
          const fd = fs.openSync(transcriptPath, "r");
          fs.readSync(fd, buf, 0, readSize, stats.size - readSize);
          fs.closeSync(fd);
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
    } else if (hook_event_name === "Stop" || hook_event_name === "SessionEnd") {
      // Session finished — clear all per-agent and per-tool files so the status line resets cleanly
      for (const dir of [agentsDir, toolsDir]) {
        try {
          const files = fs.readdirSync(dir);
          for (const f of files) {
            try {
              fs.unlinkSync(path.join(dir, f));
            } catch (_) {}
          }
        } catch (_) {}
      }
    }
  } catch (_) {
    // Silently swallow all errors — hook must never crash or block Claude
  }
  process.exit(0);
});

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
