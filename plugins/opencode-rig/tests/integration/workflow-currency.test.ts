/** Workflow handoffs never promote stale validation or change deleted-file identities. */
import { expect, it } from "vitest"
import { sha256 } from "../../src/digest.ts"
import { createHarness, Project } from "../helpers/harness.ts"

it("requires new validation after a later gate changes source", async () => {
  const project = Project.withCommit({ "src/a.ts": "valid\n" })
  const harness = await createHarness(project, {
    gates: [
      "node -e \"process.exit(require('node:fs').readFileSync('src/a.ts','utf8') === 'valid\\n' ? 0 : 1)\"",
      "node -e \"require('node:fs').writeFileSync('src/a.ts','invalid\\n')\"",
    ],
  })
  const started = await harness.call("rig_evidence", {
    action: "start",
    skill: "rig-implement",
    trivial_plan: "Update the scoped return value and run its regression.",
    paths: ["src"],
  })
  await harness.call("rig_gate", { action: "run" })
  expect(project.read("src/a.ts")).toBe("invalid\n")
  await harness.call("rig_evidence", { action: "refresh", message: "gate changed source" })
  const finalized = await harness.call("rig_evidence", { action: "finalize" })
  expect(finalized.metadata.ok).toBe(false)
  const run = JSON.parse(
    (await harness.call("rig_evidence", { action: "inspect", run_id: started.metadata.run_id })).content,
  )
  expect(run.gates).toHaveLength(2)
  expect(run.gates[0].status).toBe("passed")
  expect(run.gates[1].status).toBe("passed")
  expect(run.gates[0].source_digest).toBeUndefined()
  expect(run.gates[1].source_digest).toBeUndefined()
  expect(run.scope.digest).not.toBe(started.metadata.scope_digest)
  const validation = await harness.call("rig_gate", { action: "run", names: ["configured-1"] })
  expect(validation.metadata.failed).toEqual(["configured-1"])
})

it("does not revive a drifting gate batch when source returns to its original bytes", async () => {
  const project = Project.withCommit({ "src/a.ts": "baseline\n" })
  const harness = await createHarness(project, {
    gates: [
      "node -e \"require('node:fs').writeFileSync('src/a.ts','candidate\\n')\"",
      "node -e \"process.exit(require('node:fs').readFileSync('src/a.ts','utf8') === 'candidate\\n' ? 0 : 1)\"",
    ],
  })
  await harness.call("rig_evidence", {
    action: "start",
    skill: "rig-implement",
    trivial_plan: "Update the scoped return value and run its regression.",
    paths: ["src"],
  })
  await harness.call("rig_gate", { action: "run" })
  expect(project.read("src/a.ts")).toBe("candidate\n")
  project.write("src/a.ts", "baseline\n")
  await harness.call("rig_evidence", { action: "refresh", message: "reverted gate's candidate" })
  const finalized = await harness.call("rig_evidence", { action: "finalize" })
  expect(finalized.metadata.ok).toBe(false)
})

it("does not certify a batch whose later gate restores source after another validated different bytes", async () => {
  const project = Project.withCommit({ "src/a.ts": "baseline\n" })
  const harness = await createHarness(project, {
    gates: [
      "node -e \"require('node:fs').writeFileSync('src/a.ts','candidate\\n')\"",
      "node -e \"process.exit(require('node:fs').readFileSync('src/a.ts','utf8') === 'candidate\\n' ? 0 : 1)\"",
      "node -e \"require('node:fs').writeFileSync('src/a.ts','baseline\\n')\"",
    ],
  })
  const started = await harness.call("rig_evidence", {
    action: "start",
    skill: "rig-implement",
    trivial_plan: "Update the scoped return value and run its regression.",
    paths: ["src"],
  })
  await harness.call("rig_gate", { action: "run" })
  expect(project.read("src/a.ts")).toBe("baseline\n")
  const finalized = await harness.call("rig_evidence", { action: "finalize" })
  expect(finalized.metadata.ok).toBe(false)
  const run = JSON.parse(
    (await harness.call("rig_evidence", { action: "inspect", run_id: started.metadata.run_id })).content,
  )
  expect(run.gates).toHaveLength(3)
  expect(run.gates[1].source_digest).toBeUndefined()
})

it("completes when every gate validates unchanged source", async () => {
  const project = Project.withCommit({ "src/a.ts": "valid\n" })
  const harness = await createHarness(project, {
    gates: [
      "node -e \"process.exit(require('node:fs').readFileSync('src/a.ts','utf8') === 'valid\\n' ? 0 : 1)\"",
      'node -e "process.exit(0)"',
    ],
  })
  const started = await harness.call("rig_evidence", {
    action: "start",
    skill: "rig-implement",
    trivial_plan: "Update the scoped return value and run its regression.",
    paths: ["src"],
  })
  await harness.call("rig_gate", { action: "run" })
  const finalized = await harness.call("rig_evidence", { action: "finalize" })
  expect(finalized.metadata.ok).toBe(true)
  const run = JSON.parse(
    (await harness.call("rig_evidence", { action: "inspect", run_id: started.metadata.run_id })).content,
  )
  expect(run.gates).toHaveLength(2)
  expect(run.gates[0].source_digest).toBe(started.metadata.scope_digest)
  expect(run.gates[1].source_digest).toBe(started.metadata.scope_digest)
})

it("certifies a formatter rerun after refreshing its edited source while retaining the first sample", async () => {
  const project = Project.withCommit({ "src/a.ts": "unformatted\n" })
  const harness = await createHarness(project, {
    gates: ["node -e \"require('node:fs').writeFileSync('src/a.ts','formatted\\n')\""],
  })
  const started = await harness.call("rig_evidence", {
    action: "start",
    skill: "rig-implement",
    trivial_plan: "Update the scoped return value and run its regression.",
    paths: ["src"],
  })
  await harness.call("rig_gate", { action: "run" })
  await harness.call("rig_evidence", { action: "refresh", message: "formatter updated source" })
  await harness.call("rig_gate", { action: "run" })
  const finalized = await harness.call("rig_evidence", { action: "finalize" })
  expect(finalized.metadata.ok).toBe(true)
  const run = JSON.parse(
    (await harness.call("rig_evidence", { action: "inspect", run_id: started.metadata.run_id })).content,
  )
  expect(run.gates).toHaveLength(2)
  expect(run.gates[0].source_digest).toBeUndefined()
  expect(run.gates[1].source_digest).toBe(run.scope.digest)
})

it("preserves a deleted implementation output's digest in the review scope", async () => {
  const project = Project.withCommit()
  const harness = await createHarness(project)
  await harness.call("rig_evidence", {
    action: "start",
    skill: "rig-implement",
    trivial_plan: "Update the scoped return value and run its regression.",
    paths: ["src/a.ts"],
  })
  project.git("rm", "-q", "src/a.ts")
  await harness.call("rig_evidence", { action: "refresh", message: "removed obsolete source" })
  await harness.call("rig_evidence", { action: "record", outputs: ["src/a.ts"] })
  const implementation = JSON.parse((await harness.call("rig_evidence", { action: "inspect" })).content)
  await harness.call("rig_evidence", { action: "start", skill: "rig-review", paths: ["src/a.ts"] })
  const review = JSON.parse((await harness.call("rig_evidence", { action: "inspect" })).content)
  expect(review.scope.entries).toEqual([{ path: "src/a.ts", status: "D ", sha256: sha256("") }])
  expect(implementation.outputs).toEqual([{ path: "src/a.ts", sha256: review.scope.entries[0].sha256 }])
})
