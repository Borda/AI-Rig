#!/usr/bin/env node
// stats-reader.js — standalone session stats script (not a hook)
//
// PURPOSE
//   Reads Claude Code session JSONL files and ~/.claude/logs/timings.jsonl to produce a
//   per-session summary of token usage (by model), tool call counts, and optional
//   wall-clock timing data.  All analysis is done in plain Node.js from persisted data —
//   no LLM involvement, no network calls.
//
// HOW IT WORKS
//   1. Derive the project slug from cwd (matching Claude Code's own slug convention)
//   2. Discover session JSONL files in ~/.claude/projects/<slug>/
//   3. Filter by --latest, --date, --from/--to, or a bare <session-uuid> positional arg
//   4. Stream-parse each JSONL file with readline; accumulate token counts by model,
//      tool_use counts by tool name, turn count (user messages), and timestamps
//   5. If --timings: read ~/.claude/logs/timings.jsonl, group by session_id, compute
//      per-tool stats (count, total_ms, mean_ms, p95_ms) and error count
//   6. Print one JSON object per matching session to stdout
//
// EXIT CODES
//   0  Success — even if no sessions match (prints nothing)
//   1  Invalid or missing arguments

"use strict";

const fs = require("fs");
const path = require("path");
const readline = require("readline");
const os = require("os");

// ── CLI parsing ──────────────────────────────────────────────────────────────

const args = process.argv.slice(2);

function flagValue(name) {
  const i = args.indexOf(name);
  return i === -1 ? null : args[i + 1] || null;
}

const withTimings = args.includes("--timings");
const totalsOnly = args.includes("--totals-only");
const latestFlag = args.includes("--latest");
const dateFlag = flagValue("--date");
const fromFlag = flagValue("--from");
const toFlag = flagValue("--to");
const uuidArg = args.find((a) => !a.startsWith("-") && /^[0-9a-f]{8}-[0-9a-f]{4}-/.test(a));

if (!latestFlag && !dateFlag && !fromFlag && !uuidArg) {
  process.stderr.write(
    "Usage: stats-reader.js [--latest | <session-uuid> | --date YYYY-MM-DD | --from YYYY-MM-DD [--to YYYY-MM-DD]] [--timings] [--totals-only]\n",
  );
  process.exit(1);
}

// ── Session discovery ────────────────────────────────────────────────────────

const cwd = process.cwd();
// Slug: replace every / and . with - (matches Claude Code's own convention)
const slug = cwd.replace(/[/.]/g, "-");
const projectsDir = path.join(os.homedir(), ".claude", "projects", slug);

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.jsonl$/;

let sessionFiles = [];
try {
  sessionFiles = fs
    .readdirSync(projectsDir)
    .filter((f) => UUID_RE.test(f))
    .map((f) => {
      const full = path.join(projectsDir, f);
      return { name: f, full, mtime: fs.statSync(full).mtimeMs };
    })
    .sort((a, b) => b.mtime - a.mtime); // newest first
} catch (_) {}

function inDateRange(mtime) {
  const d = new Date(mtime).toISOString().slice(0, 10);
  if (dateFlag) return d === dateFlag;
  if (fromFlag && d < fromFlag) return false;
  if (toFlag && d > toFlag) return false;
  return true;
}

let targets;
if (uuidArg) {
  targets = sessionFiles.filter((f) => f.name.startsWith(uuidArg));
} else if (latestFlag) {
  targets = sessionFiles.slice(0, 1);
} else {
  targets = sessionFiles.filter((f) => inDateRange(f.mtime));
}

if (targets.length === 0) process.exit(0);

// ── Timing data (optional) ────────────────────────────────────────────────────

/** @type {Record<string, Array<{tool:string, duration_ms:number, status:string}>>} */
const timingsBySession = {};

if (withTimings) {
  const timingsFile = path.join(os.homedir(), ".claude", "logs", "timings.jsonl");
  try {
    const raw = fs.readFileSync(timingsFile, "utf8");
    for (const line of raw.split("\n")) {
      if (!line.trim()) continue;
      try {
        const t = JSON.parse(line);
        if (!t.session_id) continue;
        if (!timingsBySession[t.session_id]) timingsBySession[t.session_id] = [];
        timingsBySession[t.session_id].push(t);
      } catch (_) {}
    }
  } catch (_) {}
}

function computeTimings(entries) {
  /** @type {Record<string, number[]>} */
  const byTool = {};
  let errors = 0;
  for (const e of entries) {
    const name = e.tool || "unknown";
    if (!byTool[name]) byTool[name] = [];
    byTool[name].push(e.duration_ms);
    if (e.status === "error") errors++;
  }
  /** @type {Record<string, {count:number, total_ms:number, mean_ms:number, p95_ms:number|undefined}>} */
  const result = {};
  for (const [name, ms] of Object.entries(byTool)) {
    const sorted = [...ms].sort((a, b) => a - b);
    const total = ms.reduce((a, b) => a + b, 0);
    const p95idx = Math.min(sorted.length - 1, Math.floor(sorted.length * 0.95));
    result[name] = {
      count: ms.length,
      total_ms: total,
      mean_ms: Math.round(total / ms.length),
      p95_ms: sorted[p95idx],
    };
  }
  result.errors = errors;
  return result;
}

// ── Stream-parse one session ──────────────────────────────────────────────────

const TOKEN_KEYS = ["input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"];

async function parseSession(file) {
  const sessionId = file.name.replace(".jsonl", "");

  /** @type {Record<string, {input_tokens:number,output_tokens:number,cache_creation_input_tokens:number,cache_read_input_tokens:number,total_tokens:number,api_calls:number}>} */
  const models = {};
  /** @type {Record<string, number>} */
  const tool_calls = {};
  let turns = 0;
  let start_ts = null;
  let end_ts = null;
  let version = null;

  const rl = readline.createInterface({
    input: fs.createReadStream(file.full),
    crlfDelay: Infinity,
  });

  for await (const line of rl) {
    if (!line.trim()) continue;
    let o;
    try {
      o = JSON.parse(line);
    } catch (_) {
      continue;
    }

    const ts = o.timestamp;
    if (ts) {
      if (!start_ts || ts < start_ts) start_ts = ts;
      if (!end_ts || ts > end_ts) end_ts = ts;
    }

    if (o.type === "user") {
      turns++;
    } else if (o.type === "assistant") {
      const msg = o.message || {};
      const model = msg.model || "unknown";
      const usage = msg.usage || {};

      if (!models[model]) {
        models[model] = {
          input_tokens: 0,
          output_tokens: 0,
          cache_creation_input_tokens: 0,
          cache_read_input_tokens: 0,
          total_tokens: 0,
          api_calls: 0,
        };
      }
      const m = models[model];
      let rowTotal = 0;
      for (const k of TOKEN_KEYS) {
        const v = usage[k] || 0;
        m[k] += v;
        rowTotal += v;
      }
      m.total_tokens += rowTotal;
      m.api_calls++;

      for (const c of msg.content || []) {
        if (c && c.type === "tool_use" && c.name) {
          tool_calls[c.name] = (tool_calls[c.name] || 0) + 1;
        }
      }
    } else if (o.type === "system" && o.version && !version) {
      version = o.version;
    }
  }

  // Aggregate totals across all models
  const totals = {
    input_tokens: 0,
    output_tokens: 0,
    cache_creation_input_tokens: 0,
    cache_read_input_tokens: 0,
    total_tokens: 0,
    api_calls: 0,
  };
  for (const m of Object.values(models)) {
    for (const k of Object.keys(totals)) totals[k] += m[k] || 0;
  }

  const duration_min = start_ts && end_ts ? Math.round((new Date(end_ts) - new Date(start_ts)) / 6000) / 10 : null;

  if (totalsOnly) {
    return { session_id: sessionId, start_ts, end_ts, duration_min, version, totals };
  }

  const result = { session_id: sessionId, start_ts, end_ts, duration_min, version, models, totals, tool_calls, turns };

  if (withTimings) {
    result.timings = computeTimings(timingsBySession[sessionId] || []);
  }

  return result;
}

// ── Main ─────────────────────────────────────────────────────────────────────

(async () => {
  for (const file of targets) {
    try {
      const result = await parseSession(file);
      process.stdout.write(JSON.stringify(result, null, 2) + "\n");
    } catch (e) {
      process.stderr.write(`Error parsing ${file.name}: ${e.message}\n`);
    }
  }
})();
