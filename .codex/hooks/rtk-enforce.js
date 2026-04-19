#!/usr/bin/env node
"use strict";

const { spawnSync } = require("child_process");

// Fail open if RTK is not installed.
if (spawnSync("which", ["rtk"]).status !== 0) {
  process.exit(0);
}

const DENY_PATTERNS = [
  /^git\s+(push|branch\s+-[Dd]|branch\s+--delete|reset\s+--hard|clean\s+-[fd]|checkout\s+--)\b/,
  /^gh\s+(release\s+create|pr\s+merge|repo\s+delete)\b/,
  /^curl\s+(?:.*-X\s+(?:POST|PUT|PATCH|DELETE)|.*--(?:data|data-raw|upload-file))\b/,
];

const RTK_PREFIXES = [
  "git",
  "gh",
  "tsc",
  "vitest",
  "next",
  "pnpm",
  "npm",
  "prettier",
  "lint",
  "format",
  "cargo",
  "ruff",
  "pytest",
  "mypy",
  "pip",
  "go",
  "golangci-lint",
  "rake",
  "rubocop",
  "rspec",
  "docker",
  "kubectl",
  "aws",
  "psql",
  "prisma",
  "dotnet",
  "wget",
  "curl",
  "ls",
  "tree",
  "grep",
  "find",
  "wc",
  "diff",
];

function matchesPrefix(cmd, prefix) {
  return cmd === prefix || cmd.startsWith(prefix + " ");
}

let raw = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => {
  raw += chunk;
});
process.stdin.on("end", () => {
  try {
    const data = JSON.parse(raw);
    if (data.tool_name !== "Bash") {
      process.exit(0);
    }

    const cmd = (data.tool_input && data.tool_input.command ? data.tool_input.command : "").trim();
    if (!cmd || cmd.startsWith("rtk ")) {
      process.exit(0);
    }

    if (!RTK_PREFIXES.some((p) => matchesPrefix(cmd, p))) {
      process.exit(0);
    }

    if (DENY_PATTERNS.some((p) => p.test(cmd))) {
      process.exit(0);
    }

    // Codex PreToolUse currently cannot rewrite updatedInput in place. Denying
    // here turns ordinary safe commands into noisy hook failures, so RTK
    // routing is handled by agent instructions instead of enforcement.
    process.exit(0);
  } catch (_err) {
    process.exit(0);
  }
});
