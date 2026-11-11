import { spawnSync } from "node:child_process"
import { mkdirSync, symlinkSync } from "node:fs"
import { describe, expect, it, vi, beforeEach } from "vitest"
import { GateError, runGate, type GateSpec } from "../../src/gates.ts"
import { Project } from "../helpers/harness.ts"

vi.mock("node:child_process", () => ({ spawnSync: vi.fn() }))

const PRE_COMMIT: GateSpec = {
  id: "pre-commit",
  command: "pre-commit run --files",
  source: ".pre-commit-config.yaml",
  needsFiles: true,
}

beforeEach(() => {
  vi.mocked(spawnSync).mockReset()
  vi.mocked(spawnSync).mockReturnValue({ status: 0, stdout: "ok\n", stderr: "", signal: null, pid: 1, output: [] })
})

describe("pre-commit argv", () => {
  it("passes shell metacharacters and quotes as literal file arguments", () => {
    const project = new Project()
    const files = ["a & calc", "quote's.ts", 'double"quote.ts', "$(touch injected).ts"]
    const result = runGate(PRE_COMMIT, project.root, files, 1_000)

    expect(spawnSync).toHaveBeenCalledWith(
      "pre-commit",
      ["run", "--files", ...files],
      expect.objectContaining({ cwd: project.root, shell: false }),
    )
    expect(result.receipt.status).toBe("passed")
  })

  it.each(["", "../outside.ts"])("rejects an unsafe file %s before spawning", (file) => {
    const project = new Project()
    expect(() => runGate(PRE_COMMIT, project.root, [file], 1_000)).toThrow(/file|project/)
    expect(spawnSync).not.toHaveBeenCalled()
  })

  it.each(["--all-files", "-x"])("passes a leading-dash filename %s with a relative prefix", (file) => {
    const project = new Project()
    const result = runGate(PRE_COMMIT, project.root, [file], 1_000)
    expect(spawnSync).toHaveBeenCalledWith(
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
    expect(spawnSync).not.toHaveBeenCalled()
  })

  it("renders receipt file arguments with literal POSIX shell quoting", () => {
    const project = new Project()
    const files = ["plain.ts", "a b.ts", "$(touch injected).ts", "quote's.ts", 'double"quote.ts']
    const result = runGate(PRE_COMMIT, project.root, files, 1_000)
    expect(result.receipt.command).toBe(
      `pre-commit run --files plain.ts 'a b.ts' '$(touch injected).ts' 'quote'"'"'s.ts' 'double"quote.ts'`,
    )
  })

  it("rejects an absolute path outside the project", () => {
    const project = new Project()
    const outside = new Project()
    expect(() => runGate(PRE_COMMIT, project.root, [outside.path("outside.ts")], 1_000)).toThrow(/project/)
    expect(spawnSync).not.toHaveBeenCalled()
  })

  it("rejects parent traversal whose actual path follows a symlink outside the project", () => {
    const project = new Project()
    const outside = new Project()
    mkdirSync(outside.path("subdir"))
    outside.write("victim.ts", "outside bytes")
    project.write("victim.ts", "inside bytes")
    symlinkSync(outside.path("subdir"), project.path("link"), "junction")
    expect(() => runGate(PRE_COMMIT, project.root, ["link/../victim.ts"], 1_000)).toThrow(/project/)
    expect(spawnSync).not.toHaveBeenCalled()
  })

  it("rejects an empty pre-commit file list", () => {
    expect(() => runGate(PRE_COMMIT, new Project().root, [], 1_000)).toThrow(/files/)
    expect(spawnSync).not.toHaveBeenCalled()
  })
})

describe("gate runtime limits", () => {
  it.each([0, -1, 999, 3_600_001, Infinity, NaN])("rejects invalid timeout %s", (timeout) => {
    expect(() => runGate(PRE_COMMIT, new Project().root, ["a.ts"], timeout)).toThrow(/timeout/)
    expect(spawnSync).not.toHaveBeenCalled()
  })

  it("truncates a fractional timeout before launching the gate", () => {
    runGate(PRE_COMMIT, new Project().root, ["a.ts"], 1_000.5)
    expect(spawnSync).toHaveBeenCalledWith(
      "pre-commit",
      ["run", "--files", "a.ts"],
      expect.objectContaining({ timeout: 1_000 }),
    )
  })

  it("does not label a process terminated by SIGTERM as a timeout", () => {
    vi.mocked(spawnSync).mockReturnValue({
      status: null,
      stdout: "",
      stderr: "",
      signal: "SIGTERM",
      pid: 1,
      output: [],
    })
    const result = runGate(PRE_COMMIT, new Project().root, ["a.ts"], 1_000)
    expect(result.receipt.status).toBe("failed")
    expect(result.receipt.reason).not.toBe("timeout")
  })

  it("labels an ETIMEDOUT error as a timeout", () => {
    const error = Object.assign(new Error("spawn timed out"), { code: "ETIMEDOUT" })
    vi.mocked(spawnSync).mockReturnValue({
      status: null,
      stdout: "",
      stderr: "",
      signal: null,
      error,
      pid: 1,
      output: [],
    })
    const result = runGate(PRE_COMMIT, new Project().root, ["a.ts"], 1_000)
    expect(result.receipt.status).toBe("failed")
    expect(result.receipt.reason).toBe("timeout")
  })
})
