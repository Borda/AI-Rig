import { existsSync, utimesSync } from "node:fs"
import { join } from "node:path"
import { describe, expect, it } from "vitest"
import { Project, createHarness } from "../helpers/harness.ts"

function changedProject(files: Record<string, string> = { "src/a.ts": "export const a = 1\n" }): Project {
  const project = Project.withCommit(files)
  project.write("src/a.ts", "export const a = 2\n")
  project.write(".gitignore", ".opencode-rig/\n")
  return project
}

describe("plugin registration", () => {
  it("registers the documented tools, skills, and commands", async () => {
    const project = changedProject()
    const harness = await createHarness(project)
    expect([...harness.tools.keys()].sort()).toEqual([
      "rig_dispatch",
      "rig_doctor",
      "rig_evidence",
      "rig_findings",
      "rig_gate",
      "rig_git_grant",
      "rig_loop",
    ])
    expect(harness.skills).toHaveLength(8)
    expect(harness.skills.map((skill) => skill.id)).toContain("rig-challenge")
    expect(harness.skills.every((skill) => typeof skill.content === "string" && skill.content.length > 0)).toBe(true)
    expect(harness.skills.every((skill) => !skill.content.startsWith("---"))).toBe(true)
    expect(harness.commands.map((command) => command.name).sort()).toEqual(
      harness.skills.map((skill) => skill.id).sort(),
    )
  })

  it("registers a permission hook, tool journal hooks, and a context hook", async () => {
    const project = changedProject()
    const harness = await createHarness(project)
    expect(harness.permissionHooks.has("evaluate")).toBe(true)
    expect(harness.toolHooks.get("execute.before")).toHaveLength(1)
    expect(harness.toolHooks.get("execute.after")).toHaveLength(1)
    expect(harness.sessionHooks.get("context")).toHaveLength(1)
  })
})

describe("rig_evidence", () => {
  it("runs the documented lifecycle and writes a report", async () => {
    const project = changedProject()
    const harness = await createHarness(project, { gates: ["true"] })

    const started = await harness.call("rig_evidence", { action: "start", skill: "rig-implement", paths: ["src"] })
    expect(started.content).toMatch(/Run started/)
    const runId = started.metadata.run_id as string
    expect(runId).toMatch(/Z-[0-9a-f]{4}$/)

    const inspected = await harness.call("rig_evidence", { action: "inspect" })
    expect(JSON.parse(inspected.content)).toMatchObject({
      run_id: runId,
      skill: "rig-implement",
      status: "in_progress",
    })

    const recorded = await harness.call("rig_evidence", {
      action: "record",
      outputs: ["src/a.ts"],
      limits: ["No Windows host"],
      confidence: 0.9,
      decision: "Keep the change minimal.",
    })
    expect(recorded.content).toMatch(/outputs: 1/)
    expect(project.exists(".opencode-rig/state.json")).toBe(true)

    await harness.call("rig_evidence", { action: "refresh", message: "formatter rewrote one line" })

    const gate = await harness.call("rig_gate", { action: "run" })
    expect(gate.content).toMatch(/All recorded gates passed/)

    const finalized = await harness.call("rig_evidence", { action: "finalize" })
    expect(finalized.metadata.ok).toBe(true)
    expect(finalized.content).toMatch(/Run completed/)
    expect(existsSync(finalized.metadata.report_path as string)).toBe(true)

    const run = JSON.parse((await harness.call("rig_evidence", { action: "inspect" })).content)
    expect(run.status).toBe("completed")
    expect(run.outputs).toHaveLength(1)
    expect(run.limits).toEqual(["No Windows host"])
    expect(run.confidence).toBe(0.9)
  })

  it("rejects a start without a shipped skill", async () => {
    const harness = await createHarness(changedProject())
    await expect(harness.call("rig_evidence", { action: "start" })).rejects.toThrow(/start requires skill/)
    await expect(harness.call("rig_evidence", { action: "start", skill: "rig-deploy" })).rejects.toThrow(
      /start requires skill/,
    )
  })

  it("rejects a missing run and an unknown run id", async () => {
    const harness = await createHarness(changedProject())
    await expect(harness.call("rig_evidence", { action: "inspect" })).rejects.toThrow(/no active run/)
    await expect(harness.call("rig_evidence", { action: "inspect", run_id: "missing" })).rejects.toThrow(/unknown run/)
  })

  it("refuses to finalize without a gate receipt", async () => {
    const harness = await createHarness(changedProject(), { gates: ["true"] })
    await harness.call("rig_evidence", { action: "start", skill: "rig-implement", paths: ["src"] })
    const result = await harness.call("rig_evidence", { action: "finalize" })
    expect(result.metadata).toMatchObject({ ok: false, status: "in_progress" })
    expect(result.content).toMatch(/no passing gate receipt/)
  })

  it("marks a run stale when source changes after the scope was recorded", async () => {
    const project = changedProject()
    const harness = await createHarness(project, { gates: ["true"] })
    await harness.call("rig_evidence", { action: "start", skill: "rig-implement", paths: ["src"] })
    await harness.call("rig_gate", { action: "run" })
    project.write("src/a.ts", "export const a = 3\n")
    const result = await harness.call("rig_evidence", { action: "finalize" })
    expect(result.metadata).toMatchObject({ ok: false, status: "stale" })
    expect(result.content).toMatch(/refresh/)
  })

  it("requires a reason to refresh and refuses to refresh a commit scope", async () => {
    const project = changedProject()
    const harness = await createHarness(project)
    await harness.call("rig_evidence", { action: "start", skill: "rig-investigate", paths: ["src"] })
    await expect(harness.call("rig_evidence", { action: "refresh" })).rejects.toThrow(/refresh requires message/)

    const commit = project.git("rev-parse", "HEAD")
    await harness.call("rig_evidence", { action: "start", skill: "rig-investigate", commit })
    await expect(harness.call("rig_evidence", { action: "refresh", message: "x" })).rejects.toThrow(
      /commit scope is immutable/,
    )
  })

  it("binds a plan by digest and rejects a missing plan file", async () => {
    const project = changedProject()
    project.write("plan.md", "# Plan\n")
    const harness = await createHarness(project)
    await harness.call("rig_evidence", { action: "start", skill: "rig-plan", paths: ["src"] })
    await expect(harness.call("rig_evidence", { action: "record", plan_path: "missing.md" })).rejects.toThrow(
      /plan does not exist/,
    )
    const recorded = await harness.call("rig_evidence", {
      action: "record",
      plan_path: "plan.md",
      reviewer: "rig-challenger",
    })
    expect(recorded.content).toMatch(/plan: plan.md/)
  })

  it("rejects an evidence path outside the project", async () => {
    const harness = await createHarness(changedProject())
    await harness.call("rig_evidence", { action: "start", skill: "rig-investigate", paths: ["src"] })
    await expect(harness.call("rig_evidence", { action: "record", outputs: ["../escape.txt"] })).rejects.toThrow(
      /escapes the project/,
    )
  })

  it("refuses review evidence recorded after source drift", async () => {
    const project = changedProject()
    const harness = await createHarness(project)
    await harness.call("rig_evidence", { action: "start", skill: "rig-review", paths: ["src"] })
    project.write("src/a.ts", "export const a = 4\n")
    await expect(harness.call("rig_evidence", { action: "record", reviewer: "rig-reviewer" })).rejects.toThrow(
      /source changed after the recorded scope/,
    )
  })
})

describe("rig_gate", () => {
  it("reports that no gate is configured instead of inventing one", async () => {
    const harness = await createHarness(changedProject())
    const detected = await harness.call("rig_gate", { action: "detect" })
    expect(detected.content).toMatch(/No configured gates detected/)
    expect(detected.metadata.gates).toEqual([])
  })

  it("detects configured gates in order", async () => {
    const harness = await createHarness(changedProject(), { gates: ["true", "false"] })
    const detected = await harness.call("rig_gate", { action: "detect" })
    expect(detected.metadata.gates).toEqual(["configured-1", "configured-2"])
    expect(detected.content).toContain("configured-1\ttrue\t(options.gates)")
  })

  it("records a passing proof and a failing exit code", async () => {
    const project = changedProject()
    const harness = await createHarness(project, { gates: ["true", "false"] })
    await harness.call("rig_evidence", { action: "start", skill: "rig-implement", paths: ["src"] })

    const passing = await harness.call("rig_gate", { action: "run", names: ["configured-1"] })
    expect(passing.metadata.failed).toEqual([])
    expect(harness.assertions.at(-1)).toMatchObject({ action: "shell", resources: ["true"] })

    const failing = await harness.call("rig_gate", { action: "run", names: ["configured-2"] })
    expect(failing.metadata.failed).toEqual(["configured-2"])
    expect(failing.content).toMatch(/FAILED: configured-2/)

    expect(project.exists(".opencode-rig/runs/rig-implement")).toBe(true)
  })

  it("rejects an unknown gate name", async () => {
    const harness = await createHarness(changedProject(), { gates: ["true"] })
    await harness.call("rig_evidence", { action: "start", skill: "rig-implement", paths: ["src"] })
    await expect(harness.call("rig_gate", { action: "run", names: ["nope"] })).rejects.toThrow(
      /unknown gate\(s\): nope/,
    )
  })

  it("rejects a run when no gate is configured", async () => {
    const harness = await createHarness(changedProject())
    await harness.call("rig_evidence", { action: "start", skill: "rig-implement", paths: ["src"] })
    await expect(harness.call("rig_gate", { action: "run" })).rejects.toThrow(/no configured gates detected/)
  })
})

describe("rig_findings", () => {
  const open = {
    id: "F-001",
    severity: "high",
    dimension: "correctness",
    file: "src/a.ts",
    title: "Drops the last record",
    evidence: "Splitting on newline without filtering loses a trailing record.",
    status: "open",
  }

  async function reviewHarness(): Promise<{ project: Project; harness: Awaited<ReturnType<typeof createHarness>> }> {
    const project = changedProject()
    const harness = await createHarness(project)
    await harness.call("rig_evidence", { action: "start", skill: "rig-review", paths: ["src"] })
    return { project, harness }
  }

  it("writes, merges, and reads a validated inventory", async () => {
    const { harness } = await reviewHarness()
    const first = await harness.call("rig_findings", {
      action: "write",
      axes: ["correctness", "tests"],
      coverage: ["src/a.ts"],
      reviewer: "rig-reviewer",
      findings: [open],
    })
    expect(first.content).toMatch(/open 1, fixed 0/)
    expect(first.metadata.open).toBe(1)

    await harness.call("rig_findings", {
      action: "write",
      reviewer: "rig-reviewer",
      findings: [{ ...open, status: "fixed", disposition: "Filtered empty lines and added a test." }],
    })
    const read = JSON.parse((await harness.call("rig_findings", { action: "read" })).content)
    expect(read.findings).toHaveLength(1)
    expect(read.findings[0].status).toBe("fixed")
    expect(read.axes).toEqual(["correctness", "tests"])
    expect(read.target_digest).toMatch(/^[a-f0-9]{64}$/)
  })

  it("requires axes and coverage", async () => {
    const { harness } = await reviewHarness()
    await expect(
      harness.call("rig_findings", { action: "write", coverage: ["src"], reviewer: "rig-reviewer", findings: [] }),
    ).rejects.toThrow(/requires axes/)
  })

  it("requires a reviewer for a rig-review write", async () => {
    const { harness } = await reviewHarness()
    await expect(
      harness.call("rig_findings", { action: "write", axes: ["correctness"], coverage: ["src"], findings: [] }),
    ).rejects.toThrow(/requires reviewer/)
  })

  it("rejects a terminal status without a disposition", async () => {
    const { harness } = await reviewHarness()
    await expect(
      harness.call("rig_findings", {
        action: "write",
        axes: ["correctness"],
        coverage: ["src"],
        reviewer: "rig-reviewer",
        findings: [{ ...open, status: "fixed" }],
      }),
    ).rejects.toThrow(/without a disposition/)
  })

  it("rejects a review write after source drift", async () => {
    const { project, harness } = await reviewHarness()
    project.write("src/a.ts", "export const a = 9\n")
    await expect(
      harness.call("rig_findings", {
        action: "write",
        axes: ["correctness"],
        coverage: ["src"],
        reviewer: "rig-reviewer",
        findings: [],
      }),
    ).rejects.toThrow(/start a new review run/)
  })

  it("completes a review run with recorded findings", async () => {
    const { harness } = await reviewHarness()
    await harness.call("rig_findings", {
      action: "write",
      axes: ["correctness"],
      coverage: ["src/a.ts"],
      reviewer: "rig-reviewer",
      findings: [open],
    })
    const finalized = await harness.call("rig_evidence", { action: "finalize" })
    expect(finalized.metadata.ok).toBe(true)
  })
})

describe("rig_loop", () => {
  async function challengeHarness(options: Record<string, unknown> = { gates: ["true"] }) {
    const project = changedProject()
    const harness = await createHarness(project, options)
    await harness.call("rig_evidence", { action: "start", skill: "rig-challenge", paths: ["src"] })
    return { project, harness }
  }

  const high = { signature: "src/a.ts:null-deref", severity: "high" as const }

  it("drives a converged loop and completes the run", async () => {
    const { harness } = await challengeHarness()
    const started = await harness.call("rig_loop", {
      action: "start",
      reviewer: "rig-challenger",
      findings: [{ ...high, status: "open" }],
    })
    expect(started.content).toMatch(/Weighted score W0 = 6/)

    const appended = await harness.call("rig_loop", {
      action: "append",
      reviewer: "rig-challenger",
      findings: [{ ...high, status: "fixed" }],
    })
    expect(appended.metadata.converged).toBe(true)

    const finalized = await harness.call("rig_loop", {
      action: "finalize",
      reviewer: "rig-challenger",
      stop_reason: "converged",
    })
    expect(finalized.metadata.stop_reason).toBe("converged")

    await harness.call("rig_gate", { action: "run" })
    const done = await harness.call("rig_evidence", { action: "finalize" })
    expect(done.metadata.ok).toBe(true)
  })

  it("rejects appending without re-assessing a prior signature", async () => {
    const { harness } = await challengeHarness()
    await harness.call("rig_loop", {
      action: "start",
      reviewer: "rig-challenger",
      findings: [{ ...high, status: "open" }],
    })
    await expect(
      harness.call("rig_loop", {
        action: "append",
        reviewer: "rig-challenger",
        findings: [{ signature: "src/b.ts:other", severity: "low", status: "open" }],
      }),
    ).rejects.toThrow(/must address prior signatures/)
  })

  it("stops on a plateau and blocks completion", async () => {
    const { harness } = await challengeHarness()
    await harness.call("rig_loop", {
      action: "start",
      reviewer: "rig-challenger",
      findings: [{ ...high, status: "open" }],
    })
    const plateau = await harness.call("rig_loop", {
      action: "append",
      reviewer: "rig-challenger",
      findings: [
        { ...high, status: "fixed" },
        { signature: "src/b.ts:other", severity: "high", status: "open" },
      ],
    })
    expect(plateau.content).toMatch(/plateau/)
    await expect(
      harness.call("rig_loop", { action: "finalize", reviewer: "rig-challenger", stop_reason: "converged" }),
    ).rejects.toThrow(/cannot record convergence/)
    const stopped = await harness.call("rig_loop", {
      action: "finalize",
      reviewer: "rig-challenger",
      stop_reason: "plateau",
    })
    expect(stopped.metadata.stop_reason).toBe("plateau")
  })

  it("refuses convergence on a baseline alone", async () => {
    const { harness } = await challengeHarness()
    await harness.call("rig_loop", {
      action: "start",
      reviewer: "rig-challenger",
      findings: [{ ...high, status: "open" }],
    })
    await expect(
      harness.call("rig_loop", { action: "finalize", reviewer: "rig-challenger", stop_reason: "converged" }),
    ).rejects.toThrow(/baseline only/)
  })

  it("refuses to finalize with open security residue", async () => {
    const { harness } = await challengeHarness()
    await harness.call("rig_loop", {
      action: "start",
      reviewer: "rig-challenger",
      findings: [{ signature: "src/a.ts:leak", severity: "security", status: "open" }],
    })
    await harness.call("rig_loop", {
      action: "append",
      reviewer: "rig-challenger",
      findings: [{ signature: "src/a.ts:leak", severity: "security", status: "open" }],
    })
    await expect(
      harness.call("rig_loop", { action: "finalize", reviewer: "rig-challenger", stop_reason: "plateau" }),
    ).rejects.toThrow(/cannot finalize with 1 security and 0 critical/)
  })

  it("requires a reviewer and operates only on a challenge run", async () => {
    const { harness } = await challengeHarness()
    await expect(
      harness.call("rig_loop", { action: "start", findings: [{ ...high, status: "open" }] }),
    ).rejects.toThrow(/requires reviewer/)

    const project = changedProject()
    const other = await createHarness(project)
    await other.call("rig_evidence", { action: "start", skill: "rig-investigate", paths: ["src"] })
    await expect(other.call("rig_loop", { action: "read", reviewer: "rig-challenger" })).rejects.toThrow(
      /only operates on a rig-challenge run/,
    )
  })

  it("prints cumulative progress", async () => {
    const { harness } = await challengeHarness()
    await harness.call("rig_loop", {
      action: "start",
      reviewer: "rig-challenger",
      findings: [{ ...high, status: "open" }],
    })
    const read = await harness.call("rig_loop", { action: "read", reviewer: "rig-challenger" })
    expect(read.content).toContain("| Iteration | Security | Critical | High |")
    expect(read.content).toContain("| 1 | 0 + 0 | 0 + 0 | 0 + 1 |")
  })
})

describe("rig_git_grant", () => {
  it("records and revokes a grant only after explicit confirmation", async () => {
    const project = changedProject()
    const harness = await createHarness(project)

    const absent = JSON.parse((await harness.call("rig_git_grant", { action: "status" })).content)
    expect(absent.status).toBe("absent")

    await expect(harness.call("rig_git_grant", { action: "approve" })).rejects.toThrow(/requires confirm/)
    await expect(harness.call("rig_git_grant", { action: "approve", confirm: "yes, commit locally" })).rejects.toThrow(
      /scope_acknowledged=true/,
    )

    const approved = await harness.call("rig_git_grant", {
      action: "approve",
      confirm: "yes, allow plain local commits",
      scope_acknowledged: true,
    })
    expect(approved.metadata.status).toBe("granted")
    expect(harness.assertions.at(-1)).toMatchObject({ action: "rig_git_grant" })

    const status = JSON.parse((await harness.call("rig_git_grant", { action: "status" })).content)
    expect(status.status).toBe("granted")

    const revoked = await harness.call("rig_git_grant", { action: "revoke" })
    expect(revoked.metadata.status).toBe("absent")
    expect(JSON.parse((await harness.call("rig_git_grant", { action: "status" })).content).status).toBe("absent")
  })
})

describe("rig_dispatch", () => {
  it("rejects write scope for a read-only role", async () => {
    const harness = await createHarness(changedProject())
    await expect(
      harness.call("rig_dispatch", { role: "rig-reviewer", task: "review", read_only: false, paths: ["src"] }),
    ).rejects.toThrow(/role rig-reviewer is read-only/)
  })

  it("requires owned paths for a writing child", async () => {
    const harness = await createHarness(changedProject())
    await expect(
      harness.call("rig_dispatch", { role: "rig-implementer", task: "implement", read_only: false }),
    ).rejects.toThrow(/needs explicit owned paths/)
  })

  it("injects the role card, returns the final text, and removes the child session", async () => {
    const harness = await createHarness(changedProject())
    const result = await harness.call("rig_dispatch", {
      role: "rig-reviewer",
      task: "Review src/a.ts",
      paths: ["src"],
    })
    expect(result.content).toContain("role: rig-reviewer")
    expect(result.content).toContain("the child reports a concrete result")
    expect(harness.childPrompts[0]?.text).toMatch(/You are an independent reviewer/)
    expect(harness.childPrompts[0]?.text).toMatch(/Read-only: do not edit files/)
    expect(harness.removedSessions).toEqual(["child-1"])
  })

  it("keeps the child session when asked", async () => {
    const harness = await createHarness(changedProject())
    await harness.call("rig_dispatch", { role: "rig-investigator", task: "Inspect", keep_session: true })
    expect(harness.createdSessions).toEqual(["child-1"])
    expect(harness.removedSessions).toEqual([])
  })

  it("fails when the child returns no final text", async () => {
    const harness = await createHarness(changedProject())
    harness.childReply = ""
    await expect(harness.call("rig_dispatch", { role: "rig-investigator", task: "Inspect" })).rejects.toThrow(
      /no final assistant text/,
    )
    expect(harness.removedSessions).toEqual(["child-1"])
  })
})

describe("rig_doctor", () => {
  it("reports a healthy install and every checked surface", async () => {
    const project = changedProject()
    const harness = await createHarness(project)
    await harness.call("rig_evidence", { action: "start", skill: "rig-doctor", paths: ["src"] })
    const report = await harness.call("rig_doctor", { action: "report" })
    expect(report.metadata.ok).toBe(true)
    expect(report.content).toContain("plugin loaded")
    expect(report.content).toContain("version sources agree")
    expect(report.content).toContain("local Git grant")
    expect(report.content).toContain("artifact root is gitignored")
  })

  it("requires confirmation before removing stale runs", async () => {
    const project = changedProject()
    const harness = await createHarness(project)
    const started = await harness.call("rig_evidence", { action: "start", skill: "rig-investigate", paths: ["src"] })
    const directory = join(project.root, ".opencode-rig", "runs", "rig-investigate", started.metadata.run_id as string)
    const old = new Date(Date.now() - 400 * 24 * 60 * 60 * 1000)
    utimesSync(directory, old, old)

    await expect(harness.call("rig_doctor", { action: "clean" })).rejects.toThrow(/confirm=true/)
    const cleaned = await harness.call("rig_doctor", { action: "clean", confirm: true })
    expect(cleaned.metadata.removed).toBe(1)
    expect(existsSync(directory)).toBe(false)
    expect(harness.assertions.at(-1)?.action).toBe("rig_doctor_clean")
  })
})
