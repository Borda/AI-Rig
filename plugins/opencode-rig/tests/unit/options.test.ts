import { describe, expect, it } from "vitest"
import { DEFAULT_OPTIONS, OptionError, parseOptions } from "../../src/options.ts"

describe("parseOptions", () => {
  it("returns defaults for missing options", () => {
    expect(parseOptions(undefined)).toEqual(DEFAULT_OPTIONS)
    expect(parseOptions(null)).toEqual(DEFAULT_OPTIONS)
    expect(parseOptions({})).toEqual(DEFAULT_OPTIONS)
  })

  it("accepts every documented option", () => {
    const parsed = parseOptions({
      guardrails: "strict",
      artifacts: ".artifacts",
      gates: ["make check"],
      gateTimeoutMs: 1_000,
      maxParallel: 2,
      maxLoopRounds: 5,
      ttlDays: 7,
      journal: false,
      codemap: "require",
      compactionPreserve: false,
    })
    expect(parsed).toEqual({
      guardrails: "strict",
      artifacts: ".artifacts",
      gates: ["make check"],
      gateTimeoutMs: 1_000,
      maxParallel: 2,
      maxLoopRounds: 5,
      ttlDays: 7,
      journal: false,
      codemap: "require",
      compactionPreserve: false,
    })
  })

  it("rejects an unknown key instead of ignoring a typo", () => {
    expect(() => parseOptions({ guardrail: "strict" })).toThrow(OptionError)
    expect(() => parseOptions({ guardrail: "strict" })).toThrow(/unknown option 'guardrail'/)
  })

  it("rejects an invalid enum value", () => {
    expect(() => parseOptions({ guardrails: "lenient" })).toThrow(/must be one of standard, strict, off/)
    expect(() => parseOptions({ codemap: "always" })).toThrow(/must be one of auto, off, require/)
  })

  it("rejects out-of-range numbers", () => {
    expect(() => parseOptions({ maxParallel: 0 })).toThrow(/between 1 and 16/)
    expect(() => parseOptions({ gateTimeoutMs: 10 })).toThrow(/between 1000 and 3600000/)
    expect(() => parseOptions({ maxLoopRounds: 11 })).toThrow(/between 1 and 10/)
    expect(() => parseOptions({ ttlDays: 0 })).toThrow(/between 1 and 3650/)
  })

  it("truncates fractional numeric options", () => {
    expect(parseOptions({ maxParallel: 2.9 }).maxParallel).toBe(2)
    expect(parseOptions({ ttlDays: 14.9 }).ttlDays).toBe(14)
  })

  it("rejects non-string and empty array entries", () => {
    expect(() => parseOptions({ gates: [1] })).toThrow(/array of non-empty strings/)
    expect(() => parseOptions({ gates: [""] })).toThrow(/array of non-empty strings/)
    expect(() => parseOptions({ gates: "make test" })).toThrow(/array of non-empty strings/)
  })

  it("rejects an empty artifact directory and non-object options", () => {
    expect(() => parseOptions({ artifacts: "  " })).toThrow(/non-empty string/)
    expect(() => parseOptions([])).toThrow(/options must be an object/)
    expect(() => parseOptions("strict")).toThrow(/options must be an object/)
  })

  it("rejects a non-boolean flag", () => {
    expect(() => parseOptions({ journal: "yes" })).toThrow(/must be a boolean/)
  })
})
