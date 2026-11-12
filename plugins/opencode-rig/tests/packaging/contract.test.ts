import { execFileSync } from "node:child_process"
import { existsSync, readFileSync, readdirSync } from "node:fs"
import { join } from "node:path"
import { describe, expect, it } from "vitest"
import { SKILLS } from "../../src/skill-manifest.ts"
import { PACKAGE_ROOT, Project, RESOURCES_DIR } from "../helpers/harness.ts"

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

describe("scope contract wording", () => {
  /** Skill prose with code spans removed, so a phrase survives formatting edits. */
  function plain(skill: string): string {
    return readFileSync(join(RESOURCES_DIR, "skills", skill, "SKILL.md"), "utf8")
      .replaceAll("`", "")
      .toLowerCase()
  }

  it("keeps the rig-plan verdict table, revision, and delegation contract", () => {
    const text = plain("rig-plan")
    for (const verdict of ["in-scope", "deferred", "out-of-scope"]) {
      expect(text, `rig-plan must name the ${verdict} verdict`).toContain(verdict)
    }
    expect(text).toMatch(/verdict table/)
    expect(text).toMatch(/exactly one verdict/)
    expect(text).toMatch(/revision counter/)
    expect(text).toMatch(/verdict changes since the previous revision/)
    expect(text).toMatch(/reconcile both ways/)
    expect(text).toMatch(/every owned path has an in-scope row/)
    expect(text).toMatch(/every in-scope row names a path/)
    expect(text).toMatch(/derive the delegation brief/)
    expect(text).toMatch(/never hand-write/)
  })

  it("keeps the rig-implement scope accounting, handoff, delegation, and tooling contract", () => {
    const text = plain("rig-implement")
    expect(text).toMatch(/no plan, no implementation/)
    expect(text).toMatch(/map every changed path to an in-scope row/)
    expect(text).toMatch(/never revert a path that is not in an in-scope row/)
    expect(text).toMatch(/did not author/)
    expect(text).toMatch(/ask when the tree is shared/)
    expect(text).toMatch(/runtime shell permission prompt/)
    expect(text).toMatch(/do not ask for a conversational \*\*approve\*\* first/)
    expect(text).toContain("rig_git_grant")
    expect(text).toMatch(/scope_acknowledged: true/)
    const handoff = text.split("\n").find((line) => line.includes("**commit handoff.**"))
    expect(handoff).toContain("**approve** authorizes only the proposed commit without a persistent grant")
    expect(handoff).toContain("only **approve always** records rig_git_grant")
    expect(handoff).not.toContain("never commit on an absent grant")
    expect(text).toMatch(/parallel/)
    expect(text).toMatch(/one owner per path/)
    expect(text).toMatch(/serialise only rows that share a path/)
    expect(text).toMatch(/evidence\.md#optional-command-compression/)
    const guidance = readFileSync(join(RESOURCES_DIR, "references", "evidence.md"), "utf8")
    expect(guidance).toMatch(/command -v rtk/)
    expect(guidance).toMatch(/never install (it|rtk) during a run/)
  })
})

describe("shipped permissions", () => {
  const manifest = readJson("src/resources/permissions.json")
  const rules = manifest.permissions as Array<{ action: string; resource: string; effect: string }>

  /**
   * The reviewed read-only list. Every `allow` the package ships has to appear here,
   * so this is the one place to audit when a command gains a writing form upstream.
   */
  const REVIEWED_READ_ONLY = new Set([
    "git show *",
    "git log *",
    "git status *",
    "git blame *",
    "git rev-parse *",
    "git rev-list *",
    "git ls-files *",
    "git ls-tree *",
    "git cat-file *",
    "git describe *",
    "git shortlog *",
    "git show-ref *",
    "git for-each-ref *",
    "git merge-base *",
    "git check-ignore *",
    "git check-attr *",
    "git count-objects *",
    "git worktree list *",
    "git stash list *",
    "git config --get *",
    "git var *",
    "cat *",
    "head *",
    "tail *",
    "grep *",
    "wc *",
    "cut *",
    "tr *",
    "comm *",
    "cmp *",
    "diff *",
    "nl *",
    "basename *",
    "dirname *",
    "realpath *",
    "which *",
    "type *",
    "stat *",
    "file *",
    "du *",
    "df *",
    "date",
    "ls *",
    "pwd *",
    "jq *",
    "shasum *",
    "sha256sum *",
    "uniq *",
    "echo *",
    "printf *",
  ])

  const RECOVERY_ALLOWS = ["git rebase --abort", "git rebase --continue", "git rebase --skip", "git rebase --quit"]

  const STOP_RULES = ["git push*", "git reset --hard*", "git clean*", "git rebase*"]

  /**
   * Resolve a command the way the host resolves it: whole-value wildcards, a pattern ending in
   * ` *` also matching the bare command, and the last matching rule winning.
   */
  function hostMatches(pattern: string, command: string): boolean {
    const escaped = pattern
      .replaceAll("\\", "/")
      .replace(/[.+^${}()|[\]\\]/g, "\\$&")
      .replace(/\*/g, ".*")
      .replace(/\?/g, ".")
    const normalized = escaped.endsWith(" .*") ? `${escaped.slice(0, -3)}( .*)?` : escaped
    return new RegExp(`^${normalized}$`, "s").test(command.replaceAll("\\", "/"))
  }

  function hostEffect(command: string): string | undefined {
    let effect: string | undefined
    for (const rule of rules) {
      if (rule.action === "shell" && hostMatches(rule.resource, command)) effect = rule.effect
    }
    return effect
  }

  /** The resources the guide lists as the read-only allowlist, read out of its two code blocks. */
  function guideAllowlist(guide: string): string[] {
    const resources: string[] = []
    for (const heading of ["Git inspection:", "File and text inspection:"]) {
      const start = guide.indexOf(heading)
      expect(start, `the guide needs a ${heading} section`).toBeGreaterThan(-1)
      const block = /```text\n([\s\S]*?)```/.exec(guide.slice(start))
      expect(block, `the guide needs a code block after ${heading}`).not.toBeNull()
      for (const line of block![1]!.split("\n")) {
        const resource = line.trim()
        if (resource) resources.push(resource)
      }
    }
    return resources
  }

  it("declares a well-formed ordered ruleset", () => {
    expect(manifest.schema).toBe(1)
    const actions = new Set(["read", "edit", "glob", "grep", "list", "shell", "task", "webfetch", "websearch"])
    const effects = new Set(["allow", "ask", "deny"])
    expect(rules.length).toBeGreaterThan(0)
    for (const rule of rules) {
      expect(actions.has(rule.action), `${rule.resource} action`).toBe(true)
      expect(effects.has(rule.effect), `${rule.resource} effect`).toBe(true)
      expect(rule.resource.length).toBeGreaterThan(0)
    }
    expect(new Set(rules.map((rule) => `${rule.action} ${rule.resource}`)).size).toBe(rules.length)
  })

  it("ships an allow exactly for the reviewed read-only list and the rebase recovery commands", () => {
    const shippedAllows = rules.filter((rule) => rule.effect === "allow").map((rule) => rule.resource)
    for (const resource of shippedAllows) {
      expect(
        REVIEWED_READ_ONLY.has(resource) || RECOVERY_ALLOWS.includes(resource),
        `${resource} is not on the reviewed read-only list`,
      ).toBe(true)
    }
    // The other direction: a name on the reviewed list the package no longer ships is a stale audit list.
    for (const resource of REVIEWED_READ_ONLY) {
      expect(shippedAllows, `${resource} is reviewed but not shipped`).toContain(resource)
    }
    // No blanket commit rule, so a recorded grant can take effect.
    expect(rules.map((rule) => rule.resource)).not.toContain("git commit *")
    expect(rules.some((rule) => rule.resource.startsWith("git commit"))).toBe(false)
    // The recovery allows must follow the blanket rebase rule: the last match wins.
    const blanket = rules.findIndex((rule) => rule.resource === "git rebase*")
    expect(blanket).toBeGreaterThan(-1)
    expect(rules[blanket]!.effect).toBe("ask")
    for (const recovery of RECOVERY_ALLOWS) {
      const index = rules.findIndex((rule) => rule.resource === recovery)
      expect(index, `${recovery} must exist`).toBeGreaterThan(-1)
      expect(index, `${recovery} must follow git rebase*`).toBeGreaterThan(blanket)
      expect(rules[index]!.effect).toBe("allow")
    }
  })

  it("ships the four stop rules with the ask effect", () => {
    for (const resource of STOP_RULES) {
      const rule = rules.find((candidate) => candidate.resource === resource)
      expect(rule, `${resource} must ship`).toBeDefined()
      expect(rule!.effect, `${resource} effect`).toBe("ask")
    }
  })

  it("resolves a command against the shipped rules the way the host does", () => {
    // The last match wins, so a recovery allow only takes effect after the blanket rebase ask.
    expect(hostEffect("git rebase")).toBe("ask")
    expect(hostEffect("git rebase --abort")).toBe("allow")
    expect(hostEffect("git rebase --continue")).toBe("allow")
    expect(hostEffect("git rebase --skip")).toBe("allow")
    expect(hostEffect("git rebase --quit")).toBe("allow")
    // A pattern ending in ` *` also matches the bare command.
    expect(hostEffect("git status")).toBe("allow")
    expect(hostEffect("git status --short")).toBe("allow")
    // The blanket rules cover their whole family.
    expect(hostEffect("git push --force")).toBe("ask")
    expect(hostEffect("git clean -n")).toBe("ask")
    expect(hostEffect("git reset --hard HEAD")).toBe("ask")
    // A read-only command that writes a file asks instead of writing silently.
    expect(hostEffect("git log")).toBe("allow")
    expect(hostEffect("git log --output=out.txt")).toBe("ask")
    expect(hostEffect("git log --output out.txt")).toBe("ask")
    expect(hostEffect("git show --output=out.txt")).toBe("ask")
    expect(hostEffect("git rev-list --output=out.txt")).toBe("ask")
  })

  it("ships a review guide that documents every rule and its effect", () => {
    const guide = readFileSync(join(RESOURCES_DIR, "permissions-guide.md"), "utf8")
    expect(guide).toContain("permissions.json")
    for (const rule of rules) expect(guide, rule.resource).toContain(rule.resource)
    expect(guide).toMatch(/never turns a configured `ask` or `deny` into `allow`/)

    // The guide's effect column has to agree with the shipped effect, in both directions.
    const tableEffects = new Map<string, string>()
    for (const match of guide.matchAll(/^\| `([^`]+)` \| `(allow|ask|deny)` \|/gm)) {
      tableEffects.set(match[1]!, match[2]!)
    }
    const allowlist = guideAllowlist(guide)
    for (const rule of rules) {
      const documented = tableEffects.get(rule.resource)
      if (documented !== undefined) {
        expect(documented, `${rule.resource} effect in the guide`).toBe(rule.effect)
        continue
      }
      expect(allowlist, `${rule.resource} must be documented`).toContain(rule.resource)
      expect(rule.effect, `${rule.resource} effect`).toBe("allow")
    }
    for (const [resource, effect] of tableEffects) {
      const rule = rules.find((candidate) => candidate.resource === resource)
      expect(rule, `${resource} is documented but not shipped`).toBeDefined()
      expect(rule!.effect, `${resource} effect`).toBe(effect)
    }
    for (const resource of allowlist) {
      const rule = rules.find((candidate) => candidate.resource === resource)
      expect(rule, `${resource} is listed but not shipped`).toBeDefined()
      expect(rule!.effect, `${resource} effect`).toBe("allow")
    }
    expect(new Set(allowlist).size, "the guide lists a resource twice").toBe(allowlist.length)
    expect([...allowlist].sort()).toEqual([...REVIEWED_READ_ONLY].sort())
    // The defaults this package targets are the v2 ones, not the v1 ones.
    expect(guide).toMatch(/`external_directory` is allowed by default, not asked/)
    expect(guide).not.toMatch(/doom_loop` and `external_directory` default to `ask`/)
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

describe("rendered role agents", () => {
  const CLI = join(PACKAGE_ROOT, "bin", "opencode-rig.mjs")
  const shipped = readJson("src/resources/permissions.json").permissions as Array<{
    action: string
    resource: string
    effect: string
  }>
  const agentManifest = readJson("src/resources/agents.json").agents as Record<string, any>

  function parseFrontmatter(text: string): Array<{ action: string; resource: string; effect: string }> {
    const block = /^---\n([\s\S]*?)\n---/.exec(text)?.[1]
    if (!block) throw new Error("the rendered agent has no frontmatter")
    const rules: Array<{ action: string; resource: string; effect: string }> = []
    for (const match of block.matchAll(/^ {2}- action: (.+)\n {4}resource: (.+)\n {4}effect: (.+)$/gm)) {
      rules.push({
        action: match[1]!.trim(),
        resource: match[2]!.trim().replace(/^"(.*)"$/, "$1"),
        effect: match[3]!.trim(),
      })
    }
    return rules
  }

  function renderAll(): Project {
    const project = new Project()
    execFileSync(process.execPath, [CLI, "init", "--project"], {
      cwd: project.root,
      encoding: "utf8",
      env: {
        ...process.env,
        OPENCODE_CONFIG_DIR: project.path("config"),
        OPENCODE_RIG_HOME: project.path("home"),
      },
    })
    return project
  }

  it("carries the shipped rules after the agent's own shell catch-all", () => {
    const project = renderAll()
    const rendered = parseFrontmatter(project.read(".opencode/agents/rig-reviewer.md"))
    const catchAll = rendered.findIndex(
      (rule) => rule.action === "shell" && rule.resource === "*" && rule.effect === "ask",
    )
    expect(catchAll, "the agent keeps its own shell catch-all").toBeGreaterThan(-1)
    // The host appends agent rules after the global ones and resolves the last match,
    // so the shipped rules only take effect when they land after the catch-all.
    expect(rendered.slice(catchAll + 1)).toEqual(shipped)
  })

  it("gives every role agent the shipped rules with their shipped action and effect", () => {
    const project = renderAll()
    const names = Object.keys(agentManifest)
    expect(names).toHaveLength(7)
    for (const name of names) {
      const own = agentManifest[name].permissions
      const rendered = parseFrontmatter(project.read(`.opencode/agents/${name}.md`))
      // The agent keeps its own rules first, then the shipped ruleset in its shipped order.
      expect(rendered.slice(0, own.length), name).toEqual(own)
      expect(rendered.slice(own.length), name).toEqual(shipped)
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

  it("documents setup, the managed block, and the adopt opt-in", () => {
    expect(readme).toContain("opencode-rig setup")
    expect(readme).toContain("--adopt-claude-instructions")
    expect(readme).toContain("--agents-only")
    expect(readme).toMatch(/managed block/i)
    expect(readme).toContain("OPENCODE_RIG_HOME")

    expect(changelog).toContain("opencode-rig setup")
    expect(changelog).toContain("--adopt-claude-instructions")
    expect(changelog).toContain("--agents-only")
    expect(changelog).toMatch(/managed block/i)
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
