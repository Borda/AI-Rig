#!/usr/bin/env node
// rtk-rewrite.js — PreToolUse hook
//
// PURPOSE
//   Transparently rewrites a small, measured set of read-heavy Bash commands
//   to their `rtk <cmd>` equivalents for token savings, without making any
//   permission decision.
//
// SECURITY MODEL — why the rewrite is safe
//   Rewriting a command changes its string. The rewritten form matches the
//   shipped rtk allow mirrors in permissions-allow.json only where the original
//   command already has an allow rule; original-command deny prefixes may not
//   match it. A rewrite carrying permissionDecision:"allow" BYPASSES the deny
//   list entirely. Therefore this hook emits updatedInput without any
//   permission decision; normal permission and deny rules decide.
//
// FIDELITY MODEL — why only one verb is rewritten
//   rtk may elide or reformat output. A command is routed only when a measured
//   comparison against the raw command the agent actually runs (exit code,
//   whole lines, paths) passed and rtk actually saves tokens. Measured with
//   rtk 1.2.0:
//     routed    git status (long form compresses, every path kept; not with
//               -b/--branch: the short `-sb` form is a machine-read format whose
//               `## branch` header rtk rewrites to `* branch`).
//     excluded  ls: rtk cuts a listing by size and by well-known build or
//               environment directory names (node_modules, dist, build, venv),
//               with a `[+N hidden: rtk recall <hash>]` hint outside a sandbox
//               but NO hash inside the Claude or Codex sandbox, where the cut
//               entries are then unrecoverable; several operands also lose their
//               `dir:` headers and options (-l, -a) reformat the listing.
//     excluded  grep: the Bash tool's `grep` is a shell function (ugrep) that
//               honours .gitignore and skips binaries, while `rtk grep` runs
//               the system grep over node_modules, .venv and worktrees, takes
//               a different option dialect (`-P` fails) and elides past 200
//               output lines with whitespace stripped.
//     excluded  lossy: git log (commit cap, body and line cuts, author/date
//               dropped), git diff, git show, wc (drops a shared directory), gh repo view
//               (drops the README), gh pr/issue list (elides rows and drops
//               columns), gh run list (reshaped).
//     excluded  identical output, so no saving and only risk: git blame,
//               ls-tree, ls-files, reflog, rev-list, rev-parse, cat-file,
//               for-each-ref, git grep, git remote, gh release list.
//     excluded  unmeasured: gh view/diff/checks, docker, kubectl, aws, next,
//               cargo, tree.
//   A verb returns to the table only after it passes the same comparison,
//   including a run inside the sandbox, where `rtk recall` has no store.
//
// EXIT CODES
//   0  passthrough (no output) or successful rewrite (JSON to stdout)

"use strict";

const { spawnSync } = require("child_process");

// Bail out silently if rtk is not installed — hook becomes a no-op,
// so removing rtk without touching config doesn't break anything.
if (spawnSync("which", ["rtk"]).status !== 0) {
  process.exit(0);
}

// Programs rewritten on their own prefix. None: `ls` fails the sandbox fidelity check, and `find` and `diff` are
// absent because rtk alters their results or a bare prefix cannot separate a read from `-delete`/`-exec`.
const SAFE_PREFIXES = [];

// Programs that mix read-only and mutating subcommands. Rewritten ONLY when a
// READ_ONLY_GUARDS pattern matches; anything else passes through unchanged to
// normal permission and deny checking on the original string.
const GUARDED_PREFIXES = ["git"];

// Positive allowlist for GUARDED_PREFIXES. Add a pattern only after the verb
// passes the fidelity comparison above; never widen to cover a mutating one.
const READ_ONLY_GUARDS = [
  // git status: long form compresses with every path kept. Not with `-b`/`--branch`: the short `-sb` header
  // `## main...origin/main` is read by scripts and rtk rewrites it to `* main...origin/main`.
  /^git\s+status\b(?!.*\s(?:--branch\b|-[a-zA-Z]*b))/,
];

// Shell control operators that could chain, substitute, or redirect a second
// command after a read-only prefix. A prefix-anchored read-only match proves
// only the FIRST command is safe — `git status && git push` would otherwise get
// the WHOLE string rewritten, changing how the compound is matched.
// Any of these present → refuse to rewrite, so the
// original string reaches normal permission + deny checking intact.
const SHELL_META = /[;&|`\n<>]|\$\(/;

// A quote, backslash, `$` or `{` lets the shell hand git an option the text never spells (`git status "-sb"`,
// `git status -s {-b,}`), so a guard that reads option words would judge words git never receives. Refuse then.
const SHELL_WORD_REWRITE = /["'\\${]/;

/**
 * Returns true if `cmd` starts with `prefix` as a whole word
 * (exact match or followed by a space).
 */
function matchesPrefix(cmd, prefix) {
  return cmd === prefix || cmd.startsWith(prefix + " ");
}

/**
 * Decide whether `cmd` is eligible for rewriting to `rtk <cmd>`.
 * This selects token compression, never permission to execute.
 */
function isRewritable(cmd) {
  // Preserve compound commands for normal matching on their original text.
  if (SHELL_META.test(cmd) || SHELL_WORD_REWRITE.test(cmd)) {
    return false;
  }
  if (SAFE_PREFIXES.some((p) => matchesPrefix(cmd, p))) {
    return true;
  }
  if (GUARDED_PREFIXES.some((p) => matchesPrefix(cmd, p))) {
    return READ_ONLY_GUARDS.some((re) => re.test(cmd));
  }
  return false;
}

let raw = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (d) => (raw += d));
process.stdin.on("end", () => {
  try {
    const data = JSON.parse(raw);

    // Only handle shell-command tool calls.
    if (data.tool_name !== "Bash" && data.tool_name !== "Monitor") {
      process.exit(0);
    }

    const cmd = ((data.tool_input && data.tool_input.command) || "").trim();

    // Skip empty commands or those already prefixed
    if (!cmd || cmd.startsWith("rtk ")) {
      process.exit(0);
    }

    // Only rewrite eligible command prefixes; everything else passes through
    // to normal permission + deny checking on the ORIGINAL string.
    if (!isRewritable(cmd)) {
      process.exit(0);
    }

    // Rewrite without a permission decision so normal checks remain authoritative.
    process.stdout.write(
      JSON.stringify({
        hookSpecificOutput: {
          hookEventName: "PreToolUse",
          updatedInput: {
            ...data.tool_input,
            command: "rtk " + cmd,
          },
        },
      }),
    );
    process.exit(0);
  } catch (_) {
    // Never crash or block Claude due to a hook bug
    process.exit(0);
  }
});
