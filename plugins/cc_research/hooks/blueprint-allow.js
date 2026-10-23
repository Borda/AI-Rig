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

// An unescaped backslash, then a blank, then `#`: the shell keeps `\ #…` as word text, normalization drops it.
const ESCAPED_BLANK_BEFORE_COMMENT = /(?<!\\)(?:\\\\)*\\[ \t]#/;

/**
 * True when normalization would erase a blank the shell reads as escaped: a line ending in an odd backslash run plus
 * blanks (the shell ends the command there, normalization sees a continuation), or an escaped blank before `#` (the
 * shell passes it as an argument, normalization drops it as a comment). Raw text and normalized form are then different
 * commands sharing one digest, so the raw text is refused before any lookup.
 * Port of build_blueprint_manifest.aliases_escaped_blank.
 */
function aliasesEscapedBlank(text) {
  for (const line of text.replace(/\r\n/g, "\n").replace(/\r/g, "\n").split("\n")) {
    const trimmed = rstrip(line);
    if (trimmed.length < line.length && trailingBackslashes(trimmed) % 2 === 1) return true;
    if (ESCAPED_BLANK_BEFORE_COMMENT.test(line)) return true;
  }
  return false;
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
 * Read a nesting-aware delimited run beginning at `start`. A `(…)` body is shell code, so its quoted strings are
 * read whole: a parenthesis inside `'…'`, `"…"` or `$'…'` neither opens nor closes the body.
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
    if (openChar === "(" && (char === "'" || char === '"' || ansiCBody(text, index) !== -1)) {
      index = skipQuoted(text, index);
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

/**
 * Where the body of a `$'…'` string opening at `index` starts, or -1 when none opens there. The shell removes a
 * backslash-newline before it reads quotes, so `$` and `'` split across a line continuation still open `$'…'`.
 */
function ansiCBody(text, index) {
  if (text[index] !== "$") return -1;
  let next = index + 1;
  while (text.startsWith("\\\n", next)) next += 2;
  return text[next] === "'" ? next + 1 : -1;
}

/** Index just past the `'…'`, `"…"` or `$'…'` string opening at `index`. */
function skipQuoted(text, index) {
  if (text[index] === "'") {
    const end = text.indexOf("'", index + 1);
    return end < 0 ? text.length : end + 1;
  }
  const body = ansiCBody(text, index);
  if (body !== -1) return readAnsiC(text, body)[1];
  return readDoubleQuoted(text, index + 1)[1];
}

const SEGMENT_SEPARATORS = ";&|\n";

/** True when the separator at `index` is the `&` of `>&`, `<&` or `&>`, not a command boundary. */
function isRedirectionAmpersand(text, index) {
  if (text[index] !== "&") return false;
  const before = index > 0 ? text[index - 1] : "";
  const after = index + 1 < text.length ? text[index + 1] : "";
  return before === "<" || before === ">" || after === ">";
}

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
 * inside single quotes, where bash expands nothing. A `$'…'` string honours its backslash
 * escapes, and the `&` of a redirection (`2>&1`, `&>/dev/null`) is not a separator.
 * @returns {[number[], string[], boolean]} separator indices, substitution inner texts, and
 *   whether the text ends inside an unclosed quote or holds a `$'…'` string with an escape
 *   bash and zsh read differently.
 */
function scanTopLevel(text) {
  const separators = [];
  const substitutions = [];
  let quote = null;
  let unsettled = false;
  let index = 0;
  while (index < text.length) {
    const char = text[index];
    if (quote !== null) {
      if (char === "\\" && quote !== "'") {
        index += 2;
        continue;
      }
      const read = quote === '"' ? readSubstitution(text, index) : null;
      if (read !== null) {
        substitutions.push(read[0]);
        index = read[1];
        continue;
      }
      if (char === quote[quote.length - 1]) quote = null;
      index += 1;
      continue;
    }
    if (char === "\\") {
      index += 2;
      continue;
    }
    const body = ansiCBody(text, index);
    if (char === "'" || char === '"' || body !== -1) {
      quote = body === -1 ? char : "$'";
      unsettled = unsettled || (body !== -1 && readAnsiC(text, body)[0].indexOf("\u0000") !== -1);
      index = body === -1 ? index + 1 : body;
      continue;
    }
    const read = readSubstitution(text, index) || readProcessSubstitution(text, index);
    if (read !== null) {
      substitutions.push(read[0]);
      index = read[1];
      continue;
    }
    if (SEGMENT_SEPARATORS.indexOf(char) !== -1 && !isRedirectionAmpersand(text, index)) separators.push(index);
    index += 1;
  }
  return [separators, substitutions, quote !== null || unsettled];
}

/** True when `command` or a substitution body in it ends inside an unclosed quote or has an unsettled escape. */
function quotingUnsettled(command) {
  const scanned = scanTopLevel(command);
  return scanned[2] || scanned[1].some(quotingUnsettled);
}

/**
 * True when a `$(...)` body is really a `$(( ... ))` arithmetic expansion: the paren it
 * opens closes at the very end of the body (`$((echo a); echo b)` is a command substitution).
 */
function isArithmetic(inner) {
  if (!(inner.startsWith("(") && inner.endsWith(")"))) return false;
  const read = readDelimited(inner, 1, "(", ")");
  return read[1] === inner.length && read[0] === inner.slice(1, -1);
}

/**
 * Position in `edges` of the separator closing the segment that starts after `edges[index]`.
 * Inside `[[ ... ]]` the `&&` / `||` operators join conditions, not commands, so a `[[` test
 * stays one segment; a `[[` never closed by a `]]` word leaves the segment unmerged.
 */
function conditionalEnd(command, edges, index) {
  const end = index + 1;
  const opener = commandWords(command.slice(edges[index] + 1, edges[end]));
  if (opener.length === 0 || opener[0] !== "[[") return end;
  for (let candidate = end; candidate < edges.length; candidate++) {
    const words = command.slice(edges[index] + 1, edges[candidate]).split(WS_SPLIT_RE);
    if (words.indexOf("]]") !== -1) return candidate;
  }
  return end;
}

/** Split a command into unquoted segments, including substitution bodies (recursively). */
function splitSegments(command) {
  const scanned = scanTopLevel(command);
  const edges = [-1].concat(scanned[0], [command.length]);
  const segments = [];
  let index = 0;
  while (index < edges.length - 1) {
    const end = conditionalEnd(command, edges, index);
    const segment = command.slice(edges[index] + 1, edges[end]);
    if (hasContent(segment)) segments.push(segment);
    index = end;
  }
  for (const inner of scanned[1]) {
    const bodies = isArithmetic(inner) ? scanTopLevel(inner)[1] : [inner];
    for (const body of bodies) {
      for (const segment of splitSegments(body)) segments.push(segment);
    }
  }
  return segments;
}

// ── Danger filter ─────────────────────────────────────────────────────────────

const DANGER_COMMANDS = new Set(["rm", "dd", "chmod", "chown", "mkfs", "shutdown", "kill", "pkill"]);
// Commands that change what a command name runs, reach a builtin behind a prefix, fill a variable or array named on
// the command line, or run text built at run time. No shipped block runs one, so each is denied by name.
const REBIND_COMMANDS = new Set([
  "hash",
  "builtin",
  "enable",
  "disable",
  "alias",
  "unalias",
  "functions",
  "autoload",
  "zmodload",
  "emulate",
  "mapfile",
  "readarray",
  "vared",
  "zparseopts",
  "getln",
  "eval",
]);
// `noglob`, `nocorrect` and `-` are zsh precommand modifiers: `noglob rm -rf x` runs `rm`.
const DEFERRING_COMMANDS = new Set([
  "noglob",
  "nocorrect",
  "-",
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
  "uv",
  "poetry",
  "bash",
  "sh",
  "zsh",
  "dash",
  "ksh",
]);
const SHELL_INTERPRETERS = new Set(["bash", "sh", "zsh", "dash", "ksh"]);
const SHELL_RESERVED_WORDS = new Set(["if", "then", "elif", "else", "while", "until", "do", "!"]);
const GIT_DANGER_TOKENS = new Set(["push", "commit", "reset", "revert", "restore", "clean"]);
const GIT_FORCE_TOKENS = new Set(["--force", "--force-with-lease", "-f"]);
const GIT_BRANCH_DELETE_TOKENS = new Set(["-D", "-d", "--delete"]);
const ASSIGNMENT_RE = /^[A-Za-z_][A-Za-z0-9_]*=/;

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
  const has = (word) => tokens.indexOf(word) !== -1;
  if (has("worktree") && has("remove")) return true;
  if (has("checkout") && (has("--") || has("."))) return true;
  if (has("branch") && tokens.some((token) => GIT_BRANCH_DELETE_TOKENS.has(token))) return true;
  return has("stash") && (has("drop") || has("clear"));
}

/** True when any token is a short-option cluster carrying `letter` (`-i`, `-ni`, `-i.bak`). */
function hasShortFlag(tokens, letter) {
  const pattern = new RegExp("^-[A-Za-z]*" + letter);
  return tokens.some((token) => pattern.test(token));
}

/** True when a `sed` segment rewrites its input files in place. */
function sedEditsInPlace(tokens) {
  return hasShortFlag(tokens, "i") || tokens.some((token) => token.startsWith("--in-place"));
}

/** A command word as the shell runs it: backslashes and quotes removed, directory dropped (`\rm`, `r""m` → `rm`). */
function bareWord(token) {
  const bare = token.replace(/[\\'"]/g, "");
  return bare.slice(bare.lastIndexOf("/") + 1);
}

/**
 * Option words after a segment's command word, and its operands. An option can take its argument as the next word
 * (`set -o noglob -A …`, `read -t 5 -a …`), so every word before `--` starting with `-` or `+` counts as an option.
 */
function splitOptions(tokens) {
  const rest = commandHead(tokens)[1];
  const options = [];
  const operands = [];
  for (let index = 0; index < rest.length; index++) {
    const word = rest[index];
    if (word === "--") return [options, operands.concat(rest.slice(index + 1))];
    ((word.startsWith("-") || word.startsWith("+")) && word.length > 1 ? options : operands).push(word);
  }
  return [options, operands];
}

/** True when a `set` segment assigns an array (zsh `set -A name …` / `set +A name …`), whatever the name. */
function setAssignsArray(tokens) {
  return splitOptions(tokens)[0].some((word) => word.includes("A"));
}

/**
 * True when a `read` segment fills an array (`-a`/`-A`) or a variable whose name comes from an expansion. The tokens
 * come from `shellWords`, which has already dropped redirections (`read -r v < "$f"` arrives as `read -r v`).
 */
function readAssignsByName(tokens) {
  const [options, operands] = splitOptions(tokens);
  if (options.some((word) => word.includes("a") || word.includes("A"))) return true;
  return operands.some((word) => word.includes("$") || word.includes("`"));
}

/**
 * True when a `printf`/`print` segment writes its output into a variable (`-v name`). Both stop reading options at
 * their first other word, so only leading options count; a zsh `print` value option (`-u 2`) consumes the next word.
 */
function printAssignsVariable(tokens) {
  const [head, rest] = commandHead(tokens);
  const valueLetters = bareWord(head) === "print" ? "ufCxX" : "";
  let index = 0;
  while (index < rest.length) {
    const word = rest[index];
    if (word === "--" || !word.startsWith("-") || word.length < 2) return false;
    if (word.includes("v")) return true;
    index += valueLetters.includes(word[word.length - 1]) ? 2 : 1;
  }
  return false;
}

/** True when a declaring builtin makes a name reference (`-n`) or declares a name taken from an expansion. */
function declaresByName(tokens) {
  const [options, operands] = splitOptions(tokens);
  if (options.some((word) => word.startsWith("-") && word.includes("n"))) return true;
  return operands.some((word) => {
    const name = word.split("=", 1)[0];
    return name.includes("$") || name.includes("`");
  });
}

// Any mention of a table that maps a name to what it runs (zsh `commands`, `functions`, `aliases` and siblings,
// `fpath`/`FPATH`, bash `BASH_CMDS`/`BASH_ALIASES`), an attached option prefix included (`set -Acommands`).
const COMMAND_TABLE_USE_RE =
  /(?<![A-Za-z0-9_./-])(?:[-+][A-Za-z]*)?(?:commands|functions|aliases|galiases|saliases|dis_[a-z_]+|fpath|FPATH|BASH_CMDS|BASH_ALIASES)(?![A-Za-z0-9_./-])/;
// Indirect parameter expansion, which can assign through a name held in another variable: `${(P)n::=…}`, `${!n:=…}`.
const INDIRECT_EXPANSION_RE = /\$\{(?:\([^)]*P|!)/;

/**
 * True when `command` may rebind a name through a command table or an indirect expansion. Table names are matched
 * as written and with quotes and backslashes removed: the shell joins `co""mmands` into `commands`.
 */
function writesCommandTable(command) {
  const unquoted = command.replace(/[\\'"]/g, "");
  return (
    COMMAND_TABLE_USE_RE.test(command) || COMMAND_TABLE_USE_RE.test(unquoted) || INDIRECT_EXPANSION_RE.test(command)
  );
}

/** True when a `gh api` segment issues an HTTP DELETE. */
function ghApiDeletes(tokens) {
  if (tokens.indexOf("api") === -1) return false;
  const methods = [];
  tokens.forEach((token, index) => {
    if (token === "-X" || token === "--method") {
      if (index + 1 < tokens.length) methods.push(tokens[index + 1]);
    } else if (token.startsWith("--method=")) {
      methods.push(token.slice("--method=".length));
    } else if (token.startsWith("-X")) {
      methods.push(token.slice(2).replace(/^=+/, ""));
    }
  });
  return methods.some((method) => method.toUpperCase() === "DELETE");
}

/** True when a shell invocation runs the script piped into it (`curl … | sh`). */
function readsScriptFromStdin(args) {
  if (hasShortFlag(args, "c")) return false;
  const operands = args.filter((token) => !token.startsWith("-"));
  return operands.length === 0 || hasShortFlag(args, "s") || args.indexOf("-") !== -1;
}

/** True when `unset` removes a function (`-f`, zsh `-F`) or pattern-matched names (zsh `-m`), like `unalias`. */
function unsetRemovesFunction(tokens) {
  for (const word of tokens.slice(1)) {
    if (word === "--" || !word.startsWith("-")) return false;
    if (/^-[A-Za-z]*[fFm]/.test(word)) return true;
  }
  return false;
}

/** Command-specific destructive-use checks, keyed by command word and fed the segment's tokens. */
const TOOL_CHECKS = new Map([
  ["unset", unsetRemovesFunction],
  ["git", gitIsDangerous],
  ["sed", sedEditsInPlace],
  ["gh", ghApiDeletes],
  ["set", setAssignsArray],
  ["read", readAssignsByName],
  ["printf", printAssignsVariable],
  ["print", printAssignsVariable],
  ...["declare", "typeset", "local", "export", "readonly", "integer", "float"].map((name) => [name, declaresByName]),
]);

/**
 * Find the word after a `NAME=value` assignment whose value may span several words.
 * An unterminated quote, `(` or `{` falls back to skipping the assignment word alone.
 */
function assignmentEnd(words, start) {
  let quote = "";
  let depth = 0;
  for (let index = start; index < words.length; index++) {
    const word = words[index];
    for (let at = 0; at < word.length; at++) {
      const char = word[at];
      if (char === "\\") {
        at += 1;
      } else if (quote) {
        if (char === quote) quote = "";
      } else if (char === "'" || char === '"' || char === "`") {
        quote = char;
      } else if (char === "(" || char === "{") {
        depth += 1;
      } else if ((char === ")" || char === "}") && depth) {
        depth -= 1;
      }
    }
    if (!quote && !depth) return index + 1;
  }
  return start + 1;
}

/** Whitespace words of a segment from its command word on (reserved words and assignments skipped). */
function commandWords(segment) {
  const words = segment.split(WS_SPLIT_RE).filter((word) => word);
  let index = 0;
  while (index < words.length) {
    const bare = stripChars(words[index], "(){}");
    if (!bare || SHELL_RESERVED_WORDS.has(bare)) {
      index += 1;
    } else if (ASSIGNMENT_RE.test(bare)) {
      index = assignmentEnd(words, index);
    } else {
      break;
    }
  }
  return words.slice(index);
}

/** True when the arguments of a command that merely carries another command hold a destructive one. */
function deferredIsDangerous(head, rest) {
  if (rest.indexOf("-delete") !== -1) return true;
  if (SHELL_INTERPRETERS.has(head) && readsScriptFromStdin(rest)) return true;
  for (const token of rest) {
    const bare = bareWord(token);
    if (DANGER_COMMANDS.has(bare) || REBIND_COMMANDS.has(bare)) return true;
  }
  // A command-specific check gets the words from the carried command word on (`timeout 5 set -A …` → `set -A …`).
  for (let index = 0; index < rest.length; index++) {
    const check = TOOL_CHECKS.get(bareWord(rest[index]));
    if (check && check(rest.slice(index))) return true;
  }
  return false;
}

// ── Shell words ───────────────────────────────────────────────────────────────
// Mirrors the Python generator's `shell_words` lexer: each word's text as written (`raw`), the value the shell
// builds from it after quote removal (`value`), whether quoting took part (`quoted`), and whether an unquoted
// `*`, `?`, `[` or `{` can expand it (`pattern`). Redirections and their targets are dropped.

const WS_CHAR_RE = new RegExp("[" + PY_WS + "]");
// A redirection operator with its optional file-descriptor prefix: `2>`, `>>`, `<<<`, `&>`, `>&`, `<>`.
const REDIRECTION_OPERATOR_RE = /(?:[0-9]+|&|\{[A-Za-z_][A-Za-z0-9_]*\})?(?:<<<|<<-?|<>|<&|>&|>>|>\||<|>)/y;
// A word read so far that opens an array assignment when `(` follows (`ARGS=(`), and a leading assignment word.
const ARRAY_ASSIGNMENT_OPEN_RE = /^[A-Za-z_][A-Za-z0-9_]*\+?=$/;
const ASSIGNMENT_WORD_RE = /^[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]*\])?\+?=/;
const VARIABLE_WORD_RE = /^\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))$/;
const ANSI_C_ESCAPES = new Map(
  Object.entries({ a: "\x07", b: "\b", e: "\x1b", E: "\x1b", f: "\f", n: "\n", r: "\r", t: "\t", v: "\v" }).concat(
    Object.entries({ "\\": "\\", "'": "'", '"': '"', "?": "?" }),
  ),
);
// Only the hex escape is numeric in both shells alike; `\u…` is missing from bash 3.2.
const ANSI_C_NUMERIC = new Map([["x", [/^[0-9A-Fa-f]{1,2}/, 16]]]);

/** Body of a `$'…'` string from its first inner character, escapes resolved, and the index past its close. */
function readAnsiC(text, index) {
  let out = "";
  while (index < text.length) {
    const char = text[index];
    if (char === "'") return [out, index + 1];
    if (char !== "\\") {
      out += char;
      index += 1;
      continue;
    }
    const escape = text.slice(index + 1, index + 2);
    index += 2;
    if (ANSI_C_ESCAPES.has(escape)) {
      out += ANSI_C_ESCAPES.get(escape);
    } else if (ANSI_C_NUMERIC.has(escape) || (escape && "01234567".includes(escape))) {
      const [pattern, base] = ANSI_C_NUMERIC.get(escape) || [/^[0-7]{0,2}/, 8];
      const digits = pattern.exec(text.slice(index));
      const number = (base === 8 ? escape : "") + (digits ? digits[0] : "");
      out += number && parseInt(number, base) <= 0o377 ? String.fromCharCode(parseInt(number, base)) : "\u0000";
      index += digits ? digits[0].length : 0;
    } else {
      // an escape bash and zsh decode differently (`\m`, `\u…`, `\c…`) reads as NUL, which is refused
      out += "\u0000";
      if (escape === "c" && index < text.length) index += text.codePointAt(index) > 0xffff ? 2 : 1;
    }
  }
  return [out, text.length];
}

/** Index just past a `$(…)`, backtick, `${…}`, `<(…)` or `>(…)` opening at `index`, or null when none opens there. */
function readExpansion(text, index) {
  if (text.startsWith("${", index)) return readDelimited(text, index + 2, "{", "}")[1];
  const read = readSubstitution(text, index) || readProcessSubstitution(text, index);
  return read === null ? null : read[1];
}

/** Body of a `"…"` string from its first inner character; a backslash escapes only `$`, backtick, `"`, `\`, newline. */
function readDoubleQuoted(text, index) {
  let out = "";
  while (index < text.length) {
    const char = text[index];
    if (char === '"') return [out, index + 1];
    if (char === "\\") {
      const escaped = text.slice(index + 1, index + 2);
      out += '$`"\\'.includes(escaped) ? escaped : escaped === "\n" ? "" : char + escaped;
      index += 2;
      continue;
    }
    const end = readExpansion(text, index);
    out += end !== null ? text.slice(index, end) : char;
    index = end !== null ? end : index + 1;
  }
  return [out, text.length];
}

/** One quoted or escaped piece of a word as [value, next index], or null when none opens at `index`. */
function readQuoted(text, index) {
  const char = text[index];
  if (char === "\\") {
    const escaped = text.slice(index + 1, index + 2);
    return [escaped === "\n" ? "" : escaped, index + 2];
  }
  if (char === "'") {
    let end = text.indexOf("'", index + 1);
    if (end < 0) end = text.length;
    return [text.slice(index + 1, end), end + 1];
  }
  const body = ansiCBody(text, index);
  if (body !== -1) return readAnsiC(text, body);
  if (char === '"' || text.startsWith('$"', index)) return readDoubleQuoted(text, index + (char === '"' ? 1 : 2));
  return null;
}

/** One shell word from `index` as [word, next index]; ends at whitespace, a redirection, or an unquoted paren. */
function readWord(text, index) {
  const start = index;
  let out = "";
  let quoted = false;
  let pattern = false;
  while (index < text.length) {
    const char = text[index];
    if (WS_CHAR_RE.test(char) || ((char === "<" || char === ">") && text[index + 1] !== "(")) break;
    if (char === "(" && !quoted && ARRAY_ASSIGNMENT_OPEN_RE.test(out)) {
      const end = readDelimited(text, index + 1, "(", ")")[1];
      out += text.slice(index, end);
      index = end;
      continue;
    }
    if (char === "(" || char === ")") {
      if (!out) {
        out += char;
        index += 1;
      }
      break;
    }
    const piece = readQuoted(text, index);
    if (piece !== null) {
      out += piece[0];
      quoted = true;
      index = piece[1];
      continue;
    }
    const end = readExpansion(text, index);
    pattern = pattern || (end === null && "*?[{".includes(char));
    out += text.slice(index, end !== null ? end : index + 1);
    index = end !== null ? end : index + 1;
  }
  return [{ raw: text.slice(start, index), value: out, quoted, pattern }, index];
}

/** The words the shell passes to a segment's command, quotes resolved and redirections left out. */
function shellWords(segment) {
  const words = [];
  let index = 0;
  while (index < segment.length) {
    if (WS_CHAR_RE.test(segment[index]) || segment.startsWith("\\\n", index)) {
      index += segment[index] === "\\" ? 2 : 1;
      continue;
    }
    REDIRECTION_OPERATOR_RE.lastIndex = index;
    const operator = REDIRECTION_OPERATOR_RE.exec(segment);
    if (operator && segment[REDIRECTION_OPERATOR_RE.lastIndex] !== "(") {
      index = REDIRECTION_OPERATOR_RE.lastIndex;
      while (index < segment.length && WS_CHAR_RE.test(segment[index])) index += 1;
      if (index < segment.length) index = readWord(segment, index)[1];
      continue;
    }
    let [word, end] = readWord(segment, index);
    if (end === index) {
      word = { raw: segment[index], value: segment[index], quoted: false, pattern: false };
      end = index + 1;
    }
    words.push(word);
    index = end;
  }
  return words;
}

/** Words from the command word on, past reserved words, grouping and leading assignments. */
function commandStartWords(words) {
  let index = 0;
  while (index < words.length) {
    const raw = words[index].raw;
    if (SHELL_RESERVED_WORDS.has(raw) || !stripChars(raw, "(){}") || ASSIGNMENT_WORD_RE.test(raw)) {
      index += 1;
    } else {
      break;
    }
  }
  return words.slice(index);
}

/** The inside of a `[…]` pattern class as a regex class; an out-of-order range matches nothing. */
function globClass(body) {
  const negate = body[0] === "!" || body[0] === "^";
  const chars = negate ? body.slice(1) : body;
  const escape = (char) => char.replace(/[\\\][^-]/g, "\\$&");
  const parts = [];
  let index = 0;
  while (index < chars.length) {
    if (index + 2 < chars.length && chars[index + 1] === "-") {
      const low = chars[index];
      const high = chars[index + 2];
      if (low <= high) parts.push(escape(low) + "-" + escape(high));
      index += 3;
    } else {
      parts.push(escape(chars[index]));
      index += 1;
    }
  }
  if (parts.length === 0) return negate ? "." : "(?!)";
  return "[" + (negate ? "^" : "") + parts.join("") + "]";
}

/** A shell filename pattern as a full-match regex: `*`, `?` and `[…]`/`[!…]` classes, the rest literal. */
function globRegex(pattern) {
  let out = "";
  let index = 0;
  while (index < pattern.length) {
    const char = pattern[index];
    const close = char === "[" ? pattern.indexOf("]", index + 2) : -1;
    if (char === "*") {
      out += ".*";
    } else if (char === "?") {
      out += ".";
    } else if (close > 0) {
      out += globClass(pattern.slice(index + 1, close));
      index = close;
    } else {
      out += char.replace(/[.*+?^${}()|[\]\\/-]/g, "\\$&");
    }
    index += 1;
  }
  return new RegExp("^(?:" + out + ")$");
}

/** True when an unquoted pattern in command position can expand to a refused command name (`r?`, `{ha,}sh`). */
// A `[…]` pattern class with a quote or backslash inside it.
const QUOTED_CLASS_RE = /\[[^\]]*[\\'"]/;

function spellingHidesName(word) {
  if (!word.pattern) return false;
  let name = word.value.slice(word.value.lastIndexOf("/") + 1);
  if (name.includes("{") && (name.includes(",") || name.includes(".."))) return true;
  // a POSIX bracket class (`[[:alpha:]]`) can match any letter of a refused name
  if (name.includes("[:") || name.includes("[=") || name.includes("[.")) return true;
  // quote removal turns a quoted `!` or `-` in a class into negation or a range: count the class as any one character
  if (QUOTED_CLASS_RE.test(word.raw)) name = name.replace(/\[[^\]]*\]/g, "?");
  const matcher = globRegex(name);
  for (const refused of [...DANGER_COMMANDS, ...REBIND_COMMANDS]) {
    if (matcher.test(refused)) return true;
  }
  return false;
}

// Long shell options whose value is the next word.
const SHELL_VALUE_OPTIONS = new Set(["--rcfile", "--init-file"]);

/**
 * Every argument that may be a shell's `-c` script. Options are read as clusters, each `o`/`O` letter taking the
 * next word; once a cluster carries `c`, every later argument is returned, so a misplaced value is still judged.
 */
function shellScript(args) {
  let index = 0;
  while (index < args.length) {
    const option = args[index];
    if (option === "--" || !(option.startsWith("-") || option.startsWith("+")) || option.length < 2) return [];
    if (option.startsWith("-") && hasShortFlag([option], "c"))
      return args.slice(index + 1).filter((arg) => arg !== "--");
    let values = 0;
    if (SHELL_VALUE_OPTIONS.has(option)) values = 1;
    else if (!option.startsWith("--")) values = (option.match(/[oO]/g) || []).length;
    index += 1 + values;
  }
  return [];
}

// A backslash escape or a single-quoted span (`'…'`, `$'…'`), where `$` and a backtick expand nothing.
const INERT_SPAN_RE = /\\[\s\S]|\$?'[^']*'/g;

/** Index just past the `${…}`, `$(…)` or backtick expansion opening at `start`; unterminated runs to the end. */
function expansionEnd(text, start) {
  if (text[start] === "`") {
    const end = text.indexOf("`", start + 1);
    return end === -1 ? text.length : end + 1;
  }
  const opening = text[start + 1];
  const closing = opening === "{" ? "}" : ")";
  let depth = 0;
  for (let index = start + 1; index < text.length; index++) {
    if (text[index] === opening) {
      depth += 1;
    } else if (text[index] === closing) {
      depth -= 1;
      if (depth === 0) return index + 1;
    }
  }
  return text.length;
}

/** The text with each `${…}`, `$(…)` and backtick expansion collapsed to a lone `$`. */
function collapseExpansions(text) {
  let out = "";
  let index = 0;
  while (index < text.length) {
    if (text[index] === "`" || (text[index] === "$" && (text[index + 1] === "{" || text[index + 1] === "("))) {
      index = expansionEnd(text, index);
      out += "$";
    } else {
      out += text[index];
      index += 1;
    }
  }
  return out;
}

/** The part of a word after its last path separator outside any expansion (`h${x:-/}sh` stays whole). */
function lastSegment(text) {
  const collapsed = collapseExpansions(text);
  return collapsed.slice(collapsed.lastIndexOf("/") + 1);
}

/** True when a word's last path segment holds a live expansion after its first character (`h${x}sh`). */
function expandsMidWord(word) {
  // a quoted or escaped span stands in as one literal character, so `'h'${x}sh` keeps its expansion mid-word
  const name = lastSegment(word.raw.replace(INERT_SPAN_RE, "_").replace(/"/g, ""));
  return name.slice(1).includes("$") || name.slice(1).includes("`");
}

/**
 * Every string `env -S`/`--split-string` may split into a command line. `S` is found anywhere in a short-option
 * cluster (`-iS`); the rest of the cluster after it and the next word are both returned.
 */
function envSplitString(args) {
  for (let index = 0; index < args.length; index++) {
    const arg = args[index];
    if (arg === "--split-string") return args.slice(index + 1, index + 2);
    if (arg.startsWith("--split-string=")) return [arg.slice(arg.indexOf("=") + 1)];
    if (/^-[A-Za-z]*S/.test(arg)) {
      const rest = arg.slice(arg.indexOf("S") + 1);
      return (rest ? [rest] : []).concat(args.slice(index + 1, index + 2));
    }
  }
  return [];
}

/**
 * Command lines a deferring command runs from one argument: a shell's `-c` string, a `trap` action, a `watch`
 * command, an `env -S` string. A lone variable (`bash -c "$METRIC_CMD"`) is left to the allowlist gate.
 */
function carriedCommands(values) {
  const texts = [];
  values.forEach((value, index) => {
    const name = bareWord(value);
    if (SHELL_INTERPRETERS.has(name)) {
      texts.push(...shellScript(values.slice(index + 1)));
    } else if (name === "trap" || name === "watch") {
      texts.push(...values.slice(index + 1).filter((arg) => !arg.startsWith("-")));
    } else if (name === "env") {
      texts.push(...envSplitString(values.slice(index + 1)));
    }
  });
  return texts.filter((text) => !VARIABLE_WORD_RE.test(text));
}

// One `case` pattern: unquoted chars other than space, parentheses and `|`, escapes, or quoted runs.
// ASCII whitespace spelled out, matching the Python generator's `_CASE_PATTERN` exactly.
const CASE_PATTERN = "(?:[^ \\t\\n\\r\\f\\v()'\"|\\\\]|\\\\.|\"[^\"]*\"|'[^']*')+";
const CASE_LABEL_RE = new RegExp("^[ \\t\\n\\r\\f\\v]*\\(?" + CASE_PATTERN + "\\)");
const CASE_IN_RE = /[ \t]in(?:[ \t]|$)/;
const FUNCTION_NAME_RE = /^[A-Za-z_][A-Za-z0-9_-]*$/;

/** Index just past a leading `case` arm label (`b)`), or -1 when the text opens with none. */
function caseLabelEnd(text) {
  const match = CASE_LABEL_RE.exec(text);
  return match ? match[0].length : -1;
}

/** Command text of a `case` arm written on the `case … in` line itself, or null. */
function caseLineArm(text) {
  if (commandWords(text)[0] !== "case") return null;
  const keyword = CASE_IN_RE.exec(text);
  if (!keyword) return null;
  const after = text.slice(keyword.index + keyword[0].length);
  const end = caseLabelEnd(after);
  return end === -1 ? null : after.slice(end);
}

/** Name a segment defines as a shell function (`f() {`, `f () {`, `function f {`), or null. */
function functionName(words) {
  let name = "";
  if (words.length > 1 && words[0] === "function") {
    name = words[1].endsWith("()") ? words[1].slice(0, -2) : words[1];
  } else if (words.length > 0 && words[0].endsWith("()")) {
    name = words[0].slice(0, -2);
  } else if (words.length > 1 && words[1] === "()") {
    name = words[0];
  }
  return FUNCTION_NAME_RE.test(name) ? name : null;
}

/** Words after a function definition's name and opening brace. */
function functionBody(words) {
  const start = words[0] === "function" || (words.length > 1 && words[1] === "()") ? 2 : 1;
  let body = words.slice(start);
  while (body.length > 0 && (body[0] === "{" || body[0] === "(")) body = body.slice(1);
  return body;
}

/**
 * Commands a segment runs behind syntax that hides them from the command-word check: a `case`
 * label, an arm on the `case … in` line, a function definition. Judged in addition to the
 * segment as written, never instead of it.
 */
function hiddenCommands(segment) {
  const hidden = [];
  const labelEnd = caseLabelEnd(segment);
  if (labelEnd !== -1) hidden.push(segment.slice(labelEnd));
  const arm = caseLineArm(segment);
  if (arm !== null) hidden.push(arm);
  const words = commandWords(segment);
  if (functionName(words) !== null) hidden.push(functionBody(words).join(" "));
  return hidden.concat(compoundBodies(segment));
}

// Words opening a loop or condition whose zsh short or brace form (`for x (a) cmd`, `if true { cmd }`, `repeat 3 cmd`)
// or bash `coproc` runs a command later on the same line.
const COMPOUND_HEADS = new Set(["for", "foreach", "select", "repeat", "if", "elif", "while", "until", "coproc"]);

/**
 * Text after each word that may close a loop list or condition in a compound command: after a `)`, `{`, `}` or `]]`,
 * after `repeat`'s count, or right after `coproc`. Judging a list word as a command can only refuse a block.
 */
function compoundBodies(segment) {
  const words = shellWords(segment).map((word) => word.raw);
  if (words.length === 0 || !COMPOUND_HEADS.has(words[0])) return [];
  const starts = new Set(words[0] === "coproc" ? [1] : words[0] === "repeat" ? [2] : []);
  words.forEach((word, index) => {
    if (word === "{" || word === "}" || word === "]]" || word.endsWith(")")) starts.add(index + 1);
  });
  return [...starts]
    .sort((a, b) => a - b)
    .filter((start) => start <= words.length)
    .map((start) => words.slice(start).join(" "));
}

// Builtins whose options name a variable or array to fill (`printf -v`, `set -A`, `read -a`, `declare -n`,
// `unset -f`), so an option supplied by an expansion may rebind a command name.
const OPTION_REBINDERS = new Set([
  "set",
  "read",
  "printf",
  "print",
  "unset",
  "declare",
  "typeset",
  "local",
  "export",
  "readonly",
  "integer",
  "float",
]);

/**
 * True when an expansion stands where a builtin still reads options, so its value may be one (`printf "$o" …` with
 * `o=-v`). `printf`/`print` read options only before their first other word; the others anywhere before `--`.
 */
function optionFromExpansion(head, words) {
  const leadingOnly = head === "printf" || head === "print";
  let index = 0;
  while (index < words.length) {
    const word = words[index];
    const live = word.raw.replace(INERT_SPAN_RE, "_");
    if (word.value === "--") return false;
    const first = live.replace(/^"+/, "").charAt(0);
    if (first === "$" || first === "`") return true;
    if ((word.value.startsWith("-") || word.value.startsWith("+")) && word.value.length > 1) {
      if (live.includes("$") || live.includes("`")) return true;
      index += head === "print" && "ufCxX".includes(word.value[word.value.length - 1]) ? 2 : 1;
      continue;
    }
    if (leadingOnly) return false;
    index += 1;
  }
  return false;
}

/**
 * True when one segment runs a destructive command, including one hidden behind a label or function definition.
 * A `case` label is a pattern, never a command, so a labelled segment is judged by the command after its label.
 */
function segmentIsDangerous(segment) {
  const hidden = hiddenCommands(segment);
  return (caseLabelEnd(segment) !== -1 ? hidden : [segment, ...hidden]).some(commandIsDangerous);
}

/**
 * True when one command runs a destructive command, directly or through a deferring command. The segment is read as
 * shell words (quotes resolved, redirections dropped); a command-word pattern that can expand to a refused name and
 * a table name in any word are refused, and command text a wrapper carries in one argument is judged as a command.
 */
function commandIsDangerous(segment) {
  const words = commandStartWords(shellWords(segment));
  if (words.length === 0) return false;
  const values = words.map((word) => word.value);
  // bash ends a $'…' string at a decoded NUL and zsh keeps it: no single reading is right for both shells
  if (values.some((value) => value.indexOf("\u0000") !== -1)) return true;
  const head = bareWord(values[0]);
  if (DANGER_COMMANDS.has(head) || REBIND_COMMANDS.has(head) || spellingHidesName(words[0])) return true;
  const base = lastSegment(values[0]);
  if (base.includes("$") || base.includes("`") || values.some((value) => COMMAND_TABLE_USE_RE.test(value))) {
    return true;
  }
  if (OPTION_REBINDERS.has(head) && optionFromExpansion(head, words.slice(1))) return true;
  const check = TOOL_CHECKS.get(head);
  if (check) return check(values);
  if (!DEFERRING_COMMANDS.has(head)) return false;
  if (words.slice(1).some((word) => spellingHidesName(word) || expandsMidWord(word))) return true;
  return deferredIsDangerous(head, values.slice(1)) || carriedCommands(values).some(isDangerous);
}

/**
 * True when any segment of `command` is destructive. Text ending inside an unclosed quote is refused: the reader and
 * the shell disagree about where its commands start.
 */
function isDangerous(command) {
  if (writesCommandTable(command) || quotingUnsettled(command)) return true;
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
  if (aliasesEscapedBlank(command) || isDangerous(normalize(command))) return null;
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
  if (aliasesEscapedBlank(command) || isDangerous(normalized)) return declined("danger");
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
  aliasesEscapedBlank,
  sha256Text,
  matchBlueprint,
  decide,
  evaluate,
};
