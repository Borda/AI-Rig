#!/usr/bin/env node
// lint-on-save.js — PostToolUse hook
//
// PURPOSE
//   Automatically lint every file Claude writes or edits, using the project's
//   own pre-commit configuration.  This closes the gap between "Claude edits a
//   file" and "a human runs pre-commit" — catching style violations, spell errors,
//   JSON/YAML syntax issues, and auto-fixable formatting problems the moment
//   they're introduced rather than at commit time.
//
// HOW IT WORKS
//   1. Fires on every PostToolUse event for the Write and Edit tools.
//   2. Checks whether .pre-commit-config.yaml exists in the project root.
//      If absent, exits 0 silently — the hook is a no-op in repos without
//      pre-commit, so it is safe to keep active globally.
//   3. Runs `pre-commit run --files <file_path>` targeting only the changed
//      file, which is fast (no full-repo scan).
//   4. On success (exit 0) → silent, no output.
//      On failure (exit 1 or 2) → writes hook output to stdout and exits 2,
//      which causes Claude Code to surface the message as feedback so Claude
//      can immediately read it and apply the necessary fix.
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
// TIMEOUT
//   60 s hard limit per invocation.  Heavy hooks (mypy, eslint full project)
//   should be configured with `--show-diff-on-failure` or `pass_filenames: false`
//   in .pre-commit-config.yaml to avoid slow single-file runs.
//
// ADDING TO SETTINGS
//   Register under PostToolUse in .claude/settings.json:
//     { "command": "node .claude/hooks/lint-on-save.js", "type": "command" }

const fs = require("fs");
const path = require("path");
const { spawnSync } = require("child_process");

let raw = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (d) => (raw += d));
process.stdin.on("end", () => {
  try {
    const data = JSON.parse(raw);
    const { hook_event_name, tool_name, tool_input } = data;

    // Only act on PostToolUse for Write or Edit
    if (hook_event_name !== "PostToolUse") process.exit(0);
    if (tool_name !== "Write" && tool_name !== "Edit") process.exit(0);

    const filePath = tool_input?.file_path;
    if (!filePath) process.exit(0);

    // Resolve project root (hooks run with CWD = project root)
    const root = process.cwd();

    // Skip files outside the project root (e.g. edits to ~/.claude/ files)
    const rel = path.relative(root, filePath);
    if (rel.startsWith("..") || path.isAbsolute(rel)) process.exit(0);

    // Skip if no pre-commit config in this project
    const configPath = path.join(root, ".pre-commit-config.yaml");
    if (!fs.existsSync(configPath)) process.exit(0);

    // Run pre-commit on the specific file
    const result = spawnSync("pre-commit", ["run", "--files", filePath], {
      cwd: root,
      encoding: "utf8",
      timeout: 60_000, // 60s max — some hooks (mypy, eslint) can be slow
    });

    if (result.error) {
      // Missing pre-commit binary should not block Claude.
      if (result.error.code === "ENOENT") process.exit(0);
      const errMsg =
        result.error.code === "ETIMEDOUT"
          ? "pre-commit timed out after 60s"
          : `pre-commit failed to run: ${result.error.message}`;
      const out = [result.stdout, result.stderr, errMsg].filter(Boolean).join("\n").trim();
      process.stdout.write(out);
      process.exit(2);
    }

    if (result.status !== 0) {
      const out = [result.stdout, result.stderr].filter(Boolean).join("\n").trim();
      process.stdout.write(out || "pre-commit: hooks failed (no output)");
      process.exit(2);
    }

    process.exit(0);
  } catch (_) {
    // Never block Claude — swallow all errors
    process.exit(0);
  }
});
