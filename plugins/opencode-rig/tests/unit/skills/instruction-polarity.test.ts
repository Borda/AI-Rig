import { describe, expect, it } from "vitest"
import { affirmedSentence, ruleSentence } from "../../helpers/instructions.ts"

describe("instruction sentence polarity", () => {
  it("returns the sentence that states the rule, past a lettered list marker", () => {
    const line = "   - c. Check the ref first. Record the limit in `decision` with its reason."
    expect(affirmedSentence(line, "Record the limit")).toBe("Record the limit in `decision` with its reason.")
  })

  it.each([
    ["an obligation gains a negation", "Record the limit and never the reason.", "Record the limit", []],
    ["a prohibition loses its negation", "Finish waiting for checks.", "Finish", ["without"]],
    ["a prohibition gains a second negation", "Never write to GitHub, not even once.", "Never write", ["never"]],
    ["a contraction inverts an obligation", "Report it; don't skip the check.", "Report it", []],
    ["the stated rule is missing", "Record the limit.", "Report the limit", []],
  ])("fails when %s", (_case, line, opening, negators) => {
    expect(() => ruleSentence(line, opening, negators)).toThrow()
  })

  it("pins the exact negations of a prohibition", () => {
    expect(ruleSentence("Finish without waiting or polling.", "Finish", ["without"])).toBe(
      "Finish without waiting or polling.",
    )
  })

  it("ignores negating words inside inline code, which are literal flags or tokens", () => {
    const line = "Fetch with `git fetch --no-tags` (no `+`). A `not-closed` verdict stays `open`."
    ruleSentence(line, "Fetch with", ["no"])
    affirmedSentence(line, "A `not-closed` verdict stays `open`")
  })
})
