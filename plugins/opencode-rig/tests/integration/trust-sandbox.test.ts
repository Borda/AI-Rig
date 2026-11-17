/** rig_trust and rig_sandbox: the forked-PR decision and the container boundary. */
import { execFileSync } from "node:child_process"
import { existsSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { describe, expect, it } from "vitest"
import { detectRuntime } from "../../src/sandbox.ts"
import { TRUST_WORDS } from "../../src/trust.ts"
import { Project, createHarness } from "../helpers/harness.ts"

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

  it("reports the verification unavailable when no runtime exists", async () => {
    const project = Project.withCommit()
    project.write(".gitignore", ".opencode-rig/\n")
    const harness = await createHarness(project)
    const original = process.env.PATH
    process.env.PATH = ""
    try {
      expect(detectRuntime()).toBeUndefined()
      project.write(".gitignore", ".opencode-rig/\n")
      const harness = await createHarness(project)
      const result = await harness.call("rig_sandbox", {
        worktree: project.root,
        argv: ["true"],
        image: `repo@sha256:${"a".repeat(64)}`,
      })
      expect(result.metadata.status).toBe("unavailable")
      expect(result.content).toMatch(/unverified/)
      expect(existsSync(result.metadata.receipt_path as string)).toBe(true)
    } finally {
      process.env.PATH = original
    }
  })
})
