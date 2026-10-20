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
//   to remove routine friction while untracked agent instructions, permission
//   config, the pre-commit gate and package metadata still stop you.
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
//   and (b) costly when written without review: agent instructions (the file
//   that tells the next agent what to do), Claude's own permission config, the
//   pre-commit gate and package metadata.
//   Source, tests, CI helper scripts, changelogs and lockfiles are deliberately
//   NOT protected — that is the routine work, and a new file under `.github/`
//   or a fresh CHANGELOG.md is untracked by nature, so asking on it only
//   harassed edits to work the user had just requested.
//
// VERSION-CONTROLLED FILES PASS THROUGH
//   A protected file already tracked by git (present in the index) is never
//   gated: every write to it shows up in `git diff` and is revertible, which is
//   the review the confirmation was standing in for. Asking on each tracked
//   write only produced a per-edit prompt the user could never dismiss, since a
//   hook "ask" is not remembered across calls. Untracked, new and gitignored
//   protected files (e.g. `.claude/settings.local.json`) still ask — nothing
//   else records or reverts a write to them. Any failure of the git probe (no
//   git, not a repository, timeout) counts as untracked, so the guard keeps
//   asking rather than silently opening.
//
// FILES THIS SESSION CREATED PASS THROUGH AFTER THE FIRST ASK
//   A protected file that does not exist yet asks once and is recorded in a
//   per-session ledger (`write-guard-created-<session id>` in the temp dir).
//   Once the file exists and is in that ledger, later writes in the same
//   session pass: re-asking on every edit of a file the user just approved
//   creating only repeated the same answer. The ledger entry alone is not
//   enough — a denied creation leaves the file absent, so the retry still asks.
//   A protected file that already existed untracked was not made by this
//   session and keeps asking on every write.
//
//   Why dropping the tracked-file confirmation is safe for the two entries the
//   hook once held back (`.claude/settings*.json`, `.pre-commit-config.yaml`):
//   revertibility alone is not the argument. Claude Code itself prompts on
//   writes to `.claude/` and `.pre-commit-config.yaml` in the default and
//   acceptEdits modes (permission-modes docs, protected paths), whatever this
//   hook does. Do not re-add an `always` flag on the grounds that git can undo
//   the write — it can, and that was never the reason.
//
// UNVERIFIED PREMISE — carried deliberately
//   "ask" is a documented permissionDecision value, but whether it overrides an
//   already-auto-approving permission mode (acceptEdits / an allow rule) is not
//   stated in the docs section, which truncates there. If it does NOT override,
//   this hook degrades to a silent no-op — it never false-blocks. Settle it live:
//   in a default-permission session with acceptEdits on, create a new untracked
//   `docs/AGENTS.md` (not a natively protected path, and not tracked by git) and
//   look for a prompt carrying this hook's permissionDecisionReason. No prompt
//   means "ask" does not override, and the decision has to become "deny".
//
// EXIT CODES
//   0  always — passthrough (no output) or a decision on stdout. Never crashes
//      the session, matching every other hook in this plugin.

"use strict";

const fs = require("fs");
const os = require("os");
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
  { re: /(^|\/)CLAUDE\.md$/i, why: "agent instructions" },
  { re: /(^|\/)AGENTS\.md$/i, why: "agent instructions" },
  { re: /(^|\/)\.claude\/settings(\.local)?\.json$/i, why: "Claude permission config" },
  { re: /(^|\/)\.pre-commit-config\.yaml$/i, why: "lint/format gate" },
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
 * @returns {{why: string}|null} Match reason, or null when unprotected.
 */
function classify(filePath) {
  if (!filePath) return null;
  const posix = toPosix(filePath);
  for (const entry of PROTECTED) {
    if (entry.re.test(posix)) return { why: entry.why };
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
 * Locate the per-session ledger of protected files this session first asked to create.
 *
 * The session id comes from the hook payload so two concurrent sessions never share a ledger; the name ends in
 * `-<id>` per the repository sentinel convention.
 *
 * @param {object} data Parsed hook payload.
 * @returns {string} Ledger file path.
 */
function ledgerPath(data) {
  const raw = data.session_id || process.env.CSID || process.env.CLAUDE_CODE_SESSION_ID || "shared";
  const sid = String(raw).replace(/[^A-Za-z0-9_-]/g, "_");
  return path.join(process.env.TMPDIR || os.tmpdir(), `write-guard-created-${sid}`);
}

/**
 * Check whether a path exists, a dangling symlink included.
 *
 * `lstat` rather than `stat`: a dangling symlink to a protected file elsewhere must read as existing, or a write
 * through it would count as a creation.
 *
 * @param {string} abs Absolute path.
 * @returns {boolean} True when anything occupies the path.
 */
function occupied(abs) {
  try {
    fs.lstatSync(abs);
    return true;
  } catch (_) {
    return false;
  }
}

/**
 * Check whether this session earlier asked to create the file, which now exists.
 *
 * Both halves are needed: a ledger entry alone would let a denied creation retry without a prompt, and existence
 * alone says nothing about who made the file. Any read failure counts as not created, so the guard keeps asking.
 *
 * @param {string} abs Absolute path of the target.
 * @param {object} data Parsed hook payload.
 * @returns {boolean} True when the file is a product of this session.
 */
function createdThisSession(abs, data) {
  if (!occupied(abs)) return false;
  try {
    const key = JSON.stringify(abs);
    return fs.readFileSync(ledgerPath(data), "utf8").split("\n").includes(key);
  } catch (_) {
    return false;
  }
}

/**
 * Record that this session asked to create a not-yet-existing protected file; a failed write only loses the record.
 *
 * @param {string} abs Absolute path of the target.
 * @param {object} data Parsed hook payload.
 */
function recordCreation(abs, data) {
  try {
    fs.appendFileSync(ledgerPath(data), `${JSON.stringify(abs)}\n`);
  } catch (_) {
    // unwritable temp dir → later edits ask again; never block the session
  }
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
  if (isTracked(target, data.cwd)) return null;
  const abs = path.resolve(data.cwd || process.cwd(), target);
  if (createdThisSession(abs, data)) return null;
  if (!occupied(abs)) recordCreation(abs, data);
  return {
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "ask",
      permissionDecisionReason: `protected file (${hit.why}) — confirm this write`,
    },
  };
}

module.exports = { classify, decide, isTracked, createdThisSession, toPosix, PROTECTED, GUARDED_TOOLS };

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
