#!/usr/bin/env node
// statusline.js — Claude Code status line renderer
//
// PURPOSE
//   Renders a two-line live status display in the Claude Code terminal, refreshed
//   on every hook event.  Gives an at-a-glance view of model, cost, context usage,
//   active subagents (incl. codex:* subagents) and current tool activity.
//
// HOW IT WORKS
//   1. Parse stdin JSON for model, workspace, context_window, cost, session_id, and effort
//      (top-level field with `.level` sub-property — e.g. {level: "high"})
//   2. Resolve the per-session temp dir at /tmp/claude-state-<session_id>/ (written by task-log.js)
//   3. Build Line 1: model name, project dir, billing (API key = yellow; OAuth = cyan plan name),
//      context bar (10-char block bar; green <50% · yellow <75% · red ≥75% used), and 💬 [N]
//      badge while Claude is processing the current turn; N shown only when >1 messages queued
//   3a. /clear detection: remaining_percentage === 0 → wipe state/agents/ before rendering
//       so agent badges reset immediately after /clear
//   4. Build Line 2 skills segment (⚡): read state/skills/*.json; render each active skill in
//      bright yellow; shows "none" when idle (consistent with agents/tools segments)
//   5. Build Line 2 agent segment (🤖): read state/agents/*.json; skip entries idle > 10 min
//      (worktree agents, real last_active signal) or > 30 min (non-worktree agents, no per-agent
//      liveness signal exists — see line ~228); group by type; color from frontmatter; codex:* here
//   6. Build Line 2 tool segment (🛠️): read state/tools/*.json; skip entries older than 30 s;
//      render per-type call counts with fixed TOOL_COLORS palette
//   7. Write both lines to stdout with \x1b[K (clear-to-end-of-line) on each line
//
// OUTPUT FORMAT
//   Line 1 — session metadata:
//     <model>  <project-dir>  <billing>  <context-bar pct%>  [💬 while processing]
//
//   Line 2 — runtime activity (skills shown only when active; agents/tools always shown):
//     [⚡ <skill> │] 🤖 N <type> [×N], …  │  🛠️ <tools>
//     codex:* subagents shown in 🤖 alongside other agents
//
// LINE 1 DETAILS
//   model       display_name or id from session JSON, with " (effort.level)" suffix when
//               thinking effort is present (e.g. "claude-sonnet-4-6 (high)"). The hook
//               payload exposes effort as a top-level field with a `.level` sub-property,
//               not nested under model.
//   project-dir basename of workspace.current_dir
//   billing     API key mode  → yellow  "API $X.XX"  (real spend, every token costs)
//               OAuth/sub mode → cyan   "<Plan> ~$X.XX"  (theoretical API-rate cost,
//               NOT actual quota; use /status for real monthly usage)
//   Plan name   priority: CLAUDE_PLAN env var → subscription.json cached at SessionStart
//               by `claude auth status` → fallback "Sub"
//   context bar 10-char block bar; color: green <50% · yellow <75% · red ≥75% used
//
// LINE 2 DETAILS
//   ⚡ skills   reads /tmp/claude-state-<session_id>/skills/*.json written by task-log.js
//               PreToolUse(Skill); deleted by PostToolUse(Skill). Shows active Skill()
//               invocation in bright yellow with plugin: prefix stripped (e.g. "audit" not
//               "foundry:audit"). Shows "none" when idle (consistent with agents/tools).
//               No count shown — only one skill active per session at a time.
//               No age gate — relies on PostToolUse cleanup; SessionEnd wipes any crash remnants.
//   🤖 agents   reads /tmp/claude-state-<session_id>/agents/*.json written by task-log.js
//               SubagentStart/Stop. Groups by type; all agents (incl. codex:*) shown in their
//               declared color (from agent frontmatter color: field); general-purpose gray.
//               Safety-net: ignores entries idle too long, measured from last_active (last tool
//               activity, worktree agents only) ?? since (dispatch time). Worktree agents get a
//               tight 10-min cutoff (last_active is a real per-agent signal). Non-worktree agents
//               (the common case — plain Agent() calls with no isolation:"worktree") have no
//               per-agent liveness signal at all (CC's PreToolUse payload carries no agent_id), so
//               a tight cutoff would hide genuinely still-working agents; they get a 30-min cutoff
//               instead — long enough to cover normal multi-file coding runs. This is a backstop,
//               not the primary reaper: task-log.js's SubagentStart re-keys the record from
//               tool_use_id to agent_id (renameAgentFile) so SubagentStop's unlink matches and
//               removes it promptly on actual completion; the 30-min cutoff only catches entries
//               that never got a SubagentStop at all (crash, dropped session).
//   🛠️ tools    reads /tmp/claude-state-<session_id>/tools/*.json written by task-log.js
//               PreToolUse. Shows tool types active within the last 30 s with per-type
//               call counts. Each tool type has a fixed ANSI color for visual stability.
//               Agent and Task tool calls are excluded (tracked under 🤖 instead).
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

function getSentinelDir() {
  return process.platform === "win32" ? os.tmpdir() : "/tmp";
}

let raw = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (d) => (raw += d));
process.stdin.on("end", () => {
  try {
    const { model, workspace, context_window, cost, session_id, effort } = JSON.parse(raw);

    // Session-scoped temp dir — mirrors the layout written by task-log.js.
    // Falls back to 'default' for older Claude Code versions without session_id.
    const sid = (session_id || "default").replace(/[^a-zA-Z0-9_-]/g, "_");
    const tmpDir = path.join(getSentinelDir(), `claude-state-${sid}`);

    const modelName = model?.display_name || model?.id || "";
    // Thinking effort: hook payload has `effort` as a top-level field with a `.level` sub-property.
    const effortLevel = effort?.level || null;
    const modelDisplay = effortLevel ? `${modelName} (${effortLevel})` : modelName;
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
      red: "\x1b[31m", // used by foundry:challenger
      cyan: "\x1b[36m",
      green: "\x1b[32m",
      indigo: "\x1b[34m", // closest ANSI to indigo — reserved (no agent declares this color)
      lime: "\x1b[92m", // bright green — used by oss:shepherd
      magenta: "\x1b[35m", // used by research:scientist
      orange: "\x1b[33m", // closest ANSI to orange
      pink: "\x1b[95m", // bright magenta
      purple: "\x1b[94m", // bright blue
      teal: "\x1b[96m", // bright cyan — used by linting-expert, perf-optimizer
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

    if (modelName) parts.push(`\x1b[2m${modelDisplay}\x1b[0m`);
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
      // remaining_percentage === 0 means EITHER fresh/just-cleared (total_input_tokens≈0) OR genuinely full (total_input_tokens≫0).
      // Discriminate by token count: <1000 tokens = fresh or /clear → show empty bar (0% used);
      // ≥1000 tokens = context genuinely full → show full bar (100% used).
      const totalTokens = context_window?.total_input_tokens || 0;
      const pct = remaining === 0 && totalTokens < 1000 ? 0 : Math.max(0, Math.min(100, 100 - remaining)); // pct = context used (100 - remaining_pct)
      const filled = Math.round(pct / 10);
      const bar = "█".repeat(filled) + "░".repeat(10 - filled);
      const color = pct < 50 ? 32 : pct < 75 ? 33 : 31; // green / yellow / red
      parts.push(`\x1b[${color}m${bar} ${Math.round(pct)}%\x1b[0m`);
    }

    // Wipe fires on every render while remaining===0 (not one-shot). In practice benign:
    // remaining===0 at session start (before first turn) has no agents yet; after /clear
    // remaining becomes nonzero after the first response. Narrow window, no data loss.
    if (remaining === 0) {
      try {
        const d = path.join(tmpDir, "agents");
        for (const f of fs.readdirSync(d)) {
          try {
            fs.unlinkSync(path.join(d, f));
          } catch (_) {}
        }
      } catch (_) {}
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
      // Tolerate a missing agents/ dir — a session may have codex/ entries but no agents/ yet;
      // an unguarded readdirSync would throw and short-circuit the codex merge below.
      let files = [];
      try {
        files = fs.readdirSync(agentsDir).filter((f) => f.endsWith(".json"));
      } catch (_) {}
      // Safety-net: drop agents idle too long. Freshness is measured from last_active (last tool
      // activity, refreshed by task-log.js for worktree agents only — no agent_id in the tool-event
      // payload means non-worktree agents can never be attributed) falling back to since (dispatch
      // time). A worktree agent with fresh last_active is a real liveness signal → tight 10-min
      // cutoff. A non-worktree agent has no such signal — since is only ever dispatch time, so a
      // still-working 20-30 min conversion/refactor task looks identical to a crashed one; use a
      // 30-min cutoff so normal long-running background agents don't vanish from the badge
      // while they're still working (was 10 min for both — hid genuinely active agents, see
      // .temp/investigate/2026-08-07T16-51-33Z/hypotheses.md; widened to 60 then tightened back
      // to 30 on 2026-09-10 per user request — reassess if this hides active agents again).
      const WORKTREE_MAX_AGE_MS = 10 * 60 * 1000;
      const NON_WORKTREE_MAX_AGE_MS = 30 * 60 * 1000;
      const allAgents = files.flatMap((f) => {
        try {
          return [JSON.parse(fs.readFileSync(path.join(agentsDir, f), "utf8"))];
        } catch (_) {
          return [];
        }
      });
      // codex:* subagents are tracked in a sibling codex/ dir (written by task-log.js on
      // Skill(codex:*)/Agent(codex:*)), not agents/. Merge them so they render in 🤖 as the
      // header comment promises. Dedup by id in case a codex agent also lands in agents/.
      try {
        const seenIds = new Set(allAgents.map((a) => a.id).filter(Boolean));
        const codexDir = path.join(tmpDir, "codex");
        for (const f of fs.readdirSync(codexDir).filter((cf) => cf.endsWith(".json"))) {
          try {
            const c = JSON.parse(fs.readFileSync(path.join(codexDir, f), "utf8"));
            if (c.id && seenIds.has(c.id)) continue;
            allAgents.push({ id: c.id, type: `codex:${c.type}`, model: "codex", color: "cyan", since: c.since });
          } catch (_) {}
        }
      } catch (_) {}
      const agents = allAgents.filter((a) => {
        const freshness = a.last_active || a.since;
        if (!freshness) return true;
        const maxAge = a.last_active ? WORKTREE_MAX_AGE_MS : NON_WORKTREE_MAX_AGE_MS;
        return now - new Date(freshness).getTime() < maxAge;
      });
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
          const label = isGeneral ? model : a.type.replace(/^[^:]+:/, ""); // strip optional plugin: prefix
          // Use agent's declared color (from frontmatter) if available and not gray.
          // Fallback: hash the agent type name to a stable palette color so typed agents
          // never render gray even when their frontmatter file isn't found.
          let ansiColor = "";
          if (!isGray) {
            if (a.color && COLOR_MAP[a.color]) {
              ansiColor = COLOR_MAP[a.color];
            } else {
              const palette = Object.values(COLOR_MAP);
              const hash = [...(a.type || "")].reduce((h, c) => (h * 31 + c.charCodeAt(0)) | 0, 0);
              ansiColor = palette[Math.abs(hash) % palette.length];
            }
          }
          if (!groups.has(key)) groups.set(key, { label, isGray, ansiColor, count: 0 });
          groups.get(key).count++;
        }
        const isSingleGroup = groups.size === 1;
        const items = [...groups.values()]
          .sort((a, b) => b.count - a.count)
          .map(({ label, isGray, ansiColor, count }) => {
            const displayLabel = count > 1 ? `${label}(${count})` : label;
            return isGray ? `\x1b[2m${displayLabel}\x1b[0m` : `${ansiColor}${displayLabel}\x1b[0m`;
          });
        const robotPrefix = isSingleGroup ? `\x1b[35m🤖\x1b[0m` : `\x1b[35m🤖 ${agents.length}\x1b[0m \x1b[2m·\x1b[0m`;
        agentsPart = `${robotPrefix} ${items.join(", ")}`;
      } else {
        agentsPart = `\x1b[35m🤖\x1b[0m \x1b[2mnone\x1b[0m`;
      }
    } catch (_) {
      agentsPart = `\x1b[35m🤖\x1b[0m \x1b[2mnone\x1b[0m`;
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
          const label = `${t}:${n}x`;
          return `${TOOL_COLORS[t] || TOOL_DEFAULT_COLOR}${label}\x1b[0m`;
        });
        toolLine = `\x1b[2m🛠️\x1b[0m ${colored.join(" \x1b[2m·\x1b[0m ")}`;
      } else {
        toolLine = `\x1b[2m🛠️ none\x1b[0m`;
      }
    } catch (_) {
      toolLine = `\x1b[2m🛠️ none\x1b[0m`;
    }

    // Line 2 — skills (always shown; "none" when idle — mirrors agents/tools)
    // Two data sources:
    //   skills/*.json      — tool-call scoped; written by PreToolUse(Skill), deleted by PostToolUse
    //   current-skill.json — turn-persistent; written by UserPromptSubmit for slash commands,
    //                        cleared by next UserPromptSubmit or SessionEnd; survives Stop so
    //                        multi-turn skills (e.g. /oss:resolve) stay visible between turns
    let skillsPart = "";
    try {
      const sDir = path.join(tmpDir, "skills");
      const sFiles = fs.readdirSync(sDir).filter((f) => f.endsWith(".json"));
      const activeSkills = sFiles.flatMap((f) => {
        try {
          return [JSON.parse(fs.readFileSync(path.join(sDir, f), "utf8"))];
        } catch (_) {
          return [];
        }
      });
      if (activeSkills.length > 0) {
        const names = activeSkills.map((s) => {
          // Strip plugin: prefix for brevity (e.g. "foundry:audit" → "audit")
          const name = (s.skill || "?").replace(/^[^:]+:/, "");
          return `\x1b[93m${name}\x1b[0m`; // bright yellow
        });
        skillsPart = `\x1b[93m⚡\x1b[0m ${names.join(", ")}`;
      }
    } catch (_) {}
    // Fallback to current-skill.json when no in-flight Skill() tool call (between turns)
    if (!skillsPart) {
      try {
        const cs = JSON.parse(fs.readFileSync(path.join(tmpDir, "current-skill.json"), "utf8"));
        if (cs.skill) {
          const name = cs.skill.replace(/^[^:]+:/, "");
          skillsPart = `\x1b[93m⚡\x1b[0m \x1b[93m${name}\x1b[0m`;
        }
      } catch (_) {}
    }
    if (!skillsPart) skillsPart = `\x1b[93m⚡\x1b[0m \x1b[2mnone\x1b[0m`;

    const line1 = parts.join(" \x1b[2m│\x1b[0m ");
    const line2Parts = [];
    line2Parts.push(skillsPart);
    line2Parts.push(agentsPart);
    line2Parts.push(toolLine);
    const line2 = line2Parts.join(` \x1b[2m│\x1b[0m `);
    const lines = [line1, line2];
    // \x1b[K clears to end of line — erases stale chars from longer previous renders.
    process.stdout.write(lines.map((l) => l + "\x1b[K").join("\n") + "\x1b[K");
  } catch (_) {
    process.stdout.write("\x1b[2m?\x1b[0m");
  }
});
