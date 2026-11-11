/** Doctor verifies installed bytes and preserves active evidence dependencies. */
import { utimesSync } from "node:fs"
import { join } from "node:path"
import { expect, it } from "vitest"
import { AGENT_NAMES, doctorReport, installedAgentStatus, staleRuns } from "../../src/doctor.ts"
import { sha256 } from "../../src/digest.ts"
import { DEFAULT_OPTIONS } from "../../src/options.ts"
import { PACKAGE_ROOT, Project } from "../helpers/harness.ts"
function oldRun(project: Project, path: string, record: Record<string, unknown>) {
  project.write(`${path}/run.json`, JSON.stringify(record))
  utimesSync(project.path(path), new Date("2000-01-01"), new Date("2000-01-01"))
  return project.path(path)
}
it("reports content edited after installation", () => {
  const project = new Project()
  const body = "---\nmode: subagent\n---\n\nRole\n"
  for (const name of AGENT_NAMES)
    project.write(
      `agents/${name}.md`,
      `---\nmode: subagent\n---\n<!-- opencode-rig: v1 sha256:${sha256(body)} -->\n\nRole\n`,
    )
  expect(installedAgentStatus(project.path("agents"), "1")).toEqual([])
  project.write("agents/rig-reviewer.md", project.read("agents/rig-reviewer.md") + "Modified\n")
  expect(installedAgentStatus(project.path("agents"), "1")).toContainEqual(
    expect.stringMatching(/rig-reviewer.*modified/),
  )
})
it("preserves in-progress runs and their referenced plan container", () => {
  const project = new Project()
  const plan = oldRun(project, "runs/rig-plan/old-plan", { status: "completed" })
  project.write("runs/rig-plan/old-plan/plan.md", "Frozen plan")
  utimesSync(plan, new Date("2000-01-01"), new Date("2000-01-01"))
  oldRun(project, "runs/rig-remediate/live", { status: "in_progress", plan: { path: join(plan, "plan.md") } })
  const expired = oldRun(project, "runs/rig-review/expired", { status: "completed" })
  expect(staleRuns(project.path("runs"), 30)).toEqual([expired])
})
it("honors Git ignore wildcards and negations", () => {
  const project = Project.withCommit()
  project.write(".gitignore", "cache*/\n!cache-keep/\n")
  const base = { packageRoot: PACKAGE_ROOT, projectRoot: project.root, pluginListed: true }
  const ignored = doctorReport({
    ...base,
    artifactRoot: project.path("cache-rig"),
    options: { ...DEFAULT_OPTIONS, artifacts: "cache-rig" },
  })
  expect(ignored.lines).toContain("PASS  artifact root is gitignored")
  const visible = doctorReport({
    ...base,
    artifactRoot: project.path("cache-keep"),
    options: { ...DEFAULT_OPTIONS, artifacts: "cache-keep" },
  })
  expect(visible.lines).toContain("WARN  add 'cache-keep/' to .gitignore")
})
it("preserves relative plan references with nested artifact roots", () => {
  const project = new Project()
  const plan = oldRun(project, "nested/cache/runs/rig-plan/old", { status: "completed" })
  project.write("nested/cache/runs/rig-plan/old/plan.md", "Frozen plan")
  utimesSync(plan, new Date("2000-01-01"), new Date("2000-01-01"))
  oldRun(project, "nested/cache/runs/rig-remediate/live", {
    status: "in_progress",
    plan: { path: "nested/cache/runs/rig-plan/old/plan.md" },
  })
  const expired = oldRun(project, "nested/cache/runs/rig-review/expired", { status: "completed" })
  expect(staleRuns(project.path("nested/cache/runs"), 30)).toEqual([expired])
})
