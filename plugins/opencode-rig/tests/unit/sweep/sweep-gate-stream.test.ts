/** Gates stay responsive and preserve verbose output without buffering it all. */
import { readFileSync } from "node:fs"
import { describe, expect, it } from "vitest"
import { sha256 } from "../../../src/digest.ts"
import { runGate, type GateSpec } from "../../../src/gates.ts"
import { Project } from "../../helpers/harness.ts"

function nodeGate(script: string): GateSpec {
  return {
    id: "test",
    command: `node -e "${script.replaceAll('"', '\\"')}"`,
    source: "test project",
    needsFiles: false,
  }
}

describe("asynchronous streamed gates", () => {
  it("allows the host event loop to progress while a gate is running", async () => {
    const project = new Project()
    let hostProgress = false
    const progress = setTimeout(() => {
      hostProgress = true
    }, 30)
    const pending = runGate(nodeGate('setTimeout(() => process.stdout.write("done"), 200)'), project.root, [], 1_000)
    expect(pending).toBeInstanceOf(Promise)
    expect(hostProgress).toBe(false)
    const result = await pending
    clearTimeout(progress)
    expect(hostProgress).toBe(true)
    expect(result.receipt.status).toBe("passed")
  })

  it("preserves output beyond the former buffer limit in a durable log", async () => {
    const project = new Project()
    const size = 17 * 1024 * 1024
    const result = await runGate(nodeGate(`process.stdout.write(\"x\".repeat(${size}))`), project.root, [], 5_000)
    expect(result.receipt.status).toBe("passed")
    expect(result.receipt.stdout_sha256).toBe(sha256("x".repeat(size)))
    expect(result.receipt.tail).toBe("x".repeat(4_000))
    expect(result.log_path).toBeTypeOf("string")
    expect(readFileSync(result.log_path!)).toHaveLength(size)
  })

  it("labels a timer-triggered termination as timeout", async () => {
    const result = await runGate(nodeGate("setInterval(() => {}, 1000)"), new Project().root, [], 1_000)
    expect(result.receipt.status).toBe("failed")
    expect(result.receipt.reason).toBe("timeout")
    expect(result.receipt.duration_ms).toBeLessThan(2_500)
  })

  it("records independent stdout and stderr digests", async () => {
    const result = await runGate(
      nodeGate('process.stdout.write("out"); process.stderr.write("err")'),
      new Project().root,
      [],
      1_000,
    )
    expect(result.receipt.stdout_sha256).toBe(sha256("out"))
    expect(result.receipt.stderr_sha256).toBe(sha256("err"))
    expect(readFileSync(result.log_path!, "utf8")).toContain("out")
    expect(readFileSync(result.log_path!, "utf8")).toContain("err")
  })

  it("bounds a gate that ignores the initial timeout signal", async () => {
    const result = await runGate(
      nodeGate('process.on("SIGTERM", () => {}); setInterval(() => {}, 1000)'),
      new Project().root,
      [],
      1_000,
    )
    expect(result.receipt.status).toBe("failed")
    expect(result.receipt.reason).toBe("timeout")
    expect(result.receipt.duration_ms).toBeLessThan(3_500)
  })
})
