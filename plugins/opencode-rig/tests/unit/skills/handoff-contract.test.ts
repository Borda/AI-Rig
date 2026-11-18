/** Every workflow hands off with the same decision table, and the shared contract names its values. */
import { readFileSync } from "node:fs"
import { join } from "node:path"
import { describe, expect, it } from "vitest"
import { RESOURCES_DIR } from "../../helpers/harness.ts"

const HANDOFF = join(RESOURCES_DIR, "references", "handoff.md")

/** Read one instruction section without depending on paragraph wrapping. */
function section(skill: string, heading: string): string {
  const lines = readFileSync(join(RESOURCES_DIR, "skills", skill, "SKILL.md"), "utf8").split("\n")
  const start = lines.indexOf(`## ${heading}`)
  expect(start, `${skill}: ${heading}`).toBeGreaterThan(-1)
  const end = lines.findIndex((line, index) => index > start && line.startsWith("## "))
  return lines.slice(start + 1, end < 0 ? undefined : end).join("\n")
}

describe("handoff table", () => {
  const skills: Array<[string, string]> = [
    ["rig-plan", "Output"],
    ["rig-investigate", "Output"],
    ["rig-research", "Output"],
    ["rig-implement", "Output"],
    ["code-review", "Output"],
    ["code-remediate", "Output"],
    ["rig-challenge", "Output"],
    ["rig-optimize", "Output"],
    ["rig-distill", "Output"],
    ["rig-release", "Output"],
    ["rig-health", "Response"],
  ]

  it.each(skills)("%s leads its handoff with the shared table", (skill, heading) => {
    const text = section(skill, heading)
    expect(text).toContain("handoff table")
    expect(text).toContain("../../references/handoff.md")
    expect(text).toMatch(/verdict/i)
    expect(text).toMatch(/alignment/i)
    expect(text).toMatch(/findings/i)
    expect(text).toMatch(/next action/i)
    expect(text).toMatch(/confidence/i)
  })

  it("names every verdict and alignment value the table accepts", () => {
    const reference = readFileSync(HANDOFF, "utf8")
    const verdictRow = reference.split("\n").find((line) => line.startsWith("| Verdict |"))
    for (const verdict of [
      "APPROVE",
      "MINOR_EDITS",
      "MAJOR_REVISION",
      "INSUFFICIENT_BUT_PROMISING",
      "REJECT",
      "null",
      "investigated",
      "blocked",
    ]) {
      expect(verdictRow).toContain(`\`${verdict}\``)
    }
    for (const level of ["high", "partial", "off"]) {
      expect(reference).toContain(`\`${level}\``)
    }
  })

  it("retires the earlier verdict words in favour of the source scale", () => {
    const reference = readFileSync(HANDOFF, "utf8")
    for (const retired of ["approved", "approved with notes", "changes requested"]) {
      expect(reference).not.toContain(`\`${retired}\``)
    }
  })

  it("orders the source scale and keeps run states outside it", () => {
    const lines = readFileSync(HANDOFF, "utf8").split("\n")
    const scale: Array<[string, string, string]> = [
      ["1", "APPROVE", "Approve"],
      ["2", "MINOR_EDITS", "Minor changes"],
      ["3", "MAJOR_REVISION", "Major revision"],
      ["4", "INSUFFICIENT_BUT_PROMISING", "Insufficient, but a good initiative"],
      ["5", "REJECT", "Block"],
      ["none", "null", "No source judgment"],
    ]
    for (const [level, token, label] of scale) {
      expect(
        lines.some((line) => line.startsWith(`| ${level} | \`${token}\` | ${label} |`)),
        token,
      ).toBe(true)
    }
    expect(lines.join("\n")).toContain("`investigated` and `blocked` are run states, not source judgments")
  })

  it("routes both review workflows through the source scale", () => {
    const review = section("code-review", "Output")
    expect(review).toContain("../../references/handoff.md#verdict-scale")
    expect(review).toMatch(/`MINOR_EDITS`: every required correction has bounded impact and a localized remedy/)
    expect(review).toMatch(/`MAJOR_REVISION`: a remaining confirmed defect has material impact/)
    expect(review).toContain("never grounds for `INSUFFICIENT_BUT_PROMISING` or `REJECT`")
    expect(review).toContain("undecidable is `null`")
    const remediate = section("code-remediate", "Output")
    expect(remediate).toContain("../code-review/SKILL.md#output")
  })

  it("requires evidence, a blocking rule, and a stated limit", () => {
    const reference = readFileSync(HANDOFF, "utf8")
    expect(reference).toMatch(/module layout[\s\S]*naming[\s\S]*documentation[\s\S]*tests[\s\S]*dependency policy/)
    expect(reference).toContain("`MAJOR_REVISION` and `REJECT` require at least one blocking finding")
    expect(reference).toContain("`APPROVE` requires the verification behind it")
    expect(reference).toMatch(/Confidence reflects the weakest accepted evidence/)
    expect(reference).toContain("traceable to the body")
  })
})
