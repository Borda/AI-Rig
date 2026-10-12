// Shared report-delivery checks for six workflow follow-up hooks.
// Active sentinels scope enforcement to their owning workflow question;
// diagnostic/recovery questions remain usable. The first question's text must
// show a plain summary: the resolved report path, Title, and Outcome (Verdict
// when no Outcome exists), omitting fields absent from the header. Audit names
// its aggregate and the existing Audit Report headline instead.
// Question text is capped at 1500 characters, leaving headroom below the host's
// 2000-character cut. Optional previews remain subject to the host's preview cap.
// Stop separately requires the full header table in the final reply (the full
// findings report for audit). A shown question never settles that check; Stop
// checks each report version once, allowing one continuation to deliver it.
// Final-reply evidence is the Stop payload's last_assistant_message, else text
// after the transcript's last tool call. Earlier text may be hidden progress.
// Transcript inspection is bounded to 200 KB. Missing/unreadable evidence denies
// the transition, not the session. Value matching preserves saved literals while
// tolerating the existing whole-cell wrappers and inline Markdown.
// This is a workflow guard, not proof of UI rendering or semantic correctness.
// Canonical copy: cc_foundry; propagate_shared.py distributes identical local
// copies to independently installed cc_oss, cc_develop, and cc_research.

"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");

// Claude Code shows "(preview cannot be shown in full …)" instead of an option preview longer than this — measured on
// the raw string (UTF-16 units, html tags included), so none of an over-long preview reaches the user.
const PREVIEW_MAX_CHARS = 2000;
// The preview box shows terminal rows − 26 lines and cannot scroll; 12 lines stay whole on a 38-row terminal.
// Counted as the rows a preview spans in that box (previewLineCount): each line, html `<br>` included, wraps.
const PREVIEW_MAX_LINES = 12;
// Width of the preview box (terminal columns − 34) on the reference terminal both caps assume: 120 × 38, a box of
// 86 × 12. An 80 × 24 terminal cannot be the reference — its box is max(1, 24 − 26) = 1 line, so no 12-line cap holds
// there — and pairing the width with the height cap's own terminal keeps the two bounds consistent; a wider terminal
// only shows more. A line spans ceil(width / 86) rows, at least 1. Width counts code points with trailing whitespace
// removed. Not measured: padding or borders the host's markdown table renderer may add, and double-width glyphs — a
// padded table can still render wider than counted, so this bound is a floor, not a guarantee.
const PREVIEW_WRAP_COLUMNS = 86;
// The host cuts question text after 2000 characters; 1500 keeps headroom.
const QUESTION_MAX_CHARS = 1500;

// Below this many data rows a pipe-table match is treated as noise (a stray
// `|` in prose), not a rendered report header. The smallest report this
// module guards (foundry:profile / research:topic) still carries at least
// this many fields — keep at or below that skill's minimum field count.
const MIN_TABLE_ROWS = 3;

// Bounded tail read, mirroring task-log.js's PreCompact transcript scan:
// the file can grow to many MB over a long session, and only the most
// recent turns matter here.
const TAIL_BYTES = 200 * 1024;

/** Parse one JSONL line to a row object, or null on any failure. */
function parseRow(line) {
  try {
    return JSON.parse(line);
  } catch (_) {
    return null;
  }
}

/** The last `TAIL_BYTES` of the transcript as JSONL lines, or [] when it can't be read. */
function transcriptTailLines(transcriptPath) {
  if (!transcriptPath) return [];
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
    return [];
  }
  return buf.toString("utf8").split("\n").filter(Boolean);
}

/**
 * Concatenated `text` content of the turn's final reply: the orchestrator's assistant text after its last tool call,
 * walking backward from the end of the transcript. The walk stops at any non-sidechain `user` row (a tool_result or
 * a human turn) and at an assistant row holding a `tool_use`, whose own text came before that call. Text written
 * before a tool call never counts — it may be an empty progress update, and focus mode shows only the final message.
 * Returns "" when the transcript can't be read or the final reply is not written yet — callers treat "" exactly like
 * "no table printed" (never a crash), so a lagging transcript never passes on an earlier table.
 */
function finalReplyText(transcriptPath) {
  const lines = transcriptTailLines(transcriptPath);
  const texts = [];
  for (let i = lines.length - 1; i >= 0; i--) {
    const row = parseRow(lines[i]);
    if (!row || row.isSidechain) continue;
    if (row.type === "user") break; // tool_result or human turn — the final reply starts after it
    if (row.type !== "assistant") continue;
    const content = row.message && row.message.content;
    if (!Array.isArray(content)) continue;
    const lastToolUse = content.map((block) => block && block.type).lastIndexOf("tool_use");
    const blocks = content.slice(lastToolUse + 1).reverse();
    for (const block of blocks) {
      if (block && block.type === "text" && typeof block.text === "string") texts.push(block.text);
    }
    if (lastToolUse >= 0) break; // this row ends with the turn's last tool call
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

/**
 * Suffix for a Stop-time delivery problem: how much final reply text the check found. A report "printed" only in
 * thinking leaves it at 0; without the number a model blames the hook and retries instead of printing.
 */
function visibilityNote(text) {
  return (
    ` (final reply text found — after the turn's last tool call: ${(text || "").length} chars; ` +
    "text written before a tool call, thinking/reasoning and tool output may never reach the user and do not count)"
  );
}

/** The call's `questions` array, or [] for any malformed input. */
function callQuestions(toolInput) {
  return toolInput && Array.isArray(toolInput.questions) ? toolInput.questions : [];
}

/** A question's options, malformed entries dropped. */
function questionOptions(question) {
  return Array.isArray(question.options) ? question.options.filter(Boolean) : [];
}

/**
 * Rows one line spans in the reference preview box: ceil(width / PREVIEW_WRAP_COLUMNS), at least 1 so a blank line
 * still takes a row. Width counts code points, trailing whitespace removed.
 */
function wrappedRows(line) {
  const width = Array.from(line.replace(/\s+$/, "")).length;
  return Math.max(1, Math.ceil(width / PREVIEW_WRAP_COLUMNS));
}

/**
 * Rows a preview renders in the reference box: each line (html `<br>` included) as `wrappedRows`, trailing blank
 * lines not counted. A line wider than the box wraps, so twelve wide lines can clip as surely as thirteen short ones.
 */
function previewLineCount(text) {
  // Optional HTML comparisons still need their rendered height measured; no table decoding or delivery matching.
  const plain = text
    .replace(/<br\s*\/?>/gi, "\n")
    .replace(/<\/?(?:pre|code|div|span|p|samp|kbd|tt)\b[^>]*>/gi, "")
    .replace(/\s+$/, "");
  return plain ? plain.split(/\r\n|\r|\n/).reduce((rows, line) => rows + wrappedRows(line), 0) : 0;
}

/**
 * How one option preview breaks the preview cap — "2618 chars", "18 lines", or both — or null when it fits (or is no
 * string). Chars are the raw string's length, the host's own measure, html tags included.
 */
function previewCapProblem(text) {
  if (typeof text !== "string") return null;
  const over = [];
  if (text.length > PREVIEW_MAX_CHARS) over.push(`${text.length} chars`);
  const lines = previewLineCount(text);
  if (lines > PREVIEW_MAX_LINES) over.push(`${lines} lines`);
  return over.length > 0 ? over.join(", ") : null;
}

// Characters that end a path token in question text: whitespace, quotes, and markdown or bracket punctuation.
const PATH_TOKEN_BREAK = /[\s`'"<>|()[\]{},;*]+/;

/** Path-like tokens of `text` — holding a `/` or `\` — with trailing sentence punctuation and a leading arrow removed. */
function pathTokens(text) {
  return text
    .split(PATH_TOKEN_BREAK)
    .map((token) => token.replace(/^[→:]+/, "").replace(/[.:!?]+$/, ""))
    .filter((token) => /[\\/]/.test(token) && /\w/.test(token));
}

/** Real path of an existing file (symlinked dirs such as macOS /var resolved), or null when it is no regular file. */
function existingFile(file) {
  try {
    return fs.statSync(file).isFile() ? fs.realpathSync(file) : null;
  } catch (_) {
    return null;
  }
}

/** Existing file a path token names — absolute, `~/`-relative, or relative to `cwd` or this process — else null. */
function resolvePathToken(token, cwd) {
  let candidates;
  if (/^~[\\/]/.test(token)) candidates = [path.join(os.homedir(), token.slice(2))];
  else if (path.isAbsolute(token)) candidates = [token];
  else
    candidates = [cwd, process.cwd()]
      .filter((base) => typeof base === "string" && base)
      .map((base) => path.resolve(base, token));
  for (const candidate of candidates) {
    const file = existingFile(candidate);
    if (file) return file;
  }
  return null;
}

/**
 * Existing files `text` names, each as `{ token, file }` (the path as written, its real path): path tokens resolved
 * from `cwd`, a relative token that ends one of `knownPaths` (the hook's own `cwd` may differ from the session's), and
 * any of `knownPaths` spelled out in full — a path holding a space is no single token.
 */
function namedFiles(text, cwd, knownPaths) {
  const named = [];
  const add = (token, file) => {
    if (file && !named.some((entry) => entry.file === file)) named.push({ token, file });
  };
  const slashed = (value) => value.replace(/\\/g, "/");
  for (const token of pathTokens(text)) {
    const suffix = "/" + slashed(token).replace(/^\.\//, "");
    const known = knownPaths.find((candidate) => slashed(candidate).endsWith(suffix));
    add(token, resolvePathToken(token, cwd) || (known ? existingFile(known) : null));
  }
  for (const known of knownPaths) if (text.includes(known)) add(known, existingFile(known));
  return named;
}

module.exports = {
  finalReplyText,
  hasHeaderTable,
  MIN_TABLE_ROWS,
  PREVIEW_MAX_CHARS,
  PREVIEW_MAX_LINES,
  PREVIEW_WRAP_COLUMNS,
  QUESTION_MAX_CHARS,
  isWorkflowFollowUp,
  deliveryProblem,
  followUpProblem,
  previewCapProblem,
  previewLineCount,
  questionDeliveryProblem,
  recordFollowUpShown,
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

/** Version stamp (path, mtime, size) of a written report, or null while it is missing or empty. */
function reportStamp(reportFile) {
  try {
    const stats = fs.statSync(reportFile);
    if (!stats.isFile() || stats.size === 0) return null; // producer not done — nothing to deliver yet
    return `${reportFile}\n${stats.mtimeMs}\n${stats.size}`;
  } catch (_) {
    return null;
  }
}

/** PreToolUse checks the first question's summary without settling final-reply delivery. */
function followUpProblem(_sentinelPath, reportFile, payload) {
  return questionDeliveryProblem(reportFile, payload && payload.tool_input, payload && payload.cwd);
}

/** A shown summary never records full report delivery; retain the PostToolUse caller interface. */
function recordFollowUpShown(_sentinelPath, _reportFile, _payload) {
  return false;
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
  const stamp = reportStamp(reportFile);
  if (!stamp) return null;
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
 * Match a headline value inside a plain summary line using the cell matcher’s Markdown tolerance.
 * The expected saved value is never stripped, so literal underscores, stars and backticks survive.
 */
function summaryContains(text, expected) {
  return text.split(/\r?\n/).some((line) => {
    const plain = normalize(line);
    return (
      edgeUnwrapped(plain).some((value) => value.includes(expected)) ||
      normalize(inlineMarkdownText(plain)).includes(expected)
    );
  });
}

/** Check only the first question's plain summary; earlier reply text and option fields never count. */
function questionDeliveryProblem(reportFile, toolInput, cwd) {
  const check = reportDeliveryCheck(reportFile);
  if (check.problem) return check.problem;
  const first = callQuestions(toolInput)[0];
  const text = first && typeof first.question === "string" ? first.question : "";
  const problems = [];
  if (text.length > QUESTION_MAX_CHARS)
    problems.push(`question text exceeds ${QUESTION_MAX_CHARS} characters (${text.length})`);
  if (tableBlocks(text).length || /<\/?[a-z][^>]*>/i.test(text))
    problems.push("use plain text, without a table or HTML");
  const reportReal = existingFile(reportFile);
  // A report in cwd can be named without a slash; other path tokens require one.
  const bareName = text.split(PATH_TOKEN_BREAK).some((token) => {
    const name = token.replace(/^[→:]+/, "").replace(/[.:!?]+$/, "");
    return name === path.basename(reportFile) && resolvePathToken(name, cwd) === reportReal;
  });
  if (!bareName && !namedFiles(text, cwd, [reportFile]).some((entry) => entry.file === reportReal)) {
    problems.push(`name the report file \`${reportFile}\` in the first question's text`);
  }
  for (const [name, value] of check.headlines) {
    if (!summaryContains(text, value)) problems.push(`include \`${name}: ${quoted(value)}\` verbatim`);
  }
  for (const [questionIndex, question] of callQuestions(toolInput).entries()) {
    if (!question) continue;
    for (const [optionIndex, option] of questionOptions(question).entries()) {
      const cap = previewCapProblem(option.preview);
      if (cap)
        problems.push(
          `Its option previews exceed the preview cap (question ${questionIndex + 1}, option ${optionIndex + 1}: ${cap})`,
        );
    }
  }
  if (problems.length === 0) return null;
  return (
    `${check.undelivered} by this question: ${problems.join("; ")}. ` +
    `Keep only the fixed lines (report headline values and report path) in the first question's \`question\` text, ` +
    `at most ${QUESTION_MAX_CHARS} characters, and name the report file \`${reportFile}\`. ` +
    "Option descriptions and previews do not supply the summary; reply text before the call does not count. " +
    "The full report belongs in the final reply."
  );
}

/**
 * Stop-time delivery problem: the current report checked against the turn's final reply, not just arbitrary table
 * presence. `lastAssistantMessage` (Stop payload: the text of the last assistant message) is that reply; without it
 * the transcript's text after the turn's last tool call stands in (`finalReplyText`). Text from earlier in the turn
 * never counts. One read, no wait: a missing table costs at most the one forced continuation per report version that
 * `stopBlockReason` allows.
 */
function deliveryProblem(reportFile, transcriptPath, lastAssistantMessage) {
  const text =
    typeof lastAssistantMessage === "string" && lastAssistantMessage.trim()
      ? lastAssistantMessage
      : finalReplyText(transcriptPath);
  const check = reportDeliveryCheck(reportFile);
  if (check.unreadable) return check.problem;
  if (!text)
    return "report delivery is unverified; print the report in this turn before following up" + visibilityNote(text);
  if (check.problem) return check.problem;
  if (check.delivers(text)) return null;
  const mismatch = check.diagnose([text]);
  return check.missing + (mismatch ? `. ${mismatch.trim()}` : "") + visibilityNote(text);
}

/**
 * The delivery check for a report file — `{ problem }` when it cannot be judged, else the per-report predicates and
 * remedy parts listed at `headerDeliveryCheck`.
 */
function reportDeliveryCheck(reportFile) {
  let content;
  try {
    content = fs.readFileSync(reportFile, "utf8");
  } catch (_) {
    return { problem: "report is unreadable; recover its producer before following up", unreadable: true };
  }
  return reportFile.endsWith("summary.jsonl") ? auditDeliveryCheck(content) : headerDeliveryCheck(content);
}

/** Collapse whitespace runs, so line wrapping and table padding never decide a match. */
function normalize(value) {
  return value.replace(/\s+/g, " ").trim();
}

/** Longest value quoted back in a remedy; a long Summary would otherwise bury the fix. */
const QUOTE_CHARS = 80;

/** At most this many mismatches are listed in one remedy; the rest are counted. */
const MAX_LISTED = 3;

/** `value` shortened for a remedy sentence. */
function quoted(value) {
  return value.length > QUOTE_CHARS ? `${value.slice(0, QUOTE_CHARS)}…` : value;
}

/** `problems` as one remedy clause: the first `MAX_LISTED`, then how many more. */
function listed(problems) {
  const more = problems.length - MAX_LISTED;
  return problems.slice(0, MAX_LISTED).join("; ") + (more > 0 ? `; and ${more} more` : "");
}

/**
 * `text` with paired inline markdown removed anywhere — code spans, `**x**`/`__x__`, `*x*`/`_x_` — one layer per pass
 * until nothing changes; `_x_` inside a word (snake_case) stays. A lone `*`, `_` or backtick stays, but two literal
 * ones in one value pair up once a wrapper is gone (`**x*y*z**` → `xyz`), so `cellMatches` tries whole-cell
 * wrappers first (`edgeUnwrapped`).
 */
function inlineMarkdownText(text) {
  let previous;
  let current = text;
  do {
    previous = current;
    current = current
      .replace(/(`+)(.+?)\1/g, "$2")
      .replace(/(\*\*|__)(?=\S)(.*?\S)\1/g, "$2")
      .replace(/(^|[^\w*])\*(?=\S)(.*?\S)\*(?![\w*])/g, "$1$2")
      .replace(/(^|[^\w_])_(?=\S)(.*?\S)_(?![\w_])/g, "$1$2");
  } while (current !== previous);
  return current;
}

// A whole-cell emphasis wrapper: `**x**`, `__x__`, `*x*` or `_x_` around the entire value.
const EMPHASIS_WRAPPER = /^(\*\*|__|\*|_)(?=\S)([\s\S]*?\S)\1$/;
// A whole-cell code span: one backtick run of any length around the entire value.
const CODE_WRAPPER = /^(`+)([\s\S]*?)\1$/;

/**
 * `cell` followed by each layer left after removing one whole-cell wrapper at a time (code span, bold, italics),
 * normalized. Interior markup is never touched, so a literal `*` or backtick inside the value survives:
 * `**x*y*z**` → `x*y*z` (a glob value), ``` `` `a` b `` ``` → `` `a` b ``.
 */
function edgeUnwrapped(cell) {
  const layers = [cell];
  let current = cell;
  for (;;) {
    const match = current.match(CODE_WRAPPER) || current.match(EMPHASIS_WRAPPER);
    if (!match || !match[2].trim()) return layers;
    current = normalize(match[2]);
    layers.push(current);
  }
}

/**
 * True when a normalized table cell shows `expected` (a normalized header name or value): verbatim, with whole-cell
 * wrappers removed (`edgeUnwrapped`), or with inline markdown removed anywhere (`inlineMarkdownText`) — a bolded
 * verdict or a backticked path renders as the same text. Only the cell is unwrapped, never `expected`, so a cell
 * dropping characters the file holds (`init` for `__init__`) never matches.
 */
function cellMatches(cell, expected) {
  return edgeUnwrapped(cell).includes(expected) || normalize(inlineMarkdownText(cell)) === expected;
}

/** Markdown pipe-table blocks (consecutive `|`-bordered lines) in `text`. */
function tableBlocks(text) {
  return text.match(/(?:^\s*\|[^\n]+\|\s*$\n?)+/gm) || [];
}

/** Normalized cells of each data row of one table block, or null when its second line is no header separator. */
function tableRows(table) {
  const lines = table.trim().split(/\r?\n/);
  if (lines.length < 2 || !/^\s*\|[\s:|-]+\|\s*$/.test(lines[1])) return null;
  return lines.slice(2).map((line) =>
    line
      .trim()
      .slice(1, -1)
      .split(/(?<!\\)\|/)
      .map((cell) => normalize(cell.replace(/\\\|/g, "|"))),
  );
}

/**
 * Audit asks before its final report exists, so delivery binds to the pre-question aggregate instead.
 * Returns `{ problem }` for an unusable aggregate, else the fields listed at `headerDeliveryCheck`. The aggregate
 * supplies the question's report path; the existing Audit Report heading is its summary headline.
 * Stop still requires the heading, exact total and every finding line.
 */
function auditDeliveryCheck(content) {
  let findings;
  try {
    findings = content
      .split(/\r?\n/)
      .filter((line) => line.trim())
      .map((line) => JSON.parse(line));
  } catch (_) {
    return { problem: "audit aggregate is malformed; finish consolidation before following up" };
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
    return { problem: "audit aggregate is incomplete; finish consolidation before following up" };
  }
  const delivers = (text) => {
    const delivered = normalize(text);
    return (
      /\bAudit Report\b/.test(text) &&
      new RegExp(`\\bTotal: ${findings.length}\\b`).test(delivered) &&
      findings.every(
        (finding) =>
          delivered.includes(normalize(finding.one_line)) ||
          delivered.includes(normalize(finding.one_line).replace(/\|/g, "\\|")),
      )
    );
  };
  // What an Audit Report present in `texts` lacks, as one remedy sentence — "" when none is present.
  const diagnose = (texts) => {
    let fewest = null;
    for (const text of texts) {
      if (typeof text !== "string" || !/\bAudit Report\b/.test(text)) continue;
      const delivered = normalize(text);
      const problems = new RegExp(`\\bTotal: ${findings.length}\\b`).test(delivered)
        ? []
        : [`no \`Total: ${findings.length}\` line`];
      for (const finding of findings) {
        const line = normalize(finding.one_line);
        if (!delivered.includes(line) && !delivered.includes(line.replace(/\|/g, "\\|"))) {
          problems.push(`finding \`${quoted(line)}\` is missing`);
        }
      }
      if (problems.length && (!fewest || problems.length < fewest.length)) fewest = problems;
    }
    if (!fewest) return "";
    return (
      `An Audit Report is present but does not match the findings aggregate: ${listed(fewest)}; ` +
      "copy the Total line and every finding line verbatim from the aggregate. "
    );
  };
  const undelivered = "current audit findings were not delivered";
  return {
    delivers,
    diagnose,
    undelivered,
    missing: `${undelivered}; emit Step 7 before following up`,
    headlines: [["headline", "Audit Report"]],
  };
}

/**
 * A `---` header report counts as delivered only when one complete table repeats every field and value.
 * Returns `{ problem }` for an unfinished header, else:
 * - `delivers(text)`: the per-source predicate;
 * - `diagnose(texts)`: near-match remedy — which rows of the closest table differ from the file, "" when no source
 *   holds a table naming any field;
 * - `undelivered` / `missing`: question-time and Stop-time reason prefixes;
 * - `headlines`: present Title and Outcome (Verdict only when no Outcome exists), for the question summary.
 */
function headerDeliveryCheck(content) {
  const header = content.match(/^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)/);
  const fields = header ? Array.from(header[1].matchAll(/^([^:\r\n]+):[ \t]*(.+)$/gm)) : [];
  if (
    fields.length < MIN_TABLE_ROWS ||
    fields.length !== header[1].split(/\r?\n/).length ||
    new Set(fields.map((field) => field[1].trim())).size !== fields.length ||
    fields.some((field) => !field[2].trim())
  ) {
    return { problem: "report header is incomplete; finish the report before following up" };
  }
  const entries = fields.map((field) => [normalize(field[1]), normalize(field[2])]);
  // Match one complete table, so a random table plus raw header prose cannot authorize the transition.
  const matchesTable = (table) => {
    const rows = tableRows(table);
    return (
      rows !== null &&
      entries.every(([name, value]) =>
        rows.some((row) => row.length === 2 && cellMatches(row[0], name) && cellMatches(row[1], value)),
      )
    );
  };
  const delivers = (text) => tableBlocks(text).some(matchesTable);
  // Rows of the table naming the most header fields that differ from the file. Without it a near-match was denied
  // with "no field holds the table", so the model re-sent the same table and was denied again.
  const diagnose = (texts) => {
    let best = null;
    for (const text of texts) {
      if (typeof text !== "string") continue;
      for (const table of tableBlocks(text)) {
        const rows = tableRows(table);
        if (!rows) continue;
        const named = entries.filter(([name]) => rows.some((row) => cellMatches(row[0], name))).length;
        if (named > 0 && (!best || named > best.named)) best = { rows, named };
      }
    }
    if (!best) return "";
    const problems = [];
    for (const [name, value] of entries) {
      const row = best.rows.find((cells) => cellMatches(cells[0], name));
      if (!row) problems.push(`no \`${name}\` row`);
      else if (row.length !== 2)
        problems.push(`the \`${name}\` row splits into ${row.length} cells (write a \`|\` inside a value as \`\\|\`)`);
      else if (!cellMatches(row[1], value))
        problems.push(`\`${name}\` reads \`${quoted(row[1])}\` but the file has \`${quoted(value)}\``);
    }
    if (problems.length === 0) return "";
    return (
      `A \`| Field | Value |\` table is present but does not match the report file: ${listed(problems)}; ` +
      "copy every field name and value verbatim from the file's `---` header. "
    );
  };
  const title = entries.find(([name]) => name === "Title");
  const outcome = entries.find(([name]) => name === "Outcome") || entries.find(([name]) => name === "Verdict");
  return {
    delivers,
    diagnose,
    undelivered: "current report header was not delivered",
    missing: "current report header was not delivered; print every header field as a table before following up",
    headlines: [title, outcome].filter(Boolean),
  };
}
