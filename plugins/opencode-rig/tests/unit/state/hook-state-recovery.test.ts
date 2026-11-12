/** Hook observations tolerate damaged state without weakening permission escalation. */
import { expect, it } from "vitest"
import { createHarness, Project } from "../../helpers/harness.ts"
import { rememberRun } from "../../../src/state.ts"

it.each(["corrupt", "invalid", "missing-run"])("keeps hooks usable with %s active state", async (damage) => {
  const project = Project.withCommit()
  project.write("outside-scope.txt", "uncommitted work")
  const harness = await createHarness(project)
  const artifactRoot = project.path(".opencode-rig")
  if (damage === "missing-run") rememberRun(artifactRoot, "session-1", { runId: "missing", skill: "rig-review" })
  else project.write(".opencode-rig/state.json", damage === "corrupt" ? "{broken" : '{"schema":99}')
  const state = project.read(".opencode-rig/state.json")
  const before = harness.toolHooks.get("execute.before")![0]!
  const after = harness.toolHooks.get("execute.after")![0]!
  const context = harness.sessionHooks.get("context")![0]!
  const permission = harness.permissionHooks.get("evaluate")!

  await before({ sessionID: "session-1", tool: "rig_doctor", id: "call", input: {} })
  await after({ sessionID: "session-1", tool: "rig_doctor", id: "call", status: "success" })
  expect(
    project
      .read(".opencode-rig/journal.jsonl")
      .trim()
      .split("\n")
      .map((line) => JSON.parse(line)),
  ).toMatchObject([
    { event: "tool.before", tool: "rig_doctor" },
    { event: "tool.after", tool: "rig_doctor" },
  ])
  const event = { sessionID: "session-1", system: [] }
  await context(event)
  expect(event.system).toEqual([])

  const destructive: any = { action: "shell", sessionID: "session-1", resources: ["git reset --hard"], effect: "allow" }
  await permission(destructive)
  expect(destructive.effect).toBe("ask")
  expect(destructive.message).toContain("destructive local operation requires confirmation")
  expect(destructive.message).not.toContain("outside-scope.txt")
  const history: any = { action: "shell", sessionID: "session-1", resources: ["git commit --amend"], effect: "allow" }
  await permission(history)
  expect(history.effect).toBe("ask")
  expect(history.message).toContain("history rewrite requires confirmation")
  expect(history.message).not.toContain("outside-scope.txt")
  expect(project.read(".opencode-rig/state.json")).toBe(state)
})
