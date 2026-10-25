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
 * Read a `<(...)`, `>(...)` or zsh `=(...)` process substitution opening at `index`.
 * Only the two-character opener triggers this; a bare `<`/`>` redirection is left alone
 * and is not a segment separator. zsh runs the body of `=(...)` only when the opener starts
 * a word, so `x=(a b)` stays an array.
 * @returns {[string, number]|null} inner text and next index, or null when none opens here.
 */
function readProcessSubstitution(text, index) {
  const char = text[index];
  if ((char === "<" || char === ">") && text.startsWith("(", index + 1)) {
    return readDelimited(text, index + 2, "(", ")");
  }
  const wordStart = index === 0 || WS_CHAR_RE.test(text[index - 1]) || ";&|(".includes(text[index - 1]);
  if (wordStart && text.startsWith("=(", index)) return readDelimited(text, index + 2, "(", ")");
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
function scanTopLevel(text, substitutionSpans = null) {
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
        if (substitutionSpans !== null) substitutionSpans.push([read[0], index + (char === "`" ? 1 : 2)]);
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
      if (substitutionSpans !== null) substitutionSpans.push([read[0], index + (char === "`" ? 1 : 2)]);
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
  "source",
]);
// Deferring commands that can run a builtin, so `command . file` sources a file like `. file`. `.` is checked only as
// the first operand of one of these: elsewhere it is a directory (`find . -name x`).
const BUILTIN_RUNNERS = new Set(["command", "noglob", "nocorrect", "-", "time"]);
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
 * True when a `printf`/`print` writes a variable through `-v`. Only leading options count for `-v`;
 * a zsh `print` value option (`-u 2`) consumes the next word. This callback also covers resolved wrapped commands.
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
// An expansion that treats a variable's value as code or as a name: `${(P)n::=…}` and `${!n:=…}` assign through a
// held name; zsh `${(e)x}`, `${(%)x}`, `${(#)x}` and bash `${x@P}` evaluate the value; zsh `${~x}`/`$~x` globs it, so a
// `(e:…:)` qualifier in the value runs. Stored text is inert to the lexer, so each form is refused.
const INDIRECT_EXPANSION_RE = /\$\{(?:\([^)]*[Pe%#]|!)|\$\{[^}]*@P|\$\{?~/;
// Variables that decide which program a command name runs or which library it loads, which file a shell runs at
// start-up, text a shell expands as code later, how words split (`IFS`) or which options are on (zsh `options`).
// `sentinel-read-allow.js`'s SENSITIVE_VARS holds every other name here; `IFS` has its own check there.
const LOOKUP_VARIABLES =
  "PATH|path|CDPATH|cdpath|BASH_ENV|ENV|ZDOTDIR|SHELLOPTS|BASHOPTS|GLOBIGNORE|PROMPT_COMMAND" +
  "|LD_PRELOAD|LD_LIBRARY_PATH|LD_AUDIT|DYLD_[A-Z_]+|PS[0-4]|PROMPT[0-4]?|RPROMPT2?|RPS[12]|SPROMPT" +
  "|(?:precmd|preexec|chpwd|periodic|zshexit|zshaddhistory)_functions|IFS|options";
// A word assigning a lookup variable (`PATH=…`, `path+=(…)`, `path[1]=…`), wherever it stands in a segment.
const LOOKUP_VARIABLE_ASSIGNMENT_RE = new RegExp("^(?:" + LOOKUP_VARIABLES + ")(?:\\[[^\\]]*\\])?\\+?=[\\s\\S]*$");
// A raw write is refused in every operand and quote state, before the existing position checks.
const LOOKUP_WRITE_TOKEN_RE = new RegExp(
  "(?<![A-Za-z0-9_])(?:(?:\\+\\+|--)(?:" +
    LOOKUP_VARIABLES +
    ")(?![A-Za-z0-9_])|(?:" +
    LOOKUP_VARIABLES +
    ")(?:\\[[^\\]]*\\])?(?:\\+\\+|--|[ \\t\\n\\r\\v\\f]*(?:\\*\\*=|<<=|>>=|&&=|\\|\\|=|\\^\\^=|[+*/%&|^\\-]=|=(?!=))))",
  "g",
);
const LOOKUP_WRITE_FLAG_RE = new RegExp("^(?:\\$?'|\\\")?--?(?:" + LOOKUP_VARIABLES + ")=(?!=)");
// Closed format language: string widths, literal percent signs and three text escapes only.
const PRINTF_TEXT_FORMAT_RE = /^(?:[^%\\]|%%|%(?:-?[0-9]+)?s|\\[nt\\])*$/;

// Closed inert-argument allowlist, mirrored in the generator and the shape lane.
const LOOKUP_TEXT_PROGRAMS = new Set([
  "python",
  "python3",
  "node",
  "git",
  "gh",
  "grep",
  "rg",
  "sed",
  "awk",
  "jq",
  "cat",
  "ls",
  "find",
  "mkdir",
  "cp",
  "mv",
  "date",
  "wc",
  "sort",
  "head",
  "tail",
  "tr",
  "cut",
  "diff",
  "uv",
  "pytest",
  "pre-commit",
]);

// A word naming a lookup variable on its own, which assigns it only as an operand of a name-assigning command.
const LOOKUP_VARIABLE_NAME_RE = new RegExp("^(?:-[A-Za-z]*)?(?:" + LOOKUP_VARIABLES + ")(?:\\[[^\\]]*\\])?$");
// A lookup variable named inside arithmetic, read (`$PATH`) or assigned (`PATH`).
const LOOKUP_ARITHMETIC_NAME_RE = new RegExp("^\\$?(?:" + LOOKUP_VARIABLES + ")$");
// Commands that assign or unset a variable named by a bare operand; elsewhere a bare `path` is an ordinary argument.
// `print`/`printf` assign only through `-v` and `set` only through `-A`, which their own checks refuse; a loop
// keyword assigns only its loop variable.
const NAME_ASSIGNERS = new Set([
  "read",
  "getopts",
  "unset",
  "vared",
  "zparseopts",
  "zstyle",
  "zformat",
  "typeset",
  "declare",
  "local",
  "export",
  "readonly",
  "integer",
  "float",
  "let",
]);
// Loop keywords whose next word is the variable each pass assigns.
const LOOP_KEYWORDS = new Set(["for", "foreach", "select"]);
// Commands that run the command word after them, past their own options (`command read PATH`).
const COMMAND_RUNNERS = new Set(["command", "builtin", "exec", "noglob", "nocorrect", "-", "time"]);
// The option letters a runner may carry, none taking an operand; any other (`exec -a NAME`) leaves the command word
// unknown, so the segment is refused.
const RUNNER_OPTION_LETTERS = { command: "pvV", exec: "cl", time: "p" };
// Commands whose operands are only text to print or match: an operand spelled like an assignment assigns nothing
// there. Any other command may read `NAME=value` operands as assignments (`env`, `export`, `make`).
const TEXT_COMMANDS = new Set(["echo", "printf", "print", "grep", "egrep", "fgrep", "rg"]);
// Commands that make every later assignment to a name arithmetic, and those that do so through an `-i` option.
const ARITHMETIC_COMMANDS = new Set(["integer", "float", "let"]);
const ATTRIBUTE_DECLARERS = new Set(["typeset", "declare", "local", "export", "readonly"]);
// A word opening an array assignment (`NAME=(`, `NAME+=(`) as written.
const ARRAY_ASSIGNMENT_WORD_RE = /^[A-Za-z_][A-Za-z0-9_]*\+?=\(/;
// A subscripted name (`a[…]`) and the subscripts that hold no arithmetic able to assign or run anything.
const SUBSCRIPT_RE = /[A-Za-z_][A-Za-z0-9_]*\[([^\]]*)\]/g;
const PLAIN_SUBSCRIPT_RE = /^(?:[0-9]+|[@*]|\$?[A-Za-z_][A-Za-z0-9_]*)$/;
// Shell options a block may switch, by exact unquoted name; any other (`GLOB_SUBST` in any spelling) is refused.
const SAFE_SHELL_OPTIONS = new Set(["pipefail", "errexit", "nounset", "nomatch"]);
const OPTION_SETTERS = new Set(["setopt", "unsetopt", "shopt"]);
// A `set` cluster limited to `e`/`u`/`x`/`a`/`o`; a shell cluster taking an option name next (`-o`, `-xo`, `-O`).
const SET_FLAG_CLUSTER_RE = /^[-+][euxao]+$/;
const SHELL_OPTION_CLUSTER_RE = /^[-+][A-Za-z]*[oO][A-Za-z]*$/;
const SHELL_LONG_OPTIONS = new Set(["--", "--norc", "--noprofile", "--login"]);
// The first character of a word's value (quotes and escapes resolved; expansions, tilde, globs and braces kept as
// written) that ends a shell's options: a literal letter, digit, `_`, `.` or `/`. Every word the shell builds from it
// starts with that character, so none can be a flag; any other lead (`'-o'`, `"$F"`, `~` with `HOME=-o`) may.
const OPTION_OPERAND_LEAD_RE = /^[A-Za-z0-9_./]/;
// An opener of command substitution or bash `$[…]` arithmetic, whose text runs when an expansion evaluates it.
const CODE_OPENER_RE = /\$[(\[]|`/;
// A heredoc operator in the only forms the reader models: `<<`/`<<-`, blanks, then an identifier delimiter, bare
// or wholly in one pair of quotes, ending the word. `<< \EOF`, `<<$'EOF'`, `<<EOF.` are refused.
const HEREDOC_OPENING_RE =
  /<<(-?)[ \t]*(?:([A-Za-z_][A-Za-z0-9_]*)|'([A-Za-z_][A-Za-z0-9_]*)'|"([A-Za-z_][A-Za-z0-9_]*)")(?=[ \t;&|<>)\n]|$)/y;
// The parameter a `${…}` expansion reads, its length form, its reading operators and a digit substring offset.
const PARAMETER_NAME_RE = /(?:[A-Za-z_][A-Za-z0-9_]*|[0-9]+|[?#@*$-])(?:\[(?:@|\*|[0-9]+)\])?/y;
const PARAMETER_LENGTH_RE = new RegExp("^#(?:" + PARAMETER_NAME_RE.source + ")$");
const PARAMETER_OPERATOR_RE = /^(?::-|:\+|-|\+|##|#|%%|%|\/\/|\/)/;
const PARAMETER_OFFSET_RE = /^:[0-9]+(?::[0-9]+)?$/;
// Arithmetic text that assigns, shifts or indexes, or spans lines; an operand (a number, name, `$NAME` or numeric
// special parameter `$?` `$#` `$$` `$!` `$-`, which `exit $?` already reads) and a binary operator.
const ARITHMETIC_REFUSED_RE = /\+\+|--|<<|>>|(?<![=!<>])=(?!=)|[\n,[]/;
const ARITHMETIC_OPERAND_RE = /[0-9]+|\$?[A-Za-z_][A-Za-z0-9_]*|\$[?#$!*@-]/y;
const ARITHMETIC_BINARY_RE = /&&|\|\||<=|>=|==|!=|[-+*/%<>?:]/y;
// Commands whose count operand zsh evaluates as arithmetic (bash reads a plain number; the Bash tool's shell is zsh
// on macOS): `shift`/`break`/`continue`/`return`/`exit` (zshbuiltins) and `repeat` (zshmisc).
const ARITHMETIC_COUNT_COMMANDS = new Set(["shift", "break", "continue", "return", "exit", "repeat"]);
// Comparison operators of `[[ … ]]` whose operands undergo arithmetic evaluation ("as if enclosed in $((…))"); `test`
// and `[` take integers only, so they are excluded.
const CONDITIONAL_ARITHMETIC_OPS = new Set(["-eq", "-ne", "-lt", "-le", "-gt", "-ge"]);
// Special parameters zsh integer-attributes by default (`typeset +i`), so a plain `NAME=value` assignment evaluates
// value as arithmetic (zshparam). bash integer-attributes only EUID/UID/PPID, so this is the zsh superset.
const INTEGER_PARAMETERS =
  "ARGC|COLUMNS|EGID|ERRNO|EUID|FUNCNEST|GID|HISTCMD|HISTSIZE|KEYTIMEOUT|LINENO|LINES|LISTMAX|MAILCHECK" +
  "|OPTIND|PPID|RANDOM|SAVEHIST|SECONDS|SHLVL|TRY_BLOCK_ERROR|TRY_BLOCK_INTERRUPT|TTYIDLE|UID|ZLE_RPROMPT_INDENT|ZSH_SUBSHELL";
const INTEGER_ASSIGNMENT_RE = new RegExp("^(?:" + INTEGER_PARAMETERS + ")\\+?=");
// An array-literal element key (`a=([key]=value)`), evaluated in arithmetic context.
const ARRAY_ELEMENT_KEY_RE = /\[([^\]]*)\]\+?=/g;
// A zsh `$name[…]` subscript, live inside double quotes too. Its arithmetic text is admitted only when made of names,
// numbers, `@`/`*`, reading operators, `.` and the `[` of a bracket class (shipped `"$PLUGIN[^/]*"`); increments and
// a lookup variable (LOOKUP_ARITHMETIC_NAME_RE) are refused within it.
const ZSH_SUBSCRIPTED_RE = /\$[A-Za-z_][A-Za-z0-9_]*\[([^\]]*)/y;
// What may follow a `$` in live text besides the `${`/`$(`/backtick openers read on their own: a name or digit, a
// special parameter, or a character that opens no expansion in bash or zsh. A quote also counts inside double quotes
// and heredoc bodies. Every other form (zsh `$~x` `$=x` `$^x`, `$[…]`) is outside the grammar. Mirrors `_DOLLAR_FOLLOW`.
const DOLLAR_FOLLOW_RE = /[A-Za-z0-9_?#@*$!-]|[ \t\n/.,:;|&)<>\\%}\]]|$/y;
const SUBSCRIPT_TEXT_RE = /^[A-Za-z0-9_@*^/!,.:?<>%+[-]*$/;
const SUBSCRIPT_REFUSED_RE = new RegExp("\\+\\+|--|(?<![A-Za-z0-9_])(?:" + LOOKUP_VARIABLES + ")(?![A-Za-z0-9_])");
// A word that a following `()` makes a function definition.
const FUNCTION_WORD_RE = /^[A-Za-z0-9_.:-]+$/;
// Reserved words after which a `(` still opens a subshell (`if (…)`, `! (…)`, `{ (…)`), as written.
const GROUP_OPENERS = new Set([...SHELL_RESERVED_WORDS, "{"]);

/** Hide ordinary single quotes while retaining double-quoted and ANSI-C expansion spellings. */
function maskSingleQuoted(text) {
  const out = text.split("");
  let index = 0;
  let double = false;
  while (index < text.length) {
    if (text[index] === "\\") {
      index += 2;
      continue;
    }
    if (text[index] === '"') double = !double;
    if (!double && text.startsWith("$'", index)) {
      index = readAnsiC(text, index + 2)[1];
      continue;
    }
    if (!double && text[index] === "'") {
      const close = text.indexOf("'", index + 1);
      const end = close < 0 ? text.length : close + 1;
      out.fill(" ", index, end);
      index = end;
      continue;
    }
    index++;
  }
  return out.join("");
}

/** Detect command-table rebinding and indirect expansion outside ordinary single quotes. */
function writesCommandTable(command) {
  const [text, live, unterminated] = splitHeredocs(command);
  if (unterminated) return true;
  const visible = maskSingleQuoted(text);
  const unquoted = visible.replace(/[\\'"]/g, "");
  return (
    COMMAND_TABLE_USE_RE.test(visible) ||
    COMMAND_TABLE_USE_RE.test(unquoted) ||
    INDIRECT_EXPANSION_RE.test(visible) ||
    live.some(writesCommandTable)
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
  if (BUILTIN_RUNNERS.has(head) && rest.find((token) => !token.startsWith("-")) === ".") return true;
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
// Shell word boundaries are narrower than normalization whitespace; unusual name characters must remain visible.
const SHELL_WORD_BLANK_RE = /[ \t\n]/;
// A redirection operator with its optional file-descriptor number: `2>`, `>>`, `<<<`, `&>`, `>&`, `<>`. The
// descriptor-allocating `{name}>` form is no prefix here: it assigns `name` a number, so the grammar scan refuses it
// (FD_VARIABLE_PREFIX_RE) and this reader keeps `{name}` as a word.
const REDIRECTION_OPERATOR_RE = /(?:[0-9]+|&)?(?:<<<|<<-?|<>|<&|>&|>>|>\||<|>)/y;
// A word written right against a redirection operator as a brace group (`{name}>`, `{IFS}<`): bash and zsh allocate a
// descriptor and assign its number to the name, a variable write no `=` shows. Only a digit descriptor is admitted.
const FD_VARIABLE_PREFIX_RE = /^\{[^}]*\}$/;
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
    if (SHELL_WORD_BLANK_RE.test(char) || ((char === "<" || char === ">") && text[index + 1] !== "(")) break;
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
  return shellWordSpans(segment).map(([word]) => word);
}

/** Each word shellWords reads, as [word, index just past it in `segment`]. */
function shellWordSpans(segment) {
  const spans = [];
  let index = 0;
  while (index < segment.length) {
    if (SHELL_WORD_BLANK_RE.test(segment[index]) || segment.startsWith("\\\n", index)) {
      index += segment[index] === "\\" ? 2 : 1;
      continue;
    }
    REDIRECTION_OPERATOR_RE.lastIndex = index;
    const operator = REDIRECTION_OPERATOR_RE.exec(segment);
    if (operator && segment[REDIRECTION_OPERATOR_RE.lastIndex] !== "(") {
      index = REDIRECTION_OPERATOR_RE.lastIndex;
      while (index < segment.length && SHELL_WORD_BLANK_RE.test(segment[index])) index += 1;
      if (index < segment.length) index = readWord(segment, index)[1];
      continue;
    }
    let [word, end] = readWord(segment, index);
    if (end === index) {
      word = { raw: segment[index], value: segment[index], quoted: false, pattern: false };
      end = index + 1;
    }
    spans.push([word, end]);
    index = end;
  }
  return spans;
}

/** Index of the first word past reserved words, grouping and leading assignments, as written. */
function commandStartIndex(words) {
  let index = 0;
  while (index < words.length) {
    const raw = words[index].raw;
    if (SHELL_RESERVED_WORDS.has(raw) || !stripChars(raw, "(){}") || ASSIGNMENT_WORD_RE.test(raw)) {
      index += 1;
    } else {
      break;
    }
  }
  return index;
}

/** Words from the command word on, past reserved words, grouping and leading assignments. */
function commandStartWords(words) {
  return words.slice(commandStartIndex(words));
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
const FUNCTION_NAME_RE = /^[A-Za-z_][A-Za-z0-9_-]*$/;

/** Index just past a leading `case` arm label (`b)`), or -1 when the text opens with none. */
function caseLabelEnd(text) {
  const match = CASE_LABEL_RE.exec(text);
  return match ? match[0].length : -1;
}

/**
 * Command text of a `case` arm written on the `case … in` line itself, or null. Read on shell words, as written:
 * `case` and `in` count only unquoted and the subject is one whole word, so `case 'x in y' in` never ends early.
 */
function caseLineArm(text) {
  const spans = shellWordSpans(text);
  const start = commandStartIndex(spans.map(([word]) => word));
  if (spans.length < start + 3 || spans[start][0].raw !== "case" || spans[start + 2][0].raw !== "in") return null;
  const after = text.slice(spans[start + 2][1]);
  const end = caseLabelEnd(after);
  return end === -1 ? null : after.slice(end);
}

/**
 * A segment and each text left as its `case` arm patterns come off one by one; the last is its command. A pattern
 * is no word of the command it guards, and an arm may open another `case`, so labels come off until none is left.
 * Every reader that skips a label uses this chain, so a nested arm's command is never left unjudged.
 */
function caseArmChain(segment) {
  const chain = [segment];
  for (;;) {
    const text = chain[chain.length - 1];
    let arm = caseLineArm(text);
    if (arm === null) {
      const end = caseLabelEnd(text);
      arm = end > 0 ? text.slice(end) : null;
    }
    if (arm === null || arm === text) return chain;
    chain.push(arm);
  }
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
 * The segment and every command text it runs behind syntax that hides it from the command-word check: each `case`
 * label or arm on the `case … in` line, a function definition, a zsh short or brace compound form. Each hidden text
 * is read again, so an arm whose command is a compound, a function or another `case` is followed to its innermost
 * command. The one reader of "what runs here" every judge consumes (judgedTexts), mirroring `_command_texts`.
 */
function commandTexts(segment) {
  return commandReading(segment).texts;
}

/**
 * A segment's command texts (commandTexts) and whether it holds brace syntax the reader skips, mirroring
 * `_command_reading`. A bare `{`/`}` word opens or closes a body only where the reader follows it: before the command
 * word, in a compound text whose every brace is followed (compoundBodies), around a function body, or as the closing
 * `}` run at the end of a text. Any other brace word (zsh `case WORD {`, `} always {`) leaves the segment unmodeled.
 */
function commandReading(segment) {
  const texts = [];
  let unmodeled = false;
  // each text carries whether its braces are already followed: a compound body is a suffix of a text whose every
  // brace word starts another suffix
  const queue = [[segment, false]];
  while (queue.length) {
    const [text, followed] = queue.shift();
    if (texts.includes(text)) continue;
    texts.push(text);
    const words = commandWords(text);
    const defines = functionName(words) !== null;
    if (!(followed || defines || isCaseSyntax(text)) && braceUnfollowed(text)) unmodeled = true;
    queue.push(
      ...caseArmChain(text)
        .slice(1)
        .map((arm) => [arm, false]),
    );
    if (defines) queue.push([functionBody(words).join(" "), false]);
    queue.push(...compoundBodies(text).map((body) => [body, true]));
  }
  return { texts, unmodeled };
}

/**
 * True when a command text holds a bare `{` or `}` word after its command word, other than a closing `}` run at its
 * end. A text opening with a COMPOUND_HEADS word has every brace followed by compoundBodies; a runner option that hides
 * the command word leaves any brace unplaced. Mirrors `_brace_unfollowed`.
 */
function braceUnfollowed(text) {
  const words = shellWords(text);
  const raws = words.map((word) => word.raw);
  const braces = [];
  raws.forEach((raw, index) => {
    if (raw === "{" || raw === "}") braces.push(index);
  });
  if (braces.length === 0 || COMPOUND_HEADS.has(raws[0])) return false;
  const start = commandWordIndex(words);
  if (start === null) return true;
  let closing = raws.length;
  while (closing > 0 && raws[closing - 1] === "}") closing -= 1;
  return braces.some((index) => start < index && index < closing);
}

/**
 * True when a text opens with `case` syntax rather than a command: an arm label or a `case … in` arm. A text opening
 * with `(` is judged as written too, since `(rm)` is a subshell running a command.
 */
function isCaseSyntax(text) {
  const label = caseLabelEnd(text) !== -1 && !stripChars(text, " \t\n\r\f\v").startsWith("(");
  return label || caseLineArm(text) !== null;
}

/** The command texts of a segment that are commands rather than `case` syntax; a labelled text is judged by its arm. */
function judgedTexts(segment) {
  return commandTexts(segment).filter((text) => !isCaseSyntax(text));
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
 * True when one segment runs a destructive command, including one hidden behind a label, a function definition or
 * a compound form. A `case` label is a pattern, never a command, so a labelled text is judged by its arm. Brace
 * syntax the reader does not follow (commandReading) is refused whatever it runs.
 */
function segmentIsDangerous(segment) {
  const { texts, unmodeled } = commandReading(segment);
  return unmodeled || texts.some((text) => !isCaseSyntax(text) && commandIsDangerous(text));
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
  if (DANGER_COMMANDS.has(head) || REBIND_COMMANDS.has(head) || head === "." || spellingHidesName(words[0])) {
    return true;
  }
  const base = lastSegment(values[0]);
  const textPositions = lookupTextPositions(words, commandWordIndex(words), new Set());
  if (
    base.includes("$") ||
    base.includes("`") ||
    words.some(
      (word, index) =>
        !(textPositions.has(index) && /^'[^']*'$/.test(word.raw)) && COMMAND_TABLE_USE_RE.test(word.value),
    )
  ) {
    return true;
  }
  if (OPTION_REBINDERS.has(head) && optionFromExpansion(head, words.slice(1))) return true;
  const check = TOOL_CHECKS.get(head);
  if (check) return check(values);
  if (!DEFERRING_COMMANDS.has(head)) return false;
  if (words.slice(1).some((word) => spellingHidesName(word) || expandsMidWord(word))) return true;
  // Preserve format quoting when a wrapper carries a formatter.
  if (
    words.some(
      (word, index) =>
        index > 0 &&
        ["printf", "print"].includes(bareWord(word.value)) &&
        printfWritesName(bareWord(word.value), words, index),
    )
  )
    return true;
  return deferredIsDangerous(head, values.slice(1)) || carriedCommands(values).some(isDangerous);
}

/** Raw expression spans never receive an inert-text exemption, including expressions written inside quotes. */
function lookupExpressionSpans(text) {
  const spans = [];
  let index = 0;
  while (index < text.length) {
    const opening = text.startsWith("${", index)
      ? "{"
      : text.startsWith("((", index)
        ? "("
        : text[index] === "["
          ? "["
          : "";
    if (!opening) {
      index++;
      continue;
    }
    const begin = index;
    const stack = opening === "{" ? ["}"] : opening === "(" ? [")", ")"] : ["]"];
    index += opening === "[" ? 1 : 2;
    let quote = "";
    while (index < text.length && stack.length) {
      const char = text[index];
      if (char === "\\" && quote !== "'") {
        index += 2;
        continue;
      }
      if (quote) {
        if (char === quote) quote = "";
      } else if (char === "'" || char === '"') {
        quote = char;
      } else if ("([{".includes(char)) {
        stack.push({ "(": ")", "[": "]", "{": "}" }[char]);
      } else if (char === stack[stack.length - 1]) {
        stack.pop();
      }
      index++;
    }
    spans.push([begin, index]);
  }
  return spans;
}

/** Inert operand positions of a literal unshadowed program, echo, or printf/print format. */
function lookupTextPositions(words, start, shadowed) {
  if (start === null || start >= words.length) return new Set();
  const command = words[start];
  const head = command.value.split("/").pop();
  if (
    command.quoted ||
    command.pattern ||
    command.raw !== command.value ||
    /[$`~=\\]/.test(command.raw) ||
    shadowed.has(head)
  )
    return new Set();
  if (head === "echo" || LOOKUP_TEXT_PROGRAMS.has(head) || /^[A-Za-z0-9_./+-]+\.(?:py|js)$/.test(command.raw)) {
    return new Set(words.map((_, index) => index).filter((index) => index > start));
  }
  const operands = words.slice(start + 1);
  if (head === "read") {
    const positions = new Set();
    let index = 0;
    while (index < operands.length) {
      const value = operands[index].raw;
      if (value === "--" || !value.startsWith("-")) break;
      if (value === "-d" && index + 1 < operands.length) {
        positions.add(start + 2 + index);
        index += 2;
      } else if (value === "-t") index += 2;
      else index++;
    }
    return positions;
  }
  if (head === "printf" || head === "print") {
    const fmt = printfFormatIndex(head, operands);
    if (fmt === null || fmt < 0 || fmt >= operands.length) return new Set();
    if (!printfWritesName(head, words, start))
      return new Set(words.map((_, index) => index).filter((index) => index >= start + 1 + fmt));
    return new Set([start + 1 + fmt]);
  }
  return new Set();
}

/** Keep literal format operands and delimiters out of expression scans. */
function literalLookupData(word, head, inert) {
  return inert && (/^'[^']*'$/.test(word.raw) || (literalWord(word) && ["printf", "print", "read"].includes(head)));
}

/** Command regions with exact offsets; nested substitutions cannot borrow a parent's inert-argument exemption. */
function lookupRegions(command) {
  const regions = [];
  const queue = [[command, 0]];
  while (queue.length) {
    const [text, offset] = queue.pop();
    const bodies = [];
    const [separators] = scanTopLevel(text, bodies);
    const edges = [-1, ...separators, text.length];
    let index = 0;
    while (index < edges.length - 1) {
      const end = conditionalEnd(text, edges, index);
      const begin = edges[index] + 1;
      const finish = edges[end];
      if (text.slice(begin, finish).trim()) regions.push([text.slice(begin, finish), offset + begin]);
      index = end;
    }
    queue.push(...bodies.map(([body, begin]) => [body, offset + begin]));
  }
  return regions;
}

/** Identify controls and non-ASCII Unicode spaces or format characters. */
function unusualShellCharacter(text) {
  return /[\x00-\x08\x0b-\x1f\x7f]|\p{Cf}|\p{Zl}|\p{Zp}|(?! )\p{Zs}/u.test(text);
}

/** Refuse unusual characters in expressions and lookup-name writes, leaving assignment values as data. */
function unusualLookupWrite(value, head, inert) {
  if (!unusualShellCharacter(value)) return false;
  if (lookupExpressionSpans(value).some(([low, high]) => unusualShellCharacter(value.slice(low, high)))) return true;
  if (inert) return false;
  const positions = [];
  let offset = 0;
  for (const char of value) {
    if (!unusualShellCharacter(char)) {
      for (let index = 0; index < char.length; index++) positions.push(offset + index);
    }
    offset += char.length;
  }
  const cleaned = positions.map((index) => value[index]).join("");
  if ((NAME_ASSIGNERS.has(head) || LOOP_KEYWORDS.has(head)) && LOOKUP_VARIABLE_NAME_RE.test(cleaned)) return true;
  for (const match of cleaned.matchAll(LOOKUP_WRITE_TOKEN_RE)) {
    // Include removed characters before the name, but never the assigned value after the operator.
    const begin = match.index ? positions[match.index - 1] + 1 : 0;
    const end = positions[match.index + match[0].length - 1] + 1;
    if (unusualShellCharacter(value.slice(begin, end))) return true;
  }
  return false;
}

/** Refuse raw and decoded writes except inert operands, heredoc data, read prefixes and flag prefixes. */
function positionIndependentWrite(command) {
  const [text, live, unterminated] = splitHeredocs(command);
  if (unterminated || live.some(positionIndependentWrite)) return true;
  command = text;
  const regions = lookupRegions(command);
  const shadowed = new Set();
  for (const [segment] of regions) {
    for (const text of judgedTexts(segment)) {
      for (const name of definedFunctionNames(text) || []) shadowed.add(readWord(name, 0)[0].value);
      const words = shellWords(text);
      const start = commandWordIndex(words);
      if (start !== null && start < words.length && bareWord(words[start].value) === "alias") {
        for (const word of words.slice(start + 1))
          if (word.value.includes("=")) shadowed.add(word.value.split("=", 1)[0]);
      }
    }
  }
  const contexts = new Map();
  for (const [segment, offset] of regions) {
    const spans = shellWordSpans(segment);
    const raws = spans.map(([word]) => word.raw);
    for (const text of judgedTexts(segment)) {
      const words = shellWords(text);
      if (!words.length || !words.every((word, index) => word.raw === raws[raws.length - words.length + index]))
        continue;
      const start = commandWordIndex(words);
      const head = start !== null && start < words.length ? bareWord(words[start].value) : "";
      const positions = lookupTextPositions(words, start, shadowed);
      if (head === "printf" || head === "print") {
        if (printfWritesName(head, words, start)) return true;
      }
      for (const [index, [word, end]] of spans.slice(-words.length).entries()) {
        const begin = offset + end - word.raw.length;
        const flag = LOOKUP_WRITE_FLAG_RE.exec(word.raw);
        let prefixEnd = flag ? begin + flag[0].length : begin;
        if (head === "read" && index < start && word.raw.startsWith("IFS=")) prefixEnd = begin + 4;
        contexts.set(begin + ":" + (offset + end), [
          begin,
          offset + end,
          positions.has(index),
          prefixEnd,
          segment.length,
          head,
        ]);
      }
    }
  }
  for (const [begin, end, inert, prefixEnd, , head] of contexts.values()) {
    // Mask live expansions so nested commands keep ownership of their operands.
    const raw = command.slice(begin, end);
    const value = readWord(maskLiveExpansions(raw), 0)[0].value;
    if (literalLookupData(readWord(raw, 0)[0], head, inert)) continue;
    if (unusualLookupWrite(value, head, inert)) return true;
    const flag = LOOKUP_WRITE_FLAG_RE.exec(value);
    const prefix = flag ? flag[0].length : raw.startsWith("IFS=") && prefixEnd === begin + 4 ? 4 : 0;
    const spans = lookupExpressionSpans(value);
    for (const match of value.matchAll(LOOKUP_WRITE_TOKEN_RE)) {
      if (spans.some(([low, high]) => low < match.index + match[0].length && match.index < high)) return true;
      if (!inert && match.index + match[0].length > prefix) return true;
    }
  }
  const protectedSpans = [...contexts.values()].flatMap(([begin, end, inert, , , head]) =>
    literalLookupData(readWord(command.slice(begin, end), 0)[0], head, inert)
      ? []
      : lookupExpressionSpans(command.slice(begin, end)).map(([low, high]) => [begin + low, begin + high]),
  );
  if (protectedSpans.some(([begin, end]) => unusualShellCharacter(command.slice(begin, end)))) return true;
  for (const match of command.matchAll(LOOKUP_WRITE_TOKEN_RE)) {
    if (protectedSpans.some(([begin, end]) => begin < match.index + match[0].length && match.index < end)) return true;
    const candidates = [...contexts.values()]
      .filter(([begin, end]) => begin <= match.index && match.index < end)
      .sort((a, b) => a[4] - b[4] || a[1] - a[0] - (b[1] - b[0]));
    if (!candidates.length) return true;
    const [, , inert, prefixEnd] = candidates[0];
    if (!inert && match.index + match[0].length > prefixEnd) return true;
  }
  return false;
}

/**
 * True when any segment of `command` is destructive. Text ending inside an unclosed quote is refused: the reader and
 * the shell disagree about where its commands start.
 */
function isDangerous(command) {
  if (positionIndependentWrite(command)) return true;
  if (writesCommandTable(command) || quotingUnsettled(command) || rebindsThroughWords(command)) return true;
  // a heredoc body is data: only the text around it and the substitutions of an unquoted body are segments
  const [text, live] = splitHeredocs(command);
  return [text, ...live].some((chunk) => splitSegments(chunk).some(segmentIsDangerous));
}

/** Index past a backslash escape, `'…'` or `$'…'` opening at `index`, or null; inside `"…"` only an escape counts. */
function inertSpanEnd(raw, index, double) {
  if (raw[index] === "\\") return index + 2;
  if (double) return null;
  const body = ansiCBody(raw, index);
  if (body !== -1) return readAnsiC(raw, body)[1];
  if (raw[index] === "'") {
    const close = raw.indexOf("'", index + 1);
    return close < 0 ? raw.length : close + 1;
  }
  return null;
}

/** A word with each expansion the shell runs (`$(…)`, backticks, `${…}`) replaced by `_`; quoted text kept. */
function maskLiveExpansions(raw) {
  let out = "";
  let index = 0;
  let double = false;
  while (index < raw.length) {
    let end = inertSpanEnd(raw, index, double);
    if (end !== null) {
      out += raw.slice(index, end);
      index = end;
      continue;
    }
    if (raw[index] === '"') double = !double;
    end = readExpansion(raw, index);
    if (end !== null && raw.startsWith("${", index)) {
      // a `${x:-'…'}` operand is stored text too, so only the expansions nested in it are masked
      const closed = raw[end - 1] === "}";
      out += "${" + maskLiveExpansions(raw.slice(index + 2, closed ? end - 1 : end)) + (closed ? "}" : "");
    } else {
      out += end !== null ? "_" : raw[index];
    }
    index = end !== null ? end : index + 1;
  }
  return out;
}

/**
 * True when an assignment word stores command-substitution text its own expansion leaves inert (`x='a[$(cmd)0]'`):
 * a later `$((x))`, `${(e)x}` or prompt expansion runs it. Live expansions are masked before decoding.
 */
function storesCode(word) {
  if (!ASSIGNMENT_WORD_RE.test(word.value)) return false;
  return CODE_OPENER_RE.test(readWord(maskLiveExpansions(word.raw), 0)[0].value);
}

/**
 * Every name a segment defines as a function, or null. Read from shell words, so a quoted `()` is no definition;
 * zsh defines one function per word before an unquoted `()` and per word after `function`.
 */
function definedFunctionNames(segment) {
  const raws = commandStartWords(shellWords(segment)).map((word) => word.raw);
  if (raws[0] === "function") {
    const names = [];
    for (const raw of raws.slice(1)) {
      if (raw === "(" || raw === "{") break;
      names.push(raw);
    }
    return names;
  }
  for (let index = 0; index < raws.length - 1; index++) {
    if (raws[index] === "(" && raws[index + 1] === ")") return raws.slice(0, index);
  }
  return null;
}

/**
 * True when a segment defines a function that may shadow a command. Helper names carry an underscore (`_q`,
 * `preflight_ok`); any other name, more than one name, or an anonymous function is refused.
 */
function definesShadowingFunction(segment) {
  const names = definedFunctionNames(segment);
  if (names === null) return false;
  return names.length !== 1 || !names[0].includes("_") || !FUNCTION_NAME_RE.test(names[0]);
}

/**
 * True when one segment rebinds a name through its words or stores code: a shadowing function, a lookup-variable
 * assignment wherever it stands (except an `IFS=…` prefix of a `read`), an assignment storing code or assigning
 * through an arithmetic subscript, or a name-writing command (`commandWritesName`).
 */
function segmentRebinds(segment) {
  // a `case` arm's pattern is no word of the command it runs (`"uv run") R=(uv run)` assigns before `R`'s command)
  return judgedTexts(segment).some(textRebinds);
}

/** True when one command text, `case` syntax already read off (judgedTexts), rebinds a name or stores code. */
function textRebinds(text) {
  if (definesShadowingFunction(text)) return true;
  const words = shellWords(text);
  const values = words.map((word) => word.value);
  const start = commandWordIndex(words);
  if (start === null) return true;
  const head = start < values.length ? bareWord(values[start]) : "";
  const textPositions = lookupTextPositions(words, start, new Set());
  for (const [index, word] of words.entries()) {
    const prefixIfs = word.value.startsWith("IFS=") && index < start && head === "read";
    const assigning = index < start || !TEXT_COMMANDS.has(head);
    if (assigning && LOOKUP_VARIABLE_ASSIGNMENT_RE.test(word.value) && !prefixIfs && !textPositions.has(index))
      return true;
    if (assignmentArithmeticUnmodeled(word, assigning)) return true;
    // `NAME=(…)` is an array only before the command word or after a declaration builtin; anywhere else zsh reads
    // its `(…)` as glob qualifiers, which can run code
    if (index >= start && !ATTRIBUTE_DECLARERS.has(head) && ARRAY_ASSIGNMENT_WORD_RE.test(word.raw)) return true;
    if (!textPositions.has(index) && (storesCode(word) || assignsThroughSubscript(word))) return true;
  }
  return commandWritesName(words, start);
}

/**
 * Index of the command word, past reserved words, grouping, leading assignments, and runners with their options;
 * null when a runner carries an option outside RUNNER_OPTION_LETTERS, which may take the next word as its operand.
 */
function commandWordIndex(words) {
  const values = words.map((word) => word.value);
  let index = 0;
  while (index < values.length) {
    // reserved words, grouping and assignments count only as written: a quoted `'X=1'` is the command word
    const value = values[index];
    const raw = words[index].raw;
    if (SHELL_RESERVED_WORDS.has(raw) || !stripChars(raw, "(){}") || ASSIGNMENT_WORD_RE.test(raw)) {
      index += 1;
    } else if (value === "env" && raw === "env") {
      index++;
      while (index < values.length && values[index].startsWith("-")) {
        const option = words[index];
        if (!["-i", "--ignore-environment", "--"].includes(option.raw)) return null;
        index++;
        if (option.raw === "--") break;
      }
      while (index < words.length && /^[A-Za-z_]\w*=/.test(words[index].raw)) {
        if (!literalWord(words[index])) return null;
        index++;
      }
    } else if (COMMAND_RUNNERS.has(value)) {
      index += 1;
      const letters = RUNNER_OPTION_LETTERS[value] || "";
      while (index < values.length && values[index].startsWith("-") && values[index] !== "-") {
        const option = values[index];
        index += 1;
        if (option === "--") break;
        if (![...option.slice(1)].every((letter) => letters.includes(letter))) return null;
      }
    } else {
      return index;
    }
  }
  return index;
}

/** Require a literal identifier, optionally followed by a declaration's assignment value. */
function nameOperandUnmodeled(word, assignment = false) {
  let raw = word.raw;
  if (raw.length >= 2 && "'\"".includes(raw[0]) && raw.at(-1) === raw[0]) raw = raw.slice(1, -1);
  const name = assignment && raw.includes("=") ? raw.slice(0, raw.indexOf("=")).replace(/\+$/, "") : raw;
  return (
    !/^[A-Za-z_][A-Za-z0-9_]*$/.test(name) || LOOKUP_VARIABLE_NAME_RE.test(name) || COMMAND_TABLE_USE_RE.test(name)
  );
}

/** Close name-writing operands independently of option expansion and option terminators. */
function builtinNamesUnmodeled(head, operands) {
  if (head === "shift")
    return operands.length > 1 || operands.some((word) => !literalWord(word) || !/^[0-9]+$/.test(word.value));
  if (head === "getopts")
    return (
      operands.length < 2 ||
      !literalWord(operands[0]) ||
      operands[0].value.startsWith("-") ||
      nameOperandUnmodeled(operands[1])
    );
  if (!NAME_ASSIGNERS.has(head)) return false;
  const assignment = ATTRIBUTE_DECLARERS.has(head) || ["integer", "float"].includes(head);
  let options = true;
  let index = 0;
  while (index < operands.length) {
    const word = operands[index++];
    const value = word.value;
    if (options && value === "--" && word.raw === "--") {
      options = false;
      continue;
    }
    if (options && /^[+-]/.test(value)) {
      if (word.raw !== value || !/^[-+][A-Za-z]+$/.test(value)) return true;
      if (head === "read") {
        if (value === "-t") {
          if (index >= operands.length || !/^[0-9]+$/.test(operands[index].raw)) return true;
          index++;
        } else if (value === "-d") {
          if (index >= operands.length || !literalWord(operands[index])) return true;
          index++;
        } else if (!["-a", "-A"].includes(value) && !/^-[ers]+$/.test(value)) return true;
      }
      continue;
    }
    if (nameOperandUnmodeled(word, assignment)) return true;
  }
  return false;
}

/** Judge name-writing commands, loop targets, arithmetic and shell-option changes. */
function commandWritesName(words, start) {
  const values = words.map((word) => word.value);
  const head = start < values.length ? bareWord(values[start]) : "";
  const operands = values.slice(start + 1);
  if (LOOP_KEYWORDS.has(head)) {
    // zsh loops over several names (`for a b in …`) and takes a `(…)` word list (`for x (a b)`)
    for (const word of words.slice(start + 1)) {
      if (word.raw === "in" || word.raw === "do" || word.raw.startsWith("(")) break;
      if (nameOperandUnmodeled(word)) return true;
    }
  }
  if (builtinNamesUnmodeled(head, words.slice(start + 1))) return true;
  if (
    NAME_ASSIGNERS.has(head) &&
    operands.some((value) => LOOKUP_VARIABLE_NAME_RE.test(value) || unplainSubscript(value))
  ) {
    return true;
  }
  if (
    ARITHMETIC_COMMANDS.has(head) ||
    (ATTRIBUTE_DECLARERS.has(head) && operands.some((value) => /^[-+][A-Za-z]*i[A-Za-z]*$/.test(value)))
  ) {
    return true;
  }
  if (evaluatesArithmetic(head, words, start)) return true;
  const raws = words.map((word) => word.raw);
  // a shell a wrapper runs (`env zsh -o …`, `sudo bash -O …`) takes its options from the words after it
  return (
    optionUnmodeled(head, raws.slice(start + 1)) ||
    values.some(
      (value, index) =>
        SHELL_INTERPRETERS.has(bareWord(value)) && optionUnmodeled(bareWord(value), raws.slice(index + 1)),
    )
  );
}

/** True when a word holds a subscript other than a number, `@`/`*` or one name: that one is arithmetic. */
function unplainSubscript(value) {
  return [...value.matchAll(SUBSCRIPT_RE)].some((match) => !PLAIN_SUBSCRIPT_RE.test(match[1]));
}

/** True when an unquoted assignment word assigns through an arithmetic subscript (`a[PATH=1]=0`). */
function assignsThroughSubscript(word) {
  const target = /^[A-Za-z_][A-Za-z0-9_]*\[[^\]]*\]\+?=/.exec(word.raw);
  return target !== null && unplainSubscript(target[0]);
}

/**
 * True when a word used as an arithmetic operand assigns a name or runs code when it is evaluated. Arithmetic
 * evaluation re-expands the operand's text, so a `$(…)`/backtick/`$[…]` stored in it by quoting runs; the live
 * expansions are masked first, leaving a code opener only where quoting stored one. The rest must pass the one
 * arithmetic admit set (arithmeticUnmodeled). Mirrors `_arithmetic_word_unmodeled`.
 */
function arithmeticWordUnmodeled(word) {
  const stored = decodeOperand(maskLiveExpansions(word.raw));
  return CODE_OPENER_RE.test(stored) || arithmeticUnmodeled(word.value);
}

/**
 * True when a `[[ … ]]` test evaluates an arithmetic operand that assigns a name: either operand of a numeric
 * comparison (CONDITIONAL_ARITHMETIC_OPS), or the subscript of a `-v name[idx]`. Mirrors `_conditional_writes_name`.
 */
function conditionalWritesName(words, start) {
  const tokens = words.slice(start);
  for (let index = 0; index < tokens.length; index += 1) {
    const value = tokens[index].value;
    if (CONDITIONAL_ARITHMETIC_OPS.has(value)) {
      if (index > 0 && arithmeticWordUnmodeled(tokens[index - 1])) return true;
      if (index + 1 < tokens.length && arithmeticWordUnmodeled(tokens[index + 1])) return true;
    } else if (value === "-v" && index + 1 < tokens.length && unplainSubscript(tokens[index + 1].value)) {
      return true;
    }
  }
  return false;
}

/**
 * True when a count command's first non-option operand (the count: `shift -p n`, `repeat n body`) assigns a name
 * when evaluated as arithmetic. Mirrors `_count_writes_name`.
 */
function countWritesName(words, start) {
  for (const word of words.slice(start + 1)) {
    if (word.value.startsWith("-") && word.value.length > 1) continue;
    return arithmeticWordUnmodeled(word);
  }
  return false;
}

/**
 * Locate the sole format, returning null without one and -1 for unsupported options. Mirrors `_printf_format_index`.
 */
function printfFormatIndex(head, operands) {
  if (head === "printf") {
    const index = operands.length && operands[0].raw === "--" ? 1 : 0;
    if (index >= operands.length) return null;
    const value = operands[index].value;
    return index === 0 && value.startsWith("-") && !value.startsWith("-%") ? -1 : index;
  }
  let fmt = null;
  let index = 0;
  while (index < operands.length) {
    const value = operands[index].value;
    if (value === "--") {
      if (
        operands[index].raw !== "--" ||
        (fmt === null && index + 1 < operands.length && operands[index + 1].value.startsWith("-"))
      )
        return -1;
      return fmt;
    }
    if (!value.startsWith("-") || value.length < 2) return fmt;
    if (/^-[nr]+$/.test(value) && literalWord(operands[index])) {
      index++;
    } else if (/^-[nr]*f/.test(value) && fmt === null && literalWord(operands[index])) {
      const optionEnd = value.indexOf("f") + 1;
      fmt = value.length > optionEnd ? index : index + 1;
      index += value.length > optionEnd ? 1 : 2;
    } else return -1;
  }
  return fmt;
}

/** True when quote removal yields one fixed word, without live expansion or unquoted word generation. */
function literalWord(word) {
  if (word.pattern || word.value.includes("\u0000")) return false;
  let double = false;
  for (let index = 0; index < word.raw.length;) {
    const end = inertSpanEnd(word.raw, index, double);
    if (end !== null) {
      index = end;
      continue;
    }
    const char = word.raw[index];
    if ("$`".includes(char) || (!double && char === "~")) return false;
    if (char === '"') double = !double;
    index++;
  }
  return true;
}

/**
 * Refuse formats outside the literal text-only language, including all numeric conversions. String operands are
 * data; unknown syntax never gets a partial conversion match. Mirrors `_printf_writes_name`.
 */
function printfWritesName(head, words, start) {
  const operands = words.slice(start + 1);
  const fmtIndex = printfFormatIndex(head, operands);
  if (fmtIndex === null) return head === "printf";
  if (fmtIndex < 0 || fmtIndex >= operands.length) return true;
  const word = operands[fmtIndex];
  let fmt = word.value;
  if (head === "print" && /^-[nr]*f/.test(word.value)) {
    // Strip an option prefix only from an attached format, never from the following format word.
    const separate = fmtIndex > 0 && /^-[nr]*f$/.test(operands[fmtIndex - 1].value);
    if (!separate) fmt = fmt.slice(fmt.indexOf("f") + 1);
  }
  return !literalWord(word) || !PRINTF_TEXT_FORMAT_RE.test(fmt);
}

/**
 * True when a `read -t` timeout operand assigns a name when evaluated. The timeout follows a `t` cluster, attached
 * (`-t1`) or as the next word (`-t 1`). Mirrors `_read_timeout_unmodeled`.
 */
function readTimeoutUnmodeled(words, start) {
  const operands = words.slice(start + 1);
  for (let index = 0; index < operands.length; index += 1) {
    const value = operands[index].value;
    if (value === "--" || !value.startsWith("-")) return false;
    const match = /^-[A-Za-z]*t([A-Za-z0-9_,.$+*/%<>?:()!=-]*)$/.exec(value);
    if (match === null) continue;
    if (match[1]) return arithmeticUnmodeled(match[1]);
    return index + 1 < operands.length && arithmeticWordUnmodeled(operands[index + 1]);
  }
  return false;
}

/**
 * True when a command evaluates one of its operands as arithmetic that assigns a name or runs code: `[[ … ]]`
 * comparison operands, a count command's count, a `printf`/`print -f` numeric conversion or `%n`, or a `read -t`
 * timeout. Mirrors `_evaluates_arithmetic`.
 */
function evaluatesArithmetic(head, words, start) {
  if (head === "[[") return conditionalWritesName(words, start);
  if (ARITHMETIC_COUNT_COMMANDS.has(head)) return countWritesName(words, start);
  if (head === "printf" || head === "print") return printfWritesName(head, words, start);
  if (head === "read") return readTimeoutUnmodeled(words, start);
  return false;
}

/**
 * True when an assignment word evaluates arithmetic that assigns a name: an integer parameter's value or an
 * array-literal element key. Mirrors `_assignment_arithmetic_unmodeled`.
 */
function assignmentArithmeticUnmodeled(word, assigning) {
  if (assigning) {
    // the name is matched on `word.value` (quotes resolved), so `export "SECONDS"=…` assigns it as the shell does
    const integer = INTEGER_ASSIGNMENT_RE.exec(word.value);
    if (integer !== null && arithmeticUnmodeled(word.value.slice(integer[0].length))) return true;
  }
  return (
    ARRAY_ASSIGNMENT_WORD_RE.test(word.raw) &&
    [...word.raw.matchAll(ARRAY_ELEMENT_KEY_RE)].some((key) => arithmeticUnmodeled(key[1]))
  );
}

/**
 * True when a command switches a shell option outside SAFE_SHELL_OPTIONS: an operand of `setopt`/`unsetopt`/`shopt`,
 * or the name after `-o`/`+o` (bash also `-O`) for `set` or a shell. Operands are the words as written and names
 * compare exactly, so no spelling of `GLOB_SUBST` passes and a quoted name is refused; `shopt` flags are limited to
 * `-s`/`-u`/`-p`/`-q`/`-o`, `set` flags to SET_FLAG_CLUSTER_RE and a shell's long options to SHELL_LONG_OPTIONS.
 */
function optionUnmodeled(head, operands) {
  if (OPTION_SETTERS.has(head)) {
    return operands.some((value) => !SAFE_SHELL_OPTIONS.has(value) && !(head === "shopt" && /^-[supqo]+$/.test(value)));
  }
  if (head !== "set" && !SHELL_INTERPRETERS.has(head)) return false;
  let index = 0;
  while (index < operands.length) {
    const value = operands[index];
    if (value === "--" || value === "-") return false;
    // the first operand ends the options only when the lead of its value is literal text that keeps every word built
    // from it from starting `-` or `+`; an expansion, tilde, glob or brace lead stays as written (`'-o'`, `"$F"`, `~`)
    if (!(value.startsWith("-") || value.startsWith("+")))
      return !OPTION_OPERAND_LEAD_RE.test(readWord(value, 0)[0].value);
    // a shell reads options up to its first operand, so a long option it may carry and a `-c` cluster are passed over
    if (value.startsWith("--") || !/^[-+][A-Za-z]+$/.test(value)) {
      if (head === "set" || !SHELL_LONG_OPTIONS.has(value)) return true;
      index += 1;
    } else if (head === "set" && !SET_FLAG_CLUSTER_RE.test(value)) {
      // `set -A`/`+A` assigns an array, judged by its own check; any other flag is refused
      return value[1] !== "A";
    } else if (SHELL_OPTION_CLUSTER_RE.test(value)) {
      // the letters after the first `o`/`O` are the option name (`-oglobsubst`); with none, the next word is
      const attached = value.slice(value.slice(1).search(/[oO]/) + 2);
      const name = attached || (index + 1 < operands.length ? operands[index + 1] : null);
      if (name === null || !SAFE_SHELL_OPTIONS.has(name)) return true;
      index += attached ? 1 : 2;
    } else {
      index += 1;
    }
  }
  return false;
}

/** True when shell code holds any construct outside the grammar the reader models (see LiveScan). */
function unmodeledConstruct(text) {
  return new LiveScan(text, "code", false).refused();
}

/**
 * True when the body of a `$(…)`, backtick or `<(…)` substitution leaves the modeled grammar, or a comment runs to
 * its very end: the shell would have read the `)` the reader took as the close as comment text.
 */
function unmodeledSubstitution(body) {
  return new LiveScan(body, "code", true).refused();
}

/**
 * Walks shell text as the shell expands it, refusing every construct outside the modeled grammar. Mirrors the
 * Python generator's `_LiveScan`: only live text is judged (top level, `"…"`, substitution bodies, unquoted heredoc
 * bodies). `mode` is "code", "operand" (a `${…}` operand: quotes apply, no heredoc, comment or paren rules) or
 * "body" (an unquoted heredoc body: only `$`, backticks and backslashes are special).
 */
class LiveScan {
  constructor(text, mode, substitution = false) {
    this.text = text;
    this.mode = mode;
    // a substitution body, where a comment must end before the body does
    this.substitutionBody = substitution;
    this.index = 0;
    this.wordStart = 0;
    this.double = false;
    this.pending = [];
    // the simple command read so far, for telling a grouping `(` from one that opens a glob word: its words as
    // written, the grouping parentheses still open, whether a `)` just closed one, and whether the next word is a
    // redirection target
    this.words = [];
    this.groups = 0;
    this.closed = false;
    this.redirect = false;
  }

  refused() {
    while (this.index < this.text.length) {
      let verdict = this.quoting();
      if (verdict === null) verdict = this.expansion();
      if (verdict === null && this.mode === "code" && !this.double) verdict = this.syntax();
      if (verdict === null) this.index += 1;
      else if (verdict) return true;
    }
    return this.pending.length > 0;
  }

  quoting() {
    const { text, index } = this;
    if (text[index] === "\\") {
      this.index += 2;
      return false;
    }
    if (this.mode === "body") return null;
    if (text[index] === '"') {
      this.double = !this.double;
      this.index += 1;
      return false;
    }
    if (this.double) return null;
    if (text[index] === "'") {
      const close = text.indexOf("'", index + 1);
      this.index = close + 1;
      return close < 0;
    }
    const body = ansiCBody(text, index);
    if (body === -1) return null;
    this.index = readAnsiC(text, body)[1];
    return false;
  }

  expansion() {
    const { text, index } = this;
    if (text.startsWith("$((", index)) {
      const read = readClosed(text, index + 3, "(", ")");
      if (read === null || text[read[1]] !== ")") return true;
      this.index = read[1] + 1;
      return arithmeticUnmodeled(read[0]);
    }
    const readers = [
      ["$(", "(", ")", unmodeledSubstitution],
      ["${", "{", "}", parameterUnmodeled],
      ["`", null, "`", unmodeledSubstitution],
    ];
    for (const [opener, openChar, closeChar, judge] of readers) {
      if (text.startsWith(opener, index)) {
        const read = readClosed(text, index + opener.length, openChar, closeChar);
        if (read === null) return true;
        this.index = read[1];
        return judge(read[0]);
      }
    }
    if (text[index] === "$") {
      DOLLAR_FOLLOW_RE.lastIndex = index + 1;
      const quote = (this.double || this.mode === "body") && (text[index + 1] === "'" || text[index + 1] === '"');
      // admit set: zsh `$~x` `$=x` `$^x`, `$[…]` and any other form opened by `$` are refused
      if (!quote && !DOLLAR_FOLLOW_RE.test(text)) return true;
    }
    ZSH_SUBSCRIPTED_RE.lastIndex = index;
    const subscript = ZSH_SUBSCRIPTED_RE.exec(text);
    if (subscript === null) return null;
    return !SUBSCRIPT_TEXT_RE.test(subscript[1]) || SUBSCRIPT_REFUSED_RE.test(subscript[1]) ? true : null;
  }

  syntax() {
    const { text, index } = this;
    const char = text[index];
    if (char === "#" && index === this.wordStart) {
      // right after a grouping `(` or a `|` bash reads a comment, but zsh may read `#` inside a pattern
      // (`[[ x == (#i)y ]]`, `(a|#b)`) as a glob flag, so where the comment ends is unsettled
      if (index > 0 && (text[index - 1] === "(" || text[index - 1] === "|")) return true;
      let end = text.indexOf("\n", index);
      if (end < 0 && this.substitutionBody) return true;
      if (end < 0) end = text.length;
      this.index = this.wordStart = end;
      return text.slice(index, end).includes("<<");
    }
    if (char === "\n" && this.pending.length) return this.heredocBodies();
    if (
      (char === "<" || char === ">" || text.startsWith("&>", index)) &&
      FD_VARIABLE_PREFIX_RE.test(text.slice(this.wordStart, index))
    ) {
      // `{name}>` allocates a descriptor and assigns its number to `name`; only a digit descriptor is modeled
      return true;
    }
    if (text.startsWith("<<<", index)) {
      this.endWord(index);
      this.redirect = true;
      this.index = this.wordStart = index + 3;
      return false;
    }
    if (text.startsWith("<<", index)) return this.heredocOpening();
    if ((char === "<" || char === ">") && text[index + 1] === "(") return this.substitution(index + 2);
    if (char === "(") return this.parenthesis();
    // only blanks and newlines separate words: `\r`, `\v` and `\f` are word characters to the shell
    if (" \t\n;&|<>)".includes(char)) {
      this.endWord(index);
      this.separator(index);
      this.wordStart = index + 1;
    }
    return null;
  }

  /** Record the word that ends at `index`, if one does; a redirection's target is then consumed. */
  endWord(index) {
    if (index > this.wordStart) {
      this.words.push(this.text.slice(this.wordStart, index));
      this.redirect = false;
    }
  }

  /** Update the simple-command state at a word-ending character other than a blank. */
  separator(index) {
    const { text } = this;
    const char = text[index];
    if (
      char === "<" ||
      char === ">" ||
      (char === "&" && isRedirectionAmpersand(text, index)) ||
      (char === "|" && index > 0 && text[index - 1] === ">")
    ) {
      this.redirect = true;
    } else if (";&|\n".includes(char)) {
      this.words = [];
      this.closed = false;
      this.redirect = false;
    } else if (char === ")") {
      // a `)` closing a grouping ends a command; one with none open closes a `case` pattern, after which the arm's
      // command starts
      this.words = [];
      this.redirect = false;
      this.closed = this.groups > 0;
      this.groups = Math.max(this.groups - 1, 0);
    }
  }

  heredocOpening() {
    HEREDOC_OPENING_RE.lastIndex = this.index;
    const match = HEREDOC_OPENING_RE.exec(this.text);
    if (match === null) return true;
    this.endWord(this.index);
    const delimiter = match[2] || match[3] || match[4];
    this.pending.push([delimiter, match[2] === undefined, match[1] === "-"]);
    this.index = this.wordStart = HEREDOC_OPENING_RE.lastIndex;
    return false;
  }

  heredocBodies() {
    this.index += 1;
    for (const [delimiter, quoted, stripsTabs] of this.pending) {
      const body = this.heredocBody(delimiter, stripsTabs);
      if (body === null || (!quoted && new LiveScan(body, "body").refused())) return true;
    }
    this.pending = [];
    this.wordStart = this.index;
    this.words = [];
    this.closed = false;
    this.redirect = false;
    return false;
  }

  heredocBody(delimiter, stripsTabs) {
    const lines = [];
    while (this.index < this.text.length) {
      let end = this.text.indexOf("\n", this.index);
      if (end < 0) end = this.text.length;
      const line = this.text.slice(this.index, end);
      this.index = end + 1;
      if ((stripsTabs ? line.replace(/^\t+/, "") : line) === delimiter) return lines.join("\n");
      lines.push(line);
    }
    return null;
  }

  parenthesis() {
    const word = this.text.slice(this.wordStart, this.index);
    if (!word) {
      if (this.text.startsWith("((", this.index) || !this.opensGroup()) return true;
      this.groups += 1;
      this.words = [];
      this.closed = false;
      this.index = this.wordStart = this.index + 1;
      return false;
    }
    if (word === "=" || ARRAY_ASSIGNMENT_OPEN_RE.test(word)) return this.substitution(this.index + 1);
    if (this.text[this.index + 1] === ")" && FUNCTION_WORD_RE.test(word)) {
      this.index = this.wordStart = this.index + 2;
      return false;
    }
    return true;
  }

  /**
   * True when a `(` opening a word here groups: at command position (after a separator, another grouping `(`, a
   * `case` pattern's `)` or a reserved word such as `if` or `!`), inside `[[ … ]]`, as a `case` pattern right after
   * `in`, or as the `()` of a function definition. Anywhere else zsh reads `(` as the start of a glob word, whose
   * glued second group (`(*)(e:…:)`) is a qualifier that runs code.
   */
  opensGroup() {
    if (this.text[this.index + 1] === ")") return true;
    if (this.redirect || this.closed) return false;
    const { words } = this;
    return (
      words.every((word) => GROUP_OPENERS.has(word)) ||
      (words[0] === "[[" && !words.includes("]]")) ||
      (words.length === 3 && words[0] === "case" && words[2] === "in")
    );
  }

  substitution(start) {
    const read = readClosed(this.text, start, "(", ")");
    if (read === null) return true;
    this.index = read[1];
    return unmodeledSubstitution(read[0]);
  }
}

/** A delimited run like readDelimited as [inner, end], or null when the text ends before it closes. */
function readClosed(text, start, openChar, closeChar) {
  const [inner, end] = readDelimited(text, start, openChar, closeChar);
  return inner.length === end - start - 1 ? [inner, end] : null;
}

/**
 * True when the body of a `${…}` expansion leaves the modeled forms: a parameter, its length, a digit offset, or one
 * reading operator whose operand passes the grammar scan and, decoded, stores no code opener.
 */
function parameterUnmodeled(inner) {
  if (inner === "#" || PARAMETER_LENGTH_RE.test(inner)) return false;
  PARAMETER_NAME_RE.lastIndex = 0;
  const name = PARAMETER_NAME_RE.exec(inner);
  if (name === null) return true;
  const rest = inner.slice(PARAMETER_NAME_RE.lastIndex);
  if (!rest || PARAMETER_OFFSET_RE.test(rest)) return false;
  const operator = PARAMETER_OPERATOR_RE.exec(rest);
  if (operator === null) return true;
  const operand = rest.slice(operator[0].length);
  const stored = decodeOperand(maskLiveExpansions(operand));
  return new LiveScan(operand, "operand").refused() || CODE_OPENER_RE.test(stored);
}

/** Text with backslash escapes and `'…'`/`"…"`/`$'…'` quotes resolved, as the shell stores it. */
function decodeOperand(text) {
  let out = "";
  let index = 0;
  while (index < text.length) {
    const char = text[index];
    const body = ansiCBody(text, index);
    if (body !== -1) {
      const [decoded, end] = readAnsiC(text, body);
      out += decoded;
      index = end;
    } else if (char === "\\") {
      out += text.slice(index + 1, index + 2);
      index += 2;
    } else if (char === "'" || char === '"') {
      let close = text.indexOf(char, index + 1);
      if (close < 0) close = text.length;
      out += text.slice(index + 1, close);
      index = close + 1;
    } else {
      out += char;
      index += 1;
    }
  }
  return out;
}

/**
 * True when the text of `$((…))` does more than read values and compare them: substitutions inside are judged as
 * code and stand for a number; the rest must alternate operands (not lookup variables) and binary operators, with
 * unary `!`/`-`/`+` and balanced parentheses.
 */
function arithmeticUnmodeled(expr) {
  const masked = maskArithmeticOperands(expr);
  if (masked === null || ARITHMETIC_REFUSED_RE.test(masked)) return true;
  let expectOperand = true;
  let depth = 0;
  let index = 0;
  while (index < masked.length) {
    const char = masked[index];
    let operand = null;
    let binary = null;
    if (expectOperand) {
      ARITHMETIC_OPERAND_RE.lastIndex = index;
      operand = ARITHMETIC_OPERAND_RE.exec(masked);
    } else {
      ARITHMETIC_BINARY_RE.lastIndex = index;
      binary = ARITHMETIC_BINARY_RE.exec(masked);
    }
    if (char === " " || char === "\t" || (expectOperand && "!-+".includes(char))) {
      index += 1;
    } else if (char === "(" && expectOperand) {
      depth += 1;
      index += 1;
    } else if (char === ")" && !expectOperand && depth) {
      depth -= 1;
      index += 1;
    } else if (operand !== null && !LOOKUP_ARITHMETIC_NAME_RE.test(operand[0])) {
      expectOperand = false;
      index += operand[0].length;
    } else if (binary !== null) {
      expectOperand = true;
      index += binary[0].length;
    } else {
      return true;
    }
  }
  return expectOperand || depth !== 0;
}

/** Arithmetic text with each substitution and `${…}` replaced by `0`, or null when one is refused. */
function maskArithmeticOperands(expr) {
  const readers = { "$(": ["(", ")"], "${": ["{", "}"], "`": [null, "`"] };
  let out = "";
  let index = 0;
  while (index < expr.length) {
    const opener = ["$(", "${", "`"].find((candidate) => expr.startsWith(candidate, index));
    if (opener === undefined) {
      out += expr[index];
      index += 1;
      continue;
    }
    const read = readClosed(expr, index + opener.length, ...readers[opener]);
    const judge = opener === "${" ? arithmeticParameterUnmodeled : unmodeledSubstitution;
    if (read === null || judge(read[0])) return null;
    out += "0";
    index = read[1];
  }
  return out;
}

/**
 * True when a `${…}` inside `$((…))` may expand to arithmetic that does more than read a value: arithmetic
 * evaluates the text an expansion yields, so an operand it may expand to (`${x:-PATH=1}`) must itself be a number or
 * a name other than a lookup variable once its quotes and escapes are resolved.
 */
function arithmeticParameterUnmodeled(inner) {
  if (parameterUnmodeled(inner)) return true;
  const operand = parameterOperand(inner);
  if (operand === null) return false;
  const value = decodeOperand(operand);
  ARITHMETIC_OPERAND_RE.lastIndex = 0;
  const number = ARITHMETIC_OPERAND_RE.exec(value);
  return number === null || number[0] !== value || LOOKUP_ARITHMETIC_NAME_RE.test(value);
}

/** The operand after the reading operator of a `${…}` body, or null when it has no operator. */
function parameterOperand(inner) {
  PARAMETER_NAME_RE.lastIndex = 0;
  if (PARAMETER_NAME_RE.exec(inner) === null) return null;
  const rest = inner.slice(PARAMETER_NAME_RE.lastIndex);
  const operator = PARAMETER_OPERATOR_RE.exec(rest);
  return operator === null ? null : rest.slice(operator[0].length);
}

// A heredoc operator `<<WORD`, `<<-WORD` or `<<'WORD'`; a `<<<` here-string opens none.
const HEREDOC_OPERATOR_RE = /(?<!<)<<(?!<)(-?)[ \t]*(['"]?)([A-Za-z_][A-Za-z0-9_]*)\2/y;

/** The heredocs a line opens outside quotes, in order, as [delimiter, quoted, stripsTabs]. */
function heredocOperators(line) {
  const found = [];
  let quote = null;
  let index = 0;
  while (index < line.length) {
    const char = line[index];
    if (char === "\\" && quote !== "'") {
      index += 2;
      continue;
    }
    if (quote !== null) {
      if (char === quote) quote = null;
    } else if (char === "'" || char === '"') {
      quote = char;
    } else {
      HEREDOC_OPERATOR_RE.lastIndex = index;
      const match = HEREDOC_OPERATOR_RE.exec(line);
      if (match) {
        found.push([match[3], match[2] !== "", match[1] !== ""]);
        index = HEREDOC_OPERATOR_RE.lastIndex;
        continue;
      }
    }
    index += 1;
  }
  return found;
}

/** Bodies of `$(...)` and backtick substitutions in a heredoc line, where quotes are literal. */
function heredocSubstitutions(line) {
  const bodies = [];
  let index = 0;
  while (index < line.length) {
    const read = readSubstitution(line, index);
    if (read === null) {
      index += 1;
      continue;
    }
    bodies.push(read[0]);
    index = read[1];
  }
  return bodies;
}

/**
 * [text without heredoc bodies and delimiter lines, live substitution bodies of unquoted heredocs, true when a heredoc
 * never reaches its delimiter].
 */
function splitHeredocs(command) {
  const kept = [];
  const live = [];
  const pending = [];
  let body = [];
  for (const line of command.split("\n")) {
    if (pending.length) {
      const [delimiter, quoted, stripsTabs] = pending[0];
      if ((stripsTabs ? line.replace(/^\t+/, "") : line) !== delimiter) {
        body.push(line);
        continue;
      }
      pending.shift();
      // a substitution may span body lines, so the body is read whole once its delimiter is reached
      if (!quoted) live.push(...heredocSubstitutions(body.join("\n")));
      body = [];
      continue;
    }
    kept.push(line);
    pending.push(...heredocOperators(line));
  }
  return [kept.join("\n"), live, pending.length > 0];
}

/**
 * True when `command` leaves the modeled grammar or any of its words rebinds a name or stores code. Heredoc bodies
 * are data and their words left out: the shells that would run one read stdin (`bash` with no script, `source`,
 * `eval`) and are refused by name; the expansions of an unquoted body are judged by the grammar scan.
 */
function rebindsThroughWords(command) {
  if (unmodeledConstruct(command)) return true;
  const [text, live, unterminated] = splitHeredocs(command);
  // an unterminated body means the reader and the shell disagree about where commands resume
  if (unterminated) return true;
  return [text, ...live, ...scanTopLevel(text)[1]].some((chunk) => splitSegments(chunk).some(segmentRebinds));
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
  INTEGER_ASSIGNMENT_RE,
  COMMAND_TABLE_USE_RE,
  commandWordIndex,
  lookupTextPositions,
  literalLookupData,
  commandWritesName,
  shellWordSpans,
  lookupRegions,
  readWord,
  maskLiveExpansions,
  literalWord,
  printfFormatIndex,
  printfWritesName,
  unusualLookupWrite,
  splitHeredocs,
  heredocOperators,
  printAssignsVariable,
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
