/** Permission scope and content-free journal contracts. */
import { execFileSync } from "node:child_process"
import { chmodSync, mkdirSync, mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { TRUST_WORDS, writeTrust } from "../../../src/trust.ts"
import { afterEach, describe, expect, it, vi } from "vitest"
import { sha256 } from "../../../src/digest.ts"
import * as grants from "../../../src/git-grant.ts"
import { DEFAULT_OPTIONS } from "../../../src/options.ts"
import * as pathsModule from "../../../src/paths.ts"
import { registerHooks } from "../../../src/register/hooks.ts"
import { createHarness, RESOURCES_DIR, Project } from "../../helpers/harness.ts"

afterEach(() => vi.restoreAllMocks())

async function permissionHook(overrides: Partial<typeof DEFAULT_OPTIONS> = {}, sharedProject?: Project) {
  const project = sharedProject ?? new Project()
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
  await registerHooks({
    ctx,
    paths,
    options: { ...DEFAULT_OPTIONS, journal: false, compactionPreserve: false, ...overrides },
    resourcesDir: RESOURCES_DIR,
  })
  return callback
}

describe("inspection allow", () => {
  it.each(["sed -n 1,20p src/a.ts", "grep -n x -A 40 src/a.ts", "git diff HEAD --stat", "uv run pytest tests -q"])(
    "allows positively modelled inspection: %s",
    async (command) => {
      const hook = await permissionHook()
      const event: any = { action: "shell", resources: [command], effect: "ask" }
      await hook(event)
      expect(event.effect).toBe("allow")
      expect(event.message).toMatch(/inspection|verification/)
    },
  )

  it.each([
    "cat src/a.ts > out.txt",
    "sort -o out.txt src/a.ts",
    "git rm src/a.ts",
    "bash script.sh",
    "ls && ./evil",
    "./cat src/a.ts",
  ])("leaves a write or exec command to the rules: %s", async (command) => {
    const hook = await permissionHook()
    const event: any = { action: "shell", resources: [command], effect: "ask" }
    await hook(event)
    expect(event.effect).toBe("ask")
  })

  it("never overrides a host deny", async () => {
    const hook = await permissionHook()
    const event: any = { action: "shell", resources: ["git diff HEAD --stat"], effect: "deny" }
    await hook(event)
    expect(event.effect).toBe("deny")
  })

  it("leaves inspection to the rules when the option is off", async () => {
    const hook = await permissionHook({ inspection: false })
    const event: any = { action: "shell", resources: ["git diff HEAD --stat"], effect: "ask" }
    await hook(event)
    expect(event.effect).toBe("ask")
  })

  it("asks when one resource in the event fails the positive model", async () => {
    const hook = await permissionHook()
    const event: any = { action: "shell", resources: ["cat src/a.ts", "cat src/a.ts > out.txt"], effect: "ask" }
    await hook(event)
    expect(event.effect).toBe("ask")
  })

  it("does not apply the inspection allow to execute events", async () => {
    const hook = await permissionHook()
    const event: any = { action: "execute", resources: ["git diff HEAD --stat"], effect: "ask" }
    await hook(event)
    expect(event.effect).toBe("ask")
  })
})

describe("designed Git writes", () => {
  const sha = "0123456789abcdef0123456789abcdef01234567"
  const fetchForm =
    "git fetch --no-tags --no-write-fetch-head --no-auto-maintenance https://example.invalid/o/r.git refs/pull/563/head:refs/rig-review/pr-563-0123456789ab"

  it.each([
    fetchForm,
    `git worktree add --detach ${join(tmpdir(), "opencode/review-pr563")} ${sha}`,
    `git worktree remove ${join(tmpdir(), "opencode/review-pr563")}`,
    "git add src/a.ts",
  ])("allows %s", async (command) => {
    const hook = await permissionHook()
    const event: any = { action: "shell", resources: [command], effect: "ask" }
    await hook(event)
    expect(event.effect).toBe("allow")
    expect(event.message).toMatch(/designed Git write/)
  })

  it.each(["git fetch origin", "git worktree remove /tmp/elsewhere", "git add -A", "git add src/a.ts && rm -rf src"])(
    "leaves %s to the rules",
    async (command) => {
      const hook = await permissionHook()
      const event: any = { action: "shell", resources: [command], effect: "ask" }
      await hook(event)
      expect(event.effect).toBe("ask")
    },
  )

  it("never overrides a host deny", async () => {
    const hook = await permissionHook()
    const event: any = { action: "shell", resources: ["git add src/a.ts"], effect: "deny" }
    await hook(event)
    expect(event.effect).toBe("deny")
  })

  it("is disabled by designedWrites: false", async () => {
    const hook = await permissionHook({ designedWrites: false })
    const event: any = { action: "shell", resources: ["git add src/a.ts"], effect: "ask" }
    await hook(event)
    expect(event.effect).toBe("ask")
  })
})

describe("all-sandbox execution mode", () => {
  it("refuses host verification even in the project root but keeps inspection", async () => {
    const project = Project.withCommit()
    const hook = await permissionHook({ execution: "all-sandbox" }, project)
    const verification: any = { action: "shell", resources: ["python -m pytest tests -q"], effect: "ask" }
    await hook(verification)
    expect(verification.effect).toBe("ask")
    const inspection: any = { action: "shell", resources: ["git status --porcelain"], effect: "ask" }
    await hook(inspection)
    expect(inspection.effect).toBe("allow")
    const alreadyAllowed: any = { action: "shell", resources: ["python -m pytest tests -q"], effect: "allow" }
    await hook(alreadyAllowed)
    expect(alreadyAllowed.effect).toBe("ask")
  })
})

describe("verification trust", () => {
  function reviewRepo(): { root: string; commit: string } {
    mkdirSync(join(tmpdir(), "opencode"), { recursive: true })
    const root = mkdtempSync(join(tmpdir(), "opencode/review-"))
    const run = (...args: string[]) => execFileSync("git", args, { cwd: root, encoding: "utf8", stdio: "ignore" })
    run("init", "-q")
    run("config", "user.email", "rig@example.com")
    run("config", "user.name", "Rig Test")
    run("config", "commit.gpgsign", "false")
    writeFileSync(join(root, "probe.txt"), "third-party code\n")
    run("add", "-A")
    run("commit", "-qm", "third-party")
    return { root, commit: execFileSync("git", ["rev-parse", "HEAD"], { cwd: root, encoding: "utf8" }).trim() }
  }

  async function ask(command: string, project: Project) {
    const hook = await permissionHook({}, project)
    const event: any = { action: "shell", resources: [command], effect: "ask" }
    await hook(event)
    return event
  }

  it("allows read-only inspection in a fetched worktree but asks for verification", async () => {
    const project = Project.withCommit()
    const review = reviewRepo()
    try {
      expect((await ask(`cd ${review.root} && git diff --stat`, project)).effect).toBe("allow")
      expect((await ask(`cd ${review.root} && python -m pytest tests -q`, project)).effect).toBe("ask")
      expect((await ask("cat ../../etc/passwd", project)).effect).toBe("ask")
      expect((await ask("cd /etc && cat hosts", project)).effect).toBe("ask")
    } finally {
      rmSync(review.root, { recursive: true, force: true })
    }
  })

  it("allows verification after a matching per-commit trust record", async () => {
    const project = Project.withCommit()
    const review = reviewRepo()
    try {
      writeTrust(project.path("artifacts"), {
        commit: review.commit,
        worktree: review.root,
        ref: "refs/rig-review/pr-563-0123456789ab",
        words: TRUST_WORDS,
      })
      const event = await ask(`cd ${review.root} && python -m pytest tests -q`, project)
      expect(event.effect).toBe("allow")
      expect(event.message).toMatch(/trusted/)
    } finally {
      rmSync(review.root, { recursive: true, force: true })
    }
  })

  it("asks again after the worktree commit changes", async () => {
    const project = Project.withCommit()
    const review = reviewRepo()
    try {
      writeTrust(project.path("artifacts"), {
        commit: review.commit,
        worktree: review.root,
        ref: "refs/rig-review/pr-563-0123456789ab",
        words: TRUST_WORDS,
      })
      writeFileSync(join(review.root, "next.txt"), "next\n")
      execFileSync("git", ["add", "-A"], { cwd: review.root, stdio: "ignore" })
      execFileSync("git", ["commit", "-qm", "next"], { cwd: review.root, stdio: "ignore" })
      expect((await ask(`cd ${review.root} && python -m pytest tests -q`, project)).effect).toBe("ask")
    } finally {
      rmSync(review.root, { recursive: true, force: true })
    }
  })

  it("allows verification in the project root without a record", async () => {
    const project = Project.withCommit()
    expect((await ask("python -m pytest tests -q", project)).effect).toBe("allow")
    expect((await ask(`cd ${project.root} && python -m pytest tests -q`, project)).effect).toBe("allow")
  })

  it("allows verification in a project subdirectory", async () => {
    const project = Project.withCommit()
    mkdirSync(project.path("packages/api"), { recursive: true })
    expect((await ask(`cd ${project.root}/packages/api && python -m pytest -q`, project)).effect).toBe("allow")
  })

  it("refuses a verification hidden behind a proven compound prefix", async () => {
    const project = Project.withCommit()
    const review = reviewRepo()
    try {
      expect((await ask(`echo ok && cd ${review.root} && python -m pytest tests -q`, project)).effect).toBe("ask")
      expect((await ask(`cd ${review.root} && python -m pytest tests -q && cd ${project.root}`, project)).effect).toBe(
        "ask",
      )
    } finally {
      rmSync(review.root, { recursive: true, force: true })
    }
  })
})

describe("shipped command blocks", () => {
  it("allows an exact shipped command and a compound of shipped fragments", async () => {
    const hook = await permissionHook()
    const single: any = { action: "shell", resources: ["test -f package.json"], effect: "ask" }
    await hook(single)
    expect(single.effect).toBe("allow")
    expect(single.message).toMatch(/shipped command block/)

    const compound: any = {
      action: "shell",
      resources: ["git status --porcelain\ngit diff --stat"],
      effect: "ask",
    }
    await hook(compound)
    expect(compound.effect).toBe("allow")
  })

  it("asks when a fragment is not shipped or the option is off", async () => {
    const hook = await permissionHook()
    const compound: any = { action: "shell", resources: ["git status --porcelain && git push"], effect: "ask" }
    await hook(compound)
    expect(compound.effect).toBe("ask")

    const off = await permissionHook({ blueprint: false })
    const command: any = { action: "shell", resources: ["test -f package.json"], effect: "ask" }
    await off(command)
    expect(command.effect).toBe("ask")
  })

  it("never overrides a host deny", async () => {
    const hook = await permissionHook()
    const event: any = { action: "shell", resources: ["test -f package.json"], effect: "deny" }
    await hook(event)
    expect(event.effect).toBe("deny")
  })

  it("does not let a non-inspection shipped block bypass all-sandbox", async () => {
    const hook = await permissionHook({ execution: "all-sandbox" })
    const event: any = { action: "shell", resources: ["test -f package.json"], effect: "ask" }
    await hook(event)
    expect(event.effect).toBe("ask")
  })

  it("refuses non-ASCII whitespace and heredoc bodies", async () => {
    const hook = await permissionHook()
    const injected: any = { action: "shell", resources: ["git diff\u000b#; echo INJECTED"], effect: "ask" }
    await hook(injected)
    expect(injected.effect).toBe("ask")
    const heredoc: any = { action: "shell", resources: ["cat <<\\EOF\nrm -rf x\nEOF\n"], effect: "ask" }
    await hook(heredoc)
    expect(heredoc.effect).toBe("ask")
  })
})

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
