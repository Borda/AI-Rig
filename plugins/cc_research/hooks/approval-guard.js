#!/usr/bin/env node
// approval-guard.js — PostToolUse(AskUserQuestion) approval-record writer + PreToolUse self-grant guard
//
// PURPOSE
//   Runs the two hook halves of lib/approval-grants.js, which holds the scope table, the
//   record store in the git common dir and every rule quoted here.
//   1. PostToolUse(AskUserQuestion): the user's recording answer to a scope's exact question
//      writes that scope's record — `git-approve` answered `Approve always` writes
//      `claude-git-approval.json`, `git-push` answered `Approve` the single-use push token
//      `claude-push-approval.json` for one push of the current branch at the current HEAD, and
//      `gh-write` answered `Approve` the single-use gh write token `claude-gh-write-approval.json`
//      for the one plain gh command the question names in an inline code span, binding the files it
//      names. Each token names the answered call (`question_id`); its spend re-reads that call and
//      answer from the session transcript. `Deny` to `git-push` or `gh-write` removes that pending
//      token. Every other answer and question writes nothing; a refusal (pre-filled answer, spawned
//      agent, undisclosed revoke path, rejected location, detached HEAD, no single gh command span, an
//      invisible or control character, a command that is not one plain gh invocation, stdin) is
//      reported as additionalContext and writes nothing. Of the copies answering one question, only
//      the copy that wrote reports it: the temp file is named after the answer's call id and created
//      exclusively, and a later copy finds the record already holding the answer.
//   2. PreToolUse(Bash|Write|Edit|MultiEdit|NotebookEdit): a direct self-grant exits 2 — a Bash
//      command naming a record as a write target (redirection, output option, or an operand of
//      anything but an exact read, delete, print or git mention), naming the git common dir as a
//      whole in anything but a read, delete or print, or able to execute this writer, and a
//      file-tool write to a record name or to a path named `.git`. The guard sees names, never
//      contents: a patch, `git apply`, an archive, a script or a forged hook payload stays prompt
//      discipline (lib/approval-grants.js LIMITS).
//   Ships byte-identical in foundry, oss, develop and research (bin/propagate_shared.py), so each
//   plugin installed alone carries it; every copy runs, the writer is atomic and idempotent, the
//   guard a pure function of the payload. Reading a record for authority stays with foundry's
//   commit-guard.js (`--check-grant`).
//
// PREMISES — verified, and the one still open
//   * Exit 2 blocks even beside another hook's allow: the hooks reference
//     (code.claude.com/docs/en/hooks) states exit 2 "blocks whether or not you print JSON: even a
//     JSON `permissionDecision` of "allow" can't override it", so allow-dispatch.js's parallel
//     `allow` cannot reopen the call.
//   * Payload and transcript shape: 21 of 21 AskUserQuestion calls in real session transcripts
//     carry `toolUseResult` = {questions, answers, annotations} with `answers` keyed by question
//     text; no assistant `tool_use` input or `wireToolInputs` entry holds `answers`; the
//     `tool_use` line precedes its result line. The hooks reference documents PostToolUse
//     `tool_response` as that result, so the answer is read from there, never from the prose
//     tool_result string. Re-checked 2026-10-10 on 32 more calls: the result is a `type: "user"`
//     line whose `message.content` holds one `tool_result` block naming the call id, with the
//     top-level `toolUseResult` and `timestamp`; it follows its call, not always on the next line.
//     The token spend reads the answer from there.
//   * Still unverified live: that the `tool_use` line is already flushed to `transcript_path`
//     when PostToolUse fires (plan premise probe 2). Settle it with one interactive run answering
//     the git-approve question `Approve always`: the additionalContext names the record path, or
//     the exact refusal reason. Until then a missing line refuses (fail closed): no grant, the
//     question keeps being asked.
//
// EXIT CODES
//   0  passthrough, or PostToolUse done (stdout may carry additionalContext).
//   2  PreToolUse self-grant blocked; stderr names the reason and alternatives.

"use strict";

const path = require("path");

// Loaded defensively: a guard that crashed on a missing module would exit 1, which Claude Code treats
// as a non-blocking error. Without the module nothing is recorded, and a call naming a record is blocked.
let grants = null;
try {
  grants = require(path.join(__dirname, "lib", "approval-grants.js"));
} catch (_) {
  // grants stays null
}

// Record names, for the degraded guard when lib/approval-grants.js cannot load.
const FALLBACK_RECORD_NAME = /(?:git|push|gh-write)-approval\.json/i;

/**
 * Reason to block a PreToolUse call when the module is missing, or null.
 *
 * @param {object} data Parsed hook payload.
 * @returns {string|null}
 */
function fallbackProblem(data) {
  if (!data || data.hook_event_name !== "PreToolUse") return null;
  const input = data.tool_input || {};
  const text = [input.command, input.file_path, input.notebook_path].filter((v) => typeof v === "string").join("\n");
  return FALLBACK_RECORD_NAME.test(text)
    ? "approval-guard cannot load hooks/lib/approval-grants.js; every call naming an approval record is blocked " +
        "until the plugin is reinstalled"
    : null;
}

let raw = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => (raw += chunk));
process.stdin.on("end", () => {
  let data;
  try {
    data = JSON.parse(raw);
  } catch (_) {
    process.exit(0); // malformed payload → passthrough, never block
  }
  if (grants === null) {
    const problem = fallbackProblem(data);
    if (problem) {
      process.stderr.write(`approval record guard — blocked: ${problem}.\n`);
      process.exit(2);
    }
    process.exit(0);
  }
  try {
    const problem = grants.selfGrantProblem(data);
    if (problem) {
      process.stderr.write(grants.blockMessage(problem));
      process.exit(2);
    }
    const envelope = grants.recordAnswers(data);
    if (envelope) process.stdout.write(JSON.stringify(envelope));
  } catch (_) {
    // Never strand the session on a hook bug; the rule still asks without a record.
  }
  process.exit(0);
});
