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
//   B. Anthropic LLM pick (ANTHROPIC_API_KEY set — absent under subscription/OAuth login):
//      PreToolUse:   pass agent list + query to the newest Haiku → pick best name or "none"
//      Model id resolved at runtime, never frozen: ANTHROPIC_DEFAULT_HAIKU_MODEL → 24h cache
//      file → GET /v1/models (first entry with line "haiku") → logged constant fallback
//      5-second timeout via Promise.race; non-2xx, error body, missing text block or
//      timeout → logged to stderr, falls through to tier 3 (fail open, never blocks)
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

function getSentinelDir() {
  return process.platform === "win32" ? os.tmpdir() : "/tmp";
}

// ── Constants ─────────────────────────────────────────────────────────────────

const BUILT_INS = new Set(["claude", "general-purpose", "claude-code-guide", "Explore", "Plan", "statusline-setup"]);

const OPENAI_EMBEDDING_MODEL = "text-embedding-3-small";
const COSINE_THRESHOLD = 0.65;

// The raw Messages API needs an API model id — the Claude Code alias "haiku" is rejected — so the
// id is resolved at runtime (resolveHaikuModel). This constant is only the last resort when the
// env override, the cache and the Models API all fail: the latest Haiku known when written.
const FALLBACK_HAIKU_MODEL = "claude-haiku-5-5";
const HAIKU_MODEL_CACHE_TTL_MS = 24 * 60 * 60 * 1000;
const MODELS_TIMEOUT_MS = 2000;
// Guards ids read back from the cache or the Models API before they reach a request body.
const MODEL_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:@/[\]-]{0,199}$/;
// Haiku 5.5 thinks adaptively by default and thinking tokens count toward max_tokens, so a
// small budget can end after the thinking block with no text. Thinking is disabled (allowed at
// effort high or below); the budget still leaves room for a one-name reply.
const ANTHROPIC_LLM_MAX_TOKENS = 256;
const LLM_TIMEOUT_MS = 5000;

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
            // Sort versions descending; skip orphaned; use first non-orphaned
            const versions = fs
              .readdirSync(pluginDir)
              .filter((v) => /^\d+\.\d+\.\d+/.test(v))
              .sort((a, b) => {
                const pa = a.split(".").map(Number);
                const pb = b.split(".").map(Number);
                for (let i = 0; i < 3; i++) {
                  if (pa[i] !== pb[i]) return pb[i] - pa[i];
                }
                return 0;
              });
            let activeVersion = null;
            for (const v of versions) {
              if (!fs.existsSync(path.join(pluginDir, v, ".orphaned_at"))) {
                activeVersion = v;
                break;
              }
            }
            if (!activeVersion) continue;
            const agentsDir = path.join(pluginDir, activeVersion, "agents");
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
    req.setTimeout(5000, () => req.destroy(new Error("embedding request timeout")));
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

// ── Anthropic API helpers ─────────────────────────────────────────────────────

// Parses an API response body, throwing on non-2xx, non-object or error-typed bodies.
function parseApiBody(statusCode, raw) {
  let body;
  try {
    body = JSON.parse(raw);
  } catch (_) {
    throw new Error(`HTTP ${statusCode}: response body is not JSON`);
  }
  const isObject = body !== null && typeof body === "object";
  const failed = statusCode < 200 || statusCode >= 300 || !isObject || body.type === "error" || body.error;
  if (failed) {
    const err = (isObject && body.error) || {};
    const detail = err.message ? `: ${err.message}` : "";
    throw new Error(`HTTP ${statusCode} ${err.type || "error"}${detail}`);
  }
  return body;
}

function anthropicRequest(apiKey, { timeoutMs, ...options }, payload) {
  return new Promise((resolve, reject) => {
    const headers = { "x-api-key": apiKey, "anthropic-version": "2023-06-01" };
    if (payload !== undefined) {
      headers["Content-Type"] = "application/json";
      headers["Content-Length"] = Buffer.byteLength(payload);
    }
    const req = https.request({ hostname: "api.anthropic.com", headers, ...options }, (res) => {
      let raw = "";
      res.setEncoding("utf8");
      res.on("data", (d) => (raw += d));
      res.on("end", () => resolve({ statusCode: res.statusCode, raw }));
    });
    req.on("error", reject);
    if (timeoutMs) req.setTimeout(timeoutMs, () => req.destroy(new Error(`request timeout after ${timeoutMs}ms`)));
    if (payload !== undefined) req.write(payload);
    req.end();
  });
}

// ── Haiku model resolution ────────────────────────────────────────────────────

function haikuModelCachePath(env) {
  const token = env.CSID || env.CLAUDE_CODE_SESSION_ID || "shared";
  const safe = /^[A-Za-z0-9_-]+$/.test(token) ? token : "shared";
  return path.join(env.TMPDIR || os.tmpdir(), `agent-router-haiku-model-${safe}`);
}

function readCachedModel(cachePath, nowMs) {
  try {
    if (nowMs - fs.statSync(cachePath).mtimeMs > HAIKU_MODEL_CACHE_TTL_MS) return null;
    const id = fs.readFileSync(cachePath, "utf8").trim();
    return MODEL_ID_PATTERN.test(id) ? id : null;
  } catch (_) {
    return null;
  }
}

// The Models API lists newest releases first; `line` names the family. The docs say never to
// infer the line from the id, so an id merely containing "haiku" does not qualify.
function pickHaikuFromModels(statusCode, raw) {
  const body = parseApiBody(statusCode, raw);
  const models = Array.isArray(body.data) ? body.data : [];
  const entry = models.find((m) => m && m.line === "haiku" && MODEL_ID_PATTERN.test(String(m.id)));
  if (!entry) throw new Error(`no model with line "haiku" among ${models.length} listed`);
  return entry.id;
}

function listModels(apiKey) {
  return anthropicRequest(apiKey, { path: "/v1/models?limit=1000", method: "GET", timeoutMs: MODELS_TIMEOUT_MS });
}

// Newest Haiku id, never frozen: env override → cache (24h by mtime) → Models API → constant.
// `fetchModels` and `log` are injectable so tests run without network.
async function resolveHaikuModel({ env, apiKey, nowMs = Date.now(), fetchModels = listModels, log }) {
  const override = (env.ANTHROPIC_DEFAULT_HAIKU_MODEL || "").trim();
  if (override) return { model: override, source: "env" };
  const cachePath = haikuModelCachePath(env);
  const cached = readCachedModel(cachePath, nowMs);
  if (cached) return { model: cached, source: "cache" };
  try {
    const { statusCode, raw } = await fetchModels(apiKey);
    const model = pickHaikuFromModels(statusCode, raw);
    try {
      atomicWrite(cachePath, model + "\n");
    } catch (_) {
      // An unwritable cache only costs a repeat lookup next call.
    }
    return { model, source: "models-api" };
  } catch (e) {
    log(`agent-router: Haiku model lookup failed (${e.message}); using fallback ${FALLBACK_HAIKU_MODEL}\n`);
    return { model: FALLBACK_HAIKU_MODEL, source: "fallback" };
  }
}

// ── Anthropic LLM pick ────────────────────────────────────────────────────────

function buildLlmRequest(agents, query, model) {
  const list = agents.map((a) => `- ${a.name}: ${a.description.slice(0, 120)}`).join("\n");
  const prompt =
    `Pick the best agent for this request. Reply with ONLY the agent name, or "none" if no agent fits.\n\n` +
    `Request: ${query}\n\nAgents:\n${list}`;
  return {
    model,
    max_tokens: ANTHROPIC_LLM_MAX_TOKENS,
    thinking: { type: "disabled" },
    messages: [{ role: "user", content: prompt }],
  };
}

// Returns the picked name (trimmed, lowercased) or throws. A failed request must surface as an
// error, never as "none" — "none" is a legitimate model answer, so conflating the two hides a
// broken router behind what looks like an honest no-fit.
function parseLlmResponse(statusCode, raw) {
  const body = parseApiBody(statusCode, raw);
  // The response may open with a thinking block — select by type, never by position.
  const blocks = Array.isArray(body.content) ? body.content : [];
  const textBlock = blocks.find((b) => b && b.type === "text" && typeof b.text === "string");
  if (!textBlock) throw new Error(`HTTP ${statusCode}: no text block (stop_reason: ${body.stop_reason || "unknown"})`);
  return textBlock.text.trim().toLowerCase();
}

async function askLlm(agents, query, apiKey, log) {
  const { model } = await resolveHaikuModel({ env: process.env, apiKey, log });
  const payload = JSON.stringify(buildLlmRequest(agents, query, model));
  const { statusCode, raw } = await anthropicRequest(apiKey, { path: "/v1/messages", method: "POST" }, payload);
  return parseLlmResponse(statusCode, raw);
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

// ── Exports (test-only; no-op when run as a hook) ───────────────────────────────
// Pure helpers are exported for unit testing. require.main guard below ensures the
// stdin main path only runs when executed directly (always true in production).
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    FALLBACK_HAIKU_MODEL,
    buildLlmRequest,
    cosine,
    findBestCosine,
    haikuModelCachePath,
    parseLlmResponse,
    pickHaikuFromModels,
    readDescription,
    resolveHaikuModel,
  };
}

// ── Main ──────────────────────────────────────────────────────────────────────

if (require.main === module) {
  let raw = "";
  process.stdin.setEncoding("utf8");
  process.stdin.on("data", (d) => (raw += d));
  process.stdin.on("end", async () => {
    try {
      const data = JSON.parse(raw);
      const event = data.hook_event_name;
      const sessionId = data.session_id || "unknown";
      if (!/^[a-zA-Z0-9_-]+$/.test(sessionId)) process.exit(0);
      const stateDir = `${getSentinelDir()}/claude-state-${sessionId}`;
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
          const picked = await Promise.race([
            askLlm(index.local_agents, queryText, anthropicKey, (m) => process.stderr.write(m)),
            new Promise((_, reject) =>
              setTimeout(() => reject(new Error(`timeout after ${LLM_TIMEOUT_MS}ms`)), LLM_TIMEOUT_MS),
            ),
          ]);
          if (picked && picked !== "none" && index.local_agents.some((a) => a.name === picked)) {
            target = picked;
            routingNote = `[Router: '${subagentType}' → '${target}' (llm)]`;
          }
        } catch (e) {
          // Fail open: the Agent() call still proceeds via tier 3; stderr keeps the failure visible.
          process.stderr.write(`agent-router: LLM pick failed (${e.message}); falling back to general-purpose\n`);
        }
      }

      process.stdout.write(
        JSON.stringify({
          hookSpecificOutput: {
            hookEventName: "PreToolUse",
            permissionDecision: "allow",
            updatedInput: {
              description: (data.tool_input && data.tool_input.description) || prompt.slice(0, 100),
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
}
