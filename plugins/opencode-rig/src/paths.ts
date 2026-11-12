/**
 * Project and Git path resolution.
 *
 * Git lookups strip environment overrides before invoking Git so a planted
 * `GIT_DIR` or a bare-repository fixture cannot redirect the approval-grant
 * check to an attacker-controlled directory.
 */

import { existsSync, lstatSync, realpathSync } from "node:fs"
import { dirname, isAbsolute, join, relative, resolve, sep } from "node:path"
import { runGit } from "./util/git.ts"

export interface RigPaths {
  readonly projectRoot: string
  readonly artifactRoot: string
  readonly runsRoot: string
  readonly statePath: string
}

function git(cwd: string, args: readonly string[]): string | undefined {
  const result = runGit(cwd, args)
  if (result.error || result.status !== 0) return undefined
  return result.stdout.toString("utf8").trim()
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
  let ancestor = artifactRoot
  while (!lstatSync(ancestor, { throwIfNoEntry: false })) ancestor = dirname(ancestor)
  const physical = relative(realpathSync(projectRoot), realpathSync(ancestor))
  if (physical === ".." || physical.startsWith(`..${sep}`) || isAbsolute(physical)) {
    throw new Error(`artifact root resolves outside the project: ${artifactRoot}`)
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
