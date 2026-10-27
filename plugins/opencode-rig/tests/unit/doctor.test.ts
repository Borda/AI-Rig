import { join } from "node:path"
import { describe, expect, it } from "vitest"
import { checkAssets } from "../../src/doctor.ts"
import { Project, RESOURCES_DIR } from "../helpers/harness.ts"

const LONG_SHARED_LINE =
  "- Do not present a reproduced result as a general guarantee when the environment differs from the reported one."

function fixture(project: Project, skills: Record<string, string>): string {
  project.write("pkg/rig.json", JSON.stringify({ schema: 1, skills: Object.keys(skills), agents: ["rig-demo"] }))
  project.write("pkg/src/resources/agents.json", JSON.stringify({ schema: 1, agents: { "rig-demo": {} } }))
  project.write("pkg/src/resources/roles/rig-demo.md", "You are a demo role. Never edit files.\n")
  for (const [skill, body] of Object.entries(skills)) {
    project.write(`pkg/src/resources/skills/${skill}/SKILL.md`, body)
  }
  return join(project.root, "pkg", "src", "resources")
}

describe("checkAssets", () => {
  it("passes the shipped package with no failures", () => {
    const result = checkAssets(RESOURCES_DIR)
    expect(result.failures).toEqual([])
    expect(result.summary).toMatch(/^assets: 11 skills, 7 role agents, \d+ repeated prose line\(s\)$/)
  })

  it("reports a card that is missing from the skill manifest and has a dead cross-reference", () => {
    const project = new Project()
    const resources = fixture(project, {
      "rig-demo":
        "---\nname: Rig Demo\ndescription: demo\n---\n\n# Rig Demo\n\nSee [missing](../../references/nope.md).\n\nNever assert an unmeasured result.\n",
    })

    const result = checkAssets(resources)
    expect(result.failures).toContain("assets: rig-demo is missing from the skill manifest")
    expect(result.failures).toContain("assets: rig-demo/SKILL.md references missing ../../references/nope.md")
  })

  it("fails roster drift between rig.json and shipped cards", () => {
    const project = new Project()
    const resources = fixture(project, {
      "rig-demo": "---\nname: Rig Demo\ndescription: demo\n---\n\n# Rig Demo\n\nNever assert an unmeasured result.\n",
    })
    project.write(
      "pkg/rig.json",
      JSON.stringify({ schema: 1, skills: ["rig-demo", "rig-ghost"], agents: ["rig-demo"] }),
    )

    expect(checkAssets(resources).failures).toContain("assets: skills do not match shipped files (missing rig-ghost)")
  })

  it("warns about duplicated prose and an oversized role card", () => {
    const project = new Project()
    const resources = fixture(project, {
      "rig-alpha": `---\nname: Rig Alpha\ndescription: alpha\n---\n\n# Rig Alpha\n\n${LONG_SHARED_LINE}\n`,
      "rig-beta": `---\nname: Rig Beta\ndescription: beta\n---\n\n# Rig Beta\n\n${LONG_SHARED_LINE}\n`,
    })
    project.write(
      "pkg/src/resources/roles/rig-demo.md",
      Array.from({ length: 90 }, (_, index) => `- rule ${index}`).join("\n"),
    )

    const result = checkAssets(resources)
    expect(result.warnings.some((warning) => warning.startsWith("assets: repeated prose in rig-alpha, rig-beta"))).toBe(
      true,
    )
    expect(result.warnings).toContain("assets: roles/rig-demo.md has 90 lines (soft limit 80)")
  })
})
