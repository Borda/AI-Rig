#!/usr/bin/env node
// rtk-rewrite.js — PreToolUse hook
//
// PURPOSE
//   Transparently rewrites read-heavy Bash commands to their `rtk <cmd>`
//   equivalents for token savings, without making any permission decision.
//
// SECURITY MODEL — why the rewrite is safe
//   Rewriting `gh …` to `rtk gh …` changes the command string. The rewritten
//   form matches shipped rtk allow mirrors in permissions-allow.json only
//   where the original command already has an allow rule;
//   original-command deny prefixes may not match it. A rewrite carrying permissionDecision:"allow"
//   BYPASSES the deny list entirely. Therefore this hook emits updatedInput
//   without any permission decision; normal permission and deny rules decide.
//   Two prefix classes limit which commands receive token compression:
//
//     SAFE_PREFIXES    — file/search tools, subject to argument checks below.
//                        Rewritten without a permission decision.
//     GUARDED_PREFIXES — CLIs that mix read and mutating subcommands (git, gh,
//                        docker, kubectl, aws). Rewritten ONLY
//                        when the command matches a READ_ONLY_GUARDS pattern.
//                        These prefix patterns do not prove argument safety.
//                        Anything else — including every unknown or
//                        mutating subcommand — passes through UNCHANGED (exit 0)
//                        to normal permission + deny checking on the original
//                        string. No hand-copied deny mirror to drift.
//
//   Commands whose OUTPUT semantics rtk alters (e.g. `diff` exit status /
//   "identical" summary) are excluded outright — wrapping them corrupts
//   results that callers branch on.
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

// Read-heavy CLIs eligible for compression, with permissions left to the host.
// `diff` is intentionally absent — rtk changes its exit
// status and prints "Files are identical" for differing files, corrupting any
// caller that branches on the result.
//
// `find` is intentionally absent: a bare prefix match cannot tell `find . -name x`
// from `find . -delete` or `find . -exec rm {} +`. The `\;` form of -exec carries a
// semicolon and so trips SHELL_META, but `+` and `-delete` do not — they would be
// rewritten despite their destructive intent.
// `ls`/`tree`/`grep` cover the read-only token payoff without that hole.
const SAFE_PREFIXES = ["ls", "tree", "grep", "wc"];

// CLIs that mix read-only and mutating subcommands. Rewritten
// ONLY when a READ_ONLY_GUARDS pattern matches; otherwise passthrough so the
// original command hits the real allow/deny matcher. curl, wget, psql, and
// prisma are deliberately NOT here — their read/write intent is hard to prove
// from the command line and the token payoff is marginal, so they always
// passthrough (deny list stays authoritative for them).
// `cargo` and `next` sit here rather than in SAFE_PREFIXES: both mix inspection
// with subcommands that execute arbitrary project code or mutate the machine
// (`cargo install`, `cargo run`, `next build`), and a bare prefix match cannot
// separate them.
const GUARDED_PREFIXES = ["git", "gh", "docker", "kubectl", "aws", "cargo", "next"];

// Positive read-only allowlist for GUARDED_PREFIXES. A guarded command is
// rewritten only if it matches one of these. Add patterns here to
// grant token savings to more inspection subcommands — never widen to cover a
// mutating one.
const READ_ONLY_GUARDS = [
  // git — inspection verbs only; add/commit/checkout/merge/fetch passthrough
  // to the real allow list (they are low-output, so no token loss). `branch` is
  // intentionally absent: `git branch <name>` creates a branch (a mutation) and
  // the list output is tiny — no token payoff to justify the risk.
  /^git\s+(status|log|diff|show|rev-parse|rev-list|describe|shortlog|ls-files|ls-tree|merge-base|blame|cat-file|for-each-ref|reflog(?:\s+show\b|$)|whatchanged|grep|tag\s+(?:-l|--list)|tag$|stash\s+list|remote(?:\s+(?:-v|show|get-url))?$|remote\s+-v)\b/,
  // gh — read subcommands + gh api GET (no mutating --method)
  /^gh\s+(issue|pr|release|run|repo|search|cache|workflow)\s+(list|view|diff|checks|status|ls)\b/,
  /^gh\s+api\s+(?!.*(?:-X|--method)\s*(?:POST|PUT|PATCH|DELETE))(?!.*(?:-f|--field|--input)\b)/,
  /^gh\s+(auth\s+status|repo\s+view)\b/,
  // docker — inspection subcommands
  /^docker\s+(ps|images|image\s+ls|logs|inspect|version|info|stats|top|history|port|diff)\b/,
  // kubectl — read verbs
  /^kubectl\s+(get|describe|logs|top|explain|version|api-resources|api-versions|cluster-info|config\s+view)\b/,
  // aws — describe-/get-/list- verbs and `aws s3 ls`
  /^aws\s+\S+\s+(describe|get|list)[\w-]*\b/,
  /^aws\s+s3\s+ls\b/,
  // cargo — inspection only. `check`/`build`/`test` are absent: they execute
  // build.rs, which is arbitrary code. `tree` and `metadata` resolve the
  // dependency graph without building.
  /^cargo\s+(tree|metadata|search|--version|-V)\b/,
  // next — `info` and `telemetry status` report; `build`/`dev`/`start` execute.
  /^next\s+(info|telemetry\s+status)\b/,
];

// Shell control operators that could chain, substitute, or redirect a second
// command after a read-only prefix. A prefix-anchored read-only match proves
// only the FIRST command is safe — `git status && git push` would otherwise get
// the WHOLE string rewritten, changing how the compound is matched.
// Any of these present → refuse to rewrite, so the
// original string reaches normal permission + deny checking intact.
const SHELL_META = /[;&|`\n<>]|\$\(/;

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
  if (SHELL_META.test(cmd)) {
    return false;
  }
  if (/(?:^|\s)["']?--output(?:=|\s|["']?(?:\s|$))/.test(cmd)) return false;
  if (matchesPrefix(cmd, "tree") && /(?:^|\s)["']?-[^-\s]*o/.test(cmd)) return false;
  if (/^git\s+grep\b/.test(cmd) && /(?:^|\s)["']?(?:-[^-\s]*O|--open-files-in-pager(?:=|\s|["']?(?:\s|$)))/.test(cmd))
    return false;
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
