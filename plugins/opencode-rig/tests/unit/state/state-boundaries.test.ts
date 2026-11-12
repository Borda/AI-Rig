/** State recovery preserves authoritative run records and rejects unsafe indexes. */
import { expect, it, vi } from "vitest"
import { Project, RESOURCES_DIR } from "../../helpers/harness.ts"
import { resolvePaths } from "../../../src/paths.ts"
import { createRun, saveRun } from "../../../src/run.ts"
import { activeRun, loadState, rememberRun, saveState, skillForRun } from "../../../src/state.ts"
import { dispatchRole } from "../../../src/dispatch.ts"

function allocated(project: Project) {
  const paths = resolvePaths(project.root, ".opencode-rig")
  const run = createRun(paths, {
    skill: "rig-investigate",
    scope: { base: null, paths: [], dirty: false, entries: [], digest: "scope-digest" },
  })
  return { paths, run }
}

it.each(["completed", "blocked", "failed", "stale"] as const)("forgets %s session pointers", (status) => {
  const project = new Project()
  const { paths, run } = allocated(project)
  rememberRun(paths.artifactRoot, "session", { runId: run.run_id, skill: run.skill })
  saveRun(paths, { ...run, status })
  expect(activeRun(paths.artifactRoot, "session")).toBeUndefined()
  expect(loadState(paths.artifactRoot).sessions).toEqual({})
  expect(skillForRun(paths.artifactRoot, run.run_id)).toBe(run.skill)
})

it("keeps a live session tied to the actual createRun record", () => {
  const project = new Project()
  const { paths, run } = allocated(project)
  const entry = { runId: run.run_id, skill: run.skill }
  rememberRun(paths.artifactRoot, "session", entry)
  expect(activeRun(paths.artifactRoot, "session")).toEqual(entry)
})

it.each([
  "{bad json",
  '{"schema":99,"runs":{},"sessions":{}}',
  '{"schema":1,"runs":[],"sessions":{}}',
  '{"schema":1,"runs":{"../outside":"rig-review"},"sessions":{}}',
  '{"schema":1,"runs":{"safe":"..\\\\outside"},"sessions":{}}',
  '{"schema":1,"runs":{},"sessions":{"session":{"runId":"../outside","skill":"rig-review"}}}',
])("recovers real run IDs without overwriting invalid state %s", (corrupt) => {
  const project = new Project()
  const { paths, run } = allocated(project)
  project.write(".opencode-rig/state.json", corrupt)
  expect(skillForRun(paths.artifactRoot, run.run_id)).toBe(run.skill)
  expect(loadState(paths.artifactRoot).sessions).toEqual({})
  expect(() => rememberRun(paths.artifactRoot, "session", { runId: run.run_id, skill: run.skill })).toThrow(/state/)
  expect(project.read(".opencode-rig/state.json")).toBe(corrupt)
})

it("recovers a missing index but rejects incomplete and mismatched run records", () => {
  const project = new Project()
  const { paths, run } = allocated(project)
  project.write(".opencode-rig/runs/rig-investigate/forged/run.json", JSON.stringify({ status: "completed" }))
  project.write(".opencode-rig/runs/rig-review/mismatch/run.json", JSON.stringify(run))
  expect(loadState(paths.artifactRoot).runs).toEqual({ [run.run_id]: run.skill })
})

it.each(["../outside", "..\\outside", "/outside", "__proto__", "constructor"])(
  "rejects unsafe run identifiers %s at lookups and writes",
  (runId) => {
    const project = new Project()
    const { paths } = allocated(project)
    expect(() => skillForRun(paths.artifactRoot, runId)).toThrow(/run/)
    expect(() => rememberRun(paths.artifactRoot, "session", { runId, skill: "rig-review" })).toThrow(/run/)
  },
)

it("does not trust inherited session keys", () => {
  const project = new Project()
  expect(activeRun(project.root, "toString")).toBeUndefined()
})

it("rejects unsafe state entries before writing", () => {
  const project = new Project()
  expect(() => saveState(project.root, { schema: 1, runs: { safe: "../outside" }, sessions: {} })).toThrow(/state/)
  expect(project.exists("state.json")).toBe(false)
})

it("rejects missing active evidence without discarding the session pointer", () => {
  const project = new Project()
  const entry = { runId: "missing", skill: "rig-review" }
  rememberRun(project.root, "session", entry)
  expect(() => activeRun(project.root, "session")).toThrow(/scope/)
  expect(loadState(project.root).sessions.session).toEqual(entry)
})

it("does not clear a session from an incomplete terminal-status artifact", () => {
  const project = new Project()
  const { paths, run } = allocated(project)
  const entry = { runId: run.run_id, skill: run.skill }
  rememberRun(paths.artifactRoot, "session", entry)
  project.write(`.opencode-rig/runs/${run.skill}/${run.run_id}/run.json`, JSON.stringify({ status: "completed" }))
  expect(() => activeRun(paths.artifactRoot, "session")).toThrow(/scope/)
  expect(loadState(paths.artifactRoot).sessions.session).toEqual(entry)
})

it("does not recover ambiguous run IDs shared by two valid skills", () => {
  const project = new Project()
  const { paths, run } = allocated(project)
  project.write(`.opencode-rig/runs/rig-review/${run.run_id}/run.json`, JSON.stringify({ ...run, skill: "rig-review" }))
  expect(loadState(paths.artifactRoot).runs).toEqual({})
})

it.each(['{"schema":99}', '{"runs":[]}'])(
  "refuses dispatch before child creation for invalid scope state %s",
  async (invalid) => {
    const project = Project.withCommit()
    const { paths, run } = allocated(project)
    saveRun(paths, { ...run, scope: { ...run.scope, paths: ["src"] } })
    rememberRun(paths.artifactRoot, "parent", { runId: run.run_id, skill: run.skill })
    const state = JSON.stringify({ ...loadState(paths.artifactRoot), ...JSON.parse(invalid) })
    project.write(".opencode-rig/state.json", state)
    const create = vi.fn()
    const ctx = { location: { directory: project.root }, session: { create } }
    await expect(
      dispatchRole(
        ctx,
        { role: "rig-implementer", task: "Edit", sessionID: "parent", paths: ["."], read_only: false },
        RESOURCES_DIR,
      ),
    ).rejects.toThrow(/scope state/)
    expect(create).not.toHaveBeenCalled()
    expect(project.read(".opencode-rig/state.json")).toBe(state)
  },
)

it("refuses array-valued terminal status before dispatch can bypass recorded scope", async () => {
  const project = Project.withCommit()
  const { paths, run } = allocated(project)
  rememberRun(paths.artifactRoot, "parent", { runId: run.run_id, skill: run.skill })
  project.write(
    `.opencode-rig/runs/${run.skill}/${run.run_id}/run.json`,
    JSON.stringify({ ...run, status: ["completed"] }),
  )
  const create = vi.fn()
  const ctx = { location: { directory: project.root }, session: { create } }
  await expect(
    dispatchRole(
      ctx,
      { role: "rig-implementer", task: "Edit", sessionID: "parent", paths: ["."], read_only: false },
      RESOURCES_DIR,
    ),
  ).rejects.toThrow(/scope/)
  expect(create).not.toHaveBeenCalled()
  expect(loadState(paths.artifactRoot).sessions.parent).toEqual({ runId: run.run_id, skill: run.skill })
})
