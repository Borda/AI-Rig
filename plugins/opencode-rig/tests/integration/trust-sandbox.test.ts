/** rig_trust and rig_sandbox: the forked-PR decision and the container boundary. */
import { execFileSync, spawn, spawnSync } from "node:child_process"
import {
  existsSync,
  lstatSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  realpathSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs"
import { tmpdir } from "node:os"
import { dirname, join } from "node:path"
import { afterEach, describe, expect, it, vi } from "vitest"
import { TRUST_WORDS } from "../../src/trust.ts"
import { Project, createHarness, track } from "../helpers/harness.ts"

vi.mock("node:child_process", async (original) => {
  const actual = await original<typeof import("node:child_process")>()
  return { ...actual, spawn: vi.fn(actual.spawn), spawnSync: vi.fn(actual.spawnSync) }
})
vi.mock("node:fs", async (original) => {
  const actual = await original<typeof import("node:fs")>()
  return { ...actual, rmSync: vi.fn(actual.rmSync) }
})

const actualProcess = await vi.importActual<typeof import("node:child_process")>("node:child_process")
const actualFs = await vi.importActual<typeof import("node:fs")>("node:fs")
const pinned = `repo@sha256:${"a".repeat(64)}`
afterEach(() => vi.resetAllMocks())

/** Use a real local child for the process lifecycle while substituting the external container engine. */
function stubEngine(
  ready: boolean,
  payload: string | null = "process.exit(0)",
  onRun?: (args: readonly string[]) => void,
) {
  vi.mocked(spawnSync).mockImplementation((program, args, options) => {
    if (program !== "docker" && program !== "podman") return actualProcess.spawnSync(program, args, options)
    const status = args?.[0] === "--version" || (ready && program === "docker") ? 0 : 125
    const stdout = args?.[0] === "image" ? `sha256:${"b".repeat(64)}` : Buffer.alloc(0)
    return { status, pid: 0, signal: null, output: [], stdout, stderr: Buffer.alloc(0) }
  })
  vi.mocked(spawn).mockImplementation((program, args, options) => {
    if (program !== "docker" && program !== "podman") return actualProcess.spawn(program, args, options)
    onRun?.(args ?? [])
    if (payload === null) return actualProcess.spawn(join(tmpdir(), "missing-engine", "docker"), [], options)
    return actualProcess.spawn(process.execPath, ["-e", payload], options)
  })
}

function reviewRepo(): { root: string; commit: string } {
  mkdirSync(join(tmpdir(), "opencode"), { recursive: true })
  const root = mkdtempSync(join(tmpdir(), "opencode/review-"))
  const run = (...args: string[]) => execFileSync("git", args, { cwd: root, encoding: "utf8", stdio: "ignore" })
  run("init", "-q")
  run("config", "user.email", "rig@example.com")
  run("config", "user.name", "Rig Test")
  run("config", "commit.gpgsign", "false")
  writeFileSync(join(root, "probe.txt"), "third-party\n")
  run("add", "-A")
  run("commit", "-qm", "third-party")
  return { root, commit: execFileSync("git", ["rev-parse", "HEAD"], { cwd: root, encoding: "utf8" }).trim() }
}

describe("rig_trust", () => {
  it("records one commit only after the literal answer and reports it", async () => {
    const project = Project.withCommit()
    project.write(".gitignore", ".opencode-rig/\n")
    const harness = await createHarness(project)
    const review = reviewRepo()
    try {
      await expect(
        harness.call("rig_trust", {
          action: "approve",
          commit: review.commit,
          worktree: review.root,
          ref: "refs/rig-review/pr-563-0123456789ab",
          confirm: "yes",
        }),
      ).rejects.toThrow(/Trust this PR/)
      const approved = await harness.call("rig_trust", {
        action: "approve",
        commit: review.commit,
        worktree: review.root,
        ref: "refs/rig-review/pr-563-0123456789ab",
        confirm: TRUST_WORDS,
      })
      expect(approved.content).toContain(review.commit)
      expect(existsSync(project.path(`.opencode-rig/trust/${review.commit}.json`))).toBe(true)
      expect(harness.assertions.at(-1)?.action).toBe("rig_trust")
      const status = await harness.call("rig_trust", { action: "status" })
      expect(status.content).toContain(review.commit)
    } finally {
      rmSync(review.root, { recursive: true, force: true })
    }
  })

  it.each([{ execution: "all-sandbox" }, { inspection: false }, { guardrails: "off" }])(
    "refuses a trust record the active policy cannot honor (%j)",
    async (options) => {
      const project = Project.withCommit()
      const harness = await createHarness(project, options)
      const review = reviewRepo()
      track(() => rmSync(review.root, { recursive: true, force: true }))
      await expect(
        harness.call("rig_trust", {
          action: "approve",
          commit: review.commit,
          worktree: review.root,
          ref: "refs/rig-review/pr-1-0123456789ab",
          confirm: TRUST_WORDS,
        }),
      ).rejects.toThrow(/active execution policy/)
      expect(project.exists(`.opencode-rig/trust/${review.commit}.json`)).toBe(false)
      expect(harness.assertions).toHaveLength(0)
    },
  )
})

describe("rig_sandbox", () => {
  it("rejects an empty command", async () => {
    const project = Project.withCommit()
    project.write(".gitignore", ".opencode-rig/\n")
    const harness = await createHarness(project)
    await expect(harness.call("rig_sandbox", { worktree: project.root, argv: [] })).rejects.toThrow(/argv/)
  })

  it("refuses a worktree outside the project or review root before any runtime work", async () => {
    const project = Project.withCommit()
    project.write(".gitignore", ".opencode-rig/\n")
    const harness = await createHarness(project)
    await expect(
      harness.call("rig_sandbox", {
        worktree: "/etc",
        argv: ["true"],
        image: `repo@sha256:${"a".repeat(64)}`,
      }),
    ).rejects.toThrow(/project root or a review worktree/)
    await expect(
      harness.call("rig_sandbox", {
        worktree: join(project.root, "missing-worktree"),
        argv: ["true"],
        image: `repo@sha256:${"a".repeat(64)}`,
      }),
    ).rejects.toThrow(/project root or a review worktree/)
  })

  describe.each([
    { prerequisite: "image", image: undefined, reason: /no sandbox image is configured/i },
    {
      prerequisite: "runtime",
      image: `repo@sha256:${"a".repeat(64)}`,
      reason: /no container runtime .* reachable/i,
    },
  ])("when the sandbox $prerequisite is unavailable", ({ prerequisite, image, reason }) => {
    it.each([
      { scenario: "fork-sandbox review worktree", options: {}, root: "review", trust: true },
      { scenario: "host review worktree", options: { execution: "host" }, root: "review", trust: true },
      { scenario: "all-sandbox review worktree", options: { execution: "all-sandbox" }, root: "review", trust: false },
      { scenario: "project root", options: {}, root: "project", trust: false },
      { scenario: "review directory without a commit", options: {}, root: "empty-review", trust: false },
      { scenario: "inspection disabled", options: { inspection: false }, root: "review", trust: false },
      { scenario: "guardrails disabled", options: { guardrails: "off" }, root: "review", trust: false },
      { scenario: "unmodeled command", options: {}, root: "review", trust: false, argv: ["true"] },
      { scenario: "read-only command", options: {}, root: "review", trust: false, argv: ["git", "status"] },
      { scenario: "ambiguous shell argument", options: {}, root: "review", trust: false, argv: ["npm test"] },
      {
        scenario: "assignment-shaped executable",
        options: {},
        root: "review",
        trust: false,
        argv: ["MODE=test", "npm", "test"],
      },
      {
        scenario: "out-of-tree verification operand",
        options: {},
        root: "review",
        trust: false,
        argv: ["pytest", "../outside"],
      },
    ])("reports the available next action for $scenario", async ({ options, root, trust, argv = ["npm", "test"] }) => {
      const project = Project.withCommit({ ".gitignore": ".opencode-rig/\n" })
      const harness = await createHarness(project, options)
      const worktree = sandboxWorktree(project, root)
      stubEngine(false)
      const result = await harness.call("rig_sandbox", { worktree, argv, image })
      const expected = trust ? "request-trust" : root === "project" ? "run-on-host" : "mark-incomplete"
      const receipt = JSON.parse(result.content)
      expect(receipt).toMatchObject({
        status: "unavailable",
        disposition: `not-run:${prerequisite}`,
        next_action: { kind: expected },
        network: "none",
      })
      expect(receipt.reason).toMatch(reason)
      expect(receipt.next_action.command).toEqual(
        expected === "mark-incomplete" ? undefined : expect.stringContaining("&& npm test"),
      )
      expect(JSON.parse(readFileSync(result.metadata.receipt_path, "utf8"))).toEqual(receipt)
      expect(spawn).not.toHaveBeenCalled()
    })
  })

  it.each(["recorded trust", "own worktree"])("reuses %s when the runtime is unavailable", async (authority) => {
    const project = Project.withCommit({ ".gitignore": ".opencode-rig/\n" })
    const harness = await createHarness(project)
    const worktree = sandboxWorktree(project, authority === "recorded trust" ? "review" : "empty-review")
    if (authority === "recorded trust") {
      const commit = project.git("-C", worktree, "rev-parse", "HEAD")
      await harness.call("rig_trust", {
        action: "approve",
        commit,
        worktree,
        ref: "refs/rig-review/pr-1-0123456789ab",
        confirm: TRUST_WORDS,
      })
    } else {
      project.git("worktree", "add", "--detach", worktree, "HEAD")
    }
    stubEngine(false)
    const { content } = await harness.call("rig_sandbox", { worktree, argv: ["npm", "test"], image: pinned })
    expect(JSON.parse(content)).toMatchObject({
      disposition: "not-run:runtime",
      next_action: { kind: "run-on-host", command: expect.stringContaining("&& npm test") },
    })
  })

  it("mounts a filtered physical copy of the directory named in the authorization", async () => {
    const project = Project.withCommit({ ".env": "filtered\n" })
    const harness = await createHarness(project)
    const alias = new Project().path("alias")
    symlinkSync(project.root, alias, "junction")
    let mounted: { directory: boolean; filtered: boolean } | undefined
    stubEngine(true, "process.exit(0)", (args) => {
      const source = args[args.indexOf("-v") + 1]!.slice(0, -":/work".length)
      mounted = { directory: lstatSync(source).isDirectory(), filtered: !existsSync(join(source, ".env")) }
    })
    const { content } = await harness.call("rig_sandbox", { worktree: alias, argv: ["npm", "test"], image: pinned })
    expect(mounted).toEqual({ directory: true, filtered: true })
    const authorized = realpathSync.native(project.root)
    expect(harness.assertions.at(-1)?.resources).toEqual([authorized])
    expect(JSON.parse(content)).toMatchObject({ worktree: authorized, disposition: "ran-passed" })
  })

  it.each([
    { code: 0, network: true, disposition: "ran-passed", action: "record-result" },
    { code: 1, network: false, disposition: "ran-failed", action: "record-result" },
    ...[125, 126, 127].map((code) => ({
      code,
      network: false,
      disposition: "not-run:launch",
      action: "mark-incomplete",
    })),
  ])(
    "records $disposition for engine exit $code with the effective network setting",
    async ({ code, network, disposition, action }) => {
      const project = Project.withCommit()
      const harness = await createHarness(project)
      const networkMode = network ? "bridge" : "none"
      stubEngine(true, `process.exit(${code})`)
      const { content } = await harness.call("rig_sandbox", {
        worktree: project.root,
        argv: ["npm", "test"],
        image: pinned,
        network,
      })
      expect(spawn).toHaveBeenCalledWith(
        "docker",
        expect.arrayContaining(["--network", networkMode]),
        expect.anything(),
      )
      expect(JSON.parse(content)).toMatchObject({
        disposition,
        exit_code: code,
        network: networkMode,
        next_action: { kind: action },
      })
    },
  )

  it("returns an inline setup receipt when the artifact directory is unusable", async () => {
    const project = Project.withCommit()
    const harness = await createHarness(project)
    project.write(".opencode-rig/sandbox", "file in place of log directory")
    stubEngine(true)
    const { content, metadata } = await harness.call("rig_sandbox", {
      worktree: project.root,
      argv: ["npm", "test"],
      image: pinned,
    })
    expect(JSON.parse(content)).toMatchObject({
      disposition: "not-run:setup",
      next_action: { kind: "mark-incomplete" },
    })
    expect(metadata.receipt_error).toBeDefined()
    expect(spawn).not.toHaveBeenCalled()
  })

  it("records a setup failure when the authorized path cannot be copied as a directory", async () => {
    const project = Project.withCommit()
    const harness = await createHarness(project)
    const worktree = sandboxWorktree(project, "empty-review")
    rmSync(worktree, { recursive: true })
    writeFileSync(worktree, "file in place of directory")
    stubEngine(true)
    const { content, metadata } = await harness.call("rig_sandbox", { worktree, argv: ["npm", "test"], image: pinned })
    expect(JSON.parse(content)).toMatchObject({
      disposition: "not-run:setup",
      reason: expect.stringContaining("real directory"),
      next_action: { kind: "mark-incomplete" },
    })
    expect(JSON.parse(readFileSync(metadata.receipt_path, "utf8"))).toEqual(JSON.parse(content))
    expect(spawn).not.toHaveBeenCalled()
  })

  it.each([
    { event: "engine disappearance after readiness probe", payload: null, disposition: "not-run:launch" },
    { event: "timeout", payload: "setInterval(() => {}, 500)", disposition: "not-run:timeout" },
  ])("returns an incomplete receipt for $event", async ({ payload, disposition }) => {
    const project = Project.withCommit()
    const harness = await createHarness(project)
    stubEngine(true, payload)
    const { content } = await harness.call("rig_sandbox", {
      worktree: project.root,
      argv: ["npm", "test"],
      image: pinned,
      timeout_ms: 1_000,
    })
    expect(JSON.parse(content)).toMatchObject({
      disposition,
      exit_code: null,
      next_action: { kind: "mark-incomplete" },
    })
  })

  it("settles with the execution outcome and cleanup error when copy deletion fails", async () => {
    const project = Project.withCommit()
    const harness = await createHarness(project)
    stubEngine(true, "process.exit(0)", (args) => {
      const root = dirname(args[args.indexOf("-v") + 1]!.slice(0, -":/work".length))
      track(() => actualFs.rmSync(root, { force: true, recursive: true }))
      vi.mocked(rmSync).mockImplementation((path, options) => {
        if (path === root) throw Object.assign(new Error("cleanup denied"), { code: "EACCES" })
        return actualFs.rmSync(path, options)
      })
    })
    const { content, metadata } = await harness.call("rig_sandbox", {
      worktree: project.root,
      argv: ["npm", "test"],
      image: pinned,
    })
    expect(JSON.parse(content)).toMatchObject({
      disposition: "ran-passed",
      cleanup_error: expect.stringContaining("cleanup denied"),
      next_action: { kind: "record-result" },
    })
    expect(JSON.parse(readFileSync(metadata.receipt_path, "utf8"))).toEqual(JSON.parse(content))
  }, 3_000)
})

/** Arrange the checkout, a committed review worktree, or an empty directory with the same shape. */
function sandboxWorktree(project: Project, root: string): string {
  if (root === "project") return project.root
  mkdirSync(join(tmpdir(), "opencode"), { recursive: true })
  const worktree = root === "review" ? reviewRepo().root : mkdtempSync(join(tmpdir(), "opencode/review-"))
  track(() => rmSync(worktree, { recursive: true, force: true }))
  return worktree
}
