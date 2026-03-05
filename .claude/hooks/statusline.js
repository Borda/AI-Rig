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
        const sub = JSON.parse(
          fs.readFileSync(path.join(process.env.HOME || "/tmp", ".claude/state/subscription.json"), "utf8"),
        );
        if (sub.subscriptionType) planName = sub.subscriptionType[0].toUpperCase() + sub.subscriptionType.slice(1);
      } catch (_) {}
    }

    const parts = [];

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
      const pct = Math.max(0, Math.min(100, 100 - remaining));
      const bar = "█".repeat(Math.round(pct / 10)) + "░".repeat(10 - Math.round(pct / 10));
      const color = pct < 50 ? 32 : pct < 75 ? 33 : 31; // green / yellow / red
      parts.push(`\x1b[${color}m${bar} ${Math.round(pct)}%\x1b[0m`);
    }

    // Active subagents indicator — populated by SubagentStart/SubagentStop hooks in task-log.js
    try {
      const stateFile = path.join(workspace?.current_dir || process.cwd(), ".claude/state/agents.json");
      const allAgents = JSON.parse(fs.readFileSync(stateFile, "utf8"));
      // Safety-net: drop agents stuck > 10 min (SubagentStop didn't fire — crash or hang)
      const MAX_AGE_MS = 10 * 60 * 1000;
      const now = Date.now();
      const agents = allAgents.filter((a) => !a.since || now - new Date(a.since).getTime() < MAX_AGE_MS);
      if (agents.length > 0) {
        const counts = agents.reduce((acc, a) => {
          acc[a.type] = (acc[a.type] || 0) + 1;
          return acc;
        }, {});
        const types = Object.entries(counts)
          .map(([t, n]) => (n > 1 ? `${t} ×${n}` : t))
          .join(", ");
        parts.push(`\x1b[35m⚡ ${agents.length} agent${agents.length > 1 ? "s" : ""} (${types})\x1b[0m`);
      }
    } catch (_) {} // file missing = no active agents

    process.stdout.write(parts.join(" \x1b[2m│\x1b[0m "));
  } catch (_) {
    process.stdout.write("\x1b[2m?\x1b[0m");
  }
});
