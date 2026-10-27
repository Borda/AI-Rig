import { describe, expect, it } from "vitest"
import {
  bucketCounts,
  mergeFindings,
  openBlocking,
  validateFinding,
  validateFindings,
  type Finding,
} from "../../src/findings.ts"
import { ValidationError } from "../../src/util/schema.ts"

function finding(overrides: Partial<Finding> = {}): Finding {
  return {
    id: "F-001",
    severity: "high",
    dimension: "correctness",
    file: "src/a.ts",
    title: "Drops the last record",
    evidence: "Reading `run.jsonl` with split('\\n') loses a trailing record.",
    status: "open",
    ...overrides,
  }
}

describe("validateFinding", () => {
  it("accepts a complete finding", () => {
    expect(validateFinding(finding())).toEqual(finding())
  })

  it("accepts every documented severity and dimension", () => {
    for (const severity of ["security", "critical", "high", "medium", "low", "nit"] as const) {
      expect(validateFinding(finding({ severity })).severity).toBe(severity)
    }
    for (const dimension of ["correctness", "security", "tests", "api", "performance", "docs", "data"] as const) {
      expect(validateFinding(finding({ dimension })).dimension).toBe(dimension)
    }
  })

  it("rejects a malformed id", () => {
    expect(() => validateFinding(finding({ id: "finding-1" }))).toThrow(ValidationError)
  })

  it("rejects empty evidence", () => {
    expect(() => validateFinding(finding({ evidence: "   " }))).toThrow(/evidence/)
  })

  it("rejects an unknown severity and status", () => {
    expect(() => validateFinding({ ...finding(), severity: "blocker" })).toThrow(/severity/)
    expect(() => validateFinding({ ...finding(), status: "wontfix" })).toThrow(/status/)
  })

  it("rejects a non-positive line number", () => {
    expect(() => validateFinding(finding({ line: 0 }))).toThrow(/positive integer/)
    expect(() => validateFinding(finding({ line: 1.5 }))).toThrow(/positive integer/)
  })

  it("drops blank optional fields", () => {
    const validated = validateFinding(finding({ disposition: "  " }))
    expect(validated.disposition).toBeUndefined()
  })

  it("rejects non-objects", () => {
    expect(() => validateFinding(null)).toThrow(ValidationError)
    expect(() => validateFinding("F-001")).toThrow(ValidationError)
  })
})

describe("validateFindings", () => {
  it("rejects duplicate ids", () => {
    expect(() => validateFindings([finding(), finding()])).toThrow(/duplicate finding id: F-001/)
  })

  it("rejects a non-array payload", () => {
    expect(() => validateFindings({ findings: [] })).toThrow(/must be an array/)
  })

  it("accepts an empty inventory", () => {
    expect(validateFindings([])).toEqual([])
  })
})

describe("bucketCounts", () => {
  it("counts every terminal bucket", () => {
    const counts = bucketCounts([
      finding({ id: "F-001", status: "open" }),
      finding({ id: "F-002", status: "fixed" }),
      finding({ id: "F-003", status: "rejected" }),
      finding({ id: "F-004", status: "deferred" }),
      finding({ id: "F-005", status: "accepted-risk" }),
      finding({ id: "F-006", status: "fixed" }),
    ])
    expect(counts).toEqual({ open: 1, fixed: 2, rejected: 1, deferred: 1, accepted_risk: 1 })
  })

  it("reconciles the total", () => {
    const inventory = [
      finding({ id: "F-001" }),
      finding({ id: "F-002", status: "fixed" }),
      finding({ id: "F-003", status: "deferred" }),
    ]
    const counts = bucketCounts(inventory)
    expect(Object.values(counts).reduce((sum, value) => sum + value, 0)).toBe(inventory.length)
  })
})

describe("mergeFindings", () => {
  it("replaces a finding by id and keeps the rest", () => {
    const merged = mergeFindings(
      [finding({ id: "F-001" }), finding({ id: "F-002", status: "open" })],
      [finding({ id: "F-002", status: "fixed", disposition: "Guarded the empty split." })],
    )
    expect(merged.map((entry) => entry.id)).toEqual(["F-001", "F-002"])
    expect(merged[1]?.status).toBe("fixed")
    expect(merged[1]?.disposition).toBe("Guarded the empty split.")
  })

  it("sorts by id for a stable artifact", () => {
    const merged = mergeFindings([], [finding({ id: "F-010" }), finding({ id: "F-002" })])
    expect(merged.map((entry) => entry.id)).toEqual(["F-002", "F-010"])
  })
})

describe("openBlocking", () => {
  it("returns only open security and critical findings", () => {
    const blocking = openBlocking([
      finding({ id: "F-001", severity: "security" }),
      finding({ id: "F-002", severity: "critical" }),
      finding({ id: "F-003", severity: "high" }),
      finding({ id: "F-004", severity: "security", status: "fixed" }),
    ])
    expect(blocking.map((entry) => entry.id)).toEqual(["F-001", "F-002"])
  })
})
