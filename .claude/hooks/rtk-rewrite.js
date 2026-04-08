#!/usr/bin/env node
// rtk-rewrite.js — PreToolUse hook
//
// PURPOSE
//   Transparently rewrites Bash commands to their `rtk <cmd>` equivalents,
//   giving 60–99% token savings without requiring duplicate allow entries in
//   settings.json.  Only commands matching an explicit RTK prefix list are
//   rewritten and auto-approved — everything else passes through to normal
//   permission checking untouched.
//
// HOW IT WORKS
//   1. Parse stdin JSON for tool_name and tool_input.command.
//   2. Skip non-Bash tools or commands already prefixed with `rtk` (exit 0).
//   3. Check the command's first word against RTK_PREFIXES.
//   4. If matched, check DENY_PATTERNS — excluded subcommands (git push, branch -D, etc.)
//      exit 0 so normal permission checking and deny rules apply unchanged.
//   5. Otherwise: emit hookSpecificOutput with updatedInput (prepends `rtk `)
//      and permissionDecision:"allow" so no allow-list lookup is needed.
//   6. If not matched: exit 0 — normal permission checking proceeds.
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

// Subcommands that must never be auto-approved even when their prefix is in RTK_PREFIXES.
// These match commands that are intentionally excluded from settings.json allow entries
// or that are covered by settings.json deny entries. Matched commands exit 0 (passthrough)
// so normal permission checking and deny rules apply.
const DENY_PATTERNS = [
  // git: push and destructive subcommands excluded from allow list by design
  /^git\s+(push|branch\s+-[Dd]|branch\s+--delete|reset\s+--hard|clean\s+-[fd]|checkout\s+--)\b/,
  // gh: release/publish actions require explicit user confirmation
  /^gh\s+(release\s+create|pr\s+merge|repo\s+delete)\b/,
  // curl: block mutation methods to prevent auto-approving external state changes
  /^curl\s+(?:.*-X\s+(?:POST|PUT|PATCH|DELETE)|.*--(?:data|data-raw|upload-file))\b/,
];

// Commands RTK knows how to filter (derived from `rtk --help`).
// Each entry is the bare command name (no trailing space).
// Omitted intentionally:
//   test, read, env  — bash builtins; rewriting would break scripts
//   err, log, json, deps, summary, smart  — RTK-only wrappers, not standard CLIs
//   gain, proxy, discover, session, learn, init, config, …  — RTK meta commands
const RTK_PREFIXES = [
  // Version control
  "git",
  "gh",
  // JS / TS
  "tsc",
  "vitest",
  "next",
  "pnpm",
  "npm",
  "prettier",
  "lint",
  "format",
  // Rust
  "cargo",
  // Python
  "ruff",
  "pytest",
  "mypy",
  "pip",
  // Go
  "go",
  "golangci-lint",
  // Ruby
  "rake",
  "rubocop",
  "rspec",
  // Cloud & containers
  "docker",
  "kubectl",
  "aws",
  // Database
  "psql",
  "prisma",
  // .NET
  "dotnet",
  // Network
  "wget",
  "curl",
  // Files & search
  "ls",
  "tree",
  "grep",
  "find",
  "wc",
  "diff",
];

/**
 * Returns true if `cmd` starts with `prefix` as a whole word
 * (exact match or followed by a space).
 */
function matchesPrefix(cmd, prefix) {
  return cmd === prefix || cmd.startsWith(prefix + " ");
}

let raw = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (d) => (raw += d));
process.stdin.on("end", () => {
  try {
    const data = JSON.parse(raw);

    // Only handle Bash tool calls
    if (data.tool_name !== "Bash") {
      process.exit(0);
    }

    const cmd = (data.tool_input && data.tool_input.command) || "";

    // Skip empty commands or those already prefixed
    if (!cmd || cmd.startsWith("rtk ")) {
      process.exit(0);
    }

    // Check against known RTK-filterable prefixes
    if (!RTK_PREFIXES.some((p) => matchesPrefix(cmd, p))) {
      process.exit(0);
    }

    // Safety gate: never auto-approve commands that are intentionally excluded
    // from the allow list or covered by deny rules (e.g. git push, git branch -D).
    if (DENY_PATTERNS.some((p) => p.test(cmd))) {
      process.exit(0);
    }

    // Rewrite and auto-approve — permission check already covered by prefix list
    process.stdout.write(
      JSON.stringify({
        hookSpecificOutput: {
          hookEventName: "PreToolUse",
          permissionDecision: "allow",
          updatedInput: {
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
