#!/usr/bin/env node
// agent-router.js — SessionStart + PreToolUse hook
//
// PURPOSE
//   3-tier fallback routing for Agent() tool calls when a requested agent is absent.
//   Tier 1: Exact match — agent in plugin cache or user agents dir → passthrough
//   Tier 2: No match → semantic similarity against session-cached agent index → reroute
//   Tier 3: No fit → reroute to general-purpose with routing context in prompt
//
// TIER 2 STRATEGIES (in priority order):
//   A. OpenAI cosine (OPENAI_API_KEY set):
//      SessionStart: embed each local agent description via text-embedding-3-small
//      PreToolUse:   embed query → cosine similarity → best agent above threshold
//
//   B. Anthropic LLM pick (ANTHROPIC_API_KEY set — always available in Claude Code):
//      PreToolUse:   pass agent list + query to claude-haiku → pick best name or "none"
//      No SessionStart work needed for this path
//
// HOW IT WORKS
//   SessionStart:
//     1. Enumerate plugin agents (full cache) → tier-1 presence set
//     2. Enumerate local agents (~/.claude/agents/, .claude/agents/)
//     3. Embed descriptions if OPENAI_API_KEY present; store in session index
//     4. Merge into /tmp/claude-state-<session_id>/agent-router-index.json
//        (additive — never overwrites existing entries)
//
//   PreToolUse(Agent):
//     1. Skip built-ins; read session index (build on-demand if missing)
//     2. Tier 1: check plugin_agents set or bare name in local_agents
//     3. Tier 2A: cosine similarity (OpenAI embeddings) if available
//     3. Tier 2B: LLM pick (Anthropic) if no embeddings
//     4. Above threshold / valid pick → reroute; else tier 3 → general-purpose
//
// EXIT CODES
//   0  passthrough (no output) or rerouted (JSON to stdout)

"use strict";

const fs = require("fs");
const https = require("https");
const os = require("os");
const path = require("path");

// ── Constants ─────────────────────────────────────────────────────────────────

const BUILT_INS = new Set(["general-purpose", "claude-code-guide", "Explore", "Plan", "statusline-setup"]);

const OPENAI_EMBEDDING_MODEL = "text-embedding-3-small";
const COSINE_THRESHOLD = 0.65;

const ANTHROPIC_LLM_MODEL = "claude-haiku-4-5-20251001";

// ── Semver sort ───────────────────────────────────────────────────────────────

function semverLatest(versions) {
  return versions
    .filter((v) => /^\d+\.\d+\.\d+/.test(v))
    .sort((a, b) => {
      const pa = a.split(".").map(Number);
      const pb = b.split(".").map(Number);
      for (let i = 0; i < 3; i++) {
        if (pa[i] !== pb[i]) return pa[i] - pb[i];
      }
      return 0;
    })
    .pop();
}

// ── Atomic write ──────────────────────────────────────────────────────────────

function atomicWrite(filePath, data) {
  const tmp = filePath + ".tmp." + process.pid;
  fs.writeFileSync(tmp, data);
  fs.renameSync(tmp, filePath);
}

// ── File helpers ──────────────────────────────────────────────────────────────

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
      if (val && !val.startsWith(">") && !val.startsWith("|")) return val.toLowerCase();
    }
    const block = text.match(/^description:\s*[>|][^\n]*\n[ \t]+(.+)$/m);
    if (block) return block[1].toLowerCase().trim();
    return "";
  } catch (_) {
    return "";
  }
}

function collectLocalAgents(dir) {
  const agents = [];
  try {
    for (const file of fs.readdirSync(dir)) {
      if (!file.endsWith(".md")) continue;
      const filePath = path.join(dir, file);
      try {
        fs.accessSync(filePath);
      } catch (_) {
        continue;
      }
      const desc = readDescription(filePath);
      if (!desc) continue;
      agents.push({ name: file.slice(0, -3), description: desc });
    }
  } catch (_) {}
  return agents;
}

function collectPluginAgents() {
  const agents = new Set();
  const cacheBase = path.join(os.homedir(), ".claude", "plugins", "cache");
  try {
    for (const vendor of fs.readdirSync(cacheBase)) {
      const vendorDir = path.join(cacheBase, vendor);
      try {
        for (const namespace of fs.readdirSync(vendorDir)) {
          const pluginDir = path.join(vendorDir, namespace);
          try {
            const latest = semverLatest(fs.readdirSync(pluginDir));
            if (!latest) continue;
            const orphanMarker = path.join(pluginDir, latest, ".orphaned_at");
            if (fs.existsSync(orphanMarker)) continue;
            const agentsDir = path.join(pluginDir, latest, "agents");
            try {
              for (const file of fs.readdirSync(agentsDir)) {
                if (file.endsWith(".md")) agents.add(`${namespace}:${file.slice(0, -3)}`);
              }
            } catch (_) {}
          } catch (_) {}
        }
      } catch (_) {}
    }
  } catch (_) {}
  return agents;
}

// ── OpenAI embedding ──────────────────────────────────────────────────────────

function getEmbedding(text, apiKey) {
  return new Promise((resolve, reject) => {
    const body = JSON.stringify({ model: OPENAI_EMBEDDING_MODEL, input: text });
    const req = https.request(
      {
        hostname: "api.openai.com",
        path: "/v1/embeddings",
        method: "POST",
        headers: {
          Authorization: `Bearer ${apiKey}`,
          "Content-Type": "application/json",
          "Content-Length": Buffer.byteLength(body),
        },
      },
      (res) => {
        let raw = "";
        res.on("data", (d) => (raw += d));
        res.on("end", () => {
          try {
            resolve(JSON.parse(raw).data[0].embedding);
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

function cosine(a, b) {
  let dot = 0,
    na = 0,
    nb = 0;
  for (let i = 0; i < a.length; i++) {
    dot += a[i] * b[i];
    na += a[i] * a[i];
    nb += b[i] * b[i];
  }
  const denom = Math.sqrt(na) * Math.sqrt(nb);
  return denom === 0 ? 0 : dot / denom;
}

function findBestCosine(index, queryEmbedding) {
  let best = { name: null, score: 0 };
  for (const a of index.local_agents) {
    if (!a.embedding) continue;
    const score = cosine(queryEmbedding, a.embedding);
    if (score > best.score) best = { name: a.name, score };
  }
  return best;
}

// ── Anthropic LLM pick ────────────────────────────────────────────────────────

function askLlm(agents, query, apiKey) {
  return new Promise((resolve, reject) => {
    const list = agents.map((a) => `- ${a.name}: ${a.description.slice(0, 120)}`).join("\n");
    const prompt =
      `Pick the best agent for this request. Reply with ONLY the agent name, or "none" if no agent fits.\n\n` +
      `Request: ${query}\n\nAgents:\n${list}`;
    const body = JSON.stringify({
      model: ANTHROPIC_LLM_MODEL,
      max_tokens: 50,
      messages: [{ role: "user", content: prompt }],
    });
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
            const r = JSON.parse(raw);
            resolve((r.content?.[0]?.text || "none").trim().toLowerCase());
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

// ── Index build ───────────────────────────────────────────────────────────────

async function buildIndex(cwd, openaiKey) {
  const globalDir = path.join(os.homedir(), ".claude", "agents");
  const projectDir = path.join(cwd, ".claude", "agents");

  const raw = [...collectLocalAgents(globalDir), ...collectLocalAgents(projectDir)];
  // Deduplicate by name; project takes precedence
  const seen = new Set();
  const localAgents = [];
  for (const a of raw.reverse()) {
    if (!seen.has(a.name)) {
      seen.add(a.name);
      localAgents.push(a);
    }
  }
  localAgents.reverse();

  if (openaiKey) {
    await Promise.all(
      localAgents.map(async (a) => {
        try {
          a.embedding = await getEmbedding(a.description, openaiKey);
        } catch (_) {
          a.embedding = null;
        }
      }),
    );
  }

  return {
    plugin_agents: [...collectPluginAgents()],
    local_agents: localAgents,
  };
}

// ── Main ──────────────────────────────────────────────────────────────────────

let raw = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (d) => (raw += d));
process.stdin.on("end", async () => {
  try {
    const data = JSON.parse(raw);
    const event = data.hook_event_name;
    const sessionId = data.session_id || "unknown";
    if (!/^[a-zA-Z0-9_-]+$/.test(sessionId)) process.exit(0);
    const stateDir = `/tmp/claude-state-${sessionId}`;
    const indexPath = path.join(stateDir, "agent-router-index.json");
    const cwd = process.cwd();
    const openaiKey = process.env.OPENAI_API_KEY || null;
    const anthropicKey = process.env.ANTHROPIC_API_KEY || null;

    // ── SessionStart: build or merge into shared routing index ───────────────
    if (event === "SessionStart") {
      try {
        fs.mkdirSync(stateDir, { recursive: true });
        const fresh = await buildIndex(cwd, openaiKey);
        let index;
        if (fs.existsSync(indexPath)) {
          index = JSON.parse(fs.readFileSync(indexPath, "utf8"));
          const knownPlugins = new Set(index.plugin_agents);
          for (const a of fresh.plugin_agents) {
            if (!knownPlugins.has(a)) index.plugin_agents.push(a);
          }
          const knownLocals = new Set(index.local_agents.map((a) => a.name));
          for (const a of fresh.local_agents) {
            if (!knownLocals.has(a.name)) index.local_agents.push(a);
          }
        } else {
          index = fresh;
        }
        atomicWrite(indexPath, JSON.stringify(index));
      } catch (_) {}
      process.exit(0);
    }

    // ── PreToolUse: route Agent() calls ──────────────────────────────────────
    if (event !== "PreToolUse" || data.tool_name !== "Agent") process.exit(0);

    const subagentType = (data.tool_input && data.tool_input.subagent_type) || "";
    const prompt = (data.tool_input && data.tool_input.prompt) || "";
    if (!subagentType) process.exit(0);

    if (BUILT_INS.has(subagentType)) process.exit(0);

    // Load or build index on-demand
    let index;
    try {
      index = JSON.parse(fs.readFileSync(indexPath, "utf8"));
    } catch (_) {
      index = await buildIndex(cwd, openaiKey);
    }

    // Tier 1: exact match
    if (new Set(index.plugin_agents).has(subagentType)) process.exit(0);
    if (!subagentType.includes(":") && new Set(index.local_agents.map((a) => a.name)).has(subagentType))
      process.exit(0);

    // Tier 2: semantic match
    const queryText = `${subagentType.replace(/[:\-_]/g, " ")} ${prompt.slice(0, 300)}`;
    let target = "general-purpose";
    let routingNote = `[Router: '${subagentType}' no fit → general-purpose]`;

    const hasEmbeddings = index.local_agents.some((a) => a.embedding);
    if (openaiKey && hasEmbeddings) {
      try {
        const qEmb = await getEmbedding(queryText, openaiKey);
        const best = findBestCosine(index, qEmb);
        if (best.name && best.score >= COSINE_THRESHOLD) {
          target = best.name;
          routingNote = `[Router: '${subagentType}' → '${target}' (cosine: ${best.score.toFixed(3)})]`;
        }
      } catch (_) {
        // fall through to LLM
      }
    }

    if (target === "general-purpose" && anthropicKey && index.local_agents.length > 0) {
      try {
        const picked = await askLlm(index.local_agents, queryText, anthropicKey);
        if (picked && picked !== "none" && index.local_agents.some((a) => a.name === picked)) {
          target = picked;
          routingNote = `[Router: '${subagentType}' → '${target}' (llm)]`;
        }
      } catch (_) {}
    }

    process.stdout.write(
      JSON.stringify({
        hookSpecificOutput: {
          hookEventName: "PreToolUse",
          permissionDecision: "allow",
          updatedInput: {
            subagent_type: target,
            prompt: prompt + " " + routingNote,
          },
        },
      }),
    );
    process.exit(0);
  } catch (_) {
    process.exit(0);
  }
});
