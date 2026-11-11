import { readFileSync, rmSync, utimesSync } from "node:fs"
import { dirname, join } from "node:path"
import { describe, expect, it } from "vitest"
import { createHarness, Project, RESOURCES_DIR } from "../helpers/harness.ts"

/** Read the instruction lines in one named skill section. */
function section(skill: string, heading: string): string[] {
  const lines = readFileSync(join(RESOURCES_DIR, "skills", skill, "SKILL.md"), "utf8").split("\n")
  const start = lines.indexOf(`## ${heading}`)
  expect(start, `${skill}: ${heading}`).toBeGreaterThan(-1)
  const end = lines.findIndex((line, index) => index > start && line.startsWith("## "))
  return lines.slice(start + 1, end < 0 ? undefined : end)
}

describe("challenge workflow", () => {
  it("runs gates before finalizing even when the baseline has no findings", async () => {
    const project = new Project()
    project.write("src/a.ts", "export const a = 1\n")
    const harness = await createHarness(project, { gates: ['node -e "process.exit(0)"'] })
    await harness.call("rig_evidence", { action: "start", skill: "rig-challenge", paths: ["src/a.ts"] })
    await harness.call("rig_loop", { action: "start", reviewer: "independent-challenger", findings: [] })
    await harness.call("rig_loop", {
      action: "finalize",
      reviewer: "independent-challenger",
      stop_reason: "converged",
    })
    const missingGate = await harness.call("rig_evidence", { action: "finalize" })
    expect(missingGate.metadata).toMatchObject({ ok: false, status: "in_progress" })
    expect(missingGate.content).toContain("no passing gate receipt")

    const workflow = section("rig-challenge", "Workflow")
    const gates = workflow.findIndex((line) => /^\d+\. /.test(line) && line.includes("**Run configured gates.**"))
    const finalize = workflow.findIndex((line) => line.includes("**Finalize.**"))
    expect(gates).toBeGreaterThan(-1)
    expect(gates).toBeLessThan(finalize)
    expect(workflow[gates]).toContain("even when the baseline has no findings")
    expect(workflow[gates]).toContain("rig_gate")
    expect(workflow[gates]).toContain('action: "run"')

    await harness.call("rig_gate", { action: "run" })
    const finalized = await harness.call("rig_evidence", { action: "finalize" })
    expect(finalized.metadata.ok).toBe(true)
    const completed = JSON.parse(
      (await harness.call("rig_evidence", { action: "inspect", run_id: finalized.metadata.run_id })).content,
    )
    expect(completed.gates).toMatchObject([{ status: "passed", exit_code: 0 }])
    expect(completed.loop).toMatchObject({ rounds: 1, weighted: 0, stop_reason: "converged" })
  })

  it("starts evidence with an explicit action and target paths", () => {
    const start = section("rig-challenge", "Workflow").find((line) => line.includes("**Freeze the baseline.**"))
    expect(start).toContain('action: "start"')
    expect(start).toContain('skill: "rig-challenge"')
    expect(start).toContain("`paths`")
  })

  it("detects gates before starting and records a manual substitute when none exist", async () => {
    const workflow = section("rig-challenge", "Workflow")
    const start = workflow.find((line) => line.includes("**Freeze the baseline.**")) ?? ""
    expect(start).toContain('`rig_gate` with `action: "detect"`')
    expect(start.indexOf("`rig_gate`")).toBeLessThan(start.indexOf("`rig_evidence`"))
    expect(start).toContain("no configured gate")
    expect(start).toContain("`gate_exemption`")
    expect(start).toContain("reason and manual substitute at start")
    const gates = workflow.find((line) => line.includes("**Run configured gates.**"))
    expect(gates).toContain("neither a configured gate nor a recorded exemption")

    const project = new Project()
    project.write("notes.md", "# Notes\n")
    const harness = await createHarness(project)
    expect((await harness.call("rig_gate", { action: "detect" })).metadata.gates).toEqual([])
    const exemption = "No configured gate; manually check the notes against the acceptance criteria."
    await harness.call("rig_evidence", {
      action: "start",
      skill: "rig-challenge",
      paths: ["notes.md"],
      gate_exemption: exemption,
    })
    await harness.call("rig_loop", { action: "start", reviewer: "independent-challenger", findings: [] })
    await harness.call("rig_evidence", { action: "record", decision: "Manual acceptance check passed." })
    await harness.call("rig_loop", {
      action: "finalize",
      reviewer: "independent-challenger",
      stop_reason: "converged",
    })
    const finalized = await harness.call("rig_evidence", { action: "finalize" })
    expect(finalized.metadata.ok).toBe(true)
    const completed = JSON.parse(
      (await harness.call("rig_evidence", { action: "inspect", run_id: finalized.metadata.run_id })).content,
    )
    expect(completed.gate_exemption).toBe(exemption)
    expect(completed.gates).toEqual([])
  })

  it("keeps a late gate failure open for repair and independent reassessment", async () => {
    const gates = section("rig-challenge", "Workflow").find((line) => line.includes("**Run configured gates.**"))
    expect(gates).toContain("If any gate fails, do not finalize the loop")
    expect(gates).toContain("treat the failure as a finding, fix it, refresh the scope")
    expect(gates).toContain("return to step 5 for an independent reassessment before rerunning gates")

    const project = new Project()
    project.write("check.cjs", "process.exit(1)\n")
    const harness = await createHarness(project, { gates: ["node check.cjs"] })
    await harness.call("rig_evidence", { action: "start", skill: "rig-challenge", paths: ["check.cjs"] })
    await harness.call("rig_loop", { action: "start", reviewer: "independent-challenger", findings: [] })
    expect((await harness.call("rig_gate", { action: "run" })).metadata.failed).toEqual(["configured-1"])
    const pending = JSON.parse((await harness.call("rig_evidence", { action: "inspect" })).content)
    expect(pending.status).toBe("in_progress")
    expect(pending.loop).toBeUndefined()

    project.write("check.cjs", "process.exit(0)\n")
    const refreshed = await harness.call("rig_evidence", { action: "refresh", message: "Fixed the failed gate." })
    await expect(
      harness.call("rig_loop", { action: "finalize", reviewer: "independent-challenger", stop_reason: "converged" }),
    ).rejects.toThrow("refresh and append a re-assessment first")
    const reassessed = await harness.call("rig_loop", {
      action: "append",
      reviewer: "fresh-challenger",
      findings: [{ signature: "check.cjs:exit-status", severity: "high", status: "fixed" }],
    })
    expect(reassessed.metadata).toMatchObject({ weighted: 0, converged: true })
    expect((await harness.call("rig_gate", { action: "run" })).metadata.failed).toEqual([])
    await harness.call("rig_loop", { action: "finalize", reviewer: "fresh-challenger", stop_reason: "converged" })
    const finalized = await harness.call("rig_evidence", { action: "finalize" })
    expect(finalized.metadata.ok).toBe(true)
    const completed = JSON.parse(
      (await harness.call("rig_evidence", { action: "inspect", run_id: finalized.metadata.run_id })).content,
    )
    expect(completed.loop).toMatchObject({ rounds: 2, weighted: 0, stop_reason: "converged" })
    expect(completed.review).toMatchObject({
      reviewer: "fresh-challenger",
      source_digest: refreshed.metadata.scope_digest,
    })
    expect(completed.gates.at(-1)).toMatchObject({ status: "passed", source_digest: refreshed.metadata.scope_digest })
  })

  it("supplies the required reason when refreshing after a fix", async () => {
    const fix = section("rig-challenge", "Workflow").find((line) => line.includes("**Fix.**"))
    expect(fix).toContain('`action: "refresh"`, `message` giving the reason')
    const project = new Project()
    project.write("src/a.ts", "export const a = 1\n")
    const harness = await createHarness(project)
    const started = await harness.call("rig_evidence", {
      action: "start",
      skill: "rig-challenge",
      paths: ["src/a.ts"],
    })
    project.write("src/a.ts", "export const a = 2\n")
    await expect(harness.call("rig_evidence", { action: "refresh" })).rejects.toThrow("refresh requires message")
    const refreshed = await harness.call("rig_evidence", { action: "refresh", message: "Corrected the source value." })
    expect(refreshed.metadata.scope_digest).not.toBe(started.metadata.scope_digest)
    expect(refreshed.content).toContain("reason: Corrected the source value.")
  })

  it("provides an install-independent fallback for every challenger dispatch", () => {
    const rules = section("rig-challenge", "Rules")
    const dispatch = rules.find((line) => line.includes("every challenger dispatch"))
    expect(dispatch).toContain("native subagent when installed, otherwise `rig_dispatch`")
    expect(dispatch).toContain('role: "rig-challenger"')
    expect(dispatch).toContain("read_only: true")
  })

  it.each([
    { status: "fixed", weighted: 0, converged: true },
    { status: "open", weighted: 6, converged: false },
  ])("preserves $status signatures during reassessment", async ({ status, weighted, converged }) => {
    const reassess = section("rig-challenge", "Workflow").find((line) => line.includes("**Re-assess.**"))
    const statuses = [...(reassess ?? "").matchAll(/`(fixed|verified-fixed|rejected|open)`/g)].map((match) => match[1])
    expect(statuses).toContain(status)
    expect(statuses).not.toContain("verified-fixed")
    const project = new Project()
    project.write("src/a.ts", "export const a = 1\n")
    const harness = await createHarness(project)
    await harness.call("rig_evidence", { action: "start", skill: "rig-challenge", paths: ["src/a.ts"] })
    await harness.call("rig_loop", {
      action: "start",
      reviewer: "independent-challenger",
      findings: [{ signature: "src/a.ts:invariant", severity: "high", status: "open" }],
    })
    const assessed = await harness.call("rig_loop", {
      action: "append",
      reviewer: "fresh-challenger",
      findings: [{ signature: "src/a.ts:invariant", severity: "high", status }],
    })
    expect(assessed.metadata).toMatchObject({ weighted, converged })
  })

  it("uses only the configured round limit while referring scoring to the shared rules", () => {
    const ceiling = section("rig-challenge", "Rules").find((line) => line.includes("maxLoopRounds"))
    expect(ceiling).toContain("only configured")
    expect(ceiling).toContain("../../references/convergence.md")
    const workflow = section("rig-challenge", "Workflow")
    expect(workflow.find((line) => line.includes("**Score and decide.**"))).toContain("../../references/convergence.md")
    expect(workflow.find((line) => line.includes("**Print progress.**"))).toContain("returned by `rig_loop`")
  })
})

describe("distill workflow", () => {
  it("binds exact preselected input files without observing its own saves", async () => {
    const project = new Project()
    const input = ".opencode-rig/runs/rig-investigate/historical/run.json"
    project.write(input, JSON.stringify({ skill: "rig-investigate", status: "completed" }))
    const harness = await createHarness(project)
    await harness.call("rig_evidence", { action: "start", skill: "rig-distill", paths: [".opencode-rig"] })
    const selfObserved = await harness.call("rig_evidence", { action: "finalize" })
    expect(selfObserved.metadata).toMatchObject({ ok: false, status: "stale" })

    const workflow = section("rig-distill", "Workflow")
    const start = workflow.find((line) => line.includes("**Start the run.**"))
    expect(start).toContain("before starting")
    expect(start).toContain("`paths`")
    expect(start).toContain("exact input files")
    expect(start).toContain("never the artifact root")
    const index = workflow.find((line) => line.includes("**Build the index.**"))
    expect(index).toContain('action: "record"')
    expect(index).toContain("`inputs`")

    await harness.call("rig_evidence", { action: "start", skill: "rig-distill", paths: [input] })
    await harness.call("rig_evidence", { action: "record", inputs: [input], decision: "Count historical evidence." })
    await harness.call("rig_evidence", { action: "record", limits: ["Only selected historical runs were read."] })
    const finalized = await harness.call("rig_evidence", { action: "finalize" })
    expect(finalized.metadata.ok).toBe(true)
    const completed = JSON.parse(
      (await harness.call("rig_evidence", { action: "inspect", run_id: finalized.metadata.run_id })).content,
    )
    expect(completed.scope.paths).toEqual([input])
    expect(completed.scope.entries.map((entry: { path: string }) => entry.path)).toEqual([input])
    expect(completed.inputs).toMatchObject([{ path: input }])
  })

  it("reselects terminal run inputs when selected evidence changes", async () => {
    const start = section("rig-distill", "Workflow").find((line) => line.includes("**Start the run.**"))
    expect(start).toContain("status other than `in_progress`")
    expect(start).toContain("changed or missing selected input")
    expect(start).toContain("restart with a fresh selection rather than finalizing")

    const project = new Project()
    const input = ".opencode-rig/runs/rig-investigate/historical/run.json"
    project.write(input, JSON.stringify({ skill: "rig-investigate", status: "completed" }))
    const harness = await createHarness(project)
    const started = await harness.call("rig_evidence", { action: "start", skill: "rig-distill", paths: [input] })
    project.write(
      input,
      JSON.stringify({ skill: "rig-investigate", status: "completed", limits: ["Updated evidence"] }),
    )
    expect((await harness.call("rig_evidence", { action: "finalize" })).metadata).toMatchObject({
      ok: false,
      status: "stale",
    })
    const restarted = await harness.call("rig_evidence", { action: "start", skill: "rig-distill", paths: [input] })
    expect(restarted.metadata.run_id).not.toBe(started.metadata.run_id)
    expect((await harness.call("rig_evidence", { action: "finalize" })).metadata.ok).toBe(true)
  })

  it("restarts with fresh evidence when a selected input disappears", async () => {
    const start = section("rig-distill", "Workflow").find((line) => line.includes("**Start the run.**"))
    expect(start).toContain("changed or missing selected input")
    expect(start).toContain("restart with a fresh selection rather than finalizing")
    const project = new Project()
    const input = ".opencode-rig/runs/rig-investigate/historical/run.json"
    const replacement = ".opencode-rig/runs/rig-investigate/retained/run.json"
    const terminal = JSON.stringify({ skill: "rig-investigate", status: "completed" })
    project.write(input, terminal)
    project.write(replacement, terminal)
    const harness = await createHarness(project)
    const started = await harness.call("rig_evidence", { action: "start", skill: "rig-distill", paths: [input] })
    rmSync(project.path(input))
    await expect(harness.call("rig_evidence", { action: "finalize" })).rejects.toThrow("scope path does not exist")
    const restarted = await harness.call("rig_evidence", {
      action: "start",
      skill: "rig-distill",
      paths: [replacement],
    })
    expect(restarted.metadata.run_id).not.toBe(started.metadata.run_id)
    expect((await harness.call("rig_evidence", { action: "finalize" })).metadata.ok).toBe(true)
  })

  it("enumerates installed names and checks roster agreement before proposing duplicate assets", () => {
    const classify = section("rig-distill", "Workflow").find((line) => line.includes("**Classify the gap.**"))
    expect(classify).toContain("installed package's `rig.json`")
    expect(classify).toContain("rig_doctor")
    expect(classify).toContain('action: "assets"')
    expect(classify).toContain("installed skill directory listing")
    expect(classify).toContain("shipped `roles/` cards")
    expect(classify).toContain("only confirms roster consistency")
  })
})

describe("doctor workflow", () => {
  it("names every roster source needed to repair an asset addition", () => {
    const remedy = section("rig-doctor", "Response").find((line) => line.includes("Asset roster drift:"))
    expect(remedy).toContain("`rig.json`")
    expect(remedy).toContain("`src/resources/agents.json`")
    expect(remedy).toContain("`src/skill-manifest.ts`")
    expect(remedy).toContain("`src/dispatch.ts` `ROLE_IDS`")
  })

  it("keeps diagnosis read-only and routes cleanup through explicit confirmation", async () => {
    const project = new Project()
    const input = ".opencode-rig/runs/rig-investigate/historical/run.json"
    const stale = project.write(input, JSON.stringify({ status: "completed" }))
    const old = new Date(Date.now() - 10 * 24 * 60 * 60 * 1000)
    utimesSync(stale, old, old)
    utimesSync(dirname(stale), old, old)
    const harness = await createHarness(project, { ttlDays: 1 })
    await harness.call("rig_doctor", { action: "report" })
    expect(project.exists(input)).toBe(true)
    await expect(harness.call("rig_doctor", { action: "clean", confirm: false })).rejects.toThrow(
      "clean requires confirm=true",
    )
    expect(project.exists(input)).toBe(true)

    const cleanup = section("rig-doctor", "Confirmed cleanup")
    expect(cleanup.some((line) => line.includes("explicit confirmation") && line.includes("deleting"))).toBe(true)
    expect(cleanup.some((line) => line.includes('action: "clean"') && line.includes("confirm: true"))).toBe(true)
    expect(
      section("rig-distill", "Rules").some((line) => line.includes("../rig-doctor/SKILL.md#confirmed-cleanup")),
    ).toBe(true)

    const removed = await harness.call("rig_doctor", { action: "clean", confirm: true })
    expect(removed.metadata.removed).toBe(1)
    expect(project.exists(input)).toBe(false)
    expect(harness.assertions.at(-1)).toMatchObject({ action: "rig_doctor_clean" })
  })
})
