// Shared report-delivery checks for six workflow follow-up hooks.
// Active sentinels scope enforcement to their owning workflow question;
// diagnostic/recovery questions remain usable. At question time a completed
// report must be carried by the call's own first question (its question text,
// or — single-select — the preview of every option; never an option
// description, see questionDelivers). Audit uses its pre-question findings
// aggregate instead.
// Why only the call counts: on models that return text written before a tool
// call as a progress-update thinking block (empty under the default display),
// a table printed before the question can never reach the user, so a reply-text
// table in the transcript proves nothing at question time (questionDeliveryProblem
// does not read the transcript).
// Missing/unreadable delivery evidence denies the transition, not the session.
// Stop runs a separate contract (stopBlockReason → deliveryProblem): a skipped
// follow-up question would otherwise skip delivery enforcement entirely, and the
// turn's final reply is reliably shown, so there the final reply counts — the
// Stop payload's last_assistant_message, else the transcript's text after the
// turn's last tool call (finalReplyText). Text written earlier in the turn does
// not: it was followed by a tool call, so it may be an empty progress update, and
// focus mode shows only the final message.
// Transcript inspection is bounded to 200 KB.
// A follow-up the user was shown records the report version on PostToolUse
// (recordFollowUpShown) so Stop does not re-demand it.
// Remedies differ by path: PreToolUse names every option preview of the first
// question (the question carries the report), where a misplaced copy sits, or
// which values of a near-match table differ from the file; Stop names the final
// reply text (no question to carry it).
// Preview cap: Claude Code (2.1.294) shows a placeholder instead of any option
// preview over 2000 chars, clips a taller one to terminal rows − 26 lines with
// no scroll, wraps a line wider than terminal columns − 34, and cuts question
// text and option descriptions after 2000 chars. A preview over
// PREVIEW_MAX_CHARS, or spanning more than PREVIEW_MAX_LINES rows at
// PREVIEW_WRAP_COLUMNS, therefore never delivers (cappedQuestionDelivers).
// Content over the cap is delivered as a file instead (fileDelivery): the
// question text names a file holding the full content, and every option preview
// is a compact summary within the cap naming that path. html-preview hosts
// render host-side, limits unverified: same cap.
// Option descriptions: the picker renders each option row on one line,
// replacing every line break in its description with U+FFFD (the option-row
// mapper passes `displayDescription` through a newline-replacing helper), so a
// table or report there reaches the user as one garbled line — no description
// ever delivers; misplacementNote names one so the copy moves in one retry.
// Value cells match verbatim, with whole-cell wrappers (bold, italics, code
// span) removed, or with inline markdown removed anywhere in the cell; the
// saved value is never altered (cellMatches).
// Hosts with previewFormat "html" reject a preview holding no html tag, so a
// preview is also matched with its wrapper tags removed (htmlPreviewText).
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
// Question text and option descriptions are cut after this many chars ("…" appended), never withheld.
const TEXT_MAX_CHARS = 2000;
// Largest file a question may name as the full copy of over-cap content; anything larger is not read.
const NAMED_FILE_MAX_BYTES = 2 * 1024 * 1024;

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

/**
 * Suffix for a question-time delivery problem: why reply text is no remedy, what the call holds instead (`misplaced`:
 * over-cap previews, see `previewCapNote`; a failed file-shape attempt, see `fileDelivery`; where a misplaced copy
 * sits, see `misplacementNote`; or which values a near-match table gets wrong, see `headerDeliveryCheck`'s
 * `diagnose`), the report's own size against the preview cap, and the two accepted shapes — so a single corrected
 * call passes. Re-issuing the same call cannot help; only the question's own content can.
 */
function questionNote(misplaced, check) {
  const { lines, chars } = check.size;
  const fits = lines <= PREVIEW_MAX_LINES && chars <= PREVIEW_MAX_CHARS;
  return (
    " (reply text before the call does not count: on some models text written before a tool call comes back as an " +
    "empty progress update the user never sees, and thinking/reasoning and tool output are never shown. " +
    (misplaced || "This call's question text, option descriptions and previews do not hold it either. ") +
    `Preview cap: at most ${PREVIEW_MAX_CHARS} chars and ${PREVIEW_MAX_LINES} lines per preview, a line over ` +
    `${PREVIEW_WRAP_COLUMNS} chars counting as one line per ${PREVIEW_WRAP_COLUMNS} chars it spans since it wraps — ` +
    "Claude Code shows a placeholder instead of a longer preview and clips a taller one, with no scroll; " +
    `this ${check.noun} takes at least ${lines} lines and ${chars} chars, ${fits ? "within" : "over"} the cap. ` +
    "Within the cap, pass it, complete, as the `preview` of every option of the call's first question (single-select). " +
    `Over the cap, ${check.target}; name that path in the question text and set the \`preview\` of every option to ` +
    "a compact summary within the cap that includes the same path. Either way not as reply text before the call; " +
    "then call it again; html preview hosts: wrap the table in `<pre>` with the table on its own lines)"
  );
}

// Tags an html-format preview wraps text in without changing its line structure; any attributes are allowed.
const HTML_WRAPPER_TAG = /<\/?(?:pre|code|div|span|p|samp|kbd|tt)\b[^>]*>/gi;

// Entities a model escapes inside an html preview, decoded in one pass so `&amp;#124;` never becomes `|`.
// The optional leading backslash is captured so an escaped pipe entity (`\&#124;`) stays one cell-escape `\|`.
const HTML_ENTITY = /(\\?)&(#124|#x7c|vert|verbar|lt|gt|quot|#39|amp);/gi;
const HTML_ENTITY_TEXT = {
  "#124": "|",
  "#x7c": "|",
  vert: "|",
  verbar: "|",
  lt: "<",
  gt: ">",
  quot: '"',
  "#39": "'",
  amp: "&",
};

/**
 * One line of an html preview with its entities decoded. A pipe entity on a line that also holds a bare `|` is a
 * value inside a cell whose borders are the bare pipes, so it becomes the cell-escape `\|` — decoding it to `|`
 * would split that cell. On a line with no bare `|` the entities are the table's own borders and become `|`.
 */
function decodeLineEntities(line) {
  const bordersAreBare = line.includes("|");
  return line.replace(HTML_ENTITY, (_, backslash, name) => {
    const decoded = HTML_ENTITY_TEXT[name.toLowerCase()];
    if (decoded !== "|") return backslash + decoded;
    return bordersAreBare || backslash ? "\\|" : "|";
  });
}

/**
 * Plain text of an html-format option preview: wrapper tags removed, `<br>` as a line break, entities decoded.
 * With `previewFormat: "html"` the host rejects a preview holding no html tag, so a table arrives `<pre>`-wrapped,
 * possibly inline (`<pre>| … |</pre>`). Text holding no html tag is returned unchanged. A `<table>` element is not
 * converted — the deny remedy names `<pre>` instead.
 */
function htmlPreviewText(text) {
  if (typeof text !== "string" || !/<[a-z][^>]*>/i.test(text)) return text;
  return text
    .replace(/<br\s*\/?>/gi, "\n")
    .replace(HTML_WRAPPER_TAG, "")
    .split("\n")
    .map(decodeLineEntities)
    .join("\n");
}

/**
 * Every string the follow-up question shows the user: question text, the question-level `description` (form-style
 * hosts; see `questionDelivers`), option descriptions, and option previews.
 * Previews count only on single-select questions, the only kind that renders them.
 */
function questionTexts(toolInput) {
  const texts = [];
  for (const question of callQuestions(toolInput)) {
    if (!question) continue;
    texts.push(question.question, question.description);
    for (const option of questionOptions(question)) {
      texts.push(option.description);
      if (question.multiSelect !== true) texts.push(option.preview);
    }
  }
  return texts.filter((text) => typeof text === "string" && text.trim());
}

/** The call's `questions` array, or [] for any malformed input. */
function callQuestions(toolInput) {
  return toolInput && Array.isArray(toolInput.questions) ? toolInput.questions : [];
}

/** A question's options, malformed entries dropped. */
function questionOptions(question) {
  return Array.isArray(question.options) ? question.options.filter(Boolean) : [];
}

/** True when one rendered field holds what `delivers` accepts, as written or in its html-preview form — never combined. */
function fieldDelivers(text, delivers) {
  return typeof text === "string" && text.trim() !== "" && (delivers(text) || delivers(htmlPreviewText(text)));
}

/**
 * True when the call's first question shows what `delivers` accepts before the user answers anything: its question
 * text on its own, or — single-select only — the `preview` of every option.
 * The picker opens on question 1 and shows only the focused option's preview, so content in a later question, in
 * some previews only, or in a multiSelect question's previews (never rendered) is no delivery.
 * An option `description` never delivers: Claude Code renders each option row on one line, every line break in the
 * description replaced (see the module header), so a multi-line table or report there is one unreadable line.
 * The question-level `description` never delivers either: it exists only in the form-style ("extended") question
 * schema a host enables when it renders forms, absent from the classic schema, and that schema describes it as an
 * "Optional single helper line shown under the question" — a one-line field is no place for a table or report.
 * `misplacementNote` names both so a copy placed there moves to the previews in one retry.
 */
function questionDelivers(toolInput, delivers) {
  const first = callQuestions(toolInput)[0];
  if (!first) return false;
  const options = questionOptions(first);
  if (fieldDelivers(first.question, delivers)) return true;
  return (
    first.multiSelect !== true &&
    options.length > 0 &&
    options.every((option) => fieldDelivers(option.preview, delivers))
  );
}

/**
 * Where the call holds a copy of the content outside the placement `questionDelivers` accepts, as one remedy
 * sentence — or "" when no field holds it. Naming the spot lets a denied model move that copy in one retry.
 * An option description is named before the question index: it is unreadable in any question, so a note sending a
 * later question's description copy to question 1 would only earn a second denial.
 */
function misplacementNote(toolInput, delivers) {
  const questions = callQuestions(toolInput);
  const inDescription = questions.some(
    (question) => question && questionOptions(question).some((option) => fieldDelivers(option.description, delivers)),
  );
  if (inDescription) {
    return (
      "It sits in an option `description`, which Claude Code renders as one line with its line breaks replaced, so " +
      "it is unreadable there — move it to the `preview` of every option of the call's first question, or over the " +
      "cap to the file named in the question text. "
    );
  }
  for (let index = 0; index < questions.length; index++) {
    const question = questions[index];
    if (!question) continue;
    const options = questionOptions(question);
    const previews = options.filter((option) => fieldDelivers(option.preview, delivers)).length;
    const helperLine = fieldDelivers(question.description, delivers);
    const shown = helperLine || fieldDelivers(question.question, delivers);
    if (index > 0 && (shown || previews)) {
      return (
        `It sits in question ${index + 1}, which the user reaches only after answering question 1 — ` +
        "make the question carrying it the first question of the call. "
      );
    }
    if (helperLine) {
      return "It sits in the question-level `description`, a single helper line under the question on form hosts. ";
    }
    if (previews && question.multiSelect === true) {
      // Never "make that question single-select": a checkbox question (an item picker) must stay multiSelect.
      return (
        "It sits in a multiSelect question's previews, which are never rendered — move it to the previews of a " +
        "single-select question asked first. "
      );
    }
    if (previews) {
      return `It sits in ${previews} of ${options.length} option previews; the user sees only the focused option's preview. `;
    }
  }
  return "";
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
  const plain = htmlPreviewText(text).replace(/\s+$/, "");
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

/** `question` as the host shows it, for delivery: question text cut after TEXT_MAX_CHARS, over-cap previews gone. */
function shownQuestion(question) {
  return {
    ...question,
    question: typeof question.question === "string" ? question.question.slice(0, TEXT_MAX_CHARS) : question.question,
    options: questionOptions(question).map((option) => ({
      ...option,
      preview: previewCapProblem(option.preview) ? undefined : option.preview,
    })),
  };
}

/**
 * `questionDelivers` judged on what the host actually shows: an option preview over the preview cap delivers nothing,
 * and question text counts only up to its first TEXT_MAX_CHARS chars.
 */
function cappedQuestionDelivers(toolInput, delivers) {
  const [first, ...rest] = callQuestions(toolInput);
  if (!first) return false;
  return questionDelivers({ questions: [shownQuestion(first), ...rest] }, delivers);
}

/** Remedy sentence naming the first question's option previews over the preview cap, or "" when none is. */
function previewCapNote(toolInput) {
  const first = callQuestions(toolInput)[0];
  if (!first || first.multiSelect === true) return "";
  const over = questionOptions(first)
    .map((option, index) => [index + 1, previewCapProblem(option.preview)])
    .filter(([, problem]) => problem)
    .map(([number, problem]) => `option ${number}: ${problem}`);
  if (over.length === 0) return "";
  return `Its option previews exceed the preview cap (${listed(over)}), so the user would not see them whole. `;
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

/** Text of a named file, or null when it is unreadable or over NAMED_FILE_MAX_BYTES. */
function readNamedFile(file) {
  try {
    if (fs.statSync(file).size > NAMED_FILE_MAX_BYTES) return null;
    return fs.readFileSync(file, "utf8");
  } catch (_) {
    return null;
  }
}

/**
 * File-shape delivery of content over the preview cap, as `{ ok, file, note }`. Passes when the call's first question
 * is single-select, its question text (as shown — the first TEXT_MAX_CHARS chars) names an existing file that
 * `rules.holds(fileText, realPath)` accepts as the full content, and every option preview is a compact summary within
 * the cap that names the same file (in any form that resolves to it — absolute, relative, `~/`) and holds more than
 * the path; `rules.conflicts(previewText)` may name a summary value contradicting the source ("" when none). Other
 * `rules`: `cwd` (session dir for relative paths), `knownPaths` (absolute paths the caller expects, matched even when
 * spelled with spaces), `content` (noun phrase for the note), `size` (`{ lines, chars }` of the content's smallest
 * complete rendering: when it fits the preview cap the shape never passes — content that fits belongs in the
 * previews, and a path-only summary would record a delivery the user never saw).
 * `note` is "" unless some preview names a file the question text names — only then was the shape attempted — and
 * then says, as one remedy sentence, what keeps that attempt from passing.
 */
function fileDelivery(toolInput, rules) {
  const { holds, conflicts = () => "", cwd, knownPaths = [], content = "the full content", size } = rules || {};
  const fits = Boolean(size) && size.lines <= PREVIEW_MAX_LINES && size.chars <= PREVIEW_MAX_CHARS;
  const first = callQuestions(toolInput)[0];
  const options = first ? questionOptions(first) : [];
  if (!first || first.multiSelect === true || options.length === 0) return { ok: false, file: null, note: "" };
  const shownText = typeof first.question === "string" ? first.question.slice(0, TEXT_MAX_CHARS) : "";
  const previews = options.map((option) => (typeof option.preview === "string" ? htmlPreviewText(option.preview) : ""));
  const previewFiles = previews.map((text) => namedFiles(text, cwd, knownPaths).map((entry) => entry.file));
  // Text a summary holds besides the path: whatever is left once every path-like word is gone.
  const beyondPath = (text) =>
    text
      .split(/\s+/)
      .filter((word) => !/[\\/]/.test(word))
      .join(" ");
  let note = "";
  for (const { token, file } of namedFiles(shownText, cwd, knownPaths)) {
    const names = (index) => previewFiles[index].includes(file) || previews[index].includes(token);
    if (!previews.some((_, index) => names(index))) continue; // no preview points at this file: no file-shape attempt
    const fileText = readNamedFile(file);
    let problem = "";
    if (fits) {
      problem =
        `${content} fits the preview cap, so a file named in the question does not deliver it — pass it, ` +
        "complete, as the `preview` of every option instead";
    } else if (fileText === null || typeof holds !== "function" || !holds(fileText, file)) {
      problem = `\`${token}\`, named in the question text, does not hold ${content}`;
    } else {
      const summaries = options.filter((option, index) => {
        const rest = beyondPath(previews[index].split(token).join(" "));
        return !previewCapProblem(option.preview) && names(index) && /\w/.test(rest);
      }).length;
      problem =
        summaries < options.length
          ? `${options.length - summaries} of ${options.length} option previews are not a compact summary within ` +
            `the cap that names \`${token}\``
          : previews.map(conflicts).find(Boolean) || "";
    }
    if (!problem) return { ok: true, file, note: "" };
    note = note || `${problem}. `;
  }
  return { ok: false, file: null, note };
}

module.exports = {
  finalReplyText,
  hasHeaderTable,
  MIN_TABLE_ROWS,
  PREVIEW_MAX_CHARS,
  PREVIEW_MAX_LINES,
  PREVIEW_WRAP_COLUMNS,
  TEXT_MAX_CHARS,
  cappedQuestionDelivers,
  isWorkflowFollowUp,
  deliveryProblem,
  fileDelivery,
  followUpProblem,
  htmlPreviewText,
  misplacementNote,
  previewCapNote,
  previewCapProblem,
  previewLineCount,
  questionDeliveryProblem,
  questionDelivers,
  questionTexts,
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

/**
 * PreToolUse delivery problem for a workflow follow-up question, or null to allow it.
 * Only the call's own first question counts (`questionDeliveryProblem`); a reply-text table never does. A pass
 * records nothing: another PreToolUse hook or a permission rule may still deny the call, so Stop's marker is
 * written only once the question was shown (`recordFollowUpShown`, PostToolUse).
 */
function followUpProblem(sentinelPath, reportFile, payload) {
  return questionDeliveryProblem(reportFile, payload && payload.tool_input, payload && payload.cwd);
}

/**
 * PostToolUse(AskUserQuestion) step for a workflow follow-up: the question was shown, so when it delivered the
 * report, record the report version in Stop's marker. A table delivered in an option preview never reaches the
 * transcript's text blocks, so Stop would otherwise demand the already-seen table once more. Re-checks the call
 * with the same rule as PreToolUse rather than trusting that pass, so a marker always means "delivered and shown".
 * Returns true when the marker was written.
 */
function recordFollowUpShown(sentinelPath, reportFile, payload) {
  if (!sentinelPath || !reportFile) return false;
  const problem = questionDeliveryProblem(reportFile, payload && payload.tool_input, payload && payload.cwd);
  const stamp = problem ? null : reportStamp(reportFile);
  if (!stamp) return false;
  try {
    fs.writeFileSync(deliveredMarkerPath(sentinelPath), stamp);
    return true;
  } catch (_) {
    return false; // unrecorded — Stop re-checks this report once, never blocks the question
  }
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
 * Question-time delivery problem judged on the AskUserQuestion input alone, or null when its first question carries
 * the current report as shown — within the preview cap (`cappedQuestionDelivers`; each field checked on its own, never
 * combined) — or in the file shape for content over the cap (`fileDelivery`; `cwd` resolves a relative path the
 * question names). The transcript is not read: reply text written before the call can come back as an empty
 * progress update, so it is no delivery here.
 */
function questionDeliveryProblem(reportFile, toolInput, cwd) {
  const check = reportDeliveryCheck(reportFile);
  if (check.problem) return check.problem;
  if (cappedQuestionDelivers(toolInput, check.delivers)) return null;
  const file = fileDelivery(toolInput, {
    holds: check.holdsFile,
    conflicts: check.conflicts,
    cwd,
    knownPaths: check.knownPaths,
    content: check.content,
    size: check.size,
  });
  if (file.ok) return null;
  const fields = questionTexts(toolInput).flatMap((field) => [field, htmlPreviewText(field)]);
  const held =
    previewCapNote(toolInput) || file.note || misplacementNote(toolInput, check.delivers) || check.diagnose(fields);
  return `${check.undelivered} by this question` + questionNote(held, check);
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
  return reportFile.endsWith("summary.jsonl")
    ? auditDeliveryCheck(content, reportFile)
    : headerDeliveryCheck(content, reportFile);
}

/**
 * Size of the smallest complete rendering of `lines`: the rows they span in the preview box (`wrappedRows`, the
 * measure the cap applies), and their chars with one newline each.
 */
function renderedSize(lines) {
  return {
    lines: lines.reduce((rows, line) => rows + wrappedRows(line), 0),
    chars: lines.reduce((total, line) => total + line.length + 1, 0),
  };
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
 * itself is JSONL, never the user-readable copy: over the cap the rendered Audit Report goes to `audit-report.md`
 * beside it, and only a named file holding that rendering counts.
 */
function auditDeliveryCheck(content, reportFile) {
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
  const reportCopy = path.join(path.dirname(reportFile), "audit-report.md");
  return {
    delivers,
    diagnose,
    undelivered,
    missing: `${undelivered}; emit Step 7 before following up`,
    noun: "Audit Report",
    size: renderedSize(["## Audit Report", `Total: ${findings.length}`, ...findings.map((f) => f.one_line)]),
    content: "the full Audit Report (heading, `Total:` line, every finding line)",
    target: "write the full Audit Report (heading, `Total:` line, every finding line) to " + `\`${reportCopy}\` first`,
    knownPaths: [reportCopy],
    holdsFile: (text) => delivers(text),
    conflicts: () => "",
  };
}

/**
 * A `---` header report counts as delivered only when one complete table repeats every field and value.
 * Returns `{ problem }` for an unfinished header, else:
 * - `delivers(text)`: the per-source predicate;
 * - `diagnose(texts)`: near-match remedy — which rows of the closest table differ from the file, "" when no source
 *   holds a table naming any field;
 * - `undelivered` / `missing`: question-time and Stop-time reason prefixes;
 * - `noun`, `size` (smallest complete rendering, for the preview-cap remedy), `target` (where the full copy lives over
 *   the cap), `content`, `knownPaths`, `holdsFile(text, realPath)`, `conflicts(text)`: the `fileDelivery` rules. The
 *   report file itself holds the full header, so naming it is enough; a summary may quote some rows, never wrongly.
 */
function headerDeliveryCheck(content, reportFile) {
  const header = content.match(/^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)/);
  const fields = header ? Array.from(header[1].matchAll(/^([^:\r\n]+):[ \t]*(.+)$/gm)) : [];
  if (
    fields.length < MIN_TABLE_ROWS ||
    fields.length !== header[1].split(/\r?\n/).length ||
    new Set(fields.map((field) => field[1].trim())).size !== fields.length ||
    fields.some((field) => !field[2].trim()) ||
    !fields.some((field) => field[1].trim() === "Title")
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
  // A summary row naming a header field must show the file's value — a stale table never passes as a summary.
  const conflicts = (text) => {
    const problems = [];
    for (const table of tableBlocks(text)) {
      for (const row of tableRows(table) || []) {
        const entry = row.length === 2 && entries.find(([name]) => cellMatches(row[0], name));
        if (entry && !cellMatches(row[1], entry[1]))
          problems.push(`\`${entry[0]}\` reads \`${quoted(row[1])}\` but the file has \`${quoted(entry[1])}\``);
      }
    }
    return problems.length > 0 ? `The preview summary contradicts the report file: ${listed(problems)}` : "";
  };
  const reportReal = existingFile(reportFile);
  return {
    delivers,
    diagnose,
    undelivered: "current report header was not delivered",
    missing: "current report header was not delivered; print every header field as a table before following up",
    noun: "header table",
    size: renderedSize([
      "| Field | Value |",
      "| --- | --- |",
      ...entries.map(([name, value]) => `| ${name} | ${value.replace(/\|/g, "\\|")} |`),
    ]),
    content: "the full report header",
    target: `the report file \`${reportFile}\` already holds the full header`,
    knownPaths: [reportFile],
    holdsFile: (text, file) => file === reportReal || delivers(text),
    conflicts,
  };
}
