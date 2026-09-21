#!/usr/bin/env node
// Enforce foundry:profile's report-delivery boundary before its workflow follow-up.
// Only PreToolUse AskUserQuestion with this workflow's header/known labels is
// gated. Diagnostic/recovery questions remain available. The existing
// session sentinel, path checks, and TTL define whether this run is active.
// Require a nonempty report and delivery of its complete matching header table
// in the parent's transcript before the follow-up; a mere file is insufficient.
// Missing/unreadable delivery evidence denies the transition with a recovery
// reason. No active sentinel, expired/implausible state, or malformed hook
// payload passes through. Unexpected hook failures retain the legacy fail-open
// behavior: this is a workflow guard, not a security or UI-rendering guarantee.
// Exit 0 always; stdout is empty for passthrough or contains the denial JSON.

"use strict";

const fs = require("fs");
const path = require("path");

// Shell-fragment state file written by SKILL.md Step 1
// (`foundry-profile-state-${CSID}`), re-sourced by Steps 2–3.
const SENTINEL_PREFIX = "foundry-profile-state-";
// Key holding the run dir inside that fragment.
const STATE_KEY = "REPORT_DIR";
// File `timing_analyzer.py` writes as its `--output` (Step 2) and Step 4 reads.
const REPORT_FILENAME = "report.md";
// Step 1 always builds ".reports/profile/$STAMP"; requiring the marker keeps the
// hook from acting on a state file holding anything else.
const REPORT_DIR_PARTS = [".reports", "profile"];
// Enforcement window measured from the sentinel's mtime (see KNOWN LIMITATION).
// profile declares no <constants> block; its own longest step is the Step 2
// analyzer at a 60s Bash timeout, and the whole skill is three Bash calls with no
// agent spawns, so 2h (matching oss:review, the shortest sibling window) is
// already orders of magnitude past any legitimate run.
const STALE_MS = 2 * 60 * 60 * 1000;

/** Sentinel base dir — mirrors `${TMPDIR:-/tmp}` used by every skill bash block. */
function sentinelDir() {
  return process.env.TMPDIR || "/tmp";
}

/** Filename-safe CSID token, or null when the candidate cannot name a sentinel. */
function sanitizeCsid(value) {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  return /^[A-Za-z0-9_-]+$/.test(trimmed) ? trimmed : null;
}

/** CSID candidates in the resolution order SKILL.md's bash uses, deduplicated. */
function csidCandidates(env, payload, ppid) {
  const raw = [
    env ? env.CLAUDE_CODE_SESSION_ID : null,
    payload ? payload.session_id : null,
    ppid == null ? null : String(ppid),
  ];
  const out = [];
  for (const candidate of raw) {
    const csid = sanitizeCsid(candidate);
    if (csid && !out.includes(csid)) out.push(csid);
  }
  return out;
}

/** Path of the first existing profile state file among `csids`, else null. */
function findSentinel(dir, csids) {
  for (const csid of csids) {
    const candidate = path.join(dir, SENTINEL_PREFIX + csid);
    try {
      if (fs.statSync(candidate).isFile()) return candidate;
    } catch (_) {
      // absent / unreadable — try the next candidate
    }
  }
  return null;
}

/**
 * Value of `REPORT_DIR` in a `KEY=VALUE` shell fragment, or null when absent.
 *
 * Deliberately mirrors what `.` (source) would bind rather than being maximally
 * permissive — a hook that read a different path than the skill's own shell does
 * would gate the wrong directory. So: leading whitespace and an `export ` prefix
 * are allowed (bash binds both) but whitespace around `=` is not, the last
 * assignment wins (later lines overwrite earlier ones), trailing whitespace is
 * dropped, and one layer of matching surrounding quotes is peeled.
 *
 * Split on `\r?\n`, not `\n`: JS treats `\r` as a line terminator, so a CRLF
 * file leaves a trailing `\r` that `.` cannot match and `$` cannot look past,
 * making every assignment silently unparsable.
 */
function parseStateValue(content, key) {
  if (typeof content !== "string") return null;
  let found = null;
  for (const line of content.split(/\r?\n/)) {
    const match = line.match(/^[ \t]*(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$/);
    if (!match || match[1] !== key) continue;
    let value = match[2].replace(/[ \t]+$/, "");
    const quote = value[0];
    if ((quote === '"' || quote === "'") && value.length >= 2 && value[value.length - 1] === quote) {
      value = value.slice(1, -1);
    }
    found = value;
  }
  return found === null || found === "" ? null : found;
}

/** Select the path implementation matching a Windows or POSIX sentinel path. */
function reportPathApi(value, cwd) {
  const isWindowsPath = (candidate) =>
    typeof candidate === "string" && (/^[A-Za-z]:[\\/]/.test(candidate) || candidate.startsWith("\\\\"));
  return isWindowsPath(value) || isWindowsPath(cwd) ? path.win32 : path.posix;
}

/** True when normalized path components contain a descendant of the expected report directory. */
function isReportPath(value, api, reportParts) {
  const parts = api.normalize(value).split(api.sep).filter(Boolean);
  const normalize = api === path.win32 ? (part) => part.toLowerCase() : (part) => part;
  const marker = reportParts.map(normalize);
  return parts.some(
    (_, index) =>
      index + marker.length < parts.length && marker.every((part, offset) => normalize(parts[index + offset]) === part),
  );
}

/**
 * Absolute form of the state file's stored report dir, or null when it cannot be
 * one.
 *
 * Step 1 assigns the relative `.reports/profile/$STAMP`, so the stored value is
 * normally relative and only means anything against the profile's own working
 * directory.
 */
function resolveReportDir(value, cwd) {
  if (typeof value !== "string" || value === "") return null;
  const api = reportPathApi(value, cwd);
  const base = typeof cwd === "string" && api.isAbsolute(cwd) ? cwd : process.cwd();
  const resolved = api.resolve(base, value);
  return isReportPath(resolved, api, REPORT_DIR_PARTS) ? resolved : null;
}

/**
 * $REPORT_DIR of an in-flight profile run, or null when the sentinel cannot be
 * trusted to describe one (stale, unreadable, malformed, or already cleaned up).
 */
function activeReportDir(sentinelPath, cwd, now) {
  let content;
  try {
    if (now - fs.statSync(sentinelPath).mtimeMs > STALE_MS) return null;
    content = fs.readFileSync(sentinelPath, "utf8");
  } catch (_) {
    return null;
  }
  const reportDir = resolveReportDir(parseStateValue(content, STATE_KEY), cwd);
  if (!reportDir) return null;
  try {
    return fs.statSync(reportDir).isDirectory() ? reportDir : null;
  } catch (_) {
    return null;
  }
}

/** True once the Step 2 analyzer has written a non-empty report.md. */
function reportWritten(reportDir) {
  try {
    return fs.statSync(path.join(reportDir, REPORT_FILENAME)).size > 0;
  } catch (_) {
    return false;
  }
}

/** Reason to deny the AskUserQuestion call, or null to allow it. */
function denyReason(sentinelPath, cwd, now) {
  const reportDir = activeReportDir(sentinelPath, cwd, now);
  if (!reportDir || reportWritten(reportDir)) return null;
  const reportFile = path.join(reportDir, REPORT_FILENAME);
  return (
    `foundry:profile report gate — ${reportFile} does not exist, so Step 2 (run analyzer) and Step 4 ` +
    "(emit terminal output) have not completed. Go back: run timing_analyzer.py so it writes " +
    `${REPORT_FILENAME}, then Read that file and print its \`---\` header block plus the ` +
    "`→ <path>` line to the terminal. Call AskUserQuestion only after that header has actually " +
    "appeared in your response. If the analyzer found no sessions in the window (exit 1), report that " +
    "and stop — Step 2 authorises no follow-up question on that path."
  );
}

// ── Exports (test-only; no-op when run as a hook) ─────────────────────────────
// Helpers are exported for unit testing. The require.main guard below keeps the
// stdin main path from running on require (always taken in production).
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    activeReportDir,
    csidCandidates,
    denyReason,
    findSentinel,
    parseStateValue,
    reportWritten,
    resolveReportDir,
    sanitizeCsid,
  };
}

// ── Main ──────────────────────────────────────────────────────────────────────

if (require.main === module) {
  let raw = "";
  process.stdin.setEncoding("utf8");
  process.stdin.on("data", (d) => (raw += d));
  process.stdin.on("end", () => {
    try {
      const data = JSON.parse(raw);
      if (data.hook_event_name && data.hook_event_name !== "PreToolUse") process.exit(0);
      if (data.tool_name !== "AskUserQuestion") process.exit(0);
      const { isWorkflowFollowUp, deliveryProblem } = require("./report-header-table.js");
      if (!isWorkflowFollowUp(data.tool_input, "foundry:profile")) process.exit(0);

      const sentinel = findSentinel(sentinelDir(), csidCandidates(process.env, data, process.ppid));
      if (!sentinel) process.exit(0);

      let reason = denyReason(sentinel, data.cwd, Date.now());
      const active = activeReportDir(sentinel, data.cwd, Date.now());
      if (!reason && active) {
        const problem = deliveryProblem(path.join(active, REPORT_FILENAME), data.transcript_path);
        if (problem)
          reason = "foundry:profile report gate — " + problem + ". Diagnostic/recovery questions remain available.";
      }
      if (!reason) process.exit(0);

      process.stdout.write(
        JSON.stringify({
          hookSpecificOutput: {
            hookEventName: "PreToolUse",
            permissionDecision: "deny",
            permissionDecisionReason: reason,
          },
        }),
      );
      process.exit(0);
    } catch (_) {
      // Workflow gate, not a security boundary — never strand the session on a hook bug.
      process.exit(0);
    }
  });
}
