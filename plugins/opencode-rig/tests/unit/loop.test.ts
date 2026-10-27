import { describe, expect, it } from "vitest"
import {
  convergence,
  latestPerRound,
  latestRecord,
  progress,
  renderProgressTable,
  score,
  WEIGHTS,
  type LoopFinding,
  type LoopRecord,
} from "../../src/loop.ts"

function record(round: number, findings: readonly LoopFinding[], stop_reason?: LoopRecord["stop_reason"]): LoopRecord {
  return {
    schema: 1,
    run_id: "run-1",
    round,
    reviewer: "rig-challenger",
    source_digest: "a".repeat(64),
    findings,
    weighted: score(findings).weighted,
    ...(stop_reason ? { stop_reason } : {}),
  }
}

const open = (signature: string, severity: LoopFinding["severity"]): LoopFinding => ({
  signature,
  severity,
  status: "open",
})
const fixed = (signature: string, severity: LoopFinding["severity"]): LoopFinding => ({
  signature,
  severity,
  status: "fixed",
})

describe("score", () => {
  it("weights each severity as documented", () => {
    expect(WEIGHTS).toEqual({ security: 20, critical: 10, high: 6, medium: 4, low: 2, nit: 1 })
  })

  it("counts only open findings", () => {
    const residue = score([open("a", "security"), fixed("b", "critical"), open("c", "high")])
    expect(residue).toEqual({ weighted: 26, open: 2, security: 1, critical: 0 })
  })

  it("is zero for a clean inventory", () => {
    expect(score([fixed("a", "security")])).toEqual({ weighted: 0, open: 0, security: 0, critical: 0 })
  })
})

describe("progress", () => {
  it("marks a signature seen earlier as old and a first sighting as new", () => {
    const rows = progress([
      record(1, [open("a.ts:null-deref", "high")]),
      record(2, [open("a.ts:null-deref", "high"), open("b.ts:race", "medium")]),
    ])
    expect(rows).toHaveLength(2)
    expect(rows[0]?.bySeverity.high).toEqual({ old: 0, new: 1 })
    expect(rows[1]?.bySeverity.high).toEqual({ old: 1, new: 0 })
    expect(rows[1]?.bySeverity.medium).toEqual({ old: 0, new: 1 })
    expect(rows[1]?.weighted).toBe(10)
  })

  it("treats a terminal marker as superseding the scored record of the same round", () => {
    const rows = progress([record(1, [open("a", "high")]), record(1, [open("a", "high")], "converged")])
    expect(rows).toHaveLength(1)
  })

  it("renders old + new cells", () => {
    const table = renderProgressTable(progress([record(1, [open("a", "nit")])]))
    expect(table.split("\n")[0]).toContain(
      "| Iteration | Security | Critical | High | Medium | Low | Nits | Weighted score |",
    )
    expect(table.split("\n")[2]).toBe("| 1 | 0 + 0 | 0 + 0 | 0 + 0 | 0 + 0 | 0 + 0 | 0 + 1 | 1 |")
  })

  it("returns an empty table for no rounds", () => {
    expect(renderProgressTable([])).toBe("")
  })
})

describe("latestPerRound / latestRecord", () => {
  it("keeps the last record for each round in round order", () => {
    const records = [record(2, []), record(1, []), record(2, [], "converged")]
    const latest = latestPerRound(records)
    expect(latest.map((entry) => entry.round)).toEqual([1, 2])
    expect(latest[1]?.stop_reason).toBe("converged")
    expect(latestRecord(records)?.round).toBe(2)
  })

  it("returns undefined for an empty ledger", () => {
    expect(latestRecord([])).toBeUndefined()
  })
})

describe("convergence", () => {
  it("requires a validated round", () => {
    const decision = convergence([])
    expect(decision.converged).toBe(false)
    expect(decision.stop).toBe("unavailable-reviewer")
  })

  it("does not converge on a baseline alone", () => {
    const decision = convergence(progress([record(1, [open("a", "high")])]))
    expect(decision.converged).toBe(false)
    expect(decision.stop).toBeUndefined()
  })

  it("converges on a clean round", () => {
    const decision = convergence(progress([record(1, [open("a", "high")]), record(2, [fixed("a", "high")])]))
    expect(decision).toMatchObject({ converged: true, stop: "converged" })
  })

  it("converges on a strict decrease", () => {
    const decision = convergence(
      progress([record(1, [open("a", "critical"), open("b", "high")]), record(2, [open("a", "critical")])]),
    )
    expect(decision.converged).toBe(true)
    expect(decision.reason).toBe("16 -> 10")
  })

  it("stops on a plateau", () => {
    const decision = convergence(progress([record(1, [open("a", "high")]), record(2, [open("b", "high")])]))
    expect(decision).toMatchObject({ converged: false, stop: "plateau" })
  })

  it("stops on an increase", () => {
    const decision = convergence(
      progress([record(1, [open("a", "high")]), record(2, [open("a", "high"), open("b", "critical")])]),
    )
    expect(decision).toMatchObject({ converged: false, stop: "non-converging" })
  })
})
