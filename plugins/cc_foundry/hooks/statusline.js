#!/usr/bin/env node
// statusline.js — Claude Code status line renderer
//
// PURPOSE
//   Renders a two-line live status display in the Claude Code terminal, refreshed
//   on every hook event.  Gives an at-a-glance view of model, cost, context usage,
//   active subagents, running Codex bridge calls and current tool activity.
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
//      liveness signal exists — see line ~228); group by type; color from frontmatter
//   6. Build Line 2 tool segment (🛠️): read state/tools/*.json; skip entries whose last call is
//      older than 30 s, unless a call is still in flight (10-min cap); render per-type call counts
//      with fixed TOOL_COLORS palette
//   7. Write both lines to stdout with \x1b[K (clear-to-end-of-line) on each line
//
// OUTPUT FORMAT
//   Line 1 — session metadata:
//     <model>  <project-dir>  <billing>  <context-bar pct%>  [💬 while processing]
//
//   Line 2 — runtime activity (skills shown only when active; agents/tools always shown):
//     [⚡ <skill> │] 🤖 N <type> [×N], …  │  🛠️ <tools>
//     🧠 codex:<advise|review|implement>[(bg)] [×N] shown only while a bridge_call.py run is active
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
//               SubagentStart/Stop. Groups by type; all agents shown in their
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
//               Freshness is measured from `last` (this type's most recent call) ?? `since`
//               (window start, the only field older hook versions wrote). Filtering on `since`
//               alone blanked the segment mid-work: `since` is a FIXED window start, so calls
//               landing at +25 s and +29 s still carried a `since` that aged past 30 s.
//               A call still in flight (inflight > 0, decremented at PostToolUse) stays shown
//               past that window up to TOOL_INFLIGHT_MAX_AGE_MS, so one slow Bash/test run is
//               visible while it runs; the cap bounds a leaked slot from a missing PostToolUse.
//               "none" at an idle prompt is correct, not a defect — task-log.js clears tools/
//               at Stop and UserPromptSubmit, so the segment is per-turn by design.
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
const crypto = require("crypto");

function getSentinelDir() {
  const user =
    typeof process.getuid === "function"
      ? process.getuid()
      : crypto.createHash("sha256").update(os.userInfo().username).digest("hex");
  const dir = path.join(process.env.TMPDIR || os.tmpdir(), `claude-state-user-${user}`);
  try {
    if (!ownedDirectory(dir)) throw new Error("unsafe state directory");
  } catch (err) {
    if (err.code !== "ENOENT") throw err;
  }
  return dir;
}

/** Remove terminal controls from labels while preserving renderer-owned ANSI codes. */
function stripControl(value) {
  return String(value ?? "")
    .replace(/[\u0000-\u001f\u007f-\u009f\u061c\u202a-\u202e\u2066-\u2069]/g, " ")
    .replace(/[\u200b\ufeff]/g, "");
}

/** Reject linked or foreign-owned state directories before destructive operations. */
function ownedDirectory(dir) {
  const stat = fs.lstatSync(dir);
  return stat.isDirectory() && (typeof process.getuid !== "function" || stat.uid === process.getuid());
}

let raw = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (d) => (raw += d));
process.stdin.on("end", () => {
  try {
    const { model, workspace, context_window, cost, session_id, effort } = JSON.parse(raw);

    // Session-scoped temp dir — mirrors the layout written by task-log.js.
    // Missing identities render model and cost without another session's activity.
    const sid = typeof session_id === "string" && /^[a-zA-Z0-9_-]+$/.test(session_id) ? session_id : null;
    const tmpDir = path.join(
      getSentinelDir(),
      sid ? `claude-state-${sid}` : `.missing-session-${crypto.randomBytes(16).toString("hex")}`,
    );

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

    if (modelName) parts.push(`\x1b[2m${stripControl(modelDisplay)}\x1b[0m`);
    if (dir) parts.push(`\x1b[2m${stripControl(dir)}\x1b[0m`);

    if (isApiKey) {
      // API key billing — every token costs real money, show actual spend
      parts.push(`\x1b[33mAPI $${usd.toFixed(2)}\x1b[0m`); // yellow
    } else {
      // OAuth subscription (Pro / Max) — cost.total_cost_usd is theoretical API-rate
      // cost (tokens × published rates), NOT actual subscription charge or quota consumption.
      // Use /status for actual monthly quota.
      parts.push(`\x1b[36m${stripControl(planName)} ~$${usd.toFixed(2)}\x1b[0m`); // cyan plan + tilde
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
        if (!ownedDirectory(tmpDir) || !ownedDirectory(d)) throw new Error("unsafe state directory");
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
      // Tolerate a missing agents/ dir — a fresh session has none until the first agent starts.
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
            const displayLabel = count > 1 ? `${stripControl(label)}(${count})` : stripControl(label);
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

    // Line 2 — Codex bridge runs (🧠, shown only while one is active). A run is a Bash call to
    // bridge_call.py advise|review|implement, recorded by task-log.js in codex/. Not an agent and not a
    // native tool, so it gets its own segment: `codex:<verb>`, `(bg)` for background or detached runs.
    // Reaping: a detached run follows its job record (<workspace>/.temp/bridge/jobs/<job_id>.json) and drops
    // on a terminal status, and a background Bash run follows the host's task output file: it drops once the
    // file carries `[exited with code N]` (also for a launch that died early) and a file without it keeps the run.
    // Only a run with no readable output file is ended by the bridge's final report in its workspace (each report
    // ends one run, the closest launch of its verb). Every run
    // also drops at an age cap derived from its --timeout-seconds or the verb's default (hard
    // cutoff 1.2x + 9 s per attempt, two attempts for advise/review), the backstop when no signal arrives. A
    // foreground run is capped at the 10-min foreground Bash ceiling, which bounds an orphan left by a call denied
    // or interrupted before PostToolUse.
    let codexPart = "";
    try {
      const codexDir = path.join(tmpDir, "codex");
      // Soft budgets bridge_call.py applies when --timeout-seconds is absent (DEFAULT_TIMEOUTS there).
      const CODEX_DEFAULT_TIMEOUT_S = { advise: 120, review: 300, implement: 600 };
      const CODEX_FOREGROUND_MAX_AGE_MS = 10 * 60 * 1000; // foreground Bash ceiling; no live foreground run is older
      const CODEX_MAX_AGE_CEILING_MS = 60 * 60 * 1000;
      const CODEX_TERMINAL = new Set(["finished", "cancelled", "failed"]);
      const CODEX_VERBS = new Set(["advise", "review", "implement"]);
      // The host closes a task's output file with one of these as its last line: after the process exits, or when the
      // task is stopped (observed with TaskStop). Only a final line counts, never the same text inside the output.
      const CODEX_EXIT_MARKER = /(?:\[exited with code -?\d+\]|\[killed\])\s*$/;
      const CODEX_DETACHED_START_SLACK_MS = 90 * 1000; // supervisor start slack (60 s in the bridge) plus margin
      // Contents of a regular file (at most `max` bytes; the last `max` bytes when `tail`), or null for anything else.
      // Files under a workspace come from the project, so a symlink to a FIFO or a device must never be opened for
      // reading: lstat, O_NOFOLLOW and O_NONBLOCK keep the renderer from blocking on it.
      const readRegular = (file, max, tail = false) => {
        let fd;
        try {
          if (!fs.lstatSync(file).isFile()) return null;
          fd = fs.openSync(
            file,
            fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW || 0) | (fs.constants.O_NONBLOCK || 0),
          );
          const st = fs.fstatSync(fd);
          if (!st.isFile() || (!tail && st.size > max)) return null;
          const len = Math.min(st.size, max);
          const buf = Buffer.alloc(len);
          fs.readSync(fd, buf, 0, len, st.size - len);
          return buf.toString("utf8");
        } catch (_) {
          return null;
        } finally {
          if (fd !== undefined) {
            try {
              fs.closeSync(fd);
            } catch (_) {}
          }
        }
      };
      // Every run record counts, ended or not: an ended run still owns its final report, so a report is never
      // charged to a different run. `over` marks a run that no longer shows.
      const all = [];
      const consumed = new Set(); // run ids of finished foreground runs, recorded by task-log.js
      for (const f of fs.readdirSync(codexDir).filter((cf) => cf.endsWith(".json"))) {
        try {
          const c = JSON.parse(fs.readFileSync(path.join(codexDir, f), "utf8"));
          if (c.kind === "consumed" && typeof c.run_id === "string") consumed.add(c.run_id);
          if (c.kind !== "run" || !CODEX_VERBS.has(c.type)) continue; // legacy Skill-level entries never render
          const launchedMs = Date.parse(c.launched); // background runs: real dispatch time, set at PostToolUse
          c.startMs = Number.isFinite(launchedMs) ? launchedMs : Date.parse(c.since);
          if (!Number.isFinite(c.startMs)) continue;
          const given = Number(c.timeout_s);
          const timeoutS = Number.isFinite(given) && given > 0 ? given : CODEX_DEFAULT_TIMEOUT_S[c.type];
          const attempts = c.type === "implement" ? 1 : 2;
          let maxAge = Math.min(CODEX_MAX_AGE_CEILING_MS, (attempts * (1.2 * timeoutS + 9) + 30) * 1000);
          if (c.detached) maxAge = Math.min(CODEX_MAX_AGE_CEILING_MS, maxAge + CODEX_DETACHED_START_SLACK_MS);
          if (!c.bg) maxAge = Math.min(maxAge, CODEX_FOREGROUND_MAX_AGE_MS);
          c.over = now - c.startMs >= maxAge;
          if (
            !c.over &&
            c.detached &&
            typeof c.job_id === "string" &&
            /^[0-9a-fA-F-]{36}$/.test(c.job_id) &&
            typeof c.workspace === "string" &&
            path.isAbsolute(c.workspace)
          ) {
            const jobFile = path.join(c.workspace, ".temp", "bridge", "jobs", `${c.job_id}.json`);
            let job = null;
            try {
              fs.lstatSync(jobFile);
              const text = readRegular(jobFile, 256 * 1024);
              job = text === null ? null : JSON.parse(text);
            } catch (err) {
              if (err.code === "ENOENT") c.over = true; // record pruned → job is over
            }
            if (job && CODEX_TERMINAL.has(job.status)) c.over = true;
            else if (job) c.followsJob = true; // a live job record is authoritative; no need to look for a report
          }
          // The host creates the task output file at launch and closes it with `[exited with code N]` (after the
          // bridge's JSON envelope) when the process ends, even for a launch that died on an argument error before
          // writing a final report, or with `[killed]` when the task is stopped. A file without a closing marker
          // means the run is still going, whatever else is in it, so it is authoritative: no final report can end
          // the run, the derived age cap no longer applies (only the ceiling), and an ended run's id is marked
          // consumed.
          if (c.bg && !c.detached && Array.isArray(c.output_files)) {
            for (const p of c.output_files.slice(0, 4)) {
              if (typeof p !== "string" || !path.isAbsolute(p) || !p.endsWith(".output")) continue;
              const tail = readRegular(p, 8192, true);
              if (tail === null) continue; // no such file under this temp-base guess
              c.hasOutputFile = true;
              if (CODEX_EXIT_MARKER.test(tail)) {
                c.over = true;
                const runId = /"run_id"\s*:\s*"([0-9a-fA-F-]{36})"/.exec(tail);
                if (runId) consumed.add(runId[1]);
              } else {
                c.over = now - c.startMs >= CODEX_MAX_AGE_CEILING_MS;
              }
              break;
            }
          }
          all.push(c);
        } catch (_) {}
      }
      // A background Bash run has no completion hook, but the bridge writes result-<finish ns>-<id>.json into the
      // run's workspace when Codex ends. Each report ends at most one run: by exact --run-id, else the same-verb run
      // launched closest before the report's start. Reports of detached jobs (job_id) and of finished foreground
      // runs (consumed) belong to other runs and never match.
      const claimants = all.filter(
        (c) => c.bg && !c.detached && typeof c.workspace === "string" && path.isAbsolute(c.workspace),
      );
      for (const c of claimants) {
        try {
          c.wsKey = fs.realpathSync(c.workspace); // one workspace spelled two ways must be one group
        } catch (_) {
          c.wsKey = c.workspace;
        }
      }
      // Reports are only read for a workspace that still holds a run nothing else can place: a run that is still
      // showing and has no output file. Every other run's end is already known.
      const earliestByWorkspace = new Map();
      for (const c of claimants) {
        if (c.over || c.hasOutputFile) continue;
        const earliest = earliestByWorkspace.get(c.wsKey);
        if (earliest === undefined || c.startMs < earliest) earliestByWorkspace.set(c.wsKey, c.startMs);
      }
      for (const [workspace, earliestMs] of earliestByWorkspace) {
        const reports = [];
        try {
          const dir = path.join(workspace, ".temp", "bridge");
          for (const name of fs.readdirSync(dir)) {
            const m = /^result-(\d{10,})-[0-9a-f]+\.json$/.exec(name);
            if (!m || Number(m[1].slice(0, -6)) < earliestMs - 2000) continue; // name carries the finish time in ns
            try {
              const text = readRegular(path.join(dir, name), 256 * 1024);
              if (text === null) continue;
              const r = JSON.parse(text);
              if (r.job_id || consumed.has(r.run_id)) continue;
              reports.push({ verb: r.verb, runId: r.run_id, startedMs: Number(r.started_at) * 1000 });
            } catch (_) {}
          }
        } catch (_) {}
        reports.sort((a, b) => a.startedMs - b.startedMs);
        const runs = claimants.filter((c) => c.wsKey === workspace);
        for (const r of reports) {
          const scored = [];
          for (const c of runs) {
            if (c.matched || c.type !== r.verb) continue;
            const lag = r.startedMs - c.startMs; // the bridge starts shortly after the launch
            if (c.run_id ? c.run_id !== r.runId : lag < -3000 || lag > 20000) continue;
            scored.push({ c, score: c.run_id ? -1 : Math.abs(lag) }); // exact id first, then the closest launch
          }
          scored.sort((a, b) => a.score - b.score);
          // Two launches within half a second of the report's start cannot be told apart: end neither rather than
          // risk hiding the run that is still going (each then waits for its age cap).
          if (!scored.length || (scored.length > 1 && scored[0].score >= 0 && scored[1].score - scored[0].score < 500))
            continue;
          scored[0].c.matched = true;
          if (!scored[0].c.hasOutputFile) scored[0].c.over = true; // a run with a live output file is judged by it
        }
      }
      const groups = new Map();
      for (const c of all) {
        if (c.over) continue;
        const key = `${c.type}${c.bg ? "/bg" : ""}`;
        if (!groups.has(key)) groups.set(key, { type: c.type, bg: !!c.bg, count: 0 });
        groups.get(key).count++;
      }
      if (groups.size > 0) {
        const items = [...groups.values()]
          .sort((a, b) => a.type.localeCompare(b.type) || Number(a.bg) - Number(b.bg))
          .map(({ type, bg, count }) => {
            const label = `codex:${type}${bg ? "(bg)" : ""}${count > 1 ? `×${count}` : ""}`;
            return `\x1b[36m${stripControl(label)}\x1b[0m`; // cyan
          });
        codexPart = `\x1b[36m🧠\x1b[0m ${items.join(", ")}`;
      }
    } catch (_) {}

    // Line 2 — tool activity segment (always shown, even when idle)
    let toolLine = "";
    try {
      const toolsDir = path.join(tmpDir, "tools");
      const toolFiles = fs.readdirSync(toolsDir).filter((f) => f.endsWith(".json"));
      const TOOL_MAX_AGE_MS = 30 * 1000;
      const TOOL_INFLIGHT_MAX_AGE_MS = 10 * 60 * 1000; // backstop for a slot leaked by a missing PostToolUse
      const activeTools = toolFiles
        .flatMap((f) => {
          try {
            const t = JSON.parse(fs.readFileSync(path.join(toolsDir, f), "utf8"));
            // `last` = most recent call; `since` = fixed window start, stale while calls keep landing
            const stamp = t.last || t.since;
            if (!stamp) return [];
            const age = now - new Date(stamp).getTime();
            const inflight = Number(t.inflight) > 0;
            const fresh = age <= TOOL_MAX_AGE_MS || (inflight && age <= TOOL_INFLIGHT_MAX_AGE_MS);
            if (!fresh) return [];
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
          const label = `${stripControl(t)}:${n}x`;
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
          return `\x1b[93m${stripControl(name)}\x1b[0m`; // bright yellow
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
          skillsPart = `\x1b[93m⚡\x1b[0m \x1b[93m${stripControl(name)}\x1b[0m`;
        }
      } catch (_) {}
    }
    if (!skillsPart) skillsPart = `\x1b[93m⚡\x1b[0m \x1b[2mnone\x1b[0m`;

    const line1 = parts.join(" \x1b[2m│\x1b[0m ");
    const line2Parts = [];
    line2Parts.push(skillsPart);
    line2Parts.push(agentsPart);
    if (codexPart) line2Parts.push(codexPart);
    line2Parts.push(toolLine);
    const line2 = line2Parts.join(` \x1b[2m│\x1b[0m `);
    const lines = [line1, line2];
    // \x1b[K clears to end of line — erases stale chars from longer previous renders.
    process.stdout.write(lines.map((l) => l + "\x1b[K").join("\n") + "\x1b[K");
  } catch (_) {
    process.stdout.write("\x1b[2m?\x1b[0m");
  }
});
