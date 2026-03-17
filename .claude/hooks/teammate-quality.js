#!/usr/bin/env node
// TeammateIdle hook — redirects to pending tasks if any exist in the shared task list.
// TaskCompleted hook — no-op (exit 0), reserved for future quality gates.
//
// TeammateIdle: exit code 2 sends stderr as feedback to the teammate and keeps it working.
// TaskCompleted: always exit 0 to avoid rejection loops.
//
// Exit 0 always on error — hooks must never block Claude.

const fs = require("fs");
const path = require("path");

let raw = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (d) => (raw += d));
process.stdin.on("end", () => {
  try {
    const data = JSON.parse(raw);
    const { hook_event_name, team_name } = data;

    if (hook_event_name === "TeammateIdle" && team_name) {
      // Look for pending tasks in the shared task list — redirect if any exist
      try {
        // Task files live at ~/.claude/tasks/<team>/ when teams use file-based tracking.
        // If the directory is absent (e.g., in-memory teams), readdirSync throws and the
        // catch block silently lets the teammate go idle — no redirect occurs.
        const tasksDir = path.join(process.cwd(), ".claude", "tasks", team_name);
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
