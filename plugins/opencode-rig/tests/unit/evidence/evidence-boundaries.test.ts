import { existsSync, rmSync, symlinkSync } from "node:fs"
import { relative } from "node:path"
import { afterEach, describe, expect, it, vi } from "vitest"
import { sha256 } from "../../../src/digest.ts"
import { collectCommitScope, collectScope, detectDrift, refreshScope } from "../../../src/evidence.ts"
import { Project } from "../../helpers/harness.ts"

afterEach(() => vi.unstubAllEnvs())

describe("commit evidence boundaries", () => {
  it("rejects option-shaped revisions without overwriting an external file", () => {
    const project = Project.withCommit()
    const outside = new Project()
    const target = outside.write("evidence.txt", "keep me\n")
    expect(() => collectCommitScope(project.root, `--output=${target}`)).toThrow(/cannot read commit/)
    expect(outside.read("evidence.txt")).toBe("keep me\n")
  })

  it("rejects ranges rather than certifying empty blob hashes", () => {
    const project = Project.withCommit()
    project.write("src/a.ts", "changed\n")
    project.commit("change")
    expect(() => collectCommitScope(project.root, "HEAD^..HEAD")).toThrow(/single commit/)
  })

  it("pins a symbolic revision to its resolved commit", () => {
    const project = Project.withCommit()
    const oid = project.git("rev-parse", "HEAD")
    const scope = collectCommitScope(project.root, "HEAD")
    project.write("src/a.ts", "later\n")
    project.commit("later")
    expect(scope.commit).toBe(oid)
    expect(refreshScope(project.root, scope).digest).toBe(scope.digest)
  })

  it("hashes a merge relative to its first parent", () => {
    const project = Project.withCommit()
    const main = project.git("branch", "--show-current")
    project.git("switch", "-qc", "topic")
    project.write("src/topic.ts", "topic bytes\n")
    project.commit("topic")
    project.git("switch", "-q", main)
    project.write("src/main.ts", "main bytes\n")
    project.commit("main")
    project.git("merge", "--no-ff", "-qm", "merge topic", "topic")
    const scope = collectCommitScope(project.root, "HEAD")
    expect(scope.entries).toEqual([{ path: "src/topic.ts", status: "A", sha256: sha256("topic bytes\n") }])
  })

  it("rejects an empty commit inventory", () => {
    const project = Project.withCommit()
    project.git("commit", "--allow-empty", "-qm", "empty")
    expect(() => collectCommitScope(project.root, "HEAD")).toThrow(/empty commit scope/)
  })

  it("rejects a filter that selects no committed paths", () => {
    const project = Project.withCommit()
    expect(() => collectCommitScope(project.root, "HEAD", ["missing"])).toThrow(/empty commit scope/)
  })

  it("reads Unicode filenames without Git quoting", () => {
    const project = Project.withCommit({ "src/café.ts": "exact bytes\n" })
    const scope = collectCommitScope(project.root, "HEAD")
    expect(scope.entries).toEqual([{ path: "src/café.ts", status: "A", sha256: sha256("exact bytes\n") }])
  })
})

describe("working-tree evidence boundaries", () => {
  it("hashes the renamed destination and detects subsequent edits", () => {
    const project = Project.withCommit()
    project.git("mv", "src/a.ts", "src/renamed.ts")
    const scope = collectScope(project.root)
    expect(scope.entries).toEqual([
      { path: "src/a.ts", status: "D", sha256: sha256("") },
      { path: "src/renamed.ts", status: "R ", sha256: sha256("export const a = 1\n") },
    ])
    project.write("src/renamed.ts", "later edit\n")
    expect(detectDrift(project.root, scope)).toBe(true)
  })

  it("normalizes absolute in-project scopes to portable relative paths", () => {
    const project = Project.withCommit()
    project.write("src/a.ts", "changed\n")
    const scope = collectScope(project.root, [project.path("src")])
    expect(scope.paths).toEqual(["src"])
    expect(scope.entries).toEqual([{ path: "src/a.ts", status: "M", sha256: sha256("changed\n") }])
    expect(collectCommitScope(project.root, "HEAD", [project.path("src")]).paths).toEqual(["src"])
  })

  it("includes root-relative scope inventories", () => {
    const project = Project.withCommit()
    project.write("new.txt", "new bytes\n")
    expect(collectScope(project.root, ["."]).entries.map((entry) => entry.path)).toEqual(["new.txt", "src/a.ts"])
  })

  it.each(["relative", "absolute"])("rejects a %s scope outside the project", (kind) => {
    const project = Project.withCommit()
    const outside = new Project()
    outside.write("secret.txt", "private bytes\n")
    const path = kind === "absolute" ? outside.root : relative(project.root, outside.root)
    expect(() => collectScope(project.root, [path])).toThrow(/outside|escapes/)
    expect(() => collectCommitScope(project.root, "HEAD", [path])).toThrow(/outside|escapes/)
  })

  it("rejects an explicit scope through an external directory symlink", () => {
    const project = Project.withCommit()
    const outside = new Project()
    outside.write("secret.txt", "private bytes\n")
    symlinkSync(outside.root, project.path("linked"), "junction")
    expect(() => collectScope(project.root, ["linked/secret.txt"])).toThrow(/outside|escapes/)
  })

  it("rejects an unfiltered changed symlink escaping the project", () => {
    const project = Project.withCommit()
    const outside = new Project()
    outside.write("secret.txt", "private bytes\n")
    symlinkSync(outside.root, project.path("linked"), "junction")
    expect(() => collectScope(project.root)).toThrow(/outside|escapes/)
  })

  it("rejects a missing explicit path", () => {
    const project = Project.withCommit()
    expect(() => collectScope(project.root, ["src/missing.ts"])).toThrow(/does not exist/)
  })

  it("retains a genuine deletion selected explicitly", () => {
    const project = Project.withCommit()
    project.git("rm", "-q", "src/a.ts")
    expect(collectScope(project.root, ["src/a.ts"]).entries).toEqual([
      { path: "src/a.ts", status: "D ", sha256: sha256("") },
    ])
  })

  it("ignores environment overrides redirecting Git to another repository", () => {
    const project = Project.withCommit()
    const outside = Project.withCommit({ "outside.txt": "outside\n" })
    project.write("src/a.ts", "changed\n")
    outside.write("outside.txt", "outside changed\n")
    vi.stubEnv("GIT_DIR", outside.path(".git"))
    vi.stubEnv("GIT_WORK_TREE", outside.root)
    expect(collectScope(project.root).entries.map((entry) => entry.path)).toEqual(["src/a.ts"])
  })

  it("does not execute a repository-configured fsmonitor", () => {
    const project = Project.withCommit()
    const script = project.write("monitor.cjs", 'require("node:fs").writeFileSync("monitor-ran", "ran")\n')
    const command = `"${process.execPath.replaceAll("\\", "/")}" "${script.replaceAll("\\", "/")}"`
    project.git("config", "core.fsmonitor", command)
    project.git("status", "--porcelain=v1")
    expect(existsSync(project.path("monitor-ran"))).toBe(true)
    rmSync(project.path("monitor-ran"))
    collectScope(project.root, ["src"])
    expect(existsSync(project.path("monitor-ran"))).toBe(false)
  })

  it("ignores environment-injected fsmonitor configuration", () => {
    const project = Project.withCommit()
    const script = project.write("monitor.cjs", 'require("node:fs").writeFileSync("monitor-ran", "ran")\n')
    vi.stubEnv("GIT_CONFIG_COUNT", "1")
    vi.stubEnv("GIT_CONFIG_KEY_0", "core.fsmonitor")
    vi.stubEnv("GIT_CONFIG_VALUE_0", `"${process.execPath}" "${script}"`)
    collectScope(project.root, ["src"])
    expect(existsSync(project.path("monitor-ran"))).toBe(false)
  })
})
