#!/usr/bin/env node
// Enforce oss:resolve's Step 3d boundary: Q1 lists pending items as plain lines,
// or lists top items and type counts beside the full table file past its budget.
// Options normally have no preview; optional plain comparisons must be distinct
// and within the host cap. The full table is printed once before the picker.
// Scope and fail-open behavior remain workflow guards, not a security boundary.
// Only bulk-selection PreToolUse calls are gated. Exit 0 always.

"use strict";

const fs = require("fs");
const path = require("path");

// Sentinel written by resolve SKILL.md Step 1 (`resolve-impl-dir-${CSID}`).
const SENTINEL_PREFIX = "resolve-impl-dir-";
const ITEMS_FILENAME = "action-items.jsonl";
// Run-dir file holding the full table before every selection call.
const TABLE_FILENAME = "action-items-table.md";
// Hard budget for the whole question, including notices and the table path.
const QUESTION_TEXT_MAX_CHARS = 1500;
// Keep named-table reads bounded, matching the legacy file-delivery limit.
const TABLE_MAX_BYTES = 2 * 1024 * 1024;
// Types SKILL.md Step 3d excludes from the pending set; `[done]` = legacy items files.
const NON_PENDING_MARKERS = ["[done]", "[info]"];
// Item `status` keeping an item open; absent = open, any other value (resolved/addressed) = closed.
const OPEN_STATUS = "pending";
// Bulk-action options SKILL.md Step 3d puts in every selection call.
const BULK_LABEL_PREFIXES = ["+all [req]", "+all [suggest]", "all (req + suggest)"];

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

/** True when the AskUserQuestion input is a Step 3d selection call (it carries the bulk-action question). */
function isSelectionCall(toolInput) {
  const questions = toolInput && Array.isArray(toolInput.questions) ? toolInput.questions : [];
  return questions.some((question) => {
    const options = question && Array.isArray(question.options) ? question.options : [];
    const labels = options.map((option) =>
      String((option && option.label) || "")
        .trim()
        .toLowerCase(),
    );
    return BULK_LABEL_PREFIXES.filter((prefix) => labels.some((label) => label.startsWith(prefix))).length >= 2;
  });
}

/** IMPL_DIR named by the first non-empty sentinel among `csids`, else null. */
function implDir(dir, csids) {
  for (const csid of csids) {
    try {
      const value = fs
        .readFileSync(path.join(dir, SENTINEL_PREFIX + csid), "utf8")
        .split("\n")[0]
        .trim();
      if (value && path.isAbsolute(value) && fs.statSync(value).isDirectory()) return value;
      if (value && fs.statSync(path.resolve(value)).isDirectory()) return path.resolve(value);
    } catch (_) {
      // absent / unreadable / not a dir — try the next candidate
    }
  }
  return null;
}

/** Pending and closed selectable records, or null when the source is unreadable or malformed. */
function selectableItems(itemsFile) {
  let content;
  try {
    content = fs.readFileSync(itemsFile, "utf8");
  } catch (_) {
    return null;
  }
  const items = { pending: [], closed: [] };
  for (const line of content.split(/\r?\n/)) {
    if (!line.trim()) continue;
    let item;
    try {
      item = JSON.parse(line);
    } catch (_) {
      return null;
    }
    if (!item || item.id == null) return null;
    const type = String(item.type || "");
    if (NON_PENDING_MARKERS.some((marker) => type.includes(marker))) continue;
    const status = String(item.status || "").trim();
    (status && status !== OPEN_STATUS ? items.closed : items.pending).push({
      ...item,
      id: String(item.id).trim(),
      type,
    });
  }
  const severity = ["security", "critical", "high", "medium", "low", "nit", "nits"];
  const rank = (item) => {
    const number = Number(item.severity);
    if (Number.isInteger(number) && number >= 1 && number <= 5) return 5 - number;
    const index = severity.indexOf(String(item.severity || "").toLowerCase());
    return index < 0 ? severity.length : index;
  };
  items.pending.sort((left, right) => rank(left) - rank(right));
  return items;
}

/**
 * One Set of cell values per Markdown table (header separator required) in `text`.
 * Rows match per line ([ \t], never \s), so a blank line ends a table: two tables never merge into one.
 */
function tableCellSets(text) {
  const tables = (text || "").match(/(?:^[ \t]*\|[^\n]+\|[ \t\r]*$\n?)+/gm) || [];
  const sets = [];
  for (const table of tables) {
    const lines = table.trim().split(/\r?\n/);
    if (lines.length < 3 || !/^\s*\|[\s:|-]+\|\s*$/.test(lines[1])) continue;
    const cells = new Set();
    for (const line of lines.slice(2)) {
      for (const cell of line
        .trim()
        .slice(1, -1)
        .split(/(?<!\\)\|/)) {
        const value = cell.replace(/[`*]/g, "").trim();
        if (value) cells.add(value.replace(/^#/, ""));
      }
    }
    sets.push(cells);
  }
  return sets;
}

/** True when one table in `text` holds a cell for every id in `ids` — rows split across tables never add up. */
function delivers(ids, text) {
  return tableCellSets(text).some((cells) => ids.every((id) => cells.has(id)));
}

/** Plain item lines shown by Q1, never mentions in descriptions, tables or later questions. */
function questionItems(text) {
  return Array.from(text.matchAll(/^#([^\s]+)[ \t]+((?:\[[^\]\r\n]+\])+)[ \t]+([^\r\n]{1,50})$/gm), (match) => ({
    id: match[1],
    type: match[2],
  }));
}

/** Canonical line length used to decide whether the complete pending list fits beside the fixed notices. */
function itemLine(item) {
  const summary = String(item.summary || `Item #${item.id}`)
    .replace(/[\s|]+/g, " ")
    .slice(0, 50);
  return `#${item.id} ${item.type} ${summary}`;
}

/** Exact pending counts by type from the question's fixed Pending line. */
function pendingCountsMatch(text, items) {
  const counts = new Map();
  for (const item of items) counts.set(item.type, (counts.get(item.type) || 0) + 1);
  const line = text.split(/\r?\n/).find((entry) => entry.startsWith("Pending: "));
  if (!line) return false;
  const shown = new Map();
  for (const entry of line.slice(9).split(/\s*·\s*/)) {
    const match = entry.match(/^((?:\[[^\]]+\])+) (\d+)$/);
    if (!match || shown.has(match[1])) return false;
    shown.set(match[1], Number(match[2]));
  }
  return shown.size === counts.size && Array.from(counts).every(([type, count]) => shown.get(type) === count);
}

/** Bounded regular-file read; missing, unreadable and oversized tables cannot authorize fallback. */
function readTableFile(tablePath) {
  try {
    const stat = fs.statSync(tablePath);
    return stat.isFile() && stat.size <= TABLE_MAX_BYTES ? fs.readFileSync(tablePath, "utf8") : null;
  } catch (_) {
    return null;
  }
}

/** Validate Q1's selection context and every optional comparison without reading the transcript. */
function selectionProblem(toolInput, items, tablePath) {
  const questions = toolInput && Array.isArray(toolInput.questions) ? toolInput.questions : [];
  const first = questions[0];
  const text = first && typeof first.question === "string" ? first.question : "";
  const problems = [];
  if (!first || first.multiSelect === true || !isSelectionCall({ questions: [first] }))
    problems.push("put the single-select bulk-action question first (Q1)");
  if (text.length > QUESTION_TEXT_MAX_CHARS) problems.push("question text exceeds 1500 characters");
  const rich = (value) => /\||```|<\/?[a-z][^>]*>/i.test(value);
  if (rich(text)) problems.push("question text must be plain item lines, without a table or HTML");
  const shown = questionItems(text);
  const has = (item) => shown.some((entry) => entry.id === item.id && entry.type === item.type);
  const missing = items.pending.filter((item) => !has(item));
  if (missing.length) {
    // Reserve the actual fixed notices/path before deciding whether a full list exceeds the whole-question budget.
    const fixed = text.split(/\r?\n/).filter((line) => !line.startsWith("#") && !line.startsWith("Pending: "));
    const fullLength = [...items.pending.map(itemLine), ...fixed].join("\n").length;
    const top = shown.filter((entry) => items.pending.some((item) => item.id === entry.id));
    const prefix =
      top.length > 0 &&
      top.every(
        (entry, index) =>
          items.pending[index] && entry.id === items.pending[index].id && entry.type === items.pending[index].type,
      );
    // Skill paths may use forward slashes on native Windows; read only the sentinel-owned table either way.
    const namesTable = [tablePath, tablePath.replace(/\\/g, "/")].some((name) => text.includes(name));
    const table = namesTable ? readTableFile(tablePath) : null;
    const allIds = [...items.pending, ...items.closed].map((item) => item.id);
    if (
      fullLength <= QUESTION_TEXT_MAX_CHARS ||
      !prefix ||
      !pendingCountsMatch(text, items.pending) ||
      !table ||
      !delivers(allIds, table)
    ) {
      problems.push(
        `missing pending items ${missing
          .slice(0, 10)
          .map((item) => `#${item.id}`)
          .join(", ")}; ` +
          "the over-budget form needs the top items in severity order, exact Pending: counts by type, and the complete table file",
      );
    }
  }
  const closedCount = text.match(/^→ (\d+) resolved\/addressed items\b/m);
  if (items.closed.length && (!closedCount || Number(closedCount[1]) !== items.closed.length))
    problems.push(`name the ${items.closed.length} resolved/addressed items by count`);
  const { previewCapProblem } = require("./report-header-table.js");
  const seen = new Set();
  for (const question of questions) {
    for (const option of question && Array.isArray(question.options) ? question.options : []) {
      if (!option || typeof option.preview !== "string") continue;
      if (seen.has(option.preview)) problems.push("identical previews are not allowed");
      seen.add(option.preview);
      const cap = previewCapProblem(option.preview);
      if (cap) problems.push(`option preview exceeds the preview cap (${cap})`);
      if (rich(option.preview)) problems.push("option previews must be plain comparisons, without a table or HTML");
    }
  }
  if (!problems.length) return null;
  return (
    "oss:resolve selection gate — " +
    problems.join("; ") +
    ". Fix the first question text: " +
    "list every pending item as #<id> [<type>] <summary up to 50 chars>, most severe first, within 1500 characters. " +
    `Past that budget, list the top items, Pending: counts by type, and name the complete table file \`${tablePath}\`. ` +
    "Options carry labels and one-line descriptions, with no preview by default. Optional previews must be distinct " +
    "plain comparisons within 2000 characters and 12 rendered lines. Print the full table once as the last text of the reply message that issues the picker, before the call; " +
    "do not reprint it after the answer or on a denied retry. Reply text before the call, thinking, tool stdout, option fields and later questions do not supply Q1's items."
  );
}

module.exports = { csidCandidates, implDir, isSelectionCall, sanitizeCsid, selectableItems, selectionProblem };

if (require.main === module) {
  let raw = "";
  process.stdin.setEncoding("utf8");
  process.stdin.on("data", (d) => (raw += d));
  process.stdin.on("end", () => {
    try {
      const data = JSON.parse(raw);
      if (data.hook_event_name && data.hook_event_name !== "PreToolUse") process.exit(0);
      if (data.tool_name !== "AskUserQuestion" || !isSelectionCall(data.tool_input)) process.exit(0);
      const dir = implDir(sentinelDir(), csidCandidates(process.env, data, process.ppid));
      if (!dir) process.exit(0);
      const items = selectableItems(path.join(dir, ITEMS_FILENAME));
      if (!items || items.pending.length + items.closed.length === 0) process.exit(0);
      const reason = selectionProblem(data.tool_input, items, path.join(dir, TABLE_FILENAME));
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
