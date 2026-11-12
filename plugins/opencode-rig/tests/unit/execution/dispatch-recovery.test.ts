/** Dispatch recovers safely from unavailable inventories and concurrent writers. */
import * as childProcess from "node:child_process"
import * as fs from "node:fs"
import { sep } from "node:path"
import { expect, it, vi, afterEach } from "vitest"
import { dispatchRole } from "../../../src/dispatch.ts"
import { Project, RESOURCES_DIR } from "../../helpers/harness.ts"

vi.mock("node:fs", async (original) => {
  const actual = await original<typeof import("node:fs")>()
  return { ...actual, readFileSync: vi.fn(actual.readFileSync), lstatSync: vi.fn(actual.lstatSync) }
})
vi.mock("node:child_process", async (original) => {
  const actual = await original<typeof import("node:child_process")>()
  return { ...actual, spawnSync: vi.fn(actual.spawnSync) }
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.clearAllMocks()
})

const writer = { role: "rig-implementer", task: "Edit", paths: ["src"], read_only: false } as const
const reviewer = { role: "rig-reviewer", task: "Review" } as const

function context(project: Project, action: () => unknown = () => {}, text = "Done") {
  return {
    location: { directory: project.root },
    session: {
      create: vi.fn(async () => ({ id: "child" })),
      prompt: vi.fn(async () => {
        await action()
      }),
      wait: vi.fn(async () => {}),
      context: vi.fn(async () => [{ role: "assistant", parts: [{ text }] }]),
      remove: vi.fn(async () => {}),
    },
  }
}

it("runs outside Git with an explicit marker while still checking writer reports", async () => {
  const project = new Project()
  const ctx = context(project, undefined, "changed: outside.ts")
  const result = await dispatchRole(ctx, writer, RESOURCES_DIR)
  expect(result).toMatchObject({ workspace_unverified: true, scope_violation: true })
  expect(result.text).toContain("workspace_unverified: true")
  expect(result.violations).toEqual([expect.stringContaining("outside the input scope: outside.ts")])
  expect((await dispatchRole(context(project), reviewer, RESOURCES_DIR)).workspace_unverified).toBe(true)
})

it("ignores quoted change reports from a reviewer", async () => {
  const project = Project.withCommit()
  const result = await dispatchRole(context(project, undefined, "changed: outside.ts"), reviewer, RESOURCES_DIR)
  expect(result).toMatchObject({ scope_violation: false, violations: [] })
})

it("attributes edits to an overlapping writer even after that writer finishes", async () => {
  const project = Project.withCommit()
  let finishReview!: () => void
  let reviewStarted!: () => void
  const started = new Promise<void>((resolve) => {
    reviewStarted = resolve
  })
  const review = context(
    project,
    () =>
      new Promise<void>((resolve) => {
        finishReview = resolve
        reviewStarted()
      }),
  )
  const pending = dispatchRole(review, reviewer, RESOURCES_DIR)
  await started
  const write = await dispatchRole(
    context(project, () => project.write("src/a.ts", "new")),
    writer,
    RESOURCES_DIR,
  )
  finishReview()
  const result = await pending
  expect(write.scope_violation).toBe(false)
  expect(result).toMatchObject({ scope_violation: false, observed_concurrently: ["src/a.ts"] })
  expect(result.text).toContain("observed_concurrently: src/a.ts")
  expect(result.text).not.toContain("read-only child changed path")
})

it("reports unexplained read-only changes as a violation and retains the child", async () => {
  const project = Project.withCommit()
  const ctx = context(project, () => project.write("src/a.ts", "external edit"))
  const result = await dispatchRole(ctx, reviewer, RESOURCES_DIR)
  expect(result).toMatchObject({ scope_violation: true })
  expect(result.text).toContain("read-only child changed path: src/a.ts")
  expect(ctx.session.remove).not.toHaveBeenCalled()
})

it.each(["prompt", "wait"] as const)("rethrows a rejected %s after checking workspace changes", async (method) => {
  const project = Project.withCommit()
  const ctx = context(project)
  ctx.session[method].mockImplementationOnce(async () => {
    project.write("outside.ts", "unexpected")
    throw new Error("provider 500")
  })
  await expect(dispatchRole(ctx, writer, RESOURCES_DIR)).rejects.toThrow(
    /child session failed:.*provider 500.*outside.ts/s,
  )
  expect(ctx.session.remove).not.toHaveBeenCalled()
})

it("rethrows child failures without manufacturing scope violations", async () => {
  const project = Project.withCommit()
  const ctx = context(project)
  ctx.session.prompt.mockRejectedValueOnce(new Error("provider 500"))
  await expect(dispatchRole(ctx, writer, RESOURCES_DIR)).rejects.toThrow("child session failed: Error: provider 500")
})

it("does not swallow a child rejection without an error value", async () => {
  const project = Project.withCommit()
  const ctx = context(project)
  ctx.session.prompt.mockRejectedValueOnce(undefined)
  await expect(dispatchRole(ctx, writer, RESOURCES_DIR)).rejects.toThrow("child session failed: undefined")
  expect(ctx.session.remove).not.toHaveBeenCalled()
})

it("records a directory replaced by a symlink without following or rejecting it", async () => {
  const project = Project.withCommit()
  const outside = new Project()
  outside.write("a.ts", "outside")
  fs.rmSync(project.path("src"), { recursive: true })
  fs.symlinkSync(outside.root, project.path("src"), "junction")
  const result = await dispatchRole(context(project), reviewer, RESOURCES_DIR)
  expect(result).toMatchObject({ scope_violation: false, workspace_unverified: false })
})

it.each([".git/config", ".git/hooks/new-hook", ".git/info/new-info"])(
  "observes Git administrative bytes: %s",
  async (path) => {
    const project = Project.withCommit()
    const action = () => project.write(path, path.endsWith("config") ? project.read(path) + "\n# edited\n" : "new")
    const result = await dispatchRole(context(project, action), writer, RESOURCES_DIR)
    expect(result.violations).toContainEqual(expect.stringContaining(path))
  },
)

it("compares non-UTF-8 filenames using their original bytes", async () => {
  const project = Project.withCommit()
  const name = Buffer.concat([
    Buffer.from(project.git("rev-parse", "--show-toplevel") + sep),
    Buffer.from([0xff]),
    Buffer.from(".ts"),
  ])
  const actualFs = await vi.importActual<typeof fs>("node:fs")
  const actualChild = await vi.importActual<typeof childProcess>("node:child_process")
  let value = "before"
  vi.mocked(childProcess.spawnSync).mockImplementation(((command: any, args: string[], options: any) => {
    if (args.includes("--others"))
      return { status: 0, stdout: Buffer.concat([Buffer.from([0xff]), Buffer.from(".ts\0")]) }
    return actualChild.spawnSync(command, args, options)
  }) as any)
  vi.mocked(fs.lstatSync).mockImplementation(((path: any, options: any) => {
    if (Buffer.isBuffer(path) && path.equals(name))
      return {
        ...actualFs.lstatSync(project.path("src/a.ts"), options),
        size: BigInt(value.length),
        mtimeNs: BigInt(value.length),
        isFile: () => true,
        isSymbolicLink: () => false,
      }
    return actualFs.lstatSync(path, options)
  }) as any)
  vi.mocked(fs.readFileSync).mockImplementation(((path: any, options: any) => {
    if (Buffer.isBuffer(path) && path.equals(name)) return Buffer.from(value)
    return actualFs.readFileSync(path, options)
  }) as any)
  const result = await dispatchRole(
    context(project, () => {
      value = "after"
    }),
    writer,
    RESOURCES_DIR,
  )
  vi.mocked(childProcess.spawnSync).mockImplementation(actualChild.spawnSync)
  vi.mocked(fs.lstatSync).mockImplementation(actualFs.lstatSync)
  vi.mocked(fs.readFileSync).mockImplementation(actualFs.readFileSync)
  expect(result.violations).toHaveLength(1)
  expect(result.violations[0]).toContain("git-bytes:ff2e7473")
})

it("records submodule HEAD changes even while its dirty status stays the same", async () => {
  const project = Project.withCommit()
  const sub = project.path("vendor/mod")
  project.write("vendor/mod/a", "one")
  childProcess.execFileSync("git", ["init", "-q", sub])
  project.exec("git", ["-C", sub, "-c", "user.name=Test", "-c", "user.email=test@example.com", "add", "a"])
  project.exec("git", [
    "-C",
    sub,
    "-c",
    "user.name=Test",
    "-c",
    "user.email=test@example.com",
    "-c",
    "commit.gpgsign=false",
    "commit",
    "-qm",
    "one",
  ])
  project.git(
    "update-index",
    "--add",
    "--cacheinfo",
    `160000,${project.exec("git", ["-C", sub, "rev-parse", "HEAD"])},vendor/mod`,
  )
  project.commit("submodule")
  project.write("vendor/mod/dirty", "dirty")
  const result = await dispatchRole(
    context(project, () => {
      project.exec("git", [
        "-C",
        sub,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--allow-empty",
        "-qm",
        "two",
      ])
    }),
    writer,
    RESOURCES_DIR,
  )
  expect(result.violations).toContainEqual(expect.stringContaining("vendor/mod"))
})

it("accepts a Git inventory larger than the default subprocess buffer", async () => {
  const project = Project.withCommit()
  const oid = project.git("rev-parse", "HEAD:src/a.ts")
  const listing = Array.from(
    { length: 16000 },
    (_, i) => `100644 ${oid}\tfiles/${String(i).padStart(5, "0")}-${"a".repeat(70)}\n`,
  ).join("")
  childProcess.execFileSync("git", ["update-index", "--index-info"], { cwd: project.root, input: listing })
  expect(await dispatchRole(context(project), reviewer, RESOURCES_DIR)).toMatchObject({ scope_violation: false })
})

it("does not hash ignored inventories or hash unchanged files twice", async () => {
  const project = Project.withCommit({ ".gitignore": "node_modules/\n", "src/a.ts": "a" })
  project.write("node_modules/private/file", "ignored")
  const reads = vi.mocked(fs.readFileSync)
  reads.mockClear()
  await dispatchRole(context(project), reviewer, RESOURCES_DIR)
  const names = reads.mock.calls.map(([path]) => fs.realpathSync.native(path.toString()))
  expect(names.filter((path) => path === fs.realpathSync.native(project.path("src/a.ts")))).toHaveLength(1)
  expect(names).not.toContain(fs.realpathSync.native(project.path("node_modules/private/file")))
})

it("fails closed on Git output overflow without starting a child", async () => {
  const project = Project.withCommit()
  const actual = await vi.importActual<typeof childProcess>("node:child_process")
  vi.mocked(childProcess.spawnSync).mockImplementation(((command: any, args: string[], options: any) => {
    if (args.includes("status"))
      return {
        status: null,
        stdout: Buffer.from("partial"),
        stderr: Buffer.alloc(0),
        error: Object.assign(new Error("overflow"), { code: "ENOBUFS" }),
      }
    return actual.spawnSync(command, args, options)
  }) as any)
  const ctx = context(project)
  await expect(dispatchRole(ctx, reviewer, RESOURCES_DIR)).rejects.toThrow(/Git output exceeded the 64 MiB limit/)
  vi.mocked(childProcess.spawnSync).mockImplementation(actual.spawnSync)
  expect(ctx.session.create).not.toHaveBeenCalled()
})

it("attributes disjoint overlapping writers without losing foreign-change enforcement", async () => {
  const project = Project.withCommit({ "src/a.ts": "a", "docs/b.md": "b" })
  let release!: () => void
  let notify!: () => void
  const started = new Promise<void>((resolve) => {
    notify = resolve
  })
  const first = dispatchRole(
    context(
      project,
      () =>
        new Promise<void>((resolve) => {
          release = resolve
          notify()
        }),
    ),
    writer,
    RESOURCES_DIR,
  )
  await started
  const second = await dispatchRole(
    context(project, () => project.write("docs/b.md", "updated")),
    { ...writer, paths: ["docs"] },
    RESOURCES_DIR,
  )
  release()
  const result = await first
  expect(second.scope_violation).toBe(false)
  expect(result).toMatchObject({ scope_violation: false, observed_concurrently: ["docs/b.md"] })
})

it("reads administrative content from the common Git directory of a linked worktree", async () => {
  const project = Project.withCommit()
  const worktree = new Project()
  project.git("worktree", "add", "--detach", worktree.root, "HEAD")
  const result = await dispatchRole(
    context(worktree, () => project.write(".git/hooks/new-hook", "changed")),
    writer,
    RESOURCES_DIR,
  )
  expect(result.violations).toContainEqual(expect.stringContaining(".git/hooks/new-hook"))
})
