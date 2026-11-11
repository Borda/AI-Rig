/** Skills carry their reference rules and doctor searches both installation scopes. */
import { readFileSync, mkdirSync, writeFileSync } from "node:fs"
import { join } from "node:path"
import { afterEach, describe, expect, it, vi } from "vitest"
import { AGENT_NAMES } from "../../src/doctor.ts"
import { sha256 } from "../../src/digest.ts"
import { PLUGIN_VERSION } from "../../src/version.ts"
import { createHarness, Project, RESOURCES_DIR } from "../helpers/harness.ts"

afterEach(() => vi.unstubAllEnvs())

describe("reference loading", () => {
  it("includes every linked reference in the registered skill body", async () => {
    const harness = await createHarness(new Project())
    for (const skill of harness.skills) {
      const source = readFileSync(skill.path, "utf8")
      for (const match of source.matchAll(/\]\(\.\.\/\.\.\/references\/([a-z0-9-]+\.md)\)/g)) {
        const reference = readFileSync(join(RESOURCES_DIR, "references", match[1]!), "utf8").trim()
        expect(skill.content).toContain(reference)
        expect(skill.content.split(reference)).toHaveLength(2)
      }
    }
  })
})

describe("agent installation discovery", () => {
  it.each(["global", "project"])("finds a complete current %s roster despite an unrelated directory", async (scope) => {
    const project = new Project()
    const config = new Project()
    vi.stubEnv("OPENCODE_CONFIG_DIR", config.root)
    const projectAgents = project.path(".opencode/agents")
    const globalAgents = config.path("agents")
    mkdirSync(projectAgents, { recursive: true })
    mkdirSync(globalAgents, { recursive: true })
    const installed = scope === "global" ? globalAgents : projectAgents
    const unrelated = scope === "global" ? projectAgents : globalAgents
    writeFileSync(join(unrelated, "custom.md"), "My own agent\n")
    for (const name of AGENT_NAMES) {
      const body = `# ${name}\n\nInstalled role.\n`
      writeFileSync(
        join(installed, `${name}.md`),
        `<!-- opencode-rig: v${PLUGIN_VERSION} sha256:${sha256(body)} -->\n${body}`,
      )
    }
    const harness = await createHarness(project)
    const result = await harness.call("rig_doctor", { action: "report" })
    expect(result.content).toContain(`PASS  agents installed in ${installed}`)
    expect(result.content).not.toMatch(/agents: rig-[a-z]+ is missing/)
  })
})
