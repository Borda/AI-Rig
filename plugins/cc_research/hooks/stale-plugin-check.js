#!/usr/bin/env node
// stale-plugin-check.js — SessionStart hook (every source; the ask mandate is source-aware)
//
// PURPOSE
//   A Claude Code process keeps the plugin versions it loaded; installing a newer
//   version does not replace them in a session that is already running. Skills then
//   run an old copy with no visible sign — observed as an /oss:resolve run executing
//   0.40.3 steps a day after 0.41.1 was installed, and again as a session running a
//   replaced oss while the only warning named foundry. Every plugin ships a
//   byte-identical copy of this hook, and each copy checks only its own loaded root:
//   plugins install and update independently, so no one plugin can stand in for the
//   others, and the warning must name the plugin that is actually stale.
//
// SIGNALS (either one → stale)
//   1. The loaded root carries `.orphaned_at` — Claude Code marks a version dir that
//      way once a newer install replaces it.
//   2. installed_plugins.json has no entry for this plugin whose installPath is the
//      loaded root.
//
// SCOPE
//   Only roots under <config>/plugins/cache/<marketplace>/<plugin>/<version>/ are
//   checked. A source-tree or --plugin-dir root is silent — there is no install record
//   to compare against.
//
// OUTPUT
//   Stale → one JSON object on stdout and nothing else, without a trailing newline:
//   SessionStart parses stdout as JSON only when it starts with `{` and ends with `}`.
//     systemMessage          shown to the user — plugin, loaded and installed versions,
//                            and the restart step (`claude --resume <session_id>` when
//                            the payload carries a session id)
//     hookSpecificOutput     additionalContext for the model — stop that plugin's
//                            workflows on the stale copy and ask the user before going on
//   Current, unreadable, or not checkable → no stdout.
//
// ASK ONCE PER SESSION
//   Compaction and /clear re-run SessionStart (source compact/clear) and drop the
//   user's earlier answer, so a repeated mandate re-asked after every compaction.
//   Whenever the mandate is emitted, a sentinel ${TMPDIR}/stale-plugin-warned-
//   <plugin>-<version>-<CSID> is written; on compact/clear with that sentinel the
//   context omits the AskUserQuestion mandate (the user still sees systemMessage).
//   Without it the mandate stays: a plugin updated mid-session is first detectable
//   at a compact/clear, since the loaded copy was current at startup. startup and
//   resume always carry the mandate; no session id → always the mandate; a /clear
//   that starts a new session id asks once more under that id.
//
// EXIT CODES
//   0  always — warn or stay silent. Never blocks session start; every error is silent.

"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");

function configDir() {
  return process.env.CLAUDE_CONFIG_DIR || path.join(os.homedir(), ".claude");
}

function samePath(a, b) {
  const norm = (p) => path.resolve(p).replace(/[\\/]+$/, "");
  return process.platform === "win32" ? norm(a).toLowerCase() === norm(b).toLowerCase() : norm(a) === norm(b);
}

function loadedIdentity(root) {
  const cache = path.join(configDir(), "plugins", "cache");
  const rel = path.relative(cache, root);
  if (!rel || rel.startsWith("..") || path.isAbsolute(rel)) return null;
  const [marketplace, plugin, version, ...rest] = rel.split(/[\\/]+/);
  if (!marketplace || !plugin || !version || rest.length) return null;
  return { marketplace, plugin, version };
}

function installedEntries(key) {
  try {
    const data = JSON.parse(fs.readFileSync(path.join(configDir(), "plugins", "installed_plugins.json"), "utf8"));
    const entries = (data.plugins || data)[key];
    return Array.isArray(entries) ? entries : entries ? [entries] : [];
  } catch {
    return null;
  }
}

function staleFinding(root) {
  const id = loadedIdentity(root);
  if (!id) return null;
  const entries = installedEntries(`${id.plugin}@${id.marketplace}`);
  if (entries === null) return null;
  const orphaned = fs.existsSync(path.join(root, ".orphaned_at"));
  const matched = entries.some((e) => e && typeof e.installPath === "string" && samePath(e.installPath, root));
  if (!orphaned && matched) return null;
  const versions = entries.map((e) => e && e.version).filter(Boolean);
  return { ...id, orphaned, installed: versions.length ? versions.join(", ") : "none" };
}

// SessionStart sources that rebuild context inside the same running process.
const REBUILT_CONTEXT_SOURCES = new Set(["compact", "clear"]);

/** Filename-safe session token (env first, then payload), or "" when none can scope a sentinel. */
function sessionToken(payload) {
  for (const candidate of [process.env.CLAUDE_CODE_SESSION_ID, payload.session_id]) {
    const value = typeof candidate === "string" ? candidate.trim() : "";
    if (/^[A-Za-z0-9_-]+$/.test(value)) return value;
  }
  return "";
}

/** Sentinel recording that this session already got the ask mandate for this stale plugin version, or null. */
function warnedSentinel(finding, csid) {
  if (!csid) return null;
  const name = `stale-plugin-warned-${finding.plugin}-${finding.version}-${csid}`.replace(/[^A-Za-z0-9._-]/g, "_");
  return path.join(process.env.TMPDIR || os.tmpdir(), name);
}

function warning(finding, sessionId, askedBefore) {
  const { plugin } = finding;
  const loaded = `${plugin} ${finding.version}${finding.orphaned ? " (replaced — marked orphaned)" : ""}`;
  const restart = sessionId ? `exit, then claude --resume ${sessionId}` : "exit and restart Claude Code";
  const instruction = askedBefore
    ? "The user was already asked about this stale copy earlier in this session and sees this warning again now; " +
      `do not ask again. Before running a ${plugin} skill or a workflow that depends on ${plugin} hooks, say in ` +
      "one line that it runs the stale copy."
    : `Do not run ${plugin} skills or workflows that depend on ${plugin} hooks on it. In your first reply, tell ` +
      "the user which plugin is stale with its loaded and installed versions, and ask with AskUserQuestion " +
      `whether to stop so they can restart (${restart}) or to continue knowingly on the stale copy. When ` +
      "several plugins report this, ask once covering all of them; once the user chose to continue, do not ask " +
      "again. Never continue silently.";
  return {
    systemMessage:
      `⚠ Stale plugin: this session runs ${loaded}, but the installed version is ${finding.installed}. ` +
      `Its skills, agents and hooks keep running the old copy until restart — ${restart}.`,
    hookSpecificOutput: {
      hookEventName: "SessionStart",
      additionalContext:
        `STALE PLUGIN SESSION (${plugin}): this session loaded ${loaded}, but the installed version is ` +
        `${finding.installed}. Every ${plugin} skill, agent, rule and hook in this session runs that old copy. ` +
        instruction,
    },
  };
}

function readPayload() {
  let raw = "";
  try {
    raw = fs.readFileSync(0, "utf8");
  } catch {
    // no stdin — still check; the check itself does not depend on the payload
  }
  try {
    const payload = raw.trim() ? JSON.parse(raw) : {};
    return payload && typeof payload === "object" ? payload : {};
  } catch {
    return {}; // malformed payload — check anyway, without a session id
  }
}

function main() {
  const payload = readPayload();
  if (payload.hook_event_name && payload.hook_event_name !== "SessionStart") return;
  const root = process.env.CLAUDE_PLUGIN_ROOT || path.resolve(__dirname, "..");
  const finding = staleFinding(root);
  if (!finding) return;
  const sessionId = typeof payload.session_id === "string" ? payload.session_id.trim() : "";
  const sentinel = warnedSentinel(finding, sessionToken(payload));
  const askedBefore = Boolean(sentinel) && REBUILT_CONTEXT_SOURCES.has(payload.source) && fs.existsSync(sentinel);
  if (sentinel && !askedBefore) {
    try {
      fs.writeFileSync(sentinel, `${Date.now()}\n`);
    } catch {
      // unrecorded — the next compact/clear asks once more, never less
    }
  }
  process.stdout.write(JSON.stringify(warning(finding, sessionId, askedBefore)));
}

try {
  main();
} catch {
  // never block session start
}
process.exit(0);
