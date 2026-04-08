#!/usr/bin/env node
// statusline.js — Claude Code status line renderer
//
// PURPOSE
//   Renders a two-line live status display in the Claude Code terminal, refreshed
//   on every hook event.  Gives an at-a-glance view of model, cost, context usage,
//   active subagents, running codex plugin sessions, and current tool activity.
//
// HOW IT WORKS
//   1. Parse stdin JSON for model, workspace, context_window, cost, and session_id
//   2. Resolve the per-session temp dir at /tmp/claude-state-<session_id>/ (written by task-log.js)
//   3. Build Line 1: model name, project dir, billing (API key = yellow; OAuth = cyan plan name),
//      context bar (10-char block bar; green <50% · yellow <75% · red ≥75% used), and 💬 [N]
//      badge while Claude is processing the current turn; N shown only when >1 messages queued
//   4. Build Line 2 agent segment (🕵): read state/agents/*.json; skip codex:* agents and entries
//      older than 10 min (safety net); group by type; color from agent frontmatter COLOR_MAP
//   5. Build Line 2 codex segment (🤖): read state/codex/*.json; skip entries older than 30 min;
//      group by short type name
//   6. Build Line 2 tool segment (🔧): read state/tools/*.json; skip entries older than 30 s;
//      render per-type call counts with fixed TOOL_COLORS palette
//   7. Write both lines to stdout with \x1b[K (clear-to-end-of-line) on each line
//
// OUTPUT FORMAT
//   Line 1 — session metadata:
//     <model>  <project-dir>  <billing>  <context-bar pct%>  [💬 while processing]
//
//   Line 2 — runtime activity (always shown, "none" when idle):
//     🕵 N <type> [×N], …  │  🤖 <codex-type> [×N]  │  🔧 <tools>
//     codex:* subagents are excluded from 🕵 and shown in 🤖 by short name
//
// LINE 1 DETAILS
//   model       display_name or id from session JSON
//   project-dir basename of workspace.current_dir
//   billing     API key mode  → yellow  "API $X.XX"  (real spend, every token costs)
//               OAuth/sub mode → cyan   "<Plan> ~$X.XX"  (theoretical API-rate cost,
//               NOT actual quota; use /status for real monthly usage)
//   Plan name   priority: CLAUDE_PLAN env var → subscription.json cached at SessionStart
//               by `claude auth status` → fallback "Sub"
//   context bar 10-char block bar; color: green <50% · yellow <75% · red ≥75% used
//
// LINE 2 DETAILS
//   🕵 agents   reads /tmp/claude-state-<session_id>/agents/*.json written by task-log.js
//               SubagentStart/Stop. Groups by type; specialized agents shown in their
//               declared color (from agent frontmatter color: field); general-purpose gray.
//               Safety-net: ignores entries older than 10 min (SubagentStop crash/hang).
//   🤖 codex    reads /tmp/claude-state-<session_id>/codex/*.json written by task-log.js
//               PreToolUse/PostToolUse and SubagentStart/Stop. Shows short name of each
//               active codex session. Safety-net: ignores entries older than 30 min.
//   🔧 tools    reads /tmp/claude-state-<session_id>/tools/*.json written by task-log.js
//               PreToolUse. Shows tool types active within the last 30 s with per-type
//               call counts. Each tool type has a fixed ANSI color for visual stability.
//               Agent and Task tool calls are excluded (tracked under 🕵 instead).
//
// SESSION ISOLATION
//   All state dirs are scoped to /tmp/claude-state-<session_id>/ using the session_id from
//   the JSON payload. Multiple Claude Code sessions (same or different projects) each write
//   and read their own subtree — no cross-session contamination.
//
// ANSI RENDERING
//   \x1b[K at end of each line clears to end of line — prevents stale characters
//   from longer previous renders bleeding through when the new output is shorter.
//
// EXIT CODES
//   0  Always — status line render; this hook never blocks Claude.

const fs = require("fs");
const os = require("os");
const path = require("path");

let raw = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (d) => (raw += d));
process.stdin.on("end", () => {
  try {
    const { model, workspace, context_window, cost, session_id } = JSON.parse(raw);

    // Session-scoped temp dir — mirrors the layout written by task-log.js.
    // Falls back to 'default' for older Claude Code versions without session_id.
    const sid = (session_id || "default").replace(/[^a-zA-Z0-9_-]/g, "_");
    const tmpDir = path.join("/tmp", `claude-state-${sid}`);

    const modelName = model?.display_name || model?.id || "";
    const dir = path.basename(workspace?.current_dir || process.cwd());
    const remainingRaw = context_window?.remaining_percentage;
    const remaining = Number.isFinite(Number(remainingRaw)) ? Number(remainingRaw) : null;
    const usd = cost?.total_cost_usd ?? 0;
    const isApiKey = !!process.env.ANTHROPIC_API_KEY;

    // Plan detection: CLAUDE_PLAN env var wins when set; otherwise read subscription type
    // cached at SessionStart by `claude auth status`; falls back to "Sub".
    let planName;
    if (process.env.CLAUDE_PLAN) {
      planName = process.env.CLAUDE_PLAN;
    } else {
      planName = "Sub";
      try {
        const sub = JSON.parse(fs.readFileSync(path.join(os.homedir(), ".claude/state/subscription.json"), "utf8"));
        if (sub.subscriptionType) planName = sub.subscriptionType[0].toUpperCase() + sub.subscriptionType.slice(1);
      } catch (_) {}
    }

    // Agent color names (from color: frontmatter) → ANSI escape codes
    const COLOR_MAP = {
      blue: "\x1b[34m",
      cyan: "\x1b[36m",
      green: "\x1b[32m",
      indigo: "\x1b[34m", // closest ANSI to indigo — reserved (no agent declares this color)
      lime: "\x1b[92m", // bright green — reserved (no agent declares this color)
      magenta: "\x1b[35m", // reserved (no agent declares this color)
      orange: "\x1b[33m", // closest ANSI to orange
      pink: "\x1b[95m", // bright magenta
      purple: "\x1b[94m", // bright blue
      teal: "\x1b[96m", // bright cyan — reserved (no agent declares this color)
      violet: "\x1b[35m", // magenta (closest ANSI) — reserved (no agent declares this color)
      yellow: "\x1b[93m", // bright yellow
    };

    // Unique color per tool type — fixed palette so colors are stable
    const TOOL_COLORS = {
      Read: "\x1b[34m", // blue
      Write: "\x1b[92m", // bright green
      Edit: "\x1b[32m", // green
      Bash: "\x1b[33m", // yellow
      Grep: "\x1b[36m", // cyan
      Glob: "\x1b[96m", // bright cyan
      WebFetch: "\x1b[35m", // magenta
      WebSearch: "\x1b[95m", // bright magenta
      Agent: "\x1b[94m", // bright blue
      Task: "\x1b[94m", // bright blue
      Skill: "\x1b[93m", // bright yellow
      NotebookEdit: "\x1b[91m", // bright red
    };
    const TOOL_DEFAULT_COLOR = "\x1b[37m"; // white for unknowns

    const parts = [];
    let agentsPart = "";
    let codexPart = "";

    if (modelName) parts.push(`\x1b[2m${modelName}\x1b[0m`);
    if (dir) parts.push(`\x1b[2m${dir}\x1b[0m`);

    if (isApiKey) {
      // API key billing — every token costs real money, show actual spend
      parts.push(`\x1b[33mAPI $${usd.toFixed(2)}\x1b[0m`); // yellow
    } else {
      // OAuth subscription (Pro / Max) — cost.total_cost_usd is theoretical API-rate
      // cost (tokens × published rates), NOT actual subscription charge or quota consumption.
      // Use /status for actual monthly quota.
      parts.push(`\x1b[36m${planName} ~$${usd.toFixed(2)}\x1b[0m`); // cyan plan + tilde
    }

    if (remaining !== null) {
      // remaining_percentage === 0 after /clear means context was just reset, not genuinely full.
      // Treat it as 0% used; a truly full context triggers compaction before this point.
      const pct = remaining === 0 ? 0 : Math.max(0, Math.min(100, 100 - remaining)); // pct = context used (100 - remaining_pct)
      const filled = Math.round(pct / 10);
      const bar = "█".repeat(filled) + "░".repeat(10 - filled);
      const color = pct < 50 ? 32 : pct < 75 ? 33 : 31; // green / yellow / red
      parts.push(`\x1b[${color}m${bar} ${Math.round(pct)}%\x1b[0m`);
    }

    const now = Date.now(); // shared by agents, tools, and queue sections

    // Line 1 — processing badge (💬) — shown while Claude is handling the current turn.
    // UserPromptSubmit writes a marker when Claude begins processing; Stop deletes it when done.
    // No age gate: markers are ephemeral (turn-scoped); SessionEnd cleans up any crash remnants.
    // Shows 💬 N when N > 1 messages are queued (user sent more while Claude was busy).
    try {
      const queueDir = path.join(tmpDir, "queue");
      const queueFiles = fs.readdirSync(queueDir).filter((f) => f.endsWith(".json"));
      const pending = queueFiles.filter((f) => {
        try {
          const q = JSON.parse(fs.readFileSync(path.join(queueDir, f), "utf8"));
          return !q.processed_at;
        } catch (_) {
          return false;
        }
      }).length;
      if (pending > 0) {
        const badge = pending > 1 ? `💬 ${pending}` : "💬";
        parts.push(`\x1b[36m${badge}\x1b[0m`); // cyan — processing indicator
      }
    } catch (_) {}

    // Line 2 — agents (always shown, even when 0)
    try {
      const agentsDir = path.join(tmpDir, "agents");
      const files = fs.readdirSync(agentsDir).filter((f) => f.endsWith(".json"));
      // Safety-net: drop agents stuck > 10 min (SubagentStop didn't fire — crash or hang)
      const MAX_AGE_MS = 10 * 60 * 1000;
      const allAgents = files.flatMap((f) => {
        try {
          return [JSON.parse(fs.readFileSync(path.join(agentsDir, f), "utf8"))];
        } catch (_) {
          return [];
        }
      });
      // Exclude codex:* agents — they are shown in the 🤖 section instead
      const agents = allAgents.filter(
        (a) => (!a.since || now - new Date(a.since).getTime() < MAX_AGE_MS) && !(a.type && a.type.startsWith("codex:")),
      );
      if (agents.length > 0) {
        // Specialized + pinned model → type name, normal color
        // Specialized + inherit model → type name, gray (no special model assigned)
        // General-purpose → model name, gray
        const groups = new Map();
        for (const a of agents) {
          const isGeneral = !a.type || a.type === "general-purpose" || a.type === "unknown";
          const model = a.model || "inherit";
          const key = isGeneral ? `model:${model}` : `type:${a.type}`;
          const isGray = isGeneral || model === "inherit";
          const label = isGeneral ? model : a.type;
          // Use agent's declared color (from frontmatter) if available and not gray
          const ansiColor = !isGray && a.color ? COLOR_MAP[a.color] || "" : "";
          if (!groups.has(key)) groups.set(key, { label, isGray, ansiColor, count: 0 });
          groups.get(key).count++;
        }
        const items = [...groups.values()]
          .sort((a, b) => b.count - a.count)
          .map(({ label, isGray, ansiColor, count }) => {
            const colored = isGray ? `\x1b[2m${label}\x1b[0m` : `${ansiColor}${label}\x1b[0m`;
            return count > 1 ? `${colored} ×${count}` : colored;
          });
        agentsPart = `\x1b[35m🕵 ${agents.length}\x1b[0m ${items.join(", ")}`;
      } else {
        agentsPart = `\x1b[35m🕵\x1b[0m \x1b[2mnone\x1b[0m`;
      }
    } catch (_) {
      agentsPart = `\x1b[35m🕵\x1b[0m \x1b[2mnone\x1b[0m`;
    }

    // Line 2 — codex sessions (always shown, even when 0)
    try {
      const codexDir = path.join(tmpDir, "codex");
      const codexFiles = fs.readdirSync(codexDir).filter((f) => f.endsWith(".json"));
      const MAX_CODEX_AGE_MS = 30 * 60 * 1000; // 30-min safety net
      // Collect active entries with their short type names (e.g. "codex-rescue", "review")
      const activeCodexTypes = codexFiles.flatMap((f) => {
        try {
          const c = JSON.parse(fs.readFileSync(path.join(codexDir, f), "utf8"));
          if (c.since && now - new Date(c.since).getTime() >= MAX_CODEX_AGE_MS) return [];
          return [c.type || "codex"];
        } catch (_) {
          return [];
        }
      });
      if (activeCodexTypes.length > 0) {
        const groups = new Map();
        for (const n of activeCodexTypes) groups.set(n, (groups.get(n) || 0) + 1);
        const items = [...groups.entries()].map(([n, cnt]) => (cnt > 1 ? `${n} ×${cnt}` : n));
        codexPart = `\x1b[33m🤖 ${items.join(", ")}\x1b[0m`;
      } else {
        codexPart = `\x1b[33m🤖\x1b[0m \x1b[2mnone\x1b[0m`;
      }
    } catch (_) {
      codexPart = `\x1b[33m🤖\x1b[0m \x1b[2mnone\x1b[0m`;
    }

    // Line 2 — tool activity segment (always shown, even when idle)
    let toolLine = "";
    try {
      const toolsDir = path.join(tmpDir, "tools");
      const toolFiles = fs.readdirSync(toolsDir).filter((f) => f.endsWith(".json"));
      const TOOL_MAX_AGE_MS = 30 * 1000;
      const activeTools = toolFiles
        .flatMap((f) => {
          try {
            const t = JSON.parse(fs.readFileSync(path.join(toolsDir, f), "utf8"));
            if (!t.since || now - new Date(t.since).getTime() > TOOL_MAX_AGE_MS) return [];
            if (!t.tool || typeof t.tool !== "string") return [];
            const count = Number.isFinite(Number(t.count)) ? Math.max(1, Number(t.count)) : 1;
            return [{ tool: t.tool, count }];
          } catch (_) {
            return [];
          }
        })
        .sort((a, b) => a.tool.localeCompare(b.tool));
      if (activeTools.length > 0) {
        const colored = activeTools.map(({ tool: t, count: n }) => {
          const label = `${t} x${n}`;
          return `${TOOL_COLORS[t] || TOOL_DEFAULT_COLOR}${label}\x1b[0m`;
        });
        toolLine = `\x1b[2m🔧\x1b[0m ${colored.join(" \x1b[2m·\x1b[0m ")}`;
      } else {
        toolLine = `\x1b[2m🔧 none\x1b[0m`;
      }
    } catch (_) {
      toolLine = `\x1b[2m🔧 none\x1b[0m`;
    }

    const line1 = parts.join(" \x1b[2m│\x1b[0m ");
    const line2 = `${agentsPart} \x1b[2m│\x1b[0m ${codexPart} \x1b[2m│\x1b[0m ${toolLine}`;
    const lines = [line1, line2];
    // \x1b[K clears to end of line — erases stale chars from longer previous renders.
    process.stdout.write(lines.map((l) => l + "\x1b[K").join("\n") + "\x1b[K");
  } catch (_) {
    process.stdout.write("\x1b[2m?\x1b[0m");
  }
});
