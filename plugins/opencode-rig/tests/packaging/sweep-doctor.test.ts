/** CLI doctor checks installed bytes and the configured artifact ignore rules. */
import { spawnSync } from "node:child_process"
import { mkdirSync } from "node:fs"
import { join } from "node:path"
import { expect, it } from "vitest"
import { PACKAGE_ROOT, Project } from "../helpers/harness.ts"

function cli(project: Project, command: string) {
  mkdirSync(project.path("home"), { recursive: true })
  return spawnSync(process.execPath, [join(PACKAGE_ROOT, "bin", "opencode-rig.mjs"), command], {
    cwd: project.root,
    encoding: "utf8",
    env: { ...process.env, OPENCODE_RIG_HOME: project.path("home"), OPENCODE_CONFIG_DIR: project.path("config") },
  })
}

it("reports installed role edits even when the version stamp is unchanged", () => {
  const project = Project.withCommit()
  expect(cli(project, "setup").status).toBe(0)
  const role = project.read("config/agents/rig-reviewer.md")
  project.write("config/agents/rig-reviewer.md", `${role}\nlocal instructions\n`)
  const result = cli(project, "doctor")
  expect(result.status).toBe(1)
  expect(result.stdout).toContain("rig-reviewer has local edits")
})

it("checks configured artifacts against Git info excludes", () => {
  const project = Project.withCommit()
  expect(cli(project, "setup").status).toBe(0)
  project.write("opencode.jsonc", '{"plugins":[{"package":"opencode-rig","options":{"artifacts":"local-evidence"}}]}')
  project.write(".git/info/exclude", "local-evidence/\n")
  const result = cli(project, "doctor")
  expect(result.status).toBe(0)
  expect(result.stdout).toContain("artifact root is gitignored: local-evidence/")
  expect(result.stdout).not.toContain("add '.opencode-rig/'")
})
