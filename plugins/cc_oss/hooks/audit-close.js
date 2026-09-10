#!/usr/bin/env node
// audit-close.js — PostToolUse / PostToolUseFailure (matcher: Bash), SessionStart, SessionEnd
//
// PURPOSE
//   Record what happened AFTER a decision: that a Bash tool call completed, and
//   that a session began and ended. `allow-dispatch.js` writes the decision;
//   this hook writes the observation that closes it.
//
// HOW IT WORKS
//   1. Parse stdin for `hook_event_name`, `tool_name`, `session_id`,
//      `tool_use_id` and `cwd`.
//   2. PostToolUse / PostToolUseFailure on a Bash call append one after-row
//      carrying only a status word — `ok` or `error` — and which event fired.
//   3. SessionStart / SessionEnd append one lifecycle row, skipped entirely
//      when the session id is unusable: there is nothing to bound.
//   4. Anything else appends nothing.
//
// EVERY PLUGIN WRITES ITS OWN ROW
//   Four installed plugins produce four after-rows for one completed call.
//   They are four independent observations of the same fact, not duplicates:
//   there is no claim, no dedupe and no tombstone, so nothing has to coordinate
//   and nothing can lose a row by losing a race. The reader reconciles them by
//   `(session_id, tool_use_id)` and treats agreement as corroboration.
//
// NEVER WRITES TO STDOUT — ON ANY EVENT
//   `SessionStart` stdout is injected into the model's context, and PostToolUse
//   stdout can interfere with tool-result handling. This hook prints nothing,
//   ever, on every event it handles and every one it ignores.
//
// EXIT CODES
//   0  always. A logging hook must never interfere with execution.
//
// ENVIRONMENT
//   RIG_AUDIT=0  disables audit writing; checked before the audit library is
//                required, so a disabled audit costs nothing.

"use strict";

const path = require("path");

/** This hook's short name, used to build `agent_id`. */
const HOOK_NAME = "audit-close";

/** Completion events, mapped to the status word `timings.jsonl` already uses for the same two outcomes. */
const CLOSE_EVENTS = { PostToolUse: "ok", PostToolUseFailure: "error" };

/** Lifecycle events, mapped to their `action_type`. */
const LIFECYCLE_EVENTS = { SessionStart: "session.start", SessionEnd: "session.end" };

/**
 * Return the record to append for one stdin payload, or null when this event is not ours.
 * Kept separate from the append so it can be tested without a filesystem, and so the append path has no branching.
 */
function buildRecord(data, identity) {
  const event = data.hook_event_name;
  // A property lookup coerces its key, so `["PostToolUse"]` would pass the membership tests below and be written
  // verbatim into `action_detail.event` — a record no reader can classify, from a payload this hook should ignore.
  if (typeof event !== "string") return null;
  const common = {
    agent_id: `${identity.plugin}/${HOOK_NAME}`,
    agent_version: identity.version,
    session_id: data.session_id ?? null,
    project: data.cwd ?? null,
    parent_record_id: null,
    prev_hash: null,
    record_phase: "post_execution",
  };

  if (Object.prototype.hasOwnProperty.call(CLOSE_EVENTS, event)) {
    if (data.tool_name !== "Bash") return null;
    const status = CLOSE_EVENTS[event];
    return {
      ...common,
      tool_use_id: data.tool_use_id ?? null,
      action_type: "tool.bash",
      action_detail: { status, event },
      outcome: status === "ok" ? "success" : "failure",
      trust_level: "unknown",
    };
  }

  if (Object.prototype.hasOwnProperty.call(LIFECYCLE_EVENTS, event)) {
    // A lifecycle row exists to bound one session's rows. With no usable session id there is no session to bound,
    // and the shared `_no-session.jsonl` stream would gain a boundary belonging to nothing. The test is the library's
    // own `usableId`, not a length check: an ill-formed id passes a length check, then seals to null and lands in
    // exactly the shared stream this guard exists to keep boundaries out of.
    if (identity.usableId(data.session_id) === null) return null;
    // `reason` is a session.end field. The host sends `source` on SessionStart today, but a `reason` there would be
    // copied onto a session.start row that the reader rejects as schema-invalid — once per plugin, every session.
    const detail = {};
    if (event === "SessionEnd" && typeof data.reason === "string" && data.reason.length > 0) {
      detail.reason = data.reason;
    }
    return {
      ...common,
      action_type: LIFECYCLE_EVENTS[event],
      action_detail: detail,
      outcome: "success",
      trust_level: "unknown",
    };
  }

  return null;
}

/** Append the row for one raw stdin string. Swallows every failure; returns the record written, or null. */
function close(raw) {
  if (process.env.RIG_AUDIT === "0") return null;
  try {
    const data = JSON.parse(raw);
    if (!data || typeof data !== "object") return null;
    const audit = require(path.join(__dirname, "lib", "audit-log.js"));
    const record = buildRecord(data, {
      plugin: audit.logicalPlugin(),
      version: audit.pluginVersion(),
      usableId: audit.usableId,
    });
    if (record === null) return null;
    return audit.appendRecord(record);
  } catch (_error) {
    // A logging hook must never interfere with execution.
    return null;
  }
}

if (require.main === module) {
  let raw = "";
  process.stdin.setEncoding("utf8");
  process.stdin.on("data", (chunk) => (raw += chunk));
  process.stdin.on("end", () => {
    try {
      close(raw);
    } catch (_error) {
      // Never crash due to a hook bug.
    }
    process.exit(0);
  });
}

module.exports = {
  buildRecord,
  close,
};
