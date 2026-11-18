/** Tool boundaries retain evidence provenance and reject external file reads. */
import { readdirSync, symlinkSync } from "node:fs"
import { join } from "node:path"
import { expect, it } from "vitest"
import { createHarness, Project, trivialPlan } from "../helpers/harness.ts"

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
  const source = await harness.call("rig_evidence", { action: "start", skill: "code-review", paths: ["src"] })
  await harness.call("rig_findings", {
    action: "write",
    axes: ["correctness"],
    coverage: ["src/a.ts"],
    reviewer: "reviewer",
    findings: [sourceFinding("F-1"), sourceFinding("F-2")],
  })
  const done = await harness.call("rig_evidence", { action: "finalize" })
  expect(done.metadata.ok, done.content).toBe(true)
  return source.metadata.run_id as string
}

async function reviewedPlan(project: Project, harness: Awaited<ReturnType<typeof createHarness>>) {
  project.write(
    ".opencode-rig/plan.md",
    `Implement the scoped change and run its regression.
${TREE}`,
  )
  const plan = await harness.call("rig_evidence", {
    action: "start",
    skill: "rig-plan",
    paths: ["src"],
    plan_path: ".opencode-rig/plan.md",
  })
  const inspected = JSON.parse((await harness.call("rig_evidence", { action: "inspect" })).content)
  await harness.call("rig_evidence", { action: "record", reviewer: "challenger", plan_digest: inspected.plan.digest })
  const done = await harness.call("rig_evidence", { action: "finalize" })
  expect(done.metadata.ok, done.content).toBe(true)
  return plan.metadata.run_id as string
}

const TREE = [
  "",
  "## Task tree",
  "",
  "```json",
  '{"schema":1,"root":"T-1","nodes":[{"id":"T-1","parent":null,"goal":"Deliver the scoped change","owned_paths":["src/a.ts"],"acceptance_check":"focused test","depends_on":[],"leaf":true}]}',
  "```",
  "",
  "## Challenge rounds",
  "",
  "- Round 1: challenger; no blocking findings; converged.",
  "",
].join(String.fromCharCode(10))

it("requires a reviewed plan or an explicit trivial mini-plan at implementation start", async () => {
  const harness = await createHarness(Project.withCommit())
  await expect(
    harness.call("rig_evidence", { action: "start", skill: "rig-implement", paths: ["src"] }),
  ).rejects.toThrow(/plan/)
  const started = await harness.call("rig_evidence", {
    action: "start",
    skill: "rig-implement",
    trivial_plan: trivialPlan(["src/a.ts"]),
  })
  expect(started.metadata.run_id).toBeTruthy()
})

it("rejects a plan whose bytes changed after independent plan review", async () => {
  const project = Project.withCommit()
  const harness = await createHarness(project)
  const planRunId = await reviewedPlan(project, harness)
  project.write(
    ".opencode-rig/plan.md",
    `A materially different implementation.
${TREE}`,
  )
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
  project.write(
    ".opencode-rig/plan.md",
    `Changed after implementation started.
${TREE}`,
  )
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
  project.write(
    ".opencode-rig/plan.md",
    `First plan
${TREE}`,
  )
  await harness.call("rig_evidence", {
    action: "start",
    skill: "rig-plan",
    paths: ["src"],
    plan_path: ".opencode-rig/plan.md",
  })
  const inspected = JSON.parse((await harness.call("rig_evidence", { action: "inspect" })).content)
  project.write(
    ".opencode-rig/plan.md",
    `Revised plan
${TREE}`,
  )
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

it("requires a final independent challenge for a frozen plan", async () => {
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
  const without = await harness.call("rig_evidence", { action: "finalize" })
  expect(without.metadata.ok).toBe(false)
  expect(without.content).toContain("final challenge")
  await harness.call("rig_evidence", { action: "record", reviewer: "challenger" })
  const withoutFindings = await harness.call("rig_evidence", { action: "finalize" })
  expect(withoutFindings.metadata.ok).toBe(false)
  expect(withoutFindings.content).toContain("findings artifact is missing")
  await harness.call("rig_findings", { action: "write", reviewer: "challenger", findings: [] })
  const withChallenge = await harness.call("rig_evidence", { action: "finalize" })
  expect(withChallenge.metadata.ok).toBe(true)
})

it("requires disposition of omitted source findings before remediation can complete", async () => {
  const harness = await createHarness(Project.withCommit())
  const sourceRunId = await reviewSource(harness)
  await harness.call("rig_evidence", {
    action: "start",
    skill: "code-remediate",
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
  const done = await harness.call("rig_evidence", { action: "finalize" })
  expect(done.metadata.ok, done.content).toBe(true)
})

it("rejects a completed source review that has lost its independent review binding", async () => {
  const project = Project.withCommit()
  const harness = await createHarness(project)
  const sourceRunId = await reviewSource(harness)
  const path = `.opencode-rig/runs/code-review/${sourceRunId}/run.json`
  const source = JSON.parse(project.read(path))
  delete source.review
  project.write(path, JSON.stringify(source))
  await expect(
    harness.call("rig_evidence", {
      action: "start",
      skill: "code-remediate",
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
    role: "rig-auditor",
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

it("requires a converged loop when the final challenge finds a high issue", async () => {
  const project = Project.withCommit()
  const harness = await createHarness(project, { gates: ["true"] })
  const planRunId = await reviewedPlan(project, harness)
  const started = await harness.call("rig_evidence", {
    action: "start",
    skill: "rig-implement",
    paths: ["src"],
    plan_run_id: planRunId,
    plan_path: ".opencode-rig/plan.md",
  })
  const runId = started.metadata.run_id as string
  await harness.call("rig_gate", { action: "run", run_id: runId })
  await harness.call("rig_findings", {
    action: "write",
    run_id: runId,
    reviewer: "challenger",
    findings: [
      {
        id: "F-001",
        severity: "high",
        dimension: "correctness",
        file: "src/a.ts",
        title: "Return the required value",
        evidence: "The scoped return value contradicts the acceptance check",
        status: "fixed",
        disposition: "Corrected and rechecked with the configured gate",
      },
    ],
  })
  const withoutLoop = await harness.call("rig_evidence", { action: "finalize", run_id: runId })
  expect(withoutLoop.metadata.ok).toBe(false)
  expect(withoutLoop.content).toMatch(/record the fix-and-re-challenge loop/)

  await harness.call("rig_loop", {
    action: "start",
    run_id: runId,
    reviewer: "challenger",
    findings: [{ signature: "F-001", severity: "high", status: "open" }],
  })
  await harness.call("rig_loop", {
    action: "append",
    run_id: runId,
    reviewer: "challenger",
    findings: [{ signature: "F-001", severity: "high", status: "fixed" }],
  })
  const loop = await harness.call("rig_loop", {
    action: "finalize",
    run_id: runId,
    reviewer: "challenger",
    stop_reason: "converged",
  })
  expect(loop.content).toMatch(/Loop finalized: converged/)
  const done = await harness.call("rig_evidence", { action: "finalize", run_id: runId })
  expect(done.metadata.ok, done.content).toBe(true)
})

it("requires a reviewer on an implementation findings write", async () => {
  const project = Project.withCommit()
  const harness = await createHarness(project)
  await harness.call("rig_evidence", {
    action: "start",
    skill: "rig-implement",
    trivial_plan: trivialPlan(["src/a.ts"]),
  })
  await expect(harness.call("rig_findings", { action: "write", findings: [] })).rejects.toThrow(
    /requires reviewer: the independent challenger identity/,
  )
})

it("rejects a re-assessment that converges with an open high finding", async () => {
  const project = Project.withCommit()
  const harness = await createHarness(project)
  const started = await harness.call("rig_evidence", {
    action: "start",
    skill: "rig-implement",
    trivial_plan: trivialPlan(["src/a.ts"]),
    gate_exemption: "No configured gate; the focused check is recorded manually.",
  })
  const runId = started.metadata.run_id as string
  await harness.call("rig_loop", {
    action: "start",
    run_id: runId,
    reviewer: "challenger",
    findings: [
      { signature: "F-001", severity: "high", status: "open" },
      { signature: "F-002", severity: "high", status: "open" },
    ],
  })
  await harness.call("rig_loop", {
    action: "append",
    run_id: runId,
    reviewer: "challenger",
    findings: [
      { signature: "F-001", severity: "high", status: "fixed" },
      { signature: "F-002", severity: "high", status: "open" },
    ],
  })
  await expect(
    harness.call("rig_loop", { action: "finalize", run_id: runId, reviewer: "challenger", stop_reason: "converged" }),
  ).rejects.toThrow(/cannot converge an implementation loop with 1 open high finding/)
})

it("archives a closed loop ledger on refresh so a new challenge can start", async () => {
  const project = Project.withCommit({ "src/a.ts": "export const a = 1\n" })
  const harness = await createHarness(project)
  const started = await harness.call("rig_evidence", {
    action: "start",
    skill: "rig-implement",
    trivial_plan: trivialPlan(["src/a.ts"]),
    gate_exemption: "No configured gate; the focused check is recorded manually.",
  })
  const runId = started.metadata.run_id as string
  await harness.call("rig_loop", { action: "start", run_id: runId, reviewer: "challenger", findings: [] })
  await harness.call("rig_loop", {
    action: "finalize",
    run_id: runId,
    reviewer: "challenger",
    stop_reason: "converged",
  })
  project.write("src/a.ts", "export const a = 2\n")
  await harness.call("rig_evidence", { action: "refresh", message: "source changed after the first challenge" })
  const restarted = await harness.call("rig_loop", {
    action: "start",
    run_id: runId,
    reviewer: "challenger",
    findings: [],
  })
  expect(restarted.content).toMatch(/Baseline recorded/)
})

it("does not merge a findings inventory that describes different bytes", async () => {
  const project = Project.withCommit({ "src/a.ts": "export const a = 1\n" })
  const harness = await createHarness(project)
  const started = await harness.call("rig_evidence", {
    action: "start",
    skill: "rig-implement",
    trivial_plan: trivialPlan(["src/a.ts"]),
    gate_exemption: "No configured gate; the focused check is recorded manually.",
  })
  const runId = started.metadata.run_id as string
  await harness.call("rig_findings", {
    action: "write",
    run_id: runId,
    reviewer: "challenger",
    findings: [
      {
        id: "F-001",
        severity: "low",
        dimension: "correctness",
        file: "src/a.ts",
        title: "Old finding",
        evidence: "Describes the previous bytes",
        status: "fixed",
        disposition: "Corrected before the rewrite",
      },
    ],
  })
  project.write("src/a.ts", "export const a = 2\n")
  await harness.call("rig_evidence", { action: "refresh", message: "source rewritten after the challenge" })
  await harness.call("rig_findings", { action: "write", run_id: runId, reviewer: "challenger", findings: [] })
  const read = JSON.parse((await harness.call("rig_findings", { action: "read", run_id: runId })).content)
  expect(read.findings).toEqual([])
})
