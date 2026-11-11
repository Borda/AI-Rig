/**
 * Source-scope collection and drift detection.
 *
 * Scope evidence binds a run to exact bytes. A later finalize compares the
 * recomputed inventory with the recorded one, so edits made after collection
 * cannot be retroactively certified. Collection is explicit: only files that
 * changed under the requested paths or were named directly become in-scope.
 */

import { spawnSync } from "node:child_process"
import { isUtf8 } from "node:buffer"
import { existsSync, lstatSync, readdirSync, readFileSync, realpathSync, statSync } from "node:fs"
import { dirname, isAbsolute, join, relative, resolve, sep } from "node:path"
import { scopeDigest, sha256, type ScopeEntry } from "./digest.ts"
import type { ScopeRecord } from "./run.ts"

const EMPTY_SHA256 = sha256("")
const MAX_WALK_FILES = 5_000
const IGNORED_SEGMENTS = new Set(["node_modules", ".git", ".opencode-rig", ".reports", ".cache", "dist", "coverage"])

function git(cwd: string, args: readonly string[]): string | undefined {
  return gitBuffer(cwd, args)?.toString("utf8")
}

/** Run Git without environment redirection or executable filesystem monitors. */
function gitBuffer(cwd: string, args: readonly string[], discover = false): Buffer | undefined {
  const env = { ...process.env }
  for (const key of Object.keys(env)) {
    if (key.toUpperCase().startsWith("GIT_")) delete env[key]
  }
  const result = spawnSync("git", ["-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false", ...args], {
    cwd,
    env: { ...env, LC_ALL: "C" },
    maxBuffer: 64 * 1024 * 1024,
  })
  if ((result.error as NodeJS.ErrnoException | undefined)?.code === "ENOBUFS")
    throw new Error("Git output exceeded the 64 MiB limit; evidence collection aborted")
  if (result.error) throw new Error(`cannot run Git for evidence: ${result.error.message}`)
  if (result.status !== 0) {
    if (discover && !/not a git repository/i.test(result.stderr?.toString() ?? ""))
      throw new Error("cannot collect working-tree status: Git repository discovery failed")
    return undefined
  }
  return result.stdout
}

/** Refuse lexical escapes and existing symlink ancestors outside the project. */
function resolveScopePath(cwd: string, path: string): string {
  const absolute = resolve(cwd, path)
  const lexical = relative(resolve(cwd), absolute)
  if (path.split(sep).includes("..") || lexical === ".." || lexical.startsWith(`..${sep}`) || isAbsolute(lexical)) {
    throw new Error(`scope path escapes the project: ${path}`)
  }
  let ancestor = absolute
  while (!lstatSync(ancestor, { throwIfNoEntry: false })) ancestor = dirname(ancestor)
  const physical = relative(realpathSync(cwd), realpathSync(ancestor))
  if (physical === ".." || physical.startsWith(`..${sep}`) || isAbsolute(physical)) {
    throw new Error(`scope path resolves outside the project: ${path}`)
  }
  return absolute
}

/** Normalize scope filters without losing root scopes or native absolute paths. */
function normalizePaths(cwd: string, paths: readonly string[]): string[] {
  return paths.map((path) => relative(resolve(cwd), resolveScopePath(cwd, path)).split(sep).join("/") || ".")
}

function hashFile(cwd: string, path: string): { status: string; sha256: string } | undefined {
  const absolute = resolveScopePath(cwd, path)
  if (!existsSync(absolute)) return undefined
  if (statSync(absolute).isDirectory()) return undefined
  return { status: "M", sha256: sha256(readFileSync(absolute)) }
}

function walk(root: string, current: string, out: string[]): void {
  if (out.length >= MAX_WALK_FILES) return
  for (const entry of readdirSync(current, { withFileTypes: true })) {
    if (IGNORED_SEGMENTS.has(entry.name) || entry.isSymbolicLink()) continue
    const absolute = resolveScopePath(root, join(current, entry.name))
    if (entry.isDirectory()) walk(root, absolute, out)
    else if (entry.isFile()) out.push(relative(root, absolute).split(sep).join("/"))
  }
}

function collectChanged(cwd: string, paths: readonly string[]): ScopeEntry[] {
  // Plain directories keep explicit filesystem scopes; failed repository status never certifies an empty scope.
  const repository = gitBuffer(cwd, ["rev-parse", "--is-inside-work-tree"], true)
  if (repository === undefined) return []
  const status = gitBuffer(cwd, ["status", "--porcelain=v1", "-z", "--untracked-files=all"])
  if (status === undefined) throw new Error("cannot collect working-tree status: Git status failed")
  const records = nulRecords(status)
  const entries: ScopeEntry[] = []
  for (let index = 0; index < records.length; index += 1) {
    const record = records[index]
    if (!record?.length) continue
    const code = record.subarray(0, 2).toString("ascii")
    const rawPath = record.subarray(3)
    const path = evidencePath(rawPath)
    if (/[RC]/.test(code)) index += 1
    if (paths.length > 0 && !paths.some((prefix) => prefix === "." || path === prefix || path.startsWith(`${prefix}/`)))
      continue
    const deleted = code.includes("D")
    entries.push({
      path,
      status: code,
      sha256: deleted ? EMPTY_SHA256 : hashChangedFile(cwd, rawPath),
    })
  }
  return entries
}

/** Split Git's NUL-delimited output without decoding filename bytes. */
function nulRecords(value: Buffer): Buffer[] {
  const records: Buffer[] = []
  let start = 0
  for (let end = value.indexOf(0); end !== -1; end = value.indexOf(0, start)) {
    records.push(value.subarray(start, end))
    start = end + 1
  }
  if (start !== value.length) throw new Error("truncated Git path inventory")
  return records
}

/** Preserve invalid UTF-8 filenames as distinct, printable byte identities. */
function evidencePath(path: Buffer): string {
  return isUtf8(path) ? path.toString("utf8") : `git-bytes:${path.toString("hex")}`
}

/** Hash the original filename bytes after checking physical containment. */
function hashChangedFile(cwd: string, path: Buffer): string {
  const absolute = Buffer.concat([Buffer.from(resolve(cwd) + sep), path])
  if (!existsSync(absolute)) return EMPTY_SHA256
  const physical = realpathSync(absolute, { encoding: "buffer" })
  const root = Buffer.from(realpathSync(cwd) + sep)
  if (!physical.subarray(0, root.length).equals(root))
    throw new Error(`scope path resolves outside the project: ${evidencePath(path)}`)
  if (statSync(absolute).isDirectory()) return EMPTY_SHA256
  return sha256(readFileSync(absolute))
}

function collectExplicit(cwd: string, paths: readonly string[], changed: readonly ScopeEntry[]): ScopeEntry[] {
  const files: string[] = []
  for (const path of paths) {
    const absolute = resolveScopePath(cwd, path)
    if (!existsSync(absolute)) {
      if (
        changed.some(
          (entry) => (entry.path === path || entry.path.startsWith(`${path}/`)) && entry.status.includes("D"),
        )
      )
        continue
      throw new Error(`scope path does not exist: ${path}`)
    }
    if (statSync(absolute).isDirectory()) walk(cwd, absolute, files)
    else files.push(path)
  }
  return files.map((path) => {
    const hashed = hashFile(cwd, path)
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
  const normalizedPaths = normalizePaths(cwd, paths)
  const changed = collectChanged(cwd, normalizedPaths)
  const entries = mergeEntries(changed, collectExplicit(cwd, normalizedPaths, changed))
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
  const normalizedPaths = normalizePaths(cwd, paths)
  if (commit.includes("..")) throw new Error("commit evidence requires a single commit, not a range")
  if (!/^[^-]/.test(commit)) throw new Error(`cannot read commit ${commit}`)
  const oid = git(cwd, ["rev-parse", "--verify", "--end-of-options", `${commit}^{commit}`])?.trim()
  if (!oid || !/^(?:[a-f0-9]{40}|[a-f0-9]{64})$/.test(oid)) throw new Error(`cannot read commit ${commit}`)
  const ancestry = git(cwd, ["rev-list", "--parents", "--max-count=1", oid, "--"])
  if (ancestry === undefined) throw new Error(`cannot read commit ${commit}`)
  const firstParent = ancestry.trim().split(/\s+/)[1]
  const listing = gitBuffer(cwd, [
    "diff-tree",
    "--no-commit-id",
    "--raw",
    "--no-abbrev",
    "-r",
    "--root",
    "-z",
    "--no-renames",
    "--end-of-options",
    ...(firstParent ? [firstParent] : []),
    oid,
    "--",
  ])
  if (listing === undefined) throw new Error(`cannot read commit ${commit}`)
  const entries: ScopeEntry[] = []
  const records = nulRecords(listing)
  for (let index = 0; index < records.length; index += 2) {
    const header = records[index]?.toString("ascii").split(" ")
    const rawPath = records[index + 1]
    if (!header || !rawPath) continue
    const code = header[4]!
    const path = evidencePath(rawPath)
    if (
      normalizedPaths.length > 0 &&
      !normalizedPaths.some((prefix) => prefix === "." || path === prefix || path.startsWith(`${prefix}/`))
    )
      continue
    const gitlink = header[1] === "160000"
    const blob = code.startsWith("D")
      ? undefined
      : gitlink
        ? Buffer.from(header[3]!)
        : gitBuffer(cwd, ["show", "--no-ext-diff", "--no-textconv", "--end-of-options", header[3]!])
    if (!code.startsWith("D") && blob === undefined) throw new Error(`cannot read committed path: ${path}`)
    entries.push({ path, status: code, sha256: blob ? sha256(blob) : EMPTY_SHA256 })
  }
  if (entries.length === 0) throw new Error(`empty commit scope: ${commit}`)
  return {
    base: scopeBase(cwd),
    commit: oid,
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
