import { execFileSync } from "node:child_process"
import { existsSync, readFileSync, readdirSync } from "node:fs"
import { join } from "node:path"
import { describe, expect, it } from "vitest"
import { SKILLS } from "../../src/skill-manifest.ts"
import { PACKAGE_ROOT, RESOURCES_DIR } from "../helpers/harness.ts"

function readJson(relative: string): any {
  return JSON.parse(readFileSync(join(PACKAGE_ROOT, relative), "utf8"))
}

describe("version sources", () => {
  it("agree across package.json, rig.json, version.ts, and the README marker", () => {
    const pkg = readJson("package.json")
    const rig = readJson("rig.json")
    const versionTs = readFileSync(join(PACKAGE_ROOT, "src", "version.ts"), "utf8")
    const readme = readFileSync(join(PACKAGE_ROOT, "README.md"), "utf8")

    expect(pkg.version).toMatch(/^\d+\.\d+\.\d+$/)
    expect(rig.version).toBe(pkg.version)
    expect(versionTs).toContain(`export const PLUGIN_VERSION = "${pkg.version}"`)
    expect(readme).toContain(`Current version: \`${pkg.version}\``)
  })

  it("declares the shipped minimum OpenCode version", () => {
    const rig = readJson("rig.json")
    const versionTs = readFileSync(join(PACKAGE_ROOT, "src", "version.ts"), "utf8")
    expect(versionTs).toContain(`export const MIN_OPENCODE_VERSION = "${rig.opencode.minimum}"`)
  })

  it("passes the sync_version check", () => {
    const output = execFileSync(process.execPath, ["scripts/sync_version.mjs"], {
      cwd: PACKAGE_ROOT,
      encoding: "utf8",
    })
    expect(output).toMatch(/version sources agree/)
  })
})

describe("package manifest", () => {
  const pkg = readJson("package.json")

  it("exposes the TypeScript entrypoint and CLI", () => {
    expect(pkg.type).toBe("module")
    expect(pkg.exports["."]).toBe("./src/index.ts")
    expect(pkg.bin["opencode-rig"]).toBe("./bin/opencode-rig.mjs")
  })

  it("ships sources, resources, and governance files", () => {
    expect(pkg.files).toEqual(
      expect.arrayContaining(["src", "bin", "README.md", "CHANGELOG.md", "LICENSE", "NOTICE", "rig.json"]),
    )
  })

  it("keeps effect as a peer rather than a bundled runtime", () => {
    expect(pkg.peerDependencies?.effect ?? pkg.dependencies?.effect).toBeUndefined()
    expect(pkg.dependencies["@opencode/plugin"]).toMatch(/^\^?2\./)
  })

  it("declares the Node floor the source needs", () => {
    expect(pkg.engines.node).toBe(">=20")
  })
})

describe("rig.json", () => {
  const rig = readJson("rig.json")

  it("lists exactly the registered tools", () => {
    const tools = readFileSync(join(PACKAGE_ROOT, "src", "register", "tools.ts"), "utf8")
    const registered = [...tools.matchAll(/name: "(rig_[a-z_]+)"/g)].map((match) => match[1])
    expect(rig.tools.sort()).toEqual([...new Set(registered)].sort())
    expect(rig.tools).toHaveLength(7)
  })

  it("lists exactly the shipped skills and role agents", () => {
    const skills = readdirSync(join(RESOURCES_DIR, "skills"), { withFileTypes: true })
      .filter((entry) => entry.isDirectory())
      .map((entry) => entry.name)
      .sort()
    expect(rig.skills.sort()).toEqual(skills)

    const agents = Object.keys(readJson("src/resources/agents.json").agents).sort()
    expect(rig.agents.sort()).toEqual(agents)
  })
})

describe("skills", () => {
  const skills = readdirSync(join(RESOURCES_DIR, "skills"), { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .map((entry) => entry.name)
    .sort()

  it("ships eleven skills, each with a SKILL.md", () => {
    expect(skills).toHaveLength(11)
    for (const skill of skills) {
      expect(existsSync(join(RESOURCES_DIR, "skills", skill, "SKILL.md"))).toBe(true)
    }
  })

  it("give every skill a name, description, and matching heading", () => {
    const names = new Map(SKILLS.map((definition) => [definition.id, definition.name]))
    for (const skill of skills) {
      const text = readFileSync(join(RESOURCES_DIR, "skills", skill, "SKILL.md"), "utf8")
      expect(text.startsWith("---\n")).toBe(true)
      const frontmatter = text.slice(4, text.indexOf("\n---", 4))
      expect(frontmatter).toMatch(new RegExp(`^name: ${names.get(skill)}$`, "m"))
      expect(frontmatter).toMatch(/^description: .+/m)
      expect(text).toMatch(new RegExp(`^# ${names.get(skill)}`, "m"))
      expect(text).toMatch(/Do not|Never|do not|never/)
    }
  })

  it("resolves every relative reference link to a shipped file", () => {
    for (const skill of skills) {
      const directory = join(RESOURCES_DIR, "skills", skill)
      const text = readFileSync(join(directory, "SKILL.md"), "utf8")
      for (const match of text.matchAll(/\]\((\.[^)#]+)\)/g)) {
        const target = match[1]
        if (!target) continue
        expect(existsSync(join(directory, target)), `${skill} -> ${target}`).toBe(true)
      }
    }
  })

  it("reference the evidence contract from every workflow that records evidence", () => {
    for (const skill of skills.filter((name) => name !== "rig-doctor")) {
      const text = readFileSync(join(RESOURCES_DIR, "skills", skill, "SKILL.md"), "utf8")
      expect(text).toMatch(/references\/evidence\.md/)
    }
  })
})

describe("role agents", () => {
  const manifest = readJson("src/resources/agents.json")

  it("ships a body for every manifested agent", () => {
    expect(manifest.schema).toBe(1)
    for (const name of Object.keys(manifest.agents)) {
      expect(existsSync(join(RESOURCES_DIR, "roles", `${name}.md`))).toBe(true)
    }
  })

  it("declares subagent frontmatter with valid permissions", () => {
    const actions = new Set(["edit", "read", "shell", "webfetch", "task"])
    const effects = new Set(["allow", "ask", "deny"])
    for (const [name, config] of Object.entries<any>(manifest.agents)) {
      expect(name).toMatch(/^rig-[a-z]+$/)
      expect(config.mode).toBe("subagent")
      expect(config.description.length).toBeGreaterThan(10)
      expect(Array.isArray(config.permissions)).toBe(true)
      for (const permission of config.permissions) {
        expect(actions.has(permission.action)).toBe(true)
        expect(effects.has(permission.effect)).toBe(true)
        expect(typeof permission.resource).toBe("string")
      }
    }
  })

  it("keeps every role read-only except the implementer", () => {
    for (const [name, config] of Object.entries<any>(manifest.agents)) {
      const edit = config.permissions.find((permission: any) => permission.action === "edit")
      expect(edit?.effect, `${name} edit permission`).toBe(name === "rig-implementer" ? "allow" : "deny")
      expect(
        config.permissions.some((permission: any) => permission.action === "task" && permission.effect === "allow"),
      ).toBe(false)
    }
  })

  it("never grants push authority in prose", () => {
    for (const name of Object.keys(manifest.agents)) {
      const body = readFileSync(join(RESOURCES_DIR, "roles", `${name}.md`), "utf8")
      expect(body).not.toMatch(/run `git push`|you may push|push the branch/i)
    }
  })
})

describe("documentation", () => {
  const readme = readFileSync(join(PACKAGE_ROOT, "README.md"), "utf8")
  const changelog = readFileSync(join(PACKAGE_ROOT, "CHANGELOG.md"), "utf8")

  it("documents every skill, tool, and agent in the README", () => {
    for (const skill of readJson("rig.json").skills) expect(readme).toContain(skill)
    for (const tool of readJson("rig.json").tools) expect(readme).toContain(tool)
    for (const agent of readJson("rig.json").agents) expect(readme).toContain(agent)
  })

  it("documents install, upgrade, and removal", () => {
    expect(readme).toMatch(/opencode plugin add opencode-rig@/)
    expect(readme).toMatch(/opencode-rig init/)
    expect(readme).toMatch(/opencode-rig (check|remove)/)
  })

  it("records the current version in the changelog", () => {
    expect(changelog).toContain(`## ${readJson("package.json").version}`)
  })
})

describe("published tarball", () => {
  it("contains the required files and no tests or local artifacts", () => {
    const output = execFileSync(process.execPath, ["scripts/pack_check.mjs"], {
      cwd: PACKAGE_ROOT,
      encoding: "utf8",
      timeout: 120_000,
    })
    expect(output).toMatch(/package check passed: \d+ files, 11 skills, 7 role cards/)
  })
})
