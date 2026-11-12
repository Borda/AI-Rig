/** Shared Git safety and lookup bounds. */
import { spawnSync } from "node:child_process"
import { afterEach, describe, expect, it, vi } from "vitest"
import { dirtyPaths } from "../../../src/guardrails.ts"
import { resolveProjectRoot } from "../../../src/paths.ts"
import { Project } from "../../helpers/harness.ts"

vi.mock("node:child_process", () => ({ spawnSync: vi.fn() }))

afterEach(() => {
  vi.unstubAllEnvs()
  vi.mocked(spawnSync).mockReset()
})

describe("bounded hardened Git lookups", () => {
  it("scrubs every Git override and disables executable filesystem monitors", () => {
    const project = new Project()
    vi.stubEnv("GIT_CEILING_DIRECTORIES", "planted")
    vi.stubEnv("git_config_count", "planted")
    vi.mocked(spawnSync).mockReturnValue({
      status: 0,
      stdout: project.root,
      stderr: "",
      signal: null,
      pid: 1,
      output: [],
    })
    expect(resolveProjectRoot(project.root)).toBe(project.root)
    expect(spawnSync).toHaveBeenCalledWith(
      "git",
      ["-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false", "rev-parse", "--show-toplevel"],
      expect.objectContaining({ timeout: 1_000 }),
    )
    const environment = vi.mocked(spawnSync).mock.calls[0]![2]!.env!
    expect(environment).not.toHaveProperty("GIT_CEILING_DIRECTORIES")
    expect(environment).not.toHaveProperty("git_config_count")
  })

  it("keeps dirty-path lookup bounded and ignores redirected Git environment", () => {
    vi.stubEnv("GIT_CEILING_DIRECTORIES", "planted")
    vi.mocked(spawnSync).mockReturnValue({
      status: 0,
      stdout: " M source.ts\0",
      stderr: "",
      signal: null,
      pid: 1,
      output: [],
    })
    expect(dirtyPaths(new Project().root)).toEqual(["source.ts"])
    expect(vi.mocked(spawnSync).mock.calls[0]![2]!.env).not.toHaveProperty("GIT_CEILING_DIRECTORIES")
    expect(vi.mocked(spawnSync).mock.calls[0]![2]!.timeout).toBe(1_000)
  })
})
