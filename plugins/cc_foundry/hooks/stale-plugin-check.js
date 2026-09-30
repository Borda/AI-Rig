#!/usr/bin/env node
// stale-plugin-check.js — SessionStart hook (every source)
//
// PURPOSE
//   A Claude Code process keeps the plugin versions it loaded; installing a newer
//   version does not replace them in a session that is already running. Skills then
//   run an old copy with no visible sign — observed as an /oss:resolve run executing
//   0.40.3 steps a day after 0.41.1 was installed. This hook compares the foundry
//   copy this session loaded with the one currently installed and warns when they
//   differ, so the user restarts before any work happens on stale instructions.
//
// SIGNALS (either one → stale)
//   1. The loaded root carries `.orphaned_at` — Claude Code marks a version dir that
//      way once a newer install replaces it.
//   2. installed_plugins.json has no entry for this plugin whose installPath is the
//      loaded root.
//   Foundry is the proxy for the whole session: every plugin loads at process start,
//   so a stale foundry means every plugin installed since then is stale too.
//
// SCOPE
//   Only roots under <config>/plugins/cache/<marketplace>/<plugin>/<version>/ are
//   checked. A source-tree or --plugin-dir root is silent — there is no install record
//   to compare against.
//
// INJECTION
//   Raw stdout, same as session-restore.js: SessionStart stdout is added as context.
//
// EXIT CODES
//   0  always — warn (stdout) or stay silent. Never blocks session start.

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

function staleMessage(root) {
  const id = loadedIdentity(root);
  if (!id) return "";
  const entries = installedEntries(`${id.plugin}@${id.marketplace}`);
  if (entries === null) return "";
  const orphaned = fs.existsSync(path.join(root, ".orphaned_at"));
  const matched = entries.some((e) => e && typeof e.installPath === "string" && samePath(e.installPath, root));
  if (!orphaned && matched) return "";
  const versions = entries.map((e) => e && e.version).filter(Boolean);
  const installed = versions.length ? versions.join(", ") : "none";
  return (
    `⚠ STALE PLUGIN SESSION: this Claude Code session runs ${id.plugin} ${id.version}` +
    `${orphaned ? " (replaced — marked orphaned)" : ""}, but the installed version is ${installed}. ` +
    "Every plugin installed after this session started is running an older copy. " +
    "Tell the user before any other work: quit and restart Claude Code to load the installed plugins.\n"
  );
}

function main() {
  let raw = "";
  try {
    raw = fs.readFileSync(0, "utf8");
  } catch {
    // no stdin — still check; the event gate below only filters explicit other events
  }
  try {
    const payload = raw.trim() ? JSON.parse(raw) : {};
    if (payload.hook_event_name && payload.hook_event_name !== "SessionStart") return;
  } catch {
    // malformed payload — the check does not depend on it
  }
  const root = process.env.CLAUDE_PLUGIN_ROOT || path.resolve(__dirname, "..");
  const message = staleMessage(root);
  if (message) process.stdout.write(message);
}

try {
  main();
} catch {
  // never block session start
}
process.exit(0);
