/**
 * Source-scope collection and drift detection.
 *
 * Scope evidence binds a run to exact bytes. A later finalize compares the
 * recomputed inventory with the recorded one, so edits made after collection
 * cannot be retroactively certified. Collection is explicit: only files that
 * changed under the requested paths or were named directly become in-scope.
 */

import { spawnSync } from "node:child_process"
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs"
import { join, relative, sep } from "node:path"
import { scopeDigest, sha256, type ScopeEntry } from "./digest.ts"
import type { ScopeRecord } from "./run.ts"

const EMPTY_SHA256 = sha256("")
const MAX_WALK_FILES = 5_000
const IGNORED_SEGMENTS = new Set(["node_modules", ".git", ".opencode-rig", ".reports", ".cache", "dist", "coverage"])

function git(cwd: string, args: readonly string[]): string | undefined {
  const result = spawnSync("git", [...args], { cwd, encoding: "utf8" })
  if (result.status !== 0) return undefined
  return result.stdout
}

function gitBuffer(cwd: string, args: readonly string[]): Buffer | undefined {
  const result = spawnSync("git", [...args], { cwd })
  if (result.status !== 0) return undefined
  return result.stdout
}

function hashFile(absolute: string): { status: string; sha256: string } | undefined {
  if (!existsSync(absolute)) return undefined
  if (statSync(absolute).isDirectory()) return undefined
  return { status: "M", sha256: sha256(readFileSync(absolute)) }
}

function walk(root: string, current: string, out: string[]): void {
  if (out.length >= MAX_WALK_FILES) return
  for (const entry of readdirSync(current, { withFileTypes: true })) {
    if (IGNORED_SEGMENTS.has(entry.name)) continue
    const absolute = join(current, entry.name)
    if (entry.isDirectory()) walk(root, absolute, out)
    else if (entry.isFile()) out.push(relative(root, absolute).split(sep).join("/"))
  }
}

function collectChanged(cwd: string, paths: readonly string[]): ScopeEntry[] {
  const status = git(cwd, ["status", "--porcelain=v1", "-z", "--untracked-files=all"])
  if (status === undefined) return []
  const records = status.split("\0")
  const entries: ScopeEntry[] = []
  for (let index = 0; index < records.length; index += 1) {
    const record = records[index]
    if (!record) continue
    const code = record.slice(0, 2)
    let path = record.slice(3)
    if (code.startsWith("R") || code.startsWith("C")) {
      const renamed = records[index + 1]
      if (renamed) {
        path = renamed
        index += 1
      }
    }
    if (paths.length > 0 && !paths.some((prefix) => path === prefix || path.startsWith(`${prefix}/`))) continue
    const deleted = code.includes("D")
    entries.push({
      path,
      status: code,
      sha256: deleted ? EMPTY_SHA256 : (hashFile(join(cwd, path))?.sha256 ?? EMPTY_SHA256),
    })
  }
  return entries
}

function collectExplicit(cwd: string, paths: readonly string[]): ScopeEntry[] {
  const files: string[] = []
  for (const path of paths) {
    const absolute = join(cwd, path)
    if (!existsSync(absolute)) continue
    if (statSync(absolute).isDirectory()) walk(cwd, absolute, files)
    else files.push(path)
  }
  return files.map((path) => {
    const hashed = hashFile(join(cwd, path))
    return { path, status: hashed?.status ?? "?", sha256: hashed?.sha256 ?? EMPTY_SHA256 }
  })
}

function mergeEntries(...groups: ScopeEntry[][]): ScopeEntry[] {
  const byPath = new Map<string, ScopeEntry>()
  for (const group of groups) {
    for (const entry of group) byPath.set(entry.path, entry)
  }
  return [...byPath.values()].sort((left, right) => left.path.localeCompare(right.path))
}

export function scopeBase(cwd: string): string | null {
  const value = git(cwd, ["rev-parse", "HEAD"])
  return value === undefined ? null : value.trim()
}

/**
 * Collect exact working-tree bytes for the requested paths, or every changed
 * path when no filter is supplied. Explicit file paths are always hashed, so a
 * review of an unchanged file still has authoritative source.
 */
export function collectScope(cwd: string, paths: readonly string[] = []): ScopeRecord {
  const normalizedPaths = paths.map((path) => path.split(sep).join("/"))
  const entries = mergeEntries(collectChanged(cwd, normalizedPaths), collectExplicit(cwd, normalizedPaths))
  return {
    base: scopeBase(cwd),
    paths: normalizedPaths,
    dirty: entries.length > 0,
    entries,
    digest: scopeDigest(entries),
  }
}

/**
 * Collect the inventory of a single commit. The recorded bytes come from Git,
 * not the working tree, so later edits cannot change what was reviewed.
 */
export function collectCommitScope(cwd: string, commit: string, paths: readonly string[] = []): ScopeRecord {
  const normalizedPaths = paths.map((path) => path.split(sep).join("/"))
  const listing = git(cwd, ["diff-tree", "--no-commit-id", "--name-status", "-r", "--root", commit])
  if (listing === undefined) throw new Error(`cannot read commit ${commit}`)
  const entries: ScopeEntry[] = []
  for (const line of listing.split("\n")) {
    if (!line.trim()) continue
    const [code, ...parts] = line.split("\t")
    const path = parts[parts.length - 1]
    if (!code || !path) continue
    if (
      normalizedPaths.length > 0 &&
      !normalizedPaths.some((prefix) => path === prefix || path.startsWith(`${prefix}/`))
    )
      continue
    const blob = code.startsWith("D") ? undefined : gitBuffer(cwd, ["show", `${commit}:${path}`])
    entries.push({ path, status: code, sha256: blob ? sha256(blob) : EMPTY_SHA256 })
  }
  return {
    base: scopeBase(cwd),
    commit,
    paths: normalizedPaths,
    dirty: entries.length > 0,
    entries,
    digest: scopeDigest(entries),
  }
}

/**
 * Recompute an existing scope with its recorded filter. This is the only way a
 * run adopts post-change source; finalize compares whatever was recorded last.
 */
export function refreshScope(cwd: string, scope: ScopeRecord): ScopeRecord {
  if (scope.commit) return collectCommitScope(cwd, scope.commit, scope.paths)
  const next = collectScope(cwd, scope.paths)
  return { ...next, base: scope.base }
}

/**
 * A commit scope is immutable, so only a working-tree scope can drift.
 */
export function detectDrift(cwd: string, scope: ScopeRecord): boolean {
  if (scope.commit) return false
  return refreshScope(cwd, scope).digest !== scope.digest
}
