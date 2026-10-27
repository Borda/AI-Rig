/**
 * Gate discovery and execution.
 *
 * The runner never invents a check. It selects configured project commands,
 * records real exit codes, and keeps a truncated tail plus a full digest so a
 * later reader can audit what actually ran.
 */

import { spawnSync } from "node:child_process"
import { existsSync, readFileSync } from "node:fs"
import { join } from "node:path"
import { sha256 } from "./digest.ts"
import type { GateReceipt } from "./run.ts"

export interface GateSpec {
  readonly id: string
  readonly command: string
  readonly source: string
  readonly needsFiles: boolean
}

export interface GateResult {
  readonly spec: GateSpec
  readonly receipt: GateReceipt
}

export class GateError extends Error {
  constructor(message: string) {
    super(`opencode-rig gates: ${message}`)
    this.name = "GateError"
  }
}

const STANDARD_IDS = ["lint", "format", "typecheck", "test"] as const

function slug(value: string): string {
  return value
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "")
}

function readIfExists(path: string): string | undefined {
  return existsSync(path) ? readFileSync(path, "utf8") : undefined
}

function makefileTargets(text: string): Set<string> {
  const targets = new Set<string>()
  for (const line of text.split("\n")) {
    const match = /^([A-Za-z0-9_.-]+)\s*:(?!=)/.exec(line)
    if (match?.[1] && !match[1].startsWith(".")) targets.add(match[1])
  }
  return targets
}

function packageScripts(text: string): Set<string> {
  try {
    const parsed = JSON.parse(text) as { scripts?: Record<string, unknown> }
    return new Set(Object.keys(parsed.scripts ?? {}))
  } catch {
    return new Set()
  }
}

/**
 * Detect project gates in documented precedence order. The first source that
 * yields any standard gate wins; detection never mixes sources.
 */
export function detectGates(projectRoot: string, explicit: readonly string[]): GateSpec[] {
  if (explicit.length > 0) {
    return explicit.map((command, index) => ({
      id: `configured-${index + 1}`,
      command,
      source: "options.gates",
      needsFiles: false,
    }))
  }

  const makefile = readIfExists(join(projectRoot, "Makefile"))
  if (makefile) {
    const targets = makefileTargets(makefile)
    const found = STANDARD_IDS.filter((id) => targets.has(id)).map((id) => ({
      id,
      command: `make ${id}`,
      source: "Makefile",
      needsFiles: false,
    }))
    if (found.length > 0) return found
  }

  const packageJson = readIfExists(join(projectRoot, "package.json"))
  if (packageJson) {
    const scripts = packageScripts(packageJson)
    const found = STANDARD_IDS.filter((id) => scripts.has(id)).map((id) => ({
      id,
      command: `npm run ${id}`,
      source: "package.json",
      needsFiles: false,
    }))
    if (found.length > 0) return found
  }

  if (existsSync(join(projectRoot, ".pre-commit-config.yaml"))) {
    return [
      {
        id: "pre-commit",
        command: "pre-commit run --files",
        source: ".pre-commit-config.yaml",
        needsFiles: true,
      },
    ]
  }

  const pyproject = readIfExists(join(projectRoot, "pyproject.toml"))
  if (pyproject && (/\[tool\.pytest/.test(pyproject) || existsSync(join(projectRoot, "pytest.ini")))) {
    return [{ id: "test", command: "pytest", source: "pytest configuration", needsFiles: false }]
  }

  return []
}

function tailOf(stdout: string, stderr: string, limit = 4_000): string {
  const combined = [stdout.trim(), stderr.trim()].filter(Boolean).join("\n")
  return combined.length <= limit ? combined : combined.slice(-limit)
}

/**
 * Execute one gate. `files` replaces the `--files` placeholder for pre-commit.
 */
export function runGate(spec: GateSpec, cwd: string, files: readonly string[], timeoutMs: number): GateResult {
  const command = spec.needsFiles
    ? `${spec.command} ${files.map((file) => `'${file.replace(/'/g, "'\\''")}'`).join(" ")}`
    : spec.command
  const started = Date.now()
  const result = spawnSync(command, {
    cwd,
    shell: true,
    encoding: "utf8",
    timeout: timeoutMs,
    maxBuffer: 16 * 1024 * 1024,
  })
  const stdout = result.stdout ?? ""
  const stderr = result.stderr ?? ""
  const timedOut = result.error?.name === "ETIMEDOUT" || result.signal === "SIGTERM"
  const receipt: GateReceipt = {
    id: spec.id,
    command,
    source: spec.source,
    status: timedOut ? "failed" : result.status === 0 ? "passed" : "failed",
    exit_code: result.status,
    duration_ms: Date.now() - started,
    stdout_sha256: sha256(stdout),
    stderr_sha256: sha256(stderr),
    tail: tailOf(stdout, stderr),
    ...(timedOut ? { reason: "timeout" } : result.error ? { reason: result.error.message } : {}),
  }
  return { spec, receipt }
}

export function runGates(
  specs: readonly GateSpec[],
  cwd: string,
  files: readonly string[],
  timeoutMs: number,
): GateResult[] {
  if (specs.length === 0)
    throw new GateError("no configured gates detected; record gate_exemption instead of inventing one")
  return specs.map((spec) => runGate(spec, cwd, files, timeoutMs))
}
