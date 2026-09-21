#!/usr/bin/env node
// Enforce foundry:audit's report-delivery boundary before its workflow follow-up.
// Only PreToolUse AskUserQuestion with this workflow's header/known labels is
// gated. Diagnostic/recovery questions remain available. The existing
// session sentinel, path checks, and TTL define whether this run is active.
// Require aggregate.md, valid summary.jsonl, and current-turn delivery of the
// audit heading, exact finding total, and every finding before the fix question.
// Missing/unreadable delivery evidence denies the transition with a recovery
// reason. No active sentinel, expired/implausible state, or malformed hook
// payload passes through. Unexpected hook failures retain the legacy fail-open
// behavior: this is a workflow guard, not a security or UI-rendering guarantee.
// Exit 0 always; stdout is empty for passthrough or contains the denial JSON.

"use strict";

const fs = require("fs");
const path = require("path");

// State dir written by SKILL.md Pre-flight (`audit-state-${CSID}`), holding the
// run-dir file written by Step 3. A directory of small state files, not the flat
// `-${CSID}`-suffixed single file other skills use.
const STATE_DIR_PREFIX = "audit-state-";
const RUN_DIR_SENTINEL = "run-dir";
// File the Step 5 consolidator writes into $RUN_DIR — the orchestrator's
// authoritative input for the gate's severity counts.
const AGGREGATE_FILENAME = "summary.jsonl";
// Step 3 always builds "<base>/.reports/audit/<TIMESTAMP>"; requiring the marker
// keeps the hook from acting on a sentinel holding anything else.
const RUN_DIR_PARTS = [".reports", "audit"];
// Enforcement window measured from the sentinel's mtime (see KNOWN LIMITATION).
// 4h, matching the skill's own preflight-cache TTL (SKILL.md `preflight_ok`,
// 14400s) — a full sweep plus a 5-pass fix-convergence loop legitimately runs
// far longer than a single-PR review, so oss:review's 2h would under-cover it.
const STALE_MS = 4 * 60 * 60 * 1000;
// Verbatim option labels the follow-up gate is required to use (SKILL.md
// §Follow-up gate, "HARD RULE — Fixed option labels"). (a) and (c) are mandatory
// on every firing; matching either is enough to recognise the gate.
const GATE_LABELS = ["fix auto-fixable", "fix all"];

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

/** Path of the first existing run-dir sentinel among `csids`, else null. */
function findSentinel(dir, csids) {
  for (const csid of csids) {
    const candidate = path.join(dir, STATE_DIR_PREFIX + csid, RUN_DIR_SENTINEL);
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
 * Absolute form of the sentinel's stored run dir, or null when it cannot be one.
 *
 * Step 3 passes the relative base `.reports/audit` to make_run_dir.py, so the
 * stored value is normally relative and only means anything against the audit's
 * own working directory.
 */
function resolveRunDir(value, cwd) {
  if (typeof value !== "string" || value === "") return null;
  const api = reportPathApi(value, cwd);
  const base = typeof cwd === "string" && api.isAbsolute(cwd) ? cwd : process.cwd();
  const resolved = api.resolve(base, value);
  return isReportPath(resolved, api, RUN_DIR_PARTS) ? resolved : null;
}

/**
 * $RUN_DIR of an in-flight audit, or null when the sentinel cannot be trusted to
 * describe one (stale, unreadable, malformed, or already cleaned up).
 */
function activeRunDir(sentinelPath, cwd, now) {
  let content;
  try {
    if (now - fs.statSync(sentinelPath).mtimeMs > STALE_MS) return null;
    content = fs.readFileSync(sentinelPath, "utf8");
  } catch (_) {
    return null;
  }
  const runDir = resolveRunDir(content.split("\n")[0].trim(), cwd);
  if (!runDir) return null;
  try {
    return fs.statSync(runDir).isDirectory() ? runDir : null;
  } catch (_) {
    return null;
  }
}

/** True once Step 5 has written its aggregate and summary, including a valid zero-finding summary. */
function aggregateWritten(runDir) {
  try {
    return (
      fs.statSync(path.join(runDir, AGGREGATE_FILENAME)).isFile() &&
      fs.statSync(path.join(runDir, "aggregate.md")).size > 0
    );
  } catch (_) {
    return false;
  }
}

/**
 * True when `toolInput` is audit's follow-up gate rather than one of the other
 * questions a run legitimately asks (`! BREAKING` acknowledgment, unsupported
 * flag). Recognised by the audit header or verbatim fixed option labels; any unexpected shape
 * reads as "not the gate", keeping the hook fail-open.
 */
function isFollowUpGate(toolInput) {
  const questions = toolInput && Array.isArray(toolInput.questions) ? toolInput.questions : [];
  for (const question of questions) {
    if (question && question.header) {
      if (question.header === "audit") return true;
      continue;
    }
    const options = question && Array.isArray(question.options) ? question.options : [];
    for (const option of options) {
      const label = option && typeof option.label === "string" ? option.label.toLowerCase() : "";
      if (GATE_LABELS.some((known) => label.includes(known))) return true;
    }
  }
  return false;
}

/** Reason to deny the AskUserQuestion call, or null to allow it. */
function denyReason(sentinelPath, toolInput, cwd, now) {
  if (!isFollowUpGate(toolInput)) return null;
  const runDir = activeRunDir(sentinelPath, cwd, now);
  if (!runDir || aggregateWritten(runDir)) return null;
  const aggregateFile = path.join(runDir, AGGREGATE_FILENAME);
  return (
    `foundry:audit report gate — ${aggregateFile} does not exist, so Step 5 (aggregate and classify ` +
    "findings) has not completed and the follow-up gate's severity counts have no source. Go back: spawn " +
    `the foundry:curator consolidator and let it write aggregate.md and ${AGGREGATE_FILENAME}, then read ` +
    "that summary and emit the Step 7 report. Call AskUserQuestion only after those exist. If the " +
    "consolidator genuinely cannot run, keep this fix transition blocked; diagnostic/recovery questions remain available."
  );
}

// ── Exports (test-only; no-op when run as a hook) ─────────────────────────────
// Helpers are exported for unit testing. The require.main guard below keeps the
// stdin main path from running on require (always taken in production).
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    activeRunDir,
    aggregateWritten,
    csidCandidates,
    denyReason,
    findSentinel,
    isFollowUpGate,
    resolveRunDir,
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
      if (!isWorkflowFollowUp(data.tool_input, "foundry:audit")) process.exit(0);

      const sentinel = findSentinel(sentinelDir(), csidCandidates(process.env, data, process.ppid));
      if (!sentinel) process.exit(0);

      let reason = denyReason(sentinel, data.tool_input, data.cwd, Date.now());
      const active = activeRunDir(sentinel, data.cwd, Date.now());
      if (!reason && active) {
        const problem = deliveryProblem(path.join(active, AGGREGATE_FILENAME), data.transcript_path);
        if (problem)
          reason = "foundry:audit report gate — " + problem + ". Diagnostic/recovery questions remain available.";
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
