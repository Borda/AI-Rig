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

export type SandboxDisposition =
  | "ran-passed"
  | "ran-failed"
  | "not-run:image"
  | "not-run:runtime"
  | "not-run:setup"
  | "not-run:launch"
  | "not-run:timeout"

export interface SandboxOutcome {
  readonly disposition: SandboxDisposition
  readonly runtime?: SandboxRuntime
  readonly image: string
  readonly image_digest?: string
  readonly worktree: string
  readonly argv: readonly string[]
  readonly network: "none" | "bridge"
  readonly status: "passed" | "failed" | "unavailable"
  readonly exit_code: number | null
  readonly duration_ms: number
  readonly stdout_sha256?: string
  readonly stderr_sha256?: string
  readonly tail?: string
  readonly log_path?: string
  readonly reason?: string
  readonly log_error?: string
  cleanup_error?: string
}

export type SandboxNextAction =
  | { readonly kind: "record-result" | "mark-incomplete"; readonly instruction: string }
  | { readonly kind: "run-on-host" | "request-trust"; readonly command: string; readonly instruction: string }

export interface SandboxReceipt extends SandboxOutcome {
  readonly next_action: SandboxNextAction
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

/** The first container engine that can answer, including its daemon if it needs one. */
export function detectRuntime(env: NodeJS.ProcessEnv = process.env): SandboxRuntime | undefined {
  for (const runtime of ["docker", "podman"] as const) {
    const result = spawnSync(runtime, ["info"], {
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
    "--entrypoint",
    input.argv[0]!,
    input.image,
    ...input.argv.slice(1),
  ]
}

/** Copy the authorized physical directory into a disposable directory without Git metadata. */
export function copyWorktree(worktree: string, destination: string): string {
  if (!lstatSync(worktree).isDirectory()) throw new SandboxError("copy source must be a real directory")
  const existing = lstatSync(destination, { throwIfNoEntry: false })
  if (existing && !existing.isDirectory()) throw new SandboxError("copy destination must be a real directory")
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
  if (!lstatSync(destination).isDirectory()) throw new SandboxError("copy destination must be a real directory")
  return destination
}

function removeRuntimeArtifact(runtime: SandboxRuntime, name: string): void {
  spawnSync(runtime, ["rm", "-f", name], { timeout: 10_000, stdio: "ignore" })
}

/** Preserve execution evidence when closing a log or removing a temporary copy fails. */
function cleanupSandbox(outcome: SandboxOutcome, descriptor?: number, root?: string): void {
  const errors: string[] = []
  try {
    if (descriptor !== undefined) closeSync(descriptor)
  } catch (error) {
    errors.push(redact(String(error)))
  }
  try {
    if (root) rmSync(root, { recursive: true, force: true })
  } catch (error) {
    errors.push(redact(String(error)))
  }
  if (errors.length) outcome.cleanup_error = errors.join("\n")
}

/** Bind each outcome to the requested execution context, including unstarted checks. */
function executionContext(request: SandboxRequest) {
  return {
    image: request.image,
    worktree: request.worktree,
    argv: [...request.argv],
    network: request.network === true ? ("bridge" as const) : ("none" as const),
  }
}

/** Record a refusal or setup failure without claiming that a command ran. */
function unavailable(
  request: SandboxRequest,
  started: number,
  disposition: "not-run:image" | "not-run:runtime" | "not-run:setup",
  reason: string,
  runtime?: SandboxRuntime,
): SandboxOutcome {
  return {
    ...executionContext(request),
    disposition,
    ...(runtime ? { runtime } : {}),
    status: "unavailable",
    exit_code: null,
    duration_ms: Date.now() - started,
    reason,
  }
}

/**
 * Run one command in the sandbox and return its outcome. The worktree is
 * copied first, so the container never writes to the host tree.
 */
export function runInSandbox(request: SandboxRequest): Promise<SandboxOutcome> {
  if (!isAbsolute(request.worktree)) throw new SandboxError("worktree must be an absolute path")
  if (!request.argv[0]?.trim()) throw new SandboxError("argv must name a command")
  if (request.image && !isPinnedImage(request.image))
    throw new SandboxError("sandbox image must be digest-pinned as <repository>@sha256:<64 hex>")
  const timeoutMs = Math.trunc(request.timeoutMs)
  if (!Number.isInteger(timeoutMs) || timeoutMs < MIN_TIMEOUT || timeoutMs > MAX_TIMEOUT) {
    throw new SandboxError("timeout must be an integer between 1000 and 3600000 milliseconds")
  }
  const started = Date.now()
  if (!request.image)
    return Promise.resolve(unavailable(request, started, "not-run:image", "no sandbox image is configured"))
  const runtime = detectRuntime()
  if (!runtime)
    return Promise.resolve(
      unavailable(request, started, "not-run:runtime", "no container runtime (Docker or Podman) is reachable"),
    )

  return prepareContainer({ ...request, runtime, timeoutMs }, started)
}

/** Prepare the isolated mount and log before passing control to the container runner. */
function prepareContainer(
  request: SandboxRequest & { runtime: SandboxRuntime },
  started: number,
): Promise<SandboxOutcome> {
  const tempRoot = join(tmpdir(), "opencode")
  const name = `opencode-rig-${randomUUID().slice(0, 12)}`
  let sandboxRoot: string | undefined
  let descriptor: number | undefined
  try {
    mkdirSync(tempRoot, { recursive: true })
    sandboxRoot = mkdtempSync(join(tempRoot, "sandbox-"))
    const workDirectory = copyWorktree(request.worktree, join(sandboxRoot, "work"))
    const logDirectory = ensureDir(resolve(request.logDirectory))
    const logPath = join(logDirectory, `sandbox-${randomUUID().slice(0, 8)}.log`)
    descriptor = openSync(logPath, "wx", 0o600)
    return runContainer({ ...request, descriptor, workDirectory, name, sandboxRoot, logPath })
  } catch (error) {
    const outcome = unavailable(request, started, "not-run:setup", redact(String(error)), request.runtime)
    cleanupSandbox(outcome, descriptor, sandboxRoot)
    return Promise.resolve(outcome)
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
): Promise<SandboxOutcome> {
  const { runtime, timeoutMs, descriptor, workDirectory, name, sandboxRoot, logPath } = request
  const started = Date.now()
  const digest = imageDigest(runtime, request.image)
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
  return new Promise((done) => {
    const hashes = { stdout: createHash("sha256"), stderr: createHash("sha256") }
    let tail = Buffer.alloc(0)
    let launchError: string | undefined
    let logError: string | undefined
    let timedOut = false
    let cutoff: ReturnType<typeof setTimeout> | undefined
    let finished = false
    const finish = (code: number | null, signal: NodeJS.Signals | null) => {
      if (finished) return
      finished = true
      clearTimeout(timer)
      clearTimeout(cutoff)
      const { disposition, status, reason } = containerResult(code, signal, timedOut, launchError)
      const outcome: SandboxOutcome = {
        ...executionContext(request),
        runtime,
        ...(digest ? { image_digest: digest } : {}),
        disposition,
        status,
        exit_code: code,
        duration_ms: Date.now() - started,
        stdout_sha256: hashes.stdout.digest("hex"),
        stderr_sha256: hashes.stderr.digest("hex"),
        tail: redact(tail.toString("utf8").trim()),
        log_path: logPath,
        ...(reason ? { reason } : {}),
        ...(logError ? { log_error: logError } : {}),
      }
      done(outcome)
      // Promise continuations observe cleanup evidence after this callback returns.
      cleanupSandbox(outcome, descriptor, sandboxRoot)
    }
    const timer = setTimeout(() => {
      timedOut = true
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
          logError ??= redact(String(error))
        }
      })
    }
    child.once("error", (error) => {
      launchError = redact(error.message)
      removeRuntimeArtifact(runtime, name)
      finish(null, null)
    })
    child.once("close", (code, signal) => finish(code, signal))
  })
}

/** Classify what the engine's result can establish about the requested command. */
function containerResult(
  code: number | null,
  signal: NodeJS.Signals | null,
  timedOut: boolean,
  launchError?: string,
): Pick<SandboxOutcome, "disposition" | "status" | "reason"> {
  if (timedOut)
    return { disposition: "not-run:timeout", status: "unavailable", reason: "timeout; command completion unconfirmed" }
  // Docker and Podman reserve 125–127 for engine errors and unstartable commands.
  // The same exits from a payload are ambiguous, so never attest execution from them.
  if (launchError || code === null || (code >= 125 && code <= 127)) {
    const reason = launchError ?? (signal ? `engine signal: ${signal}` : `engine exit: ${code}`)
    return { disposition: "not-run:launch", status: "unavailable", reason }
  }
  if (code === 0) return { disposition: "ran-passed", status: "passed" }
  return { disposition: "ran-failed", status: "failed" }
}
