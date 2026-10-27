import { execFileSync } from "node:child_process"
import { readFileSync, writeFileSync } from "node:fs"
import { join } from "node:path"
import { describe, expect, it } from "vitest"
import { PACKAGE_ROOT, Project } from "../helpers/harness.ts"

const CLI = join(PACKAGE_ROOT, "bin", "opencode-rig.mjs")
const PACKAGE_VERSION = JSON.parse(readFileSync(join(PACKAGE_ROOT, "package.json"), "utf8")).version as string
const VERSION_PATTERN = PACKAGE_VERSION.replaceAll(".", "\\.")

interface CliResult {
  status: number
  stdout: string
  stderr: string
}

function cli(project: Project, args: string[], env: NodeJS.ProcessEnv = {}): CliResult {
  try {
    const stdout = execFileSync(process.execPath, [CLI, ...args], {
      cwd: project.root,
      encoding: "utf8",
      env: { ...process.env, OPENCODE_CONFIG_DIR: project.path("config"), ...env },
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

  it("prints configuration without a blanket commit rule", () => {
    const result = cli(new Project(), ["print-config"])
    const snippet = JSON.parse(result.stdout.slice(0, result.stdout.indexOf("\n\n//")))
    const resources = snippet.permissions.map((rule: { resource: string }) => rule.resource)
    expect(resources).toContain("git push *")
    expect(resources).toContain("git reset --hard*")
    expect(resources).not.toContain("git commit *")
    expect(snippet.plugins[0]).toMatchObject({ package: "opencode-rig" })
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
})
