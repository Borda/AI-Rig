#!/usr/bin/env node
// gh-write-guard.js — PreToolUse hook (matcher: Bash)
//
// PURPOSE
//   Claude and every agent are read-only on GitHub. The shipped allow lists pre-approve
//   `Bash(gh api repos/*)` and `Bash(gh api graphql:*)`, and both also match writes:
//   `gh api repos/o/r/issues -f title=x` is a POST, `gh api graphql -f query='mutation{…}'` a
//   mutation. The deny lists match `-X`/`--method` only directly after `gh api`, and removing the
//   allows would not reach existing installs, since setup only adds entries. This hook hard-denies
//   every gh write instead: a hook that exits 2 stops the call before permission rules are
//   evaluated, so the block holds even where an allow rule matches (permissions reference, "Extend
//   permissions with hooks"), and an exit 2 routes as `deny`, which wins over another hook's
//   `allow`. It ships byte-identical in foundry, oss, develop and research (bin/propagate_shared.py),
//   so each plugin installed alone carries it.
//
// HOW IT WORKS
//   1. lib/shell-git.js finds every gh invocation however the shell spells it: grouped, behind a
//      prefix, quoted, in a substitution, here-string or heredoc fed to a shell, or as shell source
//      (`bash -c`, `eval`). Name case and `.exe` are ignored. The lexer drops redirections
//      (`2>/dev/null`, `> out.json`, `2>&1`) while it still sees the quoting, so a quoted `'>'` stays
//      an argument and `'a>'&gh …` still starts a second command.
//   2. Subcommands: flags before and between the subcommand words are skipped, an unknown flag
//      read both as taking a value and not, so `gh pr -R o/r create` is still `pr create`. The
//      words are matched against WRITES, the group and verb aliases of the gh 2.102 reference folded
//      (`gh cs delete`, `gh agent create`, `gh repo autolink new`), case ignored. A group word, or a
//      word where a write verb of the group could stand, that holds a variable or substitution
//      (`gh pr "$VERB"`, `` gh pr "`…`" ``) is blocked: the command text does not show the subcommand.
//   3. `gh api` fails closed, parsed as gh parses its flags (clusters, `--flag=value`, `-Xvalue`),
//      flags placed before the `api` word included (`gh -X POST api …`: gh parses them on `api`):
//      * `-X`/`--method` anywhere with any value but a literal GET (case ignored), a run-time value
//        (`-X "$M"`) included, and `--input` (a body from a file or stdin)
//      * a method-override header (`-H 'X-HTTP-Method-Override: POST'`, `X-HTTP-Method`,
//        `X-Method-Override`; name case ignored) or a header whose name the command text does not
//        show (`-H "$H"`); a run-time value under a visible name (`-H "Authorization: Bearer $T"`)
//        stays a read
//      * REST: any `-f`/`-F`/`--raw-field`/`--field` without an explicit GET, since fields switch gh
//        api to POST; under `-X GET` they are query parameters (`gh api --method GET search/code
//        --field q=…`, the form `gh api --help` gives for a GET with parameters)
//      * GraphQL (`gh api graphql`): a `query` field that is not a visible inline read query — a
//        query file (`-F query=@q.graphql`), a shell value (`query="$Q"`, `$(…)`, backticks), text
//        not opening with `query` or `{`, or any `mutation`/`subscription` word
//      * an unknown `gh api` option, or more than one endpoint
//   4. Everything else — reads, `gh pr checkout`, and unknown subcommands, gh aliases and
//      extensions — passes to normal permission handling, which prompts unless an allow matches.
//   5. Fail closed where no exact argv exists: the lexer cannot follow the quoting (unbalanced
//      quote, unterminated substitution, brace expansion past its cap, or quoting bash and zsh
//      read differently — `$"…"`, a `$'…'` escape other than the named, octal and `\xHH` ones,
//      `$$` before a quote, `{` or `(`, a single quote inside a double-quoted `${…}`; the Bash tool runs the user's
//      shell), gh is named in
//      `python3 -c`/`perl -e`/`node -e` code, lib/shell-git.js cannot load, or classifying throws.
//      Then every command naming gh together with `api` or a write verb is blocked, read with and
//      without its quote and backslash characters, and so is every command whose quoting the shells
//      read differently, which may hide the gh word itself. A guessed argv cannot fail closed: stripping
//      quotes from `--title 'a b' create` shifts `create` off the verb position.
//
// LIMITS (accepted)
//   * Text built at run time: a variable as the program (`$GH pr create`), a shell value spliced
//     inside a visible query (`query="query { $X }"`), a script on disk, and the limits
//     lib/shell-git.js lists for every program.
//   * gh aliases and extensions (`gh alias set`, `gh <extension>`) are not resolved; they reach
//     the normal permission prompt, which no shipped allow entry pre-approves.
//   * A glob naming a subcommand or flag needs a planted file (`gh pr creat?`); globs are not read
//     as writes, so an unquoted `?` in a REST path (`repos/o/r/pulls?state=open`) stays a read.
//   * Other programs that reach the API (`curl`, `python -c 'requests.post(…)'`) are outside this
//     hook; the deny lists and the read-only rule cover what they can.
//   * A plugin mod that handles `tool.check` can approve a call a PreToolUse hook blocked, unless the
//     hook is in managed settings (permissions reference, "Extend permissions with hooks").
//   * Item 5 over-blocks: a read made from interpreter code, or a command the lexer cannot follow,
//     is blocked when its text names gh with `api` or a write verb anywhere.
//   * No Node.js: each plugin's hooks.json runs this file with the first Node it finds and exits 0
//     when there is none, so the guard is inactive (fail open); the plugin's SessionStart diagnostic
//     reports "Node.js not found".
//
// EXIT CODES
//   0  no gh write found, the payload is not a Bash call, or no Node.js ran the hook (launcher exits
//      0, see LIMITS): normal permission handling continues.
//   2  a gh write, or a command item 5 cannot show is not one: blocked; stderr names the reason and
//      says what to do instead.

"use strict";

const path = require("path");

// Loaded defensively: a crash would exit 1, which Claude Code treats as non-blocking, letting the
// write through. Without the lexer, gh writes fail closed below.
let programInvocations = null;
let XARGS_TAIL = null;
try {
  ({ programInvocations, XARGS_TAIL } = require(path.join(__dirname, "lib", "shell-git.js")));
} catch {
  // programInvocations stays null
}

// Every gh subcommand that writes to GitHub (gh 2.102 command reference): group → verbs; a verb of
// two words names a third level. Aliases gh ships are listed beside their command (`pr new`).
const WRITES = {
  "agent-task": ["create"],
  cache: ["delete"],
  codespace: ["create", "delete", "edit", "ports visibility", "rebuild", "stop"],
  discussion: ["comment", "create", "edit"],
  gist: ["create", "new", "delete", "edit", "rename"],
  "gpg-key": ["add", "delete"],
  issue: [
    "close",
    "comment",
    "create",
    "new",
    "delete",
    "develop",
    "edit",
    "lock",
    "pin",
    "reopen",
    "transfer",
    "unlock",
    "unpin",
  ],
  label: ["clone", "create", "delete", "edit"],
  pr: [
    "close",
    "comment",
    "create",
    "new",
    "draft",
    "edit",
    "lock",
    "merge",
    "ready",
    "reopen",
    "revert",
    "review",
    "unlock",
    "update-branch",
  ],
  project: [
    "close",
    "copy",
    "create",
    "delete",
    "edit",
    "field-create",
    "field-delete",
    "item-add",
    "item-archive",
    "item-create",
    "item-delete",
    "item-edit",
    "link",
    "mark-template",
    "unlink",
  ],
  release: ["create", "new", "delete", "delete-asset", "edit", "upload"],
  repo: [
    "archive",
    "autolink create",
    "autolink new",
    "autolink delete",
    "create",
    "new",
    "delete",
    "deploy-key add",
    "deploy-key delete",
    "edit",
    "fork",
    "rename",
    "sync",
    "unarchive",
  ],
  run: ["cancel", "delete", "rerun"],
  secret: ["delete", "remove", "set"],
  skill: ["publish"],
  "ssh-key": ["add", "delete"],
  variable: ["delete", "remove", "set"],
  workflow: ["disable", "enable", "run"],
};
// Group aliases gh ships (every group `Aliases` entry of the gh 2.102 reference; pinned by
// tests/fixtures/gh_reference_aliases.json).
const GROUP_ALIASES = {
  agent: "agent-task",
  agents: "agent-task",
  "agent-tasks": "agent-task",
  at: "attestation",
  cs: "codespace",
  ext: "extension",
  extensions: "extension",
  rs: "ruleset",
  skills: "skill",
};
// gh flags that take a value wherever they sit before the subcommand words; any other flag is read
// both ways.
const VALUE_FLAGS = new Set(["-R", "--repo"]);
// Flags that take a value after a group word, in that group only (gh 2.102 `gh codespace ports --help`).
const GROUP_VALUE_FLAGS = { codespace: new Set(["-c", "--codespace"]) };

// `gh api` options (gh 2.102 `gh api --help`), by how they take a value.
const API_VALUE_SHORT = { H: "header", p: "preview", q: "jq", t: "template", X: "method", f: "raw-field", F: "field" };
const API_BOOL_SHORT = { i: "include", h: "help" };
const API_VALUE_LONG = new Set([
  "cache",
  "field",
  "header",
  "hostname",
  "input",
  "jq",
  "method",
  "preview",
  "raw-field",
  "template",
]);
const API_BOOL_LONG = new Set(["help", "include", "paginate", "silent", "slurp", "verbose"]);
// The one method a read may name (case ignored): under it gh sends -f/-F fields as query parameters.
const READ_METHOD = "GET";
// A GraphQL query the command text shows: an operation opening with `query` or a bare selection set.
const READ_QUERY = /^\s*(?:query\b|\{)/;
const WRITE_OPERATION = /\b(?:mutation|subscription)\b/i;
// Shell expansions GraphQL syntax never uses: the query text arrives at run time.
const SHELL_VALUE = /\$[({]|`/;
// Headers that make a server read another method than the one sent (`-X GET -H '…: POST'`).
const METHOD_OVERRIDE_HEADERS = new Set(["x-http-method-override", "x-http-method", "x-method-override"]);
// A header name holding a shell expansion: the name arrives at run time.
const RUN_TIME_TEXT = /[$`]/;

const FALLBACK_VERBS = [...new Set(Object.values(WRITES).flatMap((verbs) => verbs.flatMap((v) => v.split(" "))))];
const FALLBACK_WRITE = new RegExp(
  `(?<![\\w.-])gh(?:\\.exe)?(?![\\w.-])[\\s\\S]*\\b(?:api|${FALLBACK_VERBS.join("|")})\\b`,
  "i",
);

// --- subcommands ---

/** Indexes of `args` that are not flags or flag values; `unknownTakesValue` picks the reading. */
function positionalIndexes(args, unknownTakesValue) {
  const indexes = [];
  let groupFlags = new Set();
  for (let i = 0; i < args.length; i += 1) {
    const arg = args[i];
    if (arg === "--") {
      for (let k = i + 1; k < args.length; k += 1) indexes.push(k);
      break;
    }
    if (arg.length > 1 && arg.startsWith("-")) {
      const bare = arg.startsWith("--") ? !arg.includes("=") : arg.length === 2;
      if (bare && (VALUE_FLAGS.has(arg) || groupFlags.has(arg) || unknownTakesValue)) i += 1;
    } else {
      if (!indexes.length) {
        const word = arg.toLowerCase();
        groupFlags = GROUP_VALUE_FLAGS[GROUP_ALIASES[word] || word] || groupFlags;
      }
      indexes.push(i);
    }
  }
  return indexes;
}

/** `gh <group> <verb>` for a subcommand write in `words` (lower-cased positionals), or null. */
function subcommandWrite(words) {
  const group = GROUP_ALIASES[words[0]] || words[0];
  for (const verb of WRITES[group] || []) {
    if (verb.split(" ").every((part, k) => words[k + 1] === part)) return `gh ${group} ${verb}`;
  }
  return null;
}

/**
 * Why a subcommand word the command text does not show could make `words` a write, or null: a run-time
 * group word (`gh "$G" create`), or a run-time word where a write verb of the group could stand
 * (`gh pr "$VERB"`, `gh repo autolink "$(…)"`). A run-time value after a read verb (`gh pr view "$N"`)
 * stays a read.
 */
function runTimeSubcommand(words) {
  if (RUN_TIME_TEXT.test(words[0])) return `gh ${words[0]}: a subcommand the command text does not show`;
  const group = GROUP_ALIASES[words[0]] || words[0];
  for (const verb of WRITES[group] || []) {
    const parts = verb.split(" ");
    for (const [k, part] of parts.entries()) {
      const word = words[k + 1];
      if (word !== undefined && RUN_TIME_TEXT.test(word)) {
        return `gh ${group} ${[...parts.slice(0, k), word].join(" ")}: a verb the command text does not show`;
      }
      if (word !== part) break;
    }
  }
  return null;
}

// --- gh api ---

/** `gh api` arguments sorted as gh's flag parser reads them. */
function parseApi(args) {
  const api = { endpoints: [], methods: [], headers: [], fields: [], input: false, unknown: null };
  const record = (name, value) => {
    if (name === "method") api.methods.push(value);
    else if (name === "header") api.headers.push(value);
    else if (name === "input") api.input = true;
    else if (name === "raw-field" || name === "field") api.fields.push({ typed: name === "field", spec: value });
  };
  for (let i = 0; i < args.length; i += 1) {
    const arg = args[i];
    if (arg === "--") {
      api.endpoints.push(...args.slice(i + 1));
      break;
    }
    if (arg.startsWith("--")) {
      const eq = arg.indexOf("=");
      const name = arg.slice(2, eq < 0 ? undefined : eq);
      if (API_BOOL_LONG.has(name)) continue;
      if (!API_VALUE_LONG.has(name)) {
        api.unknown = api.unknown || arg;
        continue;
      }
      if (eq >= 0) record(name, arg.slice(eq + 1));
      else record(name, args[++i] ?? "");
    } else if (arg.length > 1 && arg.startsWith("-")) {
      // A short cluster: flags without a value, then at most one that takes the rest or the next arg.
      for (let k = 1; k < arg.length; k += 1) {
        if (API_BOOL_SHORT[arg[k]]) continue;
        const name = API_VALUE_SHORT[arg[k]];
        if (!name) {
          api.unknown = api.unknown || arg;
          break;
        }
        const attached = arg.slice(k + 1);
        if (attached === "") record(name, args[++i] ?? "");
        else record(name, k === 1 && attached.startsWith("=") ? attached.slice(1) : attached);
        break;
      }
    } else {
      api.endpoints.push(arg);
    }
  }
  return api;
}

/**
 * Why a `-X`/`--method` value makes this `gh api` call a write, or null: anything but a literal GET,
 * a run-time value (`-X "$M"`) included.
 */
function apiMethodReason(api) {
  const other = api.methods.find((method) => method.toUpperCase() !== READ_METHOD);
  return other === undefined ? null : `gh api -X ${other}: a method other than a literal GET`;
}

/**
 * Why a `-H`/`--header` can change the method the server reads, or null: a method-override name, or
 * a name the command text does not show (`-H "$H"`). gh splits `name:value` at the first colon.
 */
function apiHeaderReason(api) {
  for (const header of api.headers) {
    const colon = header.indexOf(":");
    const name = (colon < 0 ? header : header.slice(0, colon)).trim().toLowerCase();
    if (RUN_TIME_TEXT.test(name)) return "gh api with a header whose name the command text does not show";
    if (METHOD_OVERRIDE_HEADERS.has(name)) return `gh api with a method-override header (${name})`;
  }
  return null;
}

/** Why a GraphQL `query` field is not a visible inline read query, or null. */
function queryReason(field) {
  const value = field.spec.slice(field.spec.indexOf("=") + 1);
  if (field.typed && value.startsWith("@")) return "a query read from a file or stdin";
  if (SHELL_VALUE.test(value)) return "a query the command text does not show";
  if (WRITE_OPERATION.test(value)) return "a mutation";
  if (!READ_QUERY.test(value)) return "a query that does not open with `query` or `{`";
  return null;
}

function isQueryField(field) {
  const eq = field.spec.indexOf("=");
  const key = (eq < 0 ? field.spec : field.spec.slice(0, eq)).toLowerCase().replace(/\[\]$/, "");
  return key === "query";
}

/** Why `gh api <args>` writes or cannot be shown not to, or null for a read. */
function apiWrite(args) {
  const api = parseApi(args);
  if (api.unknown) return `gh api with an option this guard does not know (${api.unknown})`;
  if (api.input) return "gh api --input: a request body from a file or stdin";
  const method = apiMethodReason(api) || apiHeaderReason(api);
  if (method) return method;
  if (api.endpoints.length > 1) return `gh api with ${api.endpoints.length} endpoints`;
  if (api.endpoints[0] !== "graphql") {
    // Fields make gh send a POST; under an explicit GET (every method is GET by now) they are query parameters.
    if (api.fields.length && !api.methods.length) {
      return "gh api with -f/-F fields and no explicit GET: request parameters make gh send a POST";
    }
    return null;
  }
  for (const field of api.fields.filter(isQueryField)) {
    const reason = queryReason(field);
    if (reason) return `gh api graphql with ${reason}`;
  }
  return null;
}

// --- invocation ---

/** Why one gh invocation (words from the program word on, redirections already dropped) writes, or null. */
function writeReason(tokens) {
  const args = tokens.slice(1);
  for (const unknownTakesValue of [false, true]) {
    const indexes = positionalIndexes(args, unknownTakesValue);
    if (!indexes.length) continue;
    const words = indexes.map((i) => args[i].toLowerCase());
    if (words[0] === "api" && args.includes(XARGS_TAIL)) {
      // xargs appends arguments the text does not show; `-f title=x` among them makes it a POST.
      return "gh api run by xargs: the arguments xargs appends can make it a write";
    }
    if (words[0] === "api") {
      // gh parses flags placed before `api` on `api` too: `gh -X POST api x` is a POST.
      const reason = apiWrite(args.filter((_, i) => i !== indexes[0]));
      if (reason) return reason;
    } else {
      const write = subcommandWrite(words) || runTimeSubcommand(words);
      if (write) return write;
    }
  }
  return null;
}

/** True when `command` names gh with `api` or a write verb, read with and without quote characters. */
function namesGhWrite(command) {
  return FALLBACK_WRITE.test(command) || FALLBACK_WRITE.test(command.replace(/["'\\]/g, ""));
}

/** The first gh write in a Bash command, or null; throws only if the lexer does. */
function findWrite(command) {
  const info = {};
  const invocations = programInvocations(command, "gh", info);
  if (invocations === null) {
    // No exact argv: quoting the lexer cannot follow, or interpreter code naming gh. Quoting the
    // shells read differently can hide the gh word itself, so it blocks without one.
    return namesGhWrite(command) || info.unreadable
      ? "gh-write-guard cannot read this command's gh arguments exactly (quoting it cannot follow or that bash and zsh read differently, or gh named in interpreter code); every such command naming gh with api or a write verb, and every command with quoting the shells read differently, is blocked"
      : null;
  }
  for (const tokens of invocations) {
    const reason = writeReason(tokens);
    if (reason) return reason;
  }
  return null;
}

function block(reason) {
  process.stderr.write(
    `gh write blocked — ${reason}.\n` +
      "GitHub is read-only for Claude and every agent; no allow rule or approval overrides this hook.\n" +
      "Draft the change (text, command, payload) for the user and let them run it.\n",
  );
  process.exit(2);
}

let raw = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => (raw += chunk));
process.stdin.on("end", () => {
  let data;
  try {
    data = JSON.parse(raw);
  } catch {
    process.exit(0); // nothing to inspect: normal permission handling
  }
  if (!data || data.tool_name !== "Bash") process.exit(0);
  const command = (data.tool_input && data.tool_input.command) || "";
  if (typeof command !== "string" || command === "") process.exit(0);

  let reason;
  try {
    if (programInvocations === null) throw new Error("lib/shell-git.js did not load");
    reason = findWrite(command);
  } catch (error) {
    if (!namesGhWrite(command)) process.exit(0);
    reason = `gh-write-guard cannot inspect the command (${error.message}); every command naming gh with api or a write verb is blocked until the plugin is reinstalled`;
  }
  if (reason) block(reason);
  process.exit(0);
});
