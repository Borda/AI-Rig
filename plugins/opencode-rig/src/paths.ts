/**
 * Project and Git path resolution.
 *
 * Git lookups strip environment overrides before invoking Git so a planted
 * `GIT_DIR` or a bare-repository fixture cannot redirect the approval-grant
 * check to an attacker-controlled directory.
 */

import { spawnSync } from "node:child_process"
import { existsSync } from "node:fs"
import { isAbsolute, join, relative, resolve, sep } from "node:path"

const GIT_OVERRIDE_ENV = [
  "GIT_DIR",
  "GIT_WORK_TREE",
  "GIT_COMMON_DIR",
  "GIT_INDEX_FILE",
  "GIT_OBJECT_DIRECTORY",
  "GIT_ALTERNATE_OBJECT_DIRECTORIES",
  "GIT_NAMESPACE",
  "GIT_PREFIX",
] as const

export interface RigPaths {
  readonly projectRoot: string
  readonly artifactRoot: string
  readonly runsRoot: string
  readonly statePath: string
}

function cleanGitEnv(): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = { ...process.env }
  for (const key of GIT_OVERRIDE_ENV) delete env[key]
  return env
}

function git(cwd: string, args: readonly string[]): string | undefined {
  const result = spawnSync("git", [...args], { cwd, env: cleanGitEnv(), encoding: "utf8" })
  if (result.status !== 0) return undefined
  return result.stdout.trim()
}

export function isBareRepository(cwd: string): boolean | undefined {
  const value = git(cwd, ["rev-parse", "--is-bare-repository"])
  if (value === undefined) return undefined
  return value === "true"
}

/**
 * Resolve the repository's common Git directory, shared by every worktree.
 * Returns undefined outside a repository or for a bare repository.
 */
export function resolveGitCommonDir(cwd: string): string | undefined {
  if (isBareRepository(cwd) !== false) return undefined
  const value = git(cwd, ["rev-parse", "--git-common-dir"])
  if (value === undefined) return undefined
  const absolute = isAbsolute(value) ? value : resolve(cwd, value)
  return existsSync(absolute) ? absolute : undefined
}

export function resolveProjectRoot(cwd: string): string {
  const top = git(cwd, ["rev-parse", "--show-toplevel"])
  return top === undefined ? resolve(cwd) : top
}

/**
 * Resolve the artifact root, refusing an artifacts setting that escapes the
 * project directory.
 */
export function resolvePaths(cwd: string, artifacts: string): RigPaths {
  const projectRoot = resolveProjectRoot(cwd)
  const artifactRoot = isAbsolute(artifacts) ? resolve(artifacts) : resolve(projectRoot, artifacts)
  const rel = relative(projectRoot, artifactRoot)
  if (rel.startsWith(`..${sep}`) || rel === ".." || isAbsolute(rel)) {
    throw new Error(`artifact root must stay inside the project: ${artifactRoot}`)
  }
  return {
    projectRoot,
    artifactRoot,
    runsRoot: join(artifactRoot, "runs"),
    statePath: join(artifactRoot, "state.json"),
  }
}

/**
 * Allocate a UTC, filesystem-safe run identifier with a short random suffix.
 */
export function createRunId(now: Date = new Date(), random: () => number = Math.random): string {
  const stamp = now
    .toISOString()
    .replace(/\.\d{3}Z$/, "Z")
    .replace(/[:]/g, "-")
  const suffix = Math.floor(random() * 0xffff)
    .toString(16)
    .padStart(4, "0")
  return `${stamp}-${suffix}`
}
