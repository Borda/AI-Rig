/**
 * Small mutable plugin state.
 *
 * Single-value state is exempt from append-only rules: it records only the
 * currently active run per session so tools can avoid repeating `skill` on
 * every call. Run evidence itself lives in the run directory and is append-only
 * or write-once.
 */

import { existsSync, readFileSync } from "node:fs"
import { join } from "node:path"
import { writeJsonAtomic } from "./util/fsx.ts"

export interface SessionRun {
  readonly runId: string
  readonly skill: string
}

export interface RigState {
  readonly schema: number
  readonly runs: Record<string, string>
  readonly sessions: Record<string, SessionRun>
}

const EMPTY: RigState = { schema: 1, runs: {}, sessions: {} }

export function loadState(artifactRoot: string): RigState {
  const path = join(artifactRoot, "state.json")
  if (!existsSync(path)) return EMPTY
  try {
    const parsed = JSON.parse(readFileSync(path, "utf8")) as RigState
    return { schema: 1, runs: parsed.runs ?? {}, sessions: parsed.sessions ?? {} }
  } catch {
    return EMPTY
  }
}

export function saveState(artifactRoot: string, state: RigState): void {
  writeJsonAtomic(join(artifactRoot, "state.json"), state)
}

export function rememberRun(artifactRoot: string, sessionID: string, entry: SessionRun): RigState {
  const state = loadState(artifactRoot)
  const next: RigState = {
    schema: 1,
    runs: { ...state.runs, [entry.runId]: entry.skill },
    sessions: { ...state.sessions, [sessionID]: entry },
  }
  saveState(artifactRoot, next)
  return next
}

export function activeRun(artifactRoot: string, sessionID: string): SessionRun | undefined {
  return loadState(artifactRoot).sessions[sessionID]
}

export function skillForRun(artifactRoot: string, runId: string): string | undefined {
  return loadState(artifactRoot).runs[runId]
}

export function forgetRun(artifactRoot: string, sessionID: string): void {
  const state = loadState(artifactRoot)
  const sessions = { ...state.sessions }
  delete sessions[sessionID]
  saveState(artifactRoot, { ...state, sessions })
}
