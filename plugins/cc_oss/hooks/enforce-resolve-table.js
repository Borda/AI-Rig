#!/usr/bin/env node
// Enforce oss:resolve's item-selection boundary: the user must see every
// pending ACTION_ITEMS row before the Step 3d picker opens.
// Why a hook: SKILL.md already requires the table immediately before the
// picker, but Steps 6–7a conflict resolution runs in the same turn, and its
// output pushed the table out of the reply — the user then selected blind.
// Only PreToolUse AskUserQuestion calls carrying the Step 3d bulk-action
// question are gated; push, recovery and diagnostic questions pass through.
// Scope: the run's `resolve-impl-dir-<CSID>` sentinel and its
// action-items.jsonl. No sentinel, no items file, or no pending items → pass.
// Gate: every pending item id appears as a cell of a Markdown table row in the
// parent's assistant text since the last human turn. Otherwise deny with a
// reason naming the missing ids. Workflow guard, not a security boundary:
// unexpected hook errors fail open. Exit 0 always.

"use strict";

const fs = require("fs");
const path = require("path");

// Sentinel written by resolve SKILL.md Step 1 (`resolve-impl-dir-${CSID}`).
const SENTINEL_PREFIX = "resolve-impl-dir-";
const ITEMS_FILENAME = "action-items.jsonl";
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

/** Pending item ids (as strings) from action-items.jsonl; null when unreadable or malformed. */
function pendingIds(itemsFile) {
  let content;
  try {
    content = fs.readFileSync(itemsFile, "utf8");
  } catch (_) {
    return null;
  }
  const ids = [];
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
    if (status && status !== OPEN_STATUS) continue;
    ids.push(String(item.id).trim());
  }
  return ids;
}

/** Set of cell values from every Markdown table row (header separator required) in `text`. */
function tableCells(text) {
  const cells = new Set();
  const tables = (text || "").match(/(?:^\s*\|[^\n]+\|\s*$\n?)+/gm) || [];
  for (const table of tables) {
    const lines = table.trim().split(/\r?\n/);
    if (lines.length < 3 || !/^\s*\|[\s:|-]+\|\s*$/.test(lines[1])) continue;
    for (const line of lines.slice(2)) {
      for (const cell of line
        .trim()
        .slice(1, -1)
        .split(/(?<!\\)\|/)) {
        const value = cell.replace(/[`*]/g, "").trim();
        if (value) cells.add(value.replace(/^#/, ""));
      }
    }
  }
  return cells;
}

/** Pending ids with no table row in `text`. */
function missingIds(ids, text) {
  const cells = tableCells(text);
  return ids.filter((id) => !cells.has(id));
}

/** Reason to deny the selection call, or null to allow it. */
function denyReason(ids, text) {
  if (!ids || ids.length === 0) return null;
  const missing = missingIds(ids, text);
  if (missing.length === 0) return null;
  const shown = missing.slice(0, 10).join(", ") + (missing.length > 10 ? ", …" : "");
  // Visible-text length is evidence for the model: a table written only in thinking leaves it at 0,
  // and without the number the model blamed the hook instead of re-printing (4 denials in a row).
  const visibleChars = (text || "").length;
  return (
    "oss:resolve selection gate — the user would select blind: no table row since the last user turn shows " +
    `pending item(s) ${shown}. Visible reply text found since that turn: ${visibleChars} chars. Print the ` +
    `full ACTION_ITEMS table (every pending row, rendered from ${ITEMS_FILENAME}) as visible reply text now — ` +
    "after any conflict-resolution output — then re-issue this AskUserQuestion in the same turn. " +
    "Thinking/reasoning, Bash/tool stdout and a row count do not count — the user sees none of them. " +
    "Already printed the full table as visible reply text in this same message? Re-issue the identical call once — " +
    "the transcript can lag behind the message being written."
  );
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    csidCandidates,
    denyReason,
    implDir,
    isSelectionCall,
    missingIds,
    pendingIds,
    sanitizeCsid,
    tableCells,
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
      const ids = pendingIds(path.join(dir, ITEMS_FILENAME));
      if (!ids || ids.length === 0) process.exit(0);
      const { assistantTextSinceLastUserTurn, pollUntil } = require("./report-header-table.js");
      // The picker is usually issued in the same message as the table; its text may not be in the transcript yet.
      const text = pollUntil(
        () => assistantTextSinceLastUserTurn(data.transcript_path),
        (current) => missingIds(ids, current).length === 0,
      );
      const reason = denyReason(ids, text);
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
