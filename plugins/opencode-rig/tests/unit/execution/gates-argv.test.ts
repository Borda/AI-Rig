import { spawn } from "node:child_process"
import { EventEmitter } from "node:events"
import { mkdirSync, symlinkSync } from "node:fs"
import { sep } from "node:path"
import { PassThrough } from "node:stream"
import { afterEach, describe, expect, it, vi, beforeEach } from "vitest"
import { GateError, runGate, type GateSpec } from "../../../src/gates.ts"
import { Project } from "../../helpers/harness.ts"

vi.mock("node:child_process", () => ({ spawn: vi.fn() }))

function completedGate(signal: string | null = null, error?: Error): ReturnType<typeof spawn> {
  const child = Object.assign(new EventEmitter(), {
    stdout: new PassThrough(),
    stderr: new PassThrough(),
    kill: vi.fn(),
  })
  queueMicrotask(() => {
    child.stdout.emit("data", Buffer.from("ok\n"))
    if (error) child.emit("error", error)
    child.emit("close", signal || error ? null : 0, signal)
  })
  return child as unknown as ReturnType<typeof spawn>
}

const PRE_COMMIT: GateSpec = {
  id: "pre-commit",
  command: "pre-commit run --files",
  source: ".pre-commit-config.yaml",
  needsFiles: true,
}

beforeEach(() => {
  vi.mocked(spawn).mockReset()
  vi.mocked(spawn).mockImplementation(() => completedGate())
})

afterEach(() => vi.restoreAllMocks())

describe("pre-commit argv", () => {
  it("passes shell metacharacters and quotes as literal file arguments", async () => {
    const project = new Project()
    const files = ["a & calc", "quote's.ts", 'double"quote.ts', "$(touch injected).ts"]
    const result = await runGate(PRE_COMMIT, project.root, files, 1_000)

    expect(spawn).toHaveBeenCalledWith(
      "pre-commit",
      ["run", "--files", ...files],
      expect.objectContaining({ cwd: project.root, shell: false }),
    )
    expect(result.receipt.status).toBe("passed")
  })

  it.each(["", "../outside.ts"])("rejects an unsafe file %s before spawning", (file) => {
    const project = new Project()
    expect(() => runGate(PRE_COMMIT, project.root, [file], 1_000)).toThrow(/file|project/)
    expect(spawn).not.toHaveBeenCalled()
  })

  it.each(["--all-files", "-x"])("passes a leading-dash filename %s with a relative prefix", async (file) => {
    const project = new Project()
    const result = await runGate(PRE_COMMIT, project.root, [file], 1_000)
    expect(spawn).toHaveBeenCalledWith(
      "pre-commit",
      ["run", "--files", `./${file}`],
      expect.objectContaining({ shell: false }),
    )
    expect(result.receipt.status).toBe("passed")
    expect(result.receipt.command).toContain(`./${file}`)
  })

  it("reports a dangling symlink as a gate error before spawning", () => {
    const project = new Project()
    symlinkSync(project.path("missing"), project.path("dangling"), "junction")
    expect(() => runGate(PRE_COMMIT, project.root, ["dangling"], 1_000)).toThrow(GateError)
    expect(spawn).not.toHaveBeenCalled()
  })

  it("renders receipt file arguments with literal POSIX shell quoting", async () => {
    const project = new Project()
    const files = ["plain.ts", "a b.ts", "$(touch injected).ts", "quote's.ts", 'double"quote.ts']
    const result = await runGate(PRE_COMMIT, project.root, files, 1_000)
    expect(result.receipt.command).toBe(
      `pre-commit run --files plain.ts 'a b.ts' '$(touch injected).ts' 'quote'"'"'s.ts' 'double"quote.ts'`,
    )
  })

  it("rejects an absolute path outside the project", () => {
    const project = new Project()
    const outside = new Project()
    expect(() => runGate(PRE_COMMIT, project.root, [outside.path("outside.ts")], 1_000)).toThrow(/project/)
    expect(spawn).not.toHaveBeenCalled()
  })

  it("rejects parent traversal whose actual path follows a symlink outside the project", () => {
    const project = new Project()
    const outside = new Project()
    mkdirSync(outside.path("subdir"))
    outside.write("victim.ts", "outside bytes")
    project.write("victim.ts", "inside bytes")
    symlinkSync(outside.path("subdir"), project.path("link"), "junction")
    expect(() => runGate(PRE_COMMIT, project.root, ["link/../victim.ts"], 1_000)).toThrow(/project/)
    expect(spawn).not.toHaveBeenCalled()
  })

  it("rejects a forward-slash parent segment on every platform", () => {
    const project = new Project()
    expect(() => runGate(PRE_COMMIT, project.root, ["sub/../a.ts"], 1_000)).toThrow(/project/)
    expect(spawn).not.toHaveBeenCalled()
  })

  // Windows accepts both separators, so a backslash parent segment must be refused there.
  it.skipIf(sep !== "\\")("rejects a backslash parent segment where it is a separator", () => {
    const project = new Project()
    expect(() => runGate(PRE_COMMIT, project.root, ["sub\\..\\a.ts"], 1_000)).toThrow(/project/)
    expect(spawn).not.toHaveBeenCalled()
  })

  // Where a backslash is an ordinary filename character, it must not be read as a parent segment.
  it.skipIf(sep === "\\")("does not read a POSIX filename containing backslashes as a parent segment", () => {
    const project = new Project()
    expect(() => runGate(PRE_COMMIT, project.root, ["sub\\..\\a.ts"], 1_000)).not.toThrow(/project/)
  })

  it("rejects an empty pre-commit file list", () => {
    expect(() => runGate(PRE_COMMIT, new Project().root, [], 1_000)).toThrow(/files/)
    expect(spawn).not.toHaveBeenCalled()
  })
})

describe("gate runtime limits", () => {
  it.each([0, -1, 999, 3_600_001, Infinity, NaN])("rejects invalid timeout %s", (timeout) => {
    expect(() => runGate(PRE_COMMIT, new Project().root, ["a.ts"], timeout)).toThrow(/timeout/)
    expect(spawn).not.toHaveBeenCalled()
  })

  it("truncates a fractional timeout before launching the gate", async () => {
    const timer = vi.spyOn(globalThis, "setTimeout")
    await runGate(PRE_COMMIT, new Project().root, ["a.ts"], 1_000.5)
    expect(timer).toHaveBeenCalledWith(expect.any(Function), 1_000)
  })

  it("does not label a process terminated by SIGTERM as a timeout", async () => {
    vi.mocked(spawn).mockImplementation(() => completedGate("SIGTERM"))
    const result = await runGate(PRE_COMMIT, new Project().root, ["a.ts"], 1_000)
    expect(result.receipt.status).toBe("failed")
    expect(result.receipt.reason).not.toBe("timeout")
  })

  it("labels an ETIMEDOUT error as a timeout", async () => {
    const error = Object.assign(new Error("spawn timed out"), { code: "ETIMEDOUT" })
    vi.mocked(spawn).mockImplementation(() => completedGate(null, error))
    const result = await runGate(PRE_COMMIT, new Project().root, ["a.ts"], 1_000)
    expect(result.receipt.status).toBe("failed")
    expect(result.receipt.reason).toBe("timeout")
  })
})
