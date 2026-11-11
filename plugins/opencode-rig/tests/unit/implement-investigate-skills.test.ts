import { readFileSync } from "node:fs"
import { join } from "node:path"
import { describe, expect, it } from "vitest"
import { createHarness, Project, RESOURCES_DIR } from "../helpers/harness.ts"

/** Read a skill section as independent instruction lines. */
function section(skill: string, heading: string): string[] {
  const lines = readFileSync(join(RESOURCES_DIR, "skills", skill, "SKILL.md"), "utf8").split("\n")
  const start = lines.indexOf(`## ${heading}`)
  expect(start, `${skill}: ${heading}`).toBeGreaterThan(-1)
  const end = lines.findIndex((line, index) => index > start && line.startsWith("## "))
  return lines.slice(start + 1, end < 0 ? undefined : end)
}

describe("implementation workflow", () => {
  it("explains persistent consent before the answer and records acknowledgment only afterward", () => {
    const handoff = section("rig-implement", "Workflow").find((line) => line.includes("**Commit handoff.**"))
    expect(handoff).toContain("option text")
    expect(handoff).toContain("before the user answers")
    expect(handoff).toContain("repository-wide scope across worktrees")
    expect(handoff).toContain("exclusions")
    expect(handoff).toContain("revoke")
    expect(handoff).toContain('`rig_git_grant` with `action: "revoke"`')
    expect(handoff).toContain("`scope_acknowledged: true` only after that answer")
  })

  it("compares normalized plan coordinates and raw-byte digests without rebinding", () => {
    const workflow = section("rig-implement", "Workflow")
    const verify = workflow.find((line) => line.includes("**Verify the plan.**"))
    expect(verify).toContain("normalized project-relative `plan.path` values")
    expect(verify).toContain("digest equality")
    expect(verify).not.toContain("Require identical `plan.path` values")
  })

  it("names raw-byte SHA-256 commands for POSIX and Windows at both plan checks", () => {
    const workflow = section("rig-implement", "Workflow")
    const verify = workflow.find((line) => line.includes("**Verify the plan.**"))
    expect(verify).toContain("`shasum -a 256`")
    expect(verify).toContain("`sha256sum`")
    expect(verify).toContain("`Get-FileHash -Algorithm SHA256`")
    expect(verify).toContain("lowercase hex")
    expect(verify).toContain("raw bytes")
    const finalize = workflow.find((line) => line.includes("**Finalize.**"))
    expect(finalize).toContain("raw-bytes command from step 2")
  })

  it("keeps one-time commit consent separate from a persistent grant", () => {
    const handoff = section("rig-implement", "Workflow").find((line) => line.includes("**Commit handoff.**"))
    expect(handoff).toContain("commit-handoff.md")
    expect(handoff).toContain("**Approve**, **Approve always**, and **Deny**")
    expect(handoff).toContain("Only **Approve always**")
    expect(handoff).toContain('`rig_git_grant` with `action: "approve"`')
    expect(handoff).toContain("**Approve** authorizes only the proposed commit")
    expect(handoff).toContain("**Deny** leaves changes unstaged")
    expect(handoff).not.toContain("Never commit on an absent grant")
  })

  it("accepts a bounded inline mini-plan while routing larger or risky work to planning", () => {
    const input = section("rig-implement", "Input")
    const miniPlan = input.find((line) => line.includes("inline mini-plan"))
    expect(miniPlan).toContain("`TRIVIAL`")
    expect(miniPlan).toContain("under 3 files and 50 changed lines")
    expect(miniPlan).toContain("verdict table")
    expect(miniPlan).toContain("acceptance")
    expect(input.some((line) => line.includes("HIGH_RISK") && line.includes("rig-plan"))).toBe(true)
    expect(
      section("rig-implement", "Workflow").some(
        (line) => line.includes("mini-plan") && line.includes('action: "record"'),
      ),
    ).toBe(true)
  })

  it("checks the frozen plan after binding and again before finalization", () => {
    const workflow = section("rig-implement", "Workflow")
    const start = workflow.findIndex((line) => line.includes("**Start the run.**"))
    const verify = workflow.findIndex((line) => line.includes("**Verify the plan.**"))
    expect(start).toBeGreaterThan(-1)
    expect(verify).toBeGreaterThan(start)
    expect(workflow[verify]).toContain('action: "inspect"')
    expect(workflow[verify]).toContain("completed")
    expect(workflow[verify]).toContain("reviewed `rig-plan`")
    expect(workflow[verify]).toContain("SHA-256")
    expect(workflow[verify]).toContain("instruction-enforced")
    const finalize = workflow.find((line) => line.includes("**Finalize.**"))
    expect(finalize).toContain("For a frozen plan, recheck the plan's SHA-256")
    expect(finalize).toContain("For an inline mini-plan, recheck the actual task-owned diff")
    expect(finalize).toContain("3+ files, 50+ changed lines")
    expect(finalize).toContain("`HIGH_RISK` scope")
    expect(finalize).toContain("stop")
    expect(finalize).toContain("never rebind")
  })

  it("supplies a manual substitute exemption at start only when no gate exists", async () => {
    const start = section("rig-implement", "Workflow").find((line) => line.includes("**Start the run.**"))
    expect(start).toContain('`rig_gate` with `action: "detect"`')
    expect(start).toContain("no configured gate")
    expect(start).toContain("`gate_exemption`")
    expect(start).toContain("manual substitute")
    const gates = section("rig-implement", "Workflow").find((line) => line.includes("**Run configured gates.**"))
    expect(gates).toContain("manual substitute")
    expect(gates).toContain("never substitute a manual claim for a failed check")

    const project = new Project()
    project.write("notes.md", "# Notes\n")
    const harness = await createHarness(project)
    const detected = await harness.call("rig_gate", { action: "detect" })
    expect(detected.metadata.gates).toEqual([])
    const started = await harness.call("rig_evidence", {
      action: "start",
      skill: "rig-implement",
      paths: ["notes.md"],
      gate_exemption: "No configured gate; read and validate the Markdown change manually.",
    })
    const run_id = started.metadata.run_id
    await harness.call("rig_evidence", {
      action: "record",
      run_id,
      decision: "Inline mini-plan and manual acceptance check recorded.",
    })
    const finalized = await harness.call("rig_evidence", { action: "finalize", run_id })
    expect(finalized.metadata.ok).toBe(true)
  })

  it("routes performance work to its measured optimization workflow", () => {
    const input = section("rig-implement", "Input")
    expect(input.find((line) => line.includes("`kind`"))).not.toContain("`perf`")
    expect(input.some((line) => line.includes("Performance") && line.includes("rig-optimize"))).toBe(true)
  })

  it("shows an RTK command without an unverified savings claim", () => {
    const tooling = section("rig-implement", "Tooling")
    expect(tooling.some((line) => line.includes("`rtk git status --short`"))).toBe(true)
    expect(tooling.some((line) => /\d.*%/.test(line))).toBe(false)
    expect(tooling.some((line) => line.includes("unsupported") && line.includes("original command"))).toBe(true)
    expect(tooling.some((line) => line.includes("never install it during a run"))).toBe(true)
  })
})

describe("investigation workflow", () => {
  it("records working-tree status and a diff digest before reproduction", () => {
    const workflow = section("rig-investigate", "Workflow")
    const start = workflow.findIndex((line) => line.includes("**Start the run.**"))
    const reproduce = workflow.findIndex((line) => line.includes("**Reproduce with real inputs.**"))
    expect(start).toBeLessThan(reproduce)
    expect(workflow[start]).toContain("`git status --porcelain`")
    expect(workflow[start]).toContain("SHA-256 digest of the raw `git diff` output")
    expect(workflow[start]).toContain("before reproduction")
    const preserve = section("rig-investigate", "Rules").find((line) => line.includes("Preserve the user's"))
    expect(preserve).toContain("step 1's baseline")
    expect(preserve).not.toContain("earlier in-tree experiment")
    expect(preserve).toContain("Never fall back to in-tree experiments")
  })

  it("requires scratch-byte resolution before reductions and stops on an unusable copy", () => {
    const shrink = section("rig-investigate", "Workflow").find((line) => line.includes("**Shrink the case.**"))
    expect(shrink).toContain("before any reduction or experiment")
    expect(shrink).toContain("canary edit")
    expect(shrink).toContain("must change the output")
    expect(shrink).toContain("resolved import or executable path inside the copy")
    expect(shrink).toContain("editable installs")
    expect(shrink).toContain("`.pth`")
    expect(shrink).toContain("absolute shebangs")
    expect(shrink).toContain("workspace symlinks")
    expect(shrink).toContain("does not reproduce the failure or does not isolate execution")
    expect(shrink).toContain("record that as a limit")
    expect(shrink).toContain("stop experiments")
    expect(shrink).toContain("step 7")
    expect(shrink).toContain("step 8")
  })

  it("bounds scratch inputs under ignored run artifacts and excludes credentials", () => {
    const shrink = section("rig-investigate", "Workflow").find((line) => line.includes("**Shrink the case.**"))
    expect(shrink).toContain("only the minimal failing inputs")
    expect(shrink).toContain("run directory under the project's ignored `.opencode-rig/` artifact root")
    expect(shrink).toContain("user-only")
    expect(shrink).toContain("Never copy credential files")
    expect(shrink).toContain("`.env`")
    expect(shrink).toContain("key and token files")
    expect(shrink).toContain("credential stores")
    expect(shrink).toContain("record their absence as a limit or reference them in place read-only")
    expect(shrink).not.toContain("outside the user's working tree")
    const finalize = section("rig-investigate", "Workflow").find((line) => line.includes("**Finalize.**"))
    expect(finalize).toContain("Delete the scratch copy whatever the outcome")
    expect(finalize).toContain("retained path as a limit")
    expect(finalize!.indexOf("Delete the scratch copy")).toBeLessThan(finalize!.indexOf('action: "finalize"'))
  })

  it("isolates reductions and experiments from the source read by children", () => {
    const workflow = section("rig-investigate", "Workflow")
    const shrink = workflow.find((line) => line.includes("**Shrink the case.**"))
    expect(shrink).toContain("scratch copy")
    expect(shrink).toContain("uncommitted")
    expect(shrink).toContain("never remove files from the user's working tree")
    const experiment = workflow.find((line) => line.includes("**Test one at a time.**"))
    expect(experiment).toContain("scratch copy")
    expect(experiment).toContain("untouched source snapshot")
    expect(experiment).toContain("must not edit files")
    expect(workflow.find((line) => line.includes("**Confirm the cause.**"))).toContain("scratch copy")
    const rules = section("rig-investigate", "Rules")
    expect(rules.some((line) => line.includes("production fixes") && line.includes("confirmed"))).toBe(true)
    expect(
      rules.some((line) => line.includes("undo only experiment-owned edits") && line.includes("pre-existing")),
    ).toBe(true)
  })

  it("refreshes, records a diagnosis, and finalizes the real evidence run", async () => {
    const workflow = section("rig-investigate", "Workflow")
    const record = workflow.findIndex((line) => line.includes("**Record the diagnosis.**"))
    const finalize = workflow.findIndex((line) => line.includes("**Finalize.**"))
    expect(record).toBeGreaterThan(-1)
    expect(workflow[record]).toContain('`rig_evidence` with `action: "refresh"`')
    expect(workflow[record]).toContain('action: "record"')
    expect(workflow[record]).toContain("`decision`")
    expect(workflow[record]).toContain("`limits`")
    expect(workflow[record]).toContain("`confidence`")
    expect(finalize).toBeGreaterThan(record)
    expect(workflow[finalize]).toContain('action: "finalize"')
    const reproduction = workflow.find((line) => line.includes("**Reproduce with real inputs.**"))
    expect(reproduction).toContain("incomplete diagnosis in step 7")
    expect(reproduction).toContain("finalize in step 8")
    expect(workflow[finalize]).toContain("does not turn an incomplete diagnosis")

    const project = new Project()
    project.write("input.txt", "original failing input\n")
    const harness = await createHarness(project)
    const started = await harness.call("rig_evidence", {
      action: "start",
      skill: "rig-investigate",
      paths: ["input.txt"],
    })
    const run_id = started.metadata.run_id
    await harness.call("rig_evidence", { action: "refresh", run_id, message: "Experiments used a scratch copy." })
    await harness.call("rig_evidence", {
      action: "record",
      run_id,
      decision: "Reproduction and eliminated hypotheses recorded.",
      limits: ["Controlled input only."],
      confidence: 0.92,
    })
    const finalized = await harness.call("rig_evidence", { action: "finalize", run_id })
    expect(finalized.metadata.ok).toBe(true)
    const completed = JSON.parse((await harness.call("rig_evidence", { action: "inspect", run_id })).content)
    expect(completed).toMatchObject({ status: "completed", confidence: 0.92, limits: ["Controlled input only."] })
    expect(project.read("input.txt")).toBe("original failing input\n")
  })

  it("removes the unregistered codemap probe and routes a diagnosis to planning", () => {
    expect(section("rig-investigate", "Rules").some((line) => line.includes("probe codemap"))).toBe(false)
    const output = section("rig-investigate", "Output")
    expect(output.some((line) => line.includes("rig-plan") && line.includes("fix"))).toBe(true)
    expect(output.some((line) => line.includes("Do not present the fix as already verified"))).toBe(true)
  })
})

describe.each(["rig-implement", "rig-investigate"])("%s drift recovery", (skill) => {
  it("refreshes a stale run with a reason and falls back to a replacement run only when it cannot be refreshed", () => {
    const finalize = section(skill, "Workflow").find((line) => line.includes("**Finalize.**"))
    expect(finalize).toContain("step 7 detects source drift")
    expect(finalize).toContain("finalization returns `stale`")
    expect(finalize).toContain('`action: "refresh"`, the run ID and a `message` giving the reason')
    expect(finalize).toContain("rerun the affected evidence and required checks")
    expect(finalize).toContain("finalize again")
    expect(finalize).toContain("a completed or failed run cannot be refreshed")
    expect(finalize).toContain("start one replacement run with the old run ID in its start `message`")
    expect(finalize).toContain("If drift recurs, stop and report incomplete")
    expect(finalize).not.toContain("drift means refresh and rerun")
  })
})
