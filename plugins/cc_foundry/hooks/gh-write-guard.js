#!/usr/bin/env node
// gh-write-guard.js — PreToolUse hook (matcher: Bash)
//
// PURPOSE
//   GitHub reads run free; every GitHub write made through `gh` needs the user's explicit approval
//   of that exact command. The shipped allow lists pre-approve `Bash(gh api repos/*)` and
//   `Bash(gh api graphql:*)`, and both also match writes: `gh api repos/o/r/issues -f title=x` is a
//   POST, `gh api graphql -f query='mutation{…}'` a mutation. This hook blocks every gh write
//   (exit 2) unless the user approved exactly that command: a hook that exits 2 stops the call
//   before permission rules are evaluated, so the block holds even where an allow rule matches
//   (permissions reference, "Extend permissions with hooks"), and an exit 2 routes as `deny`, which
//   wins over another hook's `allow` (hooks reference: deny > defer > ask > allow). The approval is
//   the user's `Approve` to the `gh-write` question, a single-use token (lib/approval-grants.js
//   GH WRITE TOKEN); spending it prints a PreToolUse `allow`. Settings deny rules still apply after
//   a hook `allow` (hooks reference), so the plugins ship no gh write deny entries. Only
//   /foundry:setup removes the retired ones from user settings. This guard ships in Foundry only.
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
//        with an unreadable operation, or a document containing a mutation or subscription operation
//      * an unknown `gh api` option, or more than one endpoint
//   4. Everything else — reads, `gh pr checkout`, and unknown subcommands, gh aliases and
//      extensions — passes to normal permission handling, which prompts unless an allow matches.
//   5. Fail closed where no exact argv exists: the lexer cannot follow the quoting (unbalanced
//      quote, unterminated substitution, brace expansion past its cap, or quoting bash and zsh
//      read differently — `$"…"`, a `$'…'` escape other than the named, octal and `\xHH` ones,
//      `$$` before a quote, `{` or `(`, a single quote inside a double-quoted `${…}`, a `$` form outside the lexer's
//      admit set (`${~x}`, `$=x`, `${!x}`, `$[…]`, `${x:=…}`), a `{name}>` descriptor variable, a shell option
//      switched at run time; the Bash tool runs the user's shell), gh is named in
//      `python3 -c`/`perl -e`/`node -e` code, lib/shell-git.js cannot load, or classifying throws.
//      Then every command naming gh requests ordinary permission, read with and
//      without its quote and backslash characters. An unrelated unreadable command produces no guard verdict.
//      Size, word, invocation, command, time and nesting limits use this same permission fallback. A guessed argv cannot fail closed: stripping
//      quotes from `--title 'a b' create` shifts `create` off the verb position. When the lexer names
//      the construct (shell-git `info.unreadableReason`), the permission reason names it and offers no gh-write
//      token: a token never substitutes for permission to run unreadable text.
//   6. A write items 2–3 found in an exact argv runs once when the user approved exactly this
//      command: the session project's git common dir holds a valid `gh-write-once` token
//      (claude-gh-write-approval.json, 15 min) whose `command` equals the whole Bash command,
//      trimmed, whose `files_sha256` equals the digest of the files that command names as they are
//      now (a body file rewritten, created or removed since the approval is other content), the
//      session transcript (`transcript_path`) proves the user's fresh `Approve` to the token's own
//      question (lib/approval-grants.js TRANSCRIPT PROOF), and the call carries no `agent_id`. The
//      token is spent per tool call (`spendToken`: renamed to a claim keyed by `tool_use_id`, so
//      every loaded Foundry copy of this hook allows the same call and no other) and the hook prints
//      `permissionDecision: "allow"`. Other text, a changed file, an expired, unproven or already
//      spent token, another project's token, a spawned agent's call, a missing
//      lib/approval-grants.js or a write item 5 found exits 2 and keeps the token.
//   7. A force update to remote history exits 2 before any token is read, and no approval covers
//      it, the same as the git force-push ban: `gh repo sync --force`, `gh pr update-branch
//      --rebase`, a `gh api` field whose key has a `force` segment at any nesting (`force=1`,
//      `-F 'i[force]=true'`, `-F 'u[][force]=true'`, name case ignored) with a value other than a
//      literal false, `gh api --input` to a `git/refs` endpoint or to `graphql` (the body is not
//      shown), and a GraphQL `force:` argument or a query the command text does not show. A force
//      update anywhere in the command outranks its other writes.
//   8. PR body footer (rules/public-github.md §PR body footer): `gh pr create|new|edit` read from an
//      exact argv whose body does not end with the AI Rig footer line, in its Markdown or its plain
//      form (a U+FE0F variation selector ignored), exits 2 before any token is read, so no token is
//      spent and the approval is not consumed. The rule alone did not hold: after a `/compact` the
//      model forgot it and the host's default attribution line went out in its place. The body is
//      read as pflag hands flag values over: a table of the value flags of `pr create|edit` skips
//      each value even when it looks like a flag (`--title -Fok.md`), a flag outside the tables is
//      read both ways, and every body found is checked: `--body-file`/`-F` files, `--body`/`-b`
//      text, both kinds when both appear (the last flag of a kind wins). A file is read through a
//      non-blocking open; a blank body file, a larger file than the read window whose end is blank,
//      and a body path that is no regular file (named pipe, device, directory) all block.
//      `AI_RIG_FOOTER=off` (exactly, any case) in this hook's environment, the Claude Code
//      settings `env` block, switches the check off, as it does the rule; that settings values
//      reach hook processes is expected from the settings reference, not observed. Not checked,
//      accepted limits: no body flag (`--fill*`, `-T/--template`, `--web`, a title-only edit), a
//      blank inline `--body` (a deliberate clear), stdin (`-F -`, which the approval refuses), a body
//      file the hook cannot open by the literal path (absent, or named through a shell expansion
//      such as `~`, `$VAR` or a Windows Git Bash `/c/...` path: gh fails or reads what the hook
//      cannot see), and `gh api` pull request writes.
//      An unexpected failure of the check skips it and leaves the approval requirement in force.
//
// LIMITS (accepted)
//   * Text built at run time: a variable as the program (`$GH pr create`), a script on disk, and
//     the limits lib/shell-git.js lists for every program. The query field must be a literal shell
//     argument. Separate arguments may provide GraphQL variable values at run time.
//   * gh aliases and extensions (`gh alias set`, `gh <extension>`) are not resolved; they reach
//     the normal permission prompt, which no shipped allow entry pre-approves.
//   * `gh api graphql --input <file>` is never approvable (item 7): the body, query and variables
//     alike, is not in the command text and may carry `updateRef(input: {force: true})`; the force
//     ban is approval-proof (user directive "never any force to a remote"). The same mutation
//     written inline (`-f query='mutation …'`) stays approvable.
//   * A glob naming a subcommand or flag needs a planted file (`gh pr creat?`); globs are not read
//     as writes, so an unquoted `?` in a REST path (`repos/o/r/pulls?state=open`) stays a read.
//   * Other programs that reach the API (`curl`, `python -c 'requests.post(…)'`) are outside this
//     hook; the deny lists and the read-only rule cover what they can.
//   * A plugin mod that handles `tool.check` can approve a call a PreToolUse hook blocked, unless the
//     hook is in managed settings (permissions reference, "Extend permissions with hooks").
//   * Item 5 over-blocks: a read made from interpreter code, or a command the lexer cannot follow,
//     is blocked when its text names gh with `api` or a write verb anywhere. No token covers such a
//     command, even verbatim: the text the user approved and the argv the shell runs may differ.
//   * Outside a git repository there is no token location: every gh write stays blocked and the user
//     runs it. A token is spent when its call is allowed, even if another hook then blocks the call
//     or a gh write deny rule merged by an earlier setup still denies it (re-run the setup).
//   * No Node.js: each plugin's hooks.json runs this file with the first Node it finds and exits 0
//     when there is none, so the guard is inactive (fail open); the plugin's SessionStart diagnostic
//     reports "Node.js not found".
//
// EXIT CODES
//   0  no gh write found, the payload is not a Bash call, or no Node.js ran the hook (launcher exits
//      0, see LIMITS): normal permission handling continues. Or a gh write whose token this call
//      spent (item 6): stdout carries the PreToolUse `allow`.
//   2  a gh write without a matching token, or a command item 5 cannot show is not one: blocked;
//      stderr names the reason and the `gh-write` question to ask. A force update (item 7): blocked
//      with no question to ask. A PR body without the footer (item 8): blocked with the footer
//      to append; no approval is asked for or spent until the body carries it.

"use strict";

const fs = require("fs");
const path = require("path");

// Loaded defensively: a crash would exit 1, which Claude Code treats as non-blocking, letting the
// write through. Without the lexer, gh writes fail closed below.
let programInvocations = null;
let XARGS_TAIL = null;
let coarseProgramInvocations = null;
let inspectionNow = Date.now;
// Library unavailable: ask on every Bash command rather than guessing program names.
let namesProgram = () => true;
try {
  const lexer = require(path.join(__dirname, "lib", "shell-git.js"));
  ({ programInvocations, XARGS_TAIL, coarseProgramInvocations } = lexer);
  if (lexer.namesProgram) namesProgram = lexer.namesProgram;
  if (lexer.inspectionNow) inspectionNow = lexer.inspectionNow;
} catch {
  // programInvocations stays null
}

// Every gh subcommand that writes to GitHub (gh 2.102 command reference): group → verbs; a verb of
// two words names a third level. Aliases gh ships are listed beside their command (`pr new`).
const WRITES = {
  "agent-task": ["create"],
  cache: ["delete"],
  codespace: ["create", "delete", "edit", "ports visibility", "rebuild", "stop"],
  config: ["set"],
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

// `gh api` options (gh 2.104 `gh api --help`), by how they take a value.
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
const API_BOOL_LONG = new Set(["allow-escape-sequences", "help", "include", "paginate", "silent", "slurp", "verbose"]);
// The one method a read may name (case ignored): under it gh sends -f/-F fields as query parameters.
const READ_METHOD = "GET";
// Stick to GraphQL's ignored tokens, names, strings, numbers and punctuators. Strings are opaque to the guard.
const GRAPHQL_TOKEN =
  /([ \t\r\n,\uFEFF]+|#[^\r\n]*)|("""(?:\\"""|(?!""")[\s\S])*"""|"(?:\\[^\r\n]|[^"\\\r\n])*")|([_A-Za-z][_0-9A-Za-z]*|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|\.\.\.|[!$&():=@[\]{}|])/y;
// Headers that make a server read another method than the one sent (`-X GET -H '…: POST'`).
const METHOD_OVERRIDE_HEADERS = new Set(["x-http-method-override", "x-http-method", "x-method-override"]);
// A header name holding a shell expansion: the name arrives at run time.
const RUN_TIME_TEXT = /[$`]/;
// REST endpoints that move a ref (`repos/o/r/git/refs/heads/x`, `…/git/refs`), where a body may carry `force`.
const FORCE_REF_ENDPOINT = /(?:^|\/)git\/refs(?:\/|$)/i;

// The single-use approval scope this hook spends (lib/approval-grants.js SCOPES).
const GH_WRITE_SCOPE = "gh-write-once";
const SPENT_ALLOW = {
  hookSpecificOutput: {
    hookEventName: "PreToolUse",
    permissionDecision: "allow",
    permissionDecisionReason: "one-time gh-write approval spent",
  },
};
const UNREADABLE = "gh-write-guard cannot read this command's gh arguments exactly; permission is required";
// Why no approval covers a write found without an exact argv (HOW IT WORKS item 5).
const INEXACT =
  "a command whose gh arguments cannot be read exactly is never covered, since the text approved and the argv run may differ; rewrite it in plain quoting";

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

/** Record the value-taking option in a short cluster and return the last argument it used. */
function parseApiShort(args, i, record) {
  const arg = args[i];
  for (let k = 1; k < arg.length; k += 1) {
    if (API_BOOL_SHORT[arg[k]]) continue;
    const name = API_VALUE_SHORT[arg[k]];
    if (!name) return null;
    const attached = arg.slice(k + 1);
    if (attached === "") {
      record(name, args[i + 1] ?? "", i + 1);
      return i + 1;
    }
    record(name, k === 1 && attached.startsWith("=") ? attached.slice(1) : attached, i);
    break;
  }
  return i;
}

/** Parse gh's API options and retain the shell proof for the word that supplied each field. */
function parseApi(args, literals, apiIndex) {
  args = args.filter((_, i) => i !== apiIndex);
  literals = literals?.filter((_, i) => i !== apiIndex);
  const api = { endpoints: [], methods: [], headers: [], fields: [], input: false, unknown: null };
  const record = (name, value, i) => {
    if (name === "method") api.methods.push(value);
    else if (name === "header") api.headers.push(value);
    else if (name === "input") api.input = true;
    else if (name === "raw-field" || name === "field") {
      api.fields.push({ typed: name === "field", spec: value, literal: literals?.[i] === true });
    }
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
      if (eq >= 0) record(name, arg.slice(eq + 1), i);
      else record(name, args[++i] ?? "", i);
    } else if (arg.length > 1 && arg.startsWith("-")) {
      const end = parseApiShort(args, i, record);
      if (end === null) api.unknown ||= arg;
      else i = end;
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

/** Read the shown GraphQL document, keeping strings opaque and discarding only ignored tokens. */
function graphqlTokens(text) {
  const tokens = [];
  GRAPHQL_TOKEN.lastIndex = 0;
  while (GRAPHQL_TOKEN.lastIndex < text.length) {
    const match = GRAPHQL_TOKEN.exec(text);
    if (!match) return null;
    if (!match[1]) tokens.push(match[2] === undefined ? match[3] : '""');
  }
  return tokens;
}

/** Classify each definition at its start, without mistaking names inside a selection set for operations. */
function graphqlReadReason(tokens) {
  let depth = 0;
  let start = true;
  let query = false;
  for (const token of tokens) {
    if (start) {
      if (token === "mutation" || token === "subscription") return `a ${token}`;
      if (token === "query" || token === "{") query = true;
      else if (token !== "fragment") return "a query whose operation cannot be read";
      start = false;
    }
    if ("{[(".includes(token)) depth += 1;
    else if ("}])".includes(token)) depth -= 1;
    if (depth === 0 && token === "}") start = true;
  }
  return query && start ? null : "a query whose read operation cannot be established";
}

/** Read the GraphQL document only when the shell and gh both receive the shown text. */
function visibleQuery(field) {
  const value = field.spec.slice(field.spec.indexOf("=") + 1);
  if (field.typed && value.startsWith("@")) return "a query the command text does not show (file or stdin)";
  if (!field.literal) return "a query the command text does not show";
  return graphqlTokens(value) ?? "a query whose tokens cannot be read";
}

function isQueryField(field) {
  const eq = field.spec.indexOf("=");
  const key = (eq < 0 ? field.spec : field.spec.slice(0, eq)).toLowerCase().replace(/\[\]$/, "");
  return key === "query";
}

/** Why `gh api <args>` writes or cannot be shown not to, or null for a read. */
function apiWrite(api) {
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
    const tokens = visibleQuery(field);
    const reason = typeof tokens === "string" ? tokens : graphqlReadReason(tokens);
    if (reason) return `gh api graphql with ${reason}`;
  }
  return null;
}

// --- force updates ---

/**
 * True when a `-f`/`-F` field sets `force` to anything but a literal false, at any nesting: gh builds nested JSON
 * from `key[sub]` and `key[]` segments (`-F 'i[force]=true'` is `{"i": {"force": true}}`, `u[][force]` an element of
 * a list), and a GraphQL variable object or a REST body passes `force` through either way.
 */
function isForceField(field) {
  const eq = field.spec.indexOf("=");
  const key = eq < 0 ? field.spec : field.spec.slice(0, eq);
  const segments = key.split(/[[\]]/).map((segment) => segment.trim().toLowerCase());
  return segments.includes("force") && field.spec.slice(eq + 1).toLowerCase() !== "false";
}

/** Find a non-false force input inside an argument list, excluding variable declarations. */
function graphqlForceInput(tokens) {
  let argumentsDepth = 0;
  return tokens.some((token, i) => {
    if (token === "(") argumentsDepth += 1;
    if (token === ")") argumentsDepth -= 1;
    return (
      argumentsDepth > 0 &&
      token.toLowerCase() === "force" &&
      tokens[i - 1] !== "$" &&
      tokens[i + 1] === ":" &&
      tokens[i + 2] !== "false"
    );
  });
}

/** Why `gh api <args>` can force-update a ref (body shown or not), or null. */
function apiForce(api, coarse) {
  if (api.fields.some(isForceField)) return "gh api with a force field";
  if (api.input && api.endpoints.some((endpoint) => FORCE_REF_ENDPOINT.test(endpoint))) {
    return "gh api --input to a git ref endpoint: a body the command text does not show may force the update";
  }
  if (api.endpoints[0] !== "graphql") return null;
  if (api.input) return "gh api graphql --input: a body the command text does not show may force a ref update";
  if (coarse) return null; // A partial query from whitespace splitting cannot establish an actual force argument.
  for (const field of api.fields.filter(isQueryField)) {
    const tokens = visibleQuery(field);
    if (typeof tokens === "string") return `gh api graphql with ${tokens}: it may force a ref update`;
    if (graphqlReadReason(tokens) !== null && graphqlForceInput(tokens)) {
      return "gh api graphql with a force argument";
    }
  }
  return null;
}

/**
 * Why one gh invocation force-updates remote history, or null. No approval covers it: no force to a remote,
 * the same as the git force-push ban.
 */
function forceReason(tokens, literals, coarse = false) {
  const args = tokens.slice(1);
  for (const unknownTakesValue of [false, true]) {
    const indexes = positionalIndexes(args, unknownTakesValue);
    if (!indexes.length) continue;
    const words = indexes.map((i) => args[i].toLowerCase());
    const flags = args.filter((arg, i) => !indexes.includes(i) && arg.startsWith("--")).map((a) => a.toLowerCase());
    if (words[0] === "api") {
      const reason = apiForce(parseApi(args, literals?.slice(1), indexes[0]), coarse);
      if (reason) return reason;
    } else if (words[0] === "repo" && words[1] === "sync" && flags.some((flag) => /^--force(?:=|$)/.test(flag))) {
      return "gh repo sync --force";
    } else if (
      words[0] === "pr" &&
      words[1] === "update-branch" &&
      flags.some((flag) => /^--rebase(?:=|$)/.test(flag))
    ) {
      return "gh pr update-branch --rebase: rewrites the pull request branch";
    }
  }
  return null;
}

// --- PR body footer ---

// The two forms of the footer note rules/public-github.md §PR body footer ends a PR body with; a test pins both to
// that rule's text. A Markdown body takes the italic form, any other body the plain one.
const FOOTER_PLAIN =
  "🐰 Built with AI help from [AI Rig](https://github.com/Borda/AI-Rig), the open-source agent setup I use. If it saved you time, a ⭐ helps others find it.";
const FOOTER_MARKDOWN =
  "*🐰 Built with AI help from **[AI Rig](https://github.com/Borda/AI-Rig)**, the open-source agent setup I use. If it saved you time, a ⭐ helps others find it.*";
// An emoji variation selector (U+FE0F) after the rabbit or the star renders the same note, so comparing ignores it.
const comparable = (line) => line.replace(/️/g, "");
const FOOTER_LINES = new Set([FOOTER_MARKDOWN, FOOTER_PLAIN].map(comparable));
// Writes whose body the note ends; `new` is gh's alias of `create`.
const FOOTER_WRITE = /^gh pr (?:create|new|edit)$/;
// Flags of `gh pr create` and `gh pr edit` by how they take a value (gh 2.x command reference). pflag hands the next
// word to a value flag whatever it looks like, so a title shaped like `-Fok.md`, or a bare `--`, is no body flag.
const PR_VALUE_LONG = new Set([
  "add-assignee",
  "add-label",
  "add-project",
  "add-reviewer",
  "assignee",
  "base",
  "body",
  "body-file",
  "head",
  "label",
  "milestone",
  "project",
  "recover",
  "remove-assignee",
  "remove-label",
  "remove-project",
  "remove-reviewer",
  "repo",
  "reviewer",
  "template",
  "title",
]);
const PR_BOOL_LONG = new Set([
  "draft",
  "dry-run",
  "editor",
  "fill",
  "fill-first",
  "fill-verbose",
  "help",
  "no-maintainer-edit",
  "web",
]);
const PR_VALUE_SHORT = new Set(["a", "B", "b", "F", "H", "l", "m", "p", "r", "R", "T", "t"]);
const PR_BOOL_SHORT = new Set(["d", "e", "f", "h", "w"]);
// The footer is the last non-blank line, so only the end of a body file is read; a larger file whose end is blank
// counts as lacking the footer.
const FOOTER_BODY_BYTES = 262144;

/**
 * The bodies a `gh pr create|edit` argv passes under one reading of its flags, as `{file, text}` (each the last
 * value of its flag). `unknownTakesValue` picks how a flag outside the tables reads. A value flag takes the next word
 * whatever it looks like; a boolean short cluster may precede a value flag (`-dF f`); any other short flag takes the
 * rest of its word.
 */
function prBodyFlags(args, unknownTakesValue) {
  const found = {};
  for (let i = 0; i < args.length; i += 1) {
    const arg = args[i];
    if (arg === "--") break;
    if (arg.startsWith("--")) {
      const eq = arg.indexOf("=");
      const name = arg.slice(2, eq < 0 ? undefined : eq);
      const takesValue = PR_VALUE_LONG.has(name) || (!PR_BOOL_LONG.has(name) && unknownTakesValue);
      const value = eq >= 0 ? arg.slice(eq + 1) : takesValue ? (args[++i] ?? "") : "";
      if (name === "body-file") found.file = value;
      else if (name === "body") found.text = value;
    } else if (arg.length > 1 && arg.startsWith("-")) {
      for (let k = 1; k < arg.length; k += 1) {
        const flag = arg[k];
        if (PR_BOOL_SHORT.has(flag) || !(PR_VALUE_SHORT.has(flag) || unknownTakesValue)) {
          // pflag gives `-d=t` to the boolean as its value, so the rest of the word is no further flag
          if (arg[k + 1] === "=") break;
          continue;
        }
        const value = k + 1 < arg.length ? arg.slice(k + 1).replace(/^=(?=[\s\S])/, "") : (args[++i] ?? "");
        if (flag === "F") found.file = value;
        else if (flag === "b") found.text = value;
        break;
      }
    }
  }
  return found;
}

/**
 * Every distinct body a `gh pr create|edit` argv may pass, as `{flag: "file"|"text", value}`: both readings of an
 * unknown flag, and both body kinds when a command names both (gh's precedence between them is not relied on).
 */
function prBodySources(tokens) {
  const args = tokens.slice(1);
  const sources = new Map();
  for (const unknownTakesValue of [false, true]) {
    for (const [flag, value] of Object.entries(prBodyFlags(args, unknownTakesValue))) {
      sources.set(`${flag}\0${value}`, { flag, value });
    }
  }
  return [...sources.values()];
}

/**
 * The end of a regular body file as `{text, truncated}`; `{special: true}` for an existing path that is no regular
 * file (a named pipe, a device, a directory), whose content the hook cannot read but gh may; null when the file
 * cannot be opened or its path is not literal (absent, `-`, `$VAR`): gh then fails on it or reads what the hook
 * cannot see, and the check is skipped. O_NONBLOCK keeps a named pipe, even one swapped in after a check, from
 * blocking the open until the hook times out (a timeout does not block the call); the opened descriptor, not an
 * earlier stat, proves the file is regular.
 */
function readBodyEnd(file, cwd) {
  if (file === "-" || RUN_TIME_TEXT.test(file)) return null;
  const full = path.isAbsolute(file) ? file : `${cwd}${path.sep}${file}`;
  let fd = null;
  try {
    fd = fs.openSync(full, fs.constants.O_RDONLY | (fs.constants.O_NONBLOCK || 0));
    const stat = fs.fstatSync(fd);
    if (!stat.isFile()) return { special: true };
    const length = Math.min(stat.size, FOOTER_BODY_BYTES);
    const buffer = Buffer.alloc(length);
    fs.readSync(fd, buffer, 0, length, stat.size - length);
    return { text: buffer.toString("utf8"), truncated: stat.size > length };
  } catch {
    return null;
  } finally {
    try {
      if (fd !== null) fs.closeSync(fd);
    } catch {
      // nothing left to release; a throw here would crash the hook and let the call through
    }
  }
}

/** The last non-blank line of `text`, trimmed, or null when the text is blank. */
function lastLine(text) {
  const lines = text.split(/\r?\n/);
  for (let i = lines.length - 1; i >= 0; i -= 1) {
    if (lines[i].trim() !== "") return lines[i].trim();
  }
  return null;
}

/**
 * `{reason, inline}` for a `gh pr create|edit` body that lacks the footer note (`inline`: it came from `--body`), or
 * null when every body ends with it or none is checked (item 8). A blank inline `--body` is a deliberate clear and
 * passes; a blank body file is a draft that was never written, so it blocks, as does a blank end of a larger file.
 * A body file that is no regular file blocks too: its content cannot be checked.
 */
function footerProblem(found, cwd) {
  if (!found.tokens || !FOOTER_WRITE.test(found.reason)) return null;
  if ((process.env.AI_RIG_FOOTER || "").toLowerCase() === "off") return null;
  for (const { flag, value } of prBodySources(found.tokens)) {
    const body = flag === "text" ? { text: value, truncated: false } : readBodyEnd(value, cwd);
    if (body === null) continue;
    const named = flag === "file" ? value : "--body";
    if (body.special) {
      return {
        reason: `the pull request body file (${named}) is not a regular file, so its end cannot be checked for the AI Rig footer note`,
        inline: false,
      };
    }
    const last = lastLine(body.text);
    if (last === null ? flag === "text" && !body.truncated : FOOTER_LINES.has(comparable(last))) continue;
    return {
      reason: `the pull request body (${named}) does not end with the AI Rig footer note`,
      inline: flag === "text",
    };
  }
  return null;
}

// --- invocation ---

/** Why one gh invocation (words from the program word on, redirections already dropped) writes, or null. */
function writeReason(tokens, literals) {
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
      const reason = apiWrite(parseApi(args, literals?.slice(1), indexes[0]));
      if (reason) return reason;
    } else {
      const write = subcommandWrite(words) || runTimeSubcommand(words);
      if (write) return write;
    }
  }
  return null;
}

/** True when `command` names gh, even without an exact program or subcommand word. */
function namesGh(command) {
  return namesProgram(command, "gh");
}

/**
 * The first gh write in a Bash command, or null; throws only if the lexer does. `exact` when it was read from an
 * exact argv, the only kind a gh-write approval can cover.
 */
function findWrite(command, info) {
  const coarse = coarseProgramInvocations?.(command, "gh", 0, info.deadline) || [];
  info.coarseInvocations = coarse;
  for (const tokens of coarse) {
    const reason = forceReason(tokens, undefined, true);
    if (reason) return { reason, exact: true, force: true };
  }
  const parsed = programInvocations(command, "gh", info);
  const invocations = info.unreadable || parsed === null ? info.exactInvocations || [] : parsed;
  const { literalArguments } = info;
  // A force update anywhere in the command outranks every other write: no approval covers it.
  for (const tokens of [...invocations, ...(info.prefixInvocations || [])]) {
    const reason = forceReason(tokens, literalArguments.get(tokens), !literalArguments.has(tokens));
    if (reason) return { reason, exact: true, force: true };
  }
  for (const tokens of [...invocations, ...(info.unreadable || parsed === null ? coarse : [])]) {
    const reason = writeReason(tokens, literalArguments.get(tokens));
    if (reason) return { reason, exact: true, unreadable: info.unreadable || parsed === null, tokens };
  }
  if (parsed === null || info.unreadable)
    return namesGh(command) || info.unreadableExecutable
      ? { reason: info.unreadableReason || UNREADABLE, exact: false, syntax: true }
      : null;
  return null;
}

// --- approval ---

/** The approval-record library, or null when it cannot load: every gh write then stays blocked. */
function loadGrants() {
  try {
    return require(path.join(__dirname, "lib", "approval-grants.js"));
  } catch {
    return null;
  }
}

/**
 * Null after this call spent the user's gh-write approval of exactly this command, else why none applies.
 *
 * The token must hold this command and the digest of the files it names as they are now (a body file rewritten after
 * the approval is other content), and the session transcript must prove the user's answer (`spendToken`).
 */
function spendApproval(command, data) {
  const grants = loadGrants();
  if (!grants) return "gh-write-guard cannot load hooks/lib/approval-grants.js to read a gh-write approval";
  if (data.agent_id) return "a spawned agent never spends the user's gh-write approval";
  try {
    const approved = command.trim();
    // Text the writer never records has no digest; `command` then mismatches first and names the real reason.
    const files = grants.commandFileDigest(approved, data.cwd);
    const spent = grants.spendToken(GH_WRITE_SCOPE, {
      cwd: data.cwd,
      callId: data.tool_use_id,
      transcriptPath: data.transcript_path,
      expected: { command: approved, files_sha256: files.digest || null },
    });
    return spent.ok ? null : spent.reason;
  } catch (error) {
    return `the gh-write approval could not be read (${error.message})`;
  }
}

function block(reason, approvalProblem) {
  process.stderr.write(
    `gh write blocked — ${reason}.\n` +
      `No gh-write approval covers this call: ${approvalProblem}.\n` +
      "Every gh write needs the user's explicit approval of that exact command. Draft it, then invoke one " +
      "AskUserQuestion with header `gh-write` and options exactly `Approve` / `Decline`, whose question names the " +
      "exact command once as an inline code span (`gh …`; wrap it in double backticks when it holds a backtick; no " +
      "fenced block, one line, one plain gh command: no `;`, `&&`, `|`, redirection, `$`, backtick or glob outside " +
      "quotes — pass long text with --body-file and show that text in the question). The user's `Approve` records a " +
      "single-use token (claude-gh-write-approval.json in the git common dir, 15 min) for that command text and the " +
      "files it names as they are then; run it exactly as approved, as the whole Bash command, without changing those " +
      "files. Never create, copy or edit the token yourself. Outside a git repository no token can be recorded: give " +
      "the user the command to run.\n",
  );
  process.exit(2);
}

/**
 * Ask permission for a command naming gh that the lexer cannot read exactly (`reason` names the construct).
 * A gh-write token never substitutes for this ordinary permission decision.
 */
function blockSyntax(reason) {
  process.stdout.write(
    JSON.stringify({
      hookSpecificOutput: {
        hookEventName: "PreToolUse",
        permissionDecision: "ask",
        permissionDecisionReason: `gh-write-guard cannot read this command exactly: ${reason}; it may hold a push or GitHub write the guard could not read`,
      },
    }),
  );
  process.exit(0);
}

function blockForce(reason) {
  process.stderr.write(
    `gh force update blocked — ${reason}.\n` +
      "No approval covers a force update to a remote, the same as the git force-push ban: do not ask for one, " +
      "and never record a gh-write token for it. Update without force: a merge, or a new branch.\n",
  );
  process.exit(2);
}

/** Block a PR body without the footer note; no approval is asked for or spent until the body carries it. */
function blockFooter({ reason, inline }) {
  const how = inline
    ? "An approved command is one line and cannot hold the note's line break: write the body to a file that ends " +
      "with the note and pass it with --body-file"
    : "Append the note as the last line of the body";
  process.stderr.write(
    `gh write blocked — ${reason}.\n` +
      `Pull request bodies end with the AI Rig footer note (rules/public-github.md §PR body footer). ${how}, then ask ` +
      "for the gh-write approval again: an edited body file voids an earlier approval. A Markdown body ends with one " +
      `blank line, a \`---\` line, one blank line and this line:\n${FOOTER_MARKDOWN}\n` +
      "(a body that already ends with another footer set off by a rule takes the note under it after one blank line, " +
      `with no second \`---\`). A body that is not Markdown ends with one blank line and this line instead:\n${FOOTER_PLAIN}\n` +
      "The user opts out with AI_RIG_FOOTER=off in the env block of Claude Code settings; never set it yourself, and " +
      "never replace the note with the host's default attribution line.\n",
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
  if (!data || (data.tool_name !== "Bash" && data.tool_name !== "Monitor")) process.exit(0);
  const command = (data.tool_input && data.tool_input.command) || "";
  if (typeof command !== "string" || command === "") process.exit(0);

  let found;
  const info = { deadline: inspectionNow() + 1000 };
  try {
    if (programInvocations === null) throw new Error("lib/shell-git.js did not load");
    found = findWrite(command, info);
  } catch (error) {
    if (!namesGh(command)) process.exit(0);
    found = {
      reason: `gh-write-guard cannot inspect the command (${error.message}); permission is required`,
      exact: false,
      syntax: true,
    };
  }
  if (!found) process.exit(0);
  if (found.force) blockForce(found.reason);
  if (found.syntax) blockSyntax(found.reason);
  let noFooter = null;
  try {
    if (found.exact && !found.unreadable) noFooter = footerProblem(found, data.cwd || process.cwd());
  } catch {
    // An unexpected failure of this convenience check must not crash the hook (exit 1 lets the call through);
    // the approval below still applies.
  }
  if (noFooter) blockFooter(noFooter);
  const problem = found.exact ? spendApproval(command, data) : INEXACT;
  if (problem === null) {
    if (found.unreadable) blockSyntax(UNREADABLE);
    process.stdout.write(JSON.stringify(SPENT_ALLOW));
    process.exit(0);
  }
  block(found.reason, problem);
});
