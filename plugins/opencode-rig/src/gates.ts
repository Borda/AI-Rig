/**
 * Gate discovery and execution.
 *
 * The runner never invents a check. It selects configured project commands,
 * records real exit codes, and keeps a truncated tail plus a full digest so a
 * later reader can audit what actually ran.
 */

import { spawnSync } from "node:child_process"
import { existsSync, lstatSync, readFileSync, realpathSync } from "node:fs"
import { dirname, isAbsolute, join, relative, resolve, sep } from "node:path"
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
  let definitionDepth = 0
  for (const line of text.split("\n")) {
    if (/^\s*(?:(?:override|export|private)\s+)*define\s+/.test(line)) definitionDepth += 1
    else if (/^\s*endef\s*(?:#.*)?$/.test(line)) definitionDepth = Math.max(0, definitionDepth - 1)
    else if (definitionDepth === 0) {
      const match = /^([A-Za-z0-9_.-]+(?:[ \t]+[A-Za-z0-9_.-]+)*)\s*:(?!=)/.exec(line)
      for (const target of match?.[1]?.split(/\s+/) ?? []) {
        if (!target.startsWith(".")) targets.add(target)
      }
    }
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
  const setup = readIfExists(join(projectRoot, "setup.cfg"))
  const tox = readIfExists(join(projectRoot, "tox.ini"))
  if (
    /^\s*\[\s*tool\s*\.\s*pytest(?:\s*\.\s*ini_options)?\s*\]/m.test(pyproject ?? "") ||
    existsSync(join(projectRoot, "pytest.ini")) ||
    /^\s*\[tool:pytest\]\s*$/m.test(setup ?? "") ||
    /^\s*\[pytest\]\s*$/m.test(tox ?? "")
  ) {
    return [{ id: "test", command: "pytest", source: "pytest configuration", needsFiles: false }]
  }

  return []
}

function tailOf(stdout: string, stderr: string, limit = 4_000): string {
  const combined = [stdout.trim(), stderr.trim()].filter(Boolean).join("\n")
  return combined.length <= limit ? combined : combined.slice(-limit)
}

/** Refuse file arguments that escape the project, including through symlinks. */
function resolveInsideProject(projectRoot: string, file: string): string {
  const absolute = resolve(projectRoot, file)
  const rel = relative(resolve(projectRoot), absolute)
  if (file.split(sep).includes("..") || rel === ".." || rel.startsWith(`..${sep}`) || isAbsolute(rel)) {
    throw new GateError(`file escapes the project: ${file}`)
  }
  let physical: string
  try {
    let ancestor = absolute
    while (!lstatSync(ancestor, { throwIfNoEntry: false })) ancestor = dirname(ancestor)
    physical = relative(realpathSync(projectRoot), realpathSync(ancestor))
  } catch (error) {
    throw new GateError(`cannot resolve gate file ${file}: ${String(error)}`)
  }
  if (physical === ".." || physical.startsWith(`..${sep}`) || isAbsolute(physical)) {
    throw new GateError(`file resolves outside the project: ${file}`)
  }
  return absolute
}

/** Validate the fixed pre-commit invocation before passing files as literal argv. */
function preCommitArgs(spec: GateSpec, cwd: string, files: readonly string[]): string[] {
  if (spec.command !== "pre-commit run --files") throw new GateError("unsupported file-based gate command")
  if (files.length === 0) throw new GateError("pre-commit needs files")
  for (const file of files) {
    if (!file) throw new GateError(`invalid gate file: ${file}`)
    resolveInsideProject(cwd, file)
  }
  return ["run", "--files", ...files.map((file) => (file.startsWith("-") ? `./${file}` : file))]
}

/**
 * Execute one gate with bounded runtime and literal file arguments for pre-commit.
 */
export function runGate(spec: GateSpec, cwd: string, files: readonly string[], timeoutMs: number): GateResult {
  timeoutMs = Math.trunc(timeoutMs)
  if (!Number.isInteger(timeoutMs) || timeoutMs < 1_000 || timeoutMs > 3_600_000) {
    throw new GateError("timeout must be an integer between 1000 and 3600000 milliseconds")
  }
  const args = spec.needsFiles ? preCommitArgs(spec, cwd, files) : []
  const command = spec.needsFiles
    ? `pre-commit ${args.map((arg) => (/^[a-zA-Z0-9_./:-]+$/.test(arg) ? arg : `'${arg.replaceAll("'", `'"'"'`)}'`)).join(" ")}`
    : spec.command
  const started = Date.now()
  const result = spawnSync(spec.needsFiles ? "pre-commit" : command, args, {
    cwd,
    shell: !spec.needsFiles,
    encoding: "utf8",
    timeout: timeoutMs,
    maxBuffer: 16 * 1024 * 1024,
  })
  const stdout = result.stdout ?? ""
  const stderr = result.stderr ?? ""
  const timedOut = (result.error as NodeJS.ErrnoException | undefined)?.code === "ETIMEDOUT"
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
