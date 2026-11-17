/** Declared script files: source roots, digests, bounds and the execution receipt. */
import { mkdirSync, readFileSync, realpathSync, symlinkSync } from "node:fs"
import { join } from "node:path"
import { describe, expect, it } from "vitest"
import { executeScript, resolveScript, ScriptError, scriptEnv } from "../../../src/script.ts"
import { sha256File } from "../../../src/digest.ts"
import { PACKAGE_ROOT, Project } from "../../helpers/harness.ts"

const listing = JSON.parse(readFileSync(join(PACKAGE_ROOT, "src/resources/plugin-files.json"), "utf8")) as {
  files: Record<string, string>
}

function request(project: Project, overrides: Record<string, unknown> = {}) {
  return {
    source: "project" as const,
    path: "scripts/probe.mjs",
    interpreter: "node" as const,
    argv: [],
    timeoutMs: 30_000,
    projectRoot: project.root,
    packageRoot: PACKAGE_ROOT,
    runsRoot: project.path(".opencode-rig/runs"),
    logDirectory: project.path("logs"),
    ...overrides,
  }
}

describe("script source classes", () => {
  it("accepts a run probe only under the active run's scripts/", () => {
    const project = new Project()
    project.write("runs/rig-implement/run-1/scripts/probe.py", "print(1)\n")
    const runDirectory = project.path("runs/rig-implement/run-1")
    expect(
      resolveScript(
        request(project, { source: "run", path: "runs/rig-implement/run-1/scripts/probe.py", runDirectory }),
      ),
    ).toMatchObject({
      source: "run",
      digest: sha256File(project.path("runs/rig-implement/run-1/scripts/probe.py")),
    })
    expect(() =>
      resolveScript(request(project, { source: "run", path: "runs/rig-implement/run-1/plan.md", runDirectory })),
    ).toThrow(ScriptError)
    expect(() =>
      resolveScript(request(project, { source: "run", path: "runs/rig-implement/run-1/scripts/probe.py" })),
    ).toThrow(/outside its declared root/)
    project.write(".opencode-rig/runs/rig-implement/run-2/scripts/probe.py", "print(2)\n")
    expect(
      resolveScript(
        request(project, {
          source: "run",
          path: ".opencode-rig/runs/rig-implement/run-2/scripts/probe.py",
        }),
      ),
    ).toMatchObject({ source: "run" })
  })

  it.each(["scripts/probe.mjs", "bin/probe.mjs", "tools/probe.mjs"])("accepts the project root %s", (path) => {
    const project = new Project()
    project.write(path, "console.log('probe')\n")
    expect(resolveScript(request(project, { path }))).toMatchObject({ source: "project" })
  })

  it("rejects a project script outside its roots, a parent path and a symlink escape", () => {
    const project = new Project()
    project.write("src/probe.mjs", "console.log(1)\n")
    project.write("scripts/real.mjs", "console.log(1)\n")
    expect(() => resolveScript(request(project, { path: "src/probe.mjs" }))).toThrow(/outside its declared root/)
    expect(() => resolveScript(request(project, { path: "scripts/../../src/probe.mjs" }))).toThrow(
      /outside its declared root/,
    )
    mkdirSync(join(project.root, "scripts"), { recursive: true })
    symlinkSync(project.path("src/probe.mjs"), project.path("scripts/link.mjs"))
    expect(() => resolveScript(request(project, { path: "scripts/link.mjs" }))).toThrow(/escapes its root/)
  })

  it("binds a plugin script to the shipped listing", () => {
    expect(
      resolveScript(
        request(new Project(), {
          source: "plugin",
          path: "bin/opencode-rig.mjs",
          pluginFiles: listing.files,
        }),
      ),
    ).toMatchObject({ source: "plugin" })
    expect(() =>
      resolveScript(
        request(new Project(), {
          source: "plugin",
          path: "bin/opencode-rig.mjs",
          pluginFiles: { ...listing.files, "bin/opencode-rig.mjs": "0".repeat(64) },
        }),
      ),
    ).toThrow(/digest does not match/)
    expect(() => resolveScript(request(new Project(), { source: "plugin", path: "bin/opencode-rig.mjs" }))).toThrow(
      /no shipped plugin listing/,
    )
  })

  it("refuses a script over the size cap", () => {
    const project = new Project()
    project.write("scripts/big.mjs", `// ${"x".repeat(256 * 1024)}\n`)
    expect(() => executeScript(request(project, { path: "scripts/big.mjs" }))).toThrow(/exceeds/)
  })
})

describe("script execution receipt", () => {
  it("runs the file with the fixed interpreter, argv and a project cwd", async () => {
    const project = new Project()
    project.write("scripts/probe.mjs", "console.log(process.argv.slice(2).join(','), process.cwd())\n")
    const receipt = await executeScript(request(project, { argv: ["a", "b"] }))
    expect(receipt.status).toBe("passed")
    expect(receipt.exit_code).toBe(0)
    expect(receipt.argv).toEqual(["a", "b"])
    expect(receipt.digest).toBe(sha256File(project.path("scripts/probe.mjs")))
    expect(receipt.tail).toContain(`a,b ${realpathSync(project.root)}`)
    expect(receipt.stdout_sha256).toMatch(/^[0-9a-f]{64}$/)
    expect(readFileSync(receipt.log_path, "utf8")).toContain("a,b")
  })

  it("records a failing exit and redacts the tail", async () => {
    const project = new Project()
    project.write("scripts/fail.mjs", "console.error('GITHUB_TOKEN=ghp_secretvalue'); process.exit(3)\n")
    const receipt = await executeScript(request(project, { path: "scripts/fail.mjs" }))
    expect(receipt.status).toBe("failed")
    expect(receipt.exit_code).toBe(3)
    expect(receipt.tail).not.toContain("ghp_secretvalue")
  })

  it("uses only the positive environment allowlist", () => {
    const env = scriptEnv({ PATH: "/bin", HOME: "/home/u", PYTEST_ADDOPTS: "-p evil", SECRET: "x" })
    expect(env).toEqual({ PATH: "/bin", HOME: "/home/u" })
  })
})
