/**
 * Container isolation for a forked pull request's verification.
 *
 * A forked PR is third-party code. Its tests may run in a sandbox instead of on
 * the host: a copy of the worktree is mounted into a pinned container image, the
 * network is off unless the call opts in, and the run records the image,
 * command, exit status and log. This is the boundary the plan relies on; the
 * host has no other one.
 */

import { spawn, spawnSync } from "node:child_process"
import { createHash, randomUUID } from "node:crypto"
import { closeSync, cpSync, lstatSync, mkdirSync, mkdtempSync, openSync, rmSync, writeSync } from "node:fs"
import { tmpdir } from "node:os"
import { basename, isAbsolute, join, resolve } from "node:path"
import { redact } from "./journal.ts"
import { ensureDir } from "./util/fsx.ts"

export type SandboxRuntime = "docker" | "podman"

export interface SandboxRequest {
  readonly worktree: string
  readonly argv: readonly string[]
  readonly image: string
  readonly timeoutMs: number
  readonly network?: boolean
  readonly logDirectory: string
}

export interface SandboxReceipt {
  readonly runtime: SandboxRuntime
  readonly image: string
  readonly image_digest?: string
  readonly worktree: string
  readonly argv: readonly string[]
  readonly status: "passed" | "failed"
  readonly exit_code: number | null
  readonly duration_ms: number
  readonly stdout_sha256: string
  readonly stderr_sha256: string
  readonly tail: string
  readonly log_path: string
  readonly reason?: string
}

/** A sandbox image must be digest-pinned, not a mutable tag. */
export function isPinnedImage(image: string): boolean {
  return /^[^\s@]+@sha256:[0-9a-f]{64}$/.test(image)
}

export class SandboxError extends Error {
  constructor(message: string) {
    super(`opencode-rig sandbox: ${message}`)
    this.name = "SandboxError"
  }
}

const MIN_TIMEOUT = 1_000
const MAX_TIMEOUT = 3_600_000

/** The first container runtime that answers on this host, if any. */
export function detectRuntime(env: NodeJS.ProcessEnv = process.env): SandboxRuntime | undefined {
  for (const runtime of ["docker", "podman"] as const) {
    const result = spawnSync(runtime, ["--version"], {
      encoding: "utf8",
      timeout: 2_000,
      stdio: ["ignore", "ignore", "ignore"],
      env,
    })
    if (result.status === 0) return runtime
  }
  return undefined
}

function imageDigest(runtime: SandboxRuntime, image: string): string | undefined {
  const result = spawnSync(runtime, ["image", "inspect", "--format", "{{.Id}}", image], {
    encoding: "utf8",
    timeout: 5_000,
    stdio: ["ignore", "pipe", "ignore"],
  })
  const digest = result.stdout?.trim()
  return result.status === 0 && /^sha256:[0-9a-f]{64}$/.test(digest ?? "") ? digest : undefined
}

/** Build the container invocation for a copy of the worktree. */
export function sandboxArgs(input: {
  runtime: SandboxRuntime
  image: string
  name: string
  workDirectory: string
  argv: readonly string[]
  network: boolean
}): string[] {
  return [
    "run",
    "--rm",
    "--name",
    input.name,
    "--network",
    input.network ? "bridge" : "none",
    "--cpus",
    "2",
    "--memory",
    "2g",
    "--pids-limit",
    "512",
    "-v",
    `${input.workDirectory}:/work`,
    "-w",
    "/work",
    input.image,
    ...input.argv,
  ]
}

/** Copy the worktree into a disposable directory; a `.git` file link is skipped. */
export function copyWorktree(worktree: string, destination: string): string {
  cpSync(worktree, destination, {
    recursive: true,
    dereference: false,
    // Never copy Git metadata or credential-shaped files into the container.
    filter: (source) => {
      const name = basename(source)
      if (name === ".git" || name === ".ssh" || name === ".aws" || name === ".npmrc" || name === ".pypirc") return false
      if (
        /^\.env(?:$|\.)/.test(name) ||
        /\.(?:pem|key)$/i.test(name) ||
        /credential/i.test(name) ||
        /^id_(?:rsa|ed25519|ecdsa|dsa)/.test(name)
      )
        return false
      return true
    },
  })
  return destination
}

function removeRuntimeArtifact(runtime: SandboxRuntime, name: string): void {
  spawnSync(runtime, ["rm", "-f", name], { timeout: 10_000, stdio: "ignore" })
}

/**
 * Run one command in the sandbox and return its receipt. The worktree is
 * copied first, so the container never writes to the host tree.
 */
export function runInSandbox(request: SandboxRequest): Promise<SandboxReceipt> {
  if (!isAbsolute(request.worktree)) throw new SandboxError("worktree must be an absolute path")
  if (request.argv.length === 0) throw new SandboxError("argv must name a command")
  if (!request.image.trim()) throw new SandboxError("no sandbox image is configured")
  if (!isPinnedImage(request.image))
    throw new SandboxError("sandbox image must be digest-pinned as <repository>@sha256:<64 hex>")
  const timeoutMs = Math.trunc(request.timeoutMs)
  if (!Number.isInteger(timeoutMs) || timeoutMs < MIN_TIMEOUT || timeoutMs > MAX_TIMEOUT) {
    throw new SandboxError("timeout must be an integer between 1000 and 3600000 milliseconds")
  }
  const runtime = detectRuntime()
  if (!runtime)
    throw new SandboxError("no container runtime (docker or podman) is available; the verification stays unverified")

  mkdirSync(join(tmpdir(), "opencode"), { recursive: true })
  const name = `opencode-rig-${randomUUID().slice(0, 12)}`
  let sandboxRoot: string | undefined
  let descriptor: number | undefined
  let logPath = ""
  try {
    sandboxRoot = mkdtempSync(join(tmpdir(), "opencode/sandbox-"))
    const workDirectory = copyWorktree(request.worktree, join(sandboxRoot, "work"))
    const logDirectory = ensureDir(resolve(request.logDirectory))
    logPath = join(logDirectory, `sandbox-${randomUUID().slice(0, 8)}.log`)
    descriptor = openSync(logPath, "wx", 0o600)
    return runContainer({ ...request, runtime, timeoutMs, descriptor, workDirectory, name, sandboxRoot, logPath })
  } catch (error) {
    if (descriptor !== undefined) closeSync(descriptor)
    if (sandboxRoot) rmSync(sandboxRoot, { recursive: true, force: true })
    throw error
  }
}

function runContainer(
  request: SandboxRequest & {
    runtime: SandboxRuntime
    timeoutMs: number
    descriptor: number
    workDirectory: string
    name: string
    sandboxRoot: string
    logPath: string
  },
): Promise<SandboxReceipt> {
  const { runtime, timeoutMs, descriptor, workDirectory, name, sandboxRoot, logPath } = request
  const started = Date.now()
  const digest = imageDigest(runtime, request.image)
  return new Promise((done) => {
    const child = spawn(
      runtime,
      sandboxArgs({
        runtime,
        image: request.image,
        name,
        workDirectory,
        argv: request.argv,
        network: request.network === true,
      }),
      {
        detached: process.platform !== "win32",
        stdio: ["ignore", "pipe", "pipe"],
        windowsHide: true,
      },
    )
    const hashes = { stdout: createHash("sha256"), stderr: createHash("sha256") }
    let tail = Buffer.alloc(0)
    let failure: NodeJS.ErrnoException | undefined
    let cutoff: ReturnType<typeof setTimeout> | undefined
    let finished = false
    const finish = (code: number | null, signal: NodeJS.Signals | null) => {
      if (finished) return
      finished = true
      clearTimeout(timer)
      clearTimeout(cutoff)
      closeSync(descriptor)
      rmSync(sandboxRoot, { recursive: true, force: true })
      const reason =
        failure?.code === "ETIMEDOUT" ? "timeout" : (failure?.message ?? (signal ? `signal: ${signal}` : undefined))
      done({
        runtime,
        image: request.image,
        ...(digest ? { image_digest: digest } : {}),
        worktree: request.worktree,
        argv: [...request.argv],
        status: !failure && code === 0 ? "passed" : "failed",
        exit_code: code,
        duration_ms: Date.now() - started,
        stdout_sha256: hashes.stdout.digest("hex"),
        stderr_sha256: hashes.stderr.digest("hex"),
        tail: redact(tail.toString("utf8").trim()),
        log_path: logPath,
        ...(reason ? { reason } : {}),
      })
    }
    const timer = setTimeout(() => {
      failure = Object.assign(new Error("sandbox timed out"), { code: "ETIMEDOUT" })
      removeRuntimeArtifact(runtime, name)
      try {
        if (process.platform !== "win32" && child.pid) process.kill(-child.pid, "SIGTERM")
        else child.kill("SIGTERM")
      } catch {
        // The runtime may already have exited.
      }
      cutoff = setTimeout(() => {
        try {
          if (process.platform !== "win32" && child.pid) process.kill(-child.pid, "SIGKILL")
          else child.kill("SIGKILL")
        } catch {
          // Best effort after the term signal.
        }
        child.stdout?.destroy()
        child.stderr?.destroy()
        child.unref?.()
        finish(null, null)
      }, 1_000)
    }, timeoutMs)
    for (const stream of ["stdout", "stderr"] as const) {
      child[stream]?.on("data", (chunk: Buffer) => {
        if (finished) return
        hashes[stream].update(chunk)
        tail = Buffer.concat([tail, chunk]).subarray(-4_000)
        try {
          writeSync(descriptor, chunk)
        } catch (error) {
          failure = error as NodeJS.ErrnoException
        }
      })
    }
    child.once("error", (error) => {
      failure = error as NodeJS.ErrnoException
      removeRuntimeArtifact(runtime, name)
      finish(null, null)
    })
    child.once("close", (code, signal) => finish(code, signal))
  })
}
