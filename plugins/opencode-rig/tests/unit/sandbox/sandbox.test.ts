/** Sandbox invocation: runtime detection, container shape and bounded input. */
import { existsSync, mkdtempSync, readFileSync, rmSync, symlinkSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { describe, expect, it } from "vitest"
import {
  copyWorktree,
  detectRuntime,
  isPinnedImage,
  runInSandbox,
  sandboxArgs,
  SandboxError,
} from "../../../src/sandbox.ts"
import { Project } from "../../helpers/harness.ts"

describe("sandbox runtime", () => {
  it("detects no runtime with an empty PATH", () => {
    expect(detectRuntime({ PATH: "" })).toBeUndefined()
  })

  it("builds a capped, network-off container invocation", () => {
    const args = sandboxArgs({
      runtime: "docker",
      image: "python:3.12-slim",
      name: "rig-1",
      workDirectory: "/tmp/opencode/sandbox-1/work",
      argv: ["python", "-m", "pytest", "tests", "-q"],
      network: false,
    })
    expect(args).toEqual([
      "run",
      "--rm",
      "--name",
      "rig-1",
      "--network",
      "none",
      "--cpus",
      "2",
      "--memory",
      "2g",
      "--pids-limit",
      "512",
      "-v",
      "/tmp/opencode/sandbox-1/work:/work",
      "-w",
      "/work",
      "--entrypoint",
      "python",
      "python:3.12-slim",
      "-m",
      "pytest",
      "tests",
      "-q",
    ])
    expect(
      sandboxArgs({ runtime: "podman", image: "x", name: "n", workDirectory: "/w", argv: ["x"], network: true }),
    ).toContain("bridge")
  })

  it.each(["source", "destination"])("refuses a symlinked copy %s", (position) => {
    const project = Project.withCommit()
    const temporary = new Project()
    const link = temporary.path("link")
    symlinkSync(project.root, link, "junction")
    const source = position === "source" ? link : project.root
    const destination = position === "destination" ? link : temporary.path("copy")
    expect(() => copyWorktree(source, destination)).toThrow(`copy ${position} must be a real directory`)
    expect(existsSync(project.path("src/a.ts"))).toBe(true)
  })

  it("copies the worktree without any Git metadata", () => {
    const project = Project.withCommit()
    const copied = copyWorktree(project.root, mkdtempSync(join(tmpdir(), "copied-")))
    const linkedCopy = mkdtempSync(join(tmpdir(), "linked-"))
    try {
      expect(readFileSync(join(copied, "src/a.ts"), "utf8")).toContain("export const a")
      expect(() => readFileSync(join(copied, ".git"))).toThrow()
      project.write("linked/.env", "TOKEN=secret\n")
      project.write("linked/id_rsa", "secret\n")
      project.write("linked/.pypirc", "secret\n")
      const linkedSecrets = copyWorktree(project.path("linked"), mkdtempSync(join(tmpdir(), "secrets-")))
      try {
        expect(() => readFileSync(join(linkedSecrets, ".env"))).toThrow()
        expect(() => readFileSync(join(linkedSecrets, "id_rsa"))).toThrow()
        expect(() => readFileSync(join(linkedSecrets, ".pypirc"))).toThrow()
      } finally {
        rmSync(linkedSecrets, { recursive: true, force: true })
      }
      project.write("linked/.git", "gitdir: elsewhere\n")
      const linked = copyWorktree(project.path("linked"), linkedCopy)
      expect(() => readFileSync(join(linked, ".git"))).toThrow()
    } finally {
      rmSync(copied, { recursive: true, force: true })
      rmSync(linkedCopy, { recursive: true, force: true })
    }
  })

  it("rejects an empty command, an unpinned image and a bad timeout before spawning", () => {
    const project = Project.withCommit()
    const pinned = `repo@sha256:${"a".repeat(64)}`
    const base = {
      worktree: project.root,
      argv: ["true"],
      image: pinned,
      timeoutMs: 30_000,
      logDirectory: project.path("logs"),
    }
    expect(isPinnedImage(pinned)).toBe(true)
    expect(isPinnedImage("repo:latest")).toBe(false)
    expect(() => runInSandbox({ ...base, argv: [] })).toThrow(SandboxError)
    expect(() => runInSandbox({ ...base, argv: [""] })).toThrow(/command/)
    expect(() => runInSandbox({ ...base, image: "repo:latest" })).toThrow(/digest-pinned/)
    expect(() => runInSandbox({ ...base, timeoutMs: 10 })).toThrow(/timeout/)
  })

  it("reports unavailable instead of running host code when no runtime exists", async () => {
    const project = Project.withCommit()
    const original = process.env.PATH
    process.env.PATH = ""
    try {
      expect(detectRuntime()).toBeUndefined()
      await expect(
        runInSandbox({
          worktree: project.root,
          argv: ["true"],
          image: `repo@sha256:${"a".repeat(64)}`,
          timeoutMs: 30_000,
          logDirectory: join(tmpdir(), "opencode-rig-test-logs"),
        }),
      ).resolves.toMatchObject({ disposition: "not-run:runtime", exit_code: null })
    } finally {
      process.env.PATH = original
    }
  })
})
