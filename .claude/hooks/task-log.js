#!/usr/bin/env node
// PreToolUse hook  — logs Task/Skill invocations to the audit log.
// SubagentStart hook — adds the subagent to the active-agents state.
// SubagentStop hook  — removes the subagent from state and logs completion.
//
// Writes to:
//   .claude/logs/invocations.jsonl  — append-only audit log
//   .claude/state/agents.json       — currently active subagents (mutable)
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
    const logFile = path.join(logsDir, "invocations.jsonl");
    const stateFile = path.join(stateDir, "agents.json");

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
    } else if (hook_event_name === "SubagentStart") {
      // Subagent has actually spawned — add to active state (filter first to deduplicate)
      mutateAgents(stateFile, stateDir, (agents) => [
        ...agents.filter((a) => a.id !== agent_id),
        { id: agent_id || ts, type: agent_type || "unknown", since: ts },
      ]);
    } else if (hook_event_name === "SubagentStop") {
      // Subagent finished — remove from active state and log completion
      mutateAgents(stateFile, stateDir, (agents) => (agent_id ? agents.filter((a) => a.id !== agent_id) : agents));
      appendLog(logFile, logsDir, { ts, event: "completed", tool: "Task", agent: agent_type || "unknown" });
    }
  } catch (_) {
    // Silently swallow all errors — hook must never crash or block Claude
  }
  process.exit(0);
});

function appendLog(logFile, dir, entry) {
  try {
    fs.mkdirSync(dir, { recursive: true });
    fs.appendFileSync(logFile, JSON.stringify(entry) + "\n");
  } catch (_) {}
}

function mutateAgents(stateFile, dir, transform) {
  try {
    fs.mkdirSync(dir, { recursive: true });
    let agents = [];
    try {
      agents = JSON.parse(fs.readFileSync(stateFile, "utf8"));
    } catch (_) {}
    fs.writeFileSync(stateFile, JSON.stringify(transform(agents), null, 2));
  } catch (_) {}
}
