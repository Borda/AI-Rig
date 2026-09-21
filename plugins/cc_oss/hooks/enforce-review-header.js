#!/usr/bin/env node
// Enforce oss:review's report-delivery boundary before its workflow follow-up.
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

// Sentinel written by SKILL.md Step 2 (`oss-review-report-dir-${CSID}`).
const SENTINEL_PREFIX = "oss-review-report-dir-";
// File the Step 5 consolidator writes into $REPORT_DIR.
const REPORT_FILENAME = "review-report.md";
// Step 2 always builds "$_REPORT_BASE/.reports/review/pr-<N>/run-<NNN>"; requiring
// the marker keeps the hook from acting on a sentinel holding anything else.
const REPORT_DIR_PARTS = [".reports", "review"];
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

/** Path of the first existing report-dir sentinel among `csids`, else null. */
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

/** True when an absolute path's normalized components contain a report directory. */
function isReportPath(value, reportParts) {
  if (typeof value !== "string") return false;
  const api = reportPathApi(value);
  if (!api.isAbsolute(value)) return false;
  const parts = api.normalize(value).split(api.sep).filter(Boolean);
  const normalize = api === path.win32 ? (part) => part.toLowerCase() : (part) => part;
  const marker = reportParts.map(normalize);
  return parts.some(
    (_, index) =>
      index + marker.length < parts.length && marker.every((part, offset) => normalize(parts[index + offset]) === part),
  );
}

/** True when `value` has the shape Step 2 writes: absolute path under .reports/review/. */
function isReviewReportDir(value) {
  return isReportPath(value, REPORT_DIR_PARTS);
}

/**
 * $REPORT_DIR of an in-flight review, or null when the sentinel cannot be
 * trusted to describe one (stale, unreadable, malformed, or already cleaned up).
 */
function activeReportDir(sentinelPath, now) {
  let content;
  try {
    if (now - fs.statSync(sentinelPath).mtimeMs > STALE_MS) return null;
    content = fs.readFileSync(sentinelPath, "utf8");
  } catch (_) {
    return null;
  }
  const reportDir = content.split("\n")[0].trim();
  if (!isReviewReportDir(reportDir)) return null;
  try {
    return fs.statSync(reportDir).isDirectory() ? reportDir : null;
  } catch (_) {
    return null;
  }
}

/** True once the consolidator has written a non-empty review-report.md. */
function reportWritten(reportDir) {
  try {
    return fs.statSync(path.join(reportDir, REPORT_FILENAME)).size > 0;
  } catch (_) {
    return false;
  }
}

/** Reason to deny the AskUserQuestion call, or null to allow it. */
function denyReason(sentinelPath, now) {
  const reportDir = activeReportDir(sentinelPath, now);
  if (!reportDir || reportWritten(reportDir)) return null;
  const reportFile = path.join(reportDir, REPORT_FILENAME);
  return (
    `oss:review report gate — ${reportFile} does not exist, so Step 5 (consolidate) and Step 5b ` +
    "(print report header) have not completed. Go back: spawn the consolidator agent and let it write " +
    `${REPORT_FILENAME}, then Read that file and print its \`---\` header block to the terminal. Call ` +
    "AskUserQuestion only after that header has actually appeared in your response. If the consolidator " +
    "genuinely cannot run, report that failure and block this follow-up; diagnostic/recovery questions remain available."
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
    isReviewReportDir,
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
      if (!isWorkflowFollowUp(data.tool_input, "oss:review")) process.exit(0);

      const sentinel = findSentinel(sentinelDir(), csidCandidates(process.env, data, process.ppid));
      if (!sentinel) process.exit(0);

      let reason = denyReason(sentinel, Date.now());
      const active = activeReportDir(sentinel, Date.now());
      if (!reason && active) {
        const problem = deliveryProblem(path.join(active, REPORT_FILENAME), data.transcript_path);
        if (problem)
          reason = "oss:review report gate — " + problem + ". Diagnostic/recovery questions remain available.";
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
