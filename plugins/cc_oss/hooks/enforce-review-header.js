#!/usr/bin/env node
// enforce-review-header.js — PreToolUse hook (matcher: AskUserQuestion)
//
// PURPOSE
//   /oss:review must print the consolidated report's `---` header block to the
//   terminal (SKILL.md Step 5b) before Step 7a's follow-up `AskUserQuestion`
//   fires. That ordering was a prose-only mandate, and a real run skipped it
//   entirely: no consolidator spawned, no `review-report.md` written, no header
//   printed — yet `AskUserQuestion` (a hard tool call) still fired, so the user
//   saw an ad-hoc summary instead of the report header. This hook converts the
//   mandate into a structural gate: while an oss:review run is in flight and
//   its report file is absent from disk, `AskUserQuestion` is denied with
//   instructions to finish Step 5 + Step 5b first.
//
// HOW IT WORKS
//   1. Inspect only PreToolUse calls for `AskUserQuestion`; everything else
//      exits 0 with no output (passthrough).
//   2. Resolve CSID the way SKILL.md's bash does (`${CLAUDE_CODE_SESSION_ID:-$PPID}`):
//      env `CLAUDE_CODE_SESSION_ID` first, then the hook payload's `session_id`
//      (same value Claude Code reports to hooks), then `process.ppid` as a
//      best-effort stand-in for bash's `$PPID`. Candidates are tried in order;
//      a candidate that names no sentinel simply does not match.
//   3. Look for `${TMPDIR:-/tmp}/oss-review-report-dir-<CSID>`, written by
//      Step 2 the moment `$REPORT_DIR` exists. No sentinel → allow: either no
//      review is running, or it has not reached Step 2 yet (so review's own
//      Step 0/1 questions and every other skill's questions stay unblocked).
//   4. Sentinel present → read `$REPORT_DIR` from it, require it to look like a
//      review report dir and to exist on disk, then check
//      `$REPORT_DIR/review-report.md`. Present and non-empty → allow. Missing
//      or empty → deny, naming Step 5 / Step 5b in the reason.
//   5. Every can't-tell case (unreadable sentinel, implausible path, vanished
//      report dir, stale sentinel) resolves to allow.
//
//   KNOWN LIMITATION — stale sentinel. A run that dies between Step 2 and
//   Step 5 leaves the sentinel behind with no report, and nothing on disk
//   distinguishes that from a live run that skipped the print. Bounding on the
//   sentinel's mtime (written once, at Step 2) caps the blast radius: past
//   STALE_MS the gate stops firing, so a session can never be permanently
//   unable to ask a question. The reverse cost — a review still running after
//   STALE_MS loses enforcement — only restores the pre-hook status quo.
//   Secondary limitation: hooks are session-wide, so a subagent spawned mid
//   review is gated too. Review subagents are non-interactive by contract
//   (they write files and return envelopes), so this is intended.
//
// EXIT CODES
//   0  always — passthrough (no output) or decision JSON on stdout.
//      Deliberate deviation from the "gatekeeper crash → block" guidance in
//      hook-authoring.md: this gate protects workflow integrity, not a security
//      boundary. Failing closed on a hook bug would strand the session with no
//      way to ask the user anything, while failing open merely restores the
//      prose-only mandate. Matches sentinel-read-allow.js, which also emits a
//      permissionDecision yet exits 0 on any internal error.

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
    "genuinely cannot run, report that failure and stop instead of asking the user."
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

      const sentinel = findSentinel(sentinelDir(), csidCandidates(process.env, data, process.ppid));
      if (!sentinel) process.exit(0);

      const reason = denyReason(sentinel, Date.now());
      if (!reason) {
        // Additive nudge, not a deny — see report-header-table.js for why a
        // missing table rides as additionalContext instead of blocking.
        try {
          const { assistantTextSinceLastUserTurn, hasHeaderTable, tableReminder } = require("./report-header-table.js");
          const text = assistantTextSinceLastUserTurn(data.transcript_path);
          if (text && !hasHeaderTable(text)) {
            process.stdout.write(
              JSON.stringify({
                hookSpecificOutput: {
                  hookEventName: "PreToolUse",
                  permissionDecision: "allow",
                  additionalContext: tableReminder("oss:review", "Step 5b (print report header)"),
                },
              }),
            );
          }
        } catch (_) {
          // Missing/broken copy of the shared detector — fall through to plain allow.
        }
        process.exit(0);
      }

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
