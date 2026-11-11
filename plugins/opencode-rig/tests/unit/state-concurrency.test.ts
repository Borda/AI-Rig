/** Session cleanup preserves updates made after the initial state observation. */
import { readFileSync, writeFileSync } from "node:fs"
import { beforeEach, expect, it, vi } from "vitest"
import { activeRun, forgetRun, loadState, rememberRun } from "../../src/state.ts"
import { createRun, saveRun } from "../../src/run.ts"
import { resolvePaths } from "../../src/paths.ts"
import { Project } from "../helpers/harness.ts"

vi.mock("node:fs", async (original) => {
  const fs = await original<typeof import("node:fs")>()
  return { ...fs, readFileSync: vi.fn(fs.readFileSync) }
})

beforeEach(async () => {
  const fs = await vi.importActual<typeof import("node:fs")>("node:fs")
  vi.mocked(readFileSync).mockReset().mockImplementation(fs.readFileSync)
})

it.each(["another", "same"])("preserves a concurrent update to the %s session", (target) => {
  const project = new Project()
  const old = { runId: "old", skill: "rig-review" }
  const replacement = { runId: "new", skill: "rig-review" }
  rememberRun(project.root, "session", old)
  const previous = loadState(project.root)
  const next = {
    ...previous,
    runs: { ...previous.runs, new: replacement.skill },
    sessions: { ...previous.sessions, [target === "same" ? "session" : "other"]: replacement },
  }
  const read = vi.mocked(readFileSync).getMockImplementation()!
  vi.mocked(readFileSync).mockImplementationOnce((...args: Parameters<typeof readFileSync>) => {
    const snapshot = read(...args)
    writeFileSync(project.path("state.json"), JSON.stringify(next))
    return snapshot
  })
  forgetRun(project.root, "session")
  expect(loadState(project.root).sessions).toEqual(
    target === "same" ? { session: replacement } : { other: replacement },
  )
})

it("preserves a replacement pointer published during terminal run validation", () => {
  const project = new Project()
  const paths = resolvePaths(project.root, ".opencode-rig")
  const run = createRun(paths, {
    skill: "rig-review",
    scope: { base: null, paths: [], dirty: false, entries: [], digest: "digest" },
  })
  rememberRun(paths.artifactRoot, "session", { runId: run.run_id, skill: run.skill })
  saveRun(paths, { ...run, status: "completed" })
  const previous = loadState(paths.artifactRoot)
  const replacement = { runId: "new", skill: run.skill }
  const next = { ...previous, runs: { ...previous.runs, new: run.skill }, sessions: { session: replacement } }
  const read = vi.mocked(readFileSync).getMockImplementation()!
  vi.mocked(readFileSync).mockImplementationOnce((...args: Parameters<typeof readFileSync>) => {
    const snapshot = read(...args)
    writeFileSync(project.path(".opencode-rig/state.json"), JSON.stringify(next))
    return snapshot
  })
  expect(activeRun(paths.artifactRoot, "session")).toBeUndefined()
  expect(loadState(paths.artifactRoot).sessions.session).toEqual(replacement)
})

it("explains how to preserve corrupt state while recovering", () => {
  const project = new Project()
  project.write("state.json", "{broken")
  expect(() => activeRun(project.root, "session")).toThrow(/move state\.json aside/i)
  expect(project.read("state.json")).toBe("{broken")
})

it("explains how to recover a missing active run without deleting its pointer", () => {
  const project = new Project()
  const entry = { runId: "missing", skill: "rig-review" }
  rememberRun(project.root, "session", entry)
  expect(() => activeRun(project.root, "session")).toThrow(/move state\.json aside to recover/i)
  expect(loadState(project.root).sessions.session).toEqual(entry)
})
