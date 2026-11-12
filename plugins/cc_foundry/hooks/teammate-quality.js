#!/usr/bin/env node
// teammate-quality.js — agent team quality gate hook
//
// PURPOSE
//   Prevents teammates in an agent team from going idle while work remains, and
//   provides a hook point for future output quality validation.  Works alongside
//   task-log.js which tracks session state; this hook acts on team-level events.
//
// HOW IT WORKS
//   1. Parse stdin JSON for hook_event_name and team_name
//   2. TeammateIdle: validate team_name is safe (basename check), then read
//      ~/.claude/tasks/<team_name>/*.json from the user home
//      (Claude Code stores agent-team task files there — not a project artifact dir)
//   3. Filter task files to those with status === "pending"
//   4. Pending tasks found: write task list to stderr and exit 2 — Claude Code
//      surfaces the message to the teammate, redirecting it back to claim a task
//   5. No pending tasks (or task directory absent): exit 0 — teammate idles normally
//   6. TaskCompleted: always exit 0 — rejection loops are worse than missing validation
//
// HOOK EVENT RESPONSIBILITIES
//
//   TeammateIdle
//     Fires when a teammate finishes its current task and has nothing to do.
//     Checks for pending tasks in the shared task list for the current team
//     (~/.claude/tasks/<team>/*.json — Claude Code's internal agent-teams state dir).
//     If any pending tasks exist, writes a redirect message to stderr and exits 2 —
//     Claude Code interprets exit 2 as "feedback for the teammate", re-activating it
//     with the message as context so it can claim and complete the next task.
//     If no pending tasks are found (or the task directory is absent), exits 0
//     and the teammate idles normally.
//
//   TaskCompleted
//     Fires when a teammate signals it has completed a task.
//     Currently a no-op (exit 0).  Reserved as a hook point for future quality
//     gates: output validation, confidence score checks, or handoff verification.
//     Deliberately always exits 0 — rejecting a completed task risks infinite
//     rejection loops if the quality bar cannot be met.
//
// EXIT CODES
//   0  No action needed (idle is fine, or task completion accepted).
//   2  Pending tasks found → stderr message redirects the teammate back to work.
//      Never exit 2 on TaskCompleted — rejection loops are worse than gaps.
//
// ERROR HANDLING
//   I/O and parse errors are swallowed (exit 0); missing or unsafe team names
//   produce a diagnostic. A quality-gate hook must never
//   crash or block Claude — a broken hook that prevents teammates from completing
//   tasks is worse than no gate at all.

const fs = require("fs");
const path = require("path");
const os = require("os");

let raw = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (d) => (raw += d));
process.stdin.on("end", () => {
  try {
    const data = JSON.parse(raw);
    const { hook_event_name, team_name } = data;

    if (hook_event_name === "TeammateIdle") {
      // Newer payloads may omit the deprecated team_name field: diagnose and no-op.
      if (
        typeof team_name !== "string" ||
        !team_name ||
        team_name === "." ||
        team_name === ".." ||
        /[\\/\x00]/.test(team_name) ||
        path.basename(team_name) !== team_name
      ) {
        process.stderr.write("teammate-quality: missing or unsafe team_name; task lookup skipped.\n");
        process.exit(0);
      }
      // Look for pending tasks in the shared task list — redirect if any exist
      try {
        // Claude Code's documented shared task storage is scoped to the user, not the workspace.
        // If the directory is absent, readdirSync throws and the catch block
        // lets the teammate go idle without redirecting it to an unknown task.
        const tasksDir = path.join(os.homedir(), ".claude", "tasks", team_name);
        const taskFiles = fs.readdirSync(tasksDir).filter((f) => f.endsWith(".json"));
        const pendingTasks = taskFiles
          .map((f) => {
            try {
              return JSON.parse(fs.readFileSync(path.join(tasksDir, f), "utf8"));
            } catch (_) {
              return null;
            }
          })
          .filter((t) => t && t.status === "pending");

        if (pendingTasks.length > 0) {
          const taskList = pendingTasks.map((t) => `- ${t.id}: ${t.subject || t.title || "(untitled)"}`).join("\n");
          process.stderr.write(
            `There are ${pendingTasks.length} pending task(s) in the shared task list:\n${taskList}\n` +
              "Claim and complete tasks appropriate for your capabilities before going idle. " +
              "Use omega @lead idle DONE only when there are no remaining tasks for you.",
          );
          process.exit(2);
        }
      } catch (_) {
        // Cannot read task list — don't interfere, let teammate go idle
      }
    }

    // TaskCompleted: always exit 0 — rejection loops are worse than missing validation
  } catch (_) {
    // Never block Claude
  }
  process.exit(0);
});
