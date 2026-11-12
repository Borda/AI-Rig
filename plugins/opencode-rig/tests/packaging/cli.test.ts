import { execFileSync } from "node:child_process"
import { createHash } from "node:crypto"
import { mkdirSync, readFileSync, rmSync, statSync, symlinkSync, writeFileSync } from "node:fs"
import { join } from "node:path"
import { parse } from "jsonc-parser"
import { describe, expect, it } from "vitest"
import { PACKAGE_ROOT, Project, RESOURCES_DIR } from "../helpers/harness.ts"

const CLI = join(PACKAGE_ROOT, "bin", "opencode-rig.mjs")
const PACKAGE_VERSION = JSON.parse(readFileSync(join(PACKAGE_ROOT, "package.json"), "utf8")).version as string
const VERSION_PATTERN = PACKAGE_VERSION.replaceAll(".", "\\.")
const PRINCIPLES_PATH = join(RESOURCES_DIR, "references", "delivery-principles.md")
const PRINCIPLES_BODY = `${readFileSync(PRINCIPLES_PATH, "utf8").replace(/\r\n/g, "\n").replace(/\n+$/, "")}\n`
const SHIPPED = JSON.parse(readFileSync(join(RESOURCES_DIR, "permissions.json"), "utf8")).permissions as Array<{
  action: string
  resource: string
  effect: string
}>
const SHIPPED_COUNT = SHIPPED.length

function digest(value: string): string {
  return createHash("sha256").update(value).digest("hex")
}

/** Wrap a body exactly the way setup does, so a test can plant a stale or edited block. */
function managedBlock(body: string): string {
  return `<!-- opencode-rig:delivery-principles begin sha256=${digest(body)} -->\n${body}<!-- opencode-rig:delivery-principles end -->\n`
}

function globalInstructionsFile(project: Project): string {
  return project.path("home/.config/opencode/AGENTS.md")
}

function claudeInstructionsFile(project: Project): string {
  return project.path("home/.claude/CLAUDE.md")
}

interface CliResult {
  status: number
  stdout: string
  stderr: string
}

/** The rules of a merged configuration file, in the order the host resolves them. */
function readPermissions(text: string): Array<{ action: string; resource: string; effect: string }> {
  const config = parse(text, [], { allowTrailingComma: true }) as {
    permissions: Array<{ action: string; resource: string; effect: string }>
  }
  return config.permissions
}

function cli(project: Project, args: string[], env: NodeJS.ProcessEnv = {}): CliResult {
  // OPENCODE_RIG_HOME fails closed, so the fixture home must exist like a real one.
  mkdirSync(project.path("home"), { recursive: true })
  try {
    const stdout = execFileSync(process.execPath, [CLI, ...args], {
      cwd: project.root,
      encoding: "utf8",
      env: {
        ...process.env,
        OPENCODE_CONFIG_DIR: project.path("config"),
        OPENCODE_RIG_HOME: project.path("home"),
        ...env,
      },
      stdio: ["ignore", "pipe", "pipe"],
    })
    return { status: 0, stdout, stderr: "" }
  } catch (error) {
    const failure = error as { status?: number; stdout?: string; stderr?: string }
    return { status: failure.status ?? 1, stdout: failure.stdout ?? "", stderr: failure.stderr ?? "" }
  }
}

describe("opencode-rig CLI", () => {
  it("prints its version", () => {
    const result = cli(new Project(), ["version"])
    expect(result.status).toBe(0)
    expect(result.stdout.trim()).toBe(PACKAGE_VERSION)
  })

  it("prints help for no command and for --help", () => {
    const project = new Project()
    expect(cli(project, []).stdout).toContain("Usage: opencode-rig <command>")
    expect(cli(project, ["--help"]).stdout).toContain("print-config")
  })

  it("installs seven role agents into a project", () => {
    const project = new Project()
    const result = cli(project, ["init", "--project"])
    expect(result.status).toBe(0)
    expect(result.stdout).toMatch(/7 file\(s\) written/)
    expect(result.stdout).toMatch(/0 problem\(s\)/)

    for (const name of [
      "rig-investigator",
      "rig-implementer",
      "rig-reviewer",
      "rig-challenger",
      "rig-verifier",
      "rig-scientist",
      "rig-optimizer",
    ]) {
      const content = readFileSync(project.path(`.opencode/agents/${name}.md`), "utf8")
      expect(content.startsWith("---\n")).toBe(true)
      expect(content).toMatch(new RegExp(`<!-- opencode-rig: v${VERSION_PATTERN} sha256:[a-f0-9]{64} -->`))
      expect(content).toMatch(/mode: subagent/)
    }

    expect(cli(project, ["check", "--project"]).status).toBe(0)
    expect(cli(project, ["init", "--project"]).status).toBe(0)
  })

  it("installs into the global configuration directory by default", () => {
    const project = new Project()
    const result = cli(project, ["init"])
    expect(result.stdout).toContain(join(project.root, "config", "agents"))
    expect(project.exists("config/agents/rig-reviewer.md")).toBe(true)
  })

  it("detects an edited agent, refuses to overwrite silently, and repairs it with --upgrade", () => {
    const project = new Project()
    cli(project, ["init", "--project"])
    const path = project.path(".opencode/agents/rig-reviewer.md")
    writeFileSync(path, `${readFileSync(path, "utf8")}\nlocal edit\n`, "utf8")

    const check = cli(project, ["check", "--project"])
    expect(check.status).toBe(1)
    expect(check.stdout).toMatch(/CONFLICT rig-reviewer/)

    const plain = cli(project, ["init", "--project"])
    expect(plain.status).toBe(1)
    expect(plain.stdout).toMatch(/pass --upgrade/)
    expect(readFileSync(path, "utf8")).toMatch(/local edit/)

    const upgraded = cli(project, ["init", "--upgrade", "--project"])
    expect(upgraded.status).toBe(0)
    expect(readFileSync(path, "utf8")).not.toMatch(/local edit/)
    expect(cli(project, ["check", "--project"]).status).toBe(0)
  })

  it("updates a card whose package content changed instead of calling it a user edit", () => {
    const project = new Project()
    cli(project, ["init", "--project"])
    const path = project.path(".opencode/agents/rig-reviewer.md")
    // Simulate a file installed from an older package: the body differs from today's package but
    // still matches the digest its own stamp recorded, so it is not a user edit.
    const installed = readFileSync(path, "utf8")
    const frontmatter = installed.slice(0, installed.indexOf("\n---\n", 4) + 4)
    const olderBody = "Read-only review from an older package."
    const digest = createHash("sha256").update(`${frontmatter}\n\n${olderBody}\n`).digest("hex")
    writeFileSync(path, `${frontmatter}\n<!-- opencode-rig: v0.2.0 sha256:${digest} -->\n\n${olderBody}\n`, "utf8")

    const plain = cli(project, ["init", "--project"])
    expect(plain.status).toBe(0)
    expect(plain.stdout).not.toMatch(/CONFLICT rig-reviewer/)
    expect(plain.stdout).toMatch(/STALE {4}rig-reviewer/)
    expect(readFileSync(path, "utf8")).not.toMatch(/older package/)
    expect(cli(project, ["check", "--project"]).status).toBe(0)
  })

  it("never overwrites an unstamped file without --force", () => {
    const project = new Project()
    project.write(".opencode/agents/rig-reviewer.md", "# my own reviewer\n")
    cli(project, ["init", "--project"])
    expect(project.read(".opencode/agents/rig-reviewer.md")).toBe("# my own reviewer\n")

    const forced = cli(project, ["init", "--project", "--force"])
    expect(forced.status).toBe(0)
    expect(project.read(".opencode/agents/rig-reviewer.md")).toMatch(/opencode-rig: v/)
  })

  it("removes only stamped files", () => {
    const project = new Project()
    cli(project, ["init", "--project"])
    project.write(".opencode/agents/custom.md", "# custom\n")

    const result = cli(project, ["remove", "--project"])
    expect(result.stdout).toMatch(/7 removed, 1 kept/)
    expect(project.exists(".opencode/agents/rig-reviewer.md")).toBe(false)
    expect(project.exists(".opencode/agents/custom.md")).toBe(true)
    expect(cli(project, ["remove", "--project"]).status).toBe(0)
  })

  it("prints exactly the shipped rules, without a blanket commit rule", () => {
    const result = cli(new Project(), ["print-config"])
    const snippet = JSON.parse(result.stdout.slice(0, result.stdout.indexOf("\n\n//")))
    const shipped = JSON.parse(readFileSync(join(RESOURCES_DIR, "permissions.json"), "utf8"))
    // print-config must print the shipped bytes, not a second hand-maintained copy.
    expect(snippet.permissions).toEqual(shipped.permissions)
    const resources = snippet.permissions.map((rule: { resource: string }) => rule.resource)
    expect(resources).toContain("git push*")
    expect(resources).toContain("git reset --hard*")
    expect(resources).not.toContain("git commit *")
    // Recovery commands stay available under the blanket rebase rule (last match wins).
    expect(resources).toContain("git rebase --abort")
    expect(snippet.plugins[0]).toMatchObject({ package: "opencode-rig" })
  })

  it("prints the configuration without writing anything", () => {
    const project = new Project()
    const result = cli(project, ["print-config"])
    expect(result.status).toBe(0)
    expect(project.exists("config"), "print-config created the configuration directory").toBe(false)
    expect(project.exists("config/opencode.json")).toBe(false)
    expect(project.exists("config/opencode.jsonc")).toBe(false)
    expect(project.exists("opencode.json")).toBe(false)
    expect(project.exists("opencode.jsonc")).toBe(false)
  })

  it("reports a healthy doctor after installing into a project", () => {
    const project = new Project()
    project.git("init", "-q")
    project.write(".gitignore", ".opencode-rig/\n")
    cli(project, ["init", "--project"])
    const result = cli(project, ["doctor", "--project"])
    expect(result.stdout).toContain("opencode-rig doctor: healthy")
    expect(result.status).toBe(0)
  })

  it("curates the shipped skill and role cards with doctor --assets", () => {
    const project = new Project()
    project.git("init", "-q")
    project.write(".gitignore", ".opencode-rig/\n")
    cli(project, ["init", "--project"])
    const result = cli(project, ["doctor", "--project", "--assets"])
    expect(result.status).toBe(0)
    expect(result.stdout).toContain("opencode-rig doctor: healthy")
    expect(result.stdout).toMatch(/assets: 11 skills, 7 role agents, \d+ repeated prose line\(s\)/)
  })

  it("fails doctor when the agents are missing", () => {
    const project = new Project()
    const result = cli(project, ["doctor", "--project"])
    expect(result.status).toBe(1)
    expect(result.stdout).toMatch(/does not exist/)
  })

  it("rejects an unknown command", () => {
    const result = cli(new Project(), ["deploy"])
    expect(result.status).toBe(2)
    expect(result.stderr).toMatch(/unknown command 'deploy'/)
  })

  it("creates the global configuration with the shipped rules on a fresh install", () => {
    const project = new Project()
    const result = cli(project, ["init"])
    expect(result.status).toBe(0)
    expect(result.stdout).toMatch(new RegExp(`PERMS    created .*opencode\\.jsonc with ${SHIPPED_COUNT} rule\\(s\\)`))
    const config = JSON.parse(project.read("config/opencode.jsonc"))
    expect(config.permissions).toEqual(SHIPPED)
    expect(config.$schema).toBe("https://opencode.ai/config.json")
    // The agent count must keep counting role cards only.
    expect(result.stdout).toMatch(/7 file\(s\) written, 0 problem\(s\)/)
  })

  it("ships exactly the documented number of permission rules", () => {
    expect(SHIPPED_COUNT).toBe(60)
  })

  it("merges into an existing global opencode.jsonc instead of creating a second file", () => {
    const project = new Project()
    project.write(
      "config/opencode.jsonc",
      '{\n  "permissions": [\n    { "action": "shell", "resource": "git push*", "effect": "deny" }\n  ]\n}\n',
    )

    const result = cli(project, ["init"])
    expect(result.status).toBe(0)
    expect(result.stdout).toContain(join(project.root, "config", "opencode.jsonc"))
    expect(project.exists("config/opencode.json"), "a second global config file was created").toBe(false)

    // check has to resolve the same file init merged into.
    const check = cli(project, ["check"])
    expect(check.stdout).toContain(`${join(project.root, "config", "opencode.jsonc")}:`)
    expect(check.stdout).not.toContain(`${join(project.root, "config", "opencode.json")}:`)
  })

  it("merges into an existing project config, preserving comments and the user's own rules", () => {
    const project = new Project()
    const original = [
      "{",
      '  "$schema": "https://opencode.ai/config.json",',
      "  // my own note about plugins",
      '  "plugins": ["github:Borda/AI-Rig#main::path:plugins/opencode-rig"],',
      '  "permissions": [',
      '    { "action": "shell", "resource": "git push*", "effect": "deny" },',
      '    { "action": "shell", "resource": "npm test*", "effect": "allow" }',
      "  ],",
      "}",
      "",
    ].join("\n")
    project.write("opencode.jsonc", original)

    const result = cli(project, ["init", "--project"])
    expect(result.status).toBe(0)
    // The user's own git push* rule is the one the package does not add, so it counts once.
    expect(result.stdout).toMatch(
      new RegExp(`PERMS    merged ${SHIPPED_COUNT - 1} rule\\(s\\) into .*opencode\\.jsonc`),
    )

    const text = project.read("opencode.jsonc")
    expect(text).toContain("  // my own note about plugins")
    expect(text).toContain('"plugins": ["github:Borda/AI-Rig#main::path:plugins/opencode-rig"]')

    // Shipped rules are inserted first, so the user's later rules keep last-match precedence.
    const config = readPermissions(text)
    const push = config.findIndex((rule) => rule.resource === "git push*")
    const npmTest = config.findIndex((rule) => rule.resource === "npm test*")
    // The user's stricter effect for a shipped resource is kept, not overwritten.
    expect(config[push]!.effect).toBe("deny")
    expect(npmTest).toBeGreaterThan(push)
    // git push* ships as ask but is skipped because the user already declared it.
    expect(config.filter((rule) => rule.resource === "git push*")).toHaveLength(1)
    // Every shipped rule is present exactly once alongside the user's own.
    const resources = config.map((rule) => rule.resource)
    for (const rule of SHIPPED) {
      expect(resources.filter((resource) => resource === rule.resource)).toHaveLength(1)
    }
    expect(resources).toContain("npm test*")
  })

  it("keeps the shipped rules ahead of the user's own rules in the merged file", () => {
    const project = new Project()
    project.write(
      "opencode.jsonc",
      '{\n  "permissions": [\n    { "action": "shell", "resource": "npm test*", "effect": "allow" }\n  ]\n}\n',
    )
    expect(cli(project, ["init", "--project"]).status).toBe(0)

    const config = readPermissions(project.read("opencode.jsonc"))
    const userIndex = config.findIndex((rule) => rule.resource === "npm test*")
    expect(userIndex).toBeGreaterThan(-1)
    for (const rule of SHIPPED) {
      const index = config.findIndex((candidate) => candidate.resource === rule.resource)
      expect(index, `${rule.resource} must be merged`).toBeGreaterThan(-1)
      expect(index, `${rule.resource} must precede the user's rules`).toBeLessThan(userIndex)
    }
    // The recovery allows have to follow the blanket rebase rule: the last match wins.
    const blanket = config.findIndex((rule) => rule.resource === "git rebase*")
    const abort = config.findIndex((rule) => rule.resource === "git rebase --abort")
    expect(blanket).toBeGreaterThan(-1)
    expect(abort).toBeGreaterThan(blanket)
  })

  it("leaves the git rebase family to a rule the user already declares", () => {
    const project = new Project()
    project.write(
      "opencode.jsonc",
      '{\n  "permissions": [\n    { "action": "shell", "resource": "git rebase*", "effect": "ask" }\n  ]\n}\n',
    )
    expect(cli(project, ["init", "--project"]).status).toBe(0)

    const config = readPermissions(project.read("opencode.jsonc"))
    const resources = config.map((rule) => rule.resource)
    // The user's rule governs the whole family, so the shipped recovery allows are not added.
    for (const recovery of ["git rebase --abort", "git rebase --continue", "git rebase --skip", "git rebase --quit"]) {
      expect(resources, recovery).not.toContain(recovery)
    }
    expect(resources.filter((resource) => resource === "git rebase*")).toHaveLength(1)
    // The user's rule stays last, so it is the one the host resolves.
    expect(config[config.length - 1]!.resource).toBe("git rebase*")
  })

  it("deduplicates by action and resource, not by resource alone", () => {
    const project = new Project()
    project.write(
      "opencode.jsonc",
      '{\n  "permissions": [\n    { "action": "read", "resource": "cat *", "effect": "deny" }\n  ]\n}\n',
    )
    expect(cli(project, ["init", "--project"]).status).toBe(0)

    const config = readPermissions(project.read("opencode.jsonc"))
    // The user's rule uses another action on a shipped resource, so both rules have to survive.
    const shell = config.filter((rule) => rule.action === "shell" && rule.resource === "cat *")
    expect(shell).toHaveLength(1)
    expect(shell[0]!.effect).toBe("allow")
    const read = config.filter((rule) => rule.action === "read" && rule.resource === "cat *")
    expect(read).toHaveLength(1)
    expect(read[0]!.effect).toBe("deny")
  })

  it("rewrites comments inside the permissions array and keeps the ones outside it", () => {
    const project = new Project()
    const original = [
      "{",
      "  // my own note about the file",
      '  "permissions": [',
      "    // a comment inside the array",
      '    { "action": "shell", "resource": "npm test*", "effect": "allow" }',
      "  ]",
      "}",
      "",
    ].join("\n")
    project.write("opencode.jsonc", original)

    expect(cli(project, ["init", "--project"]).status).toBe(0)
    const text = project.read("opencode.jsonc")
    expect(text).toContain("// my own note about the file")
    expect(text).not.toContain("// a comment inside the array")
  })

  it("merges into the project configuration file that already exists", () => {
    const onlyJson = new Project()
    onlyJson.write("opencode.json", '{\n  "permissions": []\n}\n')
    expect(cli(onlyJson, ["init", "--project"]).status).toBe(0)
    expect(onlyJson.exists("opencode.jsonc"), "a second project config file was created").toBe(false)
    expect(readPermissions(onlyJson.read("opencode.json"))).toHaveLength(SHIPPED_COUNT)

    const onlyJsonc = new Project()
    onlyJsonc.write("opencode.jsonc", '{\n  "permissions": []\n}\n')
    expect(cli(onlyJsonc, ["init", "--project"]).status).toBe(0)
    expect(onlyJsonc.exists("opencode.json"), "a second project config file was created").toBe(false)
    expect(readPermissions(onlyJsonc.read("opencode.jsonc"))).toHaveLength(SHIPPED_COUNT)
  })

  it("adds no duplicate rules when init runs twice", () => {
    const project = new Project()
    project.write("opencode.jsonc", '{\n  "plugins": []\n}\n')
    cli(project, ["init", "--project"])
    const first = project.read("opencode.jsonc")
    const second = cli(project, ["init", "--project"])
    expect(second.stdout).toContain("shipped rules already present")
    expect(project.read("opencode.jsonc")).toBe(first)
    const permissions = JSON.parse(first).permissions as Array<{ resource: string }>
    expect(permissions).toHaveLength(new Set(permissions.map((rule) => rule.resource)).size)
  })

  it("leaves a deprecated singular permission key untouched", () => {
    const project = new Project()
    const original = '{\n  // old-style config\n  "permission": { "bash": "ask" },\n  "plugins": []\n}\n'
    project.write("opencode.jsonc", original)

    const result = cli(project, ["init", "--project"])
    expect(result.status).toBe(0)
    expect(result.stdout).toContain("uses the deprecated 'permission' key, left untouched")
    expect(result.stdout).toContain("print-config")
    expect(project.read("opencode.jsonc")).toBe(original)
  })

  it("refuses to merge into unreadable JSONC and leaves the file byte-identical", () => {
    const project = new Project()
    const broken = '{\n  "plugins": [,]\n}\n'
    project.write("opencode.jsonc", broken)

    const result = cli(project, ["init", "--project"])
    expect(result.status).toBe(1)
    expect(result.stdout).toMatch(/CONFIG   .*JSONC error/)
    expect(project.read("opencode.jsonc")).toBe(broken)
  })

  it("reports unmerged permissions from check without writing or failing on them", () => {
    const project = new Project()
    cli(project, ["init", "--project"])
    // The user read the guide and declined the rules: the file stays, the rules do not.
    project.write("opencode.jsonc", '{\n  "plugins": []\n}\n')
    const before = project.read("opencode.jsonc")

    const result = cli(project, ["check", "--project"])
    expect(result.stdout).toMatch(/PERMS    .*shipped rule\(s\) missing \(run init\)/)
    // The permission state is reported, not failed on: a declined merge is not a defect.
    expect(result.status).toBe(0)
    expect(project.read("opencode.jsonc")).toBe(before)
  })

  it("reports a missing configuration from check without failing on it", () => {
    const project = new Project()
    cli(project, ["init", "--project"])
    rmSync(project.path("opencode.jsonc"))

    const result = cli(project, ["check", "--project"])
    expect(result.stdout).toMatch(/PERMS    .*not merged \(run init\)/)
    expect(result.status).toBe(0)
  })

  it("never merges silently: init prints the rules it wrote and the guide to review", () => {
    const project = new Project()
    const created = cli(project, ["init"])
    expect(created.stdout).toMatch(new RegExp(`applied ${SHIPPED_COUNT} rule\\(s\\):`))
    expect(created.stdout).toMatch(/ask\s+shell git push\*/)
    expect(created.stdout).toContain("permissions-guide.md")

    // A second run writes nothing, so it must not re-print the rules.
    const again = cli(project, ["init"])
    expect(again.stdout).toContain("shipped rules already present")
    expect(again.stdout).not.toMatch(/applied \d+ rule\(s\):/)
  })
})

describe("opencode-rig setup global instructions", () => {
  it("writes the shipped delivery principles as a body-digest managed block", () => {
    const project = new Project()
    const result = cli(project, ["setup"])
    expect(result.status).toBe(0)
    expect(project.exists("home/.config/opencode/AGENTS.md")).toBe(true)

    const text = readFileSync(globalInstructionsFile(project), "utf8")
    expect(text).toBe(managedBlock(PRINCIPLES_BODY))
    // The body carries shipped text only: no plugin version and no install path, so a bump and a
    // second run never rewrite the user's file.
    expect(text).not.toContain(PACKAGE_VERSION)
    expect(text).not.toContain(PACKAGE_ROOT)
    expect(text).toContain(`sha256=${digest(PRINCIPLES_BODY)}`)
  })

  it("is idempotent and keeps the block current on a second setup", () => {
    const project = new Project()
    expect(cli(project, ["setup"]).status).toBe(0)
    const first = readFileSync(globalInstructionsFile(project), "utf8")

    const second = cli(project, ["setup"])
    expect(second.status).toBe(0)
    expect(second.stdout).toContain("delivery-principles block current")
    expect(readFileSync(globalInstructionsFile(project), "utf8")).toBe(first)
  })

  it("documents setup as the recommended command and init as its alias", () => {
    const help = cli(new Project(), ["--help"]).stdout
    expect(help).toMatch(/setup\s+Install the rig-\* role agents/)
    expect(help).toContain("Alias for setup")
  })

  it("creates the file when no Claude global instructions exist", () => {
    const project = new Project()
    expect(cli(project, ["check"]).stdout).toMatch(/GLOBAL   .*absent \(run setup\)/)
    expect(cli(project, ["setup"]).status).toBe(0)
    expect(cli(project, ["check"]).stdout).toContain("delivery-principles block current")
  })

  it("refuses to create the file when ~/.claude/CLAUDE.md exists and would be shadowed", () => {
    const project = new Project()
    project.write("home/.claude/CLAUDE.md", "# Claude global\n\nAlways answer in English.\n")
    const claude = project.read("home/.claude/CLAUDE.md")

    // A decision only the user can make: the install reports it loudly but does not fail the
    // whole sync, and `check` keeps it visible as an outstanding item.
    const setup = cli(project, ["setup"])
    expect(setup.status).toBe(0)
    expect(setup.stdout).toMatch(/WARN .*exists and OpenCode reads this file instead of it/)
    expect(setup.stdout).toContain(claudeInstructionsFile(project))
    expect(setup.stdout).toContain("--adopt-claude-instructions")
    expect(project.exists("home/.config/opencode/AGENTS.md")).toBe(false)
    expect(project.read("home/.claude/CLAUDE.md")).toBe(claude)

    const check = cli(project, ["check"])
    expect(check.status).toBe(1)
    expect(check.stdout).toMatch(/conflict:/)
    expect(check.stdout).toContain(claudeInstructionsFile(project))

    // remove leaves both files alone.
    expect(cli(project, ["remove"]).status).toBe(0)
    expect(project.read("home/.claude/CLAUDE.md")).toBe(claude)
    expect(project.exists("home/.config/opencode/AGENTS.md")).toBe(false)
  })

  it("adopts the Claude instructions opt-in and restores the fallback on remove", () => {
    const project = new Project()
    project.write("home/.claude/CLAUDE.md", "# Claude global\n\nAlways answer in English.\n")
    const claude = project.read("home/.claude/CLAUDE.md")

    const setup = cli(project, ["setup", "--adopt-claude-instructions"])
    expect(setup.status).toBe(0)
    const text = readFileSync(globalInstructionsFile(project), "utf8")
    expect(text.startsWith(managedBlock(PRINCIPLES_BODY))).toBe(true)
    expect(text.endsWith(claude)).toBe(true)
    expect(cli(project, ["check"]).status).toBe(0)

    expect(cli(project, ["remove"]).status).toBe(0)
    expect(project.exists("home/.config/opencode/AGENTS.md")).toBe(false)
    expect(project.read("home/.claude/CLAUDE.md")).toBe(claude)
  })

  it("appends the block after existing user content separated by one blank line", () => {
    const project = new Project()
    const user = "# My OpenCode rules\n\nBe concise.\n"
    project.write("home/.config/opencode/AGENTS.md", user)

    expect(cli(project, ["setup"]).status).toBe(0)
    const text = readFileSync(globalInstructionsFile(project), "utf8")
    expect(text).toBe(`${user}\n${managedBlock(PRINCIPLES_BODY)}`)
    expect(cli(project, ["check"]).status).toBe(0)

    expect(cli(project, ["remove"]).status).toBe(0)
    expect(readFileSync(globalInstructionsFile(project), "utf8")).toBe(user)
  })

  it("replaces a stale block but keeps the surrounding user content", () => {
    const project = new Project()
    expect(cli(project, ["setup"]).status).toBe(0)
    const user = "# My OpenCode rules\n\nKeep the house style.\n\n"
    const staleBody = `${PRINCIPLES_BODY}\n- **Stale.** shipped by an older release.\n`
    project.write("home/.config/opencode/AGENTS.md", `${user}${managedBlock(staleBody)}`)

    const check = cli(project, ["check"])
    expect(check.status).toBe(1)
    expect(check.stdout).toMatch(/stale delivery-principles block/)

    expect(cli(project, ["setup"]).status).toBe(0)
    const repaired = readFileSync(globalInstructionsFile(project), "utf8")
    expect(repaired).toBe(`${user}${managedBlock(PRINCIPLES_BODY)}`)
  })

  it("refuses a hand-edited block and leaves every byte in place", () => {
    const project = new Project()
    expect(cli(project, ["setup"]).status).toBe(0)
    const path = globalInstructionsFile(project)
    const edited = readFileSync(path, "utf8").replace(PRINCIPLES_BODY, `${PRINCIPLES_BODY}hand edit\n`)
    writeFileSync(path, edited, "utf8")

    const setup = cli(project, ["setup"])
    expect(setup.status).toBe(1)
    expect(setup.stdout).toMatch(/edited by hand/)
    expect(readFileSync(path, "utf8")).toBe(edited)

    const check = cli(project, ["check"])
    expect(check.status).toBe(1)
    expect(check.stdout).toMatch(/edited by hand/)

    expect(cli(project, ["remove"]).status).toBe(0)
    expect(readFileSync(path, "utf8")).toBe(edited)
  })

  it.runIf(process.platform !== "win32")("refuses a symlinked target and never writes through the link", () => {
    const project = new Project()
    const target = project.write("elsewhere/AGENTS.md", "# real target\n")
    mkdirSync(project.path("home/.config/opencode"), { recursive: true })
    symlinkSync(target, globalInstructionsFile(project))

    const setup = cli(project, ["setup"])
    expect(setup.status).toBe(1)
    expect(setup.stdout).toMatch(/symlink/)
    expect(readFileSync(target, "utf8")).toBe("# real target\n")

    const check = cli(project, ["check"])
    expect(check.status).toBe(1)
    expect(check.stdout).toMatch(/symlink/)

    expect(cli(project, ["remove"]).status).toBe(0)
    expect(readFileSync(target, "utf8")).toBe("# real target\n")
    expect(project.exists("home/.config/opencode/AGENTS.md")).toBe(true)
  })

  it("refuses a symlinked parent directory and writes nothing through it", () => {
    const project = new Project()
    const elsewhere = project.path("elsewhere")
    mkdirSync(elsewhere, { recursive: true })
    mkdirSync(project.path("home/.config"), { recursive: true })
    symlinkSync(elsewhere, project.path("home/.config/opencode"), "junction")

    const setup = cli(project, ["setup"])
    expect(setup.status).toBe(1)
    expect(setup.stdout).toMatch(/symlink/)
    expect(project.exists("elsewhere/AGENTS.md")).toBe(false)
  })

  it("refuses a symlinked Claude instructions file instead of adopting its target", () => {
    const project = new Project()
    const secret = project.write("secret/token.txt", "GITHUB_TOKEN=ghp_SECRET123\n")
    mkdirSync(project.path("home/.claude"), { recursive: true })
    symlinkSync(secret, claudeInstructionsFile(project))

    const setup = cli(project, ["setup", "--adopt-claude-instructions"])
    expect(setup.status).toBe(1)
    expect(setup.stdout).toMatch(/symlink/)
    expect(project.exists("home/.config/opencode/AGENTS.md")).toBe(false)
  })

  it("refuses an oversized Claude instructions file instead of copying it", () => {
    const project = new Project()
    project.write("home/.claude/CLAUDE.md", `GITHUB_TOKEN=ghp_SECRET123\n${"x".repeat(1024 * 1024)}`)

    const setup = cli(project, ["setup", "--adopt-claude-instructions"])
    expect(setup.status).toBe(1)
    expect(setup.stdout).toMatch(/larger than/)
    expect(project.exists("home/.config/opencode/AGENTS.md")).toBe(false)
  })

  it.runIf(process.platform !== "win32")("creates the adopted instructions file owner-only", () => {
    const project = new Project()
    project.write("home/.claude/CLAUDE.md", "GITHUB_TOKEN=ghp_SECRET123\n")

    expect(cli(project, ["setup", "--adopt-claude-instructions"]).status).toBe(0)
    expect(statSync(globalInstructionsFile(project)).mode & 0o777).toBe(0o600)
  })

  it("still adopts when the managed block already exists", () => {
    const project = new Project()
    expect(cli(project, ["setup"]).status).toBe(0)
    project.write("home/.claude/CLAUDE.md", "# Claude global\n\nAlways answer in English.\n")
    const claude = project.read("home/.claude/CLAUDE.md")

    const setup = cli(project, ["setup", "--adopt-claude-instructions"])
    expect(setup.status).toBe(0)
    const text = readFileSync(globalInstructionsFile(project), "utf8")
    expect(text.startsWith(managedBlock(PRINCIPLES_BODY))).toBe(true)
    expect(text.endsWith(claude)).toBe(true)

    // A second opt-in run must not append the same copy again.
    expect(cli(project, ["setup", "--adopt-claude-instructions"]).stdout).toContain("delivery-principles block current")
    expect(readFileSync(globalInstructionsFile(project), "utf8")).toBe(text)
  })

  it("restores a user file that had no final newline", () => {
    const project = new Project()
    const user = "# My OpenCode rules\n\nBe concise."
    project.write("home/.config/opencode/AGENTS.md", user)

    expect(cli(project, ["setup"]).status).toBe(0)
    expect(cli(project, ["remove"]).status).toBe(0)
    expect(readFileSync(globalInstructionsFile(project), "utf8")).toBe(user)
  })

  it("installs only role agents with --agents-only", () => {
    const project = new Project()
    project.write("home/.claude/CLAUDE.md", "# Claude global\n")

    const result = cli(project, ["init", "--agents-only"])
    expect(result.status).toBe(0)
    expect(project.exists("config/agents/rig-reviewer.md")).toBe(true)
    expect(project.exists("config/opencode.jsonc")).toBe(false)
    expect(project.exists("home/.config/opencode/AGENTS.md")).toBe(false)
  })

  it("fails closed when the test-only home override does not exist", () => {
    const project = new Project()
    const missing = project.path("missing-home")

    const result = cli(project, ["setup"], { OPENCODE_RIG_HOME: missing })
    expect(result.status).toBe(1)
    expect(result.stdout).toMatch(/OPENCODE_RIG_HOME points at a directory that does not exist/)
    expect(project.exists("missing-home")).toBe(false)
  })

  it("removes the managed block even when the agents directory is absent", () => {
    const project = new Project()
    expect(cli(project, ["setup"]).status).toBe(0)
    rmSync(project.path("config/agents"), { recursive: true, force: true })

    const remove = cli(project, ["remove"])
    expect(remove.status).toBe(0)
    expect(remove.stdout).toMatch(/no agent files to remove/)
    expect(project.exists("home/.config/opencode/AGENTS.md")).toBe(false)
  })

  it("keeps the block out of the project scope", () => {
    const project = new Project()
    expect(cli(project, ["setup", "--project"]).status).toBe(0)
    expect(project.exists("home/.config/opencode/AGENTS.md")).toBe(false)

    const check = cli(project, ["check", "--project"])
    expect(check.status).toBe(0)
    expect(check.stdout).not.toContain("GLOBAL")
  })

  it("reports RTK on the doctor line as an optional ok/warn check", () => {
    const project = new Project()
    project.git("init", "-q")
    project.write(".gitignore", ".opencode-rig/\n")
    cli(project, ["setup", "--project"])

    const result = cli(project, ["doctor", "--project"])
    expect(result.status).toBe(0)
    expect(result.stdout).toMatch(/RTK\s+(ok|warn)/)
    expect(result.stdout).toMatch(/reduce .*output/)
    expect(result.stdout).not.toMatch(/\d+[-–]\d+%/)
  })
})
