#!/usr/bin/env node
// write-guard.js — PreToolUse hook (matchers: Edit, Write, NotebookEdit)
//
// PURPOSE
//   Ship the PROTECT list in the plugin instead of re-adding an ALLOW list by
//   hand to every repository's settings.local.json. Allow rules are inherently
//   project-specific (one repo's sources live in `src/`, another's in `lib/`),
//   so they drift per machine and per project and never generalize. The set of
//   files where an unreviewed agent write actually costs something is the same
//   in essentially every repository, which is what makes it shippable policy.
//
//   This hook grants NOTHING. It forces a confirmation on writes to that
//   protected set; everything else is silent passthrough, so the session's own
//   permission mode still governs the ordinary case. Pair it with `acceptEdits`
//   to remove routine friction while untracked CI definitions, lockfiles and
//   agent instructions, and all permission config, still stop you.
//
// SECURITY MODEL — deliberately the inverse of blueprint-allow.js
//   blueprint-allow.js can safely emit "allow" because it has PROVENANCE: the
//   exact command text came from a reviewed, versioned plugin file, so allowing
//   it means "you already reviewed this". An Edit has no provenance — its
//   content is arbitrary new code. So this hook never emits "allow"; auto-
//   allowing writes by directory convention would just be acceptEdits with
//   worse visibility. Guard, do not grant.
//
// WHAT IS PROTECTED, AND WHY THESE
//   Only files that are (a) named identically across essentially every repo,
//   and (b) costly when written without review: CI definitions, agent
//   instructions (the file that tells the next agent what to do), release
//   metadata, dependency lockfiles, and Claude's own permission config.
//   Source and tests are deliberately NOT protected — that is the routine work.
//
// VERSION-CONTROLLED FILES PASS THROUGH
//   A protected file already tracked by git (present in the index) is not
//   gated: every write to it shows up in `git diff` and is revertible, which is
//   the review the confirmation was standing in for. Asking on each tracked
//   write only produced a per-edit prompt the user could never dismiss, since a
//   hook "ask" is not remembered across calls. Untracked, new and gitignored
//   protected files (e.g. `.claude/settings.local.json`) still ask — nothing
//   else records or reverts a write to them. Any failure of the git probe (no
//   git, not a repository, timeout) counts as untracked, so the guard keeps
//   asking rather than silently opening.
//   Exception: entries marked `always` (`.claude/settings*.json`,
//   `.pre-commit-config.yaml`) ask even when tracked. A new allow rule or hook
//   takes effect when settings load, and a changed commit gate runs on the next
//   commit — both before anyone reads the diff, so revertibility is no review.
//
// UNVERIFIED PREMISE — carried deliberately
//   "ask" is a documented permissionDecision value, but whether it overrides an
//   already-auto-approving permission mode (acceptEdits / an allow rule) is not
//   stated in the docs section, which truncates there. If it does NOT override,
//   this hook degrades to a silent no-op — it never false-blocks. Settle it live:
//   in a default-permission session with acceptEdits on, edit CHANGELOG.md and
//   look for a prompt carrying this hook's permissionDecisionReason. No prompt
//   means "ask" does not override, and the decision has to become "deny".
//
// EXIT CODES
//   0  always — passthrough (no output) or a decision on stdout. Never crashes
//      the session, matching every other hook in this plugin.

"use strict";

const path = require("path");
const { spawnSync } = require("child_process");

const GUARDED_TOOLS = new Set(["Edit", "Write", "NotebookEdit"]);

// Matched against the POSIX-normalized path. Windows hosts hand us backslashes,
// so every pattern assumes "/" and the caller normalizes first — a separator
// mismatch would silently disable the whole guard on one OS.
// Anchored with `(^|\/)` so a path merely CONTAINING the name never matches:
// `src/my_changelog_helper.py` and `src/github/client.py` stay routine work.
// Case-INSENSITIVE by design: macOS and Windows filesystems are case-folding, so
// a write addressed as `changelog.md` lands in the real `CHANGELOG.md`. Under a
// case-sensitive matcher that path classifies as unprotected, and passthrough
// means auto-approved whenever the hook is paired with acceptEdits — the exact
// configuration it exists for. The cost is a false ask on case-sensitive Linux
// for a genuinely distinct `docs/claude.md`; one extra confirmation is the
// cheaper side of that trade.
const PROTECTED = [
  { re: /(^|\/)\.github\//i, why: "CI/workflow definition" },
  { re: /(^|\/)CLAUDE\.md$/i, why: "agent instructions" },
  { re: /(^|\/)AGENTS\.md$/i, why: "agent instructions" },
  { re: /(^|\/)\.claude\/settings(\.local)?\.json$/i, why: "Claude permission config", always: true },
  { re: /(^|\/)\.pre-commit-config\.yaml$/i, why: "lint/format gate", always: true },
  { re: /(^|\/)CHANGELOG\.md$/i, why: "release metadata" },
  { re: /(^|\/)(uv|poetry|package-lock|yarn|Cargo|pnpm-lock)\.(lock|json|yaml)$/i, why: "dependency lockfile" },
  { re: /(^|\/)pyproject\.toml$/i, why: "package/release metadata" },
];

/**
 * Normalize a filesystem path to POSIX separators for pattern matching.
 *
 * @param {string} p Raw path from tool_input.
 * @returns {string} Path with "/" separators.
 */
function toPosix(p) {
  return String(p).replace(/\\/g, "/");
}

/**
 * Classify a path against the protected set.
 *
 * @param {string} filePath Raw path from tool_input.
 * @returns {{why: string, always: boolean}|null} Match reason and whether it asks even when tracked, or null when
 *   unprotected.
 */
function classify(filePath) {
  if (!filePath) return null;
  const posix = toPosix(filePath);
  for (const entry of PROTECTED) {
    if (entry.re.test(posix)) return { why: entry.why, always: Boolean(entry.always) };
  }
  return null;
}

/**
 * Check whether git tracks the file, so a write to it stays diffable and revertible.
 *
 * Runs from the file's own directory so a nested repository or a path outside
 * the session cwd resolves against the repository that actually owns it.
 * `--literal-pathspecs` keeps a name such as `:x` or `*.md` from being read as
 * pathspec magic or a glob that could match some other tracked file.
 *
 * @param {string} filePath Raw path from tool_input.
 * @param {string} [cwd] Session cwd from the hook payload; relative paths resolve against it.
 * @returns {boolean} True only when git confirms the file is in the index.
 */
function isTracked(filePath, cwd) {
  const abs = path.resolve(cwd || process.cwd(), filePath);
  const res = spawnSync(
    "git",
    ["--literal-pathspecs", "-C", path.dirname(abs), "ls-files", "--error-unmatch", "--", path.basename(abs)],
    { stdio: "ignore", timeout: 3000, windowsHide: true },
  );
  return res.status === 0;
}

/**
 * Build the hook decision for one tool call, or null for passthrough.
 *
 * `notebook_path` is read alongside `file_path` because NotebookEdit names its
 * target differently; without it that matcher would ship unguarded.
 *
 * @param {object} data Parsed hook payload.
 * @returns {object|null} hookSpecificOutput envelope, or null.
 */
function decide(data) {
  if (!data || !GUARDED_TOOLS.has(data.tool_name)) return null;
  const input = data.tool_input || {};
  const target = input.file_path || input.notebook_path;
  const hit = classify(target);
  if (!hit) return null;
  if (!hit.always && isTracked(target, data.cwd)) return null;
  return {
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "ask",
      permissionDecisionReason: `protected file (${hit.why}) — confirm this write`,
    },
  };
}

module.exports = { classify, decide, isTracked, toPosix, PROTECTED, GUARDED_TOOLS };

if (require.main === module) {
  let raw = "";
  process.stdin.setEncoding("utf8");
  process.stdin.on("data", (c) => (raw += c));
  process.stdin.on("end", () => {
    let out = null;
    try {
      out = decide(JSON.parse(raw));
    } catch (_) {
      out = null; // malformed payload → passthrough, never block
    }
    if (out) process.stdout.write(JSON.stringify(out));
    process.exit(0);
  });
}
