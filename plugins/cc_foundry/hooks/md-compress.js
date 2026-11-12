#!/usr/bin/env node
// md-compress.js — PostToolUse hook (Edit only)
//
// PURPOSE
//   MD files (config, reports, plans, handover, skills) are dense with
//   pipe-table column padding that wastes tokens without adding meaning.
//   After a completed Edit, normalize the authorized document at its real
//   on-disk project path. Never mutate files before permission checks or grant
//   tool permission, and never rewrite tool input.
//
//   NOTE — two prior designs were tried and rejected here, both on Read:
//   1. Serve Read from a session-scoped temp file, redirecting file_path,
//      to keep Read side-effect-free. Broke the Edit tool's own "must Read
//      this path before Edit" precondition — Claude's Read got recorded
//      against the temp path, so Edit on the real path always failed with
//      "File has not been read yet", even right after a successful Read.
//   2. Normalize the real file in place on Read too (mirroring Edit), to
//      fix (1) while still saving Read-time tokens. This fixed the Edit
//      precondition, but silently rewrote table alignment in every .md
//      file Claude so much as looked at — including files nobody asked to
//      touch — clobbering deliberately-aligned GFM tables repo-wide with
//      no mdformat pass to restore them (mdformat-gfm's default table
//      style does not re-pad columns).
//   Read is now a pure no-op for this hook — no compression, no disk
//   writes, ever. Since Read never redirects or rewrites, Read's tracked
//   path and Edit's target path are always identical by construction,
//   which is what actually fixes the "File has not been read yet" bug —
//   no Read-time mutation is needed for that fix.
//
//   Post-edit (lint-on-save.js): runs explicitly configured trusted pre-commit
//     hooks after Write/Edit, applying the user's selected formatters.
//
// COMPRESSIONS (outside fenced code blocks only)
//     1. Table column padding: collapses 2+ spaces on pipe-table lines to 1.
//     2. Consecutive blank lines: collapses runs of 2+ blanks to 1.
//     3. Trailing whitespace: strips trailing spaces on every non-fence line.
//
// HOW IT WORKS — Edit path (the only path)
//   1. Skip events other than PostToolUse/Edit or non-.md/.markdown files.
//   2. Require a regular project file with no symlink components; read its content.
//   3. Run compressMarkdown; if unchanged, skip disk write.
//   4. Preserve permissions and write normalized content atomically through an
//      exclusively created temporary file, rechecking source identity before rename.
//   5. Emit no permission decision or updatedInput: the Edit has already completed.
//
// EXIT CODES
//   0  passthrough (other event/tool, non-.md file, unsafe path, read error, no-op, or successful rewrite)

"use strict";

const fs = require("fs");
const path = require("path");
const { randomBytes } = require("crypto");

/**
 * Compress markdown content:
 *  - Outside fenced code blocks:
 *    • Strip trailing whitespace from each line
 *    • On pipe-table lines: collapse runs of 2+ spaces to 1
 *    • Collapse runs of 2+ consecutive blank lines to 1
 *
 * @param {string} content
 * @returns {string}
 */
function compressMarkdown(content) {
  const lines = content.split("\n");
  const out = [];
  let inFence = false;
  let fenceChar = "";
  let fenceLen = 0;
  let consecutiveBlanks = 0;

  for (const line of lines) {
    const trimmed = line.trimStart();

    // --- Fence tracking ---
    if (!inFence) {
      const m = trimmed.match(/^(`{3,}|~{3,})/);
      if (m) {
        inFence = true;
        fenceChar = m[1][0];
        fenceLen = m[1].length;
        consecutiveBlanks = 0;
        out.push(line); // preserve fence line as-is
        continue;
      }
    } else {
      const m = trimmed.match(/^(`{3,}|~{3,})/);
      // CommonMark: a closing fence must use the same char AND be at least as
      // long as the opener — a 3-backtick line does NOT close a 4-backtick fence.
      if (m && m[1][0] === fenceChar && m[1].length >= fenceLen) {
        inFence = false;
        fenceChar = "";
        fenceLen = 0;
      }
      out.push(line); // preserve all content inside fence as-is
      continue;
    }

    // --- Outside fence ---

    // Strip trailing whitespace
    const stripped = line.trimEnd();

    // Blank line handling: collapse consecutive blank lines
    if (stripped === "") {
      consecutiveBlanks++;
      if (consecutiveBlanks <= 1) {
        out.push(""); // allow exactly one blank line through
      }
      // subsequent blanks in the same run are dropped
      continue;
    }

    consecutiveBlanks = 0;

    // Pipe-table lines: collapse internal padding (2+ spaces → 1)
    if (stripped.startsWith("|")) {
      out.push(stripped.replace(/ {2,}/g, " "));
    } else {
      out.push(stripped);
    }
  }

  return out.join("\n");
}

/** Validate containment and reject every symlink component before touching a document. */
function documentStat(root, absPath) {
  const rel = path.relative(root, absPath);
  if (!rel || rel === ".." || rel.startsWith(".." + path.sep) || path.isAbsolute(rel)) return null;
  let current = root;
  let stat;
  for (const component of rel.split(path.sep)) {
    current = path.join(current, component);
    stat = fs.lstatSync(current);
    if (stat.isSymbolicLink()) return null;
  }
  return stat.isFile() && fs.realpathSync(absPath) === absPath ? stat : null;
}

/**
 * Read a project markdown file, normalize it, and write the result back in place
 * if it changed. Refuse symlinks and source changes; preserve file permissions.
 *
 * @param {string} root
 * @param {string} absPath
 * @returns {void}
 */
function normalizeFileInPlace(root, absPath) {
  const original = documentStat(root, absPath);
  if (!original) return;
  const content = fs.readFileSync(absPath, "utf8");
  const normalized = compressMarkdown(content);
  if (normalized === content) return;

  const tmpPath = `${absPath}.md-compress-${randomBytes(12).toString("hex")}.tmp`;
  let created = false;
  try {
    // Write-then-rename: atomic on the same filesystem, so a crash or a
    // concurrent read mid-write never sees a truncated file. Exclusive creation
    // refuses a planted temp path rather than following or overwriting it.
    const fd = fs.openSync(tmpPath, "wx", original.mode & 0o777);
    created = true;
    try {
      fs.writeFileSync(fd, normalized, "utf8");
      // Creation respects umask; restore the original mode on POSIX explicitly.
      if (process.platform !== "win32") fs.fchmodSync(fd, original.mode & 0o777);
    } finally {
      fs.closeSync(fd);
    }
    const current = documentStat(root, absPath);
    if (
      !current ||
      current.dev !== original.dev ||
      current.ino !== original.ino ||
      current.mtimeMs !== original.mtimeMs ||
      current.size !== original.size
    )
      return;
    fs.renameSync(tmpPath, absPath);
    created = false;
  } finally {
    if (created) {
      try {
        fs.unlinkSync(tmpPath);
      } catch (_) {}
    }
  }
}

let raw = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (d) => (raw += d));
process.stdin.on("end", () => {
  try {
    const data = JSON.parse(raw);
    if (data.hook_event_name !== "PostToolUse" || data.tool_name !== "Edit") process.exit(0);
    const editPath = data.tool_input?.file_path;
    if (typeof editPath !== "string" || !/\.(?:md|markdown)$/i.test(editPath)) process.exit(0);
    const root = fs.realpathSync(process.cwd());
    normalizeFileInPlace(root, path.resolve(editPath));
  } catch (_) {
    // Failed validation or I/O leaves the authorized edit untouched.
  }
  process.exit(0);
});
