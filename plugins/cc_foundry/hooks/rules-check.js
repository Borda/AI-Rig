#!/usr/bin/env node
// rules-check.js — SessionStart hook (every source)
//
// PURPOSE
//   Foundry rules reach the model only through `<config>/rules/foundry-<name>.md` links that
//   /foundry:setup creates. When setup never ran or failed (no Python 3.10+ on a blank machine),
//   every rule is silently absent: commits then go out wrapped, without the `---` co-author block,
//   with no sign anything was missing. This hook detects the missing links, names the rule files
//   to read instead, and injects the commit rule, whose violations are the hardest to undo.
//
// SIGNAL
//   Any top-level `<plugin>/rules/<name>.md` without a resolving `<config>/rules/foundry-<name>.md`.
//   A dangling link counts as missing (fs.existsSync follows the link).
//
// INJECTION
//   Raw stdout, same as session-restore.js: SessionStart stdout is added as context.
//
// EXIT CODES
//   0  always — inject (stdout) or stay silent. Never blocks session start.

"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");

const INLINED_RULE = "git-commit.md";

function configDir() {
  return process.env.CLAUDE_CONFIG_DIR || path.join(os.homedir(), ".claude");
}

function ruleNames(root) {
  try {
    return fs
      .readdirSync(path.join(root, "rules"), { withFileTypes: true })
      .filter((entry) => entry.isFile() && entry.name.endsWith(".md"))
      .map((entry) => entry.name)
      .sort();
  } catch {
    return [];
  }
}

function withoutFrontmatter(text) {
  return text.replace(/^---\r?\n[\s\S]*?\r?\n---\r?\n/, "").trim();
}

function missingRulesMessage(root) {
  const names = ruleNames(root);
  const linkDir = path.join(configDir(), "rules");
  const missing = names.filter((name) => !fs.existsSync(path.join(linkDir, `foundry-${name}`)));
  if (!missing.length) return "";
  const listing = missing.map((name) => `- ${path.join(root, "rules", name)}`).join("\n");
  let message =
    `⚠ FOUNDRY RULES NOT INSTALLED: ${missing.length} of ${names.length} foundry rules are missing from ${linkDir}, ` +
    "so they are not in context. Tell the user to run /foundry:setup. Until then, Read the matching file below " +
    `before work its topic covers:\n${listing}\n`;
  if (missing.includes(INLINED_RULE)) {
    try {
      const rule = withoutFrontmatter(fs.readFileSync(path.join(root, "rules", INLINED_RULE), "utf8"));
      message += `\nCommit rule (${INLINED_RULE}), applies to every commit and overrides default commit attribution:\n\n${rule}\n`;
    } catch {
      // listing above still names the file
    }
  }
  return message;
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
  const message = missingRulesMessage(root);
  if (message) process.stdout.write(message);
}

try {
  main();
} catch {
  // never block session start
}
process.exit(0);
