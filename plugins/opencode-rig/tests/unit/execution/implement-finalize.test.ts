/**
 * A frozen-plan implementation completes only with a challenge bound to the
 * final bytes, every finding resolved, and a converged loop whenever the
 * challenge found a security, critical or high issue. A trivial mini-plan keeps
 * the light path but stops once it outgrows its label.
 */
import { describe, expect, it } from "vitest"
import { collectScope } from "../../../src/evidence.ts"
import type { Finding } from "../../../src/findings.ts"
import { resolvePaths } from "../../../src/paths.ts"
import { createRun, finalizeRun, requirementsForRun, type LoopSummary, type RunRecord } from "../../../src/run.ts"
import { Project } from "../../helpers/harness.ts"

function frozenRun(project: Project): RunRecord {
  const paths = resolvePaths(project.root, ".opencode-rig")
  project.write("src/a.ts", "export const a = 2\n")
  const scope = collectScope(project.root, ["src"])
  const run = createRun(paths, {
    skill: "rig-implement",
    scope,
    plan: { path: "plan.md", digest: "a".repeat(64), run_id: "run-plan" },
  })
  return {
    ...run,
    gate_exemption: "No configured gate; the focused check is recorded manually.",
    review: { run_id: run.run_id, reviewer: "rig-challenger", source_digest: scope.digest },
    findings: {
      path: "findings.json",
      digest: "b".repeat(64),
      open: 0,
      fixed: 0,
      rejected: 0,
      deferred: 0,
      accepted_risk: 0,
    },
  }
}

function finding(overrides: Partial<Finding> = {}): Finding {
  return {
    id: "F-001",
    severity: "high",
    dimension: "correctness",
    file: "src/a.ts",
    title: "Wrong value",
    evidence: "The return value contradicts the test",
    status: "fixed",
    disposition: "Corrected and rechecked",
    ...overrides,
  }
}

const converged: LoopSummary = {
  path: "loop.jsonl",
  rounds: 2,
  weighted: 0,
  stop_reason: "converged",
  open_security: 0,
  open_critical: 0,
}

function finalize(run: RunRecord, extra: Partial<Parameters<typeof finalizeRun>[1]> = {}) {
  return finalizeRun(run, { drift: false, plan_digest: run.plan?.digest, ...extra })
}

describe("frozen-plan implementation finalize", () => {
  it("takes the full contract and a trivial mini-plan does not", () => {
    const project = Project.withCommit()
    const run = frozenRun(project)
    expect(requirementsForRun(run)).toMatchObject({ review: true, findings: true, closedFindings: true })
    expect(requirementsForRun({ ...run, plan: undefined, trivial_plan: "{}" })).toMatchObject({
      review: false,
      findings: false,
      loop: "off",
    })
  })

  it("requires the final-challenge findings artifact", () => {
    const project = Project.withCommit()
    const run = frozenRun(project)
    expect(finalize({ ...run, findings: undefined }).problems).toContain(
      "findings artifact is missing; write it with rig_findings",
    )
  })

  it("binds the challenge to the final bytes", () => {
    const project = Project.withCommit()
    const run = frozenRun(project)
    const stale = finalize({ ...run, review: { ...run.review!, source_digest: "f".repeat(64) } })
    expect(stale.ok).toBe(false)
    expect(stale.problems).toContain(
      "the final challenge does not match the current scope; re-challenge the changed bytes and record it",
    )
  })

  it("refuses to complete with an unresolved finding", () => {
    const project = Project.withCommit()
    const run = frozenRun(project)
    const open = finalize({ ...run, findings: { ...run.findings!, open: 2 } })
    expect(open.ok).toBe(false)
    expect(open.problems).toContain("the final challenge still has 2 unresolved finding(s); fix or disprove each one")
  })

  it("requires the recorded loop for a security, critical or high finding", () => {
    const project = Project.withCommit()
    const run = frozenRun(project)
    const result = finalize(run, { findings: [finding()] })
    expect(result.ok).toBe(false)
    expect(result.problems.join("\n")).toMatch(/security, critical or high finding .*rig_loop/)
  })

  it("accepts a disproved high finding without a loop", () => {
    const project = Project.withCommit()
    const run = frozenRun(project)
    const result = finalize(run, {
      findings: [finding({ status: "rejected", disposition: "Source proves the review read the wrong branch" })],
    })
    expect(result.problems).toEqual([])
  })

  it("completes once the loop converged and assessed the finding", () => {
    const project = Project.withCommit()
    const run = frozenRun(project)
    const result = finalize({ ...run, loop: converged }, { findings: [finding()], loop_signatures: ["F-001"] })
    expect(result).toEqual({ ok: true, problems: [], status: "completed" })
  })

  it("rejects a loop that never assessed the reported finding", () => {
    const project = Project.withCommit()
    const run = frozenRun(project)
    const result = finalize({ ...run, loop: converged }, { findings: [finding()], loop_signatures: [] })
    expect(result.ok).toBe(false)
    expect(result.problems).toContain("the challenge loop did not assess F-001; append a round with that signature")
  })

  it("blocks when the loop stopped without convergence", () => {
    const project = Project.withCommit()
    const run = frozenRun(project)
    const result = finalize(
      { ...run, loop: { ...converged, stop_reason: "round-limit", open_security: 1 } },
      { findings: [finding({ severity: "security" })] },
    )
    expect(result.ok).toBe(false)
    expect(result.status).toBe("blocked")
  })
})
