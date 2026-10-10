// commit-guard.js — multi-event hook
//
// PURPOSE
//   Claude must never commit or push autonomously.
//
//   COMMIT: prompt-discipline only, no hook enforcement. Claude asks the
//   `git-approve` question (Approve / Approve always / Deny) before a plain
//   commit unless a skill workflow step, an explicit same-turn request or the
//   user's recorded grant (<git common dir>/claude-git-approval.json, written
//   only by approval-guard.js) covers it — a documented rule
//   (rules/git-commit.md), not a runtime check. The hook does not intercept
//   `git commit` at all, and every other local Git operation and every remote
//   read (fetch, pull, ls-remote, clone) passes it untouched.
//
// GRANT CHECK (`node commit-guard.js --check-grant`)
//   The rule's validity check for the local Git grant; reads no stdin. Prints
//   `grant .git/claude-git-approval.json@<created_at>` and exits 0 only when
//   lib/approval-grants.js accepts the record: the session's own git common dir
//   (discovery env removed, bare repositories refused, named `.git`, linked to
//   its work tree, bound to CLAUDE_PROJECT_DIR when set), a regular non-linked
//   file named exactly so, version 1, scope `local-git-non-destructive`, answer
//   `Approve always`. Otherwise prints `no grant: <reason>` and exits 1. Foundry
//   only: the grant is consumed here, while the writer ships in every plugin.
//
// PUSH AUTHORIZATION (hook-enforced)
//   Force-push is forbidden on every branch, always — a hard, unconditional
//   block. No token bypasses it: the force check runs before any token
//   lookup, so even a valid push token cannot authorize `git push --force`.
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
//   `subtree … push`, `lfs … push` and every `remote-<transport>` helper are
//   pushes; the program name is matched lower-cased (macOS runs `git HTTP-PUSH`).
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
//       …/git/refs/…`) run no git push; gh-write-guard.js blocks them with no
//       approval path.
//     * a `!` shell alias re-lexes the arguments appended to it, so a quoted
//       glob character there reads as unquoted: a false force block only.
//   The settings.json
//   deny entries do not cover it either: Claude Code matches them per
//   subcommand after stripping a fixed wrapper set, never by path, inside
//   `sh -c`, or behind `sudo` (permissions reference, "Wrappers").
//
//   A dry run (`git push --dry-run`/`-n`, `git lfs push --dry-run`) sends
//   nothing: after the force check it passes with no token and spends none —
//   only when every push in the command is one, no other option could undo it
//   and no word is set at run time (`"$X"` may expand to `--no-dry-run`).
//
//   Regular (non-force) push requires a single-use push token, recorded only
//   from the user's own answer: `Approve` to the `git-push` question (options
//   `Approve` / `Deny`) makes approval-guard.js write <git common
//   dir>/claude-push-approval.json, scope `push-once`, with the current
//   branch, HEAD, the approving question's tool_use_id and a 15-minute expiry
//   (lib/approval-grants.js PUSH TOKEN).
//   By the user's decision ("approved branch only") it covers one push of that
//   branch at that HEAD to a configured remote name: `git push`, `git push
//   <remote>`, `-u`, `<remote> HEAD|<branch>|refs/heads/<branch>`, `HEAD:<branch>`,
//   a substitution printing the current branch (`"$(git branch
//   --show-current)"`, read as the branch it is on), `git -c x=y push`,
//   `git lfs push <remote> <branch>`. Never covered, with or without a token —
//   blocked, the token kept, the message telling Claude the user runs it by
//   hand: deleting a remote ref (`:ref`, `--delete`/`-d`), `--prune`,
//   `--all`/`--branches`, `--tags`, `--follow-tags`, any option outside
//   PUSH_COVERED_OPTIONS or any abbreviation (`--del`), another ref on either
//   side of a refspec, a destination that is a URL, a path or an unknown name
//   (configured names from `git remote` in the checkout), a variable or
//   substitution naming the remote or a refspec, `cd`/`pushd`/`popd`/`chdir`
//   anywhere in the command, `-C`/`--git-dir`/`--work-tree`/GIT_DIR, inline
//   config redirecting the push (`-c`/`--config-env` remote.*, url.*, push.*,
//   branch.*, include*; GIT_CONFIG_* variables), and `send-pack`, `http-push`
//   and `subtree push` (a URL or path destination; a split commit, not HEAD).
//   After the force check and the dry-run pass, a push runs only when, in this
//   order: the library loads; the call is no spawned agent's (`agent_id`); the
//   checkout is the session project's (CLAUDE_PROJECT_DIR) on a named branch;
//   the command is one push run once (`repeatProblem`: one push reading with
//   run-time words and word boundaries set aside — the lexer's two readings
//   split `"$(…)"` and `'a b'` differently — one git-and-push segment, no loop, function,
//   `watch`, `parallel` or `xargs`); the push is covered (`uncoveredProblem`);
//   no other git in the command can move HEAD or a branch or redirect the push
//   first (`movingGitProblem`: commit, reset, checkout, update-ref, a config or
//   remote write, …: the approval names the HEAD the user saw); the token is
//   valid, on this branch at this HEAD; the call carries a tool_use_id; and
//   `spendToken` claims it — a rename to a claim named by the call's
//   tool_use_id, of concurrent calls exactly one wins, after proving the
//   approving question and the user's `Approve` in the session transcript. A
//   failed push therefore needs a new approval; an expired token is deleted
//   when found. Any exception on this path exits 2 (an exit 1 would be a
//   non-blocking error). There is no auto-arm: a "push"-mentioning prompt never
//   creates a token, and the former /tmp/claude-push-auth-* sentinel is no
//   longer read.
//   LIMITS: what git reads from config at run time is unseen — a remote
//   re-pointed (`git remote set-url`, `remote.<name>.pushurl`, `url.*.insteadOf`)
//   or given push refspecs (`remote.<name>.push`, `remote.<name>.mirror`,
//   `push.default=matching`) by an earlier Bash call or by the user sends a
//   covered spelling elsewhere or wider; the remote is bound by name, never by
//   URL. A current-branch substitution is read as the branch only when the
//   branch name is one plain shell word (PLAIN_BRANCH); any other branch name
//   keeps it a substitution, which blocks as one. A program other than git that moves HEAD or a ref earlier in the same
//   command (a script, a Makefile, `gh pr checkout`) is beyond the text checks.
//   The token, branch and HEAD are read where the payload `cwd` points (else
//   the hook's own cwd); whether that follows a `cd` made in an earlier Bash
//   call is unverified. A linked worktree of the project is checked against its
//   own branch and HEAD. The shape checks read text: a repeat or second push
//   assembled at run time (an alias or function defined elsewhere, `eval "$X"`)
//   is beyond them, and a commit message naming git push beside a real push
//   reads as a second push (a false block only). SessionStart wipes pending
//   tokens per project, not per session: a second session started in the same
//   project clears the first session's pending push and gh write approvals
//   (fail closed: that session asks again).
//
// HOW IT WORKS
//   1. PreToolUse(Bash): acts only on commands holding a `git push`,
//      `git send-pack`, `git http-push`, `git subtree … push`, `git lfs … push`
//      or a `git remote-<transport>` helper.
//      Force-push forbidden unconditionally (exit 2 before any token
//      lookup); a dry run passes; a push outside the approved branch's push
//      exits 2 with the by-hand advice; otherwise spends a matching push token
//      or exits 2 with the git-push question advice.
//   2. SessionStart: deletes the push token and the gh write token (with their
//      spent claims, lib/approval-grants.js `removeRecord`), so prior-session
//      approval never carries over, and this repository's leftover
//      /tmp/claude-push-auth-<repo-slug>-* sentinels of the former mechanism.
//   3. UserPromptSubmit: /clear → deletes both tokens with their claims.
//
// EXIT CODES
//   0  Allow (a matching push token was spent, a dry run, or command isn't a
//      push); `--check-grant`: a valid grant.
//   1  `--check-grant`: no grant.
//   2  Block — force-push (unconditionally), a push no approval covers, no
//      matching push token, a spawned agent's call, or an exception while
//      checking; stderr shown to Claude.

"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");
const { execFileSync } = require("child_process");

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
// git programs whose own `push` subcommand writes to a remote: `git subtree … push` runs `git push`,
// `git lfs push` uploads LFS objects.
const PUSH_WRAPPERS = ["subtree", "lfs"];
// A `git remote-<transport>` helper reads `push +src:dst` commands on stdin, which no reading sees,
// so it reads as a force push.
const REMOTE_HELPER_PUSH = ["push", "--force"];
// Long options whose any prefix git accepts (`--mir` = `--mirror`). A prefix git finds ambiguous
// (`--f`: `--follow-tags` or `--force`) is refused before anything is pushed, so counting it costs
// nothing. `--follow-tags` itself is a prefix of none of them.
const FORCE_LONG_OPTIONS = ["force", "force-with-lease", "force-if-includes", "mirror"];

// Directory of the former push sentinels (claude-push-auth-*), wiped at SessionStart only.
function getSentinelDir() {
  return process.platform === "win32" ? os.tmpdir() : "/tmp";
}

const LOCAL_GIT_SCOPE = "local-git-non-destructive";
const PUSH_SCOPE = "push-once";
const GH_WRITE_SCOPE = "gh-write-once";

// The lexer's coarse operator split (lib/shell-git.js `coarseSegments`): an operator inside quotes
// only adds a segment, which can only add a count.
const COARSE_OPERATORS = /(?:\|\||&&|[;&|\n`()])/;
// A segment running git with a push-family word after it. `push` inside a ref (`refs/heads/push`,
// `push-fix`) is no push word; `alias.p=push` is.
const GIT_THEN_PUSH = /(?<![\w.-])git(?:\.exe)?(?![\w.-])[\s\S]*?(?<![\w./-])(?:push|send-pack|http-push)(?![\w./-])/i;
// Constructs that run their body more than once: one approval would push again and again.
const LOOP_KEYWORDS = new Set(["for", "while", "until", "select"]);
const REPEAT_RUNNERS = new Set(["watch", "parallel", "xargs"]);
const FUNCTION_DEFINITION = /(?:^|[\s;&|{(])(?:function\s+[\w.-]+|[\w.-]+\s*\(\s*\))\s*\{/;
// git options and variables that run git in another repository than the checkout whose branch and
// HEAD the token names.
const REPOSITORY_OPTION = /^(?:-C|--git-dir|--work-tree)(?:=|$)/;
const REPOSITORY_VARIABLE = /\bGIT_(?:DIR|WORK_TREE|COMMON_DIR)=/;
// git global options that take the next word as their value.
const GLOBAL_OPTIONS_WITH_VALUE = new Set(["-c", "--config-env", "--namespace", "--exec-path", "--attr-source"]);
// Shell builtins that move a later command into another directory, so into another repository.
const DIRECTORY_CHANGERS = ["cd", "pushd", "popd", "chdir"];
// Inline config that changes where a push goes or what it sends: remote.<name>.url/pushurl/push/mirror,
// url.<base>.insteadOf/pushInsteadOf, push.default/followTags, branch.<name>.pushRemote, config includes.
const REDIRECTING_CONFIG = /^(?:remote|url|push|branch|include|includeif)\./i;
// Variables that inject config the same way (GIT_CONFIG_COUNT/_KEY_<n>, GIT_CONFIG_PARAMETERS) or swap a config file
// (GIT_CONFIG_GLOBAL, and HOME / XDG_CONFIG_HOME, where git finds the global one).
const CONFIG_VARIABLE = /\b(?:GIT_CONFIG(?:_[A-Z0-9_]+)?|HOME|XDG_CONFIG_HOME)=/;
// A word whose value the shell sets at run time.
const DYNAMIC_WORD = /[$`]/;

// What one push approval covers (user decision "approved branch only"): one push of the current branch at the
// approved HEAD to a configured remote name. Options one approval may carry; every other one is outside it — the
// deleting, pruning and every-ref ones (`--delete`, `--prune`, `--all`, `--branches`, `--tags`, `--follow-tags`), a
// destination (`--repo`), a program run for the remote (`--receive-pack`, `--exec`), submodule pushes, any
// abbreviation (`--del`, `--pru`). Exact names only: git takes any unambiguous prefix, so a prefix rule would have to
// track git's whole option table (git-push(1), 2.54).
const PUSH_COVERED_OPTIONS = new Set([
  "--set-upstream",
  "--quiet",
  "--verbose",
  "--progress",
  "--no-progress",
  "--atomic",
  "--no-atomic",
  "--porcelain",
  "--verify",
  "--no-verify",
  "--thin",
  "--no-thin",
  "--ipv4",
  "--ipv6",
  "--signed",
  "--no-signed",
  "--push-option",
  "--no-recurse-submodules",
  "--no-force-with-lease",
  "--no-force-if-includes",
  "--no-follow-tags",
]);
// `--option=<value>` forms one approval may carry, with the values allowed (null: any).
const PUSH_COVERED_VALUES = new Map([
  ["--signed", null],
  ["--push-option", null],
  ["--recurse-submodules", new Set(["check", "no"])],
]);
// Long options that take the next word as their value when written without `=`.
const PUSH_VALUE_OPTIONS = new Set(["--push-option", "--repo", "--receive-pack", "--exec"]);
// Short options one approval may carry. `-o` takes a value, `-n` is a dry run, `-d` deletes, `-f` forces.
const PUSH_COVERED_SHORT = new Set(["u", "q", "v", "4", "6"]);
// Substitutions that print the current branch: a push spelled with one names the branch it is on.
const BRANCH_COMMANDS = [
  "git branch --show-current",
  "git rev-parse --abbrev-ref HEAD",
  "git symbolic-ref --short HEAD",
];
const BRANCH_SUBSTITUTION = new RegExp(
  BRANCH_COMMANDS.flatMap((text) => [`"\\$\\(${text}\\)"`, `\\$\\(${text}\\)`, `"\`${text}\`"`, `\`${text}\``]).join(
    "|",
  ),
  "g",
);
// A branch name the shell reads as one plain word, safe to put in place of its substitution.
const PLAIN_BRANCH = /^[A-Za-z0-9._/][A-Za-z0-9._/-]*$/;
// git commands that can move HEAD or a branch, or change where a push goes, before a push later in the same command
// runs: the approval names the HEAD the user saw. Read forms of `branch`, `config`, `remote`, `symbolic-ref` and
// `lfs` are told apart in READ_FORMS.
const MOVING_SUBCOMMANDS = new Set([
  "am",
  "bisect",
  "checkout",
  "cherry-pick",
  "commit",
  "fast-import",
  "fetch",
  "filter-branch",
  "filter-repo",
  "merge",
  "p4",
  "pull",
  "rebase",
  "replace",
  "reset",
  "revert",
  "subtree",
  "svn",
  "switch",
  "update-ref",
]);
const BRANCH_READ_OPTIONS = new Set(["--show-current", "-a", "-r", "-v", "-vv", "-l", "--list", "--all", "--remotes"]);
const CONFIG_READ_OPTIONS = new Set(["--get", "--get-all", "--get-regexp", "--get-urlmatch", "-l", "--list"]);
const firstOperand = (words) => words.find((word) => !word.startsWith("-"));
// True when the arguments of that subcommand only read.
const READ_FORMS = {
  branch: (rest) => rest.every((word) => BRANCH_READ_OPTIONS.has(word)),
  config: (rest) => rest.some((word) => CONFIG_READ_OPTIONS.has(word)) || ["get", "list"].includes(firstOperand(rest)),
  remote: (rest) =>
    rest.every((word) => word === "-v" || word === "--verbose") || ["get-url", "show"].includes(firstOperand(rest)),
  "symbolic-ref": (rest) =>
    !rest.some((word) => ["-d", "--delete", "-m"].includes(word)) &&
    rest.filter((word) => !word.startsWith("-")).length <= 1,
  lfs: (rest) => rest[0] !== "migrate",
};

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
// any token lookup so a valid push token cannot bypass the force block.
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
// Ref deletion (`:ref`, `--delete`, `--prune`) is not a force: it needs a
// push token like any push.
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
//   * `lfs … push` uploads LFS objects to the remote, a remote write like any push; read the same
//     way as `subtree … push`, so a word `push` anywhere after `lfs` counts (fail closed).
//   * any `remote-<transport>` helper is a force push (REMOTE_HELPER_PUSH).
function asPush(args) {
  const [subcommand = "", ...rest] = args;
  const program = subcommand.toLowerCase();
  if (PUSH_SUBCOMMANDS.includes(program)) return ["push", ...rest];
  if (PUSH_WRAPPERS.includes(program) && rest.includes("push")) return ["push", ...rest];
  if (program.startsWith("remote-")) return REMOTE_HELPER_PUSH;
  return null;
}

// The approval-record library, or null when it cannot load. Loaded only where a record is read or
// removed, so a broken install still runs the force check; every push then fails closed.
function loadGrants() {
  try {
    return require(path.join(__dirname, "lib", "approval-grants.js"));
  } catch {
    return null;
  }
}

// Delete this repository's leftover sentinels of the former push mechanism at session start; nothing
// reads them any more. Scoped to the repository, as their names are
// (claude-push-auth-<repo-slug>-<branch-slug>): another project's are removed when a session starts
// there, and a session still running an earlier release keeps its own.
function wipeLegacySentinels(cwd) {
  let slug;
  try {
    const top = execFileSync("git", ["rev-parse", "--show-toplevel"], {
      cwd: cwd || process.cwd(),
      encoding: "utf8",
      stdio: ["ignore", "pipe", "ignore"],
      timeout: 5000,
    });
    slug = path
      .basename(top.trim())
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "");
  } catch {
    return;
  }
  try {
    for (const name of fs.readdirSync(getSentinelDir())) {
      if (slug && name.startsWith(`claude-push-auth-${slug}-`)) {
        fs.rmSync(path.join(getSentinelDir(), name), { force: true });
      }
    }
  } catch {}
}

// Delete the push token and the gh write token with the claims their spent copies left, in the session project's
// common dir, so an approval never outlives its session or a /clear. Only foundry ships this hook; in an install
// without foundry each token's 15-minute expiry bounds it.
function wipeTokens(cwd) {
  const grants = loadGrants();
  for (const scope of [PUSH_SCOPE, GH_WRITE_SCOPE]) {
    try {
      if (grants) grants.removeRecord(scope, { cwd });
    } catch {}
  }
}

// One reading's words as git receives them: quote and backslash-escape characters dropped, the list cut at a comment,
// redirections and their targets left out — so the lexer's precise and coarse readings of one push read alike.
function cleanWords(args) {
  const words = [];
  for (let i = 0; i < args.length; i += 1) {
    const word = args[i].replace(/["'\\]/g, "");
    if (word.startsWith("#")) break;
    if (/^\d*(?:[<>]+|&>+)&?$/.test(word))
      i += 1; // a bare operator: its target is the next word
    else if (!/[<>]/.test(word)) words.push(word);
  }
  return words;
}

// One push reading as a comparable key: its words joined by spaces, words the shell sets at run time left out. The
// precise and coarse readings split a substitution (`"$(…)"` against `"$`) and a quoted space (`'a b'` against `a`,
// `b`) differently, which is no second push; a push inside a substitution is a reading of its own and still counts,
// and every reading still passes the scope check on its own.
function readingKey(args) {
  return cleanWords(args)
    .filter((word) => !DYNAMIC_WORD.test(word))
    .join(" ");
}

// True when a git invocation's global options move it into another repository or work tree.
function switchesRepository(words) {
  for (let i = 1; i < words.length && words[i].startsWith("-"); i += GLOBAL_OPTIONS_WITH_VALUE.has(words[i]) ? 2 : 1) {
    if (REPOSITORY_OPTION.test(words[i])) return true;
  }
  return false;
}

// The config keys a git invocation sets inline: `-c <key>=<value>`, `--config-env[=]<key>=<variable>`.
function inlineConfigKeys(words) {
  const keys = [];
  for (let i = 1; i < words.length && words[i].startsWith("-"); i += 1) {
    const word = words[i].replace(/["']/g, "");
    const next = (words[i + 1] || "").replace(/["']/g, "");
    if (word === "-c" || word === "--config-env") keys.push(next.split("=")[0]);
    else if (word.startsWith("--config-env=")) keys.push(word.slice("--config-env=".length).split("=")[0]);
    if (word === "-c" || word === "--config-env" || GLOBAL_OPTIONS_WITH_VALUE.has(word)) i += 1;
  }
  return keys;
}

// `git push` arguments as git parses them: `--` ends the options, options may follow the operands. Collects the
// operands, every option one approval never covers, and whether the last dry-run option asks for a dry run.
function parsePushWords(words) {
  const parsed = { operands: [], uncovered: [], dryRun: false };
  for (let i = 0; i < words.length; i += 1) {
    const word = words[i];
    if (word === "--") {
      parsed.operands.push(...words.slice(i + 1));
      break;
    }
    let takesNext = false;
    if (word.startsWith("--")) takesNext = readLongOption(word, parsed);
    else if (word.length > 1 && word.startsWith("-")) takesNext = readShortCluster(word, parsed);
    else parsed.operands.push(word);
    if (takesNext) i += 1;
  }
  return parsed;
}

// One long push option into `parsed`; true when it takes the next word as its value.
function readLongOption(word, parsed) {
  const eq = word.indexOf("=");
  const name = eq < 0 ? word : word.slice(0, eq);
  if (name === "--dry-run" || name === "--no-dry-run") {
    if (eq < 0) parsed.dryRun = name === "--dry-run";
    else parsed.uncovered.push(name);
    return false;
  }
  const values = PUSH_COVERED_VALUES.get(name);
  const covered =
    eq < 0
      ? PUSH_COVERED_OPTIONS.has(name)
      : values !== undefined && (values === null || values.has(word.slice(eq + 1)));
  if (!covered) parsed.uncovered.push(name);
  return eq < 0 && PUSH_VALUE_OPTIONS.has(name);
}

// One short push option cluster (`-uq`) into `parsed`; true when its `-o` takes the next word as its value. `-o`
// takes the rest of the cluster when there is one (`-on`: push option `n`, no dry run).
function readShortCluster(word, parsed) {
  const cluster = word.slice(1);
  const valueAt = cluster.indexOf("o");
  for (const letter of valueAt < 0 ? cluster : cluster.slice(0, valueAt)) {
    if (letter === "n") parsed.dryRun = true;
    else if (!PUSH_COVERED_SHORT.has(letter)) parsed.uncovered.push(`-${letter}`);
  }
  return valueAt === cluster.length - 1;
}

// True when this push reading sends nothing: `git push --dry-run`/`-n`, `git lfs push --dry-run`. Only with no
// option that could undo it and no word set at run time, which may expand to `--no-dry-run`.
function isDryRun(reading) {
  const words = cleanWords(reading.slice(1));
  if (words.some((word) => DYNAMIC_WORD.test(word))) return false;
  const program = reading[0].toLowerCase();
  if (program === "lfs") {
    const options = words.slice(1).filter((word) => word.startsWith("-"));
    return words[0] === "push" && options.length > 0 && options.every((word) => word === "--dry-run");
  }
  if (program !== "push") return false;
  const parsed = parsePushWords(words);
  return parsed.dryRun && parsed.uncovered.length === 0;
}

// Why the push's destination word is not a configured remote name, or null. No word: git picks a configured remote.
function remoteProblem(remote, scope) {
  if (remote === undefined) return null;
  if (DYNAMIC_WORD.test(remote)) return "a variable or substitution names the remote";
  return scope.remotes.includes(remote)
    ? null
    : "the destination is not a configured remote name (a URL, a path or an unknown name)";
}

// Why one refspec is not the current branch pushed to its own name, or null: source `HEAD`, `<branch>` or
// `refs/heads/<branch>`; destination, when given, `<branch>` or `refs/heads/<branch>`.
function refspecProblem(spec, scope, destinationAllowed) {
  if (DYNAMIC_WORD.test(spec)) return "a variable or substitution names a refspec";
  const names = [scope.branch, `refs/heads/${scope.branch}`];
  const colon = spec.indexOf(":");
  const source = colon < 0 ? spec : spec.slice(0, colon);
  if (source === "") return "an empty source refspec deletes the remote ref";
  if (source !== "HEAD" && !names.includes(source)) {
    return `the refspec pushes another ref than the current branch ${scope.branch}`;
  }
  if (colon >= 0 && !(destinationAllowed && names.includes(spec.slice(colon + 1)))) {
    return `the refspec updates another remote ref than the current branch ${scope.branch}`;
  }
  return null;
}

// Why one push reading is outside what an approval covers, or null.
function readingScopeProblem(reading, scope) {
  const program = reading[0].toLowerCase();
  const words = cleanWords(reading.slice(1));
  if (program === "lfs") {
    if (words[0] !== "push") return "`git lfs` options before `push` are not covered";
    const options = words.slice(1).filter((word) => word.startsWith("-"));
    if (options.length > 0) return `it carries ${options.join(" ")}, which a push approval never covers`;
    const [remote, ...refs] = words.slice(1);
    return remoteProblem(remote, scope) || refs.map((ref) => refspecProblem(ref, scope, false)).find(Boolean) || null;
  }
  if (program !== "push") return `\`git ${program}\` is not a push of the current branch to a configured remote`;
  const parsed = parsePushWords(words);
  if (parsed.uncovered.length > 0) {
    return `it carries ${parsed.uncovered.join(" ")}, which a push approval never covers`;
  }
  const [remote, ...refspecs] = parsed.operands;
  return (
    remoteProblem(remote, scope) || refspecs.map((spec) => refspecProblem(spec, scope, true)).find(Boolean) || null
  );
}

// Why no approval can cover this command — the user runs it by hand — or null: a directory change, another
// repository, inline config redirecting the push, or a push reading outside the approved branch's push.
function uncoveredProblem(command, pushReadings, scope) {
  for (const name of DIRECTORY_CHANGERS) {
    const found = programInvocations(command, name);
    if (found === null) return "the command holds text the guard cannot read for a directory change";
    if (found.length > 0) return `the command changes directory (${name}), so the push may run in another repository`;
  }
  const gits = programInvocations(command, "git");
  if (REPOSITORY_VARIABLE.test(command) || gits.some(switchesRepository)) {
    return "the push points git at another repository or work tree (-C, --git-dir, --work-tree, GIT_DIR)";
  }
  if (CONFIG_VARIABLE.test(command)) {
    return "a GIT_CONFIG, HOME or XDG_CONFIG_HOME assignment swaps config that can change where the push goes";
  }
  const key = gits.flatMap(inlineConfigKeys).find((name) => REDIRECTING_CONFIG.test(name));
  if (key) return `inline config ${key} can change where the push goes or what it sends`;
  return pushReadings.map((reading) => readingScopeProblem(reading, scope)).find(Boolean) || null;
}

// Why this command is not one push run once, or null: one push reading, once its quoting, comment, redirections and
// run-time words are set aside; one segment running git with a push word (split at operators as the lexer's coarse
// pass does: `git push; git push` reads the same twice); no loop, function definition, `watch`, `parallel` or `xargs`.
function repeatProblem(command, pushReadings) {
  if (new Set(pushReadings.map(readingKey)).size > 1) return "the command holds more than one push";
  const segments = command.split(COARSE_OPERATORS);
  if (segments.filter((segment) => GIT_THEN_PUSH.test(segment)).length > 1) {
    return "the command holds more than one push";
  }
  const words = command.split(/[\s;&|()`{}]+/).map((word) => word.replace(/["']/g, ""));
  const leading = segments.map((segment) => segment.trim().split(/\s+/)[0] || "");
  if (
    leading.some((word) => LOOP_KEYWORDS.has(word)) ||
    words.some((word) => REPEAT_RUNNERS.has(path.basename(word).toLowerCase())) ||
    FUNCTION_DEFINITION.test(command)
  ) {
    return "the command repeats what it runs (a loop, a function, watch, parallel or xargs), so it could push again";
  }
  return null;
}

// Why another git command beside the push may move HEAD or a branch, or redirect the push, before it runs, or null.
// The approval names the HEAD the user saw: that command runs in its own Bash call first, then the push is asked for.
function movingGitProblem(otherReadings) {
  for (const reading of otherReadings) {
    const [subcommand = "", ...rest] = cleanWords(reading);
    const name = subcommand.toLowerCase();
    const readForm = READ_FORMS[name];
    if (MOVING_SUBCOMMANDS.has(name) || (readForm && !readForm(rest))) {
      return `the command also runs \`git ${name}\`, which can move HEAD or a branch, or redirect the push, before it runs`;
    }
  }
  return null;
}

// The configured remote names of the checkout at `cwd`, listed with git's discovery variables removed, as the
// library resolves the checkout; none when git cannot list them.
function configuredRemotes(cwd, grants) {
  const env = {};
  for (const [key, value] of Object.entries(process.env)) {
    if (!grants.DISCOVERY_ENV.includes(key.toUpperCase())) env[key] = value;
  }
  try {
    return execFileSync("git", ["remote"], {
      cwd: cwd || process.cwd(),
      env,
      encoding: "utf8",
      stdio: ["ignore", "pipe", "ignore"],
      timeout: 5000,
    })
      .split(/\r?\n/)
      .filter(Boolean);
  } catch {
    return [];
  }
}

// Why the push token does not allow this call, or null after spending it: a token of the session project, valid, on
// the current branch at the current HEAD, claimed once for this call's tool_use_id with the approving answer proved
// in the transcript (lib/approval-grants.js `spendToken`: of concurrent calls exactly one claims it).
function tokenProblem(grants, here, data) {
  const status = grants.recordStatus(PUSH_SCOPE, { cwd: data.cwd });
  if (!status.ok) {
    if (status.expired) fs.rmSync(status.file, { force: true });
    return status.reason;
  }
  if (here.branch !== status.record.branch) return "the current branch is not the one the push was approved on";
  if (here.head !== status.record.head) return "HEAD moved since the push was approved";
  if (typeof data.tool_use_id !== "string" || data.tool_use_id === "") {
    return "the payload carries no tool_use_id to spend the push approval with";
  }
  const spent = grants.spendToken(PUSH_SCOPE, {
    cwd: data.cwd,
    callId: data.tool_use_id,
    expected: { branch: here.branch, head: here.head },
    transcriptPath: data.transcript_path,
  });
  return spent.ok ? null : spent.reason;
}

// The verdict on a non-force push, or null after spending the token. `kind` picks the advice: `ask` (the git-push
// question can cover it), `byHand` (no approval can: the user runs it), `agent` (a spawned agent's call). Order:
// the library, the caller, the checkout, the command's shape, its scope, git beside it, then the token. Reasons never
// echo token values.
function pushVerdict(command, data) {
  const grants = loadGrants();
  if (!grants)
    return { kind: "ask", reason: "commit-guard cannot load hooks/lib/approval-grants.js to read the push token" };
  if (data.agent_id) return { kind: "agent", reason: "a spawned agent never spends the user's push approval" };
  const where = grants.recordDirectory(data.cwd);
  if (where.reason) return { kind: "ask", reason: where.reason };
  const here = grants.gitHead(data.cwd);
  if (here.reason) return { kind: "ask", reason: here.reason };
  const scoped = PLAIN_BRANCH.test(here.branch) ? command.replace(BRANCH_SUBSTITUTION, here.branch) : command;
  const readings = gitSubcommandArgs(scoped);
  const pushReadings = readings.filter((args) => asPush(args) !== null);
  const repeated = repeatProblem(scoped, pushReadings);
  if (repeated) return { kind: "ask", reason: repeated };
  const scope = { branch: here.branch, remotes: configuredRemotes(data.cwd, grants) };
  const uncovered = uncoveredProblem(scoped, pushReadings, scope);
  if (uncovered) return { kind: "byHand", reason: uncovered };
  const moving = movingGitProblem(readings.filter((args) => asPush(args) === null));
  if (moving) return { kind: "ask", reason: moving };
  const token = tokenProblem(grants, here, data);
  return token ? { kind: "ask", reason: token } : null;
}

const PUSH_ADVICE = {
  ask:
    "Pushes are never auto-armed: each push needs the user's own approval. Invoke one AskUserQuestion with header " +
    "`git-push` and options exactly `Approve` / `Deny`, whose question shows the branch, the target and the command " +
    "you will run. The user's `Approve` records a single-use token (claude-push-approval.json in the git common dir, " +
    "15 min) for one push of this branch at this HEAD to a configured remote — `git push [-u] [<remote>] " +
    "[HEAD|<branch>][:<branch>]`, remote and branch written out — run from this checkout as the only push in its Bash " +
    "command, beside no git that moves HEAD. Never create, copy or edit the token yourself.",
  byHand:
    "A push approval covers only one push of the current branch at the approved HEAD to a configured remote name — " +
    "`git push [-u] [<remote>] [HEAD|<branch>][:<branch>]`, remote and branch written out, run from this checkout — " +
    "so no `git-push` answer can cover this one; do not ask for it. Give the user the exact command to run by hand in " +
    "their own shell, or write the push in that plain form when a plain push of this branch is what the task needs.",
  agent: "Only the lead session spends the user's push approval: hand the push back to it.",
  fault:
    "commit-guard fails closed: no push runs until it can check the approval. Retry; if it persists, reinstall the " +
    "foundry plugin.",
};

// The rule's grant check: one line on stdout, exit 0 for a valid grant, 1 otherwise.
function runGrantCheck() {
  const grants = loadGrants();
  const status = grants
    ? grants.recordStatus(LOCAL_GIT_SCOPE, { cwd: process.cwd() })
    : { ok: false, reason: "commit-guard cannot load hooks/lib/approval-grants.js" };
  process.stdout.write(status.ok ? `${status.line}\n` : `no grant: ${status.reason}\n`);
  process.exitCode = status.ok ? 0 : 1;
}

let raw = "";
function handlePayload() {
  let data;
  try {
    data = JSON.parse(raw);
  } catch {
    process.exit(0);
  }

  const { hook_event_name, tool_name, tool_input } = data;

  // --- SessionStart: no push approval carries over from a prior session ---
  if (hook_event_name === "SessionStart") {
    wipeTokens(data.cwd);
    wipeLegacySentinels(data.cwd);
    process.exit(0);
  }

  // --- UserPromptSubmit: wipe on /clear ---
  if (hook_event_name === "UserPromptSubmit") {
    const prompt = (data.prompt || data.user_message || "").trim();
    if (/^\/clear\b/.test(prompt)) wipeTokens(data.cwd);
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
  const readings = gitSubcommandArgs(command).filter((args) => asPush(args) !== null);
  if (readings.length === 0) process.exit(0);

  // Force-push is forbidden on any branch, always — checked before any
  // token lookup, so a valid push token never bypasses it.
  if (readings.map(asPush).some(isForcePush)) {
    const info = {};
    programInvocations(command, "git", info);
    // The lexer adds a synthetic force reading for syntax it cannot read exactly, so this branch also catches commands
    // naming no git at all (`echo ${(U)HOME}`): name the syntax, never a push or a force ban.
    if (info.unreadable) {
      const construct = info.unreadableReason || "quoting bash and zsh read differently";
      process.stderr.write(
        `Bash command blocked — it holds shell syntax the guards cannot read exactly: ${construct}. Syntax that ` +
          "bash and zsh read differently, or code no string inspection sees, may hide a git push. Rewrite the " +
          "command without that syntax.\n",
      );
      process.exit(2);
    }
    process.stderr.write(
      `git push blocked — force-push is forbidden on any branch. No override, no token bypasses this.\n`,
    );
    process.exit(2);
  }

  // A dry run sends nothing: no token needed, none spent.
  if (readings.every(isDryRun)) process.exit(0);

  // Any exception from here on exits 2: exit 1 is a non-blocking error, which would let the push run.
  let verdict;
  try {
    verdict = pushVerdict(command, data);
  } catch (error) {
    verdict = { kind: "fault", reason: `the push approval check failed (${(error && error.message) || error})` };
  }
  if (verdict) {
    process.stderr.write(`git push blocked — ${verdict.reason}.\n${PUSH_ADVICE[verdict.kind]}\n`);
    process.exit(2);
  }

  process.exit(0);
}

// `--check-grant` reads no stdin, so no payload reaches anything in that mode.
if (process.argv[2] === "--check-grant") {
  runGrantCheck();
} else {
  process.stdin.on("data", (chunk) => (raw += chunk));
  process.stdin.on("end", handlePayload);
}
