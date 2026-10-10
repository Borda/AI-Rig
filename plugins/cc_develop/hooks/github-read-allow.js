#!/usr/bin/env node
// github-read-allow.js — decision module behind allow-dispatch.js (lane `gh-read`, rank 3)
//
// PURPOSE
//   GitHub reads run without a stop or question (user policy). This module gives a PreToolUse allow
//   to the commands it covers — allowlisted gh reads and nothing else — in every session and spawned
//   agent, with no grant or record. Every other command and every command it cannot read exactly gets
//   no opinion from here and reaches normal permission handling. It never denies: hooks/gh-write-guard.js
//   gates gh writes, and this module's allowlist is strictly narrower than "not a write".
//   allow-dispatch.js asks it after blueprint (provenance) and shape.
//
// WHAT AN ALLOW COVERS — gh reads and the filters they pipe into
//   A hook allow covers the WHOLE Bash command, while Claude Code matches an allow rule against each
//   subcommand of a compound command separately and checks every output-redirect target against the
//   Edit rules (permissions reference, "Compound commands", "Redirections"). An allow from here would
//   skip both checks for everything else in the command. So a command qualifies only when every
//   pipeline in it opens with an allowlisted gh read and goes on only into FILTERS: pipelines joined by
//   newlines, `;`, `&&` or `||`; no `|&`, no background `&`, no other program; no environment prefix
//   (`GH_HOST=…`, `GH_TOKEN=…` change the host or token); no redirection but `>/dev/null`,
//   `2>/dev/null`, `&>/dev/null` and `N>&M`.
//   Two independent readings must agree before anything is classified:
//     1. A strict grammar read here: plain words, single quotes (literal), double quotes holding no
//        backtick, backslash or `!` and no `$` but a plain `$NAME`/`${NAME}` (PLAIN_VARIABLE); unquoted
//        text only from UNQUOTED_CHAR and `?` (no `$`, backtick, other glob, tilde, parenthesis, `<`,
//        lone `&`, backslash but a line continuation, `=` opening a word — zsh expands `=cmd`); a
//        whole-word `#` comment. Anything else → no allow. A variable or unquoted `?` is run-time text,
//        allowed only in a `gh api` endpoint after a literal path holding a `/` (runTimeProblem).
//     2. lib/shell-git.js `programInvocations(command, name, info)`, the lexer gh-write-guard.js reads
//        gh with. No exact gh argv (null) or quoting bash and zsh read differently (`info.unreadable`)
//        → no allow. For gh and for each filter the command runs, the lexer's invocations of that
//        name, as a set, must equal the grammar's commands running it: brace expansion (`{1,2}`,
//        `jq {.,file}`), a gh or filter word inside another command's argument, shell source handed
//        to a runner — anything the two readings see differently — gives no allow.
//
// FILTERS — what a read may pipe into
//   `jq`, `head`, `tail`, `wc`, `sort`, `uniq`, `grep`, `cut`, `tr`, `column`, each by its plain name
//   (no path, no other case), with options read as getopt reads them against a closed option spec and
//   literal values only. None of them, so restricted, reads or writes a file, runs a program or reads
//   the environment, so the pipe reaches nothing the gh read alone did not: refused are a file operand
//   (`jq . f`, `head f`, `grep x f`, `uniq in out` — uniq writes its second operand), `jq -f/-L/
//   --rawfile/--slurpfile` and a jq program naming `env`/`$ENV`, `import`, `include` or `modulemeta`,
//   `sort -o/-T/--compress-program/--files0-from`, `grep -f/-r`, `wc --files0-from`, `tail -f`, and
//   every option not listed. Long names match exactly; each listed one is a complete option name, so GNU
//   getopt never completes it to another. Why: agents emit `gh … | jq …`, `| head`, `| wc -l` far more
//   than gh's own `--jq`, and each prompted though the read itself runs free (round 9b live lane, F3).
//
// THE READ ALLOWLIST (gh 2.x manual and source)
//   * Read subcommands: `gh pr view|diff|checks|list|status`, `gh issue view|list|status`, `gh repo
//     view|list`, `gh release view|list`, `gh run view|list`, `gh workflow view|list`, `gh label list`,
//     every `gh search` subcommand, `gh gist list|view`, `gh ruleset list|view`, `gh cache list`, `gh
//     project list|view`, `gh variable list`, `gh secret list` (names only) and `gh status`, the group
//     and verb right after `gh` (a flag before the verb lets gh take a later word as the verb). Left
//     out: verbs that write local files, log in or open a session (`run download`, `release download`,
//     `repo clone`, `gist clone`, `pr checkout`, `codespace`, `auth`) and every write; gh-write-guard.js
//     passes each listed verb, whatever its arguments. The verbs added in round 9b (`repo list` through
//     `gh status`) only list or show what the token can read (user policy: no stop for gh reads).
//   * Help and local reads, matched whole (localReadProblem): `gh --version`, `gh version`, `gh --help`,
//     `gh help [command [subcommand]]` and `gh <command> --help` for a core command or help topic
//     (HELP_SUBJECTS: never an extension, since `--help` reaches the extension program, and never
//     `auth`), `gh extension list`, `gh alias list`, `gh config list` and `gh config get <key>` for the
//     documented settings only (CONFIG_KEYS: `gh config get oauth_token -h <host>` prints the token from
//     the keyring), each with an optional `-h/--host`. They print, set, install and run nothing else.
//   * Their options and values are free — any option, any spelling (`--name=value`, `-L50`, a cluster,
//     `--`), `--web` and `--watch` included — except two things, found without knowing which option
//     takes a value, so every word that may be a value is checked:
//       - another host: `--hostname`; a `-R/--repo` value naming a host other than github.com (a
//         HOST/OWNER/REPO, a URL or `git@…`, go-gh `repository.Parse`); any word that is an `http(s)`
//         URL, or holds `://`, to a host other than github.com — gh takes a PR or issue URL as the
//         locator and sends the request to its host (gh `ParseURL`, `tryParseIssueFromURL`, both
//         `http`/`https` only). `gh repo view` reads its repository argument as `-R` reads its value,
//         so all its words are held to the `-R` rule. A word without such a scheme (`owner:branch`,
//         `//x`) is a branch, number or search text to gh.
//       - `-q/--jq` naming `env` (see below).
//     A `-R` or `-q` value is read in every pflag spelling: `--repo v`, `--repo=v`, `-R v`, `-Rv`,
//     `-R=v`, and the letter inside a cluster (`-cR v`): the one word gh could take as its value.
//   * `gh api <endpoint>` (REST) stays strict: exactly one endpoint, a path — never a URL (`https://`,
//     `//host`, any `scheme:`), so the request stays on the configured host; no `--input`, no
//     `--hostname`, no `--verbose` (prints the request with its headers), headers only `Accept` and
//     `X-GitHub-Api-Version`. `-X/--method` only as GET (case ignored, every one given), the read
//     gh-write-guard.js also passes. `-f/-F/--raw-field/--field` (plain `key=value`, never `-F k=@file`)
//     only under an explicit GET, where gh sends them as query parameters instead of a POST body, and
//     only with a literal endpoint that cannot reach GraphQL (GRAPHQL_PATH, DOT_SEGMENT). The endpoint
//     may carry a `?query`, quoted or not, and double-quoted plain variables (`"repos/$SLUG/pulls/$N"`),
//     but a variable or unquoted `?` only after a literal path holding a `/` (RUN_TIME_PREFIX): Go's URL
//     parser stops reading a scheme at that `/`, so no value or glob match can name another host or make
//     the endpoint `graphql`. An unquoted variable is refused: word splitting could add `-X POST`. Why:
//     `?per_page=100`, `-X GET … -f q=…` and a variable path are how agents page and search; each
//     prompted though it reads (round 9b live lane F3, review GH-READ-GET).
//   * `gh api graphql`: one `query` field given inline with `-f/--raw-field` or with `-F/--field`
//     (never `@file` or `@-`: `-F` reads those from disk or stdin), opening with `query` or `{`, no
//     `mutation` or `subscription` word anywhere; other fields are plain variables. Only the `gh api`
//     options listed in API_OPTIONS, spelled `--name=value`, `--name value` or `-x value`.
//   * `-q/--jq` never names `env` (case ignored): gh compiles jq with the process environment loaded
//     (go-gh pkg/jq `gojq.WithEnvironLoader`), so `env.GH_TOKEN` or `$ENV` would print a secret.
//     `--template` functions (go-gh pkg/template) read no environment, file or process.
//   Never covered: a query file, a run-time word outside a `gh api` endpoint (variable, glob), a
//   substitution, a gh alias or extension (only the core groups above are named), a write.
//   The allow emits no updatedInput, so settings deny and ask rules still apply to it.
//
// LIMITS (accepted)
//   * Any repository slug the token can read is covered, private ones included.
//   * A variable endpoint is read at run time: any path on the configured host, `?query` included.
//   * A shell alias or function named like a filter (an rc file) is beyond string inspection, as for gh.
//   * The host and `--jq` checks read a word wherever it may be an option value, so a value that only
//     looks like a host or `env` prompts: a search for URL text, `--branch a/b/c` under `gh repo view`,
//     a `-R` letter inside another option's value.
//   * zsh options that change quoting or expansion when set (RC_QUOTES, BRACE_CCL, EXTENDED_GLOB with
//     an unquoted `^`, `#`, `~` — the last three never reach an allow here) are read as off.
//   * The lexer's own LIMITS (lib/shell-git.js) apply to its reading.
//
// RESULT (evaluate)
//   decision "none"         no opinion: not a Bash call (`not-applicable`) or no gh invocation (`no-gh`)
//   decision "passthrough"  a gh command examined and declined, `why` one of: unreadable, not-gh-only,
//                           lexer-mismatch, not-a-read
//   decision "allow"        payload set
//
// EXIT CODES
//   0  always — passthrough (no output) or allow (JSON on stdout).

"use strict";

const path = require("path");

// Loaded defensively: without the lexer no command has an exact gh argv, so nothing is allowed.
let programInvocations = null;
try {
  ({ programInvocations } = require(path.join(__dirname, "lib", "shell-git.js")));
} catch (_) {
  // programInvocations stays null
}

const LANE = Object.freeze({ lane: "gh-read", rank: 3 });

// --- strict grammar ---

// Unquoted characters a word may hold. `{` `}` are let through: brace expansion is caught by the
// cross-check with the lexer, which expands braces as bash does.
const UNQUOTED_CHAR = /^[A-Za-z0-9_./:=,@%+{}-]$/;
// The one unquoted glob character read, for a `?query` endpoint: a run-time character (pathname expansion
// may replace it), allowed only where runTimeProblem allows one.
const UNQUOTED_GLOB = "?";
// Inside double quotes: escape, history and backtick characters. `$` is read on its own (PLAIN_VARIABLE).
const DOUBLE_QUOTED_FORBIDDEN = /[`\\!]/;
// The one expansion read inside double quotes: `$NAME` or `${NAME}`, followed by no further name character
// and no `[` (a zsh subscript, evaluated as arithmetic). Every other `$` (`$(`, `${x:-…}`, `$1`, `$@`,
// `${(e)x}`) is refused.
const PLAIN_VARIABLE = /^\$(?:[A-Za-z_][A-Za-z0-9_]*(?![[A-Za-z0-9_])|\{[A-Za-z_][A-Za-z0-9_]*\}(?!\[))/;
// What may follow a redirection target or an operator: the end, a blank, or another operator.
const BOUNDARY = /^(?:[ \t\n;&|]|$)/;
const NULL_DEVICE = "/dev/null";

/**
 * The index after an allowed output redirection opening at `i` (`>`, `>>`, `&>`, `&>>` onto /dev/null, or
 * `>&N`), or -1 for any other redirection.
 */
function redirectionEnd(src, i) {
  let j = src[i] === "&" ? i + 2 : i + 1;
  if (src[i] === ">" && src[j] === "&") {
    const digits = /^\d+/.exec(src.slice(j + 1));
    if (!digits) return -1;
    const end = j + 1 + digits[0].length;
    return BOUNDARY.test(src.slice(end, end + 1)) ? end : -1;
  }
  if (src[j] === ">") j += 1;
  while (src[j] === " " || src[j] === "\t") j += 1;
  const end = j + NULL_DEVICE.length;
  if (src.slice(j, end) !== NULL_DEVICE) return -1;
  return BOUNDARY.test(src.slice(end, end + 1)) ? end : -1;
}

/**
 * A double-quoted string opening at `i`: its text, closing index and the offset of its first `$` (-1 for none), or
 * null when unterminated or holding a forbidden character or any `$` but a plain variable.
 */
function doubleQuoted(src, i) {
  const end = src.indexOf('"', i + 1);
  if (end < 0) return null;
  const text = src.slice(i + 1, end);
  if (DOUBLE_QUOTED_FORBIDDEN.test(text)) return null;
  let runtime = -1;
  for (let k = text.indexOf("$"); k >= 0; k = text.indexOf("$", k + 1)) {
    const variable = PLAIN_VARIABLE.exec(text.slice(k));
    if (variable === null) return null;
    if (runtime < 0) runtime = k;
    k += variable[0].length - 1;
  }
  return { text, end, runtime };
}

/**
 * Lexing state for `simpleCommands`: the commands read so far, the command and word being built, the offset of
 * the word's first run-time character (an unquoted `?` or a variable; -1 for none), whether the last operator needs
 * a command after it (`&&`, `||`, `|`) and whether that command reads the previous one's output (`|`).
 */
function grammarState() {
  return {
    commands: [],
    words: [],
    runtimes: [],
    word: null,
    runtime: -1,
    quoted: false,
    pending: false,
    piped: false,
  };
}

function endWord(state) {
  if (state.word !== null) {
    state.words.push(state.word);
    state.runtimes.push(state.runtime);
  }
  state.word = null;
  state.runtime = -1;
  state.quoted = false;
}

/**
 * End the current simple command at a separator; false when the shell would reject it (`;`, `&&`, `||` or `|` with
 * no command before it). A newline may end an empty line, also right after `|`, `&&` or `||`.
 */
function endCommand(state, separator) {
  endWord(state);
  if (state.words.length) {
    state.commands.push({ words: state.words, runtimes: state.runtimes, piped: state.piped });
    state.words = [];
    state.runtimes = [];
    state.pending = false;
    state.piped = false;
  } else if (separator !== "\n") {
    return false;
  }
  if (separator === "&&" || separator === "||" || separator === "|") state.pending = true;
  if (separator === "|") state.piped = true;
  return true;
}

/** Append `text` to the current word; `runtime` is the offset of its first run-time character in `text`, or -1. */
function appendText(state, text, runtime, quoted) {
  const before = state.word === null ? "" : state.word;
  if (runtime >= 0 && state.runtime < 0) state.runtime = before.length + runtime;
  state.word = before + text;
  if (quoted) state.quoted = true;
}

/**
 * Read one operator or redirection at `i`; the index after it, or -1 when the grammar does not allow it.
 * Covers `\n`, `;`, `&&`, `||`, `|`, `>…`, `&>…`; `|&`, a lone `&`, `<` and `;;` are refused.
 */
function readOperator(state, src, i) {
  const c = src[i];
  const next = src[i + 1];
  if (c === "\n" || (c === ";" && next !== ";")) return endCommand(state, c) ? i + 1 : -1;
  if ((c === "&" || c === "|") && next === c) return endCommand(state, c + c) ? i + 2 : -1;
  if (c === "|" && next !== "&") return endCommand(state, c) ? i + 1 : -1;
  if (c === ">" || (c === "&" && next === ">")) {
    // An unquoted number right before `>` names the file descriptor; any other word ends there.
    if (c === ">" && state.word !== null && !state.quoted && /^\d+$/.test(state.word)) state.word = null;
    endWord(state);
    return redirectionEnd(src, i);
  }
  return -1;
}

/** Read one word character, quote, escape or comment at `i`; the index after it, or -1 outside the grammar. */
function readWordPart(state, src, i) {
  const c = src[i];
  if (c === "'") {
    const end = src.indexOf("'", i + 1);
    if (end < 0) return -1;
    appendText(state, src.slice(i + 1, end), -1, true);
    return end + 1;
  }
  if (c === '"') {
    const read = doubleQuoted(src, i);
    if (read === null) return -1;
    appendText(state, read.text, read.runtime, true);
    return read.end + 1;
  }
  if (c === "\\") return src[i + 1] === "\n" ? i + 2 : -1; // a line continuation joins, any other escape is refused
  if (c === "#") {
    if (state.word !== null) return -1; // `a#b` is literal to bash; refused rather than read
    const newline = src.indexOf("\n", i);
    return newline < 0 ? src.length : newline;
  }
  if (c === UNQUOTED_GLOB) {
    appendText(state, c, 0, false);
    return i + 1;
  }
  if (!UNQUOTED_CHAR.test(c) || (c === "=" && state.word === null)) return -1;
  appendText(state, c, -1, false);
  return i + 1;
}

/**
 * The simple commands of `src` read under the strict grammar, each `{words, runtimes, piped}` — `runtimes[k]` the
 * offset of the first run-time character of `words[k]` (-1 for none), `piped` true when it reads the previous
 * command's output — or null when `src` holds anything outside the grammar. Allowed redirections are dropped with
 * their target, as the lexer drops them.
 */
function simpleCommands(src) {
  const state = grammarState();
  let i = 0;
  while (i >= 0 && i < src.length) {
    const c = src[i];
    if (c === " " || c === "\t") {
      endWord(state);
      i += 1;
    } else if (c === "\n" || c === ";" || c === "&" || c === "|" || c === ">" || c === "<") {
      i = readOperator(state, src, i);
    } else {
      i = readWordPart(state, src, i);
    }
  }
  if (i < 0 || !endCommand(state, "\n") || state.pending) return null;
  return state.commands;
}

// --- read allowlist ---

/** An option table from `[spellings, kind]` rows; kinds: flag, value, repo, jq, header, raw-field, typed-field. */
function optionTable(...groups) {
  const table = new Map();
  for (const rows of groups) {
    for (const [spellings, kind] of rows) for (const spelling of spellings) table.set(spelling, kind);
  }
  return table;
}

/**
 * `gh <group>` → its read verbs (gh manual); `null` where every subcommand of the group reads (`gh search`).
 * Options are not listed: a read verb takes any, under the host and `--jq` checks of `argumentsProblem`.
 */
const READ_VERBS = new Map([
  ["pr", new Set(["view", "diff", "checks", "list", "status"])],
  ["issue", new Set(["view", "list", "status"])],
  ["repo", new Set(["view", "list"])],
  ["release", new Set(["view", "list"])],
  ["run", new Set(["view", "list"])],
  ["workflow", new Set(["view", "list"])],
  ["label", new Set(["list"])],
  ["search", null],
  ["gist", new Set(["list", "view"])],
  ["ruleset", new Set(["list", "view"])],
  ["cache", new Set(["list"])],
  ["project", new Set(["list", "view"])],
  ["variable", new Set(["list"])],
  ["secret", new Set(["list"])],
  ["status", null],
]);
/** The read whose positional is a repository, parsed as a `-R/--repo` value is (`gh repo view [HOST/]OWNER/REPO`). */
const REPOSITORY_ARGUMENT = "repo view";
/** `gh api` read options (gh manual, `gh api`); fields are read here and refused for REST in `apiProblem`. */
const API_OPTIONS = optionTable([
  [["--paginate", "--slurp", "-i", "--include", "--silent"], "flag"],
  [["-q", "--jq"], "jq"],
  [["-t", "--template", "-p", "--preview", "--cache"], "value"],
  [["-H", "--header"], "header"],
  [["-f", "--raw-field"], "raw-field"],
  [["-F", "--field"], "typed-field"],
  [["-X", "--method"], "method"],
]);
// The one method a read may name (case ignored, as gh-write-guard.js reads it): under it gh sends fields as
// query parameters (`gh api --help`: "--method GET … -f q=…").
const READ_METHOD = "GET";

// gh commands that print help or read local state, matched whole by `localReadProblem`.
const VERSION_WORDS = new Set(["--version", "version"]);
// What `gh help` and `gh <word> --help` may name: the core command groups of the gh 2.x manual and its help topics.
// `auth` is left out; an extension is never named, since `--help` reaches the extension program itself.
const HELP_SUBJECTS = new Set([
  "agent-task",
  "alias",
  "api",
  "attestation",
  "browse",
  "cache",
  "codespace",
  "completion",
  "config",
  "extension",
  "gist",
  "gpg-key",
  "issue",
  "label",
  "org",
  "pr",
  "preview",
  "project",
  "release",
  "repo",
  "ruleset",
  "run",
  "search",
  "secret",
  "ssh-key",
  "status",
  "variable",
  "workflow",
  "accessibility",
  "actions",
  "environment",
  "exit-codes",
  "formatting",
  "mintty",
  "reference",
]);
const HELP_WORD = /^[a-z][a-z-]*$/;
// The settings `gh config get` may print (`gh config --help`). Any other key is refused: `gh config get
// oauth_token -h <host>` prints the token from the keyring.
const CONFIG_KEYS = new Set([
  "git_protocol",
  "editor",
  "prompt",
  "prefer_editor_prompt",
  "pager",
  "http_unix_socket",
  "browser",
  "color_labels",
  "accessible_colors",
  "accessible_prompter",
  "spinner",
]);
// Groups whose one read verb `list` takes no argument: it lists local installs or aliases.
const LOCAL_LISTS = new Set(["extension", "alias"]);

// The one host a URL or repository in a read may name; case ignored, as DNS ignores it.
const GITHUB_URL = /^https?:\/\/github\.com(?:\/|$)/i;
const GITHUB_REPOSITORY = /^github\.com\/[^/]+\/[^/]+$/i;
// A word gh reads as a URL locator: an `http(s)` scheme (Go's url.Parse lower-cases it), or anything holding `://`.
const URL_WORD = /^https?:|:\/\//i;
// A repository naming its host (go-gh `repository.Parse`): any `scheme:` URL, `git@…`, or HOST/OWNER/REPO.
const REPOSITORY_WITH_HOST = /^[A-Za-z][A-Za-z0-9+.-]*:|^git@|\/[^/]*\//;
// A short-option word (`-R`, `-cR`), as opposed to a long one or a lone `-`.
const SHORT_OPTION = /^-[^-]/;
// gh compiles --jq with the environment loaded; `env` and `$ENV` read it.
const JQ_ENVIRONMENT = /env/i;
// Request headers that select a representation or API version and nothing else.
const READ_HEADERS = new Set(["accept", "x-github-api-version"]);
// A GraphQL field key: a plain variable name (no `key[]` or `key[sub]` nesting).
const FIELD_KEY = /^[A-Za-z_][A-Za-z0-9_]*$/;
const READ_QUERY = /^\s*(?:query\b|\{)/;
const WRITE_OPERATION = /\b(?:mutation|subscription)\b/i;
// A REST endpoint path: optional leading `/`, then a path character or a `{owner}`-style placeholder.
const API_PATH = /^\/?[A-Za-z0-9_{][^\s]*$/;
// A URL rather than a path: a scheme, or a network-path reference.
const URL_LIKE = /^[A-Za-z][A-Za-z0-9+.-]*:|^\/\/|:\/\//;
// The literal text before an endpoint's first variable or glob: a path character, then a `/`. Go's URL parser
// stops reading a scheme at that `/`, so no value after it can send the request to another host, and none can
// make the endpoint the bare word `graphql`.
const RUN_TIME_PREFIX = /^\/?[A-Za-z0-9_{][\s\S]*\//;
// Under GET, fields become query parameters. GitHub runs GraphQL operations only from a POST body (GraphQL
// guide, "Forming calls": GET is introspection), but a REST path that may resolve to the GraphQL endpoint — a
// leading `graphql`/`api/graphql`, a dot segment, an encoded dot — gets no fields here.
const GRAPHQL_PATH = /^\/*(?:api\/)?graphql(?:[/?#]|$)/i;
const DOT_SEGMENT = /(?:^|\/)\.{1,2}(?:[/?#]|$)|%2e/i;

/**
 * Why an option value of `kind` leaves the read allowlist, or null. GraphQL fields are collected into
 * `request.fields`, GET methods counted in `request.methods`.
 */
function valueProblem(kind, value, request) {
  if (kind === "jq") return JQ_ENVIRONMENT.test(value) ? "a --jq expression naming env" : null;
  if (kind === "method") {
    if (value.toUpperCase() !== READ_METHOD) return `gh api -X ${value}: a method other than GET`;
    request.methods += 1;
    return null;
  }
  if (kind === "header") {
    const colon = value.indexOf(":");
    const name = (colon < 0 ? value : value.slice(0, colon)).trim().toLowerCase();
    return READ_HEADERS.has(name) ? null : `header ${name || "(none)"} is not Accept or X-GitHub-Api-Version`;
  }
  if (kind === "raw-field" || kind === "typed-field") {
    const eq = value.indexOf("=");
    const key = eq < 0 ? value : value.slice(0, eq);
    if (eq < 0 || !FIELD_KEY.test(key)) return "a field that is not a plain key=value";
    if (kind === "typed-field" && value.slice(eq + 1).startsWith("@")) return "a field read from a file or stdin";
    request.fields.push({ key, value: value.slice(eq + 1) });
  }
  return null;
}

/**
 * Walk `gh api` arguments against API_OPTIONS: `{problem, positionals, fields, methods}` — why one leaves the
 * allowlist (null for none; the walk stops there), the indexes in `args` of the positionals, the fields, and how
 * many `-X GET` were given. Only `--name=value`, `--name value` and `-x value` spellings are read; an attached
 * short value (`-XGET`), a cluster (`-iq`) or `--` gives no allow.
 */
function apiRequest(args) {
  const request = { problem: null, positionals: [], fields: [], methods: 0 };
  for (let i = 0; i < args.length && request.problem === null; i += 1) {
    const arg = args[i];
    if (arg.length < 2 || !arg.startsWith("-")) {
      request.positionals.push(i);
      continue;
    }
    const eq = arg.startsWith("--") ? arg.indexOf("=") : -1;
    const name = eq < 0 ? arg : arg.slice(0, eq);
    const kind = API_OPTIONS.get(name);
    if (arg === "--") request.problem = "`--` ends gh's options";
    else if (kind === undefined) request.problem = `option ${name} is not on the read allowlist`;
    else if (kind === "flag") request.problem = eq >= 0 ? `flag ${name} given a value` : null;
    else {
      const value = eq >= 0 ? arg.slice(eq + 1) : args[(i += 1)];
      request.problem = value === undefined ? `option ${name} has no value` : valueProblem(kind, value, request);
    }
  }
  return request;
}

/** True when `word`, read as a URL locator, reaches a host other than github.com. */
function urlToOtherHost(word) {
  return URL_WORD.test(word) && !GITHUB_URL.test(word);
}

/** True when `word`, read as a repository (a `-R/--repo` value, a `gh repo view` word), names a host but github.com. */
function repositoryOnOtherHost(word) {
  return REPOSITORY_WITH_HOST.test(word) && !GITHUB_URL.test(word) && !GITHUB_REPOSITORY.test(word);
}

/**
 * Every word `args` may hand to the option `long`/`-short`, in each pflag spelling: `--long v`, `--long=v`, `-s v`,
 * `-sv`, `-s=v` and the letter inside a short cluster (`-cs v`, `-csv`). Read without knowing which options take a
 * value: a word gh gives another option may be included, the word gh gives this one always is.
 */
function optionValues(args, long, short) {
  const values = [];
  args.forEach((arg, i) => {
    if (arg === long) {
      values.push(args[i + 1]);
    } else if (arg.startsWith(`${long}=`)) {
      values.push(arg.slice(long.length + 1));
    } else if (SHORT_OPTION.test(arg) && arg.includes(short, 1)) {
      const rest = arg.slice(arg.indexOf(short, 1) + 1).replace(/^=/, "");
      values.push(rest === "" ? args[i + 1] : rest);
    }
  });
  return values.filter((value) => value !== undefined);
}

/**
 * Why the words after a read verb reach another host or print a secret, or null; every other option and value is
 * free. `command` is `<group> <verb>`, whose `repo view` argument is held to the `-R` rule.
 */
function argumentsProblem(command, args) {
  if (args.some((arg) => arg === "--hostname" || arg.startsWith("--hostname="))) return "--hostname names a host";
  if (optionValues(args, "--repo", "R").some(repositoryOnOtherHost)) {
    return "-R/--repo naming a host other than github.com";
  }
  if (command === REPOSITORY_ARGUMENT && args.some(repositoryOnOtherHost)) {
    return "a repository naming a host other than github.com";
  }
  if (args.some(urlToOtherHost)) return "a URL to a host other than github.com";
  const jq = optionValues(args, "--jq", "q");
  return jq.some((value) => JQ_ENVIRONMENT.test(value)) ? "a --jq expression naming env" : null;
}

/** Why a `gh <group> <verb> …` argv is not an allowlisted read, or null. */
function verbProblem(argv) {
  const [, group, verb] = argv;
  const verbs = READ_VERBS.get(group);
  if (verbs === undefined) return `gh ${group || ""} is not an allowlisted read group`;
  if (verbs !== null && !verbs.has(verb)) return `gh ${group} ${verb || ""} is not an allowlisted read`;
  return argumentsProblem(`${group} ${verb}`, argv.slice(verbs === null ? 2 : 3));
}

/**
 * Why a GraphQL `query` field set leaves the allowlist, or null. The key compares case-insensitively, as
 * gh-write-guard.js reads it, so a second `Query` field cannot carry an unchecked operation.
 */
function graphqlProblem(fields) {
  const queries = fields.filter((field) => field.key.toLowerCase() === "query");
  if (queries.length !== 1) return "graphql without exactly one inline query field";
  const query = queries[0].value;
  if (WRITE_OPERATION.test(query)) return "graphql naming mutation or subscription";
  return READ_QUERY.test(query) ? null : "graphql query not opening with `query` or `{`";
}

/** Why a `gh api …` stage (`{words, runtimes}`) leaves the allowlist, or null. */
function apiProblem({ words, runtimes }) {
  const args = words.slice(2);
  const request = apiRequest(args);
  if (request.problem) return request.problem;
  if (request.positionals.length !== 1) return `gh api with ${request.positionals.length} endpoints`;
  const at = request.positionals[0];
  const endpoint = args[at];
  if (endpoint === "graphql") {
    return request.methods ? "gh api graphql with -X/--method" : graphqlProblem(request.fields);
  }
  if (!API_PATH.test(endpoint) || URL_LIKE.test(endpoint)) return "gh api endpoint that is not a path";
  if (!request.fields.length) return null;
  // GraphQL takes its query as a field; on REST a field turns the request into a POST unless GET is explicit.
  if (!request.methods) return "gh api REST call with fields and no explicit GET (gh sends a POST)";
  if (runtimes[at + 2] >= 0) return "gh api fields with an endpoint holding a variable or glob";
  return GRAPHQL_PATH.test(endpoint) || DOT_SEGMENT.test(endpoint)
    ? "gh api fields to a path that may reach GraphQL"
    : null;
}

/** Why `gh help …` (the words after `help`) names more than a core command or help topic, or null. */
function helpProblem(words) {
  if (words.length > 2) return "gh help with more than a command and a subcommand";
  if (words.length && !HELP_SUBJECTS.has(words[0])) return `gh help ${words[0]}: not a core command or help topic`;
  return words.length < 2 || HELP_WORD.test(words[1]) ? null : `gh help ${words.join(" ")}: not a subcommand name`;
}

/** Why `gh config …` (the words after `config`) is not `get <known key>` or `list`, with an optional host, or null. */
function configProblem(words) {
  const [verb, ...rest] = words;
  if (verb !== "get" && verb !== "list") return `gh config ${verb || ""} is not a read`;
  const operands = [];
  for (let i = 0; i < rest.length; i += 1) {
    const word = rest[i];
    if (word === "-h" || word === "--host") i += 1;
    else if (word.startsWith("-") && !word.startsWith("--host=")) return `gh config option ${word}`;
    else if (!word.startsWith("-")) operands.push(word);
  }
  if (rest.length && (rest[rest.length - 1] === "-h" || rest[rest.length - 1] === "--host")) {
    return "gh config --host without a value";
  }
  if (verb === "list") return operands.length ? "gh config list with an argument" : null;
  return operands.length === 1 && CONFIG_KEYS.has(operands[0]) ? null : "gh config get of a key outside the settings";
}

/**
 * Why a gh command printing help or reading local state takes more than it may, null when it is one, undefined when
 * `argv` is none of them: `gh --version`, `gh version`, `gh --help`, `gh help [command [subcommand]]`, `gh <command>
 * --help`, `gh extension list`, `gh alias list`, `gh config get <key>`, `gh config list`.
 */
function localReadProblem(argv) {
  const [, first, second] = argv;
  if (VERSION_WORDS.has(first) || first === "--help") return argv.length === 2 ? null : `gh ${first} with arguments`;
  if (first === "help") return helpProblem(argv.slice(2));
  if (second === "--help" && argv.length === 3) {
    return HELP_SUBJECTS.has(first) ? null : `gh ${first} --help: not a core command`;
  }
  if (LOCAL_LISTS.has(first) && second === "list") return argv.length === 3 ? null : `gh ${first} list with arguments`;
  if (first === "config") return configProblem(argv.slice(2));
  return undefined;
}

/** Why one gh stage (`{words, runtimes}`, words from `gh` on) is not an allowlisted read, or null. */
function readProblem(stage) {
  const argv = stage.words;
  if (argv[1] === "api") return apiProblem(stage);
  const local = localReadProblem(argv);
  return local === undefined ? verbProblem(argv) : local;
}

// --- filters a read may pipe into ---

// Option values: a count (`-n 5`, `-n +10`), a field or character list (`1,3-4`), any literal text.
const COUNT = /^[+-]?\d+$/;
const LIST = /^[\d,-]+$/;
const TEXT = /^[\s\S]*$/;
// jq programs that read beyond their input: the environment (`env`, `$ENV`; any `env` text is refused, as for
// `--jq`) and modules or data files from the library path (`import`, `include`, `modulemeta`).
const JQ_OUTSIDE_INPUT = /env|\b(?:import|include|modulemeta)\b/i;

/** Operand rule for a filter that reads only stdin: any operand would be a file. */
function noOperands(operands) {
  return operands.length ? `a file operand (${operands[0]})` : null;
}

/** jq: at most one operand, the program, which reads nothing but its input; a second operand is a file. */
function jqOperands(operands) {
  if (operands.length > 1) return `a file operand (${operands[1]})`;
  return operands.length && JQ_OUTSIDE_INPUT.test(operands[0])
    ? "a jq program reading the environment or a module"
    : null;
}

/** grep: the pattern is the one operand, or none when `-e`/`--regexp` gives it; any other operand is a file. */
function grepOperands(operands, seen) {
  const patterns = seen.has("e") || seen.has("--regexp") ? 0 : 1;
  return operands.length === patterns ? null : `grep with ${operands.length} operands (a file or no pattern)`;
}

/** tr: one or two character sets; tr never opens a file. */
function trOperands(operands) {
  return operands.length === 1 || operands.length === 2 ? null : `tr with ${operands.length} sets`;
}

/**
 * The filters a gh read may pipe into, as getopt reads them: `flags` short letters taking no value, `values` short
 * letters taking one (checked by pattern; attached or the next word), `long` long options (null for a flag, else the
 * value pattern; `--name=value` or `--name value`), `pairs` long options taking the next two words (jq `--arg name
 * value`), `numeric` whether `-N` is a count, and `operands(operands, seen)` why the non-option words are refused,
 * or null. Each long name is exact and none is a prefix GNU getopt would complete to another option. Nothing listed
 * reads or writes a file, runs a program or reads the environment: `jq -f/-L/--rawfile/--slurpfile`, `sort
 * -o/-T/--compress-program/--files0-from`, `uniq` output operand, `grep -f/-r`, `wc --files0-from`, `tail -f` are
 * absent, and a file operand is refused.
 */
const FILTERS = new Map([
  [
    "jq",
    {
      flags: "rcesSM",
      values: {},
      long: new Map(
        ["--raw-output", "--compact-output", "--exit-status", "--slurp", "--sort-keys", "--monochrome-output"].map(
          (name) => [name, null],
        ),
      ),
      pairs: new Set(["--arg", "--argjson"]),
      operands: jqOperands,
    },
  ],
  ["head", countFilter()],
  ["tail", countFilter()],
  [
    "wc",
    {
      flags: "lcwmL",
      long: new Map(["--lines", "--words", "--chars", "--bytes"].map((name) => [name, null])),
      operands: noOperands,
    },
  ],
  [
    "sort",
    {
      flags: "bdfghinMrsuV",
      values: { k: TEXT, t: TEXT },
      long: new Map([
        ...[
          "--reverse",
          "--numeric-sort",
          "--unique",
          "--ignore-case",
          "--human-numeric-sort",
          "--version-sort",
          "--general-numeric-sort",
          "--stable",
        ].map((name) => [name, null]),
        ["--key", TEXT],
        ["--field-separator", TEXT],
      ]),
      operands: noOperands,
    },
  ],
  [
    "uniq",
    {
      flags: "cdui",
      values: { f: COUNT, s: COUNT, w: COUNT },
      long: new Map(["--count", "--repeated", "--unique", "--ignore-case"].map((name) => [name, null])),
      operands: noOperands,
    },
  ],
  [
    "grep",
    {
      flags: "cEFinoqsvwx",
      values: { e: TEXT, m: COUNT, A: COUNT, B: COUNT, C: COUNT },
      long: new Map([
        ...[
          "--count",
          "--extended-regexp",
          "--fixed-strings",
          "--ignore-case",
          "--line-number",
          "--only-matching",
          "--quiet",
          "--silent",
          "--no-messages",
          "--invert-match",
          "--word-regexp",
          "--line-regexp",
        ].map((name) => [name, null]),
        ["--regexp", TEXT],
        ["--max-count", COUNT],
        ["--after-context", COUNT],
        ["--before-context", COUNT],
        ["--context", COUNT],
      ]),
      operands: grepOperands,
    },
  ],
  [
    "cut",
    {
      flags: "s",
      values: { d: TEXT, f: LIST, c: LIST, b: LIST },
      long: new Map([
        ["--delimiter", TEXT],
        ["--fields", LIST],
        ["--characters", LIST],
        ["--bytes", LIST],
        ["--only-delimited", null],
      ]),
      operands: noOperands,
    },
  ],
  ["tr", { flags: "cCds", operands: trOperands }],
  ["column", { flags: "tx", values: { s: TEXT, c: COUNT }, operands: noOperands }],
]);

/** The option spec `head` and `tail` share: a line or byte count, and `-N`. */
function countFilter() {
  return {
    flags: "q",
    values: { n: COUNT, c: COUNT },
    long: new Map([
      ["--lines", COUNT],
      ["--bytes", COUNT],
      ["--quiet", null],
    ]),
    numeric: true,
    operands: noOperands,
  };
}

/**
 * Read the long option `words[i]` of a filter: `{problem, next}` — why it is refused (null when allowed) and the index
 * of the last word it took.
 */
function longOption(name, spec, words, i, seen) {
  const word = words[i];
  const eq = word.indexOf("=");
  const option = eq < 0 ? word : word.slice(0, eq);
  seen.add(option);
  if (spec.pairs && spec.pairs.has(option) && eq < 0) {
    return { problem: i + 2 < words.length ? null : `${name} ${option} without its two values`, next: i + 2 };
  }
  if (!spec.long || !spec.long.has(option)) return { problem: `${name} option ${option}`, next: i };
  const pattern = spec.long.get(option);
  if (pattern === null) return { problem: eq < 0 ? null : `${name} flag ${option} given a value`, next: i };
  const next = eq < 0 ? i + 1 : i;
  const value = eq < 0 ? words[next] : word.slice(eq + 1);
  return { problem: value !== undefined && pattern.test(value) ? null : `${name} ${option} value`, next };
}

/**
 * Read the short-option cluster `words[i]` of a filter as getopt does: flags, then at most one letter taking the
 * rest of the word or the next word as its value. `{problem, next}` as for `longOption`.
 */
function shortOptions(name, spec, words, i, seen) {
  const word = words[i];
  if (spec.numeric && /^-\d+$/.test(word)) return { problem: null, next: i };
  for (let k = 1; k < word.length; k += 1) {
    const letter = word[k];
    seen.add(letter);
    if (spec.flags.includes(letter)) continue;
    const pattern = spec.values && spec.values[letter];
    if (!pattern) return { problem: `${name} option -${letter}`, next: i };
    const attached = word.slice(k + 1);
    const next = attached === "" ? i + 1 : i;
    const value = attached === "" ? words[next] : attached;
    return { problem: value !== undefined && pattern.test(value) ? null : `${name} -${letter} value`, next };
  }
  return { problem: null, next: i };
}

/** Why a filter stage (words from the filter's name on) leaves its option spec, or null. */
function filterProblem(words) {
  const [name] = words;
  const spec = FILTERS.get(name);
  const operands = [];
  const seen = new Set();
  for (let i = 1; i < words.length; i += 1) {
    const word = words[i];
    if (word === "--") return `${name} --`;
    if (word.length > 1 && word.startsWith("-")) {
      const read = word.startsWith("--")
        ? longOption(name, spec, words, i, seen)
        : shortOptions(name, spec, words, i, seen);
      if (read.problem) return read.problem;
      i = read.next;
    } else {
      operands.push(word);
    }
  }
  return spec.operands(operands, seen);
}

// --- command shape ---

/** The index in `words` of the one `gh api` endpoint, or -1 when the options leave no single one. */
function apiEndpointIndex(words) {
  const request = apiRequest(words.slice(2));
  return request.problem || request.positionals.length !== 1 ? -1 : request.positionals[0] + 2;
}

/**
 * Why a stage's run-time text (a variable or unquoted glob) may change what it does, or null. It is allowed only in a
 * `gh api` endpoint, after a literal path holding a `/` (RUN_TIME_PREFIX): elsewhere a glob may match a planted file
 * named like an option (`?R` → `-R`) and a variable may name another host or a file.
 */
function runTimeProblem({ words, runtimes }) {
  const at = runtimes.findIndex((offset) => offset >= 0);
  if (at < 0) return null;
  const endpoint = words[0] === "gh" && words[1] === "api" ? apiEndpointIndex(words) : -1;
  if (at !== endpoint || runtimes.some((offset, k) => offset >= 0 && k !== endpoint)) {
    return "a variable or glob outside the gh api endpoint";
  }
  return RUN_TIME_PREFIX.test(words[at].slice(0, runtimes[at])) ? null : "a variable or glob before a literal `/`";
}

/**
 * Why `commands` leave the allowed shape, or null: each pipeline opens with gh and goes on only into FILTERS, each
 * filter within its option spec, and run-time text appears only where runTimeProblem allows it.
 */
function shapeProblem(commands) {
  for (const stage of commands) {
    const [program] = stage.words;
    if (!stage.piped && program !== "gh") return "a command that is not gh";
    if (stage.piped && !FILTERS.has(program)) return `${program} after a pipe is not an allowed filter`;
    const problem = runTimeProblem(stage) || (stage.piped ? filterProblem(stage.words) : null);
    if (problem) return problem;
  }
  return null;
}

/**
 * True when the shared lexer reads every program of `commands` as the grammar does: for gh and each filter, the
 * lexer's invocations of that name, as a set, equal the grammar's commands running it.
 */
function lexerAgrees(command, commands, ghInvocations) {
  const byProgram = new Map();
  for (const { words } of commands) {
    if (!byProgram.has(words[0])) byProgram.set(words[0], []);
    byProgram.get(words[0]).push(words);
  }
  for (const [program, argvs] of byProgram) {
    const lexed = program === "gh" ? ghInvocations : programInvocations(command, program);
    if (lexed === null || !sameInvocations(argvs, lexed)) return false;
  }
  return true;
}

/** True when the two argv lists hold the same invocations, order and repeats ignored. */
function sameInvocations(left, right) {
  const key = (argvs) => new Set(argvs.map((argv) => JSON.stringify(argv)));
  const a = key(left);
  const b = key(right);
  return a.size === b.size && [...a].every((entry) => b.has(entry));
}

/**
 * Classify a Bash command without consulting any grant: `{ok: true}` when every pipeline is an allowlisted gh read,
 * optionally piped into allowed filters, else `{ok: false, decision, why, detail?}` — `decision` "none" when the
 * command runs no gh at all.
 */
function classify(command) {
  if (!/gh/i.test(command)) return { ok: false, decision: "none", why: "no-gh" };
  if (programInvocations === null) return { ok: false, decision: "passthrough", why: "unreadable" };
  const info = {};
  const invocations = programInvocations(command, "gh", info);
  if (invocations === null || info.unreadable) return { ok: false, decision: "passthrough", why: "unreadable" };
  if (invocations.length === 0) return { ok: false, decision: "none", why: "no-gh" };
  const commands = simpleCommands(command);
  const shape = commands === null ? "text outside the strict grammar" : shapeProblem(commands);
  if (shape) return { ok: false, decision: "passthrough", why: "not-gh-only", detail: shape };
  if (!lexerAgrees(command, commands, invocations))
    return { ok: false, decision: "passthrough", why: "lexer-mismatch" };
  for (const stage of commands) {
    const detail = stage.piped ? null : readProblem(stage);
    if (detail) return { ok: false, decision: "passthrough", why: "not-a-read", detail };
  }
  return { ok: true };
}

// --- hook ---

/** The parsed payload and its trimmed Bash command, or null when there is no command to examine. */
function bashCall(raw) {
  let data;
  try {
    data = JSON.parse(raw);
  } catch (_) {
    return null;
  }
  if (!data || data.tool_name !== "Bash") return null;
  const command = data.tool_input && data.tool_input.command;
  if (typeof command !== "string" || !command.trim()) return null;
  return { data, command: command.trim() };
}

/** This module's allow payload. */
function allowPayload() {
  return {
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "allow",
      permissionDecisionReason: "gh read auto-allow — every command is an allowlisted gh read",
    },
  };
}

/**
 * Classify what this module does with `raw`, for allow-dispatch.js and its audit record (see RESULT above).
 * Never calls `process.exit` and never writes anything.
 *
 * @returns {{payload: object|null, decision: string, lane: string, rank: number, why?: string, src?: string}}
 */
function evaluate(raw) {
  const call = bashCall(raw);
  if (call === null) return { ...LANE, payload: null, decision: "none", why: "not-applicable" };
  const shape = classify(call.command);
  if (!shape.ok) return { ...LANE, payload: null, decision: shape.decision, why: shape.why };
  return { ...LANE, payload: allowPayload(), decision: "allow" };
}

/** The hook's stdout payload for a raw stdin string, or null for passthrough. */
function decide(raw) {
  return evaluate(raw).payload;
}

// Requiring this module attaches no stdin listener and never exits: allow-dispatch.js requires it as a library.
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
  classify,
  decide,
  evaluate,
  simpleCommands,
};
