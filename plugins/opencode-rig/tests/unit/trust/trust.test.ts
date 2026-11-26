/** Per-commit trust records: literal answers, bounds and HEAD binding. */
import { execFileSync } from "node:child_process"
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join, resolve } from "node:path"
import { describe, expect, it } from "vitest"
import {
  effectiveCommand,
  isOwnWorktree,
  isReviewWorktree,
  readTrust,
  resolveHead,
  TRUST_WORDS,
  trustFor,
  writeTrust,
} from "../../../src/trust.ts"
import { Project } from "../../helpers/harness.ts"

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

const REF = "refs/rig-review/pr-563-0123456789ab"

describe("trust records", () => {
  it("preserves live trust across recreation at the same path and commit", () => {
    const project = Project.withCommit()
    const commit = project.git("rev-parse", "HEAD")
    const artifactRoot = project.path("artifacts")
    mkdirSync(join(tmpdir(), "opencode"), { recursive: true })
    const worktree = mkdtempSync(join(tmpdir(), "opencode/review-"))
    try {
      project.git("worktree", "add", "--detach", worktree, commit)
      writeTrust(artifactRoot, { commit, worktree, ref: REF, words: TRUST_WORDS })
      project.git("worktree", "remove", worktree)
      expect(trustFor(artifactRoot, worktree).status).toBe("absent")
      project.git("worktree", "add", "--detach", worktree, commit)
      expect(trustFor(artifactRoot, worktree).status).toBe("trusted")
    } finally {
      rmSync(worktree, { recursive: true, force: true })
    }
  })

  it("records a matching commit and resolves it for its worktree", () => {
    const review = reviewRepo()
    try {
      const project = Project.withCommit()
      const record = writeTrust(project.path("artifacts"), {
        commit: review.commit,
        worktree: review.root,
        ref: REF,
        words: TRUST_WORDS,
      })
      expect(record.commit).toBe(review.commit)
      expect(readTrust(project.path("artifacts"), review.commit)).toHaveLength(1)
      expect(trustFor(project.path("artifacts"), review.root)).toMatchObject({ status: "trusted" })
    } finally {
      rmSync(review.root, { recursive: true, force: true })
    }
  })

  it("refuses a different commit, a foreign path, a bad ref and a missing literal answer", () => {
    const review = reviewRepo()
    try {
      const artifactRoot = Project.withCommit().path("artifacts")
      const base = { commit: review.commit, worktree: review.root, ref: REF, words: TRUST_WORDS }
      expect(() => writeTrust(artifactRoot, { ...base, commit: "0".repeat(40) })).toThrow(/HEAD/)
      expect(() => writeTrust(artifactRoot, { ...base, commit: "nope" })).toThrow(/40-hex/)
      expect(() => writeTrust(artifactRoot, { ...base, worktree: "/tmp/elsewhere" })).toThrow(/review root|review-/)
      expect(() => writeTrust(artifactRoot, { ...base, ref: "refs/heads/main" })).toThrow(/review ref/)
      expect(() => writeTrust(artifactRoot, { ...base, words: "yes" })).toThrow(/Trust this PR/)
    } finally {
      rmSync(review.root, { recursive: true, force: true })
    }
  })

  it("ignores a hand-written record missing the words and ref", () => {
    const review = reviewRepo()
    try {
      const project = Project.withCommit()
      const artifactRoot = project.path("artifacts")
      mkdirSync(join(artifactRoot, "trust"), { recursive: true })
      writeFileSync(
        join(artifactRoot, "trust", `${review.commit}.json`),
        JSON.stringify({
          schema: 1,
          commit: review.commit,
          worktree: review.root,
          expires_at: "2099-01-01T00:00:00.000Z",
        }),
      )
      expect(readTrust(artifactRoot)).toHaveLength(0)
      expect(trustFor(artifactRoot, review.root).status).toBe("absent")
    } finally {
      rmSync(review.root, { recursive: true, force: true })
    }
  })

  it("expires and stops matching after the worktree commit changes", () => {
    const review = reviewRepo()
    try {
      const project = Project.withCommit()
      const artifactRoot = project.path("artifacts")
      writeTrust(artifactRoot, {
        commit: review.commit,
        worktree: review.root,
        ref: REF,
        words: TRUST_WORDS,
        ttlMs: -1,
      })
      expect(trustFor(artifactRoot, review.root)).toMatchObject({ status: "expired" })
      writeTrust(artifactRoot, { commit: review.commit, worktree: review.root, ref: REF, words: TRUST_WORDS })
      writeFileSync(join(review.root, "next.txt"), "next\n")
      execFileSync("git", ["add", "-A"], { cwd: review.root, stdio: "ignore" })
      execFileSync("git", ["commit", "-qm", "next"], { cwd: review.root, stdio: "ignore" })
      expect(trustFor(artifactRoot, review.root)).toMatchObject({ status: "mismatched-commit" })
    } finally {
      rmSync(review.root, { recursive: true, force: true })
    }
  })
})

describe("effective command and own worktrees", () => {
  it.each([";", "|", "||", "\n", "&"])("refuses a directory prefix joined by %j", (operator) => {
    const directory = process.cwd().replaceAll("\\", "/")
    expect(effectiveCommand(`cd "${directory}" ${operator} pytest`, directory)).toBeUndefined()
  })

  it("parses one leading cd and leaves a plain command in the project root", () => {
    expect(effectiveCommand("pytest tests -q", "/repo")).toEqual({
      directory: "/repo",
      command: "pytest tests -q",
      prefixed: false,
    })
    expect(effectiveCommand("cd /tmp/opencode/review-x && pytest tests -q", "/repo")).toEqual({
      directory: resolve("/tmp/opencode/review-x"),
      command: "pytest tests -q",
      prefixed: true,
    })
    expect(effectiveCommand("cd relative && pytest", "/repo")).toBeUndefined()
    expect(effectiveCommand("cd /tmp/x &&", "/repo")).toBeUndefined()
  })

  it("recognizes the review root and an own commit", () => {
    expect(isReviewWorktree("/tmp/opencode/review-pr1", "/tmp")).toBe(true)
    expect(isReviewWorktree("/tmp/opencode/review-pr1/nested", "/tmp")).toBe(false)
    expect(isReviewWorktree("/tmp/opencode/other", "/tmp")).toBe(false)
    const project = Project.withCommit()
    expect(resolveHead(project.root)).toBe(project.git("rev-parse", "HEAD"))
    expect(isOwnWorktree(project.root, project.root)).toBe(true)
    const foreign = reviewRepo()
    try {
      expect(isOwnWorktree(project.root, foreign.root)).toBe(false)
    } finally {
      rmSync(foreign.root, { recursive: true, force: true })
    }
  })
})
