#!/usr/bin/env node
// Enforce research:topic's report-delivery boundary before its workflow follow-up.
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

// Sentinel written once the final report path is resolved (`research-topic-report-file-${CSID}`).
const SENTINEL_PREFIX = "research-topic-report-file-";
// Every mode writes "<root>/.reports/research/topic-<...>.md"; requiring the marker
// keeps the hook from acting on a sentinel holding anything else.
const REPORT_DIR_PARTS = [".reports", "research"];
// Enforcement window measured from the sentinel's mtime (see KNOWN LIMITATION).
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

/** CSID candidates in the resolution order the skill's bash uses, deduplicated. */
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

/** Path of the first existing report-file sentinel among `csids`, else null. */
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

/** Select the path implementation that matches one absolute report path. */
function reportPathApi(value) {
  return typeof value === "string" && (/^[A-Za-z]:[\\/]/.test(value) || value.startsWith("\\\\"))
    ? path.win32
    : path.posix;
}

/** True when normalized components retain the `.reports/research/topic-*.md` containment rule. */
function isTopicReportFile(value) {
  if (typeof value !== "string") return false;
  const api = reportPathApi(value);
  if (!api.isAbsolute(value)) return false;
  const parts = api.normalize(value).split(api.sep).filter(Boolean);
  const normalize = api === path.win32 ? (part) => part.toLowerCase() : (part) => part;
  const marker = REPORT_DIR_PARTS.map(normalize);
  return parts.some(
    (_, index) =>
      index + marker.length < parts.length &&
      marker.every((part, offset) => normalize(parts[index + offset]) === part) &&
      normalize(parts[index + marker.length]).startsWith("topic-") &&
      normalize(parts[parts.length - 1]).endsWith(".md"),
  );
}

/**
 * Report path of an in-flight topic run, or null when the sentinel cannot be
 * trusted to describe one (stale, unreadable, malformed, or its output dir gone).
 */
function activeReportFile(sentinelPath, now) {
  let content;
  try {
    if (now - fs.statSync(sentinelPath).mtimeMs > STALE_MS) return null;
    content = fs.readFileSync(sentinelPath, "utf8");
  } catch (_) {
    return null;
  }
  const reportFile = content.split("\n")[0].trim();
  if (!isTopicReportFile(reportFile)) return null;
  // The mode creates `.reports/research/` before writing the sentinel — its absence
  // means the tree moved or was cleaned up, not that the report is merely pending.
  try {
    return fs.statSync(path.dirname(reportFile)).isDirectory() ? reportFile : null;
  } catch (_) {
    return null;
  }
}

/** True once a non-empty report has been written to `reportFile`. */
function reportWritten(reportFile) {
  try {
    return fs.statSync(reportFile).size > 0;
  } catch (_) {
    return false;
  }
}

/** Reason to deny the AskUserQuestion call, or null to allow it. */
function denyReason(sentinelPath, now) {
  const reportFile = activeReportFile(sentinelPath, now);
  if (!reportFile || reportWritten(reportFile)) return null;
  return (
    `research:topic report gate — ${reportFile} does not exist, so the report has not been written and its ` +
    "`---` header cannot have been printed. Go back to the report step of whichever path is running (Step 3 " +
    "single-agent, modes/team.md consolidation, or modes/plan.md Step P3), write the report to that exact " +
    'path, then print its `---` header block to the terminal and mark the "Print report header" task ' +
    "completed. Call AskUserQuestion only after that header has actually appeared in your response. If the " +
    "report genuinely cannot be produced, report that failure and block this follow-up; diagnostic/recovery questions remain available."
  );
}

// ── Exports (test-only; no-op when run as a hook) ─────────────────────────────
// Helpers are exported for unit testing. The require.main guard below keeps the
// stdin main path from running on require (always taken in production).
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    activeReportFile,
    csidCandidates,
    denyReason,
    findSentinel,
    isTopicReportFile,
    reportWritten,
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
      if (!isWorkflowFollowUp(data.tool_input, "research:topic")) process.exit(0);

      const sentinel = findSentinel(sentinelDir(), csidCandidates(process.env, data, process.ppid));
      if (!sentinel) process.exit(0);

      let reason = denyReason(sentinel, Date.now());
      const active = activeReportFile(sentinel, Date.now());
      if (!reason && active) {
        const problem = deliveryProblem(active, data.transcript_path);
        if (problem)
          reason = "research:topic report gate — " + problem + ". Diagnostic/recovery questions remain available.";
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
