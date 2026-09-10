#!/usr/bin/env node
// blueprint-allow.js — PreToolUse hook (matcher: Bash)
//
// PURPOSE
//   Plugin skills, agents and rules ship fenced `bash` blocks the model runs
//   VERBATIM. Claude Code's prefix allow-matcher cannot express most of them: a
//   command containing `$(...)` fails closed ("Contains expansion"), and a
//   prefix rule matches only the first token, so `VAR=$(...)` and `IFS= read -r`
//   can never match. This hook replaces the shape-based trust statement with a
//   provenance one — "this exact text exists in a reviewed, versioned plugin
//   file" — by hashing the incoming command with the same normalization pipeline
//   `bin/build_blueprint_manifest.py` used to build `blueprint-manifest.json`,
//   and auto-allowing an exact digest hit. Any deviation from the blueprint text
//   misses and falls through (exit 0, no output) to the normal prompt, so custom
//   or adapted code stays gated.
//
// SECURITY MODEL — why the allow is safe
//   Like sentinel-read-allow.js and unlike rtk-rewrite.js we emit NO
//   updatedInput: the ORIGINAL command string is allowed unchanged, so
//   settings.json deny rules keep matching it (deny is evaluated regardless of a
//   hook allow decision). Three layers must all hold before the allow fires:
//
//     1. PROVENANCE — hash match. The normalization pipeline here is a
//        line-for-line port of the one documented in
//        `bin/build_blueprint_manifest.py`'s module docstring, which is the
//        single source of truth. A hook that normalizes MORE aggressively than
//        that generator is the one real bug class: it lets a crafted command
//        collide with a manifest digest. Every rstrip / strip-truthiness /
//        token-split here therefore uses PY_WS, the exact character set Python's
//        `str.isspace()` reports — JavaScript's `\s` and `String.trim()` disagree
//        with it in BOTH directions (`﻿` is JS-only; `\x1c`-`\x1f` and
//        `\x85` are Python-only). The incoming command is never `.trim()`ed
//        before hashing, for the same reason. The shared vector fixture
//        `tests/fixtures/blueprint_normalization_vectors.json` is executed by
//        both languages' suites so the two implementations cannot drift apart.
//
//     2. COMPOSITION — never allow by recombining fragments. A whole-command
//        digest hit allows directly. Otherwise the command is split into logical
//        commands (top-level newlines only — never `;`/`&&`/`|`, since a
//        fragment lifted out of a compound command changes meaning) and EVERY
//        resulting command must independently be a manifest entry. Partial
//        coverage is a miss. Splicing halves of two entries together therefore
//        never allows; running two COMPLETE blueprint commands in sequence does,
//        which grants nothing — each line was independently reviewed and would
//        be allowed on its own. Per-command splitting is skipped entirely when
//        the generator's bail-out condition holds (a heredoc marker or an
//        unterminated quote), matching the generator, which emits only a
//        whole-block entry for such blocks.
//
//     3. DEFENCE IN DEPTH — independent danger re-check. The generator already
//        drops destructive entries, so the manifest should never contain one.
//        The hook does not trust that: the full `is_dangerous` port is re-run
//        and a hit refuses regardless of a digest match. This is what a generator
//        bug, a stale manifest, or a tampered `blueprint-manifest.json` runs into.
//        The re-check runs on the NORMALIZED text — exactly the text that gets
//        hashed and looked up, so the layer judges what the layer above matched.
//        Raw text would be judged with comments still attached, and a benign
//        blessed command carrying a trailing `# ...check before you push` would be
//        refused over a word bash never executes. Normalizing loses no danger:
//        the pipeline only strips comments, right-strips, and folds blank lines —
//        it never removes a token bash would run.
//
//   The manifest record's `src` is reported in the allow reason as provenance.
//   When several logical commands matched, the FIRST match's `src` is used —
//   the reason line is a human audit pointer, not a machine contract.
//
//   Every ambiguity resolves toward passthrough: a missing, unreadable or
//   malformed manifest, an empty command, a non-Bash tool, or any parse failure
//   yields silent passthrough rather than an allow.
//
//   Module note: the stdin driver is guarded by `require.main === module`, and
//   the pure functions are exported, so the test suite can exercise the shared
//   normalization vectors directly instead of only through allow/miss outcomes.
//   Requiring this file attaches no stdin listeners.
//
// EXIT CODES
//   0  always — passthrough (no output) or allow (JSON to stdout)

"use strict";

const crypto = require("crypto");
const fs = require("fs");
const path = require("path");

const MANIFEST_NAME = "blueprint-manifest.json";

// ── Python whitespace parity ──────────────────────────────────────────────────
// Exactly the characters Python's `str.isspace()` reports, so `rstrip`,
// truthiness-after-strip, and token splitting behave identically on both sides.
const PY_WS =
  "\\t\\n\\v\\f\\r\\x1c\\x1d\\x1e\\x1f \\x85\\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000";
const RSTRIP_RE = new RegExp("[" + PY_WS + "]+$");
const WS_SPLIT_RE = new RegExp("[" + PY_WS + "]+");
const BLANK_RE = new RegExp("^[" + PY_WS + "]*$");

/** Python `str.rstrip()`. */
function rstrip(text) {
  return text.replace(RSTRIP_RE, "");
}

/** Python `bool(text.strip())`. */
function hasContent(text) {
  return !BLANK_RE.test(text);
}

/** Strip every leading and trailing character present in `chars` (Python `str.strip(chars)`). */
function stripChars(text, chars) {
  let start = 0;
  let end = text.length;
  while (start < end && chars.indexOf(text[start]) !== -1) start++;
  while (end > start && chars.indexOf(text[end - 1]) !== -1) end--;
  return text.slice(start, end);
}

// ── Normalization ─────────────────────────────────────────────────────────────

/**
 * Remove a word-start `#` comment from one line, tracking quote state.
 * Port of build_blueprint_manifest.strip_line_comment.
 * @returns {[string, string|null]} cleaned line and the quote open at its end.
 */
function stripLineComment(line, quote) {
  let index = 0;
  const length = line.length;
  while (index < length) {
    const char = line[index];
    if (quote !== null) {
      if (char === "\\" && quote === '"') {
        index += 2;
        continue;
      }
      if (char === quote) quote = null;
      index += 1;
      continue;
    }
    if (char === "\\") {
      index += 2;
      continue;
    }
    if (char === "'" || char === '"') {
      quote = char;
      index += 1;
      continue;
    }
    if (char === "#" && (index === 0 || line[index - 1] === " " || line[index - 1] === "\t")) {
      return [rstrip(line.slice(0, index)), quote];
    }
    index += 1;
  }
  return [rstrip(line), quote];
}

/** Drop leading/trailing blank lines and collapse internal blank runs to one. */
function collapseBlankLines(lines) {
  const out = [];
  for (const line of lines) {
    if (line) out.push(line);
    else if (out.length && out[out.length - 1]) out.push("");
  }
  while (out.length && !out[out.length - 1]) out.pop();
  return out;
}

/** Apply the full normalization pipeline to a bash block or command. */
function normalize(text) {
  const lines = text.replace(/\r\n/g, "\n").replace(/\r/g, "\n").split("\n");
  let quote = null;
  const stripped = [];
  for (const line of lines) {
    const result = stripLineComment(rstrip(line), quote);
    stripped.push(result[0]);
    quote = result[1];
  }
  return collapseBlankLines(stripped).join("\n");
}

/** Lowercase SHA-256 hex digest of `text` encoded as UTF-8. */
function sha256Text(text) {
  return crypto.createHash("sha256").update(text, "utf8").digest("hex");
}

// ── Splitting ─────────────────────────────────────────────────────────────────

/** Count the backslashes ending `line`. */
function trailingBackslashes(line) {
  let count = 0;
  while (count < line.length && line[line.length - 1 - count] === "\\") count++;
  return count;
}

/** Split normalized text into logical commands on top-level newlines only. */
function splitLogicalCommands(normalized) {
  const commands = [];
  let buffer = [];
  for (const line of normalized.split("\n")) {
    buffer.push(line);
    if (trailingBackslashes(line) % 2 === 1) continue;
    const joined = buffer.join("\n");
    if (hasContent(joined)) commands.push(joined);
    buffer = [];
  }
  if (buffer.length && hasContent(buffer.join("\n"))) commands.push(buffer.join("\n"));
  return commands;
}

/** True when per-command extraction is unsound (heredoc marker or unterminated quote). */
function needsBailout(normalized) {
  if (normalized.indexOf("<<") !== -1) return true;
  let quote = null;
  for (const line of normalized.split("\n")) {
    quote = stripLineComment(line, quote)[1];
    if (quote !== null) return true;
  }
  return false;
}

/**
 * Read a nesting-aware delimited run beginning at `start`.
 * @returns {[string, number]} inner text and the index just past the closing delimiter.
 */
function readDelimited(text, start, openChar, closeChar) {
  let depth = 1;
  let index = start;
  while (index < text.length) {
    const char = text[index];
    if (char === "\\") {
      index += 2;
      continue;
    }
    if (openChar !== null && char === openChar) {
      depth += 1;
    } else if (char === closeChar) {
      depth -= 1;
      if (depth === 0) return [text.slice(start, index), index + 1];
    }
    index += 1;
  }
  return [text.slice(start), text.length];
}

const SEGMENT_SEPARATORS = ";&|\n";

/**
 * Read a `$(...)` or backtick command substitution opening at `index`.
 * Both forms are expanded by bash inside double quotes as well as at top level, so this
 * is called from both scanning states.
 * @returns {[string, number]|null} inner text and next index, or null when none opens here.
 */
function readSubstitution(text, index) {
  if (text.startsWith("$(", index)) return readDelimited(text, index + 2, "(", ")");
  if (text[index] === "`") return readDelimited(text, index + 1, null, "`");
  return null;
}

/**
 * Read a `<(...)` or `>(...)` process substitution opening at `index`.
 * Only the two-character opener triggers this; a bare `<`/`>` redirection is left alone
 * and is not a segment separator.
 * @returns {[string, number]|null} inner text and next index, or null when none opens here.
 */
function readProcessSubstitution(text, index) {
  const char = text[index];
  if ((char === "<" || char === ">") && text.startsWith("(", index + 1)) {
    return readDelimited(text, index + 2, "(", ")");
  }
  return null;
}

/**
 * Locate unquoted segment separators and expanded substitution bodies.
 * Command substitutions are read at top level and inside double quotes — bash expands
 * them in both — while process substitutions are read at top level only. Nothing is read
 * inside single quotes, where bash expands nothing.
 * @returns {[number[], string[]]} separator indices and substitution inner texts.
 */
function scanTopLevel(text) {
  const separators = [];
  const substitutions = [];
  let quote = null;
  let index = 0;
  while (index < text.length) {
    const char = text[index];
    if (quote !== null) {
      if (char === "\\" && quote === '"') {
        index += 2;
        continue;
      }
      const read = quote === '"' ? readSubstitution(text, index) : null;
      if (read !== null) {
        substitutions.push(read[0]);
        index = read[1];
        continue;
      }
      if (char === quote) quote = null;
      index += 1;
      continue;
    }
    if (char === "\\") {
      index += 2;
      continue;
    }
    if (char === "'" || char === '"') {
      quote = char;
      index += 1;
      continue;
    }
    const read = readSubstitution(text, index) || readProcessSubstitution(text, index);
    if (read !== null) {
      substitutions.push(read[0]);
      index = read[1];
      continue;
    }
    if (SEGMENT_SEPARATORS.indexOf(char) !== -1) separators.push(index);
    index += 1;
  }
  return [separators, substitutions];
}

/** Split a command into unquoted segments, including substitution bodies (recursively). */
function splitSegments(command) {
  const scanned = scanTopLevel(command);
  const parts = [];
  let previous = 0;
  for (const position of scanned[0]) {
    parts.push(command.slice(previous, position));
    previous = position + 1;
  }
  parts.push(command.slice(previous));
  const segments = parts.filter(hasContent);
  for (const inner of scanned[1]) {
    for (const segment of splitSegments(inner)) segments.push(segment);
  }
  return segments;
}

// ── Danger filter ─────────────────────────────────────────────────────────────

const DANGER_COMMANDS = new Set(["rm", "dd", "chmod", "chown", "mkfs", "shutdown", "kill", "pkill"]);
const DEFERRING_COMMANDS = new Set([
  "eval",
  "xargs",
  "sudo",
  "doas",
  "env",
  "nohup",
  "time",
  "timeout",
  "nice",
  "ionice",
  "command",
  "exec",
  "watch",
  "trap",
  "find",
  "bash",
  "sh",
  "zsh",
]);
const GIT_DANGER_TOKENS = new Set(["push", "commit", "reset", "revert"]);
const GIT_FORCE_TOKENS = new Set(["--force", "--force-with-lease", "-f"]);
const ASSIGNMENT_RE = /^[A-Za-z_][A-Za-z0-9_]*=/;

/** Split a segment into bare tokens with grouping and quote characters removed. */
function segmentTokens(segment) {
  const tokens = [];
  for (const raw of segment.split(WS_SPLIT_RE)) {
    if (!raw) continue;
    const token = stripChars(stripChars(raw, "(){}"), "'\"");
    if (token) tokens.push(token);
  }
  return tokens;
}

/** Return a segment's command token (assignments skipped, directory dropped) and its arguments. */
function commandHead(tokens) {
  for (let index = 0; index < tokens.length; index++) {
    const token = tokens[index];
    if (ASSIGNMENT_RE.test(token)) continue;
    return [token.slice(token.lastIndexOf("/") + 1), tokens.slice(index + 1)];
  }
  return ["", []];
}

/** True when a `git` segment mutates history, a remote, or the working tree. */
function gitIsDangerous(tokens) {
  for (const token of tokens) {
    if (GIT_DANGER_TOKENS.has(token) || GIT_FORCE_TOKENS.has(token)) return true;
  }
  return tokens.indexOf("worktree") !== -1 && tokens.indexOf("remove") !== -1;
}

/** True when one segment runs a destructive command, directly or through a deferring command. */
function segmentIsDangerous(segment) {
  const tokens = segmentTokens(segment);
  const split = commandHead(tokens);
  const head = split[0];
  const rest = split[1];
  if (DANGER_COMMANDS.has(head)) return true;
  if (head === "git") return gitIsDangerous(tokens);
  if (!DEFERRING_COMMANDS.has(head)) return false;
  if (rest.indexOf("-delete") !== -1) return true;
  for (const token of rest) {
    if (DANGER_COMMANDS.has(token.slice(token.lastIndexOf("/") + 1))) return true;
  }
  return rest.indexOf("git") !== -1 && gitIsDangerous(rest);
}

/** True when any segment of `command` is destructive. */
function isDangerous(command) {
  return splitSegments(command).some(segmentIsDangerous);
}

// ── Manifest lookup ───────────────────────────────────────────────────────────

/**
 * Load the `entries` map from the plugin's blueprint manifest.
 * The hook lives in `hooks/`, the manifest at the plugin root one level up;
 * `CLAUDE_PLUGIN_ROOT` is the fallback. Returns null when nothing is usable.
 */
function loadEntries() {
  const candidates = [path.join(__dirname, "..", MANIFEST_NAME)];
  if (process.env.CLAUDE_PLUGIN_ROOT) {
    candidates.push(path.join(process.env.CLAUDE_PLUGIN_ROOT, MANIFEST_NAME));
  }
  for (const candidate of candidates) {
    try {
      const parsed = JSON.parse(fs.readFileSync(candidate, "utf8"));
      const entries = parsed && parsed.entries;
      if (entries && typeof entries === "object" && !Array.isArray(entries)) return entries;
    } catch (_) {
      // Unreadable or malformed — try the next candidate, never throw.
    }
  }
  return null;
}

/** Return the manifest record for `digest`, or null. */
function lookup(entries, digest) {
  if (!Object.prototype.hasOwnProperty.call(entries, digest)) return null;
  const record = entries[digest];
  return record && typeof record === "object" ? record : null;
}

/**
 * Decide whether `command` is verbatim blueprint text.
 * @returns {object|null} the matched manifest record, or null for passthrough.
 */
function matchBlueprint(command, entries) {
  const normalized = normalize(command);
  if (!normalized) return null;
  const direct = lookup(entries, sha256Text(normalized));
  if (direct) return direct;
  if (needsBailout(normalized)) return null;
  const commands = splitLogicalCommands(normalized);
  if (commands.length < 2) return null;
  let first = null;
  for (const logical of commands) {
    const record = lookup(entries, sha256Text(logical));
    if (!record) return null;
    if (!first) first = record;
  }
  return first;
}

/** Return the `src` provenance label of a matched manifest record. */
function recordSrc(record) {
  return typeof record.src === "string" ? record.src : "unknown";
}

/**
 * Build the allow payload for a matched manifest record.
 * Shared by `decide` and `evaluate` so the two can never disagree on a byte of stdout.
 */
function allowPayload(record) {
  return {
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "allow",
      permissionDecisionReason: "plugin blueprint — verbatim block from " + recordSrc(record),
    },
  };
}

/** Build the hook's stdout payload for a raw stdin string, or null for passthrough. */
function decide(raw) {
  let data;
  try {
    data = JSON.parse(raw);
  } catch (_) {
    return null;
  }
  if (!data || data.tool_name !== "Bash") return null;
  const command = data.tool_input && data.tool_input.command;
  if (typeof command !== "string" || !command) return null;
  if (isDangerous(normalize(command))) return null;
  const entries = loadEntries();
  if (!entries) return null;
  const record = matchBlueprint(command, entries);
  if (!record) return null;
  return allowPayload(record);
}

/**
 * Classify what this module did with `raw`, for the audit record the dispatcher writes.
 * Same checks in the same order as `decide` — nothing is moved, added or skipped — with each outcome labelled:
 *
 *   decision "allow"        the module produced an allow payload;
 *   decision "passthrough"  the module reached a decision and declined, `why` naming which check declined it;
 *   decision "none"         the module returned before deciding anything at all.
 *
 * `none` is the ABSENCE of an opinion, not an abstention, and the two stay distinct everywhere downstream.
 *
 * `digest` is present exactly when the command was normalized, i.e. whenever a decision was reached. A `none` result
 * carries no digest because nothing normalized the command — that is correct, not a gap.
 *
 * Never calls `process.exit`, and never writes an audit record: only the dispatcher does that.
 *
 * @returns {{payload: object|null, decision: string, lane: string, rank: number, digest?: string, src?: string,
 *   why?: string}}
 */
function evaluate(raw) {
  const lane = { lane: "blueprint", rank: 1 };
  const none = (why) => ({ ...lane, payload: null, decision: "none", why });
  let data;
  try {
    data = JSON.parse(raw);
  } catch (_) {
    return none("not-applicable");
  }
  if (!data || data.tool_name !== "Bash") return none("not-applicable");
  const command = data.tool_input && data.tool_input.command;
  if (typeof command !== "string" || !command) return none("not-applicable");
  const normalized = normalize(command);
  const digest = sha256Text(normalized);
  const declined = (why) => ({ ...lane, payload: null, decision: "passthrough", why, digest });
  if (isDangerous(normalized)) return declined("danger");
  const entries = loadEntries();
  if (!entries) return declined("manifest-unavailable");
  const record = matchBlueprint(command, entries);
  // `needsBailout` is pure and is consulted only to LABEL a decline that has already happened; a digest hit returns
  // above, so this can never turn an allow into a passthrough.
  if (!record) return declined(needsBailout(normalized) ? "bailout" : "no-match");
  return { ...lane, payload: allowPayload(record), decision: "allow", digest, src: recordSrc(record) };
}

if (require.main === module) {
  let raw = "";
  process.stdin.setEncoding("utf8");
  process.stdin.on("data", (chunk) => (raw += chunk));
  process.stdin.on("end", () => {
    try {
      const payload = decide(raw);
      if (payload) process.stdout.write(JSON.stringify(payload));
    } catch (_) {
      // Never crash or block Claude due to a hook bug.
    }
    process.exit(0);
  });
}

module.exports = {
  normalize,
  needsBailout,
  splitLogicalCommands,
  isDangerous,
  sha256Text,
  matchBlueprint,
  decide,
  evaluate,
};
