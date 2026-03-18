#!/usr/bin/env node
// Minimal statusline: model | cwd | billing | context bar
// Receives session JSON via stdin from Claude Code
//
// Billing detection:
//   - ANTHROPIC_API_KEY set → API key billing → show real cost in yellow ($X.XX)
//   - No API key          → OAuth subscription → show "<Plan> ~$X.XX" in cyan
//   Plan name: read from ~/.claude/state/subscription.json (written at SessionStart by `claude auth status`).
//   Falls back to CLAUDE_PLAN env var, then "Sub" if cache is unavailable.
//   Note: cost.total_cost_usd is tokens × API rates — NOT actual subscription charge.
//   Subscription quota % is not exposed in hook data. Check /status for monthly usage.

const fs = require("fs");
const os = require("os");
const path = require("path");

let raw = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (d) => (raw += d));
process.stdin.on("end", () => {
  try {
    const { model, workspace, context_window, cost } = JSON.parse(raw);

    const modelName = model?.display_name || model?.id || "";
    const dir = path.basename(workspace?.current_dir || process.cwd());
    const remaining = context_window?.remaining_percentage ?? null;
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
      indigo: "\x1b[34m", // closest ANSI to indigo
      lime: "\x1b[92m", // bright green
      magenta: "\x1b[35m",
      orange: "\x1b[33m", // closest ANSI to orange
      pink: "\x1b[95m", // bright magenta
      purple: "\x1b[94m", // bright blue
      teal: "\x1b[96m", // bright cyan
      violet: "\x1b[35m", // magenta (closest ANSI)
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
      const pct = Math.max(0, Math.min(100, 100 - remaining)); // pct = context used (100 - remaining_pct)
      const bar = "█".repeat(Math.round(pct / 10)) + "░".repeat(10 - Math.round(pct / 10));
      const color = pct < 50 ? 32 : pct < 75 ? 33 : 31; // green / yellow / red
      parts.push(`\x1b[${color}m${bar} ${Math.round(pct)}%\x1b[0m`);
    }

    const now = Date.now(); // shared by agents and tools sections

    // Line 2 — agents (always shown, even when 0)
    try {
      const agentsDir = path.join(workspace?.current_dir || process.cwd(), ".claude/state/agents");
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
      const agents = allAgents.filter((a) => !a.since || now - new Date(a.since).getTime() < MAX_AGE_MS);
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
        agentsPart = `\x1b[35m🕵 ${agents.length} agent${agents.length > 1 ? "s" : ""}\x1b[0m (${items.join(", ")})`;
      } else {
        agentsPart = `\x1b[35m🕵\x1b[0m \x1b[2mnone\x1b[0m`;
      }
    } catch (_) {
      agentsPart = `\x1b[35m🕵\x1b[0m \x1b[2mnone\x1b[0m`;
    }

    // Line 2 — codex sessions (always shown, even when 0)
    try {
      const codexDir = path.join(workspace?.current_dir || process.cwd(), ".claude/state/codex");
      const codexFiles = fs.readdirSync(codexDir).filter((f) => f.endsWith(".json"));
      const MAX_CODEX_AGE_MS = 30 * 60 * 1000; // 30-min safety net
      const activeCodex = codexFiles.filter((f) => {
        try {
          const c = JSON.parse(fs.readFileSync(path.join(codexDir, f), "utf8"));
          return !c.since || now - new Date(c.since).getTime() < MAX_CODEX_AGE_MS;
        } catch (_) {
          return false;
        }
      });
      if (activeCodex.length > 0) {
        codexPart = `\x1b[33m🤖 codex ×${activeCodex.length}\x1b[0m`; // yellow
      } else {
        codexPart = `\x1b[33m🤖\x1b[0m \x1b[2mnone\x1b[0m`;
      }
    } catch (_) {
      codexPart = `\x1b[33m🤖\x1b[0m \x1b[2mnone\x1b[0m`;
    }

    const agentLine = `${agentsPart} \x1b[2m│\x1b[0m ${codexPart}`;

    // Line 3 — tool activity (always shown, even when idle)
    let toolLine = "";
    try {
      const toolsDir = path.join(workspace?.current_dir || process.cwd(), ".claude/state/tools");
      const toolFiles = fs.readdirSync(toolsDir).filter((f) => f.endsWith(".json"));
      const TOOL_MAX_AGE_MS = 30 * 1000;
      const activeTools = toolFiles
        .flatMap((f) => {
          try {
            const t = JSON.parse(fs.readFileSync(path.join(toolsDir, f), "utf8"));
            if (!t.since || now - new Date(t.since).getTime() > TOOL_MAX_AGE_MS) return [];
            return [{ tool: t.tool, count: t.count || 1 }];
          } catch (_) {
            return [];
          }
        })
        .sort((a, b) => a.tool.localeCompare(b.tool));
      if (activeTools.length > 0) {
        const colored = activeTools.map(({ tool: t, count: n }) => {
          const label = n > 1 ? `${t} ×${n}` : t;
          return `${TOOL_COLORS[t] || TOOL_DEFAULT_COLOR}${label}\x1b[0m`;
        });
        toolLine = `\x1b[2m🔧\x1b[0m ${colored.join(" \x1b[2m|\x1b[0m ")}`;
      } else {
        toolLine = `\x1b[2m🔧 none\x1b[0m`;
      }
    } catch (_) {
      toolLine = `\x1b[2m🔧 none\x1b[0m`;
    }

    const line1 = parts.join(" \x1b[2m│\x1b[0m ");
    const lines = [line1, agentLine, toolLine];
    // Append \x1b[K (clear to end of line) after each row so stale characters
    // from a previous longer render don't bleed through (e.g. "⚡56 agents").
    process.stdout.write(lines.map((l) => l + "\x1b[K").join("\n") + "\x1b[K");
  } catch (_) {
    process.stdout.write("\x1b[2m?\x1b[0m");
  }
});
