// Unit tests for the pure helpers exported by hooks/agent-router.js.
//
// Run: node --test plugins/cc_develop/tests/test_agent_router.js
//
// Only the network-free, deterministic helpers are covered here: cosine,
// findBestCosine, readDescription, buildLlmRequest, parseLlmResponse and
// pickHaikuFromModels. The
// stdin main path is guarded by require.main === module in the hook, so
// requiring the module is side-effect free (no network, no stdin listener
// registered).

"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const {
  buildLlmRequest,
  cosine,
  findBestCosine,
  parseLlmResponse,
  pickHaikuFromModels,
  readDescription,
} = require("../hooks/agent-router.js");

test("cosine: identical vectors → 1", () => {
  assert.equal(cosine([1, 2, 3], [1, 2, 3]), 1);
});

test("cosine: orthogonal vectors → 0", () => {
  assert.equal(cosine([1, 0], [0, 1]), 0);
});

test("cosine: opposite vectors → -1", () => {
  assert.equal(cosine([1, 0], [-1, 0]), -1);
});

test("cosine: zero-norm vector → 0 (no divide-by-zero)", () => {
  assert.equal(cosine([0, 0], [1, 1]), 0);
  assert.equal(cosine([0, 0], [0, 0]), 0);
});

test("cosine: proportional vectors → 1 (scale invariant)", () => {
  assert.ok(Math.abs(cosine([1, 2, 3], [2, 4, 6]) - 1) < 1e-12);
});

test("findBestCosine: picks highest-scoring embedded agent", () => {
  const index = {
    local_agents: [
      { name: "a", embedding: [1, 0] },
      { name: "b", embedding: [0.9, 0.1] },
      { name: "c", embedding: [0, 1] },
    ],
  };
  const best = findBestCosine(index, [1, 0]);
  assert.equal(best.name, "a");
  assert.equal(best.score, 1);
});

test("findBestCosine: skips agents with no embedding", () => {
  const index = {
    local_agents: [
      { name: "a", embedding: null },
      { name: "b", embedding: [1, 0] },
    ],
  };
  const best = findBestCosine(index, [1, 0]);
  assert.equal(best.name, "b");
});

test("findBestCosine: no embedded agents → name null, score 0", () => {
  const index = { local_agents: [{ name: "a", embedding: null }, { name: "b" }] };
  const best = findBestCosine(index, [1, 0]);
  assert.equal(best.name, null);
  assert.equal(best.score, 0);
});

// ── readDescription ─────────────────────────────────────────────────────────

let tmpDir;

test.before(() => {
  tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "agent-router-test-"));
});

test.after(() => {
  if (tmpDir) fs.rmSync(tmpDir, { recursive: true, force: true });
});

function writeAgent(name, body) {
  const p = path.join(tmpDir, name);
  fs.writeFileSync(p, body);
  return p;
}

test("readDescription: single-line description, lowercased", () => {
  const p = writeAgent("single.md", "---\nname: x\ndescription: Handles Feature Work\n---\nbody\n");
  assert.equal(readDescription(p), "handles feature work");
});

test("readDescription: block scalar '>' form uses first indented line", () => {
  const p = writeAgent("block.md", "---\nname: x\ndescription: >\n  Multi Line Description Here\n  second line\n---\n");
  assert.equal(readDescription(p), "multi line description here");
});

test("readDescription: block scalar '|' form uses first indented line", () => {
  const p = writeAgent("pipe.md", "---\nname: x\ndescription: |\n  Piped Description\n---\n");
  assert.equal(readDescription(p), "piped description");
});

test("readDescription: missing description → empty string", () => {
  const p = writeAgent("none.md", "---\nname: x\n---\nno description field\n");
  assert.equal(readDescription(p), "");
});

test("readDescription: nonexistent file → empty string (no throw)", () => {
  assert.equal(readDescription(path.join(tmpDir, "does-not-exist.md")), "");
});

// ── LLM pick request/response ───────────────────────────────────────────────

test("buildLlmRequest: carries the resolved model id, thinking disabled", () => {
  const body = buildLlmRequest([{ name: "a", description: "does a" }], "q", "claude-haiku-9");
  assert.equal(body.model, "claude-haiku-9");
  assert.deepEqual(body.thinking, { type: "disabled" });
});

test("pickHaikuFromModels: first entry with line 'haiku', never inferred from the id", () => {
  const raw = JSON.stringify({
    data: [
      { id: "claude-haiku-preview", line: null },
      { id: "claude-haiku-6", line: "haiku" },
      { id: "claude-haiku-5-5", line: "haiku" },
    ],
  });
  assert.equal(pickHaikuFromModels(200, raw), "claude-haiku-6");
});

test("parseLlmResponse: first text block wins over a leading thinking block", () => {
  const raw = JSON.stringify({ content: [{ type: "thinking" }, { type: "text", text: " Agent-A \n" }] });
  assert.equal(parseLlmResponse(200, raw), "agent-a");
});

test("parseLlmResponse: non-2xx error body throws, never returns 'none'", () => {
  const raw = JSON.stringify({ type: "error", error: { type: "invalid_request_error", message: "bad model" } });
  assert.throws(() => parseLlmResponse(400, raw), /HTTP 400 invalid_request_error: bad model/);
});

test("parseLlmResponse: no text block throws with stop_reason", () => {
  const raw = JSON.stringify({ content: [{ type: "thinking" }], stop_reason: "max_tokens" });
  assert.throws(() => parseLlmResponse(200, raw), /no text block \(stop_reason: max_tokens\)/);
});
