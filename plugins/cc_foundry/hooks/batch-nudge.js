#!/usr/bin/env node
// batch-nudge.js — parallel-tool-call batching nudge
//
// PURPOSE
//   Counts consecutive PostToolBatch events whose whole batch is ONE read-only tool
//   call: the agent issued independent calls in separate turns instead of one
//   parallel batch. At NUDGE_THRESHOLD it injects a one-line reminder to batch
//   independent calls (rules/claude-config.md §Turn Batching, installed as
//   ~/.claude/rules/foundry-claude-config.md). Informational only — never blocks.
//
// WHY POSTTOOLBATCH
//   PostToolBatch fires exactly once per batch with the full tool_calls array, so the
//   batch size is read directly. Replaces a PreToolUse timestamp-gap heuristic (calls
//   <1.5s apart assumed same-message) that misclassified slow tools and fast turns,
//   and a node spawn on every PreToolUse + PostToolUse.
//
// WHY additionalContext, NOT A DECISION
//   decision:"block", continue:false and exit 2 on PostToolBatch STOP the agentic loop
//   before the next model call. The reminder goes out as hookSpecificOutput
//   .additionalContext (exit 0), injected once before the next model call.
//   Verified against the hooks reference, PostToolBatch section.
//
// INPUT COST
//   tool_calls[].tool_response carries the full serialized tool results (can be large);
//   only tool_name and tool_input are read.
//
// CLASSIFIER — strict by design
//   Counts a Bash call only when it matches a strict read-only shape. Misses some
//   read-only idioms (`2>/dev/null`, `git -C <path> log`, pipes, `find -name '*.py'`) on
//   purpose: a missed nudge costs nothing, a "batch these" nudge on ordered writes
//   misleads. Every Bash command takes only plain characters (PLAIN_WORDS): the guards
//   read raw text, and quotes, escapes, braces, `$`, globs and zsh `=(...)` / `*(e:...:)`
//   let the shell rewrite words or run code after the check (`find . {-delete,-print}`,
//   `cat =(touch x)`), so a denylist of such characters kept missing one.
//
// STATE — one file per agent
//   /tmp/claude-state-<session_id>/batch/main.json, .../agent-<agent_id>.json — {count}
//   The `agent-` prefix keeps an agent_id of "main" from addressing the main agent's file.
//   Subagents and in-process teammates share the session_id; only agent_id tells them
//   apart. One shared counter summed interleaved agents' single calls and nudged the
//   wrong agent, and one agent's parallel batch wiped another's streak. Writes go
//   through tmp + rename so a concurrent reader never sees a torn file.
//   Reset by a parallel or non-batchable batch of the same agent, after each nudge
//   (cooldown — one nudge per streak), for the main agent by UserPromptSubmit, and for
//   a subagent by deleting its file on SubagentStart (spawn, resume, teammate handling
//   a new message — the subagent's own turn boundary) and SubagentStop (cleanup). Both
//   events carry the subagent's own agent_id (hooks reference).
//
// EXIT CODES
//   0  always

const fs = require("fs");
const os = require("os");
const path = require("path");

const NUDGE_THRESHOLD = 4; // consecutive single-call read-only batches before nudging

function getSentinelDir() {
  return process.platform === "win32" ? os.tmpdir() : "/tmp";
}

const sanitize = (s) => String(s).replace(/[^a-zA-Z0-9_-]/g, "_");

// Batchable tool types — independent, read-only, safe to fire in parallel.
const BATCHABLE_TOOLS = new Set(["Read", "Grep", "Glob"]);
// Bash first-tokens that are read-only in every form.
const READONLY_BASH_PREFIXES = new Set(["ls", "grep", "cat", "head", "tail", "wc"]);
// git is read-only only for these subcommands; add/commit/push are ordered writes.
const READONLY_GIT_SUBCOMMANDS = new Set(["log", "status", "diff", "show", "rev-parse", "ls-files", "blame"]);
// git log/diff/show --output writes a file.
const GIT_WRITE_FLAG = /(^|\s)--output(=|\s|$)/;
// find actions that write, delete or run commands.
const FIND_WRITE_ACTION = /(^|\s)-(delete|exec|execdir|ok|okdir|fprint|fprint0|fprintf|fls)(\s|$)/;
// Allowlist for every Bash command: none of these runs code, chains, redirects or splits a
// word in bash or zsh, so whitespace split = the shell's words and the write-flag guards
// above see every flag. Only zsh `=word` (EQUALS) expands, to one command path, never a flag.
const PLAIN_WORDS = /^[A-Za-z0-9_./=:@%,+ \t-]+$/;

function isBatchable(toolName, toolInput) {
  if (BATCHABLE_TOOLS.has(toolName)) return true;
  if (toolName !== "Bash") return false;
  const cmd = ((toolInput && toolInput.command) || "").trim();
  if (!cmd || !PLAIN_WORDS.test(cmd)) return false;
  const [first, second] = cmd.split(/\s+/);
  if (first === "git") return READONLY_GIT_SUBCOMMANDS.has(second) && !GIT_WRITE_FLAG.test(cmd);
  if (first === "find") return !FIND_WRITE_ACTION.test(cmd);
  return READONLY_BASH_PREFIXES.has(first);
}

let raw = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (d) => (raw += d));
process.stdin.on("end", () => {
  try {
    const { hook_event_name, session_id, agent_id, tool_calls } = JSON.parse(raw);
    const tmpDir = path.join(getSentinelDir(), `claude-state-${sanitize(session_id || "default")}`, "batch");
    const streakFile = path.join(tmpDir, agent_id ? `agent-${sanitize(agent_id)}.json` : "main.json");

    const writeStreak = (count) => {
      const tmp = `${streakFile}.${process.pid}.tmp`;
      try {
        fs.mkdirSync(tmpDir, { recursive: true });
        fs.writeFileSync(tmp, JSON.stringify({ count }));
        fs.renameSync(tmp, streakFile);
      } catch (_) {
        try {
          fs.unlinkSync(tmp);
        } catch (_) {}
      }
    };

    if (hook_event_name === "UserPromptSubmit") {
      // New user turn — a fresh streak boundary for the main agent.
      writeStreak(0);
      process.exit(0);
    }

    if (hook_event_name === "SubagentStart" || hook_event_name === "SubagentStop") {
      // Without agent_id the file would be main's — never touch it from a subagent event.
      if (agent_id) {
        try {
          fs.unlinkSync(streakFile);
        } catch (_) {}
      }
      process.exit(0);
    }

    if (hook_event_name !== "PostToolBatch") process.exit(0);

    const calls = Array.isArray(tool_calls) ? tool_calls : [];
    const call = calls.length === 1 && calls[0] && typeof calls[0] === "object" ? calls[0] : null;
    // 2+ calls = already parallel. A non-batchable tool in the middle of a run usually means a
    // real dependency (e.g. Read result feeding an Edit), not a missed-batch opportunity.
    if (!call || !isBatchable(call.tool_name, call.tool_input)) {
      writeStreak(0);
      process.exit(0);
    }

    let streak = 0;
    try {
      streak = JSON.parse(fs.readFileSync(streakFile, "utf8")).count || 0;
    } catch (_) {}
    streak += 1;

    if (streak < NUDGE_THRESHOLD) {
      writeStreak(streak);
      process.exit(0);
    }

    writeStreak(0);
    const out = {
      hookSpecificOutput: {
        hookEventName: "PostToolBatch",
        additionalContext:
          `Batching reminder: ${streak} consecutive single-call batches of read-only calls. ` +
          "If upcoming calls don't depend on each other's output, put them in the same response " +
          "as parallel tool_use blocks — see rules/foundry-claude-config.md §Turn Batching.",
      },
    };
    // Exit from the write callback: a pipe write may still be pending when exit() runs.
    process.stdout.write(JSON.stringify(out), () => process.exit(0));
  } catch (_) {
    // Never crash or block Claude due to a hook bug.
    process.exit(0);
  }
});
