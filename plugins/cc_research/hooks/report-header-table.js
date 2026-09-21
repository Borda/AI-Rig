// Shared report-delivery checks for six workflow follow-up hooks.
// Active sentinels scope enforcement to their owning workflow question;
// diagnostic/recovery questions remain usable. A completed report must have
// its current header rendered in one parent-visible table since the last
// human turn. Audit uses its pre-question findings aggregate instead.
// Missing/unreadable delivery evidence denies the transition, not the session.
// Transcript inspection is bounded to 200 KB; earlier output must be reprinted.
// This is a workflow guard, not proof of UI rendering or semantic correctness.
// Canonical copy: cc_foundry; propagate_shared.py distributes identical local
// copies to independently installed cc_oss, cc_develop, and cc_research.

"use strict";

const fs = require("fs");

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

/** Build the `additionalContext` reminder for a skill whose table check failed. */
function tableReminder(skillLabel, printStep) {
  return (
    `${skillLabel} report header gate — the report file exists, but no ` +
    `| Field | Value | table (or the ·-separated fallback line) was found in ` +
    `your reply since the last user turn. quality-gates.md §Report File Format's ` +
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
};

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

/** Check current report content against the parent-visible delivery, not just arbitrary table presence. */
function deliveryProblem(reportFile, transcriptPath) {
  let content;
  try {
    content = fs.readFileSync(reportFile, "utf8");
  } catch (_) {
    return "report is unreadable; recover its producer before following up";
  }
  const text = assistantTextSinceLastUserTurn(transcriptPath);
  if (!text) return "report delivery is unverified; print the report in this turn before following up";
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
      return "current audit findings were not delivered; emit Step 7 before following up";
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
    return "current report header was not delivered; print every header field as a table before following up";
  }
  return null;
}
