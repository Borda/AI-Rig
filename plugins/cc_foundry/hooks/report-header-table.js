// Shared report-delivery checks for six workflow follow-up hooks.
// Active sentinels scope enforcement to their owning workflow question;
// diagnostic/recovery questions remain usable. A completed report must have
// its current header rendered in one parent-visible table since the last
// human turn. Audit uses its pre-question findings aggregate instead.
// Missing/unreadable delivery evidence denies the transition, not the session.
// Transcript inspection is bounded to 200 KB; earlier output must be reprinted.
// The same check also runs on Stop (stopBlockReason), because a skipped
// follow-up question would otherwise skip delivery enforcement entirely.
// This is a workflow guard, not proof of UI rendering or semantic correctness.
// Canonical copy: cc_foundry; propagate_shared.py distributes identical local
// copies to independently installed cc_oss, cc_develop, and cc_research.

"use strict";

const fs = require("fs");
const path = require("path");

// Below this many data rows a pipe-table match is treated as noise (a stray
// `|` in prose), not a rendered report header. The smallest report this
// module guards (foundry:profile / research:topic) still carries at least
// this many fields — keep at or below that skill's minimum field count.
const MIN_TABLE_ROWS = 3;

// Bounded tail read, mirroring task-log.js's PreCompact transcript scan:
// the file can grow to many MB over a long session, and only the most
// recent turns matter here.
const TAIL_BYTES = 200 * 1024;

/** True when `content` (a message's `content` field) is NOT a bare tool_result array — i.e. a real human turn. */
function isHumanUserContent(content) {
  if (typeof content === "string") return content.trim().length > 0;
  if (!Array.isArray(content)) return false;
  return content.some((block) => block && block.type !== "tool_result");
}

/** Parse one JSONL line to a row object, or null on any failure. */
function parseRow(line) {
  try {
    return JSON.parse(line);
  } catch (_) {
    return null;
  }
}

/**
 * Concatenated `text` content of every non-sidechain assistant row since the
 * most recent human `user` row, walking backward from end of transcript.
 * Returns "" when the transcript can't be read or no assistant text is found
 * — callers treat "" exactly like "no table printed" (never a crash).
 */
function assistantTextSinceLastUserTurn(transcriptPath) {
  if (!transcriptPath) return "";
  let buf;
  try {
    const stats = fs.statSync(transcriptPath);
    const readSize = Math.min(TAIL_BYTES, stats.size);
    buf = Buffer.alloc(readSize);
    const fd = fs.openSync(transcriptPath, "r");
    try {
      fs.readSync(fd, buf, 0, readSize, stats.size - readSize);
    } finally {
      fs.closeSync(fd);
    }
  } catch (_) {
    return "";
  }

  const lines = buf.toString("utf8").split("\n").filter(Boolean);
  const texts = [];
  for (let i = lines.length - 1; i >= 0; i--) {
    const row = parseRow(lines[i]);
    if (!row) continue;
    if (row.type === "user") {
      const content = row.message && row.message.content;
      if (isHumanUserContent(content)) break; // turn boundary — stop walking back
      continue; // tool_result row — not a boundary
    }
    if (row.type !== "assistant" || row.isSidechain) continue;
    const content = row.message && row.message.content;
    if (!Array.isArray(content)) continue;
    for (const block of content) {
      if (block && block.type === "text" && typeof block.text === "string") {
        texts.push(block.text);
      }
    }
  }
  return texts.reverse().join("\n");
}

/**
 * True when `text` contains either a rendered `| Field | Value |` table with
 * at least `MIN_TABLE_ROWS` data rows, or the documented `·`-separated
 * one-line fallback SKILL.md permits when the report read genuinely fails.
 */
function hasHeaderTable(text) {
  if (!text) return false;
  if (/verdict:.*·.*·/.test(text)) return true; // `·`-fallback line

  const lines = text.split("\n");
  let sawSeparator = false;
  let dataRows = 0;
  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed.startsWith("|")) {
      if (sawSeparator && dataRows >= MIN_TABLE_ROWS) return true;
      sawSeparator = false;
      dataRows = 0;
      continue;
    }
    if (/^\|[\s:|-]+\|$/.test(trimmed)) {
      sawSeparator = true;
      continue;
    }
    if (sawSeparator) dataRows++;
  }
  return sawSeparator && dataRows >= MIN_TABLE_ROWS;
}

// Marker inside every visibilityNote, so callers can tell a delivery problem (worth re-reading the transcript for) from a malformed report.
const VISIBLE_MARKER = "visible reply text found";

// PreToolUse fires before the in-flight assistant message's text reaches the transcript file, so a table printed in the same message
// as the question is missing on the first read (seen: resolve + review, retry with no new text passed). Re-read for this long before denying.
const FLUSH_WAIT_MS = 2000;
const FLUSH_POLL_MS = 250;

/** Flush wait in ms; `CLAUDE_GATE_FLUSH_WAIT_MS` overrides it (tests set 0), any other value falls back to the default. */
function flushWaitMs() {
  const raw = process.env.CLAUDE_GATE_FLUSH_WAIT_MS;
  const ms = raw === undefined || raw === "" ? NaN : Number(raw);
  return Number.isFinite(ms) && ms >= 0 ? ms : FLUSH_WAIT_MS;
}

/** Block the thread for `ms` — hooks are one-shot processes, so a synchronous wait costs nothing. */
function sleepSync(ms) {
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
}

/** `read()` until `isDone(value)` or the flush wait elapses; returns the last value read, done or not. */
function pollUntil(read, isDone) {
  let value = read();
  const deadline = Date.now() + flushWaitMs();
  while (!isDone(value) && Date.now() < deadline) {
    sleepSync(Math.min(FLUSH_POLL_MS, Math.max(1, deadline - Date.now())));
    value = read();
  }
  return value;
}

/**
 * Suffix for a delivery problem: how much visible reply text the check found. A report "printed" only in
 * thinking leaves it at 0; without the number a model blames the hook and retries instead of printing.
 * The last sentence covers the flush lag above, so a model that did print it re-issues instead of re-printing.
 */
function visibilityNote(text) {
  return (
    ` (${VISIBLE_MARKER} since the last user turn: ${(text || "").length} chars; ` +
    "thinking/reasoning and tool output are invisible to the user and do not count. " +
    "Already printed as visible reply text in this same message? Re-issue the identical call once — the transcript can lag; " +
    "otherwise print it as reply text first)"
  );
}

/** Build the `additionalContext` reminder for a skill whose table check failed. */
function tableReminder(skillLabel, printStep) {
  return (
    `${skillLabel} report header gate — the report file exists, but no ` +
    `| Field | Value | table (or the ·-separated fallback line) was found in ` +
    `your visible reply text since the last user turn (thinking does not count). quality-gates.md §Report File Format's ` +
    `Universal terminal-print rule requires the report's --- YAML block to be ` +
    `rendered as a two-column Markdown table, never printed raw. If ${printStep} ` +
    "hasn't happened yet in this reply, do it now, in this same turn, before anything else."
  );
}

module.exports = {
  assistantTextSinceLastUserTurn,
  hasHeaderTable,
  isHumanUserContent,
  tableReminder,
  MIN_TABLE_ROWS,
  isWorkflowFollowUp,
  deliveryProblem,
  pollUntil,
  stopBlockReason,
  deliveredMarkerPath,
};

/**
 * Marker beside a workflow sentinel recording the report version Stop already checked; CSID stays terminal.
 * Skills never delete it: its stamp binds it to one report version, so a leftover marker is inert and
 * shares the sentinel's TMPDIR lifetime.
 */
function deliveredMarkerPath(sentinelPath) {
  return path.join(path.dirname(sentinelPath), "report-delivered-" + path.basename(sentinelPath));
}

/**
 * Stop-event reason to keep the turn going because it is ending with the current report
 * undelivered, or null to let it end. The follow-up question is the PreToolUse gate's only
 * trigger; when a skill skips that question, this is the only delivery check left.
 * Fires at most once per report version (path, mtime, size): the marker is written whether
 * or not delivery passed, so the forced continuation and later human turns never re-block.
 */
function stopBlockReason(sentinelPath, reportFile, payload, skillLabel) {
  if (!sentinelPath || !reportFile || !payload) return null;
  let stamp;
  try {
    const stats = fs.statSync(reportFile);
    if (!stats.isFile() || stats.size === 0) return null; // producer not done — nothing to deliver yet
    stamp = `${reportFile}\n${stats.mtimeMs}\n${stats.size}`;
  } catch (_) {
    return null;
  }
  const marker = deliveredMarkerPath(sentinelPath);
  try {
    if (fs.readFileSync(marker, "utf8") === stamp) return null;
  } catch (_) {
    // no marker yet — first Stop for this report version
  }
  const problem = deliveryProblem(reportFile, payload.transcript_path, payload.last_assistant_message);
  try {
    fs.writeFileSync(marker, stamp);
  } catch (_) {
    if (problem) return null; // cannot record the nudge — never risk re-blocking every turn
  }
  if (!problem || payload.stop_hook_active) return null;
  // Audit delivers its findings aggregate (summary.jsonl), not a `---` header.
  const deliverable = reportFile.endsWith("summary.jsonl")
    ? "the audit findings report (Audit Report heading, Total count, every finding line) is required"
    : "the report header as one two-column `| Field | Value |` table (every field, file order, " +
      "including Path to the full report) comes first and is required";
  return (
    `${skillLabel} report delivery gate — this turn is ending, but ${problem}. ` +
    `Deliver it now, in this reply: ${deliverable}; limitations or other context may follow, kept short. ` +
    "Then end the turn. This check fires once per report."
  );
}

/** Recognize only the documented follow-up question; recovery questions stay usable. */
function isWorkflowFollowUp(toolInput, skill) {
  const questions = toolInput && Array.isArray(toolInput.questions) ? toolInput.questions : [];
  return questions.some((question) => {
    if (!question) return false;
    const labels = (Array.isArray(question.options) ? question.options : []).map((option) =>
      String((option && option.label) || "").toLowerCase(),
    );
    const headers = {
      "oss:review": "oss-review",
      "oss:analyse": "oss-analyse",
      "develop:review": "dev-review",
      "foundry:audit": "audit",
      "foundry:profile": "profile",
      "research:topic": "topic",
    };
    if (question.header) return question.header === headers[skill];
    if (skill === "foundry:audit") {
      return labels.some((label) => label.includes("fix auto-fixable") || label.includes("fix all"));
    }
    const prefixes = {
      "oss:review": ["/oss:resolve", "walk through findings"],
      "oss:analyse": ["/develop:fix", "draft reply", "/oss:analyse", "/oss:review"],
      "develop:review": ["walk through findings"],
      "foundry:profile": ["drill into slowest session", "re-run with different window"],
      "research:topic": ["/research:plan"],
    };
    return labels.some((label) => (prefixes[skill] || []).some((prefix) => label.startsWith(prefix)));
  });
}

/**
 * Check current report content against the parent-visible delivery, not just arbitrary table presence.
 * `lastAssistantMessage` (Stop payload) is appended because the transcript is written asynchronously
 * and may not yet hold the turn's final text.
 */
function deliveryProblem(reportFile, transcriptPath, lastAssistantMessage) {
  const once = () => deliveryProblemOnce(reportFile, transcriptPath, lastAssistantMessage);
  // A Stop payload carries the final message itself, so only the PreToolUse path (no such string) can see transcript lag.
  if (typeof lastAssistantMessage === "string") return once();
  return pollUntil(once, (problem) => !problem || !problem.includes(VISIBLE_MARKER));
}

/** One read of the transcript — see `deliveryProblem`, which adds the flush-lag re-read. */
function deliveryProblemOnce(reportFile, transcriptPath, lastAssistantMessage) {
  let content;
  try {
    content = fs.readFileSync(reportFile, "utf8");
  } catch (_) {
    return "report is unreadable; recover its producer before following up";
  }
  const finalText = typeof lastAssistantMessage === "string" ? lastAssistantMessage : "";
  const text = [assistantTextSinceLastUserTurn(transcriptPath), finalText].filter(Boolean).join("\n");
  if (!text) {
    return "report delivery is unverified; print the report in this turn before following up" + visibilityNote(text);
  }
  const normalize = (value) => value.replace(/\s+/g, " ").trim();
  const delivered = normalize(text);
  if (reportFile.endsWith("summary.jsonl")) {
    // Audit asks before its final report exists; bind to the pre-question aggregate instead.
    let findings;
    try {
      findings = content
        .split(/\r?\n/)
        .filter((line) => line.trim())
        .map((line) => JSON.parse(line));
    } catch (_) {
      return "audit aggregate is malformed; finish consolidation before following up";
    }
    if (
      findings.some(
        (finding) =>
          !finding ||
          typeof finding.one_line !== "string" ||
          !finding.one_line.trim() ||
          !["security", "critical", "high", "medium", "low"].includes(finding.sev),
      )
    ) {
      return "audit aggregate is incomplete; finish consolidation before following up";
    }
    if (
      !/\bAudit Report\b/.test(text) ||
      !new RegExp(`\\bTotal: ${findings.length}\\b`).test(delivered) ||
      findings.some(
        (finding) =>
          !delivered.includes(normalize(finding.one_line)) &&
          !delivered.includes(normalize(finding.one_line).replace(/\|/g, "\\|")),
      )
    ) {
      return "current audit findings were not delivered; emit Step 7 before following up" + visibilityNote(text);
    }
    return null;
  }
  const header = content.match(/^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)/);
  const fields = header ? Array.from(header[1].matchAll(/^([^:\r\n]+):[ \t]*(.+)$/gm)) : [];
  if (
    fields.length < MIN_TABLE_ROWS ||
    fields.length !== header[1].split(/\r?\n/).length ||
    new Set(fields.map((field) => field[1].trim())).size !== fields.length ||
    fields.some((field) => !field[2].trim()) ||
    !fields.some((field) => field[1].trim() === "Title")
  ) {
    return "report header is incomplete; finish the report before following up";
  }
  // Match one complete table, so a random table plus raw header prose cannot authorize the transition.
  const tables = text.match(/(?:^\s*\|[^\n]+\|\s*$\n?)+/gm) || [];
  const matches = tables.some((table) => {
    const lines = table.trim().split(/\r?\n/);
    if (lines.length < 2 || !/^\s*\|[\s:|-]+\|\s*$/.test(lines[1])) return false;
    const rows = lines.slice(2).map((line) =>
      line
        .trim()
        .slice(1, -1)
        .split(/(?<!\\)\|/)
        .map((cell) => normalize(cell.replace(/\\\|/g, "|"))),
    );
    return fields.every((field) =>
      rows.some((row) => row.length === 2 && row[0] === normalize(field[1]) && row[1] === normalize(field[2])),
    );
  });
  if (!matches) {
    return (
      "current report header was not delivered; print every header field as a table before following up" +
      visibilityNote(text)
    );
  }
  return null;
}
