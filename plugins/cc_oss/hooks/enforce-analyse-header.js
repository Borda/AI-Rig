#!/usr/bin/env node
// Enforce oss:analyse's report-delivery boundary before its workflow follow-up.
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

// Sentinel written by SKILL.md Step 1 and rewritten by each mode file
// (`analyse-report-file-${CSID}`).
const SENTINEL_PREFIX = "analyse-report-file-";
// Every mode writes under ".reports/analyse/<thread|vitality|ecosystem>/";
// requiring the marker keeps the hook from acting on a sentinel holding
// anything else — notably the arbitrary path DIRECT_PATH_MODE stores.
const REPORT_DIR_PARTS = [".reports", "analyse"];
// Enforcement window measured from the sentinel's mtime (see KNOWN LIMITATION).
// 2h, matching enforce-review-header.js: the mode files write the sentinel late
// in the run (vitality only reaches Step 4 after data fetch and axis scoring),
// so what has to fit inside the window is report generation plus the review
// passes that follow it, not the whole analyse.
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
 * Absolute form of the sentinel's stored report path, or null when it cannot be
 * one. The modes build ".reports/analyse/…" relative to the repo root, so the
 * stored value only means anything against the analyse's working directory.
 */
function resolveReportFile(value, cwd) {
  if (typeof value !== "string" || value === "") return null;
  const api = reportPathApi(value, cwd);
  const base = typeof cwd === "string" && api.isAbsolute(cwd) ? cwd : process.cwd();
  const resolved = api.resolve(base, value);
  return isReportPath(resolved, api, REPORT_DIR_PARTS) ? resolved : null;
}

/**
 * $REPORT_FILE of an in-flight analyse, or null when the sentinel cannot be
 * trusted to describe one (stale, unreadable, empty, or malformed).
 */
function activeReportFile(sentinelPath, cwd, now) {
  let content;
  try {
    if (now - fs.statSync(sentinelPath).mtimeMs > STALE_MS) return null;
    content = fs.readFileSync(sentinelPath, "utf8");
  } catch (_) {
    return null;
  }
  return resolveReportFile(content.split("\n")[0].trim(), cwd);
}

/** True once the mode has written a non-empty report file. */
function reportWritten(reportFile) {
  try {
    return fs.statSync(reportFile).size > 0;
  } catch (_) {
    return false;
  }
}

/** Reason to deny the AskUserQuestion call, or null to allow it. */
function denyReason(sentinelPath, cwd, now) {
  const reportFile = activeReportFile(sentinelPath, cwd, now);
  if (!reportFile || reportWritten(reportFile)) return null;
  return (
    `oss:analyse report gate — ${reportFile} does not exist, so this run's mode file has not written its ` +
    "report and Step 6a's follow-up question would describe an unsaved analysis. Go back: write the report " +
    "with the Write tool, then Read it and print its `---` header block to the terminal. Call " +
    "AskUserQuestion only after that header has actually appeared in your response. If the report genuinely " +
    "cannot be produced, report that failure and block this follow-up; diagnostic/recovery questions remain available."
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
    reportWritten,
    resolveReportFile,
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
      if (!isWorkflowFollowUp(data.tool_input, "oss:analyse")) process.exit(0);

      const sentinel = findSentinel(sentinelDir(), csidCandidates(process.env, data, process.ppid));
      if (!sentinel) process.exit(0);

      let reason = denyReason(sentinel, data.cwd, Date.now());
      const active = activeReportFile(sentinel, data.cwd, Date.now());
      if (!reason && active) {
        const problem = deliveryProblem(active, data.transcript_path);
        if (problem)
          reason = "oss:analyse report gate — " + problem + ". Diagnostic/recovery questions remain available.";
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
