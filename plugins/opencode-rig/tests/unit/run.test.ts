import { existsSync } from "node:fs"
import { describe, expect, it } from "vitest"
import { collectScope } from "../../src/evidence.ts"
import { resolvePaths } from "../../src/paths.ts"
import { SKILLS } from "../../src/skill-manifest.ts"
import {
  createRun,
  finalizeRun,
  loadRun,
  renderReport,
  requirementsFor,
  saveRun,
  writeReport,
  type GateReceipt,
  type LoopSummary,
  type RunRecord,
  type ScopeRecord,
} from "../../src/run.ts"
import { PLUGIN_VERSION } from "../../src/version.ts"
import { Project } from "../helpers/harness.ts"

function setup(project: Project, skill: string): { run: RunRecord; scope: ScopeRecord } {
  const paths = resolvePaths(project.root, ".opencode-rig")
  project.write("src/a.ts", "export const a = 2\n")
  const scope = collectScope(project.root, ["src"])
  return { run: createRun(paths, { skill, scope }), scope }
}

function receipt(id: string, status: GateReceipt["status"]): GateReceipt {
  return {
    id,
    command: `npm run ${id}`,
    source: "package.json",
    status,
    exit_code: status === "passed" ? 0 : 1,
    duration_ms: 5,
    stdout_sha256: "a".repeat(64),
    stderr_sha256: "b".repeat(64),
    tail: "",
  }
}

const findings = {
  path: "findings.json",
  digest: "c".repeat(64),
  open: 0,
  fixed: 0,
  rejected: 0,
  deferred: 0,
  accepted_risk: 0,
}

const convergedLoop: LoopSummary = {
  path: "loop.jsonl",
  rounds: 2,
  weighted: 0,
  stop_reason: "converged",
  open_security: 0,
  open_critical: 0,
}

describe("createRun", () => {
  it("persists an in-progress record with a stamped plugin version", () => {
    const project = Project.withCommit()
    const { run } = setup(project, "rig-implement")
    const paths = resolvePaths(project.root, ".opencode-rig")
    const loaded = loadRun(paths, "rig-implement", run.run_id)
    expect(loaded).toMatchObject({
      schema: 1,
      run_id: run.run_id,
      skill: "rig-implement",
      status: "in_progress",
      plugin_version: PLUGIN_VERSION,
    })
    expect(loaded.scope.digest).toBe(run.scope.digest)
  })

  it("writes a filesystem-safe, sortable run id", () => {
    const project = Project.withCommit()
    const { run } = setup(project, "rig-implement")
    expect(run.run_id).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z-[0-9a-f]{4}$/)
  })

  it("rejects an invalid skill id", () => {
    const project = Project.withCommit()
    const paths = resolvePaths(project.root, ".opencode-rig")
    const scope = collectScope(project.root, ["src"])
    expect(() => createRun(paths, { skill: "Rig Plan", scope })).toThrow(/invalid skill id/)
  })

  it("saves updates atomically through saveRun", () => {
    const project = Project.withCommit()
    const { run } = setup(project, "rig-investigate")
    const paths = resolvePaths(project.root, ".opencode-rig")
    saveRun(paths, { ...run, limits: ["no network access"] })
    expect(loadRun(paths, "rig-investigate", run.run_id).limits).toEqual(["no network access"])
  })
})

describe("requirementsFor", () => {
  it("defines a contract for every shipped skill", () => {
    expect(SKILLS).toHaveLength(11)
    for (const { id } of SKILLS) {
      expect(requirementsFor(id)).toBeTruthy()
    }
  })

  it("gates optimize on gates and review, and keeps distill advisory", () => {
    expect(requirementsFor("rig-optimize")).toMatchObject({ gates: true, review: true, loop: false })
    expect(requirementsFor("rig-research")).toMatchObject({ gates: false, review: true })
    expect(requirementsFor("rig-distill")).toMatchObject({ gates: false, review: false, findings: false })
  })

  it("rejects an unknown skill", () => {
    expect(() => requirementsFor("rig-deploy")).toThrow(/unknown skill/)
  })
})

describe("finalizeRun", () => {
  it("refuses to finalize a completed run again", () => {
    const project = Project.withCommit()
    const { run } = setup(project, "rig-doctor")
    const result = finalizeRun({ ...run, status: "completed" }, { drift: false })
    expect(result).toMatchObject({ ok: false, status: "completed" })
    expect(result.problems[0]).toMatch(/not 'in_progress'/)
  })

  it("marks drift as stale and never certifies changed bytes", () => {
    const project = Project.withCommit()
    const { run } = setup(project, "rig-doctor")
    const result = finalizeRun(run, { drift: true })
    expect(result).toMatchObject({ ok: false, status: "stale" })
    expect(result.problems[0]).toMatch(/source drift/)
  })

  it("completes a read-only investigation without gates", () => {
    const project = Project.withCommit()
    const { run } = setup(project, "rig-investigate")
    expect(finalizeRun(run, { drift: false })).toEqual({ ok: true, problems: [], status: "completed" })
  })

  it("requires a passing gate for implementation", () => {
    const project = Project.withCommit()
    const { run } = setup(project, "rig-implement")
    const missing = finalizeRun(run, { drift: false })
    expect(missing.ok).toBe(false)
    expect(missing.status).toBe("in_progress")
    expect(missing.problems).toContain("no passing gate receipt; run rig_gate or record gate_exemption")
  })

  it("accepts a recorded gate exemption when nothing is configured", () => {
    const project = Project.withCommit()
    const { run } = setup(project, "rig-implement")
    const result = finalizeRun(
      { ...run, gate_exemption: "No check exists in this documentation-only project." },
      { drift: false },
    )
    expect(result.ok).toBe(true)
  })

  it("reports a failed gate by id", () => {
    const project = Project.withCommit()
    const { run } = setup(project, "rig-implement")
    const result = finalizeRun({ ...run, gates: [receipt("test", "failed")] }, { drift: false })
    expect(result.ok).toBe(false)
    expect(result.problems).toContain("failed gates: test")
  })

  it("completes implementation with a passing gate", () => {
    const project = Project.withCommit()
    const { run } = setup(project, "rig-implement")
    expect(finalizeRun({ ...run, gates: [receipt("test", "passed")] }, { drift: false }).ok).toBe(true)
  })

  it("requires independent review evidence", () => {
    const project = Project.withCommit()
    const { run } = setup(project, "rig-plan")
    expect(finalizeRun(run, { drift: false }).problems).toContain("independent review evidence is missing")
  })

  it("binds review evidence to the current scope digest", () => {
    const project = Project.withCommit()
    const { run } = setup(project, "rig-plan")
    const stale = finalizeRun(
      { ...run, review: { run_id: run.run_id, reviewer: "rig-challenger", source_digest: "f".repeat(64) } },
      { drift: false },
    )
    expect(stale.ok).toBe(false)
    expect(stale.problems).toContain("review evidence does not match the current scope digest")
  })

  it("requires a findings artifact from review", () => {
    const project = Project.withCommit()
    const { run } = setup(project, "rig-review")
    const reviewed = {
      ...run,
      review: { run_id: run.run_id, reviewer: "rig-reviewer", source_digest: run.scope.digest },
    }
    expect(finalizeRun(reviewed, { drift: false }).problems).toContain(
      "findings artifact is missing; write it with rig_findings",
    )
  })

  it("lets a review finish with open findings", () => {
    const project = Project.withCommit()
    const { run } = setup(project, "rig-review")
    const result = finalizeRun(
      {
        ...run,
        review: { run_id: run.run_id, reviewer: "rig-reviewer", source_digest: run.scope.digest },
        findings: { ...findings, open: 2 },
      },
      { drift: false },
    )
    expect(result.ok).toBe(true)
  })

  it("blocks remediation while a finding is open", () => {
    const project = Project.withCommit()
    const { run } = setup(project, "rig-remediate")
    const result = finalizeRun(
      {
        ...run,
        gates: [receipt("test", "passed")],
        review: { run_id: run.run_id, reviewer: "rig-reviewer", source_digest: run.scope.digest },
        findings: { ...findings, open: 1 },
      },
      { drift: false },
    )
    expect(result.ok).toBe(false)
    expect(result.problems).toContain("remediation still has 1 open finding(s)")
  })

  it("completes remediation with every finding closed", () => {
    const project = Project.withCommit()
    const { run } = setup(project, "rig-remediate")
    const result = finalizeRun(
      {
        ...run,
        gates: [receipt("test", "passed")],
        review: { run_id: run.run_id, reviewer: "rig-reviewer", source_digest: run.scope.digest },
        findings: { ...findings, fixed: 3 },
      },
      { drift: false },
    )
    expect(result.ok).toBe(true)
  })

  it("blocks a challenge run without a loop ledger", () => {
    const project = Project.withCommit()
    const { run } = setup(project, "rig-challenge")
    const result = finalizeRun(
      {
        ...run,
        gates: [receipt("test", "passed")],
        review: { run_id: run.run_id, reviewer: "rig-challenger", source_digest: run.scope.digest },
      },
      { drift: false },
    )
    expect(result).toMatchObject({ ok: false, status: "blocked" })
    expect(result.problems).toContain("challenge loop ledger is missing; use rig_loop")
  })

  it("blocks a non-converged challenge loop", () => {
    const project = Project.withCommit()
    const { run } = setup(project, "rig-challenge")
    const result = finalizeRun(
      {
        ...run,
        gates: [receipt("test", "passed")],
        review: { run_id: run.run_id, reviewer: "rig-challenger", source_digest: run.scope.digest },
        loop: { ...convergedLoop, stop_reason: "plateau" },
      },
      { drift: false },
    )
    expect(result.status).toBe("blocked")
    expect(result.problems).toContain("challenge loop stopped with 'plateau', not convergence")
  })

  it("blocks a converged loop that retains security residue", () => {
    const project = Project.withCommit()
    const { run } = setup(project, "rig-challenge")
    const result = finalizeRun(
      {
        ...run,
        gates: [receipt("test", "passed")],
        review: { run_id: run.run_id, reviewer: "rig-challenger", source_digest: run.scope.digest },
        loop: { ...convergedLoop, open_security: 1, weighted: 20 },
      },
      { drift: false },
    )
    expect(result.status).toBe("blocked")
    expect(result.problems.join("\n")).toMatch(/retains 1 security and 0 critical/)
  })

  it("completes a converged challenge with no residue", () => {
    const project = Project.withCommit()
    const { run } = setup(project, "rig-challenge")
    const result = finalizeRun(
      {
        ...run,
        gates: [receipt("test", "passed")],
        review: { run_id: run.run_id, reviewer: "rig-challenger", source_digest: run.scope.digest },
        loop: convergedLoop,
      },
      { drift: false },
    )
    expect(result.ok).toBe(true)
  })
})

describe("writeReport / renderReport", () => {
  it("writes a report with gates, findings, review, loop, changes, and limits", () => {
    const project = Project.withCommit()
    const { run } = setup(project, "rig-remediate")
    const paths = resolvePaths(project.root, ".opencode-rig")
    const rendered = renderReport({
      ...run,
      gates: [receipt("test", "passed")],
      findings,
      review: { run_id: run.run_id, reviewer: "rig-reviewer", source_digest: run.scope.digest },
      loop: convergedLoop,
      outputs: [{ path: "src/a.ts", sha256: "d".repeat(64) }],
      limits: ["Windows not exercised"],
      confidence: 0.8,
    })

    expect(rendered).toContain("| `test` | passed | 0 | `npm run test` |")
    expect(rendered).toContain("| 0 | 0 | 0 | 0 | 0 |")
    expect(rendered).toContain("Reviewer: `rig-reviewer`")
    expect(rendered).toContain("Stop reason: `converged`")
    expect(rendered).toContain("- `src/a.ts` — `" + "d".repeat(64) + "`")
    expect(rendered).toContain("- Windows not exercised")
    expect(rendered).toContain("Confidence: 0.80")

    const path = writeReport(paths, run)
    expect(existsSync(path)).toBe(true)
  })

  it("states missing evidence explicitly instead of leaving blanks", () => {
    const project = Project.withCommit()
    const { run } = setup(project, "rig-doctor")
    const rendered = renderReport(run)
    expect(rendered).toContain("No findings artifact.")
    expect(rendered).toContain("- none recorded")
    expect(rendered).toContain("- no challenge loop")
  })
})
