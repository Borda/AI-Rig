#!/usr/bin/env node
// rule-inject.js — PostToolUse + PostToolUseFailure (Agent|Bash), SessionStart (compact|clear) and
// PreCompact hook
//
// PURPOSE
//   Some foundry rules matter only during one activity: the spawn rules while calling Agent(),
//   the commit rule while staging, committing or pushing. Loading them at session start re-sends
//   their whole text on every turn of every session. They stay out of the always-loaded rule set
//   and this hook injects each one the first time its activity runs — once per session, and once
//   per subagent, since every subagent has its own context.
//
// RULES (one source of truth: the plugin's own rules/<file>, read at run time)
//   agent-spawn.md   first Agent call
//   git-commit.md    first Bash call with a git invocation that stages, commits or pushes:
//                    `add`, `commit`, `push`, the commit-creating `merge`, `revert`, `cherry-pick`,
//                    `am`, `rebase`, and `tag` with -a/-s/-u/-m/-F (an annotated or signed tag).
//                    Detection (lib/shell-git.js) sees through grouping, prefixes, quoting and
//                    nested shell source: `{ git commit; }`, `if x; then git commit; fi`,
//                    `time|sudo|nohup|xargs git add`, `git -C "dir with space" push`,
//                    `bash -c "git commit -m x"`. `echo git commit-ish`, `git commit-tree`,
//                    `git stash push` and `git status` do not trigger. Inline aliases
//                    (`git -c alias.ci=commit ci`) resolve; aliases from git config (`git ci`) do
//                    not: reading git config inside the hook is not worth a rare spelling, so such
//                    an alias delivers the rule only on the next literal trigger.
//
// TIMING — the rule arrives after the call that triggered it
//   Claude Code places additionalContext "next to the tool result" for PreToolUse, PostToolUse and
//   PostToolUseFailure alike (hooks reference, "Add context for Claude"), so the model reads it
//   after the triggering call was composed and ran. The first commit of a session or subagent is
//   drafted before this rule is in context; the commit-message format bullets therefore stay
//   eager in rules/claude-config.md, and this injection supplies the full rule for every later
//   commit and for the redraft after a failed one.
//
// WHY POST, NOT PRE
//   The once-per-context slot must be spent only on a call that ran. A call blocked by a
//   PreToolUse hook (commit-guard.js stopping an unauthorized `git push`) or denied by the user
//   fires neither PostToolUse nor PostToolUseFailure ("Permission denials fire PreToolUse but not
//   this event"), so it claims nothing and the next call that runs injects. On PreToolUse the
//   slot was claimed before the block, and whether Claude Code still delivers additionalContext
//   beside a blocked call is undocumented. PostToolUseFailure covers a call that ran and failed —
//   a non-zero Bash exit such as a commit rejected by pre-commit — where the rule is needed for
//   the redraft.
//
// INJECTION
//   One JSON object on stdout, hookEventName echoing the firing event:
//     {"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":"<rule>"}}
//   Claude Code adds additionalContext as a system reminder; it changes nothing about the call.
//   Payload = a one-line header + the rule file without frontmatter. Claude Code caps
//   additionalContext at 10,000 chars and replaces a longer one with a file path plus a preview;
//   above INJECT_CAP this hook instead sends the rule's frontmatter description and the paths to
//   Read (rule file, its rules/_full/ twin when present), never a body cut mid-rule.
//
// ONCE PER SESSION AND SUBAGENT
//   Sentinel <TMPDIR or os.tmpdir()>/foundry-rule-inject-<rule>-<agent>-<session_id>, created
//   exclusively ("wx") so parallel calls in one response inject once. <agent> = the payload's
//   agent_id inside a subagent, "main" in the main conversation.
//   SessionStart compact/clear rebuilds the main context and drops earlier injections, so it
//   unlinks this session's sentinels (exact `-<session_id>` suffix, never another session's) and
//   the next activity injects again. On /clear the session id usually changes, so that branch is
//   mostly a no-op safety net for an id that survives. Subagent sentinels go too: one extra
//   injection is cheaper than a rule lost.
//   A subagent's own auto-compaction is not a SessionStart. PreCompact carrying agent_id unlinks
//   that subagent's sentinels — but the hooks reference documents PreCompact input as `trigger`
//   and `custom_instructions` only and never states that PreCompact fires inside a subagent, so
//   this branch is unverified: inert unless Claude Code sends it, and a long subagent may lose
//   the rule after compacting. A PreCompact without agent_id (main thread) changes nothing; the
//   SessionStart compact that follows handles it. Neither event writes stdout.
//
// EXIT CODES
//   0  always — inject or stay silent. Every error (no session id, unreadable rule, unwritable temp
//      dir, malformed payload) is silent: fail open, never affect a tool or a compaction.

"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");

// Below Claude Code's 10,000-char additionalContext cap, with room for the header line.
const INJECT_CAP = 9500;
const SENTINEL_PREFIX = "foundry-rule-inject-";
// Events fired only for a call that ran; both accept hookSpecificOutput.additionalContext.
const RAN_EVENTS = new Set(["PostToolUse", "PostToolUseFailure"]);
// SessionStart sources that rebuild the context and drop earlier system reminders.
const CONTEXT_RESET_SOURCES = new Set(["compact", "clear"]);
const COMMIT_SUBCOMMANDS = new Set(["add", "commit", "push", "merge", "revert", "cherry-pick", "am", "rebase"]);
// `git tag` creates a tag object with a message only when annotated, signed or given one.
const TAG_MESSAGE_FLAG = /^(?:-[^-]*[asumF]|--(?:annotate|sign|local-user|message|file)(?:=|$))/;

const RULES = {
  spawn: { file: "agent-spawn.md", activity: "the first Agent() call", scope: "spawn" },
  commit: { file: "git-commit.md", activity: "the first commit-creating git call", scope: "commit and push" },
};

// --- rule selection ---

function isCommitTrigger(args) {
  const [subcommand, ...rest] = args;
  if (COMMIT_SUBCOMMANDS.has(subcommand)) return true;
  return subcommand === "tag" && rest.some((arg) => TAG_MESSAGE_FLAG.test(arg));
}

function ruleFor(payload) {
  if (payload.tool_name === "Agent") return "spawn";
  if (payload.tool_name !== "Bash") return null;
  // Required here, inside main()'s try: a missing library must fail open like every other error.
  const { gitSubcommandArgs } = require(path.join(__dirname, "lib", "shell-git.js"));
  return gitSubcommandArgs((payload.tool_input || {}).command).some(isCommitTrigger) ? "commit" : null;
}

// --- rule text ---

function splitFrontmatter(text) {
  const match = text.match(/^---\r?\n([\s\S]*?)\r?\n---\r?\n/);
  if (!match) return { front: "", body: text.trim() };
  return { front: match[1], body: text.slice(match[0].length).trim() };
}

function description(front) {
  const line = front.split(/\r?\n/).find((l) => l.startsWith("description:"));
  return line ? line.slice("description:".length).trim() : "";
}

/** additionalContext for one rule, or "" when the rule file is missing or empty. */
function ruleContext(key) {
  const rule = RULES[key];
  const root = process.env.CLAUDE_PLUGIN_ROOT || path.resolve(__dirname, "..");
  const file = path.join(root, "rules", rule.file);
  const { front, body } = splitFrontmatter(fs.readFileSync(file, "utf8"));
  if (!body) return "";
  const header = `foundry rule ${rule.file} (injected once by rule-inject.js; binds every later ${rule.scope}):`;
  const full = `${header}\n\n${body}`;
  if (full.length <= INJECT_CAP) return full;
  const twin = path.join(root, "rules", "_full", rule.file);
  const detail = fs.existsSync(twin) ? `; worked detail: ${twin}` : "";
  return (
    `foundry rule ${rule.file} is too long to inject (${body.length} chars) on ${rule.activity}. ` +
    `Summary: ${description(front) || rule.file}. Read ${file} now, before the next ${rule.scope}${detail}.`
  );
}

// --- sentinels ---

function token(value) {
  return typeof value === "string" ? value.trim().replace(/[^A-Za-z0-9_-]/g, "_") : "";
}

function sentinelDir() {
  return process.env.TMPDIR || os.tmpdir();
}

function sentinelPath(key, agent, sid) {
  return path.join(sentinelDir(), `${SENTINEL_PREFIX}${key}-${agent}-${sid}`);
}

/** Claim the once-per-session slot; true only for the one caller that created the sentinel. */
function claim(key, payload, sid) {
  try {
    fs.writeFileSync(sentinelPath(key, token(payload.agent_id) || "main", sid), `${Date.now()}\n`, { flag: "wx" });
    return true;
  } catch {
    return false; // EEXIST: already injected; anything else: fail open without output
  }
}

function clearSession(sid) {
  const dir = sentinelDir();
  for (const name of fs.readdirSync(dir)) {
    if (!name.startsWith(SENTINEL_PREFIX) || !name.endsWith(`-${sid}`)) continue;
    try {
      fs.unlinkSync(path.join(dir, name));
    } catch {
      // already gone, or not ours to remove
    }
  }
}

function clearAgent(agent, sid) {
  for (const key of Object.keys(RULES)) {
    try {
      fs.unlinkSync(sentinelPath(key, agent, sid));
    } catch {
      // never injected in this subagent, or already cleared
    }
  }
}

// --- entry ---

function readPayload() {
  const raw = fs.readFileSync(0, "utf8");
  const payload = raw.trim() ? JSON.parse(raw) : {};
  return payload && typeof payload === "object" ? payload : {};
}

function main() {
  const payload = readPayload();
  const sid = token(payload.session_id);
  const event = payload.hook_event_name;
  if (!sid) return;
  if (event === "SessionStart") {
    if (CONTEXT_RESET_SOURCES.has(payload.source)) clearSession(sid);
    return;
  }
  if (event === "PreCompact") {
    const agent = token(payload.agent_id);
    if (agent) clearAgent(agent, sid);
    return;
  }
  if (!RAN_EVENTS.has(event)) return;
  const key = ruleFor(payload);
  if (!key) return;
  const context = ruleContext(key);
  if (!context || !claim(key, payload, sid)) return;
  process.stdout.write(JSON.stringify({ hookSpecificOutput: { hookEventName: event, additionalContext: context } }));
}

try {
  main();
} catch {
  // fail open — never affect a tool, a session start or a compaction
}
// exitCode, not process.exit(): a pipe stdout is asynchronous on macOS, and exiting at once could cut
// an ~8K payload short. Nothing else is pending, so the process ends as soon as stdout drains.
process.exitCode = 0;
