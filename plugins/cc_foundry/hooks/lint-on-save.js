#!/usr/bin/env node
// lint-on-save.js — PostToolUse hook
//
// PURPOSE
//   Closes the gap between Claude editing a file and pre-commit running at
//   commit time. With explicit consent, auto-fixable violations are applied the
//   moment they're introduced. Pairs with md-compress.js: both normalize files
//   only after completed edits, without granting permission to the Edit tool.
//
// WHY EXPLICIT --config
//   Hook fires globally across all projects. The user must set the process
//   environment FOUNDRY_LINT_CONFIG to an absolute trusted configuration path
//   before launching Claude. Passing --config selects exactly that config, not
//   one found by walking parent directories. Repository discovery is not consent.
//
// HOW IT WORKS
//   1. Fires on every PostToolUse event for the Write and Edit tools.
//   2. Checks for explicit FOUNDRY_LINT_CONFIG consent to a regular config file.
//      If absent, exits 0 silently — safe no-op without user configuration.
//   3. Runs `pre-commit run --config <configPath> --files <file_path>`
//      targeting only the changed file (no full-repo scan).
//   4. On success (exit 0) → silent, no output.
//      On failure (exit 1 or 2) → writes hook output to stderr and exits 2,
//      which causes Claude Code to surface the message as feedback so Claude
//      can immediately read it and apply the necessary fix.
//      If pre-commit exits non-zero with no output (e.g. no hooks apply to
//      the file type), this hook exits 0 silently — no false-positive block.
//
// EXIT CODES
//   0  All hooks passed, or pre-commit is not configured / not installed.
//   2  One or more hooks failed or auto-modified the file.
//      Claude Code treats exit 2 as "blocking feedback" — the output is shown
//      and Claude will re-read the file and/or apply corrections.
//
// PRE-COMMIT EXIT CODE MAPPING
//   pre-commit 0 → this hook exits 0 (silent pass)
//   pre-commit 1 → auto-fix applied (file changed) → exits 2 so Claude re-reads
//   pre-commit 2 → errors found → exits 2 so Claude sees the diagnostics
//
// ASYNC SPAWN
//   Uses child_process.spawn (async) with explicit SIGTERM kill-on-timeout so
//   Claude Code's hook pipeline is never blocked by a stalled pre-commit run.
//   The hook process waits for the child via a Promise and exits with the
//   correct code once pre-commit finishes or the timer fires.
//
// CROSS-SESSION LOCK
//   A project-scoped lock in a private per-user 0700 temp directory prevents
//   parallel Claude terminals from running pre-commit on the same project
//   simultaneously (which would race pre-commit's own internal lock file and
//   produce spurious failures). Existing storage must be owned, private and not
//   a symlink. Both deduplication (5s) and project locks use exclusive creation,
//   including stale recovery; unsafe storage or lock failure skips linting.
//   The project lock is released on completion. Stale locks (>20s) can be removed
//   only when regular and owned, then reacquired exclusively without overwriting.
//
// TIMEOUT
//   15 s hard limit per single-file invocation. Heavy hooks (mypy, eslint full
//   project) should be configured with `--show-diff-on-failure` or
//   `pass_filenames: false` in the explicitly selected pre-commit config to avoid slow runs.
//

const fs = require("fs");
const os = require("os");
const path = require("path");
const { spawn } = require("child_process");
const { createHash, randomBytes } = require("crypto");

/** Return private lock storage, refusing links or permissive existing directories. */
function getLockDirectory() {
  const user = typeof process.getuid === "function" ? process.getuid() : hash(os.homedir());
  const dir = path.join(fs.realpathSync(os.tmpdir()), `claude-precommit-${user}`);
  try {
    fs.mkdirSync(dir, { mode: 0o700 });
  } catch (error) {
    if (error.code !== "EEXIST") throw error;
  }
  const stat = fs.lstatSync(dir);
  if (!stat.isDirectory() || stat.isSymbolicLink() || !isOwned(stat)) return null;
  // Windows temp storage inherits the user's ACL; POSIX modes are not enforced there.
  if (process.platform !== "win32" && (stat.mode & 0o777) !== 0o700) return null;
  return dir;
}

/** Hash complete lock identities without truncation or filename collisions. */
function hash(value) {
  return createHash("sha256").update(value).digest("hex");
}

/** Check ownership where the host exposes numeric process identities. */
function isOwned(stat) {
  return typeof process.getuid !== "function" || stat.uid === process.getuid();
}

/** Acquire a regular owned lock exclusively; never follow or overwrite stale paths. */
function acquireLock(filename, ttl) {
  try {
    try {
      const old = fs.lstatSync(filename);
      if (!old.isFile() || old.isSymbolicLink() || !isOwned(old)) return null;
      if (Date.now() - old.mtimeMs < ttl) return null;
      const current = fs.lstatSync(filename);
      if (current.dev !== old.dev || current.ino !== old.ino || current.mtimeMs !== old.mtimeMs) return null;
      fs.unlinkSync(filename);
    } catch (error) {
      if (error.code !== "ENOENT") return null;
    }
    const fd = fs.openSync(filename, "wx", 0o600);
    try {
      fs.writeSync(fd, String(process.pid));
      return fs.fstatSync(fd);
    } finally {
      fs.closeSync(fd);
    }
  } catch (_) {
    return null;
  }
}

/** Release only the exact lock inode this invocation created. */
function releaseLock(filename, owned) {
  if (!filename || !owned) return;
  try {
    const current = fs.lstatSync(filename);
    if (current.isFile() && isOwned(current) && current.dev === owned.dev && current.ino === owned.ino) {
      fs.unlinkSync(filename);
    }
  } catch (_) {}
}

function runPrecommit(configPath, filePath, root, timeoutMs) {
  return new Promise((resolve) => {
    const child = spawn("pre-commit", ["run", "--config", configPath, "--files", filePath], {
      cwd: root,
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "",
      stderr = "";
    child.stdout.on("data", (d) => {
      stdout += d;
    });
    child.stderr.on("data", (d) => {
      stderr += d;
    });

    const timer = setTimeout(() => {
      child.kill("SIGTERM");
      resolve({ timedOut: true, stdout, stderr, status: null });
    }, timeoutMs);

    child.on("close", (code, signal) => {
      clearTimeout(timer);
      resolve({ timedOut: false, stdout, stderr, status: code, signal });
    });
    child.on("error", (err) => {
      clearTimeout(timer);
      resolve({ error: err, stdout, stderr, status: null });
    });
  });
}

let raw = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (d) => (raw += d));
process.stdin.on("end", async () => {
  let crossLockAcquired = null;
  let crossLock = null;
  try {
    const data = JSON.parse(raw);
    const { hook_event_name, tool_name, tool_input, session_id } = data;

    // Only act on PostToolUse for Write or Edit
    if (hook_event_name !== "PostToolUse") process.exit(0);
    if (tool_name !== "Write" && tool_name !== "Edit") process.exit(0);

    const selectedConfig = process.env.FOUNDRY_LINT_CONFIG;
    if (!selectedConfig || !path.isAbsolute(selectedConfig)) process.exit(0);
    const configPath = fs.realpathSync(selectedConfig);
    if (!fs.statSync(configPath).isFile()) process.exit(0);

    const inputPath = tool_input?.file_path;
    if (typeof inputPath !== "string" || !inputPath) process.exit(0);
    const filePath = fs.realpathSync(path.resolve(inputPath));

    // Resolve project root (hooks run with CWD = project root)
    const root = fs.realpathSync(process.cwd());

    // Skip files outside the project root (e.g. edits to ~/.claude/ files)
    const rel = path.relative(root, filePath);
    if (!rel || rel === ".." || rel.startsWith(".." + path.sep) || path.isAbsolute(rel)) process.exit(0);

    // Skip ephemeral handover dirs — agents write subagent-handover .md files into
    // .temp/ at high volume during multi-agent workflows (e.g. /oss:review spawns
    // 7+ agents writing .md output concurrently). Linting each via pre-commit
    // serializes under the cross-session lock and can stall the orchestrator
    // for minutes while it waits for Agent() to return. These files are never
    // committed — short-circuit before lock acquisition.
    if (rel.startsWith(".temp/") || rel.startsWith(".temp" + path.sep)) process.exit(0);

    // Deduplication lock — project and home settings.json can both register this
    // hook. A fresh per-file/session lock skips duplicate runs for five seconds.
    // Keep it in verified private storage, never a shared writable session dir.
    const lockDir = getLockDirectory();
    if (!lockDir) process.exit(0);
    const sid = typeof session_id === "string" && session_id ? session_id : randomBytes(16).toString("hex");
    const fileLock = path.join(lockDir, `file-${hash(root + "\0" + sid + "\0" + filePath)}.lock`);
    if (!acquireLock(fileLock, 5_000)) process.exit(0);
    // Cross-session lock: one pre-commit run per project at a time.
    // Key by the complete project root to avoid blocking unrelated projects.
    crossLock = path.join(lockDir, `project-${hash(root)}.lock`);
    crossLockAcquired = acquireLock(crossLock, 20_000);
    if (!crossLockAcquired) process.exit(0);

    // Run pre-commit on the specific file (async, 15s timeout)
    const result = await runPrecommit(configPath, filePath, root, 15_000);

    releaseLock(crossLock, crossLockAcquired);
    crossLockAcquired = null;

    if (result.timedOut) {
      process.exit(0); // Timeout — don't block Claude
    }

    if (result.error) {
      // Missing pre-commit binary should not block Claude.
      if (result.error.code === "ENOENT") process.exit(0);
      const errMsg = `pre-commit failed to run: ${result.error.message}`;
      const out = [result.stdout, result.stderr, errMsg].filter(Boolean).join("\n").trim();
      process.stderr.write(out);
      process.exit(2);
    }

    if (result.status !== 0) {
      const raw = [result.stdout, result.stderr].filter(Boolean).join("\n");
      if (!raw.trim()) process.exit(0); // no hooks apply to this file type — silent pass
      // Strip dot-padded progress header lines (Passed/Skipped/Failed) — only show error details.
      // Removes the long "hookname.....Result" lines that wrap in Claude Code's narrow blocking pane.
      const filtered = raw
        .split("\n")
        .filter((line) => !/\.{10,}\s*(Passed|Skipped|Failed)\s*$/.test(line))
        .join("\n")
        .trim();
      if (!filtered) process.exit(0);
      process.stderr.write(filtered);
      process.exit(2);
    }

    process.exit(0);
  } catch (_) {
    // Never block Claude — swallow all errors
    releaseLock(crossLock, crossLockAcquired);
    process.exit(0);
  }
});
