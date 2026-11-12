/** Tool boundaries retain evidence provenance and reject external file reads. */
import { readdirSync, symlinkSync } from "node:fs"
import { join } from "node:path"
import { expect, it } from "vitest"
import { createHarness, Project } from "../helpers/harness.ts"

const sourceFinding = (id: string) => ({
  id,
  severity: "low",
  dimension: "correctness",
  file: "src/a.ts",
  title: "Return the required value",
  evidence: "Observed incorrect return value",
  status: "open",
})

async function reviewSource(harness: Awaited<ReturnType<typeof createHarness>>) {
  const source = await harness.call("rig_evidence", { action: "start", skill: "rig-review", paths: ["src"] })
  await harness.call("rig_findings", {
    action: "write",
    axes: ["correctness"],
    coverage: ["src/a.ts"],
    reviewer: "reviewer",
    findings: [sourceFinding("F-1"), sourceFinding("F-2")],
  })
  expect((await harness.call("rig_evidence", { action: "finalize" })).metadata.ok).toBe(true)
  return source.metadata.run_id as string
}

async function reviewedPlan(project: Project, harness: Awaited<ReturnType<typeof createHarness>>) {
  project.write(".opencode-rig/plan.md", "Implement the scoped change and run its regression.\n")
  const plan = await harness.call("rig_evidence", {
    action: "start",
    skill: "rig-plan",
    paths: ["src"],
    plan_path: ".opencode-rig/plan.md",
  })
  const inspected = JSON.parse((await harness.call("rig_evidence", { action: "inspect" })).content)
  await harness.call("rig_evidence", { action: "record", reviewer: "challenger", plan_digest: inspected.plan.digest })
  expect((await harness.call("rig_evidence", { action: "finalize" })).metadata.ok).toBe(true)
  return plan.metadata.run_id as string
}

it("requires a reviewed plan or an explicit trivial mini-plan at implementation start", async () => {
  const harness = await createHarness(Project.withCommit())
  await expect(
    harness.call("rig_evidence", { action: "start", skill: "rig-implement", paths: ["src"] }),
  ).rejects.toThrow(/plan/)
  const started = await harness.call("rig_evidence", {
    action: "start",
    skill: "rig-implement",
    paths: ["src"],
    trivial_plan: "Correct one return value; run its regression.",
  })
  expect(started.metadata.run_id).toBeTruthy()
})

it("rejects a plan whose bytes changed after independent plan review", async () => {
  const project = Project.withCommit()
  const harness = await createHarness(project)
  const planRunId = await reviewedPlan(project, harness)
  project.write(".opencode-rig/plan.md", "A materially different implementation.\n")
  await expect(
    harness.call("rig_evidence", {
      action: "start",
      skill: "rig-implement",
      paths: ["src"],
      plan_run_id: planRunId,
      plan_path: ".opencode-rig/plan.md",
    }),
  ).rejects.toThrow(/plan/)
})

it("refuses finalization and rebinding after an implementation plan changes", async () => {
  const project = Project.withCommit()
  const harness = await createHarness(project)
  const planRunId = await reviewedPlan(project, harness)
  await harness.call("rig_evidence", {
    action: "start",
    skill: "rig-implement",
    paths: ["src"],
    plan_run_id: planRunId,
    plan_path: ".opencode-rig/plan.md",
    gate_exemption: "No configured gate; manually inspected the result.",
  })
  project.write(".opencode-rig/plan.md", "Changed after implementation started.\n")
  await expect(harness.call("rig_evidence", { action: "record", plan_path: ".opencode-rig/plan.md" })).rejects.toThrow(
    /plan/,
  )
  const final = await harness.call("rig_evidence", { action: "finalize" })
  expect(final.metadata.ok).toBe(false)
  expect(final.content).toContain("plan")
})

it("does not certify new plan bytes with a stale challenge digest", async () => {
  const project = Project.withCommit()
  const harness = await createHarness(project)
  project.write(".opencode-rig/plan.md", "First plan\n")
  await harness.call("rig_evidence", {
    action: "start",
    skill: "rig-plan",
    paths: ["src"],
    plan_path: ".opencode-rig/plan.md",
  })
  const inspected = JSON.parse((await harness.call("rig_evidence", { action: "inspect" })).content)
  project.write(".opencode-rig/plan.md", "Revised plan\n")
  await harness.call("rig_evidence", {
    action: "record",
    plan_path: ".opencode-rig/plan.md",
    reviewer: "challenger",
    plan_digest: inspected.plan.digest,
  })
  const final = await harness.call("rig_evidence", { action: "finalize" })
  expect(final.metadata.ok).toBe(false)
  expect(final.content).toContain("plan")
})

it("requires disposition of omitted source findings before remediation can complete", async () => {
  const harness = await createHarness(Project.withCommit())
  const sourceRunId = await reviewSource(harness)
  await harness.call("rig_evidence", {
    action: "start",
    skill: "rig-remediate",
    paths: ["src"],
    source_run_id: sourceRunId,
    gate_exemption: "No configured gate; manually inspected both findings.",
  })
  await harness.call("rig_findings", {
    action: "write",
    source_run_id: sourceRunId,
    findings: [{ ...sourceFinding("F-1"), status: "fixed", disposition: "Corrected and checked" }],
  })
  await harness.call("rig_evidence", { action: "record", reviewer: "independent-reviewer" })
  const incomplete = await harness.call("rig_evidence", { action: "finalize" })
  expect(incomplete.metadata.ok).toBe(false)
  expect(incomplete.content).toContain("F-2")
  await harness.call("rig_findings", {
    action: "write",
    source_run_id: sourceRunId,
    findings: [{ ...sourceFinding("F-2"), status: "rejected", disposition: "Source evidence disproves this claim" }],
  })
  expect((await harness.call("rig_evidence", { action: "finalize" })).metadata.ok).toBe(true)
})

it("rejects a completed source review that has lost its independent review binding", async () => {
  const project = Project.withCommit()
  const harness = await createHarness(project)
  const sourceRunId = await reviewSource(harness)
  const path = `.opencode-rig/runs/rig-review/${sourceRunId}/run.json`
  const source = JSON.parse(project.read(path))
  delete source.review
  project.write(path, JSON.stringify(source))
  await expect(
    harness.call("rig_evidence", {
      action: "start",
      skill: "rig-remediate",
      source_run_id: sourceRunId,
      paths: ["src"],
    }),
  ).rejects.toThrow(/review/)
})

it.each(["inputs", "outputs", "plan_path"])("rejects external symlink evidence in %s", async (field) => {
  const project = Project.withCommit()
  project.write(".gitignore", "linked\n.opencode-rig/\n")
  const outside = new Project()
  outside.write("secret.txt", "private bytes\n")
  symlinkSync(outside.root, project.path("linked"), "junction")
  const harness = await createHarness(project)
  await harness.call("rig_evidence", { action: "start", skill: "rig-investigate", paths: ["src"] })
  await expect(
    harness.call("rig_evidence", {
      action: "record",
      [field]: field === "plan_path" ? "linked/secret.txt" : ["linked/secret.txt"],
    }),
  ).rejects.toThrow(/outside|escapes/)
})

it("marks a newly allocated run failed when session registration cannot be saved", async () => {
  const project = Project.withCommit()
  const harness = await createHarness(project)
  project.write(".opencode-rig/state.json", "{broken")
  await expect(
    harness.call("rig_evidence", {
      action: "start",
      skill: "rig-investigate",
      paths: ["src"],
    }),
  ).rejects.toThrow(/state/)
  const root = project.path(".opencode-rig/runs/rig-investigate")
  const allocated = readdirSync(root)
  expect(allocated).toHaveLength(1)
  const run = JSON.parse(project.read(join(".opencode-rig/runs/rig-investigate", allocated[0]!, "run.json")))
  expect(run.status).toBe("failed")
  expect(project.read(".opencode-rig/state.json")).toBe("{broken")
})

it("forwards dispatch scope violations as structured metadata", async () => {
  const project = Project.withCommit()
  const harness = await createHarness(project)
  harness.ctx.session.wait = async () => {
    project.write("outside.txt", "unexpected mutation\n")
  }
  const result = await harness.call("rig_dispatch", {
    role: "rig-reviewer",
    task: "Inspect source",
    paths: ["src"],
  })
  expect(result.metadata.scope_violation).toBe(true)
  expect(result.metadata.violations).toContain("read-only child changed path: outside.txt")
  expect(result.metadata.workspace_unverified).toBe(false)
  expect(result.content).toContain("ignored untracked")
  expect(result.content).toContain("submodule")
})

it("restores the session pointer when explicitly refreshing a stale run", async () => {
  const project = Project.withCommit()
  const harness = await createHarness(project)
  const started = await harness.call("rig_evidence", { action: "start", skill: "rig-investigate", paths: ["src"] })
  project.write("src/a.ts", "changed\n")
  const failed = await harness.call("rig_evidence", { action: "finalize" })
  expect(failed.metadata.status).toBe("stale")
  expect(failed.content).toContain(`run_id=${started.metadata.run_id}`)
  await harness.call("rig_evidence", { action: "refresh", run_id: started.metadata.run_id, message: "adopted source" })
  const inspected = await harness.call("rig_evidence", { action: "inspect" })
  expect(JSON.parse(inspected.content).status).toBe("in_progress")
})
