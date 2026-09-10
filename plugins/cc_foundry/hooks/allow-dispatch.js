#!/usr/bin/env node
// allow-dispatch.js — PreToolUse hook (matcher: Bash)
//
// PURPOSE
//   The single registered auto-allow hook for this plugin. It asks the two
//   decision modules — `blueprint-allow.js` (provenance: this exact command
//   text is in a reviewed, versioned plugin file) and `sentinel-read-allow.js`
//   (shape: this command is a known read-only idiom) — in rank order, emits the
//   first allow, and appends one audit record describing what both of them
//   said. The modules keep their own logic, their own standalone drivers and
//   their own test suites; this file adds no policy of its own.
//
// HOW IT WORKS
//   1. Read stdin once.
//   2. Inside its own protected block, require `blueprint-allow.js` and call
//      `evaluate(raw)`.
//   3. If blueprint allowed, write its payload to stdout immediately.
//   4. Inside its own protected block, require `sentinel-read-allow.js` and
//      call `evaluate(raw)`.
//   5. If nothing has been written yet, write shape's payload when it allowed;
//      otherwise write nothing.
//   6. Append one audit record carrying the effective verdict and both lanes'
//      raw verdicts — unless both lanes returned `none`, in which case there is
//      no opinion to record and nothing is written.
//
// ALLOW-ONLY — WHY IT EXITS 0 ON EVERY PATH
//   The only decision this hook can ever emit is `allow`. A crash therefore
//   grants nothing and the call falls back to normal permission handling, which
//   is the safe direction; exiting 2 would block a call this hook was never
//   empowered to refuse. A module that throws contributes `none` /
//   `module-error` and never suppresses the other module. Audit failure is
//   invisible to the decision: it cannot change stdout, the exit code, or which
//   payload was chosen.
//
// EXIT CODES
//   0  always — see ALLOW-ONLY above. This hook has no blocking path.
//
// ACCEPTED BEHAVIOUR DELTAS versus registering the two modules separately
//   1. When both modules allow, the host receives ONE allow payload instead of
//      two. The decision is identical and the reason string is blueprint's;
//      rank order makes that deterministic where parallel hooks were not.
//   2. A crash in this file loses both verdicts, where a crash in one of two
//      separately registered hooks lost only one. The per-module protected
//      blocks confine that to this file's own stdin handling.
//   3. The two evaluations share one process, one timeout and one failure
//      boundary. A stall anywhere can push the hook past its timeout, and the
//      host then discards its ENTIRE output — printing early does not save an
//      already-written allow. What step 3 does buy is that a slow shape
//      evaluation cannot delay a blueprint allow that already completed, and
//      that audit I/O sits after every decision.
//
// ENVIRONMENT
//   RIG_AUDIT=0  disables audit writing. The hook still evaluates both modules
//                and still prints its payload: the switch disables logging,
//                never permission behaviour. Checked before the audit library is
//                required, so a disabled audit costs nothing and cannot fail
//                during module load.

"use strict";

const path = require("path");

/** Rank order is the contract: blueprint outranks shape, so its allow and its reason string win. */
const LANES = [
  { name: "blueprint", module: "blueprint-allow.js" },
  { name: "shape", module: "sentinel-read-allow.js" },
];

/** This hook's short name, used to build `agent_id`. */
const HOOK_NAME = "allow-dispatch";

/**
 * Evaluate one decision module without letting its failure reach the other.
 * A module that cannot be required, or that throws, is reported as `none` / `module-error`: the absence of an
 * opinion, never an abstention and never a reason to suppress the other lane.
 */
function evaluateLane(lane, raw) {
  try {
    const mod = require(path.join(__dirname, lane.module));
    const result = mod.evaluate(raw);
    if (!result || typeof result !== "object") {
      return { lane: lane.name, decision: "none", why: "module-error", payload: null };
    }
    return result;
  } catch (_error) {
    return { lane: lane.name, decision: "none", why: "module-error", payload: null };
  }
}

/** Return the compact per-lane verdict that goes into `action_detail.verdicts`. */
function verdictOf(result) {
  const verdict = { lane: result.lane, decision: result.decision };
  if (typeof result.why === "string") verdict.why = result.why;
  if (typeof result.src === "string") verdict.src = result.src;
  return verdict;
}

/**
 * Return the `action_detail` of the before-row, or null when neither lane reached a decision.
 * The effective decision is the winning lane's when one allowed, `passthrough` when at least one lane examined the
 * command and declined, and nothing at all when both returned `none`.
 */
function beforeDetail(results) {
  // The winner is the first lane that both said allow AND produced a payload, which is exactly the test `dispatch`
  // uses to decide what to print. A lane claiming an allow with no payload would otherwise put an allow in the record
  // that the host was never told about.
  const winner = results.find((result) => result.decision === "allow" && result.payload) || null;
  const decided = results.filter((result) => result.decision !== "none");
  if (winner === null && decided.length === 0) return null;

  const detail = winner
    ? { decision: "allow", lane: winner.lane, rank: winner.rank }
    : { decision: "passthrough", lane: "none" };
  if (winner && typeof winner.src === "string") detail.src = winner.src;
  // The digest is the blueprint normalizer's, the only one that exists, so it is present exactly when the blueprint
  // lane normalized the command. A shape-only allow therefore carries no digest, which is correct rather than a gap.
  // Found by lane name rather than by position, so reordering LANES cannot attribute it to a lane that never
  // normalized anything.
  const normalizer = results.find((result) => result.lane === "blueprint") || {};
  if (typeof normalizer.digest === "string") detail.digest = normalizer.digest;
  detail.verdicts = results.map(verdictOf);
  return detail;
}

/** Return the stdin fields the record needs, tolerating any malformed payload. */
function context(raw) {
  try {
    const data = JSON.parse(raw);
    if (!data || typeof data !== "object") return {};
    return { session_id: data.session_id, tool_use_id: data.tool_use_id, project: data.cwd };
  } catch (_error) {
    return {};
  }
}

/**
 * Append the before-row. Every failure here is swallowed: the decision has already been printed, and no audit
 * outcome may change stdout, the exit code, or what the host was told.
 */
function recordDecision(raw, results) {
  if (process.env.RIG_AUDIT === "0") return;
  try {
    const detail = beforeDetail(results);
    if (detail === null) return;
    const audit = require(path.join(__dirname, "lib", "audit-log.js"));
    const { session_id: sessionId, tool_use_id: toolUseId, project } = context(raw);
    audit.appendRecord({
      agent_id: `${audit.logicalPlugin()}/${HOOK_NAME}`,
      agent_version: audit.pluginVersion(),
      session_id: sessionId ?? null,
      tool_use_id: toolUseId ?? null,
      project: project ?? null,
      action_type: "tool.bash",
      action_detail: detail,
      outcome: "pending",
      trust_level: detail.decision === "allow" ? "plugin" : "unknown",
      parent_record_id: null,
      prev_hash: null,
      record_phase: "pre_execution",
    });
  } catch (_error) {
    // Logging must never change the decision that was already emitted.
  }
}

/**
 * Run both lanes over `raw`, write at most one payload, and record what happened.
 * Returns the payload that was written, or null — the stdout oracle is `payload ? JSON.stringify(payload) : <empty>`,
 * and never the string "null".
 */
function dispatch(raw, write) {
  const results = [];
  let printed = null;
  for (const lane of LANES) {
    const result = evaluateLane(lane, raw);
    results.push(result);
    if (printed === null && result.decision === "allow" && result.payload) {
      printed = result.payload;
      write(JSON.stringify(printed));
    }
  }
  recordDecision(raw, results);
  return printed;
}

if (require.main === module) {
  let raw = "";
  process.stdin.setEncoding("utf8");
  process.stdin.on("data", (chunk) => (raw += chunk));
  process.stdin.on("end", () => {
    try {
      dispatch(raw, (text) => process.stdout.write(text));
    } catch (_error) {
      // Never crash or block Claude due to a hook bug.
    }
    process.exit(0);
  });
}

module.exports = {
  dispatch,
  beforeDetail,
  evaluateLane,
};
