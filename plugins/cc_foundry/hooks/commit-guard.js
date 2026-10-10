// commit-guard.js — multi-event hook
//
// PURPOSE
//   Claude must never commit or push autonomously.
//
//   COMMIT: prompt-discipline only, no hook enforcement. Claude must invoke
//   AskUserQuestion before every `git commit`, any branch, no exceptions —
//   this is a documented rule (rules/git-commit.md), not a runtime check.
//   The hook does not intercept `git commit` at all.
//
// PUSH AUTHORIZATION (hook-enforced)
//   Force-push is forbidden on every branch, always — a hard, unconditional
//   block. No sentinel bypasses it: the force check runs before any sentinel
//   lookup, so even a valid push sentinel cannot authorize `git push --force`.
//
//   Detection is on what the command does, not how it is spelled
//   (lib/shell-git.js). Every word naming git starts an invocation wherever it
//   sits, so grouping and prefixes cannot hide one (`{ git push; }`, `if x;
//   then git push; fi`, `time|sudo -u u|nohup|xargs git push`, `env -u V git
//   push`, `/usr/bin/git push`); the name ignores case (`GIT`, `Git.exe` run
//   git on macOS and Windows). Quoting and brace expansion are resolved (`git
//   -C "dir with space" push`, `git {push,} -f`); operators, substitutions and
//   shell source handed to another program are followed (`cd /x && git push`,
//   `echo "$(git push)"`, `bash -c "git push"`, `eval '…'`, `bash <<< '…'`);
//   `python3 -c`, `perl -e`, `ruby -e` and `node -e` code naming git and push
//   reads as a push carrying every token of that code. git's global options
//   are stripped (`git -C /path push`), an unknown one read both as flag and
//   as value-taking; an inline alias (`git -c alias.p=push p`, `--config-env
//   alias.p=VAR`) is expanded, and one whose value is set at run time reads
//   as a force push, as does every subcommand under an enabling inline
//   `help.autocorrect` (git runs its guess for a typo: `-c
//   help.autocorrect=1 pus -f`). `send-pack` (push's plumbing), `http-push`,
//   `subtree … push` and every `remote-<transport>` helper are pushes; the
//   program name is matched lower-cased (macOS runs `git HTTP-PUSH`).
//   Force spellings: a short cluster carrying f (`git push -fu`); any `--force*`
//   and any prefix git accepts for a force option (`--mir`, `--force-w`; an
//   ambiguous one such as `--f` git refuses anyway); a `+`-prefixed refspec
//   (`git push origin +main`, also under `subtree push`, which strips it:
//   fail closed), which names no force flag at all; an unquoted glob argument
//   (`git push origin -?` after `touch ./-f`), read as `--force` beside its
//   literal reading; a `remote-<transport>` helper, whose `push +src:dst`
//   commands arrive on stdin.
//   Fail closed: a command whose quoting the lexer cannot follow is scanned for
//   any `git` word, every glob character in it counting as unquoted, and
//   unquoted prose naming one (`echo git push -f`) is treated as the
//   invocation it spells. Quoting bash and zsh read differently (`$"…"`, most
//   `$'…'` escapes, bash 5.3's `${ cmd; }`, a spliced backslash-newline;
//   lib/shell-git.js step 5) may hide
//   the push itself, so a
//   command holding it is blocked with its own message. If the library itself
//   cannot load, every command naming both `git` and a push command or a
//   `remote-` helper is blocked.
//   LIMITS: a push assembled at run time — via a variable (`git push
//   "$FLAGS"`), an alias or include from a config file, or a script on disk —
//   is beyond what any string inspection can see, and some string-visible
//   spellings are accepted limits by decision (assigned values, commands git runs from config, Windows shells, globs naming the
//   program or subcommand, environment aliases, inline `remote.*` config,
//   dashed programs; full list in lib/shell-git.js LIMITS). Also outside this
//   guard, with no guard here:
//     * third-party `git-<name>` programs on PATH that push under their own
//       subcommand (`git town sync`, `git machete traverse`): an unbounded set.
//     * GitHub API force updates (`gh repo sync --force`, `gh api … -X PATCH
//       …/git/refs/…`) run no git push; gh-write-guard.js hard-denies them.
//     * a `!` shell alias re-lexes the arguments appended to it, so a quoted
//       glob character there reads as unquoted: a false force block only.
//   The settings.json
//   deny entries do not cover it either: Claude Code matches them per
//   subcommand after stripping a fixed wrapper set, never by path, inside
//   `sh -c`, or behind `sudo` (permissions reference, "Wrappers").
//
//   Deleting remote refs is a regular push, not a force push: `git push origin
//   :ref`, `--delete`/`-d` and `--prune` pass the force check and still need
//   the sentinel and an AskUserQuestion confirmation like any other push.
//
//   Regular (non-force) `git push` requires a per-branch sentinel:
//     /tmp/claude-push-auth-<repo-slug>-<branch-slug>  (15-min TTL)
//   There is no auto-arm shortcut — a "push"-mentioning prompt never creates
//   it. The push sentinel can only be created by the user's own shell
//   (`! touch ...`) after Claude has confirmed the push via AskUserQuestion.
//   A Claude-run touch of an auth sentinel is read by the harness classifier
//   as forging the guard, so Claude must never create it itself.
//
// HOW IT WORKS
//   1. PreToolUse(Bash): acts only on commands holding a `git push`,
//      `git send-pack`, `git http-push`, `git subtree … push` or a
//      `git remote-<transport>` helper.
//      Force-push forbidden unconditionally (exit 2 before any sentinel
//      check); otherwise checks the push sentinel present and fresh.
//   2. SessionStart: wipes all /tmp/claude-push-auth-* sentinels so
//      prior-session auth never carries over.
//   3. UserPromptSubmit: /clear → wipes all sentinel files for the repo.
//
// EXIT CODES
//   0  Allow (push sentinel present and fresh, or command isn't `git push`).
//   2  Block — push sentinel missing/expired, or push is a force-push
//      (force-push blocked unconditionally); stderr shown to Claude.

"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");
const { execSync } = require("child_process");

// Loaded defensively: a guard that crashed on a missing module would exit 1, which Claude Code treats
// as a non-blocking error — the push would go through. Without the parser, pushes fail closed below.
let gitSubcommandArgs = null;
let programInvocations = null;
try {
  ({ gitSubcommandArgs, programInvocations } = require(path.join(__dirname, "lib", "shell-git.js")));
} catch {
  // gitSubcommandArgs stays null
}

// git's commands that update refs on a remote: `send-pack` is push's plumbing, `http-push` its
// WebDAV transport.
const PUSH_SUBCOMMANDS = ["push", "send-pack", "http-push"];
// A `git remote-<transport>` helper reads `push +src:dst` commands on stdin, which no reading sees,
// so it reads as a force push.
const REMOTE_HELPER_PUSH = ["push", "--force"];
// Long options whose any prefix git accepts (`--mir` = `--mirror`). A prefix git finds ambiguous
// (`--f`: `--follow-tags` or `--force`) is refused before anything is pushed, so counting it costs
// nothing. `--follow-tags` itself is a prefix of none of them.
const FORCE_LONG_OPTIONS = ["force", "force-with-lease", "force-if-includes", "mirror"];

function getSentinelDir() {
  return process.platform === "win32" ? os.tmpdir() : "/tmp";
}

const TTL_MS = 15 * 60 * 1000; // 15 min — push sentinel

function toSlug(s) {
  return s
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

function runGit(cmd) {
  return execSync(cmd, { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] }).trim();
}

function getRepoSlug() {
  try {
    return toSlug(path.basename(runGit("git rev-parse --show-toplevel")));
  } catch {
    return null;
  }
}

function getCurrentBranch() {
  try {
    return runGit("git branch --show-current") || null; // empty = detached HEAD
  } catch {
    return null;
  }
}

function getPushSentinelPath(repoSlug, branchSlug) {
  return `${getSentinelDir()}/claude-push-auth-${repoSlug}-${branchSlug}`;
}

// Of git push's short options only `-f` uses the letter f, so any short cluster
// containing it is a force. A long option is a force when its name (before any
// `=`) starts with `force` or is a prefix of a FORCE_LONG_OPTIONS entry; never a
// substring test, since `--follow-tags` also contains an f and is not a force.
function isForceFlag(token) {
  if (token.startsWith("--")) {
    const name = token.slice(2).split("=")[0];
    return name.startsWith("force") || (name !== "" && FORCE_LONG_OPTIONS.some((option) => option.startsWith(name)));
  }
  return token.startsWith("-") && token.length > 1 && token.includes("f");
}

// A push carrying force in any spelling can never be authorized — checked before
// any sentinel so a valid push sentinel cannot bypass the force block.
//
// Spellings caught:
//   * `-f`, and any short cluster carrying it (`git push -fu origin main`)
//   * any `--force*` (--force, --force-with-lease, --force-if-includes) and
//     any abbreviation git accepts for one (`--force-w`, `--for`)
//   * `--mirror` and its abbreviations (`--mir`, `--m`) — mirrors every local
//     ref onto the remote, force-updating and deleting remote refs as needed
//   * a `+`-prefixed refspec (`git push origin +main`) — a force push that
//     names no force flag at all.
//
// `pushArgs` is one invocation's arguments from `push` on, as lib/shell-git.js
// reads them — prefix, grouping, quoting, brace expansion, inline aliases and
// git's global options already resolved.
// Ref deletion (`:ref`, `--delete`, `--prune`) is not a force: it stays
// sentinel-gated like any push.
function isForcePush(pushArgs) {
  const args = pushArgs.slice(1);
  if (args.some(isForceFlag)) return true;
  // Refspecs are the non-flag arguments after the remote. A leading `+` on any
  // of them requests a non-fast-forward update — a force push by another name.
  return args.some((t) => t.startsWith("+"));
}

// One reading's push arguments from `push` on, or null when it pushes nothing. git runs a program
// that is not a builtin by file name, so on a case-insensitive file system `git HTTP-PUSH`,
// `git SUBTREE` and `git REMOTE-HTTPS` run the push programs: the name is matched lower-cased.
//   * `subtree … push` runs `git push`; its arguments are read for force. git-subtree strips a
//     refspec's leading `+`, but the reading still counts it as force: fail closed.
//   * any `remote-<transport>` helper is a force push (REMOTE_HELPER_PUSH).
function asPush(args) {
  const [subcommand = "", ...rest] = args;
  const program = subcommand.toLowerCase();
  if (PUSH_SUBCOMMANDS.includes(program)) return ["push", ...rest];
  if (program === "subtree" && rest.includes("push")) return ["push", ...rest];
  if (program.startsWith("remote-")) return REMOTE_HELPER_PUSH;
  return null;
}

function checkSentinel(sentinelPath, ttlMs) {
  try {
    const stat = fs.statSync(sentinelPath);
    const ageMs = Date.now() - stat.mtimeMs;
    if (ageMs > ttlMs) {
      try {
        fs.unlinkSync(sentinelPath);
      } catch {}
      return "expired";
    }
    return "valid";
  } catch {
    return "missing";
  }
}

// Wipe all push-auth sentinel files for a given prefix pattern.
function wipeSentinels(prefix) {
  try {
    const files = fs.readdirSync(getSentinelDir());
    for (const f of files) {
      const isPushAuth = prefix ? f.startsWith(`claude-push-auth-${prefix}-`) : f.startsWith("claude-push-auth-");
      if (isPushAuth) {
        try {
          fs.unlinkSync(path.join(getSentinelDir(), f));
        } catch {}
      }
    }
  } catch {}
}

let raw = "";
process.stdin.on("data", (chunk) => (raw += chunk));
process.stdin.on("end", () => {
  let data;
  try {
    data = JSON.parse(raw);
  } catch {
    process.exit(0);
  }

  const { hook_event_name, tool_name, tool_input } = data;

  // --- SessionStart: wipe leftover sentinels from prior sessions ---
  if (hook_event_name === "SessionStart") {
    const repoSlug = getRepoSlug();
    if (repoSlug) wipeSentinels(repoSlug);
    process.exit(0);
  }

  // --- UserPromptSubmit: wipe on /clear ---
  if (hook_event_name === "UserPromptSubmit") {
    const prompt = (data.prompt || data.user_message || "").trim();

    if (/^\/clear\b/.test(prompt)) {
      const repoSlug = getRepoSlug();
      if (repoSlug) wipeSentinels(repoSlug);
    }

    process.exit(0);
  }

  // --- PreToolUse: guard git push (git commit is prompt-discipline only) ---
  if (tool_name !== "Bash") process.exit(0);

  const command = (tool_input && tool_input.command) || "";
  if (gitSubcommandArgs === null) {
    if (!/\bgit\b[\s\S]*(?:\b(?:push|send-pack|http-push)\b|\bremote-\w)/i.test(command)) process.exit(0);
    process.stderr.write(
      "git push blocked — commit-guard cannot load hooks/lib/shell-git.js to inspect the command.\n" +
        "Reinstall the foundry plugin; until then every command naming git and push is blocked.\n",
    );
    process.exit(2);
  }
  // Anchoring on /^\s*git push\b/ would miss `git -C /path push`, a push after
  // a shell operator, and one inside a group or behind a prefix; all reach the
  // same remote. Inspect every invocation the library finds instead.
  const pushes = gitSubcommandArgs(command)
    .map(asPush)
    .filter((args) => args !== null);
  if (pushes.length === 0) process.exit(0);

  // Force-push is forbidden on any branch, always — checked before any
  // sentinel, so a valid push sentinel never bypasses it.
  if (pushes.some(isForcePush)) {
    const info = {};
    programInvocations(command, "git", info);
    if (info.unreadable) {
      process.stderr.write(
        "git push blocked — this command holds quoting bash and zsh read differently (lib/shell-git.js step 5: " +
          "$\"…\", a $'…' escape other than a named one, octal or \\xHH, $$ before a quote, brace or paren, a quote " +
          "inside a double-quoted ${…}, bash 5.3's ${ cmd; } or ${|cmd;}, a backslash-newline the shells splice " +
          "before the lexer reads it, text with a backslash printed to a shell reading stdin), which may hide a " +
          "push; and force-push " +
          "is forbidden on any branch either " +
          "way. Rewrite it without that quoting.\n",
      );
      process.exit(2);
    }
    process.stderr.write(
      `git push blocked — force-push is forbidden on any branch. No override, no sentinel bypasses this.\n`,
    );
    process.exit(2);
  }

  const repoSlug = getRepoSlug();
  const branch = getCurrentBranch();

  if (!repoSlug || !branch) {
    process.stderr.write(
      "git push blocked — could not determine repo/branch for authorization check.\n" +
        "Ensure you are inside a git repository on a named branch (not detached HEAD).\n",
    );
    process.exit(2);
  }

  const branchSlug = toSlug(branch);
  const pushSentinel = getPushSentinelPath(repoSlug, branchSlug);
  const pushStatus = checkSentinel(pushSentinel, TTL_MS);
  if (pushStatus !== "valid") {
    const reason =
      pushStatus === "expired" ? "authorization expired (15-min TTL)" : "no push authorization for this branch";
    process.stderr.write(
      `git push blocked — ${reason}.\n` +
        `Pushes are never auto-armed. Invoke AskUserQuestion to confirm the push,\n` +
        `then ask the user to authorize from their own shell (Claude may not touch the sentinel —\n` +
        `the harness classifier reads a Claude-run touch as forging the guard):\n` +
        `  ! touch ${pushSentinel}\n` +
        `Then run git push. After push, the user removes it:\n` +
        `  ! rm -f ${pushSentinel}\n`,
    );
    process.exit(2);
  }

  process.exit(0);
});
