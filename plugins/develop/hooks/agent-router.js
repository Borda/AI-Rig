#!/usr/bin/env node
// agent-router.js — PreToolUse hook
//
// 3-tier fallback when a requested agent is absent:
//   Tier 1 — exact match in plugin cache or local agents dir → passthrough
//   Tier 2 — LLM picks best fit from local agents (ANTHROPIC_API_KEY)
//   Tier 3 — reroute to general-purpose with original subagent_type in prompt

"use strict";

const fs = require("fs");
const https = require("https");
const os = require("os");
const path = require("path");

const BUILT_INS = new Set(["general-purpose", "claude-code-guide", "Explore", "Plan", "statusline-setup"]);
const LLM_MODEL = "claude-haiku-4-5-20251001";

function semverLatest(versions) {
  return versions
    .filter((v) => /^\d+\.\d+\.\d+/.test(v))
    .sort((a, b) => {
      const pa = a.split(".").map(Number),
        pb = b.split(".").map(Number);
      for (let i = 0; i < 3; i++) if (pa[i] !== pb[i]) return pa[i] - pb[i];
      return 0;
    })
    .pop();
}

function readDescription(filePath) {
  try {
    const fd = fs.openSync(filePath, "r");
    const buf = Buffer.alloc(1024);
    const n = fs.readSync(fd, buf, 0, 1024, 0);
    fs.closeSync(fd);
    const text = buf.slice(0, n).toString("utf8");
    const single = text.match(/^description:\s*(.+)$/m);
    if (single) {
      const val = single[1].trim();
      if (val && !val.startsWith(">") && !val.startsWith("|")) return val;
    }
    const block = text.match(/^description:\s*[>|][^\n]*\n[ \t]+(.+)$/m);
    return block ? block[1].trim() : "";
  } catch (_) {
    return "";
  }
}

function localAgents(dirs) {
  const agents = [];
  for (const dir of dirs) {
    try {
      for (const file of fs.readdirSync(dir)) {
        if (!file.endsWith(".md")) continue;
        const desc = readDescription(path.join(dir, file));
        if (desc) agents.push({ name: file.slice(0, -3), description: desc });
      }
    } catch (_) {}
  }
  return agents;
}

function pluginAgentSet() {
  const agents = new Set();
  const base = path.join(os.homedir(), ".claude", "plugins", "cache");
  try {
    for (const vendor of fs.readdirSync(base)) {
      for (const ns of fs.readdirSync(path.join(base, vendor))) {
        const dir = path.join(base, vendor, ns);
        const ver = semverLatest(fs.readdirSync(dir));
        if (!ver || fs.existsSync(path.join(dir, ver, ".orphaned_at"))) continue;
        try {
          for (const f of fs.readdirSync(path.join(dir, ver, "agents")))
            if (f.endsWith(".md")) agents.add(`${ns}:${f.slice(0, -3)}`);
        } catch (_) {}
      }
    }
  } catch (_) {}
  return agents;
}

function askLlm(agents, query, apiKey) {
  return new Promise((resolve, reject) => {
    const list = agents.map((a) => `- ${a.name}: ${a.description.slice(0, 120)}`).join("\n");
    const content = `Pick the best agent for this request. Reply with ONLY the agent name, or "none".\n\nRequest: ${query}\n\nAgents:\n${list}`;
    const body = JSON.stringify({ model: LLM_MODEL, max_tokens: 50, messages: [{ role: "user", content }] });
    const req = https.request(
      {
        hostname: "api.anthropic.com",
        path: "/v1/messages",
        method: "POST",
        headers: {
          "x-api-key": apiKey,
          "anthropic-version": "2023-06-01",
          "Content-Type": "application/json",
          "Content-Length": Buffer.byteLength(body),
        },
      },
      (res) => {
        let raw = "";
        res.on("data", (d) => (raw += d));
        res.on("end", () => {
          try {
            resolve((JSON.parse(raw).content?.[0]?.text || "none").trim());
          } catch (e) {
            reject(e);
          }
        });
      },
    );
    req.on("error", reject);
    req.write(body);
    req.end();
  });
}

let raw = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (d) => (raw += d));
process.stdin.on("end", async () => {
  try {
    const data = JSON.parse(raw);
    if (data.hook_event_name !== "PreToolUse" || data.tool_name !== "Agent") process.exit(0);

    const subagentType = data.tool_input?.subagent_type || "";
    const prompt = data.tool_input?.prompt || "";
    if (!subagentType || BUILT_INS.has(subagentType)) process.exit(0);

    // Tier 1: exact match
    if (pluginAgentSet().has(subagentType)) process.exit(0);
    const cwd = process.cwd();
    const agents = localAgents([path.join(os.homedir(), ".claude", "agents"), path.join(cwd, ".claude", "agents")]);
    if (!subagentType.includes(":") && agents.some((a) => a.name === subagentType)) process.exit(0);

    // Tier 2: LLM pick
    let target = "general-purpose";
    let note = `[Router: '${subagentType}' not found → general-purpose]`;
    const apiKey = process.env.ANTHROPIC_API_KEY;
    if (apiKey && agents.length > 0) {
      try {
        const picked = await askLlm(agents, `${subagentType.replace(/[:\-_]/g, " ")} ${prompt.slice(0, 300)}`, apiKey);
        if (picked && picked !== "none" && agents.some((a) => a.name === picked)) {
          target = picked;
          note = `[Router: '${subagentType}' → '${target}' (llm)]`;
        }
      } catch (_) {}
    }

    process.stdout.write(
      JSON.stringify({
        hookSpecificOutput: {
          hookEventName: "PreToolUse",
          permissionDecision: "allow",
          updatedInput: { subagent_type: target, prompt: prompt + " " + note },
        },
      }),
    );
  } catch (_) {}
  process.exit(0);
});
