/**
 * Small mutable plugin state.
 *
 * Single-value state is exempt from append-only rules: it records only the
 * currently active run per session so tools can avoid repeating `skill` on
 * every call. Run evidence itself lives in the run directory and is append-only
 * or write-once.
 */

import { existsSync, lstatSync, readFileSync, readdirSync } from "node:fs"
import { join } from "node:path"
import { writeJsonAtomic } from "./util/fsx.ts"
import { SCHEMA_VERSION } from "./version.ts"

export interface SessionRun {
  readonly runId: string
  readonly skill: string
}

export interface RigState {
  readonly schema: number
  readonly runs: Record<string, string>
  readonly sessions: Record<string, SessionRun>
}

const EMPTY = (): RigState => ({ schema: 1, runs: {}, sessions: {} })
const SKILL = /^[a-z0-9]+(-[a-z0-9]+)*$/
const RUN_ID = /^[a-zA-Z0-9][a-zA-Z0-9_-]*$/

function object(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
}

function safeRunId(value: unknown): value is string {
  return typeof value === "string" && RUN_ID.test(value) && !Object.hasOwn(Object.prototype, value)
}

function safeSkill(value: unknown): value is string {
  return typeof value === "string" && SKILL.test(value) && !Object.hasOwn(Object.prototype, value)
}

function validState(value: unknown): value is RigState {
  if (!object(value) || value.schema !== 1 || !object(value.runs) || !object(value.sessions)) return false
  const runs = value.runs
  return (
    Object.entries(runs).every(([id, skill]) => safeRunId(id) && safeSkill(skill)) &&
    Object.values(value.sessions).every(
      (entry) => object(entry) && safeRunId(entry.runId) && safeSkill(entry.skill) && runs[entry.runId] === entry.skill,
    )
  )
}

/** Validate identity and the required createRun shape before trusting recovered status. */
function recordedStatus(artifactRoot: string, skill: string, runId: string): string {
  const skillPath = join(artifactRoot, "runs", skill)
  const directory = join(skillPath, runId)
  const path = join(directory, "run.json")
  if ([join(artifactRoot, "runs"), skillPath, directory, path].some((part) => lstatSync(part).isSymbolicLink())) {
    throw new Error("symbolic run record")
  }
  const run: unknown = JSON.parse(readFileSync(path, "utf8"))
  if (
    !object(run) ||
    run.schema !== SCHEMA_VERSION ||
    run.run_id !== runId ||
    run.skill !== skill ||
    typeof run.status !== "string" ||
    !["in_progress", "completed", "blocked", "failed", "stale"].includes(run.status) ||
    typeof run.created_at !== "string" ||
    typeof run.updated_at !== "string" ||
    typeof run.plugin_version !== "string" ||
    !object(run.scope) ||
    !Array.isArray(run.scope.paths) ||
    !run.scope.paths.every((path) => typeof path === "string") ||
    !Array.isArray(run.scope.entries) ||
    typeof run.scope.digest !== "string" ||
    typeof run.scope.dirty !== "boolean" ||
    !(run.scope.base === null || typeof run.scope.base === "string") ||
    !Array.isArray(run.inputs) ||
    !Array.isArray(run.outputs) ||
    !Array.isArray(run.gates) ||
    !Array.isArray(run.limits) ||
    !run.limits.every((limit) => typeof limit === "string") ||
    typeof run.confidence !== "number" ||
    !Number.isFinite(run.confidence)
  )
    throw new Error("invalid run record")
  return run.status
}

/** Rebuild only the run index; run evidence cannot identify the original session. */
function recoverState(artifactRoot: string): RigState {
  const runs: Record<string, string> = {}
  const root = join(artifactRoot, "runs")
  if (!existsSync(root) || lstatSync(root).isSymbolicLink()) return EMPTY()
  const ambiguous = new Set<string>()
  for (const skill of readdirSync(root, { withFileTypes: true })) {
    if (!skill.isDirectory() || !safeSkill(skill.name)) continue
    for (const run of readdirSync(join(root, skill.name), { withFileTypes: true })) {
      if (!run.isDirectory() || !safeRunId(run.name)) continue
      try {
        recordedStatus(artifactRoot, skill.name, run.name)
        if (Object.hasOwn(runs, run.name)) ambiguous.add(run.name)
        runs[run.name] = skill.name
      } catch {
        // Unreadable or incomplete evidence must not become an authoritative index entry.
      }
    }
  }
  for (const id of ambiguous) delete runs[id]
  return { schema: 1, runs, sessions: {} }
}

/** Read validated session state or recover its run index without changing damaged evidence. */
export function loadState(artifactRoot: string): RigState {
  const path = join(artifactRoot, "state.json")
  if (!existsSync(path)) return recoverState(artifactRoot)
  try {
    const parsed: unknown = JSON.parse(readFileSync(path, "utf8"))
    if (validState(parsed)) return parsed
  } catch {
    // Preserve damaged state; recovery is a read-only index built from run evidence.
  }
  return recoverState(artifactRoot)
}

/** Persist valid state while preserving any existing corrupt index for diagnosis. */
export function saveState(artifactRoot: string, state: RigState): void {
  if (!validState(state)) throw new Error("invalid state schema or run identifiers")
  const path = join(artifactRoot, "state.json")
  if (existsSync(path)) {
    let existing: unknown
    try {
      existing = JSON.parse(readFileSync(path, "utf8"))
    } catch {
      throw new Error("cannot overwrite corrupt state.json; move state.json aside to recover")
    }
    if (!validState(existing)) throw new Error("cannot overwrite invalid state.json; move state.json aside to recover")
  }
  writeJsonAtomic(path, state)
}

/** Bind a session to safe run identifiers and retain the run lookup index. */
export function rememberRun(artifactRoot: string, sessionID: string, entry: SessionRun): RigState {
  if (!safeRunId(entry.runId) || !safeSkill(entry.skill)) throw new Error("invalid run identifiers")
  const state = loadState(artifactRoot)
  const next: RigState = {
    schema: 1,
    runs: { ...state.runs, [entry.runId]: entry.skill },
    sessions: { ...state.sessions, [sessionID]: entry },
  }
  saveState(artifactRoot, next)
  return next
}

/** Resolve a session pointer and remove it only after validating terminal run evidence. */
export function activeRun(artifactRoot: string, sessionID: string): SessionRun | undefined {
  const path = join(artifactRoot, "state.json")
  if (!existsSync(path)) return undefined
  let state: unknown
  try {
    state = JSON.parse(readFileSync(path, "utf8"))
    if (!validState(state)) throw new Error("invalid state schema or run identifiers")
  } catch (error) {
    throw new Error(`cannot read active run scope state: ${String(error)}; move state.json aside to recover`)
  }
  if (!Object.hasOwn(state.sessions, sessionID)) return undefined
  const entry = state.sessions[sessionID]!
  let status: string
  try {
    status = recordedStatus(artifactRoot, entry.skill, entry.runId)
  } catch (error) {
    throw new Error(`cannot read active run scope: ${String(error)}; move state.json aside to recover`)
  }
  if (status === "in_progress") return entry
  forgetRun(artifactRoot, sessionID, entry)
  return undefined
}

/** Look up a safe run identifier without exposing inherited mapping entries. */
export function skillForRun(artifactRoot: string, runId: string): string | undefined {
  if (!safeRunId(runId)) throw new Error("invalid run identifier")
  const runs = loadState(artifactRoot).runs
  return Object.hasOwn(runs, runId) ? runs[runId] : undefined
}

/** Remove only the validated pointer, retaining newer session and index updates. */
export function forgetRun(artifactRoot: string, sessionID: string, expected?: SessionRun): void {
  const entry = expected ?? loadState(artifactRoot).sessions[sessionID]
  if (!entry) return
  const state = loadState(artifactRoot)
  const current = state.sessions[sessionID]
  if (current?.runId !== entry.runId || current.skill !== entry.skill) return
  const sessions = { ...state.sessions }
  delete sessions[sessionID]
  saveState(artifactRoot, { ...state, sessions })
}
