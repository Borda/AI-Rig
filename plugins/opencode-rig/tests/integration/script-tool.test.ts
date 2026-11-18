/** rig_script runs declared files and records a receipt. */
import { existsSync, readFileSync } from "node:fs"
import { describe, expect, it } from "vitest"
import { Project, createHarness, trivialPlan } from "../helpers/harness.ts"

async function startedRun(project: Project, harness: Awaited<ReturnType<typeof createHarness>>) {
  const started = await harness.call("rig_evidence", {
    action: "start",
    skill: "rig-implement",
    trivial_plan: trivialPlan(["src/a.ts"]),
  })
  return started.metadata.run_id as string
}

describe("rig_script", () => {
  it("runs a run probe and writes a receipt next to its log", async () => {
    const project = Project.withCommit()
    project.write(".gitignore", ".opencode-rig/\n")
    const harness = await createHarness(project)
    const runId = await startedRun(project, harness)
    const relative = `.opencode-rig/runs/rig-implement/${runId}/scripts/probe.mjs`
    project.write(relative, "console.log('probe', process.argv.slice(2).join(','))\n")

    const result = await harness.call("rig_script", {
      source: "run",
      path: relative,
      interpreter: "node",
      argv: ["x"],
    })
    const receipt = JSON.parse(result.content.split("\n").slice(1).join("\n"))
    expect(receipt.status).toBe("passed")
    expect(receipt.source).toBe("run")
    expect(receipt.tail).toContain("probe x")
    expect(existsSync(receipt.log_path)).toBe(true)
    expect(existsSync(result.metadata.receipt_path as string)).toBe(true)
    expect(JSON.parse(readFileSync(result.metadata.receipt_path as string, "utf8")).digest).toBe(receipt.digest)
    expect(harness.assertions.at(-1)?.action).toBe("rig_script")
    expect(harness.assertions.at(-1)?.resources?.[0]).toBe(receipt.path)
  })

  it("runs a project tool from scripts/ and rejects an unknown interpreter", async () => {
    const project = Project.withCommit({ "scripts/tool.mjs": "console.log('tool')\n" })
    project.write(".gitignore", ".opencode-rig/\n")
    const harness = await createHarness(project)
    const result = await harness.call("rig_script", {
      source: "project",
      path: "scripts/tool.mjs",
      interpreter: "node",
    })
    expect(result.content).toContain('"status": "passed"')
    await expect(
      harness.call("rig_script", { source: "project", path: "scripts/tool.mjs", interpreter: "perl" }),
    ).rejects.toThrow(/unknown interpreter/)
  })

  it("runs a run probe from a session without an active run when the path is under the runs root", async () => {
    const project = Project.withCommit()
    project.write(".gitignore", ".opencode-rig/\n")
    const harness = await createHarness(project)
    project.write(".opencode-rig/runs/rig-implement/other/scripts/probe.mjs", "console.log('child')\n")
    const result = await harness.call("rig_script", {
      source: "run",
      path: ".opencode-rig/runs/rig-implement/other/scripts/probe.mjs",
      interpreter: "node",
    })
    expect(result.content).toContain('"status": "passed"')

    project.write("scripts/probe.mjs", "console.log(1)\n")
    await expect(
      harness.call("rig_script", { source: "run", path: "scripts/probe.mjs", interpreter: "node" }),
    ).rejects.toThrow(/outside its declared root/)
  })

  it("runs a shipped plugin helper only when its digest matches", async () => {
    const project = Project.withCommit()
    project.write(".gitignore", ".opencode-rig/\n")
    const harness = await createHarness(project)
    const result = await harness.call("rig_script", {
      source: "plugin",
      path: "bin/opencode-rig.mjs",
      interpreter: "node",
      argv: ["--version"],
    })
    expect(result.content).toContain('"source": "plugin"')
  })
})
