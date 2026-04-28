/**
 * commit-guard.js — PreToolUse hook
 *
 * Blocks `git commit` unless a sentinel file exists at:
 *   /tmp/claude-commit-auth-<repo-slug>-<branch-slug>
 *
 * Skills that legitimately commit (oss:resolve, research:run) create this file
 * at the start of their commit phase and delete it afterwards via bash:
 *   touch /tmp/claude-commit-auth-<repo-slug>-<branch-slug>
 *   rm -f /tmp/claude-commit-auth-<repo-slug>-<branch-slug>
 *
 * TTL: 15 min — auto-expires if a skill crashes without cleanup.
 *
 * Exit 0 = allow  |  Exit 2 = block (Claude Code shows stderr as feedback)
 */

"use strict";

const fs = require("fs");
const path = require("path");
const { execSync } = require("child_process");

const TTL_MS = 15 * 60 * 1000; // 15 minutes

function toSlug(s) {
  return s.replace(/[^a-zA-Z0-9]/g, "-").toLowerCase();
}

function getSentinelPath() {
  try {
    const root = execSync("git rev-parse --show-toplevel", {
      encoding: "utf8",
      stdio: ["ignore", "pipe", "ignore"],
    }).trim();
    const repoSlug = toSlug(path.basename(root));
    const branch = execSync("git branch --show-current", {
      encoding: "utf8",
      stdio: ["ignore", "pipe", "ignore"],
    }).trim();
    if (!branch) return null; // detached HEAD
    const branchSlug = toSlug(branch);
    return `/tmp/claude-commit-auth-${repoSlug}-${branchSlug}`;
  } catch {
    return null;
  }
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

  const { tool_name, tool_input } = data;

  if (tool_name !== "Bash") process.exit(0);

  const command = (tool_input && tool_input.command) || "";

  if (!/^\s*git commit\b/.test(command)) process.exit(0);

  const sentinel = getSentinelPath();
  if (!sentinel) {
    process.stderr.write(
      "git commit blocked — could not determine repo/branch for authorization check.\n" +
        "Ensure you are inside a git repository on a named branch (not detached HEAD).\n",
    );
    process.exit(2);
  }

  let stat;
  try {
    stat = fs.statSync(sentinel);
  } catch {
    process.stderr.write(
      `git commit blocked — no commit authorization for this branch.\n` +
        `Skills like /oss:resolve and /research:run set this automatically.\n` +
        `For ad-hoc commits: invoke AskUserQuestion to confirm, ` +
        `then touch ${sentinel} before git commit, rm -f ${sentinel} after.\n`,
    );
    process.exit(2);
  }

  const ageMs = Date.now() - stat.mtimeMs;
  if (ageMs > TTL_MS) {
    try {
      fs.unlinkSync(sentinel);
    } catch {
      // best-effort cleanup
    }
    process.stderr.write(
      `git commit blocked — authorization expired (15-min TTL).\n` +
        `Re-run the skill or touch ${sentinel} after user confirmation.\n`,
    );
    process.exit(2);
  }

  process.exit(0);
});
