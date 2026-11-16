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
    ["rig-review", "Output"],
    ["rig-remediate", "Output"],
    ["rig-challenge", "Output"],
    ["rig-optimize", "Output"],
    ["rig-distill", "Output"],
    ["rig-release", "Output"],
    ["rig-doctor", "Response"],
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
    for (const verdict of ["approved", "approved with notes", "changes requested", "investigated", "blocked"]) {
      expect(reference).toContain(`\`${verdict}\``)
    }
    for (const level of ["high", "partial", "off"]) {
      expect(reference).toContain(`\`${level}\``)
    }
  })

  it("requires evidence, a blocking rule, and a stated limit", () => {
    const reference = readFileSync(HANDOFF, "utf8")
    expect(reference).toMatch(/module layout[\s\S]*naming[\s\S]*documentation[\s\S]*tests[\s\S]*dependency policy/)
    expect(reference).toContain("`changes requested` requires at least one blocking finding")
    expect(reference).toMatch(/Confidence reflects the weakest accepted evidence/)
    expect(reference).toContain("traceable to the body")
  })
})
