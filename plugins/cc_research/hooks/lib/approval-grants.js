// approval-grants.js — scope table, record store, writer and self-grant guard for approval records
//
// PURPOSE
//   A user's answer to an approval question can stand for later operations: the local Git
//   grant (`git-approve` answered `Approve always`), a push token (`git-push` answered
//   `Approve`) and a gh write token (`gh-write` answered `Approve`). One scope table
//   (SCOPES) drives every part: question header and option labels, the answer that records,
//   what a record covers and never covers, the fields it must carry, single use and lifetime.
//   This module holds the table, the record store in the git common dir, the PostToolUse
//   writer, the single-use spend shared by every plugin copy (`spendToken`) and the PreToolUse
//   self-grant guard. hooks/approval-guard.js runs the writer and the guard; commit-guard.js
//   (foundry only) reads the local Git grant with `--check-grant`. Requiring it has no side
//   effect: no stdin listener, no exit. Mirrored as contract prose in codex-rig
//   shared/native-skill-contract.md §Approval records.
//
//   Every row is written (RECORD_BUILDERS). commit-guard.js reads the local Git grant, spends
//   the push token and wipes both tokens at SessionStart and `/clear`; hooks/gh-write-guard.js
//   spends the gh write token.
//
// PUSH TOKEN — scope `push-once`: one non-force push of the approved branch at this HEAD
//   The `git-push` question shows the user the push it asks about (the branch, target, what is
//   pushed, the intended command); the token binds the checkout and commit-guard.js admits only the
//   approved branch's spellings. `Approve` writes the approving call's id (`question_id`), the
//   current branch, HEAD and `expires_at` = `created_at` + 15 min; the question text, kept in
//   `question`, is the only record of the command and must name the branch (else nothing records).
//   A detached HEAD records nothing. One file: a later `Approve` replaces a pending token and `Deny`
//   removes it, so the latest push answer decides. commit-guard.js spends it (`spendToken`, claim +
//   transcript proof): after its force ban and a dry-run pass, one push of this branch at this HEAD
//   to a configured remote name — `git push [-u] [<remote>] [HEAD|<branch>][:<branch>]`, a
//   substitution printing the current branch, `git lfs push <remote> <branch>` — run from the
//   checkout itself, alone in its command; a delete, prune, `--all`, `--tags`, `--follow-tags`,
//   another ref, a URL/path/variable destination, `cd`/`pushd`/`popd`, `-C`/`GIT_DIR`, inline config
//   redirecting the push, send-pack/http-push/subtree push, a second push, a repeating construct or
//   git that moves HEAD beside it is blocked (the user runs it by hand where no approval can cover
//   it; commit-guard.js header decides). A force push is never allowed: the force ban runs before
//   any token lookup.
//
// GH WRITE TOKEN — scope `gh-write-once`: one run of the exact gh command the question named
//   The `gh-write` question names the command once, as an inline code span whose content starts
//   with `gh ` (a double-backtick span carries a command holding a backtick; CommonMark's one
//   space each side is stripped). `Approve` writes the approving call's id (`question_id`), that
//   command, trimmed, as `command`, the digest of every file it names as they are now
//   (`files_sha256`, see FILES) and `expires_at` = `created_at` + 15 min. Records nothing and says
//   why: no gh span, two of them, a span across lines, any run of three backticks or a `~~~`
//   fence; an invisible or control character anywhere in the question (C0/C1 controls, DEL, bidi
//   overrides and isolates, zero-width characters, BOM, soft hyphen, line separators; in the command
//   also a tab and any space but U+0020), since a terminal may hide it or redraw text over it; and a
//   command that is not one plain gh invocation whose every value the text shows — a shell operator,
//   redirection or subshell outside quotes (`;`, `&&`, `|`, `>`, `<`, `(`), a `$` or backtick
//   outside single quotes (variable, substitution, `$'…'`), a glob, brace, tilde, `^`, `#` or
//   word-leading `=` outside quotes, an unterminated quote, or a file operand reading stdin (`-`,
//   `@-`). What passes has one reading in bash and zsh (`plainCommandWords`). Latest answer wins:
//   a later `Approve` replaces a pending token, `Deny` removes it. gh-write-guard.js spends it
//   (`spendToken`) only for a gh write whose whole Bash command, trimmed, equals `command` and whose
//   files still digest to `files_sha256`.
//
// FILES — what a gh write reads from disk is bound to what the user approved
//   `commandFileDigest` takes every word that may name a file gh reads, over-inclusive with no
//   per-subcommand table: each operand, the value of a `--flag=value`, the rest of a short cluster
//   after `F`/`f`, the path after `@` (`-F body=@notes.md`) and before `#` (`asset.zip#label`). Each
//   is resolved from the payload `cwd` and recorded as its state — `sha256:<content>` for a regular
//   file (links followed), `directory`, `other`, `absent` or `unreadable:<code>` — and the sorted
//   states are digested. A file rewritten, created or removed after the approval changes the digest,
//   so the call is refused and the token kept. A word naming no file only binds its absence. Sizes
//   are read first: past 1 GiB of regular-file content in all (FILE_DIGEST_MAX_BYTES), or once a
//   file grows past it while hashed, there is no digest — the writer records nothing and the
//   spender refuses at once, since hashing past the hook timeout would let the call through (a
//   timed-out PreToolUse hook does not block, and shipped `gh api` allows would run it).
//
// SINGLE-USE SPEND — once per tool call, across plugin copies
//   foundry, oss, develop and research each run their own copy of a spending guard on the same
//   call, and hooks run in parallel: deleting the token would leave the later copies without it,
//   and their exit 2 (deny) would beat the first copy's allow. `spendToken` therefore renames the
//   token to `<file>.spent-<sha256(tool_use_id)>` — of concurrent renames exactly one succeeds —
//   and a copy that finds no token accepts the claim its own `tool_use_id` names, after the same
//   checks. Another call, even with the same command text, finds no claim of its own and is
//   refused. A token renamed between this copy's checks reads as unreadable, never a throw, so the
//   claim decides; `spendToken` never throws (a guard exiting 1 would let the call through). A
//   spend removes the scope's claims older than the token lifetime; `removeRecord` (SessionStart,
//   `/clear`) deletes a token with its claims.
//
// TRANSCRIPT PROOF — a token allows nothing the transcript does not show the user approving
//   Before the rename, `spendToken` reads the spending call's `transcript_path` tail-first: the
//   token's `question_id` must name an assistant `tool_use` block for AskUserQuestion whose input
//   holds a question of the row (header, option set, single-select) with exactly the token's
//   `question` text and no pre-filled answer (input or `wireToolInputs`), followed by the user line
//   whose `tool_result` block names that id, is no error, and whose top-level
//   `toolUseResult.answers[question]` is the recording answer, timestamped within the token
//   lifetime of now. A gh write token's question must also name its `command`, a push token's its
//   `branch`. A token approved by
//   an answer another claim already holds is refused (a copy of a spent token planted again). So a
//   record written by any means but the writer needs a forged transcript too; a missing, unreadable
//   or mismatching transcript refuses (fail closed) and keeps the token.
//   LIMITS: the self-grant guard blocks named writes to a session transcript (a redirect, a writer
//   command, an in-place edit, a file tool), but a write it cannot name (a run-time-built path, an
//   interpreter's own code, a path relative to an earlier `cd`, a glob outside the basename, a
//   symlinked directory, `>|`) together with a forged record defeats it; `removeRecord` wipes the
//   claims, so a re-planted copy of a spent token passes again within 15 min of its answer when the
//   session resumes on the same transcript. Directory contents a gh command uploads (`gh repo create
//   --source`) are bound only as `directory`.
//
// RECORD — family `approval-record`, version 1
//   <git common dir>/<row.file>, one file per scope, holding
//     {"version": 1, "scope": <row.id>, "created_at": <ISO>, "question": ..., "answer": ...,
//      ...the row's own fields}
//   Git never tracks or checks out its own directory, and linked worktrees share it, so one
//   record covers the whole project. A reader that does not know a record's version or scope
//   treats it as no record: copies of different releases fail closed rather than widen.
//   Revoke = delete the file. Outside a Git repository nothing is stored or read.
//
// WHICH COMMON DIR — the checkout's own, never one discovery was steered to
//   Writer and reader resolve it alike (`recordDirectory`): git runs as
//     git -c safe.bareRepository=explicit rev-parse --path-format=absolute
//         --is-bare-repository --show-toplevel --git-dir --git-common-dir
//   with GIT_DIR, GIT_COMMON_DIR, GIT_WORK_TREE, GIT_CEILING_DIRECTORIES,
//   GIT_DISCOVERY_ACROSS_FILESYSTEM and GIT_INDEX_FILE removed from its environment (a
//   command-line `-c` beats GIT_CONFIG_PARAMETERS / GIT_CONFIG_COUNT). Accepted only when git
//   exits 0 with exactly those four lines (git 2.31+ for `--path-format`), the repository is not
//   bare, it has a work tree, the common dir is named `.git`, and the work tree links back to
//   it: in the main worktree `<toplevel>/.git` is a directory equal to the git dir and the common
//   dir; in a linked worktree `<toplevel>/.git` is a file, the git dir is
//   `<common>/worktrees/<name>`, and its `gitdir` file names `<toplevel>/.git`. When
//   CLAUDE_PROJECT_DIR is set (hooks always get it), the common dir must also equal the session
//   project's, so a `cd` into another repository finds no record and records none. A tracked
//   bare-repository fixture, an untracked `.git` file pointing at another repository and an
//   environment override therefore supply nothing. Fails closed, asking every time: a
//   `--separate-git-dir` or submodule checkout (common dir not named `.git`), a symlinked `.git`
//   or common dir, a session started outside a git checkout, a cwd inside the git dir itself,
//   git older than 2.31.
//
// SEVERAL PLUGINS INSTALLED
//   foundry, oss, develop and research each ship and register this module's hooks
//   (bin/propagate_shared.py) and nothing coordinates them. The writer is atomic (a temp file
//   named after the answer's call id, created exclusively, then a rename in the same directory)
//   and idempotent (a valid record holding the same scope, question, answer and fields, its times
//   aside, is left unchanged and the copy stays silent), so four copies leave one valid record and
//   one "recorded" line: of copies racing on one answer only one creates the temp file, and a later
//   copy finds the answer recorded. A refusal is the same in every copy, so each copy reports it.
//   The guard is a pure function of the payload, so duplicate runs agree.
//
// SELF-GRANT GUARD — names, never contents
//   Bash: a record file (any `*{git,push,gh-write}-approval.json`: Claude's, Codex's
//   `codex-git-approval.json` and the former `git-approval.json`) blocks only when it is named as
//   a write target: the target of an output redirection (`>`, `>>`, `>|`, `&>`, `<>`), or an
//   operand of a segment whose command is not an exact read verb, `rm` (deleting a record only
//   lowers authority), `echo`/`printf`, or a git subcommand that never writes a path it is handed
//   (`status`, `ls-files`, `check-ignore`, `rm`, `add`, `commit`, `log`, `show`, `diff`, `grep`,
//   `rev-parse`) — or that runs with an output option (`--output…`) or an env assignment before
//   it. git's location options (`-C`, `--git-dir`, `--work-tree`, `GIT_DIR=`, `GIT_WORK_TREE=`)
//   and paging flags keep a read a read; `-c`, `--config-env` and `--exec-path=` can run a program
//   (`core.pager`, `diff.external`), so git behind them counts as a writer. A command the guard
//   cannot tell from a writer (an interpreter, an editor, a copy) counts as one. So a commit
//   message, a `grep` or an `echo` naming a record passes; `cp`, `tee`, `python3 -c` and a
//   redirect onto one are blocked. A record name counts in any directory (a staging copy later
//   moved into place), and so do each alternative of a brace word (`approva{l,}.json`: braces
//   expand whether or not the file exists) and a literal record basename below a glob directory;
//   a glob basename counts only when its directory is the common dir (`.git/*`,
//   `.git/claude-git-ap*`) — a glob reaches only files that exist — so `mv code* x/` and
//   `staging/[!x]laude-…` pass. Scope ids in the text never count.
//   The common dir named as a whole (`.git`, `.git/`, `$(git rev-parse --git-common-dir)`, or a
//   glob or brace word the shell can expand to `.git`: `.gi?`, `.g*t`, `.git*`, `[.]git`,
//   `.gi{t,}`) blocks only where the command writes: the destination of `cp`/`mv`/`install`/
//   `rsync` (last operand, `-t`/`--target-directory`, rsync's temp, backup and partial dirs; when
//   the last word is an option or follows one, every word), the extract dir of `tar`
//   (`-C`/`--directory`) or `unzip` (`-d`), a `git clone` destination, any operand of `ln` (a link
//   is an alias the name tests cannot follow), an output redirection target (a planted gitfile),
//   and any operand of another command that is not a read, delete or print (an interpreter,
//   `find -exec`, a wrapper such as `sudo` or `xargs`) or of git behind `-c` or a program-running
//   env assignment. So `cp -R .git /tmp/x`, `tar czf b.tgz .git`, `git clone --mirror .git
//   ../m.git`, `git -C .git rev-parse` and `GIT_DIR=.git git status` pass; `cp -R x/. .git/`,
//   `ln -s .gi? gd` and `tar -xf a.tar -C .git` are blocked. A segment naming
//   hooks/approval-guard.js or this module passes only when it reads, stages, lints or
//   `node --check`s it (node's `-c`, never `-C`, which runs the script). Segments split at
//   shell operators, except a backslash-escaped one (`find … \( … \) -exec … \;`); line
//   continuations are joined. Names are matched as the shell and the filesystem resolve them:
//   quotes, backslash escapes and `$'…'` decoded; NFKC + lowercase (`ſ` is `s`, as
//   case-insensitive volumes compare); Windows trailing dots/spaces, `::$DATA` and 8.3 names;
//   repeated `/` and `/./` collapsed before the whole-directory test (`.git//`, `.git/./`). The
//   operand of an exclusion option names what a command skips, never a target, and is left out:
//   `--exclude=`/`--exclude`/`--exclude-dir`/`--ignore=`/`--ignore`/`--ignore-glob` anywhere,
//   find's pattern tests (`-path`, `-name`, …, only while the find runs no
//   `-exec`/`-ok`/`-fprint*`/`-fls`/`-delete` action), tree's `-I`, zip/unzip's `-x` list; `du` is
//   a reader. So `find . -path ./.git -prune`, `tar --exclude=.git`, `pytest --ignore=.git`,
//   `fd --exclude .git`, `tree -I .git` and `du -sh .git` pass.
//   File tools (Write/Edit/MultiEdit/NotebookEdit): a target named like a record, like a token's
//   claim (`<record>.spent-<id>`) or like the writer's temp file (`.<record>.<pid>.<hex>.tmp`), and
//   a target whose basename is `.git` (a planted gitfile points git at another repository) are
//   blocked; other paths inside a `.git` directory (`info/exclude`, `COMMIT_EDITMSG`) pass.
//   Write/Edit content is never scanned, because this repository's own tests and docs quote the
//   scope ids and file names.
//   LIMITS: the guard sees tool-call names and command text, never file content. A record carried
//   in content — a patch (`patch -p0 < p.diff` passes: it names nothing), `git apply`, an
//   archive member, a script file, a name assembled at run time (variable built from parts such
//   as `.git/${f}-approval.json`, command substitution, a name a reader or `echo` prints into a
//   pipe, alias, git config, a relative path reached after `cd`), or a forged hook
//   payload plus transcript fed to the writer through a command that never names it — is beyond
//   string inspection. Approximations, each failing closed: a brace word past MAX_BRACE_EXPANSIONS
//   alternatives counts as naming both; a numeric brace sequence is tested by its two ends (its
//   digits reach a name only as an 8.3 `~N`); a `tar -C .git` that creates rather than extracts,
//   and any other command handed `.git` that the guard cannot tell from a writer
//   (`zip -r b.zip .git`, `chmod -R u+w .git`), are blocked. The guard stops a direct self-grant; beyond that, self-grant prevention is
//   prompt discipline, not an enforced boundary (rules/git-commit.md forbids creating, copying,
//   editing or restoring a record by any means). A `.git` file forged to look like a linked
//   worktree of another repository, with that repository's `worktrees/<name>/gitdir` pointing
//   back, passes the location checks when CLAUDE_PROJECT_DIR is unset; planting it is such a
//   content-borne write.
//
// NOT A RECORD — what the writer refuses (reason reported)
//   * an answer the model pre-filled in its own call: read from the assistant `tool_use` block
//     whose `id` equals the payload's `tool_use_id` in `transcript_path` (its `input`, and the
//     line's `wireToolInputs[id]`), never from PostToolUse `tool_input`, which the harness may
//     fill with the user's pick. No transcript path, no `tool_use_id`, an unreadable file or no
//     matching block within the newest TRANSCRIPT_MAX_BYTES → refused (fail closed).
//   * `agent_id` present: the question came from a spawned agent.
//   * question shape drift: header, option set or multiSelect not exactly the row's.
//   * a standing grant whose recording option description does not name the record file (the
//     revoke step the user must be told).
//   * a location the checks above reject, a record path that is a link or no regular file.

"use strict";

const crypto = require("crypto");
const fs = require("fs");
const path = require("path");
const { execFileSync } = require("child_process");

const RECORD_FAMILY = "approval-record";
const RECORD_VERSION = 1;
const COMMON_FIELDS = Object.freeze(["version", "scope", "created_at", "question", "answer"]);
// Record fields set from the writer's clock; two records differing only here hold the same answer.
const TIME_FIELDS = new Set(["created_at", "expires_at"]);
// Scope fields a reader compares as text: each must be a non-empty string.
const TEXT_FIELDS = new Set(["question_id", "branch", "head", "command", "files_sha256"]);
const PUSH_TOKEN_TTL_MS = 15 * 60 * 1000;
const GH_WRITE_TOKEN_TTL_MS = 15 * 60 * 1000;

/**
 * Freeze a scope row and the arrays it holds.
 *
 * @param {object} row Scope row.
 * @returns {object}
 */
function frozenRow(row) {
  for (const value of Object.values(row)) if (Array.isArray(value)) Object.freeze(value);
  return Object.freeze(row);
}

// The scope table. `options` are the exact labels the question offers; `recordAnswer` is the one
// label that records; `revokeAnswer` the label that removes a pending record (null: none does);
// `fields` are the record fields beyond COMMON_FIELDS; `discloseFile` requires the recording
// option's description to name `file` (the revoke step). Codex mirrors this table.
const SCOPES = Object.freeze([
  frozenRow({
    id: "local-git-non-destructive",
    header: "git-approve",
    options: ["Approve", "Approve always", "Deny"],
    recordAnswer: "Approve always",
    file: "claude-git-approval.json",
    // Every other local Git operation runs without a grant or question; only a plain commit needs authority.
    covers: ["plain commit"],
    neverCovers: ["push (asks every time)", "force operations"],
    revokeAnswer: null,
    fields: [],
    singleUse: false,
    ttlMs: null,
    discloseFile: true,
  }),
  frozenRow({
    id: "push-once",
    header: "git-push",
    options: ["Approve", "Deny"],
    recordAnswer: "Approve",
    file: "claude-push-approval.json",
    covers: ["one non-force push of the approved branch at the approved HEAD to a configured remote"],
    neverCovers: [
      "force push",
      "a second push",
      "another branch or HEAD",
      "a delete, prune, --all, --tags or mirror push",
      "a URL or path destination",
      "a push from another checkout or after cd/pushd",
      "a spawned agent's call",
    ],
    revokeAnswer: "Deny",
    fields: ["question_id", "branch", "head", "expires_at"],
    singleUse: true,
    ttlMs: PUSH_TOKEN_TTL_MS,
    discloseFile: false,
  }),
  frozenRow({
    id: "gh-write-once",
    header: "gh-write",
    options: ["Approve", "Deny"],
    recordAnswer: "Approve",
    file: "claude-gh-write-approval.json",
    covers: [
      "one run of the exact gh command the question named — one gh invocation, no shell operator, expansion, glob " +
        "or redirection — as the whole Bash command, with every file it names unchanged",
    ],
    neverCovers: [
      "any other command text",
      "a second run",
      "a changed, new or removed file the command names",
      "a spawned agent's call",
      "another project",
    ],
    revokeAnswer: "Deny",
    fields: ["question_id", "command", "files_sha256", "expires_at"],
    singleUse: true,
    ttlMs: GH_WRITE_TOKEN_TTL_MS,
    discloseFile: false,
  }),
]);

// Scope-specific fields the writer adds, per scope. Each builder returns `{fields}`, or `{reason}` when the answer
// cannot record (reported as a refusal). Called after the location checks, so git runs in a verified checkout.
const RECORD_BUILDERS = Object.freeze({
  "local-git-non-destructive": () => ({ fields: {} }),
  "push-once": pushTokenFields,
  "gh-write-once": ghWriteTokenFields,
});

const GIT_TIMEOUT_MS = 5000;
// Environment variables that steer git's repository discovery; removed before resolving.
const DISCOVERY_ENV = Object.freeze([
  "GIT_DIR",
  "GIT_COMMON_DIR",
  "GIT_WORK_TREE",
  "GIT_CEILING_DIRECTORIES",
  "GIT_DISCOVERY_ACROSS_FILESYSTEM",
  "GIT_INDEX_FILE",
]);
const GIT_LOCATION_ARGS = Object.freeze([
  "-c",
  "safe.bareRepository=explicit",
  "rev-parse",
  "--path-format=absolute",
  "--is-bare-repository",
  "--show-toplevel",
  "--git-dir",
  "--git-common-dir",
]);
// A record is a few hundred bytes; anything far larger is not one and is not read.
const RECORD_MAX_BYTES = 64 * 1024;
// `created_at` / `expires_at` as ISO 8601, so neither can smuggle text into a record line.
const ISO_TIMESTAMP = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})$/;
// Transcript read tail-first in chunks: the question's tool_use line sits near the end, while a
// long session's transcript runs to tens of MB.
const TRANSCRIPT_CHUNK_BYTES = 256 * 1024;
const TRANSCRIPT_MAX_BYTES = 16 * 1024 * 1024;
const NEWLINE = 0x0a;
// Real paths compare in on-disk case (macOS, Windows), so one directory spelled two ways agrees.
const realPath = fs.realpathSync.native;
// A full object id as `git rev-parse` prints it: SHA-1 or SHA-256.
const COMMIT_ID = /^[0-9a-f]{40}(?:[0-9a-f]{24})?$/;

/**
 * The scope row with this id, or null.
 *
 * @param {string} scopeId Scope id.
 * @returns {object|null}
 */
function scopeById(scopeId) {
  return SCOPES.find((row) => row.id === scopeId) || null;
}

/**
 * Label a record line and messages use; `.git/` stands for the git common dir.
 *
 * @param {object} row Scope row.
 * @returns {string}
 */
function recordLabel(row) {
  return `.git/${row.file}`;
}

/**
 * The command that deletes a scope's record, revoking it in every worktree.
 *
 * @param {object} row Scope row.
 * @returns {string}
 */
function revokeCommand(row) {
  return `rm "$(git rev-parse --git-common-dir)/${row.file}"`;
}

// ── Which common dir ──────────────────────────────────────────────────────────

/**
 * `env` without the variables that steer git's repository discovery.
 *
 * Names compare upper-cased: Windows environment names are case-insensitive.
 *
 * @param {object} env Environment.
 * @returns {object}
 */
function discoveryEnv(env) {
  const clean = {};
  for (const [key, value] of Object.entries(env || {})) {
    if (!DISCOVERY_ENV.includes(key.toUpperCase())) clean[key] = value;
  }
  return clean;
}

/**
 * Why the work tree does not link back to the git dirs git printed, or null.
 *
 * @param {{toplevel: string, gitDir: string, commonDir: string}} found Real paths from git.
 * @returns {string|null}
 */
function linkProblem({ toplevel, gitDir, commonDir }) {
  const dotGit = path.join(toplevel, ".git");
  let stat;
  try {
    stat = fs.lstatSync(dotGit);
  } catch (_) {
    return "the work tree has no .git entry";
  }
  if (stat.isDirectory()) {
    return realPath(dotGit) === commonDir && gitDir === commonDir
      ? null
      : "the work tree's .git directory is not the git common directory";
  }
  if (!stat.isFile()) return "the work tree's .git is a link, neither a directory nor a file";
  if (path.dirname(gitDir) !== path.join(commonDir, "worktrees")) {
    return "the work tree's .git file names no linked worktree of its common directory";
  }
  let back;
  try {
    back = fs.readFileSync(path.join(gitDir, "gitdir"), "utf8").trim();
    return realPath(path.resolve(gitDir, back)) === realPath(dotGit)
      ? null
      : "the linked worktree's gitdir does not name this work tree";
  } catch (_) {
    return "the linked worktree's gitdir file is unreadable";
  }
}

/**
 * The verified git location of `cwd`, or a reason it has none.
 *
 * @param {string} cwd Directory to resolve from.
 * @param {object} env Environment; discovery variables are removed before git runs.
 * @returns {{toplevel: string, commonDir: string}|{reason: string}} Real paths.
 */
function gitLocation(cwd, env) {
  let out;
  try {
    out = execFileSync("git", GIT_LOCATION_ARGS, {
      cwd,
      env: discoveryEnv(env),
      encoding: "utf8",
      stdio: ["ignore", "pipe", "ignore"],
      timeout: GIT_TIMEOUT_MS,
    });
  } catch (_) {
    return { reason: "not a git work tree (no repository, a bare repository, or a directory inside .git)" };
  }
  const lines = out.split(/\r?\n/);
  if (lines[lines.length - 1] === "") lines.pop();
  if (lines.length !== 4) return { reason: "unexpected git rev-parse output (git 2.31 or later is required)" };
  const [bare, toplevel, gitDir, commonDir] = lines;
  if (bare !== "false") return { reason: "a bare repository" };
  if (path.basename(commonDir) !== ".git") return { reason: "the git common directory is not named .git" };
  let found;
  try {
    found = { toplevel: realPath(toplevel), gitDir: realPath(gitDir), commonDir: realPath(commonDir) };
  } catch (_) {
    return { reason: "the git directories are unreadable" };
  }
  const problem = linkProblem(found);
  return problem ? { reason: problem } : { toplevel: found.toplevel, commonDir: found.commonDir };
}

/**
 * The git common dir that holds records for `cwd`, bound to the session project, or a reason.
 *
 * @param {string|undefined} cwd Session directory (hook payload `cwd`, or the process cwd).
 * @param {object} [env] Environment; CLAUDE_PROJECT_DIR binds the result to that project.
 * @returns {{dir: string}|{reason: string}}
 */
function recordDirectory(cwd, env = process.env) {
  const here = gitLocation(cwd || process.cwd(), env);
  if (here.reason) return here;
  const project = env && env.CLAUDE_PROJECT_DIR;
  if (project) {
    const home = gitLocation(project, env);
    if (home.reason) return { reason: `the session project directory resolves to no usable checkout: ${home.reason}` };
    if (home.commonDir !== here.commonDir) {
      return { reason: "the git common directory is not the session project's" };
    }
  }
  return { dir: here.commonDir };
}

/**
 * The branch and HEAD commit of the checkout at `cwd`, or a reason it has none.
 *
 * Runs with git's discovery variables removed, like `gitLocation`; callers resolve the location first.
 *
 * @param {string|undefined} cwd Session directory.
 * @param {object} [env] Environment.
 * @returns {{branch: string, head: string}|{reason: string}}
 */
function gitHead(cwd, env = process.env) {
  const git = (args) =>
    execFileSync("git", args, {
      cwd: cwd || process.cwd(),
      env: discoveryEnv(env),
      encoding: "utf8",
      stdio: ["ignore", "pipe", "ignore"],
      timeout: GIT_TIMEOUT_MS,
    }).trim();
  let head;
  let branch;
  try {
    head = git(["rev-parse", "--verify", "--quiet", "HEAD"]);
  } catch (_) {
    return { reason: "HEAD names no commit" };
  }
  try {
    branch = git(["symbolic-ref", "--quiet", "--short", "HEAD"]);
  } catch (_) {
    return { reason: "HEAD is detached; a push is approved on a named branch" };
  }
  return COMMIT_ID.test(head) && branch ? { branch, head } : { reason: "git printed no branch or commit id" };
}

// ── Push token ────────────────────────────────────────────────────────────────

/**
 * The push token's own fields for an approved `git-push` question (a RECORD_BUILDERS entry).
 *
 * The token binds the checkout the push runs from — its branch and HEAD — never the command's spelling; the question
 * text the user saw is kept in the common `question` field and must name that branch (the rule's question shows it;
 * no HEAD sha is required there).
 *
 * @param {object} data Parsed hook payload.
 * @param {{question: string}} verdict The write verdict, holding the question text the user answered.
 * @param {{now: Date, env: object}} options Creation time and environment.
 * @returns {{fields: object}|{reason: string}}
 */
function pushTokenFields(data, verdict, { now, env }) {
  const here = gitHead(data.cwd, env);
  if (here.reason) return here;
  if (!String(verdict.question).includes(here.branch)) {
    return {
      reason: `the question does not name branch ${here.branch}; the git-push question shows the branch it pushes`,
    };
  }
  const expires = new Date(now.getTime() + PUSH_TOKEN_TTL_MS).toISOString();
  return { fields: { question_id: data.tool_use_id, branch: here.branch, head: here.head, expires_at: expires } };
}

// ── gh write token ────────────────────────────────────────────────────────────

// A run of three backticks anywhere, or a `~~~` fence line: the question shows a block, not one inline command.
const FENCE = /`{3}|^ {0,3}~{3}/m;
// Inline code content that names a gh command.
const GH_COMMAND = /^gh /;
// Text a terminal may hide or redraw over: controls (C0 but newline and tab, DEL, C1), format characters (bidi
// overrides and isolates, zero-width characters, BOM, soft hyphen) and line/paragraph separators. Anywhere in the
// question, the command the user read could differ from the text the token runs.
const HIDDEN_IN_QUESTION = /(?![\n\t])[\p{Cc}\p{Cf}\p{Zl}\p{Zp}]/u;
// Inside the command also a newline, a tab and every space but U+0020: the shell splits words on none of those.
const HIDDEN_IN_COMMAND = /[\p{Cc}\p{Cf}\p{Z}](?<! )/u;
const HIDDEN_REASON =
  "the question holds an invisible or control character (a terminal may hide it or redraw text over it); write " +
  "the question and the command in plain text";
// Outside quotes in the approved command: characters that start another command, a redirection or a subshell, and
// characters the shells expand (globs, tilde, zsh's extended-glob `^`/`#`, a comment). A brace expands only around a
// `,` or `..` (`{a,b}`, `{1..3}`); gh's own `{owner}`/`{repo}` placeholders stay literal in both shells.
const SHELL_OPERATOR = /[;&|<>()\n]/;
const SHELL_EXPANSION = /[*?[~^#]/;
// An unquoted brace group up to its `}` within the word holding a `,` or `..`, or a quote before the `}`.
const BRACE_EXPANSION = /^\{[^\s}]*?(?:,|\.\.|['"\\])/;
const EXPANSION_REASON = "a glob, brace, tilde or comment character outside quotes";
// Name characters of the approved command a double-quoted backslash escapes (bash and zsh agree on these).
const DOUBLE_QUOTE_ESCAPES = new Set(["$", "`", '"', "\\"]);
// A file operand reading standard input: `-` itself, or a `-F key=@-` field.
const STDIN_OPERAND = "-";
// Errors meaning no file gh could read exists at a path; any other error is recorded as `unreadable:<code>`.
const ABSENT_CODES = new Set(["ENOENT", "ENOTDIR", "ENAMETOOLONG"]);
const FILE_HASH_CHUNK_BYTES = 1024 * 1024;
// Content one gh write may bind: hashing stays far inside the hook timeout (a sparse 50 GB file is free to create).
const FILE_DIGEST_MAX_BYTES = 1024 * 1024 * 1024;
const TOO_LARGE_REASON =
  "the files the approved command names are too large to bind (over 1 GiB in all); give the user the command to run";

/**
 * True when the character at `index` is escaped by an odd run of backslashes before it.
 *
 * @param {string} text Markdown text.
 * @param {number} index Character index.
 * @returns {boolean}
 */
function escapedAt(text, index) {
  let count = 0;
  for (let k = index - 1; k >= 0 && text[k] === "\\"; k--) count += 1;
  return count % 2 === 1;
}

/**
 * Index of the backtick run of exactly `length` that closes a code span opened before `from`, or -1.
 *
 * @param {string} text Markdown text.
 * @param {number} from Index after the opening run.
 * @param {number} length Opening run length.
 * @returns {number}
 */
function closingRun(text, from, length) {
  let search = from;
  for (;;) {
    const start = text.indexOf("`", search);
    if (start < 0) return -1;
    let stop = start;
    while (text[stop] === "`") stop += 1;
    if (stop - start === length) return start;
    search = stop;
  }
}

/**
 * The inline code spans of Markdown text, read as CommonMark reads them.
 *
 * A span opens on a backtick run (a backslash-escaped first backtick is literal) and closes on the next run of exactly
 * that length, so a double-backtick span carries a single backtick; an opener without a closer is literal text. One
 * space is stripped from each side when both are present and the content is not all spaces.
 *
 * @param {string} text Markdown text.
 * @returns {Array<{content: string, multiline: boolean}>} `multiline` when the span crosses a line end.
 */
function inlineCodeSpans(text) {
  const spans = [];
  let index = 0;
  for (;;) {
    let open = text.indexOf("`", index);
    if (open < 0) return spans;
    if (escapedAt(text, open)) open += 1;
    let end = open;
    while (text[end] === "`") end += 1;
    const close = end > open ? closingRun(text, end, end - open) : -1;
    if (close < 0) {
      index = Math.max(end, open + 1);
      continue;
    }
    const raw = text.slice(end, close);
    const flat = raw.replace(/\r?\n/g, " ");
    const strip = flat.length >= 2 && flat.startsWith(" ") && flat.endsWith(" ") && flat.trim() !== "";
    spans.push({ content: strip ? flat.slice(1, -1) : flat, multiline: flat !== raw });
    index = close + (end - open);
  }
}

/**
 * The gh command a `gh-write` question names, or why it names none it can approve.
 *
 * @param {string} question Question text the user answered.
 * @returns {{command: string}|{reason: string}}
 */
function questionGhCommand(question) {
  if (HIDDEN_IN_QUESTION.test(question)) return { reason: HIDDEN_REASON };
  if (FENCE.test(question)) {
    return { reason: "the question shows a fenced block; name the one command in an inline code span" };
  }
  const named = inlineCodeSpans(question).filter((span) => GH_COMMAND.test(span.content));
  if (named.length === 0) return { reason: "the question names no gh command in an inline code span (`gh …`)" };
  if (named.length > 1) return { reason: "the question names more than one gh command; ask about one command" };
  if (named[0].multiline) {
    return {
      reason: "the gh command spans lines; a Bash command can only match one line (pass a body with --body-file)",
    };
  }
  const command = named[0].content.trim();
  if (HIDDEN_IN_COMMAND.test(command)) return { reason: HIDDEN_REASON };
  const words = plainCommandWords(command);
  return words.reason ? words : { command };
}

/**
 * The words of an approved gh command, or why it is not one plain command whose every value the text shows.
 *
 * A strict subset of shell syntax that bash and zsh read alike: words split at spaces, single quotes literal, double
 * quotes with backslash escapes of `$`, backtick, `"` and `\`, a backslash outside quotes escaping the next character.
 * Refused: any shell operator, redirection or subshell outside quotes (a second command, a file fed to stdin), any `$`
 * or backtick outside single quotes (a variable, substitution or `$'…'` decoding), and any glob, brace, tilde, `^`
 * or `#` outside quotes (an expansion, or zsh's extended glob, or a comment). What is left has exactly one reading.
 *
 * @param {string} command Command text, trimmed.
 * @returns {{words: string[]}|{reason: string}}
 */
function plainCommandWords(command) {
  const words = [];
  let word = null;
  let quote = null;
  for (let i = 0; i < command.length; i += 1) {
    const c = command[i];
    // An escaped character is consumed with its backslash below and never reaches this check.
    const braces = quote === null && c === "{" && BRACE_EXPANSION.test(command.slice(i));
    const problem = quote === "'" ? null : braces ? EXPANSION_REASON : plainCharProblem(c, quote, word === null);
    if (problem) {
      return {
        reason: `the approved command holds ${problem}; the approved text must be one gh command whose every value it shows`,
      };
    }
    if (quote === "'") {
      if (c === "'") quote = null;
      else word += c;
    } else if (quote === '"') {
      if (c === '"') quote = null;
      else if (c === "\\" && DOUBLE_QUOTE_ESCAPES.has(command[i + 1])) word += command[(i += 1)];
      else word += c;
    } else if (c === " ") {
      if (word !== null) words.push(word);
      word = null;
    } else if (c === "'" || c === '"') {
      quote = c;
      word = word ?? "";
    } else if (c === "\\") {
      if (i + 1 >= command.length) return { reason: "the approved command ends in a backslash" };
      word = (word ?? "") + command[(i += 1)];
    } else {
      word = (word ?? "") + c;
    }
  }
  if (quote) return { reason: "the approved command holds an unterminated quote" };
  if (word !== null) words.push(word);
  return { words };
}

/**
 * What an unescaped character outside single quotes adds to the command, or null when it is literal text.
 *
 * @param {string} c The character.
 * @param {string|null} quote `"` inside double quotes, null outside quotes.
 * @param {boolean} wordStart True when no word is open (zsh expands a leading `=`).
 * @returns {string|null}
 */
function plainCharProblem(c, quote, wordStart) {
  if (c === "$" || c === "`") return "a variable or substitution (`$`, a backtick)";
  if (quote !== null) return null;
  if (SHELL_OPERATOR.test(c)) return "a shell operator, redirection or subshell";
  if (SHELL_EXPANSION.test(c) || (wordStart && c === "=")) return EXPANSION_REASON;
  return null;
}

/**
 * The words of `args` that may name a file gh reads, over-inclusive: every operand, the value of a `--flag=value`,
 * the rest of a short cluster after `F`/`f` (`-Fbody.md`), the path after `@` in a `key=@path` field and the path
 * before `#` in a release asset `path#label`.
 *
 * A word that names no file costs one `stat` and binds its absence; a missed file operand would leave its content
 * unbound, so no per-subcommand table decides.
 *
 * @param {string[]} args Words after `gh`.
 * @returns {string[]}
 */
function fileCandidates(args) {
  const found = new Set();
  let operandsOnly = false;
  for (const arg of args) {
    const values = [];
    if (operandsOnly || !arg.startsWith("-") || arg === STDIN_OPERAND) values.push(arg);
    else if (arg === "--") operandsOnly = true;
    else if (arg.startsWith("--")) {
      if (arg.includes("=")) values.push(arg.slice(arg.indexOf("=") + 1));
    } else {
      const at = arg.slice(1).search(/[Ff]/);
      if (at >= 0) values.push(arg.slice(at + 2).replace(/^=/, ""));
    }
    for (const value of values) {
      found.add(value);
      if (value.includes("@")) found.add(value.slice(value.indexOf("@") + 1));
      if (value.includes("#")) found.add(value.slice(0, value.indexOf("#")));
    }
  }
  found.delete("");
  return [...found];
}

/**
 * The sha256 of a regular file's content, read in chunks (a release asset may be large), within a byte budget.
 *
 * @param {string} file Path.
 * @param {{left: number}} budget Bytes still allowed to be read for this command; decremented as content is read.
 * @returns {string|null} Hex digest, or null once the content runs past the budget.
 */
function fileSha256(file, budget) {
  const hash = crypto.createHash("sha256");
  const buffer = Buffer.alloc(FILE_HASH_CHUNK_BYTES);
  const fd = fs.openSync(file, "r");
  try {
    let read = fs.readSync(fd, buffer, 0, buffer.length, null);
    while (read > 0) {
      budget.left -= read;
      // A file grown since its stat: stop at the cap rather than read on.
      if (budget.left < 0) return null;
      hash.update(buffer.subarray(0, read));
      read = fs.readSync(fd, buffer, 0, buffer.length, null);
    }
  } finally {
    fs.closeSync(fd);
  }
  return hash.digest("hex");
}

/**
 * What a candidate path names now, resolved from `cwd`, before any content is read: its `stat` for a file or
 * directory, else the state string (`absent`, `unreadable:<code>`).
 *
 * @param {string} cwd Directory the command runs in.
 * @param {string} candidate Path as the command names it.
 * @returns {{full: string, stat: fs.Stats}|{state: string}}
 */
function fileStat(cwd, candidate) {
  const full = path.resolve(cwd, candidate);
  try {
    return { full, stat: fs.statSync(full) };
  } catch (err) {
    return { state: ABSENT_CODES.has(err.code) ? "absent" : `unreadable:${err.code || "error"}` };
  }
}

/**
 * The state of one statted candidate: `sha256:<hex>` for a regular file (links followed), `directory`, `other`, or
 * `unreadable:<code>`; null when its content runs past the remaining byte budget.
 *
 * @param {{full: string, stat: fs.Stats}|{state: string}} found From `fileStat`.
 * @param {{left: number}} budget Bytes of content still allowed to be read for this command.
 * @returns {string|null}
 */
function fileState(found, budget) {
  if (found.state) return found.state;
  if (!found.stat.isFile()) return found.stat.isDirectory() ? "directory" : "other";
  try {
    const hex = fileSha256(found.full, budget);
    return hex === null ? null : `sha256:${hex}`;
  } catch (err) {
    return `unreadable:${err.code || "error"}`;
  }
}

/**
 * The digest of every file an approved gh command may read, as those files are now, or why it has none.
 *
 * The writer records it when the user approves (`files_sha256`) and the spender recomputes it for the call: a file
 * rewritten, created or removed in between changes it. Both run `cwd` relative paths from their payload's `cwd`.
 *
 * @param {string} command Approved command text (one plain gh command).
 * @param {string|undefined} cwd Directory the command runs in.
 * @returns {{digest: string}|{reason: string}}
 */
function commandFileDigest(command, cwd) {
  const text = String(command).trim();
  if (HIDDEN_IN_COMMAND.test(text)) return { reason: HIDDEN_REASON };
  const words = plainCommandWords(text);
  if (words.reason) return words;
  const candidates = fileCandidates(words.words.slice(1));
  if (candidates.includes(STDIN_OPERAND)) {
    return { reason: "the approved command reads standard input, which the question cannot show; pass a file" };
  }
  const base = cwd || process.cwd();
  const found = candidates.map((candidate) => [candidate, fileStat(base, candidate)]);
  // Sizes first, nothing read yet: hashing past the hook timeout would let the call through (a timed-out
  // PreToolUse hook does not block), so an oversized command is refused before its first byte.
  const bytes = found.reduce((sum, [, entry]) => sum + (entry.stat && entry.stat.isFile() ? entry.stat.size : 0), 0);
  const budget = { left: FILE_DIGEST_MAX_BYTES };
  const lines = bytes > budget.left ? null : found.map(([candidate, entry]) => [candidate, fileState(entry, budget)]);
  if (lines === null || lines.some(([, state]) => state === null)) return { reason: TOO_LARGE_REASON };
  const states = lines.map(([candidate, state]) => `${candidate}\0${state}\n`).sort();
  return { digest: crypto.createHash("sha256").update(states.join("")).digest("hex") };
}

/**
 * The gh write token's own fields for an approved `gh-write` question (a RECORD_BUILDERS entry).
 *
 * @param {object} data Parsed hook payload: `tool_use_id` names the approving call, `cwd` resolves file operands.
 * @param {{question: string}} verdict The write verdict, holding the question text the user answered.
 * @param {{now: Date}} options Creation time.
 * @returns {{fields: object}|{reason: string}}
 */
function ghWriteTokenFields(data, verdict, { now }) {
  const named = questionGhCommand(verdict.question);
  if (named.reason) return named;
  const files = commandFileDigest(named.command, data.cwd);
  if (files.reason) return files;
  const expires = new Date(now.getTime() + GH_WRITE_TOKEN_TTL_MS).toISOString();
  return {
    fields: { question_id: data.tool_use_id, command: named.command, files_sha256: files.digest, expires_at: expires },
  };
}

// ── Records ───────────────────────────────────────────────────────────────────

/**
 * Why `name` in common dir `dir` cannot hold (or receive) a record, or null.
 *
 * Checked with lstat, never following a link: the common dir must be a real directory, and the
 * record — when present — a regular file, not a link, no larger than a record, whose on-disk name
 * is exactly `name` (a self-grant spelled with other letter case keeps that spelling on a
 * case-insensitive volume) and whose real path sits directly inside the real common dir.
 *
 * @param {string} dir Absolute git common dir.
 * @param {object} row Scope row.
 * @param {boolean} requireFile Reader mode: a missing file is itself the problem.
 * @returns {string|null}
 */
function locationProblem(dir, row, requireFile) {
  const label = recordLabel(row);
  const file = path.join(dir, row.file);
  let dirStat;
  let fileStat;
  try {
    dirStat = fs.lstatSync(dir);
  } catch (_) {
    return "the git common directory is unreadable";
  }
  if (dirStat.isSymbolicLink() || !dirStat.isDirectory()) return "the git common directory is not a real directory";
  try {
    fileStat = fs.lstatSync(file);
  } catch (err) {
    if (err.code !== "ENOENT") return `${label} is unreadable`;
    return requireFile ? `no ${row.file} in the git common directory` : null;
  }
  if (fileStat.isSymbolicLink()) return `${label} is a symbolic link`;
  if (!fileStat.isFile()) return `${label} is not a regular file`;
  if (fileStat.size > RECORD_MAX_BYTES) return `${label} is larger than a record`;
  try {
    if (!fs.readdirSync(dir).includes(row.file)) return `the file on disk is not named exactly ${row.file}`;
    if (realPath(file) !== path.join(realPath(dir), row.file)) {
      return `${label} resolves outside the git common directory`;
    }
  } catch (_) {
    // Renamed or removed since the lstat: another copy spending the same token, or a revoke. A spender's claim
    // fallback decides; a throw here would deny the call that copy allowed, or exit 1 and let the call through.
    return `${label} is unreadable`;
  }
  return null;
}

/**
 * Why `record` is not a valid record of `row`, or null.
 *
 * Reasons never echo record values: a planted record must not put text into the session.
 *
 * @param {object} row Scope row.
 * @param {unknown} record Parsed JSON.
 * @param {Date} now Current time, for `expires_at`.
 * @returns {string|null}
 */
function recordProblem(row, record, now) {
  const label = recordLabel(row);
  if (record === null || typeof record !== "object" || Array.isArray(record)) return `${label} is not a JSON object`;
  if (record.version !== RECORD_VERSION) return `${label} holds an unknown record version (this reader knows 1)`;
  if (record.scope !== row.id) return `${label} holds an unknown scope (expected ${row.id})`;
  if (record.answer !== row.recordAnswer) return `${label} does not hold answer ${row.recordAnswer}`;
  if (typeof record.created_at !== "string" || !ISO_TIMESTAMP.test(record.created_at)) {
    return `${label} has no ISO created_at`;
  }
  if (typeof record.question !== "string") return `${label} has no question text`;
  const missing = row.fields.filter((field) => record[field] === undefined || record[field] === null);
  if (missing.length) return `${label} lacks ${missing.join(", ")}`;
  const empty = row.fields.find(
    (field) => TEXT_FIELDS.has(field) && (typeof record[field] !== "string" || record[field] === ""),
  );
  if (empty) return `${label} has no ${empty} text`;
  if (row.fields.includes("expires_at")) {
    if (typeof record.expires_at !== "string" || !ISO_TIMESTAMP.test(record.expires_at)) {
      return `${label} has no ISO expires_at`;
    }
    if (Date.parse(record.expires_at) <= now.getTime()) return `${label} has expired`;
  }
  return null;
}

/**
 * The validated record of one scope in a resolved common dir.
 *
 * @param {string} dir Absolute git common dir, from `recordDirectory`.
 * @param {object} row Scope row.
 * @param {Date} now Current time, for `expires_at`.
 * @returns {{ok: true, record: object, file: string, line: string}|{ok: false, reason: string, expired?: true,
 *   file?: string}} An expired record also carries `expired` and its `file`, so a spender can delete it.
 */
function recordAt(dir, row, now) {
  const problem = locationProblem(dir, row, true);
  if (problem) return { ok: false, reason: problem };
  const file = path.join(dir, row.file);
  let record;
  try {
    record = JSON.parse(fs.readFileSync(file, "utf8"));
  } catch (err) {
    // ENOENT: renamed by a concurrent spend after the location check (see locationProblem).
    const reason = err.code ? `${recordLabel(row)} is unreadable` : `${recordLabel(row)} is not valid JSON`;
    return { ok: false, reason };
  }
  const invalid = recordProblem(row, record, now);
  if (invalid)
    return isExpired(row, record, now)
      ? { ok: false, reason: invalid, expired: true, file }
      : { ok: false, reason: invalid };
  return { ok: true, record, file, line: `grant ${recordLabel(row)}@${record.created_at}` };
}

/**
 * True when a record of a row with a lifetime carries an `expires_at` already past.
 *
 * @param {object} row Scope row.
 * @param {object} record Parsed JSON object.
 * @param {Date} now Current time.
 * @returns {boolean}
 */
function isExpired(row, record, now) {
  const at = row.fields.includes("expires_at") && typeof record.expires_at === "string" ? record.expires_at : "";
  return ISO_TIMESTAMP.test(at) && Date.parse(at) <= now.getTime();
}

/**
 * The record of one scope as the session sees it, validated.
 *
 * @param {string} scopeId Scope id.
 * @param {{cwd?: string, env?: object, now?: Date}} [options] Session directory, environment, time.
 * @returns {{ok: true, record: object, file: string, line: string}|{ok: false, reason: string}} `line` is the
 *   record line a rule records: `grant .git/<file>@<created_at>`.
 */
function recordStatus(scopeId, { cwd, env = process.env, now = new Date() } = {}) {
  const row = scopeById(scopeId);
  if (!row) return { ok: false, reason: "unknown approval scope" };
  const where = recordDirectory(cwd, env);
  return where.reason ? { ok: false, reason: where.reason } : recordAt(where.dir, row, now);
}

/**
 * A record for one recorded answer: COMMON_FIELDS first, in the contract's order, then the row's own fields.
 *
 * @param {string} scopeId Scope id.
 * @param {string} question Question text the user answered.
 * @param {string} answer Answer text as recorded.
 * @param {Date} now Creation time.
 * @param {object} [extra] The row's own fields.
 * @returns {object}
 */
function buildRecord(scopeId, question, answer, now, extra = {}) {
  return { version: RECORD_VERSION, scope: scopeId, created_at: now.toISOString(), question, answer, ...extra };
}

/**
 * True when two records agree on everything but their times.
 *
 * `expires_at` follows `created_at`, and each plugin copy of the writer reads its own clock: the copies answering
 * one push approval write the same token at slightly different instants.
 *
 * @param {object} a Record.
 * @param {object} b Record.
 * @returns {boolean}
 */
function sameRecord(a, b) {
  const rest = (record) =>
    JSON.stringify(
      Object.keys(record)
        .filter((key) => !TIME_FIELDS.has(key))
        .sort()
        .map((key) => [key, record[key]]),
    );
  return rest(a) === rest(b);
}

/**
 * Write a record atomically into the common dir: a fresh temp file, then a rename onto the record name.
 *
 * The temp file is created exclusively (`wx`) under an unguessable name, so a link planted at a predictable name is
 * never written through; the rename replaces a link or file at the record name itself rather than following it.
 *
 * @param {string} dir Absolute git common dir.
 * @param {object} row Scope row.
 * @param {object} record Record.
 * @returns {string} Path written.
 */
function writeRecord(dir, row, record) {
  const target = path.join(dir, row.file);
  const temp = path.join(dir, `.${row.file}.${process.pid}.${crypto.randomBytes(6).toString("hex")}.tmp`);
  fs.writeFileSync(temp, `${JSON.stringify(record, null, 2)}\n`, { flag: "wx" });
  try {
    fs.renameSync(temp, target);
  } catch (err) {
    fs.rmSync(temp, { force: true });
    throw err;
  }
  return target;
}

// ── Writer (PostToolUse AskUserQuestion) ──────────────────────────────────────

/**
 * Parse the AskUserQuestion result into its `answers` map, or null.
 *
 * @param {unknown} response PostToolUse `tool_response`: an object or its JSON text.
 * @returns {object|null} Map of question text → answer string.
 */
function answersOf(response) {
  let value = response;
  if (typeof value === "string") {
    try {
      value = JSON.parse(value);
    } catch (_) {
      return null;
    }
  }
  if (!value || typeof value !== "object") return null;
  const answers = value.answers;
  return answers && typeof answers === "object" && !Array.isArray(answers) ? answers : null;
}

/**
 * The scope row whose question this exactly is — header, option set, single-select — or null.
 *
 * @param {object} question One entry of `tool_input.questions`.
 * @returns {object|null}
 */
function questionScope(question) {
  if (!question || typeof question !== "object" || question.multiSelect === true) return null;
  const header = String(question.header || "").trim();
  const labels = (Array.isArray(question.options) ? question.options : []).map((o) =>
    String((o && o.label) || "").trim(),
  );
  const row = SCOPES.find((candidate) => candidate.header === header);
  if (!row || labels.length !== row.options.length) return null;
  return row.options.every((label) => labels.includes(label)) ? row : null;
}

/**
 * True when the recording option's description names the record file the user would delete to revoke.
 *
 * @param {object} question A question of `row`.
 * @param {object} row Scope row.
 * @returns {boolean}
 */
function recordDisclosed(question, row) {
  const option = question.options.find((o) => o && String(o.label || "").trim() === row.recordAnswer);
  return String((option && option.description) || "").includes(row.file);
}

/**
 * True when a tool input carries pre-filled answers.
 *
 * @param {object|undefined} input A recorded AskUserQuestion input.
 * @returns {boolean}
 */
function hasPrefilledAnswers(input) {
  const answers = input && input.answers;
  return Boolean(answers && typeof answers === "object" && Object.keys(answers).length > 0);
}

/**
 * The model's recorded inputs for one tool call, from one transcript line, or null.
 *
 * @param {Buffer} line One JSONL line.
 * @param {string} toolUseId Tool call id.
 * @returns {object[]|null} `[block.input, wireToolInputs[id]]` when the line holds that call.
 */
function toolUseInputs(line, toolUseId) {
  const record = transcriptRecord(line);
  return record ? callInputs(record, toolUseId) : null;
}

/**
 * One transcript line parsed, or null when it is no JSON object.
 *
 * @param {Buffer} line One JSONL line.
 * @returns {object|null}
 */
function transcriptRecord(line) {
  try {
    const record = JSON.parse(line.toString("utf8"));
    return record && typeof record === "object" ? record : null;
  } catch (_) {
    return null;
  }
}

/**
 * The model's inputs for an AskUserQuestion call a transcript record holds: `[block.input, wireToolInputs[id]]`.
 *
 * @param {object} record Parsed transcript line.
 * @param {string} toolUseId Tool call id.
 * @returns {object[]|null}
 */
function callInputs(record, toolUseId) {
  const content = record.message && record.message.content;
  const block = Array.isArray(content)
    ? content.find((b) => b && b.type === "tool_use" && b.id === toolUseId && b.name === "AskUserQuestion")
    : null;
  if (!block) return null;
  const wire = record.wireToolInputs && record.wireToolInputs[toolUseId];
  return [block.input, wire];
}

/**
 * The user's answer to a call a transcript record holds, or null.
 *
 * Claude Code writes the result as a user line whose `message.content` holds the `tool_result` block and whose
 * top-level `toolUseResult` carries `{questions, answers, annotations}`, `answers` keyed by question text; the
 * block's prose `content` is never read.
 *
 * @param {object} record Parsed transcript line.
 * @param {string} toolUseId Tool call id.
 * @returns {{isError: boolean, answers: object, timestamp: unknown}|null}
 */
function callAnswer(record, toolUseId) {
  const content = record.message && record.message.content;
  const block = Array.isArray(content)
    ? content.find((b) => b && b.type === "tool_result" && b.tool_use_id === toolUseId)
    : null;
  if (!block) return null;
  const result = record.toolUseResult;
  const answers = result && typeof result === "object" ? result.answers : null;
  return {
    isError: block.is_error === true,
    answers: answers && typeof answers === "object" && !Array.isArray(answers) ? answers : {},
    timestamp: record.timestamp,
  };
}

/**
 * Visit the transcript lines naming a tool call id, newest first, until `visit` returns something.
 *
 * Splits on newline bytes, never decoded text, so a chunk boundary inside a multi-byte character cannot corrupt a
 * line. Only the newest TRANSCRIPT_MAX_BYTES are read.
 *
 * @param {string} transcriptPath Session transcript (JSONL).
 * @param {string} toolUseId Tool call id.
 * @param {function(Buffer): *} visit Called per line holding the id; a value other than undefined stops the scan.
 * @returns {{found: *}|{error: string}} `found` is undefined when no line stopped the scan.
 */
function scanTranscript(transcriptPath, toolUseId, visit) {
  let fd;
  try {
    fd = fs.openSync(transcriptPath, "r");
  } catch (_) {
    return { error: "the session transcript is unreadable" };
  }
  try {
    const size = fs.fstatSync(fd).size;
    const floor = Math.max(0, size - TRANSCRIPT_MAX_BYTES);
    const needle = Buffer.from(JSON.stringify(toolUseId));
    let end = size;
    let carry = Buffer.alloc(0);
    while (end > floor) {
      const start = Math.max(floor, end - TRANSCRIPT_CHUNK_BYTES);
      const chunk = Buffer.alloc(end - start);
      fs.readSync(fd, chunk, 0, chunk.length, start);
      let buf = Buffer.concat([chunk, carry]);
      const cut = start > floor ? buf.indexOf(NEWLINE) : -1;
      carry = cut >= 0 ? buf.subarray(0, cut) : Buffer.alloc(0);
      buf = cut >= 0 ? buf.subarray(cut + 1) : buf;
      let stop = buf.length;
      for (;;) {
        // `stop > 0` guard: a negative lastIndexOf offset counts from the end.
        const nl = stop > 0 ? buf.lastIndexOf(NEWLINE, stop - 1) : -1;
        const line = buf.subarray(nl + 1, stop);
        const found = line.includes(needle) ? visit(line) : undefined;
        if (found !== undefined) return { found };
        if (nl < 0) break;
        stop = nl;
      }
      end = start;
    }
    return { found: undefined };
  } catch (_) {
    return { error: "the session transcript is unreadable" };
  } finally {
    fs.closeSync(fd);
  }
}

/**
 * Find the model's recorded inputs for a tool call, reading the transcript tail-first.
 *
 * @param {string} transcriptPath Session transcript (JSONL).
 * @param {string} toolUseId Tool call id from the hook payload.
 * @returns {{inputs: object[]}|{error: string}}
 */
function findToolUse(transcriptPath, toolUseId) {
  const scan = scanTranscript(transcriptPath, toolUseId, (line) => toolUseInputs(line, toolUseId) || undefined);
  if (scan.error) return scan;
  return scan.found ? { inputs: scan.found } : { error: "the question's tool call is not in the session transcript" };
}

/**
 * Find an AskUserQuestion call and the user's answer after it, reading the transcript tail-first.
 *
 * The answer line follows its call (not always the next line), so the newest answer seen before reaching the call is
 * the call's answer; a call with no answer after it has none.
 *
 * @param {string} transcriptPath Session transcript (JSONL).
 * @param {string} toolUseId The approving call's id.
 * @returns {{inputs: object[], answer: object}|{error: string}}
 */
function findAnsweredCall(transcriptPath, toolUseId) {
  let answer = null;
  const scan = scanTranscript(transcriptPath, toolUseId, (line) => {
    const record = transcriptRecord(line);
    if (!record) return undefined;
    const inputs = callInputs(record, toolUseId);
    if (inputs) return inputs;
    answer = answer || callAnswer(record, toolUseId);
    return undefined;
  });
  if (scan.error) return scan;
  if (!scan.found) return { error: "the approving question's tool call is not in the session transcript" };
  if (!answer) return { error: "the session transcript holds no answer to the approving question" };
  return { inputs: scan.found, answer };
}

/**
 * Reason the model's own call pre-filled answers or cannot be checked, or null.
 *
 * @param {object} data Parsed hook payload.
 * @param {function(string, string): object} lookup Transcript lookup (findToolUse).
 * @returns {string|null}
 */
function prefillProblem(data, lookup) {
  if (!data.transcript_path) return "the payload names no session transcript";
  if (!data.tool_use_id) return "the payload carries no tool_use_id";
  const found = lookup(data.transcript_path, data.tool_use_id);
  if (found.error) return found.error;
  return found.inputs.some(hasPrefilledAnswers)
    ? "the answer was pre-filled in the model's own call, not picked by the user"
    : null;
}

/**
 * Reason one recording answer cannot record, or null.
 *
 * @param {object} data Parsed hook payload.
 * @param {object} question The answered question.
 * @param {object} row Its scope row.
 * @param {function(string, string): object} lookup Transcript lookup.
 * @returns {string|null}
 */
function answerProblem(data, question, row, lookup) {
  if (data.agent_id) return "the question came from a spawned agent; records belong to the lead session";
  if (row.discloseFile && !recordDisclosed(question, row)) {
    return (
      `the "${row.recordAnswer}" option description does not name ${row.file}; it must tell the user the record ` +
      `persists into later sessions in every worktree of this project, what it covers, and that ${revokeCommand(row)} ` +
      "revokes it"
    );
  }
  return prefillProblem(data, lookup);
}

/**
 * Decide what a PostToolUse payload means for the records: one verdict per recording answer.
 *
 * A revoking answer (`revokeAnswer`, the push question's `Deny`) needs none of the recording checks: removing a
 * pending record only lowers authority.
 *
 * @param {object} data Parsed hook payload.
 * @param {function(string, string): object} [lookup] Transcript lookup, injectable for tests.
 * @returns {Array<{action: "refuse"|"revoke", row: object, reason?: string}|{action: "write", row: object,
 *   question: string, answer: string}>} Empty when nothing records or revokes.
 */
function evaluateAnswer(data, lookup = findToolUse) {
  if (!data || data.hook_event_name !== "PostToolUse" || data.tool_name !== "AskUserQuestion") return [];
  const input = data.tool_input || {};
  const questions = Array.isArray(input.questions) ? input.questions : [];
  const answers = answersOf(data.tool_response) || {};
  const verdicts = [];
  for (const question of questions) {
    const row = questionScope(question);
    if (!row || !RECORD_BUILDERS[row.id]) continue;
    const answer = String(answers[question.question] || "").trim();
    if (row.revokeAnswer !== null && answer === row.revokeAnswer) verdicts.push({ action: "revoke", row });
    if (answer !== row.recordAnswer) continue;
    const reason = answerProblem(data, question, row, lookup);
    verdicts.push(
      reason
        ? { action: "refuse", row, reason }
        : { action: "write", row, question: String(question.question), answer: row.recordAnswer },
    );
  }
  return verdicts;
}

/**
 * The context line for a record just written: what it allows, for how long, and how to revoke it.
 *
 * @param {object} row Scope row.
 * @param {object} record Record written.
 * @param {string} target Path written.
 * @returns {string}
 */
function recordedMessage(row, record, target) {
  const written = `approval record recorded at ${target} (${RECORD_FAMILY} version ${RECORD_VERSION}, scope ${row.id}). `;
  const never = `never covered: ${row.neverCovers.join(", ")}. Revoke: ${revokeCommand(row)}.`;
  if (row.id === "gh-write-once") {
    return (
      `${written}The gh command the question named may run once, until ${record.expires_at}, as the whole Bash ` +
      `command exactly as approved, from this project, while every file it names holds what it holds now; other ` +
      `text, a changed file, a second run or a spawned agent's call needs a new ${row.header} question; ${never}`
    );
  }
  if (row.id === "push-once") {
    return (
      `${written}One non-force push of branch ${record.branch} at ${record.head.slice(0, 12)} to a configured ` +
      `remote may run once, until ${record.expires_at}, from this checkout, as the only push in its Bash command; a ` +
      `second push, a new commit or another branch needs a new ${row.header} question; ${never}`
    );
  }
  return (
    `${written}Later sessions in every worktree of this project skip the question for what it covers ` +
    `(${row.covers.join(", ")}); ${never}`
  );
}

/**
 * Remove the pending record a revoking answer cancels, and say so; null when there was none.
 *
 * @param {object} row Scope row.
 * @param {object} data Parsed hook payload.
 * @param {object} env Environment.
 * @returns {string|null}
 */
function revokeRecord(row, data, env) {
  const where = recordDirectory(data.cwd, env);
  if (where.reason) return null;
  const target = path.join(where.dir, row.file);
  try {
    fs.unlinkSync(target);
  } catch (_) {
    return null; // nothing pending, or not a file this writer would have written
  }
  return `pending approval record ${target} removed: the user answered ${row.revokeAnswer} (scope ${row.id}).`;
}

/**
 * Write the record of one answer, once across plugin copies; null when another copy has that answer in hand.
 *
 * The temp file is named after the answer's call id and created exclusively (`wx`, never through a link): of the copies
 * racing on one answer only one creates it. A copy arriving after that copy's rename creates it anew, then finds the
 * record already holding the answer and stays silent. So one copy writes and reports, the others stay silent.
 *
 * @param {string} dir Absolute git common dir.
 * @param {object} row Scope row.
 * @param {object} record Record to write.
 * @param {string} callId The answered call's `tool_use_id` (the writer refuses an answer without one).
 * @param {Date} now Current time, to read the record present.
 * @returns {string|null} Path written, or null.
 */
function writeAnswerOnce(dir, row, record, callId, now) {
  const key = crypto.createHash("sha256").update(String(callId)).digest("hex").slice(0, 16);
  const temp = path.join(dir, `.${row.file}.${key}.tmp`);
  try {
    fs.writeFileSync(temp, `${JSON.stringify(record, null, 2)}\n`, { flag: "wx" });
  } catch (err) {
    if (err.code === "EEXIST") return null;
    throw err;
  }
  const target = path.join(dir, row.file);
  try {
    const current = recordAt(dir, row, now);
    if (current.ok && sameRecord(current.record, record)) {
      fs.rmSync(temp, { force: true });
      return null;
    }
    fs.renameSync(temp, target);
  } catch (err) {
    fs.rmSync(temp, { force: true });
    throw err;
  }
  return target;
}

/**
 * Record one verdict and say what happened, as a context line; null when this copy changed nothing worth saying.
 *
 * A record already holding this answer is left unchanged in silence: with several plugins installed, every copy of
 * the writer runs on the same answer, and only the one that wrote reports it (`writeAnswerOnce`). A refusal is
 * deterministic, so every copy reports the same refusal line.
 *
 * @param {object} verdict One `evaluateAnswer` verdict.
 * @param {object} data Parsed hook payload.
 * @param {{now: Date, env: object}} options Creation time and environment.
 * @returns {string|null}
 */
function applyVerdict(verdict, data, { now, env }) {
  const { row } = verdict;
  const refused = (reason) => `approval record ${row.file} not recorded: ${reason}.`;
  if (verdict.action === "refuse") return refused(verdict.reason);
  if (verdict.action === "revoke") return revokeRecord(row, data, env);
  const where = recordDirectory(data.cwd, env);
  if (where.reason) return refused(where.reason);
  const problem = locationProblem(where.dir, row, false);
  if (problem) return refused(problem);
  const built = RECORD_BUILDERS[row.id](data, verdict, { now, env });
  if (built.reason) return refused(built.reason);
  const record = buildRecord(row.id, verdict.question, verdict.answer, now, built.fields);
  const current = recordAt(where.dir, row, now);
  if (current.ok && sameRecord(current.record, record)) return null;
  let target;
  try {
    target = writeAnswerOnce(where.dir, row, record, data.tool_use_id, now);
  } catch (err) {
    return refused(`writing it failed (${err.code || err.message})`);
  }
  return target === null ? null : recordedMessage(row, record, target);
}

/**
 * Run the writer: record every recording answer in the payload and remove what a revoking answer cancels.
 *
 * @param {object} data Parsed hook payload.
 * @param {{now?: Date, env?: object, lookup?: function}} [options] Time, environment and transcript lookup.
 * @returns {object|null} PostToolUse envelope for stdout, or null for silence.
 */
function recordAnswers(data, { now = new Date(), env = process.env, lookup = findToolUse } = {}) {
  const lines = evaluateAnswer(data, lookup)
    .map((verdict) => applyVerdict(verdict, data, { now, env }))
    .filter((line) => line !== null);
  if (lines.length === 0) return null;
  return { hookSpecificOutput: { hookEventName: "PostToolUse", additionalContext: lines.join("\n") } };
}

// ── Single-use spend (PreToolUse) ─────────────────────────────────────────────

const CLAIM_INFIX = ".spent-";
// How far an answer's transcript timestamp may sit ahead of the hook's clock (both read this machine's clock).
const ANSWER_CLOCK_SKEW_MS = 60 * 1000;
// Refusal text for an `expected` field a token does not hold, where "approves another <field>" would mislead.
const MISMATCH_REASONS = Object.freeze({
  files_sha256: "approves other file content: a file the command names changed after the user approved it",
});

/**
 * Path of the claim a spent token becomes for one tool call.
 *
 * @param {string} dir Absolute git common dir.
 * @param {object} row Scope row.
 * @param {string} callId The call's `tool_use_id`.
 * @returns {string}
 */
function claimPath(dir, row, callId) {
  const digest = crypto.createHash("sha256").update(callId).digest("hex").slice(0, 32);
  return path.join(dir, `${row.file}${CLAIM_INFIX}${digest}`);
}

/**
 * Name of the first `expected` field a record does not hold exactly, or null when it holds them all.
 *
 * @param {object} record Parsed record.
 * @param {object} expected Field → exact value.
 * @returns {string|null}
 */
function fieldMismatch(record, expected) {
  return Object.keys(expected).find((field) => record[field] !== expected[field]) || null;
}

/**
 * The record a claim holds for the row, or null: a regular file of record size holding the row's version and scope.
 *
 * Expiry is not checked: the token was valid when the call claimed it.
 *
 * @param {string} claim Claim path.
 * @param {object} row Scope row.
 * @returns {object|null}
 */
function claimRecord(claim, row) {
  try {
    const stat = fs.lstatSync(claim);
    if (!stat.isFile() || stat.size > RECORD_MAX_BYTES) return null;
    const record = JSON.parse(fs.readFileSync(claim, "utf8"));
    const known = record !== null && typeof record === "object" && record.version === RECORD_VERSION;
    return known && record.scope === row.id ? record : null;
  } catch (_) {
    return null;
  }
}

/**
 * The claims spent tokens of a row left in the common dir, as paths.
 *
 * @param {string} dir Absolute git common dir.
 * @param {object} row Scope row.
 * @returns {string[]}
 */
function claimsOf(dir, row) {
  try {
    return fs
      .readdirSync(dir)
      .filter((name) => name.startsWith(`${row.file}${CLAIM_INFIX}`))
      .map((name) => path.join(dir, name));
  } catch (_) {
    return [];
  }
}

/**
 * True when a claim other than `ownClaim` holds a token approved by the same answer: that approval was spent.
 *
 * The answer stays in the transcript after its token is spent, so a copy of the spent token planted again would pass the
 * transcript proof; the claim left by the spend still names the answer's call id. Limit: when that claim is the only
 * one and is itself renamed back to the record name (a run-time-built name the self-grant guard cannot read), no claim
 * is left to flag the reused answer; the answer's 15-minute freshness window still bounds the replay.
 *
 * @param {string} dir Absolute git common dir.
 * @param {object} row Scope row.
 * @param {string} questionId The approving call's id.
 * @param {string} ownClaim This call's claim, which may already hold the token.
 * @returns {boolean}
 */
function answerSpentElsewhere(dir, row, questionId, ownClaim) {
  return claimsOf(dir, row).some((claim) => {
    if (claim === ownClaim) return false;
    const record = claimRecord(claim, row);
    return record !== null && record.question_id === questionId;
  });
}

/**
 * Remove the claims of a row older than its lifetime, except `ownClaim`; best effort.
 *
 * A claim only lets the other copies of its own call agree, which happens within the call; past the lifetime its answer
 * is too old for the transcript proof, so it guards no replay either. Without this, a session with no SessionStart wipe
 * (foundry absent) would keep one claim per spent token for good.
 *
 * @param {string} dir Absolute git common dir.
 * @param {object} row Scope row.
 * @param {string} ownClaim This call's claim.
 * @param {Date} now Current time.
 * @returns {void}
 */
function pruneClaims(dir, row, ownClaim, now) {
  for (const claim of claimsOf(dir, row)) {
    try {
      if (claim !== ownClaim && fs.lstatSync(claim).mtimeMs < now.getTime() - row.ttlMs)
        fs.rmSync(claim, { force: true });
    } catch (_) {
      // Removed by another copy pruning at the same time.
    }
  }
}

/**
 * Why the session transcript does not prove the user approved this token, or null.
 *
 * The token names its approving AskUserQuestion call (`question_id`). That call must be in the transcript the spending
 * call's payload names, ask the row's question (header, option set, single-select) with exactly the token's question
 * text, carry no pre-filled answer, and be followed by a result whose answer to that text is the row's recording answer,
 * no error, dated within the row's lifetime. Forging a token then needs a forged transcript too.
 *
 * @param {object} row Scope row.
 * @param {object} record The token (or this call's claim of it).
 * @param {{transcriptPath?: string, now: Date}} options The spending call's transcript and the current time.
 * @returns {string|null} Reasons never echo token values.
 */
function approvalProofProblem(row, record, { transcriptPath, now }) {
  if (typeof transcriptPath !== "string" || transcriptPath === "") {
    return "the call names no session transcript to check the approval in";
  }
  const found = findAnsweredCall(transcriptPath, record.question_id);
  if (found.error) return found.error;
  if (found.inputs.some(hasPrefilledAnswers)) return "the approving answer was pre-filled in the model's own call";
  const [input] = found.inputs;
  const questions = input && Array.isArray(input.questions) ? input.questions : [];
  const asked = questions.find((q) => questionScope(q) === row && q.question === record.question);
  if (!asked) return `the approving call asks no ${row.header} question with this token's question text`;
  const { answer } = found;
  const picked = Object.prototype.hasOwnProperty.call(answer.answers, record.question)
    ? String(answer.answers[record.question]).trim()
    : "";
  if (answer.isError || picked !== row.recordAnswer) {
    return `the session transcript holds no ${row.recordAnswer} answer to the approving question`;
  }
  const at = typeof answer.timestamp === "string" ? Date.parse(answer.timestamp) : NaN;
  if (!(at <= now.getTime() + ANSWER_CLOCK_SKEW_MS && now.getTime() - at <= row.ttlMs)) {
    return "the approving answer is older than the approval lifetime, or undated";
  }
  if (row.id === "gh-write-once" && questionGhCommand(record.question).command !== record.command) {
    return "the approving question names another command than the token";
  }
  if (row.id === "push-once" && !record.question.includes(record.branch)) {
    return "the approving question does not name the token's branch";
  }
  return null;
}

/**
 * Why a token (or this call's claim of it) does not allow this call, or null.
 *
 * @param {object} row Scope row.
 * @param {object} record Token record.
 * @param {{dir: string, claim: string, expected: object, transcriptPath?: string, now: Date}} spend The spend.
 * @returns {string|null}
 */
function spendProblem(row, record, { dir, claim, expected, transcriptPath, now }) {
  const field = fieldMismatch(record, expected);
  if (field) return `${recordLabel(row)} ${MISMATCH_REASONS[field] || `approves another ${field}`}`;
  const unproven = approvalProofProblem(row, record, { transcriptPath, now });
  if (unproven) return unproven;
  return answerSpentElsewhere(dir, row, record.question_id, claim)
    ? "that approval was already spent by another call"
    : null;
}

/**
 * Spend a single-use token for one tool call, once across every plugin copy of the spending guard.
 *
 * The valid token whose `expected` fields match, proven by the session transcript (`approvalProofProblem`) and not
 * already spent by another call (`answerSpentElsewhere`), is renamed to this call's claim; of concurrent renames
 * exactly one succeeds. A copy that finds no token (another copy of this same call renamed it) accepts the claim its
 * own call id names after the same checks. The token is kept on every refusal: another text or file, an expired,
 * invalid or unproven token, a foreign project. A spend prunes the row's old claims (`pruneClaims`). Never throws: any
 * failure refuses (fail closed), since a throw would exit a guard 1 and let the call through.
 *
 * @param {string} scopeId A single-use scope id.
 * @param {{cwd?: string, env?: object, now?: Date, callId?: string, transcriptPath?: string, expected: object}}
 *   options Session directory, environment, time, the call's `tool_use_id`, the call's `transcript_path`, and the
 *   fields the token must hold exactly.
 * @returns {{ok: true}|{ok: false, reason: string}} Reasons never echo token values.
 */
function spendToken(scopeId, options) {
  try {
    return spendUnguarded(scopeId, options);
  } catch (err) {
    return { ok: false, reason: `the approval could not be checked (${err.code || err.message})` };
  }
}

/**
 * `spendToken` without its catch-all: may throw on a filesystem error the checks above do not absorb.
 *
 * @param {string} scopeId A single-use scope id.
 * @param {object} options See `spendToken`.
 * @returns {{ok: true}|{ok: false, reason: string}}
 */
function spendUnguarded(scopeId, { cwd, env = process.env, now = new Date(), callId, transcriptPath, expected }) {
  const row = scopeById(scopeId);
  if (!row || !row.singleUse) return { ok: false, reason: "unknown single-use approval scope" };
  if (typeof callId !== "string" || callId === "") {
    return { ok: false, reason: "the payload carries no tool_use_id to spend the approval with" };
  }
  const where = recordDirectory(cwd, env);
  if (where.reason) return { ok: false, reason: where.reason };
  const claim = claimPath(where.dir, row, callId);
  const spend = { dir: where.dir, claim, expected: expected || {}, transcriptPath, now };
  const status = recordAt(where.dir, row, now);
  if (status.ok) {
    const problem = spendProblem(row, status.record, spend);
    if (problem) return { ok: false, reason: problem };
    try {
      fs.renameSync(status.file, claim);
      pruneClaims(where.dir, row, claim, now);
      return { ok: true };
    } catch (_) {
      // Another copy of this call, or another call, renamed it first: the claim decides.
    }
  }
  const claimed = claimRecord(claim, row);
  if (claimed !== null && spendProblem(row, claimed, spend) === null) return { ok: true };
  return { ok: false, reason: status.ok ? `${recordLabel(row)} was spent by another call` : status.reason };
}

/**
 * Delete a scope's record and the claims its spent tokens left, in the session project's common dir.
 *
 * @param {string} scopeId Scope id.
 * @param {{cwd?: string, env?: object}} [options] Session directory and environment.
 * @returns {void}
 */
function removeRecord(scopeId, { cwd, env = process.env } = {}) {
  const row = scopeById(scopeId);
  const where = row ? recordDirectory(cwd, env) : { reason: "unknown approval scope" };
  if (where.reason) return;
  for (const name of fs.readdirSync(where.dir)) {
    if (name === row.file || name.startsWith(`${row.file}${CLAIM_INFIX}`)) {
      fs.rmSync(path.join(where.dir, name), { force: true });
    }
  }
}

// ── Self-grant guard (PreToolUse) ─────────────────────────────────────────────

const FILE_TOOLS = new Set(["Write", "Edit", "MultiEdit", "NotebookEdit"]);
// Record names a glob word is tested against: every scope's file and Codex's grant. The former Claude name
// `git-approval.json` is matched by name only (RECORD_SUFFIXES): as a glob target every `git-*` would reach it.
const RECORD_NAMES = Object.freeze([...SCOPES.map((row) => row.file), "codex-git-approval.json"]);
// Name suffixes that identify a record in any directory — Claude's, Codex's and the former name share them.
const RECORD_SUFFIXES = Object.freeze([...new Set(SCOPES.map((row) => row.file.replace(/^claude-/, "")))]);
const regexEscaped = (text) => text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
// Windows 8.3 short names of the record files (`CLAUDE~1.JSO`, `CODEX-~1.JSO`, `GIT-AP~1.JSO`).
const SHORT_RECORD_NAME = "(?:claude|codex-|git-ap)~\\d+\\.jso";
const RECORD_NAME = new RegExp(`${RECORD_SUFFIXES.map(regexEscaped).join("|")}|${SHORT_RECORD_NAME}`);
const SHORT_RECORD_BASENAME = new RegExp(`^${SHORT_RECORD_NAME}$`);
// A file-tool basename naming a record or the token state the hooks derive from one: a spent claim
// (`<record>.spent-<id>`) and the writer's temp file (`.<record>.<pid>.<hex>.tmp`). A Bash word is tested by containment
// (RECORD_NAME), which covers both already. Built at load from CLAIM_INFIX, so the spend section must stay above.
const RECORD_FILE_NAME = new RegExp(
  `(?:${RECORD_SUFFIXES.map(regexEscaped).join("|")})(?:${regexEscaped(CLAIM_INFIX)}.*|\\..*\\.tmp)?$`,
);
// The writer (hook entry and this module) and their 8.3 short names (`APPROV~1.JS`).
const WRITER_FILES = Object.freeze([
  { name: "approval-guard.js", home: "hooks" },
  { name: "approval-grants.js", home: "lib" },
]);
const WRITER_NAME = /approval-(?:guard|grants)\.js(?!on)|approv~\d+\.js(?!o)/;
// Brace expansions one word may stand for before it counts as naming a record or the git dir untested.
const MAX_BRACE_EXPANSIONS = 1024;
// A brace sequence body: numeric (`1..9`, `01..10..2`) or one letter to another (`a..z`).
const BRACE_SEQUENCE = /^(?:(-?\d+)\.\.(-?\d+)|([a-z])\.\.([a-z]))(?:\.\.-?\d+)?$/i;
// A read-only query naming the git dir: replaced by `.git` before segmenting, so
// `rm "$(git rev-parse --git-common-dir)/<record>"` reads as one `rm` segment. Only the git-dir flavours:
// `$(git rev-parse --show-toplevel)` or `HEAD` names no git dir.
const GIT_DIR_QUERY =
  /\$\(\s*git\s+rev-parse\b[^()`;&|<>]*--(?:git-common-dir|git-dir|absolute-git-dir|git-path)\b[^()`;&|<>]*\)|`\s*git\s+rev-parse\b[^()`;&|<>]*--(?:git-common-dir|git-dir|absolute-git-dir|git-path)\b[^()`;&|<>]*`/g;
// Directory a glob word must name for it to reach the record files (`.git/*`, `.git/claude-git-ap*`).
const RECORD_HOME = ".git";
// An output redirection and the word it writes: `>`, `>>`, `>|`, `&>`, `&>>`, `<>`, after an optional fd number.
// fd duplication (`2>&1`, `>&-`) names no file.
const REDIRECT_TARGET = /(?:&>>?|\d*>>?\|?|\d*<>)\s*([^\s&;|<>()][^\s;&|<>()]*)/g;
const GLOB_META = /[*?[{]/;
// A glob character that still matches against existing files after brace expansion.
const WILDCARD = /[*?[]/;
// Wildcards a glob word may hold before it counts as matching without a regex test (see `globMatches`).
const MAX_GLOB_WILDCARDS = 8;
// An output-file option turns a reader into a writer (`git log --output=<file>`).
const OUTPUT_OPTION = /^--output/;
// Exclusion options: their operand names what a command skips, never a target.
const EXCLUSION_ASSIGNED = /^--(?:exclude|ignore)[a-z-]*=/;
const EXCLUSION_SEPARATE = new Set(["--exclude", "--exclude-dir", "--ignore", "--ignore-glob"]);
const FIND_PATTERN_TESTS = new Set([
  "-path",
  "-ipath",
  "-name",
  "-iname",
  "-wholename",
  "-iwholename",
  "-regex",
  "-iregex",
  "-lname",
  "-ilname",
]);
// find actions that run a program or write a file: with one present, a pattern operand is not left out.
const FIND_WRITING_ACTION = /^-(?:exec|execdir|ok|okdir|delete|fls|fprint|fprint0|fprintf)$/;

// Commands that only read or delete a path they are handed, none with an option that writes a file (pagers are
// left out: `less -o <file>` writes its log there). Deleting a record revokes it, which only lowers authority.
const READ_DELETE_COMMANDS = new Set([
  "cat",
  "head",
  "tail",
  "wc",
  "ls",
  "stat",
  "file",
  "du",
  "test",
  "[",
  "[[",
  "grep",
  "egrep",
  "fgrep",
  "rg",
  "jq",
  "diff",
  "cmp",
  "shasum",
  "sha256sum",
  "md5",
  "md5sum",
  "rm",
  "unlink",
  // Change directory only; whatever runs next is its own segment.
  "cd",
  "pushd",
]);
// git subcommands that never write a path they are handed: they read it, stage or delete it, print it, or carry it as
// message text. `log`, `diff` and `show` write a file only through `--output` (OUTPUT_OPTION), `grep` runs a pager
// program only through `-O` (GIT_PAGER_OPTION); both are refused separately.
const READ_GIT_SUBCOMMANDS = new Set([
  "check-ignore",
  "status",
  "ls-files",
  "rm",
  "add",
  "commit",
  "log",
  "show",
  "diff",
  "grep",
  "rev-parse",
]);
const GIT_PAGER_OPTION = /^(?:-[a-z]*o|--open-files-in-pager)/;
// git global options that only say which repository and work tree to use, or how to match and page: skipped to find
// the subcommand, their values never a target. `-c`, `--config-env` and `--exec-path=` are absent on purpose — a
// config value or exec path can run a program (`core.pager`, `diff.external`), so git behind them stays a writer.
const GIT_LOCATION_OPTIONS = new Set(["-C", "--git-dir", "--work-tree", "--namespace"]);
const GIT_LOCATION_ASSIGNED = /^--(?:git-dir|work-tree|namespace)=/;
const GIT_PLAIN_FLAGS = new Set([
  "-P",
  "--no-pager",
  "-p",
  "--paginate",
  "--bare",
  "--no-replace-objects",
  "--no-lazy-fetch",
  "--no-optional-locks",
  "--no-advice",
  "--literal-pathspecs",
  "--glob-pathspecs",
  "--noglob-pathspecs",
  "--icase-pathspecs",
]);
// Env assignments before git that only locate the repository; any other one may make git run a program.
const GIT_LOCATION_VARIABLES = new Set(["GIT_DIR", "GIT_WORK_TREE"]);
// The writer source may be read, staged, diffed and linted; never executed.
const WRITER_SAFE_COMMANDS = new Set([...READ_DELETE_COMMANDS, "git", "pre-commit", "eslint", "prettier"]);
// Printing a name writes nothing: a segment naming the git dir as a whole (`… || echo ".git"`) may print it.
const PRINT_COMMANDS = new Set(["echo", "printf"]);
// node options that only syntax-check a script, never run it (`node --check .claude/hooks/*.js`).
const NODE_CHECK_OPTIONS = new Set(["--check", "-c"]);
// Operators a backslash makes literal words (`\(`, `\;`), so they split no command.
const ESCAPED_OPERATORS = "();|&`";
// Leading shell keywords that precede the real command in a segment.
const SHELL_KEYWORDS = new Set(["if", "then", "else", "elif", "while", "until", "do", "!", "time"]);
const ENVIRONMENT_ASSIGNMENT = /^[A-Za-z_][A-Za-z0-9_]*=/;
// A redirection word (`2>/dev/null`, `>`, `<<EOF`, `&>log`); an output operator standing alone takes the next word as
// its target. An input operator keeps the next word as an operand: `cp -R x "<" .git` hands cp a `<` and `.git`.
const REDIRECTION_WORD = /^(?:\d*|&)[<>]/;
const OUTPUT_OPERATOR = /^(?:\d*|&)(?:>>?|<>)[|&]?$/;
// Where each command family writes the paths it is handed (see `gitDirDestinations`): the option letter clustered
// short (`-rt DIR`) and the long options (any unambiguous prefix, `=value` or separate) naming a destination.
const COPY_DESTINATION = Object.freeze({ letter: "t", long: ["--target-directory"] });
const SYNC_DESTINATION = Object.freeze({
  letter: "T",
  long: ["--temp-dir", "--backup-dir", "--partial-dir", "--log-file", "--write-batch", "--only-write-batch"],
});
const EXTRACT_DESTINATION = Object.freeze({ letter: "C", long: ["--directory"] });
const UNZIP_DESTINATION = Object.freeze({ letter: "d", long: [] });
const CLONE_DESTINATION = Object.freeze({ letter: null, long: ["--separate-git-dir"] });

/**
 * `text` folded the way a case-insensitive volume compares names.
 *
 * NFKC maps compatibility forms to their plain letter (`ſ` → `s`, fullwidth `ｇ` → `g`), then lowercase. Folding more
 * than a volume does only makes a name test match more often.
 *
 * @param {string} text Any text.
 * @returns {string}
 */
function folded(text) {
  return text.normalize("NFKC").toLowerCase();
}

/**
 * One path component as the filesystem resolves it: folded, and with what Windows drops from a name — an alternate
 * data stream suffix (`::$DATA`) and trailing dots and spaces — removed.
 *
 * @param {string} component One path component.
 * @returns {string}
 */
function resolvedName(component) {
  return folded(component)
    .replace(/:.*$/, "")
    .replace(/[. ]+$/, "");
}

/**
 * Reason a file-tool target may plant or forge a record, or null.
 *
 * A record name in any directory (a staging copy later moved into place) or a token's claim or writer temp name
 * (RECORD_FILE_NAME), and a basename `.git` or its 8.3 short name `GIT~1` (a planted gitfile points git at another
 * repository). Other paths inside a git directory pass. The path is normalized first, so `x/.git/.` and `x/.git//`
 * name `.git` itself.
 *
 * @param {string} tool Tool name.
 * @param {string|undefined} filePath Raw `file_path` / `notebook_path`.
 * @returns {string|null}
 */
function filePathProblem(tool, filePath) {
  if (!filePath) return null;
  const normalized = path.posix.normalize(String(filePath).replace(/\\/g, "/"));
  const components = normalized.split("/").filter(Boolean);
  const base = resolvedName(components[components.length - 1] || "");
  if (RECORD_FILE_NAME.test(base) || SHORT_RECORD_BASENAME.test(base)) {
    return `${tool} targets an approval record name`;
  }
  if (base === ".git" || /^git~\d+$/.test(base)) return `${tool} targets a path named .git`;
  if (namesTranscript(folded(normalized))) return `${tool} targets a session transcript`;
  return null;
}

// Session transcript directory of the call being judged (folded, `/`-separated), set by `selfGrantProblem` from the
// payload's `transcript_path`; empty when the payload names none.
let transcriptHome = "";

/**
 * True when one shell word or path, folded, names a session transcript: a `.jsonl` file (or a glob or brace word that
 * could be one) under a `.claude/projects/` directory or under the judged call's own transcript directory, or that
 * directory itself. Token spends trust the transcript as the record of the user's answer (TRANSCRIPT PROOF), so a
 * write there could forge an approval. Memory and other non-`.jsonl` files under `projects/` stay writable.
 *
 * @param {string} word One shell word or path, folded.
 * @returns {boolean}
 */
function namesTranscript(word) {
  const alternatives = braceExpansions(word);
  if (!alternatives) return true;
  return alternatives.some((alternative) => {
    const text = alternative.replace(/\\/g, "/");
    const dir = text.replace(/\/+$/, "");
    if (transcriptHome && dir === transcriptHome) return true;
    const home = transcriptHome && text.startsWith(`${transcriptHome}/`);
    const underProjects = /(?:^|\/)\.claude\/projects\/|claude_config_dir\}?\/projects\//.test(text);
    if (!home && !underProjects) return false;
    const base = text.slice(text.lastIndexOf("/") + 1);
    return base.endsWith(".jsonl") || WILDCARD.test(base);
  });
}

// Commands that write an operand they are handed; a transcript word elsewhere (sed, awk, python reading it) is a
// read. An interpreter writing the transcript by its own code is beyond text inspection (LIMITS).
const TRANSCRIPT_WRITERS = new Set([
  ...["cp", "mv", "tee", "install", "rsync", "ln", "dd", "truncate", "touch", "patch", "uniq"],
  ...["curl", "wget", "tar", "unzip", "gzip", "gunzip"],
]);

/**
 * The words of a segment that its command could write as a transcript: a copy family's destinations only (a copy
 * source is a read), every word of another writer, `sed`/`perl` editing in place, `sort -o`; none otherwise.
 *
 * @param {{argv0: string, rest: string[]}} command `segmentCommand` result.
 * @param {string[]} words The segment's folded target words.
 * @returns {string[]}
 */
function transcriptWriteWords({ argv0, rest }, words) {
  const normalized = (word) => (word.includes("/") ? path.posix.normalize(folded(word)) : folded(word));
  if (argv0 === "cp" || argv0 === "mv" || argv0 === "install") {
    return copyDestinations(rest, COPY_DESTINATION).map(normalized);
  }
  if (argv0 === "rsync") return copyDestinations(rest, SYNC_DESTINATION).map(normalized);
  if (TRANSCRIPT_WRITERS.has(argv0)) return words;
  const inPlace = rest.some((token) => /^(?:-[a-z]*i|--in-place)/i.test(token));
  if ((argv0 === "sed" || argv0 === "perl") && inPlace) return words;
  if (argv0 === "sort" && rest.some((token) => /^(?:-[a-z]*o|--output)/i.test(token))) return words;
  return [];
}

// One ANSI-C escape inside `$'…'`: hex, unicode, octal, control, or a single escaped character.
const ANSI_C_ESCAPE = /\\(x[0-9a-fA-F]{1,2}|u[0-9a-fA-F]{1,4}|U[0-9a-fA-F]{1,8}|[0-7]{1,3}|c.|[\s\S])/g;
const ANSI_C_LETTERS = { n: "\n", t: "\t", r: "\r", a: "\x07", b: "\b", e: "\x1b", E: "\x1b", f: "\f", v: "\v" };

/**
 * Body of an ANSI-C `$'…'` string as the shell decodes it (`\x2e` → `.`).
 *
 * @param {string} body Text between `$'` and `'`.
 * @returns {string}
 */
function ansiCDecoded(body) {
  return body.replace(ANSI_C_ESCAPE, (_, escape) => {
    const kind = escape[0];
    if (kind === "x" || kind === "u" || kind === "U") {
      const point = parseInt(escape.slice(1), 16);
      return point <= 0x10ffff ? String.fromCodePoint(point) : "";
    }
    if (kind >= "0" && kind <= "7") return String.fromCharCode(parseInt(escape, 8));
    if (kind === "c") return ""; // control character — never part of a file name
    return ANSI_C_LETTERS[kind] || kind;
  });
}

/**
 * Shell text as the words a command receives: `$'…'` decoded, line continuations joined, backslash escapes
 * resolved, quote characters dropped.
 *
 * Coarse on purpose: a quote character inside other quotes is dropped too, which can only make a name test match
 * more.
 *
 * @param {string} text Shell text.
 * @returns {string}
 */
function unquoted(text) {
  return text
    .replace(/\$'((?:[^'\\]|\\[\s\S])*)'/g, (_, body) => ansiCDecoded(body))
    .replace(/\\\r?\n/g, "")
    .replace(/\\([\s\S])/g, "$1")
    .replace(/["']/g, "");
}

/**
 * Readings of shell text for the name tests: as the shell unquotes it, and with `\` read as a Windows path separator
 * (`C:\repo\.git`), which unquoting would drop. Unfolded, so option letters keep their case (`git -C` is not
 * `git -c`); names are compared after `folded`.
 *
 * @param {string} text Shell text.
 * @returns {string[]}
 */
function plainForms(text) {
  return [unquoted(text), text.replace(/["']/g, "").replace(/\\/g, "/")];
}

/**
 * The whitespace-split words of one shell text.
 *
 * @param {string} text Shell text.
 * @returns {string[]}
 */
function shellWords(text) {
  return text.trim().split(/\s+/).filter(Boolean);
}

/**
 * Index of the `}` closing the brace group opened at `start`, or -1.
 *
 * @param {string} glob Pattern text.
 * @param {number} start Index of `{`.
 * @returns {number}
 */
function braceEnd(glob, start) {
  let depth = 0;
  for (let index = start; index < glob.length; index++) {
    if (glob[index] === "{") depth += 1;
    else if (glob[index] === "}" && --depth === 0) return index;
  }
  return -1;
}

/**
 * Index of the `]` closing the bracket expression opened at `start`, or -1.
 *
 * A leading `!`/`^` and a `]` right after it are members, and a POSIX class (`[:alpha:]`) is skipped whole, as the
 * shell reads them.
 *
 * @param {string} glob Pattern text.
 * @param {number} start Index of `[`.
 * @returns {number}
 */
function bracketEnd(glob, start) {
  let index = start + 1;
  if (glob[index] === "!" || glob[index] === "^") index += 1;
  if (glob[index] === "]") index += 1;
  for (; index < glob.length; index++) {
    if (glob.startsWith("[:", index)) {
      const classEnd = glob.indexOf(":]", index + 2);
      if (classEnd < 0) return -1;
      index = classEnd + 1;
    } else if (glob[index] === "]") return index;
  }
  return -1;
}

/**
 * Alternatives of a brace body split on its top-level commas (`a,{b,c}` → `a`, `{b,c}`).
 *
 * @param {string} body Text between `{` and its `}`.
 * @returns {string[]}
 */
function braceAlternatives(body) {
  const parts = [""];
  let depth = 0;
  for (const char of body) {
    if (char === "," && depth === 0) parts.push("");
    else {
      if (char === "{") depth += 1;
      if (char === "}") depth -= 1;
      parts[parts.length - 1] += char;
    }
  }
  return parts;
}

/**
 * Regex source matching every name one shell glob word can expand to: `*`, `?`, `[…]`, and brace alternatives or
 * sequences (`{json,js}`, `{a..z}`).
 *
 * Bracket syntax JavaScript lacks (`[[:alpha:]]`) becomes "any one character", the safe direction.
 *
 * @param {string} glob One path component, folded.
 * @returns {string}
 */
function globSource(glob) {
  let source = "";
  for (let index = 0; index < glob.length; index++) {
    const char = glob[index];
    const close = char === "[" ? bracketEnd(glob, index) : char === "{" ? braceEnd(glob, index) : -1;
    if (char === "*") source += "[^/]*";
    else if (char === "?") source += "[^/]";
    else if (char === "[" && close > 0) {
      const body = glob.slice(index + 1, close);
      source += /\[:|\\/.test(body) ? "[^/]" : `[${body.replace(/^[!^]/, "^").replace(/\]/g, "\\]")}]`;
      index = close;
    } else if (char === "{" && close > 0 && braceAlternatives(glob.slice(index + 1, close)).length > 1) {
      source += `(?:${braceAlternatives(glob.slice(index + 1, close))
        .map(globSource)
        .join("|")})`;
      index = close;
    } else if (char === "{" && close > 0 && glob.slice(index + 1, close).includes("..")) {
      source += "[^/]*";
      index = close;
    } else source += char.replace(/[.+^$(){}|\\[\]]/g, "\\$&"); // an unclosed `[` is a literal to the shell too
  }
  return source;
}

/**
 * True when glob `pattern` can expand to exactly `name`.
 *
 * Runs of `*` collapse to one; a pattern with more than MAX_GLOB_WILDCARDS wildcards, or one no regex can express,
 * counts as a match without being compiled — each wildcard is a regex repeat, and dozens of them backtrack against a
 * short name for hours, stalling every Bash call behind this guard.
 *
 * @param {string} pattern One path component, folded.
 * @param {string} name File or directory name.
 * @returns {boolean}
 */
function globMatches(pattern, name) {
  const compact = pattern.replace(/\*+/g, "*");
  if ((compact.match(/[*?]/g) || []).length > MAX_GLOB_WILDCARDS) return true;
  try {
    return new RegExp(`^${globSource(compact)}$`).test(name);
  } catch (_) {
    return true;
  }
}

/**
 * True when a shell word holds a glob or brace pattern that can expand to `name`.
 *
 * The pattern counts where the file lives — a directory component that can match `home` — so `cp fixtures/*.json
 * out/` stays usable. With `spelledAnywhere`, a pattern spelling part of the name (`approval-gu*.js`) counts in any
 * directory too; a bare wildcard (`*`, `*.json`) still needs the directory.
 *
 * @param {string} word One shell word, folded.
 * @param {string} name Protected file name.
 * @param {string} home Directory the protected file lives in.
 * @param {boolean} spelledAnywhere Whether a pattern spelling part of the name counts outside `home`.
 * @returns {boolean}
 */
function globNames(word, name, home, spelledAnywhere) {
  if (!GLOB_META.test(word)) return false;
  const slash = word.lastIndexOf("/");
  const base = word.slice(slash + 1);
  if (!globMatches(base, name)) return false;
  const literal = base.replace(/\[[^\]]*\]|\{[^}]*\}|[*?]/g, "").replace(/\.[^.]*$/, "");
  if (spelledAnywhere && /[a-z0-9]/.test(literal)) return true;
  const parent = slash > 0 ? word.slice(0, slash).split("/").pop() : "";
  return parent !== "" && globMatches(parent, home);
}

/**
 * Values a brace sequence body expands to, or null when it is no sequence.
 *
 * A letter range is listed in full. A numeric range contributes digits only — and digits reach a record name only as
 * an 8.3 name's `~N` — so its two ends stand for every value, and `{1..100000}` stays two words.
 *
 * @param {string} body Text between `{` and its `}`.
 * @returns {string[]|null}
 */
function sequenceValues(body) {
  const match = BRACE_SEQUENCE.exec(body);
  if (!match) return null;
  if (match[1] !== undefined) return [match[1], match[2]];
  const [from, to] = [match[3].charCodeAt(0), match[4].charCodeAt(0)];
  const values = [];
  for (let code = Math.min(from, to); code <= Math.max(from, to); code++) values.push(String.fromCharCode(code));
  return values;
}

/**
 * Every word a shell word's brace groups expand to (`x{1,2}.md` → `x1.md`, `x2.md`), or null past
 * MAX_BRACE_EXPANSIONS.
 *
 * Brace expansion happens whether or not the files exist, so each alternative is a literal name the command receives.
 * A group without a top-level comma or sequence (`{x}`, `${f}`) is literal, as the shell reads it; one inside a
 * parameter expansion is expanded too, which zsh does and which can only make a name test match more.
 *
 * @param {string} word One shell word.
 * @param {number} [limit] Expansions allowed before giving up.
 * @returns {string[]|null}
 */
function braceExpansions(word, limit = MAX_BRACE_EXPANSIONS) {
  for (let index = word.indexOf("{"); index >= 0; index = word.indexOf("{", index + 1)) {
    const close = braceEnd(word, index);
    const body = close > 0 ? word.slice(index + 1, close) : "";
    const parts = braceAlternatives(body);
    const choices = close < 0 ? null : parts.length > 1 ? parts : sequenceValues(body);
    if (choices) {
      const expanded = [];
      for (const choice of choices) {
        const more = braceExpansions(word.slice(0, index) + choice + word.slice(close + 1), limit - expanded.length);
        if (!more || expanded.push(...more) > limit) return null;
      }
      return expanded;
    }
  }
  return [word];
}

/**
 * True when one shell word names a record: a record name in any directory — brace alternatives each count as one,
 * and so does a literal record basename below a glob directory (`stag?ng/claude-git-approval.json`) — or a glob word
 * in the common dir that can expand to one. A glob basename elsewhere never counts, even one spelling most of a name
 * (`staging/[!x]laude-git-approval.json`): a glob reaches only files that exist, and creating one there needs its
 * literal name. A word with more alternatives than can be tested counts.
 *
 * @param {string} word One shell word, folded.
 * @returns {boolean}
 */
function namesRecord(word) {
  const alternatives = braceExpansions(word);
  if (!alternatives) return true;
  return alternatives.some((alternative) => {
    const base = alternative.slice(alternative.lastIndexOf("/") + 1);
    if (!WILDCARD.test(alternative)) return RECORD_NAME.test(alternative);
    if (!WILDCARD.test(base) && RECORD_NAME.test(base)) return true;
    return RECORD_NAMES.some((name) => globNames(alternative, name, RECORD_HOME, false));
  });
}

/**
 * True when one shell word, folded, names the common dir as a whole: its last path component (after a `=` or `:`
 * prefix, with trailing `/`, `/.` and Windows trailing dots dropped) is `.git` or its 8.3 name `git~1`, or a glob the
 * shell can expand to `.git`. A glob reaches a dot name only through a leading `.` or a bracket listing `.` (`.gi?`,
 * `.g*t`, `.git*`, `[.]git`); a bare `*` or `?git` skips it. Brace alternatives count one by one.
 *
 * @param {string} word One shell word, folded.
 * @returns {boolean}
 */
function namesGitDir(word) {
  const alternatives = braceExpansions(word);
  if (!alternatives) return true;
  return alternatives.some((alternative) => {
    const components = path.posix
      .normalize(alternative)
      .split(/[/=:<>]/)
      .filter(Boolean);
    const base = (components[components.length - 1] || "").replace(/[. ]+$/, "");
    if (base === RECORD_HOME || /^git~\d+$/.test(base)) return true;
    const bracket = base.startsWith("[") ? bracketEnd(base, 0) : -1;
    const reachesDotName =
      base.startsWith(".") || (bracket > 0 && /^\[[^!^]/.test(base) && base.slice(1, bracket).includes("."));
    return WILDCARD.test(base) && reachesDotName && globMatches(base, RECORD_HOME);
  });
}

/**
 * Split a command into the segments a shell would run separately.
 *
 * Coarse on purpose: an operator inside quotes yields an extra segment, which can only add a check. A line
 * continuation is joined and a backslash-escaped operator (`find … \( … \) -exec … \;`) is no operator, as the shell
 * reads them, so one command keeps its own words; escapes pair left to right, so `\\;` still ends a command.
 *
 * @param {string} command Bash command text.
 * @returns {string[]}
 */
function shellSegments(command) {
  const joined = command.replace(/\\(\r?\n|[\s\S])/g, (pair, char) =>
    char.endsWith("\n") || ESCAPED_OPERATORS.includes(char) ? " " : pair,
  );
  return joined.split(/(?:\|\||&&|[;&|\n`()])/);
}

/**
 * The command a segment runs, after env assignments and shell keywords.
 *
 * @param {string[]} tokens Whitespace-split words of one segment form.
 * @returns {{argv0: string, rest: string[], assigns: boolean, assigned: string[]}} argv0 basename, folded (empty for
 *   a pure assignment); `rest` the words after it as given, so unfolded words keep their option case (`git -C` is not
 *   `git -c`, `node -C` is not `node -c`); `assigned` the names of the env assignments before it, `assigns` when there
 *   is one.
 */
function segmentCommand(tokens) {
  let i = 0;
  const assigned = [];
  while (i < tokens.length && (ENVIRONMENT_ASSIGNMENT.test(tokens[i]) || SHELL_KEYWORDS.has(folded(tokens[i])))) {
    if (ENVIRONMENT_ASSIGNMENT.test(tokens[i])) assigned.push(tokens[i].slice(0, tokens[i].indexOf("=")));
    i += 1;
  }
  return {
    argv0: folded(tokens[i] || "")
      .split(/[/\\]/)
      .pop(),
    rest: tokens.slice(i + 1),
    assigns: assigned.length > 0,
    assigned,
  };
}

/**
 * The tokens of one segment that can name a target: exclusion operands left out.
 *
 * @param {string[]} tokens Whitespace-split tokens of one form; options are compared folded, tokens kept as given.
 * @param {string} argv0 Command the segment runs.
 * @returns {string[]}
 */
function targetTokens(tokens, argv0) {
  const keys = tokens.map(folded);
  const findReadsOnly = argv0 === "find" && !keys.some((key) => FIND_WRITING_ACTION.test(key));
  const takesOperand = (key) =>
    EXCLUSION_SEPARATE.has(key) || (findReadsOnly && FIND_PATTERN_TESTS.has(key)) || (argv0 === "tree" && key === "-i");
  const kept = [];
  let skip = 0;
  let excludeList = false;
  keys.forEach((key, index) => {
    if (skip > 0) skip -= 1;
    else if (excludeList && !key.startsWith("-")) return;
    else if (EXCLUSION_ASSIGNED.test(key)) excludeList = false;
    else if (takesOperand(key)) skip = 1;
    else if ((argv0 === "zip" || argv0 === "unzip") && key === "-x") excludeList = true;
    else {
      excludeList = false;
      kept.push(tokens[index]);
    }
  });
  return kept;
}

/**
 * One folded form of a segment as its name tests read it: exclusion operands dropped, repeated `/` and `/./`
 * collapsed in every path-shaped token.
 *
 * @param {string} form One folded form.
 * @param {string} argv0 Command the segment runs.
 * @returns {string}
 */
function targetText(form, argv0) {
  return targetTokens(form.trim().split(/\s+/).filter(Boolean), argv0)
    .map((token) => (token.includes("/") ? path.posix.normalize(token) : token))
    .join(" ");
}

/**
 * One shell segment as the guard judges it: the command it runs and which protected names it could reach.
 *
 * Read as the shell and filesystem resolve it (`plainForms`, folded), a segment names a record as a write target when a word
 * names a record (`namesRecord`) and either the segment's command could write its operands (`writesNothing` false)
 * or an output redirection writes onto a record; it writes the common dir as a whole when a redirection targets a path
 * named `.git` (a planted gitfile) or a word naming it sits where the command writes (`gitDirWritten`); it names the
 * writer when it spells it or holds a glob that can expand to it.
 *
 * @param {string} segment One raw shell segment.
 * @returns {{argv0: string, rest: string[], assigns: boolean, assigned: string[], recordTarget: boolean,
 *   gitDirRedirect: boolean, gitDirTarget: boolean, writer: boolean}}
 */
function segmentReading(segment) {
  const plain = plainForms(segment);
  const forms = plain.map(folded);
  const command = segmentCommand(shellWords(plain[0]));
  const texts = forms.map((form) => targetText(form, command.argv0));
  const words = texts.flatMap((text) => text.split(/[\s=<>]+/)).filter(Boolean);
  const redirected = forms.flatMap((form) =>
    [...form.matchAll(REDIRECT_TARGET)].map(([, target]) =>
      target.includes("/") ? path.posix.normalize(target) : target,
    ),
  );
  return {
    ...command,
    recordTarget: words.some(namesRecord) && (!writesNothing(command) || redirected.some(namesRecord)),
    transcriptTarget: redirected.some(namesTranscript) || transcriptWriteWords(command, words).some(namesTranscript),
    gitDirRedirect: redirected.some(namesGitDir),
    gitDirTarget: plain.some(gitDirWritten),
    writer:
      texts.some((text) => WRITER_NAME.test(text)) ||
      words.some((word) => WRITER_FILES.some(({ name, home }) => globNames(word, name, home, true))),
  };
}

/**
 * True when one form of a segment names the common dir as a whole where its command writes (`gitDirDestinations`).
 * A read, delete or print verb, or a git subcommand that never writes a path it is handed, writes nowhere.
 *
 * @param {string} form One unfolded form of a segment (`plainForms`).
 * @returns {boolean}
 */
function gitDirWritten(form) {
  const all = shellWords(form);
  const words = targetTokens(all, segmentCommand(all).argv0);
  const command = segmentCommand(words);
  // Destinations are tested, not the words: a value glued to a short option (`-d.git`, `-t.git`) is no word of its own.
  return !writesNothing(command) && gitDirDestinations(command, words).some((word) => namesGitDir(folded(word)));
}

/**
 * The words of a command handed the common dir that name where it writes.
 *
 * `cp`, `mv`, `install`, `rsync`: the destination operand and the target, temp and backup dir options; a source is
 * only read. `tar`, `unzip`: the extract directory (`-C`/`--directory`, `-d`); an archive member is content, beyond
 * the guard (LIMITS). `git`: the words after the subcommand, a clone's destination only; a repository located by
 * `-C`, `--git-dir`, `--work-tree`, `GIT_DIR=` or `GIT_WORK_TREE=` is never written by the location itself. `ln`:
 * every operand, since a link to the common dir is an alias whose later writes no name test follows. Any other
 * command, and git behind `-c`, `--exec-path=` or a program-running env assignment, may write into anything it is
 * handed: every word.
 *
 * @param {{argv0: string, rest: string[], assigned: string[]}} command `segmentCommand` result over `words`.
 * @param {string[]} words The segment's words, unfolded, exclusion operands dropped.
 * @returns {string[]}
 */
function gitDirDestinations(command, words) {
  const { argv0, rest, assigned } = command;
  if (argv0 === "cp" || argv0 === "mv" || argv0 === "install") return copyDestinations(rest, COPY_DESTINATION);
  if (argv0 === "rsync") return copyDestinations(rest, SYNC_DESTINATION);
  if (argv0 === "unzip") return optionValues(withoutRedirections(rest), UNZIP_DESTINATION);
  // An old-style tar bundle (`tar xfC a.tar dir`) hands each value letter the next word in turn: every word counts.
  if (argv0 === "tar" && /^[A-Za-z]*C[A-Za-z]*$/.test(rest[0] || "")) return rest;
  if (argv0 === "tar") return optionValues(withoutRedirections(rest), EXTRACT_DESTINATION);
  if (argv0 === "ln") return rest;
  const index = argv0 === "git" ? gitSubcommandIndex(rest) : -1;
  if (index < 0 || assigned.some((name) => !GIT_LOCATION_VARIABLES.has(name))) return words;
  const after = rest.slice(index + 1);
  return folded(rest[index]) === "clone" ? copyDestinations(after, CLONE_DESTINATION) : after;
}

/**
 * The words a copy-family command writes to: its last operand and the value of each destination option. When the
 * last word is an option or follows one, its arity is unknown, so which word is the destination is too: every word.
 *
 * @param {string[]} rest Words after the command, unfolded.
 * @param {{letter: string|null, long: string[]}} destination The command's destination options.
 * @returns {string[]}
 */
function copyDestinations(rest, destination) {
  const words = withoutRedirections(rest);
  const isOption = (word) => word !== undefined && word.startsWith("-") && word !== "-" && word !== "--";
  const last = words.length - 1;
  const unknown = last >= 0 && (isOption(words[last]) || isOption(words[last - 1]));
  return [...optionValues(words, destination), ...(unknown ? words : words.slice(Math.max(last, 0)))];
}

/**
 * The values of a command's destination options: a short cluster holding the option `letter` takes the rest of the
 * cluster or else the next word (`-t dir`, `-rt dir`, `-xzCdir`); a `long` option given by any prefix GNU getopt
 * accepts (`--target=dir`, `--dir dir`) takes its `=value` or else the next word.
 *
 * @param {string[]} words Words after the command, unfolded, redirections left out.
 * @param {{letter: string|null, long: string[]}} destination The command's destination options.
 * @returns {string[]}
 */
function optionValues(words, { letter, long }) {
  const values = [];
  words.forEach((word, index) => {
    const next = words[index + 1] || "";
    const [, name, value] = /^--([^=]+)(?:=([\s\S]*))?$/.exec(word) || [];
    if (name && long.some((option) => option.startsWith(`--${name}`))) values.push(value === undefined ? next : value);
    else if (letter && /^-[^-]/.test(word) && word.includes(letter, 1)) {
      values.push(word.slice(word.indexOf(letter, 1) + 1) || next);
    }
  });
  return values;
}

/**
 * Words with shell redirections left out: each redirection word, and the target after an output operator standing
 * alone (`> log`). Output targets are judged on their own (`gitDirRedirect`).
 *
 * @param {string[]} words Words of one segment form.
 * @returns {string[]}
 */
function withoutRedirections(words) {
  const kept = [];
  for (let index = 0; index < words.length; index++) {
    if (!REDIRECTION_WORD.test(words[index])) kept.push(words[index]);
    else if (OUTPUT_OPERATOR.test(words[index])) index += 1;
  }
  return kept;
}

/**
 * Index of the git subcommand in `rest`, past global options that only locate the repository or set matching and
 * paging (GIT_LOCATION_OPTIONS, GIT_LOCATION_ASSIGNED, GIT_PLAIN_FLAGS); -1 when another option comes first (`-c`,
 * `--exec-path=`: either can run a program) or no subcommand follows.
 *
 * @param {string[]} rest Words after `git`, unfolded: `-C` locates, `-c` configures.
 * @returns {number}
 */
function gitSubcommandIndex(rest) {
  let index = 0;
  while (index < rest.length && rest[index].startsWith("-")) {
    if (GIT_LOCATION_OPTIONS.has(rest[index])) index += 2;
    else if (GIT_LOCATION_ASSIGNED.test(rest[index]) || GIT_PLAIN_FLAGS.has(rest[index])) index += 1;
    else return -1;
  }
  return index < rest.length ? index : -1;
}

/**
 * True when a git segment runs a subcommand that never writes a path it is handed, behind location and paging options
 * only (`git -C <dir> rev-parse`, `git --git-dir=<dir> log`).
 *
 * @param {string[]} rest Words after `git`, unfolded.
 * @returns {boolean}
 */
function isReadOnlyGit(rest) {
  const index = gitSubcommandIndex(rest);
  const sub = index < 0 ? "" : folded(rest[index]);
  if (!READ_GIT_SUBCOMMANDS.has(sub)) return false;
  return sub !== "grep" || !rest.some((token) => GIT_PAGER_OPTION.test(folded(token)));
}

/**
 * True when a segment's command writes none of the paths it names: an exact read, delete or print verb, or a git
 * subcommand that never writes a path it is handed; no output-file option, no env assignment before it
 * (`GIT_EXTERNAL_DIFF=…`, `LESSOPEN=…` make a reader run a program) but `GIT_DIR=`/`GIT_WORK_TREE=` before git, which
 * only locate the repository. Its own output redirections are judged apart.
 *
 * @param {{argv0: string, rest: string[], assigned: string[]}} command `segmentCommand` result.
 * @returns {boolean}
 */
function writesNothing({ argv0, rest, assigned }) {
  const runsAfterAssignment =
    argv0 === "git" ? assigned.some((name) => !GIT_LOCATION_VARIABLES.has(name)) : assigned.length > 0;
  if (runsAfterAssignment || rest.some((token) => OUTPUT_OPTION.test(folded(token)))) return false;
  if (argv0 === "git") return isReadOnlyGit(rest);
  return READ_DELETE_COMMANDS.has(argv0) || PRINT_COMMANDS.has(argv0);
}

/**
 * True when a segment naming the writer cannot execute it: it reads, stages, lints or syntax-checks the source.
 *
 * `rest` is unfolded, so only node's own `-c` (`--check`) counts, never `-C` (`--conditions`, which runs the script).
 *
 * @param {object} reading `segmentReading` result.
 * @returns {boolean}
 */
function writerSegmentSafe({ argv0, rest, assigns }) {
  if (assigns) return false;
  if (argv0 === "node") return NODE_CHECK_OPTIONS.has(rest[0]) && !rest.slice(1).some((t) => t.startsWith("-"));
  return WRITER_SAFE_COMMANDS.has(argv0);
}

/**
 * Reason a Bash command may create or forge a record, or null.
 *
 * Each segment is judged on its own: a record named as a write target, an output redirection onto a path named `.git`,
 * the common dir named as a whole where the command writes, the writer named by anything that could run it. A record
 * name a later segment receives at run time (a variable, a pipe) is beyond text inspection (LIMITS).
 *
 * @param {string} command Bash command text.
 * @returns {string|null}
 */
function bashRecordProblem(command) {
  const text = String(command || "").replace(GIT_DIR_QUERY, ".git");
  const segments = shellSegments(text).map(segmentReading);
  if (segments.some((s) => s.recordTarget)) {
    return (
      "the command names an approval record as a write target: a redirection or output option onto it, or an " +
      "operand of a command that is not an exact read, delete or print (cat, jq, grep, ls, test, rm, echo, git " +
      "status/add/commit/log/show/diff/grep/rev-parse) or that runs after an env assignment"
    );
  }
  if (segments.some((s) => s.transcriptTarget)) {
    return (
      "the command names a session transcript as a write target (a .jsonl file under .claude/projects/ or the " +
      "session's transcript directory): token spends trust the transcript as the record of the user's answer"
    );
  }
  if (segments.some((s) => s.gitDirRedirect)) {
    return "an output redirection writes onto a path named .git (a planted gitfile points git at another repository)";
  }
  if (segments.some((s) => s.gitDirTarget)) {
    return (
      "the command names the git common dir as a whole where it writes: a copy, move, sync, extract or clone " +
      "destination, an operand of ln, or an operand of a command that is not a read, a delete or a printed name"
    );
  }
  if (segments.some((s) => s.writer && !writerSegmentSafe(s))) {
    return "a segment naming the approval writer (approval-guard.js, lib/approval-grants.js) could execute it";
  }
  return null;
}

/**
 * Reason to block this PreToolUse call as a self-grant, or null.
 *
 * @param {object} data Parsed hook payload.
 * @returns {string|null}
 */
function selfGrantProblem(data) {
  if (!data || data.hook_event_name !== "PreToolUse") return null;
  const input = data.tool_input || {};
  transcriptHome = data.transcript_path
    ? folded(path.posix.dirname(String(data.transcript_path).replace(/\\/g, "/")))
    : "";
  if (FILE_TOOLS.has(data.tool_name)) return filePathProblem(data.tool_name, input.file_path || input.notebook_path);
  return data.tool_name === "Bash" ? bashRecordProblem(input.command) : null;
}

/**
 * Stderr text for a blocked self-grant.
 *
 * @param {string} problem Specific reason.
 * @returns {string}
 */
function blockMessage(problem) {
  const local = SCOPES[0];
  return (
    `approval record guard — blocked: ${problem}.\n` +
    `Approval records (${SCOPES.map((row) => row.file).join(", ")}) live in the git common dir and are ` +
    "written only by the approval-guard hook from the user's own answer; ask the question instead of creating one " +
    `(local Git: the ${local.header} question, "${local.recordAnswer}").\n` +
    "Reading a record (Read tool, cat, jq, test, ls, grep), mentioning it (echo, git commit -m), the grant check " +
    `(node <foundry>/hooks/commit-guard.js --check-grant) and deleting it (${revokeCommand(local)} — revoke) stay ` +
    "allowed.\n" +
    "If the command only mentions the name through another program (an interpreter, a heredoc, a redirect in quoted " +
    "text), move that text into a file and pass it by path (git commit -F <file>, --body-file <file>); if a glob or " +
    "brace word could expand to it or to the git dir, name the paths you mean.\n"
  );
}

module.exports = {
  COMMON_FIELDS,
  DISCOVERY_ENV,
  RECORD_BUILDERS,
  RECORD_FAMILY,
  RECORD_NAMES,
  RECORD_VERSION,
  SCOPES,
  answersOf,
  bashRecordProblem,
  blockMessage,
  buildRecord,
  commandFileDigest,
  evaluateAnswer,
  filePathProblem,
  findToolUse,
  gitHead,
  questionScope,
  recordAnswers,
  recordDirectory,
  recordLabel,
  recordStatus,
  removeRecord,
  revokeCommand,
  scopeById,
  selfGrantProblem,
  spendToken,
  writeRecord,
};
