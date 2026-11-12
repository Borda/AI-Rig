/** Permission scope and content-free journal contracts. */
import { chmodSync, readFileSync, statSync } from "node:fs"
import { afterEach, describe, expect, it, vi } from "vitest"
import { sha256 } from "../../../src/digest.ts"
import * as grants from "../../../src/git-grant.ts"
import { DEFAULT_OPTIONS } from "../../../src/options.ts"
import * as pathsModule from "../../../src/paths.ts"
import { registerHooks } from "../../../src/register/hooks.ts"
import { createHarness, Project } from "../../helpers/harness.ts"

afterEach(() => vi.restoreAllMocks())

async function permissionHook() {
  const project = new Project()
  const paths = {
    projectRoot: project.root,
    artifactRoot: project.path("artifacts"),
    runsRoot: project.path("artifacts/runs"),
    statePath: project.path("artifacts/state.json"),
  }
  let callback: (event: any) => unknown = () => {}
  const ctx = {
    permission: {
      hook: async (_: string, hook: (event: any) => unknown) => {
        callback = hook
        return { dispose: async () => {} }
      },
    },
  }
  await registerHooks({ ctx, paths, options: { ...DEFAULT_OPTIONS, journal: false, compactionPreserve: false } })
  return callback
}

describe("grant evaluation", () => {
  it.each(["npm test", "git status", "git push origin main"])("does not resolve a grant for %s", async (command) => {
    const hook = await permissionHook()
    const lookup = vi.spyOn(pathsModule, "resolveGitCommonDir").mockImplementation(() => {
      throw new Error("Git lookup must not run")
    })
    const event: any = { action: "shell", resources: [command], effect: "allow" }
    await hook(event)
    expect(lookup).not.toHaveBeenCalled()
    expect(event.effect).toBe(command.startsWith("git push") ? "ask" : "allow")
  })

  it.each([null, {}, ["git commit -m x", 17], [null]])("asks on malformed shell resources: %j", async (resources) => {
    const hook = await permissionHook()
    const event: any = { action: "execute", resources, effect: "allow" }
    await expect(Promise.resolve().then(() => hook(event))).resolves.toBeUndefined()
    expect(event.effect).toBe("ask")
    expect(event.message).toMatch(/resources/)
  })

  it("fails closed if the grant reader throws", async () => {
    const hook = await permissionHook()
    vi.spyOn(pathsModule, "resolveGitCommonDir").mockReturnValue("unused")
    vi.spyOn(grants, "readGrant").mockImplementation(() => {
      throw new Error("EACCES")
    })
    const event: any = { action: "shell", resources: ["git commit -m x"], effect: "allow" }
    await hook(event)
    expect(event.effect).toBe("ask")
    expect(event.message).toMatch(/grant/)
  })

  it.each([
    ["cd ../other-repo && git commit -m x"],
    ["pushd ../other-repo; git commit -m x"],
    ["c\\d ../other-repo && git commit -m x"],
    ["chdir ../other-repo && git commit -m x"],
    ["CD ../other-repo && git commit -m x"],
    ["Set-Location ../other-repo; git commit -m x"],
    ["sl ../other-repo; git commit -m x"],
    ["git -C ../other-repo commit -m x"],
    ["git --git-dir=../other-repo/.git commit -m x"],
    ["cd ../other-repo", "git commit -m x"],
    ["sudo -D ../other-repo git commit -m x"],
    ["sudo -D../other-repo git commit -m x"],
    ["sudo --chdir ../other-repo git commit -m x"],
    ["sudo --chdir=../other-repo git commit -m x"],
    ["sudo -R ../other-root git commit -m x"],
    ["sudo -R../other-root git commit -m x"],
    ["sudo --chroot ../other-root git commit -m x"],
    ["sudo --chroot=../other-root git commit -m x"],
    ["env -C ../other-repo git commit -m x"],
    ["env -C../other-repo git commit -m x"],
    ["env --chdir ../other-repo git commit -m x"],
    ["env --chdir=../other-repo git commit -m x"],
  ])("never uses this repository's grant across a directory change: %j", async (...resources) => {
    const hook = await permissionHook()
    vi.spyOn(pathsModule, "resolveGitCommonDir").mockReturnValue("unused")
    const grant = vi.spyOn(grants, "readGrant").mockReturnValue({ status: "granted" })
    const event: any = { action: "shell", resources, effect: "allow" }
    await hook(event)
    expect(event.effect).toBe("ask")
    expect(grant).not.toHaveBeenCalled()
  })

  it.each(["grep -C 3 needle source.txt", "echo sl"])("keeps this project's grant beside %s", async (command) => {
    const hook = await permissionHook()
    vi.spyOn(pathsModule, "resolveGitCommonDir").mockReturnValue("unused")
    const grant = vi.spyOn(grants, "readGrant").mockReturnValue({ status: "granted" })
    const event: any = { action: "shell", resources: [command, "git commit -m x"], effect: "allow" }
    await hook(event)
    expect(event.effect).toBe("allow")
    expect(grant).toHaveBeenCalledOnce()
  })
})

describe("tool journal", () => {
  it.each(["password", "apiKey", "access_token", "clientSecret"])(
    "removes the value and digest for %s",
    async (key) => {
      const project = new Project()
      const harness = await createHarness(project)
      const before = harness.toolHooks.get("execute.before")![0]!
      await before({ sessionID: "session-1", tool: "write", id: "sensitive", input: { [key]: "1234", count: 5 } })
      const entry = JSON.parse(project.read(".opencode-rig/journal.jsonl"))
      expect(entry.input).toEqual([
        { key, value: "[redacted]" },
        { key: "count", sha256: sha256("5") },
      ])
      expect(JSON.stringify(entry)).not.toContain(sha256(JSON.stringify("1234")))
    },
  )

  it.each([{ input: "private" }, { input: ["private", 3] }, { input: null }, { input: 7 }])(
    "hashes a non-object input as one value: $input",
    async ({ input }) => {
      const project = new Project()
      const harness = await createHarness(project)
      await harness.toolHooks.get("execute.before")![0]!({ sessionID: "session-1", tool: "write", id: "shape", input })
      expect(JSON.parse(project.read(".opencode-rig/journal.jsonl")).input).toEqual({
        sha256: sha256(JSON.stringify(input)),
      })
    },
  )

  it("records at most fifty object keys", async () => {
    const project = new Project()
    const harness = await createHarness(project)
    const input = Object.fromEntries(Array.from({ length: 51 }, (_, index) => [`field${index}`, index]))
    await harness.toolHooks.get("execute.before")![0]!({ sessionID: "session-1", tool: "write", id: "bounded", input })
    const entry = JSON.parse(project.read(".opencode-rig/journal.jsonl"))
    expect(entry.input).toHaveLength(50)
    expect(entry.input.at(-1)).toEqual({ key: "field49", sha256: sha256("49") })
    expect(JSON.stringify(entry)).not.toContain("field50")
  })

  it.each(["write", "edit"])("records %s argument digests without retaining file contents", async (tool) => {
    const project = new Project()
    const harness = await createHarness(project)
    const content = "private customer text that does not resemble a credential"
    const before = harness.toolHooks.get("execute.before")![0]!
    const after = harness.toolHooks.get("execute.after")![0]!
    const journal = project.path(".opencode-rig/journal.jsonl")
    await before({ sessionID: "session-1", tool, id: "write-1", input: { path: "private.txt", content } })
    const first = readFileSync(journal, "utf8")
    expect(first).not.toContain(content)
    expect(first).not.toContain("private.txt")
    expect(JSON.parse(first).input).toEqual([
      { key: "path", sha256: sha256(JSON.stringify("private.txt")) },
      { key: "content", sha256: sha256(JSON.stringify(content)) },
    ])
    if (process.platform !== "win32") expect(statSync(journal).mode & 0o777).toBe(0o600)
    chmodSync(journal, 0o644)
    await after({ sessionID: "session-1", tool, id: "write-1", status: "error", error: { message: content } })
    expect(readFileSync(journal, "utf8").startsWith(first)).toBe(true)
    expect(readFileSync(journal, "utf8")).not.toContain(content)
    if (process.platform !== "win32") expect(statSync(journal).mode & 0o777).toBe(0o600)
  })
})
