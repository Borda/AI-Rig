import { spawnSync } from "node:child_process"
import { createHash } from "node:crypto"
import { lstatSync, mkdirSync, readFileSync, symlinkSync } from "node:fs"
import { join } from "node:path"
import { describe, expect, it } from "vitest"
import { PACKAGE_ROOT, Project } from "../helpers/harness.ts"

const CLI = join(PACKAGE_ROOT, "bin", "opencode-rig.mjs")
const VERSION = JSON.parse(readFileSync(join(PACKAGE_ROOT, "package.json"), "utf8")).version

function cli(project: Project, args: string[], cwd = project.root, env: NodeJS.ProcessEnv = {}) {
  mkdirSync(project.path("home"), { recursive: true })
  return spawnSync(process.execPath, [CLI, ...args], {
    cwd,
    encoding: "utf8",
    env: {
      ...process.env,
      OPENCODE_CONFIG_DIR: project.path("config"),
      OPENCODE_RIG_HOME: project.path("home"),
      ...env,
    },
  })
}

describe("CLI argument boundaries", () => {
  it.each([
    ["setup", "--dry-run"],
    ["setup", "--chek"],
    ["remove", "--projcet"],
    ["remove", "--check"],
    ["check", "--force"],
    ["setup", "--assets"],
    ["print-config", "--project"],
  ])("rejects %s %s before touching installed files", (command, flag) => {
    const project = new Project()
    expect(cli(project, ["setup"]).status).toBe(0)
    const agent = project.read("config/agents/rig-reviewer.md")
    const instructions = project.read("home/.config/opencode/AGENTS.md")
    project.write("config/agents/rig-reviewer.md", `${agent}local edit\n`)

    const result = cli(project, [command, flag])

    expect(result.status).toBe(2)
    expect(result.stderr).toContain(flag)
    expect(result.stderr).toContain("Usage: opencode-rig")
    expect(project.read("config/agents/rig-reviewer.md")).toBe(`${agent}local edit\n`)
    expect(project.read("home/.config/opencode/AGENTS.md")).toBe(instructions)
  })

  it("rejects an unknown flag without creating a global installation", () => {
    const project = new Project()
    const result = cli(project, ["setup", "--dry-run"])
    expect(result.status).toBe(2)
    expect(project.exists("config")).toBe(false)
    expect(project.exists("home/.config")).toBe(false)
  })

  it("prints the version for --version", () => {
    const result = cli(new Project(), ["--version"])
    expect(result.status).toBe(0)
    expect(result.stdout.trim()).toBe(VERSION)
  })

  it("labels the CLI doctor as a limited installation check", () => {
    const result = cli(new Project(), ["doctor", "--assets"])
    expect(result.stdout).toContain("install checks only; use rig_doctor")
    expect(result.stdout).toContain("rig_doctor also checks the runtime skill manifest")
  })
})

describe("instruction boundaries", () => {
  it("does not repeat adopted instructions when user text follows them", () => {
    const project = new Project()
    project.write("home/.claude/CLAUDE.md", "# Personal rules\nKeep changes small.\n")
    expect(cli(project, ["setup", "--adopt-claude-instructions"]).status).toBe(0)
    const text = `${project.read("home/.config/opencode/AGENTS.md")}\nLater user rules.\n`
    project.write("home/.config/opencode/AGENTS.md", text)

    expect(cli(project, ["setup", "--adopt-claude-instructions"]).status).toBe(0)
    expect(project.read("home/.config/opencode/AGENTS.md")).toBe(text)
  })

  it("adopts a copy found only before the managed block", () => {
    const project = new Project()
    const adopted = "# Personal rules\nKeep changes small.\n"
    project.write("home/.config/opencode/AGENTS.md", adopted)
    project.write("home/.claude/CLAUDE.md", adopted)

    expect(cli(project, ["setup", "--adopt-claude-instructions"]).status).toBe(0)
    const text = project.read("home/.config/opencode/AGENTS.md")
    expect(text.startsWith(adopted)).toBe(true)
    expect(text.endsWith(adopted)).toBe(true)
    expect(text.split(adopted)).toHaveLength(3)
  })

  it("checks and refreshes a CRLF block without changing surrounding bytes", () => {
    const project = new Project()
    project.write("home/.config/opencode/AGENTS.md", "# User rules\r\nKeep my line endings.\r\n")
    expect(cli(project, ["setup"]).status).toBe(0)
    const text = project.read("home/.config/opencode/AGENTS.md").replace(/\r?\n/g, "\r\n") + "After block.\r\n"
    project.write("home/.config/opencode/AGENTS.md", text)

    expect(cli(project, ["check"]).status).toBe(0)
    expect(cli(project, ["setup"]).status).toBe(0)
    expect(project.read("home/.config/opencode/AGENTS.md")).toBe(text)
  })

  it("removes a CRLF block and preserves the user's CRLF text", () => {
    const project = new Project()
    const user = "# User rules\r\nKeep my line endings.\r\n"
    project.write("home/.config/opencode/AGENTS.md", user)
    expect(cli(project, ["setup"]).status).toBe(0)
    const text = project.read("home/.config/opencode/AGENTS.md").replace(/\r?\n/g, "\r\n") + "After block.\r\n"
    project.write("home/.config/opencode/AGENTS.md", text)

    expect(cli(project, ["remove"]).status).toBe(0)
    expect(project.read("home/.config/opencode/AGENTS.md")).toBe(`${user}After block.\r\n`)
  })

  it("refreshes an authenticated stale CRLF block and preserves surrounding bytes", () => {
    const project = new Project()
    expect(cli(project, ["setup"]).status).toBe(0)
    const current = project.read("home/.config/opencode/AGENTS.md").replace(/\n/g, "\r\n")
    const body = "# Older principles\nKeep edits scoped.\n"
    const digest = createHash("sha256").update(body).digest("hex")
    const stale = `<!-- opencode-rig:delivery-principles begin sha256=${digest} -->\n${body}<!-- opencode-rig:delivery-principles end -->\n`
    const prefix = "Before block.\r\n\r\n"
    const suffix = "After block.\r\n"
    project.write("home/.config/opencode/AGENTS.md", `${prefix}${stale.replace(/\n/g, "\r\n")}${suffix}`)

    const check = cli(project, ["check"])
    expect(check.status).toBe(1)
    expect(check.stdout).toContain("stale delivery-principles block")
    expect(cli(project, ["setup"]).status).toBe(0)
    expect(project.read("home/.config/opencode/AGENTS.md")).toBe(`${prefix}${current}${suffix}`)
    expect(cli(project, ["check"]).status).toBe(0)
  })
})

describe("project target boundaries", () => {
  it("uses the Git root for setup, check, doctor, and removal from a subdirectory", () => {
    const project = new Project()
    project.write(".git/HEAD", "ref: refs/heads/main\n")
    project.write(".git/config", "[core]\nrepositoryformatversion = 0\nbare = false\n")
    mkdirSync(project.path(".git/objects"))
    mkdirSync(project.path(".git/refs"))
    mkdirSync(project.path("nested/deeper"), { recursive: true })
    const cwd = project.path("nested/deeper")

    expect(cli(project, ["setup", "--project"], cwd).status).toBe(0)
    expect(project.exists(".opencode/agents/rig-reviewer.md")).toBe(true)
    expect(project.exists("opencode.jsonc")).toBe(true)
    expect(project.exists("nested/deeper/.opencode")).toBe(false)
    expect(project.exists("nested/deeper/opencode.jsonc")).toBe(false)
    expect(cli(project, ["check", "--project"], cwd).status).toBe(0)
    expect(cli(project, ["doctor", "--project"], cwd).status).toBe(0)
    expect(cli(project, ["remove", "--project"], cwd).status).toBe(0)
    expect(project.exists(".opencode/agents/rig-reviewer.md")).toBe(false)
  })

  it.each([false, true])("refuses a dangling agent symlink with force=%s", (force) => {
    const project = new Project()
    mkdirSync(project.path(".opencode/agents"), { recursive: true })
    const target = project.path("outside.md")
    symlinkSync(target, project.path(".opencode/agents/rig-reviewer.md"))

    const result = cli(project, ["setup", "--project", ...(force ? ["--force"] : [])])

    expect(result.status).toBe(1)
    expect(`${result.stdout}${result.stderr}`).toContain("symlink")
    expect(project.exists("outside.md")).toBe(false)
    expect(lstatSync(project.path(".opencode/agents/rig-reviewer.md")).isSymbolicLink()).toBe(true)
  })

  it("refuses to overwrite an agent symlink target with --force", () => {
    const project = new Project()
    const target = project.write("outside.md", "Private data\n")
    mkdirSync(project.path(".opencode/agents"), { recursive: true })
    symlinkSync(target, project.path(".opencode/agents/rig-reviewer.md"))

    const result = cli(project, ["setup", "--project", "--force"])

    expect(result.status).toBe(1)
    expect(`${result.stdout}${result.stderr}`).toContain("symlink")
    expect(project.read("outside.md")).toBe("Private data\n")
  })

  it.each(["setup", "remove", "doctor"])("refuses %s through a symlinked agents directory", (command) => {
    const project = new Project()
    mkdirSync(project.path("elsewhere"))
    mkdirSync(project.path(".opencode"))
    symlinkSync(project.path("elsewhere"), project.path(".opencode/agents"), "junction")

    const result = cli(project, [command, "--project"])

    expect(result.status).toBe(1)
    expect(`${result.stdout}${result.stderr}`).toContain("symlink")
    expect(project.exists("elsewhere/rig-reviewer.md")).toBe(false)
  })

  it("refuses to merge through a project configuration symlink", () => {
    const project = new Project()
    const target = project.write("outside.jsonc", "{}\n")
    symlinkSync(target, project.path("opencode.jsonc"))

    const result = cli(project, ["setup", "--project"])

    expect(result.status).toBe(1)
    expect(`${result.stdout}${result.stderr}`).toContain("symlink")
    expect(project.read("outside.jsonc")).toBe("{}\n")
  })

  it("refuses a dangling configuration symlink", () => {
    const project = new Project()
    symlinkSync(project.path("outside.jsonc"), project.path("opencode.jsonc"))

    const result = cli(project, ["setup", "--project"])

    expect(result.status).toBe(1)
    expect(`${result.stdout}${result.stderr}`).toContain("symlink")
    expect(project.exists("outside.jsonc")).toBe(false)
  })

  it("refuses a symlinked global config root", () => {
    const project = new Project()
    mkdirSync(project.path("elsewhere"))
    symlinkSync(project.path("elsewhere"), project.path("config"), "junction")

    const result = cli(project, ["setup"])

    expect(result.status).toBe(1)
    expect(`${result.stdout}${result.stderr}`).toContain("symlink")
    expect(project.exists("elsewhere/agents")).toBe(false)
  })

  it("ignores environment redirects when resolving a project root", () => {
    const project = new Project()
    const result = cli(project, ["setup", "--project"], project.root, {
      GIT_DIR: join(PACKAGE_ROOT, "..", "..", ".git"),
      GIT_WORK_TREE: project.path("redirected"),
    })

    expect(result.status).toBe(0)
    expect(project.exists(".opencode/agents/rig-reviewer.md")).toBe(true)
    expect(project.exists("redirected/.opencode")).toBe(false)
    expect(project.exists("opencode.jsonc")).toBe(true)
  })
})
