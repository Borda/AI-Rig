#!/usr/bin/env node
// Enforce oss:resolve's item-selection boundary: the user must see every
// selectable ACTION_ITEMS row on the Step 3d picker's first screen — the Q1
// bulk-action question, whose option previews carry the table.
// Why a hook: conflict-agent output once pushed a reply-text table out of
// view — the user then selected blind (SKILL.md now dispatches Steps 6–7a
// only after this picker, so nothing runs beside it); on 5.5-family models
// reply text written before a tool call can also come back as an empty
// progress update. SKILL.md therefore shows the table once, in the bulk
// options' preview.
// Only PreToolUse AskUserQuestion calls carrying the Step 3d bulk-action
// question are gated; push, recovery and diagnostic questions pass through.
// Scope: the run's `resolve-impl-dir-<CSID>` sentinel and its
// action-items.jsonl. No sentinel, no items file, or no selectable items → pass.
// Gate ids: every pending item id plus every resolved/addressed id — closed
// items are selected only by typing their ids, which the bulk question text
// invites, so the table is the only place the user can read them, in a mixed
// picker and the closed-only call alike.
// Gate: one Markdown table holds every gate id as its own cell — one row per
// item, never a composite row clustering several ids (review-section-taxonomy.md
// §LOW Grouping Rule keeps LOW items unclustered for this reason) — in a place the user
// sees before answering anything — the call's first question (the bulk
// question): its question text alone, or the preview of EVERY option when it is
// single-select (the picker shows only the focused option's preview, so a table
// in some previews only, or in a later question, is unseen). An option
// description never counts: the host renders each option row on one line, its
// line breaks replaced. Each field is also read in its html-preview form,
// wrapper tags removed and entity pipes decoded. Fields are matched one at a
// time, never combined. Reply text is never read: on 5.5-family models text
// written before a tool call can render as an empty progress update, so only
// fields the call itself renders count.
// Preview cap: the host hides an option preview over 2000 chars and clips one
// past the terminal height with no scroll, so a preview delivers only within
// ≤2000 chars and ≤12 rendered lines, a line over 86 chars spanning one line
// per 86 chars since it wraps (report-header-table.js PREVIEW_MAX_*,
// PREVIEW_WRAP_COLUMNS). A table over the cap passes in file form instead: the
// full table in <IMPL_DIR>/action-items-table.md (or another existing file)
// named within the first 2000 chars of Q1's question text and holding every
// gate id, and every option preview a compact summary within the cap naming
// that file. The file form is for a table over the cap only: a named file whose
// own text fits the cap is denied, since that table belongs in every preview,
// where the user sees it without opening a file (sizedFileDelivery).
// Otherwise deny with a reason naming the ids the best table lacks,
// where a misplaced copy sits or what keeps the file form from passing, and the
// exact shape that passes on one retry; the denial never asks for reply text.
// Workflow guard, not a security boundary: unexpected hook errors fail open.
// Exit 0 always.

"use strict";

const fs = require("fs");
const path = require("path");

// Sentinel written by resolve SKILL.md Step 1 (`resolve-impl-dir-${CSID}`).
const SENTINEL_PREFIX = "resolve-impl-dir-";
const ITEMS_FILENAME = "action-items.jsonl";
// Run-dir file SKILL.md Step 3d writes the full item table to when it is over the preview cap.
const TABLE_FILENAME = "action-items-table.md";
// Host cut for question text: only its first chars are shown (report-header-table.js TEXT_MAX_CHARS).
const QUESTION_TEXT_MAX_CHARS = 2000;
// Preview width a line wraps at, stated in the denial (report-header-table.js PREVIEW_WRAP_COLUMNS).
const PREVIEW_WRAP_COLUMNS = 86;
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

/**
 * Selectable item ids (as strings) from action-items.jsonl, split into `pending` and `closed`; null when unreadable
 * or malformed. `[info]`/legacy `[done]` items are in neither: SKILL.md Step 3d never lets the user select them.
 */
function selectableIds(itemsFile) {
  let content;
  try {
    content = fs.readFileSync(itemsFile, "utf8");
  } catch (_) {
    return null;
  }
  const ids = { pending: [], closed: [] };
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
    (status && status !== OPEN_STATUS ? ids.closed : ids.pending).push(String(item.id).trim());
  }
  return ids;
}

/** Pending item ids (as strings) from action-items.jsonl; null when unreadable or malformed. */
function pendingIds(itemsFile) {
  const ids = selectableIds(itemsFile);
  return ids ? ids.pending : null;
}

/**
 * Ids the selection call's table must show, with the noun naming them: every pending id plus every resolved/addressed
 * id. Closed ids are selected only by typing them, which the bulk question text invites in every call holding one, so
 * a table without their rows leaves the user typing blind. Null when nothing is selectable.
 */
function gateIds(itemsFile) {
  const ids = selectableIds(itemsFile);
  if (!ids) return null;
  const kinds = [];
  if (ids.pending.length > 0) kinds.push("pending");
  if (ids.closed.length > 0) kinds.push("resolved/addressed");
  if (kinds.length === 0) return null;
  return { ids: [...ids.pending, ...ids.closed], kind: kinds.join(" and ") };
}

/**
 * Every text field of the call — question text, the question-level `description`, option descriptions and option
 * previews, multiSelect previews included — read only to name what the best table lacks. A table in a field that
 * never delivers (a multiSelect preview, the question-level description) must still count as the full table the
 * misplacement note then names, or the reason would claim no table exists beside a note saying where it sits.
 */
function callFields(toolInput) {
  const questions = toolInput && Array.isArray(toolInput.questions) ? toolInput.questions : [];
  const fields = [];
  for (const question of questions) {
    if (!question) continue;
    fields.push(question.question, question.description);
    for (const option of Array.isArray(question.options) ? question.options : []) {
      if (option) fields.push(option.description, option.preview);
    }
  }
  return fields.filter((text) => typeof text === "string" && text.trim());
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

/** Ids missing from the table that covers the most of them across `sources`; every id when no table exists. */
function missingIds(ids, sources) {
  let missing = ids;
  for (const source of sources) {
    for (const cells of tableCellSets(source)) {
      const gap = ids.filter((id) => !cells.has(id));
      if (gap.length < missing.length) missing = gap;
    }
  }
  return missing;
}

/**
 * Text of the over-cap table file, or null when it is missing, unreadable or not a regular file. Read only to name the
 * ids its table lacks; fileDelivery in report-header-table.js decides whether the file form delivers.
 */
function readTableFile(tablePath) {
  try {
    return fs.statSync(tablePath).isFile() ? fs.readFileSync(tablePath, "utf8") : null;
  } catch (_) {
    return null;
  }
}

/**
 * report-header-table.js fileDelivery for the selection call, judged with the named table file's own size, as
 * `{ ok, file, note, text }`. The file form exists for a table over the preview cap, so once the plain check passes
 * the named file is re-judged with its size (wrapped rows by `helper.previewLineCount`, raw chars): a table that fits
 * is denied with the helper's fits-the-cap remedy. `text` is the named file's text when read, for naming missing ids.
 * A file the first check accepted but this read cannot is no delivery and adds no allow.
 */
function sizedFileDelivery(toolInput, rules, helper) {
  const plain = helper.fileDelivery(toolInput, rules);
  if (!plain.ok) return { ...plain, text: null };
  const text = readTableFile(plain.file);
  if (text === null) return { ok: false, file: null, note: "", text: null };
  const size = { lines: helper.previewLineCount(text), chars: text.length };
  return { ...helper.fileDelivery(toolInput, { ...rules, size }), text };
}

/**
 * Remedy sentence for a file-form attempt fileDelivery leaves unexplained, or "" when Q1's question text never names
 * the run's table file. fileDelivery reasons only about a single-select Q1 whose shown question text (its first
 * QUESTION_TEXT_MAX_CHARS chars) names an existing file some preview names, so each other cause gets its own sentence:
 * Q1 is multiSelect (its previews never render), the name sits only past the shown part, the file does not exist, or
 * no option preview names it. A note blaming the previews for the first two sent the retry to fix the wrong thing.
 */
function tableFileNote(toolInput, tablePath, fileText) {
  const questions = toolInput && Array.isArray(toolInput.questions) ? toolInput.questions : [];
  const first = questions[0];
  const text = first && typeof first.question === "string" ? first.question : "";
  if (!text.includes(TABLE_FILENAME)) return "";
  if (first.multiSelect === true) {
    return (
      `\`${tablePath}\` is named in the question text of Q1, a multiSelect question whose option previews never ` +
      "render, so the summaries naming it are unseen — the bulk-action question is single-select and goes first (Q1). "
    );
  }
  if (!text.slice(0, QUESTION_TEXT_MAX_CHARS).includes(TABLE_FILENAME)) {
    return (
      `\`${tablePath}\` is named in Q1's question text only past its first ${QUESTION_TEXT_MAX_CHARS} chars, which ` +
      "the host cuts — move the `→ Full item table:` line within them. "
    );
  }
  if (fileText === null) {
    return `\`${tablePath}\`, named in the question text, does not exist — write the full table there with the Write tool before the call. `;
  }
  return `\`${tablePath}\` is named in the question text, but every option preview must be a compact summary within the cap that names it. `;
}

/**
 * Reason to deny a selection call the caller already found undelivered, or null when there are no gate ids.
 * `sources` = every field of the call (callFields, multiSelect previews included) and its htmlPreviewText form, plus
 * the over-cap table file's text when it exists, read only to name the ids the best table lacks. `kind` names the gate
 * ids: "pending", "resolved/addressed", or both joined by "and". `held` = why a full table the call holds or names does
 * not deliver — a preview over the cap, a file-form problem, or report-header-table.js misplacementNote ("" when
 * nothing explains it); it is kept whenever non-empty. `tablePath` = the run's over-cap table file, named in the fix.
 */
function denyReason(ids, sources, kind = "pending", held = "", tablePath = TABLE_FILENAME) {
  if (!ids || ids.length === 0) return null;
  const texts = (sources || []).filter((text) => typeof text === "string" && text);
  const missing = missingIds(ids, texts);
  const shown = missing.slice(0, 10).join(", ") + (missing.length > 10 ? ", …" : "");
  const problem =
    missing.length === 0
      ? `this call holds or names a full table, but not where the user sees it before answering. ${held}`
      : `no single table in this call shows ${kind} item(s) ${shown}. ${held}`;
  // Never ask for reply text here: the table is shown once, in the preview or the named file, and a reply-text copy
  // can arrive as an empty progress update on 5.5-family models — asking for one doubled the table or looped on
  // denials. The gate sentences state exactly what this hook checks; the fix states the shape that passes on retry.
  return (
    `oss:resolve selection gate — the user would select blind: ${problem}` +
    `The gate passes once one Markdown table holding every ${kind} id sits in the call's first question — its ` +
    "question text, or the `preview` of every option of that single-select question; an option description renders " +
    "as one line and never counts; rows split across fields, options or tables do not add up. A preview counts only " +
    `within the host's preview cap (≤2000 chars and ≤12 lines, a line over ${PREVIEW_WRAP_COLUMNS} chars counting ` +
    `as one line per ${PREVIEW_WRAP_COLUMNS} chars it spans; the host hides or clips a longer one). A table over the ` +
    `cap passes in file form: the full table in a file named within the first ${QUESTION_TEXT_MAX_CHARS} chars of ` +
    "Q1's question text, and every option's `preview` a compact summary within the cap that names the same file. " +
    "Fix: put the table in the bulk-option preview — make the single-select bulk-action question the call's first " +
    "question (Q1) and set the `preview` of every one of its options to the same full ACTION_ITEMS table (every " +
    `pending row plus every resolved/addressed row, rendered from ${ITEMS_FILENAME}; the compressed table at 19+ ` +
    "pending items) when it fits the cap, as " +
    "oss:resolve Step 3d requires — then issue the corrected AskUserQuestion; html preview hosts: wrap the table " +
    "in `<pre>` with the table on its own lines. " +
    `Over the cap: first write that full table to \`${tablePath}\` with the Write tool, add the line ` +
    `\`→ Full item table: ${tablePath}\` to Q1's question text, within its first ${QUESTION_TEXT_MAX_CHARS} chars, ` +
    "and set every option's `preview` to the same compact summary within the cap (item counts by type and status, " +
    "the highest-severity rows, and that path). " +
    "The table appears once, in that preview or file: add no reply-text copy and do not re-send the unchanged call. " +
    "Reply text, thinking/reasoning, Bash/tool stdout, a later question, an option description, only some options' " +
    "previews, a multiSelect question's preview, a preview over the cap and a row count do not count — the user may " +
    "see none of them before answering."
  );
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    callFields,
    csidCandidates,
    delivers,
    denyReason,
    gateIds,
    implDir,
    isSelectionCall,
    missingIds,
    pendingIds,
    sanitizeCsid,
    selectableIds,
    tableCellSets,
  };
}

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
      const gate = gateIds(path.join(dir, ITEMS_FILENAME));
      if (!gate) process.exit(0);
      const { ids, kind } = gate;
      const {
        cappedQuestionDelivers,
        fileDelivery,
        htmlPreviewText,
        misplacementNote,
        previewCapNote,
        previewLineCount,
      } = require("./report-header-table.js");
      const accepts = (text) => delivers(ids, text);
      // Table where the user sees it before answering — the first question's text, or every option preview of that
      // single-select question, each preview within the cap. Reply text and option descriptions never count.
      if (cappedQuestionDelivers(data.tool_input, accepts)) process.exit(0);
      // Over the cap only: the full table in a file Q1's question text names, every preview a capped summary naming it.
      const tablePath = path.join(dir, TABLE_FILENAME);
      const rules = {
        holds: accepts,
        cwd: data.cwd,
        knownPaths: [tablePath],
        content: `one table with every ${kind} ACTION_ITEMS row`,
      };
      const file = sizedFileDelivery(data.tool_input, rules, { fileDelivery, previewLineCount });
      if (file.ok) process.exit(0);
      const tableText = readTableFile(tablePath);
      const held =
        previewCapNote(data.tool_input) ||
        file.note ||
        tableFileNote(data.tool_input, tablePath, tableText) ||
        misplacementNote(data.tool_input, accepts);
      // Every field of the call — multiSelect previews too, so a full table there reads as misplaced, not missing —
      // also in its html-preview form, plus the table file, read only to name the ids the best table lacks.
      const fields = callFields(data.tool_input).flatMap((field) => [field, htmlPreviewText(field)]);
      if (tableText !== null) fields.push(tableText);
      if (file.text !== null) fields.push(file.text);
      const reason = denyReason(ids, fields, kind, held, tablePath);
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
