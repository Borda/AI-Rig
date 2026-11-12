/** Asset health rejects routing descriptions that diverge from the loaded card. */
import { join } from "node:path"
import { describe, expect, it } from "vitest"
import { checkAssets } from "../../../src/doctor.ts"
import { SKILLS } from "../../../src/skill-manifest.ts"
import { Project } from "../../helpers/harness.ts"

function resources(project: Project, description: string): string {
  project.write("pkg/rig.json", JSON.stringify({ skills: SKILLS.map((skill) => skill.id), agents: ["rig-reviewer"] }))
  project.write("pkg/src/resources/agents.json", JSON.stringify({ agents: { "rig-reviewer": {} } }))
  project.write("pkg/src/resources/roles/rig-reviewer.md", "Read the assigned source.\n")
  for (const skill of SKILLS) {
    const text = skill.id === "rig-review" ? description : skill.description
    project.write(
      `pkg/src/resources/skills/${skill.id}/SKILL.md`,
      `---\nname: ${skill.name}\ndescription: ${text}\n---\n\n# ${skill.name}\n`,
    )
  }
  return join(project.root, "pkg", "src", "resources")
}

describe("routing description health", () => {
  it("fails when the loaded card advertises a different routing contract", () => {
    const root = resources(new Project(), "Review and silently apply fixes.")
    expect(checkAssets(root).failures).toContain(
      "assets: rig-review/SKILL.md description differs from the skill manifest",
    )
  })

  it("accepts the manifest description verbatim", () => {
    const definition = SKILLS.find((skill) => skill.id === "rig-review")!
    const root = resources(new Project(), definition.description)
    expect(checkAssets(root).failures).toEqual([])
  })
})
