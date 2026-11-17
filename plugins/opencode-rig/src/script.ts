/**
 * Designed script execution for workflow probes.
 *
 * A text whitelist cannot prove arbitrary code, so the workflows run code as a
 * declared script file with a recorded source class instead. This module
 * resolves the file under its source root, binds its digest and runs it with a
 * fixed interpreter, a positive environment allowlist, a bounded runtime and a
 * receipt. It is not a sandbox: it proves what ran, not that it is safe.
 */

import { spawn } from "node:child_process"
import { createHash, randomUUID } from "node:crypto"
import { closeSync, lstatSync, openSync, realpathSync, statSync, writeSync } from "node:fs"
import { isAbsolute, join, relative, resolve, sep } from "node:path"
import { sha256File } from "./digest.ts"
import { redact } from "./journal.ts"
import { ensureDir } from "./util/fsx.ts"

export const INTERPRETERS = ["python", "python3", "node", "bash", "sh", "awk"] as const
export type ScriptInterpreter = (typeof INTERPRETERS)[number]
export type ScriptSource = "run" | "project" | "plugin"

export interface ScriptRequest {
  readonly source: ScriptSource
  readonly path: string
  readonly interpreter: ScriptInterpreter
  readonly argv: readonly string[]
  readonly timeoutMs: number
  readonly projectRoot: string
  readonly packageRoot: string
  readonly runDirectory?: string
  readonly runsRoot?: string
  /** Digest listing for the plugin source class; absent means nothing is trusted there. */
  readonly pluginFiles?: Readonly<Record<string, string>>
  readonly logDirectory: string
}

export interface ScriptReceipt {
  readonly source: ScriptSource
  readonly path: string
  readonly digest: string
  readonly interpreter: ScriptInterpreter
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

export class ScriptError extends Error {
  constructor(message: string) {
    super(`opencode-rig script: ${message}`)
    this.name = "ScriptError"
  }
}

const MAX_BYTES = 256 * 1024
const MAX_ARGS = 64
const MAX_ARG_CHARS = 4_096
const MIN_TIMEOUT = 1_000
const MAX_TIMEOUT = 3_600_000

/** Positive environment allowlist: enough to run a tool, nothing inherited wholesale. */
const ENV_KEYS = [
  "PATH",
  "HOME",
  "USERPROFILE",
  "TMPDIR",
  "TMP",
  "TEMP",
  "LANG",
  "LC_ALL",
  "LC_CTYPE",
  "TZ",
  "SystemRoot",
  "ComSpec",
  "PATHEXT",
  "APPDATA",
  "LOCALAPPDATA",
] as const

export function scriptEnv(env: NodeJS.ProcessEnv = process.env): Record<string, string> {
  const out: Record<string, string> = {}
  for (const key of ENV_KEYS) {
    const value = env[key]
    if (typeof value === "string" && value) out[key] = value
  }
  return out
}

function within(root: string, path: string): boolean {
  const rel = relative(root, path)
  // The root itself is inside; the following isFile() check rejects a directory.
  return rel === "" || (!rel.startsWith(`..${sep}`) && rel !== ".." && !isAbsolute(rel))
}

/** Resolve a candidate path and prove it is a regular file under one of the roots. */
function resolveInside(roots: readonly string[], candidate: string, label: string): string {
  for (const root of roots) {
    const absolute = resolve(candidate)
    if (!within(resolve(root), absolute)) continue
    let real: string
    let realRoot: string
    try {
      real = realpathSync(absolute)
      realRoot = realpathSync(root)
    } catch {
      throw new ScriptError(`${label} script does not exist: ${candidate}`)
    }
    if (!within(realRoot, real)) throw new ScriptError(`${label} script escapes its root: ${candidate}`)
    if (!statSync(real).isFile()) throw new ScriptError(`${label} script is not a regular file: ${candidate}`)
    if (statSync(real).size > MAX_BYTES)
      throw new ScriptError(`${label} script exceeds ${MAX_BYTES} bytes: ${candidate}`)
    return real
  }
  throw new ScriptError(`${label} script is outside its declared root: ${candidate}`)
}

export interface ResolvedScript {
  readonly absolute: string
  readonly source: ScriptSource
  readonly digest: string
}

/** Resolve the declared source class; fail closed when a root or manifest is absent. */
export function resolveScript(request: ScriptRequest): ResolvedScript {
  // A project and run script is named relative to the checkout; a plugin helper
  // is named relative to the installed package.
  let roots: string[]
  let candidates: string[]
  if (request.source === "run") {
    if (request.runDirectory) {
      const scriptsRoot = join(request.runDirectory, "scripts")
      roots = [scriptsRoot]
      const bare = request.path.replace(/^scripts[\\/]/, "")
      candidates = isAbsolute(request.path)
        ? [request.path]
        : [resolve(scriptsRoot, bare), resolve(request.projectRoot, request.path)]
    } else {
      if (!request.runsRoot)
        throw new ScriptError("source 'run' needs an active run or a run_id-scoped path under the artifact runs root")
      roots = [request.runsRoot]
      candidates = [resolve(request.projectRoot, request.path)]
    }
  } else if (request.source === "project") {
    roots = ["scripts", "bin", "tools"].map((name) => join(request.projectRoot, name))
    candidates = [resolve(request.projectRoot, request.path)]
  } else {
    roots = ["bin", "scripts"].map((name) => join(request.packageRoot, name))
    candidates = [resolve(request.packageRoot, request.path)]
  }
  const failures: string[] = []
  let absolute: string | undefined
  for (const candidate of candidates) {
    try {
      absolute = resolveInside(roots, candidate, request.source)
      break
    } catch (error) {
      failures.push(error instanceof Error ? error.message : String(error))
    }
  }
  if (!absolute) {
    const specific = failures.find((message) => /escapes its root|exceeds|does not exist/.test(message))
    throw new ScriptError(
      specific ??
        `${request.source} script is outside its declared root: ${request.path}; expected one of ${roots.join(", ")}`,
    )
  }
  if (request.source === "run" && !request.runDirectory) {
    let runsRoot = resolve(roots[0]!)
    try {
      runsRoot = realpathSync(runsRoot)
    } catch {
      // The runs root may not exist yet; the lexical path is the fallback.
    }
    const rel = relative(runsRoot, absolute).split(sep)
    if (rel.length !== 4 || rel[2] !== "scripts")
      throw new ScriptError(`run script must be under <runs>/<skill>/<run>/scripts/: ${request.path}`)
  }
  if (request.source === "plugin") {
    const files = request.pluginFiles
    if (!files) throw new ScriptError("no shipped plugin listing is available; a plugin script cannot run")
    const key = relative(request.packageRoot, absolute).split(sep).join("/")
    const expected = files[key]
    if (!expected) throw new ScriptError(`plugin script is not in the shipped listing: ${key}`)
    const digest = sha256File(absolute)
    if (digest !== expected) throw new ScriptError(`plugin script digest does not match the shipped listing: ${key}`)
    return { absolute, source: "plugin", digest }
  }
  return { absolute, source: request.source, digest: sha256File(absolute) }
}

/** Stop the script's process tree where process groups are available. */
function terminate(child: ReturnType<typeof spawn>, signal: NodeJS.Signals): void {
  if (!child.pid) return
  try {
    if (process.platform !== "win32") {
      process.kill(-child.pid, signal)
      return
    }
    // Windows has no process groups; taskkill /T reaches the interpreter's children.
    const killer = spawn("taskkill", ["/PID", String(child.pid), "/T", "/F"], {
      windowsHide: true,
      stdio: "ignore",
      timeout: 1_000,
    })
    killer.once("error", () => {
      try {
        child.kill(signal)
      } catch {
        // An exited process needs no further signal.
      }
    })
    killer.unref?.()
  } catch {
    // An exited process needs no further signal.
  }
}

/**
 * Run one declared script and return its receipt. The log keeps the complete
 * streams; the receipt keeps only their digests and a redacted tail.
 */
export function executeScript(request: ScriptRequest): Promise<ScriptReceipt> {
  const timeoutMs = Math.trunc(request.timeoutMs)
  if (!Number.isInteger(timeoutMs) || timeoutMs < MIN_TIMEOUT || timeoutMs > MAX_TIMEOUT) {
    throw new ScriptError("timeout must be an integer between 1000 and 3600000 milliseconds")
  }
  if (request.argv.length > MAX_ARGS) throw new ScriptError(`argv exceeds ${MAX_ARGS} entries`)
  for (const arg of request.argv) {
    if (typeof arg !== "string" || arg.length > MAX_ARG_CHARS || arg.includes("\0"))
      throw new ScriptError("argv entries must be short strings without NUL bytes")
  }
  const resolved = resolveScript(request)
  if (statSync(resolved.absolute).size > MAX_BYTES)
    throw new ScriptError(`script exceeds ${MAX_BYTES} bytes: ${request.path}`)
  if (!lstatSync(resolved.absolute).isFile()) throw new ScriptError(`script is not a regular file: ${request.path}`)

  const logDirectory = ensureDir(resolve(request.logDirectory))
  const logPath = join(
    logDirectory,
    `${request.source}-${resolved.digest.slice(0, 12)}-${randomUUID().slice(0, 8)}.log`,
  )
  const descriptor = openSync(logPath, "wx", 0o600)
  const started = Date.now()
  return new Promise((done) => {
    const child = spawn(request.interpreter, [resolved.absolute, ...request.argv], {
      cwd: request.projectRoot,
      env: scriptEnv(),
      detached: process.platform !== "win32",
      stdio: ["ignore", "pipe", "pipe"],
      windowsHide: true,
    })
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
      const reason =
        failure?.code === "ETIMEDOUT" ? "timeout" : (failure?.message ?? (signal ? `signal: ${signal}` : undefined))
      done({
        source: resolved.source,
        path: resolved.absolute,
        digest: resolved.digest,
        interpreter: request.interpreter,
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
      failure = Object.assign(new Error("script timed out"), { code: "ETIMEDOUT" })
      terminate(child, "SIGTERM")
      cutoff = setTimeout(() => {
        terminate(child, "SIGKILL")
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
      finish(null, null)
    })
    child.once("close", (code, signal) => finish(code, signal))
  })
}
