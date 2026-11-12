/** Evidence handles large inventories, skipped symlinks, and gitlinks. */
import * as childProcess from "node:child_process"
import { symlinkSync } from "node:fs"
import { afterEach, expect, it, vi } from "vitest"
import { collectCommitScope, collectScope } from "../../../src/evidence.ts"
import { sha256 } from "../../../src/digest.ts"
import { Project } from "../../helpers/harness.ts"

vi.mock("node:child_process", async (original) => {
  const actual = await original<typeof import("node:child_process")>()
  return { ...actual, spawnSync: vi.fn(actual.spawnSync) }
})

afterEach(() => vi.restoreAllMocks())

it.each(["repository", "plain-directory"])("skips external and dangling symlinks within a %s scope", (kind) => {
  const project =
    kind === "repository" ? Project.withCommit({ ".gitignore": "src/links/\n", "src/a.ts": "a" }) : new Project()
  project.write("src/a.ts", "a")
  const external = new Project()
  external.write("private", "secret")
  project.write("src/links/keep", "keep")
  symlinkSync(external.root, project.path("src/links/external"), "junction")
  symlinkSync(external.path("missing"), project.path("src/links/dangling"), "junction")
  expect(collectScope(project.root, ["src"]).entries).toEqual([
    { path: "src/a.ts", status: "M", sha256: sha256("a") },
    { path: "src/links/keep", status: "M", sha256: sha256("keep") },
  ])
})

it("records gitlink changes using their object ID instead of a blob read", () => {
  const project = Project.withCommit()
  const oid = "a".repeat(40)
  project.git("update-index", "--add", "--cacheinfo", `160000,${oid},vendor/mod`)
  project.git("commit", "-qm", "gitlink")
  expect(collectCommitScope(project.root, "HEAD").entries).toEqual([
    { path: "vendor/mod", status: "A", sha256: sha256(oid) },
  ])
})

it("hashes committed blobs larger than one MiB", () => {
  const value = "x".repeat(2 * 1024 * 1024)
  const project = Project.withCommit({ "large.txt": value })
  expect(collectCommitScope(project.root, "HEAD").entries).toEqual([
    { path: "large.txt", status: "A", sha256: sha256(value) },
  ])
})

it("fails closed when status collection fails", () => {
  const project = Project.withCommit()
  vi.mocked(childProcess.spawnSync)
    .mockReturnValueOnce({ status: 0, stdout: Buffer.from("true\n") } as any)
    .mockReturnValueOnce({ status: 128, stdout: Buffer.alloc(0) } as any)
  expect(() => collectScope(project.root)).toThrow(/cannot collect.*status/i)
})

it("reports buffer exhaustion instead of an empty changed inventory", () => {
  const project = Project.withCommit()
  vi.mocked(childProcess.spawnSync)
    .mockReturnValueOnce({ status: 0, stdout: Buffer.from("true\n") } as any)
    .mockReturnValueOnce({
      status: null,
      stdout: Buffer.from("truncated"),
      error: Object.assign(new Error("too large"), { code: "ENOBUFS" }),
    } as any)
  expect(() => collectScope(project.root)).toThrow(/output.*limit/i)
})

it("rejects option-shaped revisions before asking Git to resolve them", () => {
  const project = Project.withCommit()
  const runner = vi.mocked(childProcess.spawnSync)
  runner.mockClear()
  expect(() => collectCommitScope(project.root, "--version")).toThrow(/cannot read commit/)
  expect(runner).not.toHaveBeenCalled()
})

it("collects a changed inventory larger than the default subprocess buffer", () => {
  const project = Project.withCommit()
  const oid = project.git("rev-parse", "HEAD:src/a.ts")
  const listing = Array.from({ length: 16000 }, (_, i) => `100644 ${oid}\tfiles/${i}-${"a".repeat(70)}\n`).join("")
  childProcess.execFileSync("git", ["update-index", "--index-info"], { cwd: project.root, input: listing })
  const entries = collectScope(project.root).entries
  expect(entries).toHaveLength(16000)
  expect(entries.every((entry) => entry.status === "AD" && entry.sha256 === sha256(""))).toBe(true)
})
