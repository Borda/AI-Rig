/** Child scope checks use workspace evidence instead of self-reporting. */
import { rmSync, symlinkSync } from "node:fs"
import { expect, it, vi } from "vitest"
import { dispatchRole } from "../../../src/dispatch.ts"
import { resolvePaths } from "../../../src/paths.ts"
import { rememberRun } from "../../../src/state.ts"
import { createRun } from "../../../src/run.ts"
import { collectScope } from "../../../src/evidence.ts"
import { Project, RESOURCES_DIR } from "../../helpers/harness.ts"

function context(project: Project, edit: () => void = () => {}) {
  return {
    location: { directory: project.root },
    session: {
      create: vi.fn(async (_input: any) => ({ id: "child" })),
      prompt: vi.fn(async () => edit()),
      wait: vi.fn(async () => {}),
      context: vi.fn(async () => [{ info: { role: "assistant" }, parts: [{ text: "Done; changed: none" }] }]),
      remove: vi.fn(async () => {}),
    },
  }
}
const input = { role: "rig-implementer", task: "Apply change", paths: ["src"], read_only: false } as const

it("detects tracked ignored edits while excluding ignored untracked additions", async () => {
  const project = Project.withCommit({ ".gitignore": "private/\n", "src/a.ts": "a" })
  project.write("private/existing.txt", "before")
  project.git("add", "-f", "private/existing.txt")
  project.git("commit", "-qm", "track ignored file")
  const ctx = context(project, () => {
    project.write("private/existing.txt", "after")
    project.write("private/new.txt", "new")
  })
  const result = await dispatchRole(ctx, input, RESOURCES_DIR)
  expect(result.scope_violation).toBe(true)
  expect(result.violations).toEqual([expect.stringContaining("outside the input scope: private/existing.txt")])
  expect(ctx.session.remove).not.toHaveBeenCalled()
})

it("exempts generated records only under the configured artifact root", async () => {
  const project = Project.withCommit({ "src/a.ts": "a", ".gitignore": "cache/\n" })
  const ctx = {
    ...context(project, () => project.write("cache/rig/sessions/child/journal.jsonl", "{}\n")),
    options: { artifacts: "cache/rig" },
  }
  expect(await dispatchRole(ctx, input, RESOURCES_DIR)).toMatchObject({ scope_violation: false, violations: [] })
  expect(ctx.session.remove).toHaveBeenCalled()
})

it("detects all foreign edits including already dirty files and retains child evidence", async () => {
  const project = Project.withCommit({ "src/a.ts": "a", "other.ts": "before" })
  project.write("other.ts", "dirty before child")
  const ctx = context(project, () => {
    project.write("other.ts", "after")
    project.write("another.ts", "after")
  })
  const result = await dispatchRole(ctx, input, RESOURCES_DIR)
  expect(result).toMatchObject({ scope_violation: true })
  expect(result.violations).toHaveLength(2)
  expect(result.text).toContain("Done; changed: none")
  expect(result.text).toContain("other.ts")
  expect(result.text).toContain("another.ts")
  expect(result.text).toContain("retained: child")
  expect(ctx.session.remove).not.toHaveBeenCalled()
})
it("checks read-only roles without a calling session", async () => {
  const project = Project.withCommit()
  const ctx = context(project, () => project.write("src/a.ts", "changed"))
  const result = await dispatchRole(ctx, { role: "rig-reviewer", task: "Review" }, RESOURCES_DIR)
  expect(result).toMatchObject({ scope_violation: true })
  expect(result.violations).toContainEqual(expect.stringContaining("read-only child changed path: src/a.ts"))
  expect(ctx.session.remove).not.toHaveBeenCalled()
})
it("checks remediation run scopes for writers", async () => {
  const project = Project.withCommit()
  const paths = resolvePaths(project.root, ".opencode-rig")
  const run = createRun(paths, { skill: "rig-remediate", scope: collectScope(project.root, ["src"]) })
  rememberRun(paths.artifactRoot, "parent", { skill: run.skill, runId: run.run_id })
  const ctx = context(project, () => project.write("other.ts", "changed"))
  const result = await dispatchRole(ctx, { ...input, paths: ["."], sessionID: "parent" }, RESOURCES_DIR)
  expect(result.violations).toContainEqual(expect.stringContaining("recorded run scope: other.ts"))
})
it("fails closed before child creation for unreadable active runs", async () => {
  const project = Project.withCommit()
  const paths = resolvePaths(project.root, ".opencode-rig")
  rememberRun(paths.artifactRoot, "parent", { skill: "rig-remediate", runId: "missing" })
  const ctx = context(project)
  await expect(dispatchRole(ctx, { ...input, sessionID: "parent" }, RESOURCES_DIR)).rejects.toThrow(/scope/)
  expect(ctx.session.create).not.toHaveBeenCalled()
})
it("does not blame a child for unchanged existing dirty files", async () => {
  const project = Project.withCommit({ "src/a.ts": "a", "other.ts": "before" })
  project.write("other.ts", "existing dirty")
  const ctx = context(project, () => project.write("src/a.ts", "allowed"))
  expect(await dispatchRole(ctx, input, RESOURCES_DIR)).toMatchObject({ scope_violation: false, violations: [] })
  expect(ctx.session.remove).toHaveBeenCalledWith({ sessionID: "child" })
})
it("binds shipped role permissions and read-only restrictions to child creation", async () => {
  const project = Project.withCommit()
  const ctx = context(project)
  await dispatchRole(ctx, { role: "rig-scientist", task: "Inspect" }, RESOURCES_DIR)
  expect(ctx.session.create.mock.calls[0]?.[0]).toMatchObject({
    permissions: expect.arrayContaining([
      { action: "edit", resource: "*", effect: "deny" },
      { action: "read", resource: "*credentials*", effect: "deny" },
    ]),
  })
  await dispatchRole(ctx, { ...input, read_only: true }, RESOURCES_DIR)
  expect(ctx.session.create.mock.calls[1]?.[0]).toMatchObject({
    permissions: expect.arrayContaining([{ action: "edit", resource: "*", effect: "deny" }]),
  })
})
it("retains violations and failure details when a child writes then fails", async () => {
  const project = Project.withCommit()
  const ctx = context(project, () => project.write("outside.ts", "changed"))
  ctx.session.wait.mockRejectedValueOnce(new Error("child wait failed"))
  await expect(dispatchRole(ctx, input, RESOURCES_DIR)).rejects.toThrow(/child wait failed.*outside.ts/s)
  expect(ctx.session.remove).not.toHaveBeenCalled()
})

it("records a replaced directory without following the symlink", async () => {
  const project = Project.withCommit()
  const external = new Project()
  external.write("a.ts", "external bytes")
  const ctx = context(project, () => {
    rmSync(project.path("src"), { recursive: true })
    symlinkSync(external.root, project.path("src"), process.platform === "win32" ? "junction" : "dir")
  })
  const result = await dispatchRole(ctx, { ...input, paths: ["other"] }, RESOURCES_DIR)
  expect(result.scope_violation).toBe(true)
  expect(result.text).toContain("outside the input scope: src/a.ts")
  expect(ctx.session.remove).not.toHaveBeenCalled()
})
it("refuses corrupt session scope state before dispatch", async () => {
  const project = Project.withCommit()
  project.write(".opencode-rig/state.json", "{bad json")
  const ctx = context(project)
  await expect(dispatchRole(ctx, { ...input, sessionID: "parent" }, RESOURCES_DIR)).rejects.toThrow(/scope state/)
  expect(ctx.session.create).not.toHaveBeenCalled()
})
