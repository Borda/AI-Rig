/**
 * Per-commit trust records for a forked pull request.
 *
 * A forked PR is third-party code. Its own worktree may be inspected anywhere,
 * but verification (a test, gate or script) runs on the host only after the
 * user's explicit **Trust this PR** answer, or in a sandbox. The record is a
 * file under the artifact root, bound to one commit and one worktree path; it
 * expires and is never a general allow.
 */

import { spawnSync } from "node:child_process"
import { existsSync, readdirSync, realpathSync } from "node:fs"
import { tmpdir } from "node:os"
import { basename, dirname, isAbsolute, join, relative, resolve, sep } from "node:path"
import { resolveGitCommonDir } from "./paths.ts"
import { readJsonIfExists, writeJsonAtomic } from "./util/fsx.ts"

export interface TrustRecord {
  readonly schema: 1
  readonly commit: string
  readonly worktree: string
  readonly ref: string
  readonly words: string
  readonly created_at: string
  readonly expires_at: string
  readonly run_id?: string
}

export interface TrustStatus {
  readonly status: "trusted" | "absent" | "expired" | "mismatched-commit" | "invalid"
  readonly record?: TrustRecord
  readonly reason?: string
}

export const TRUST_SCHEMA = 1
export const TRUST_WORDS = "Trust this PR"

export function trustDirectory(artifactRoot: string): string {
  return join(artifactRoot, "trust")
}

export function trustPath(artifactRoot: string, commit: string): string {
  return join(trustDirectory(artifactRoot), `${commit}.json`)
}

/** Host temp root for review worktrees and sandbox copies. */
export function reviewTempRoot(tempRoot: string = tmpdir()): string {
  return join(tempRoot, "opencode")
}

/** Prove an absolute path is under `<temp>/opencode/review-*`. */
export function isReviewWorktree(path: string, tempRoot: string = tmpdir()): boolean {
  if (!isAbsolute(path)) return false
  const root = reviewTempRoot(tempRoot)
  let realRoot = resolve(root)
  let realPath = resolve(path)
  try {
    realRoot = realpathSync(root)
  } catch {
    // The temp root may not exist yet; the lexical root is the fallback.
  }
  try {
    realPath = realpathSync(resolve(path))
  } catch {
    // A worktree being added does not exist yet: resolve its parent so a
    // symlinked temp root still compares physically.
    try {
      realPath = join(realpathSync(dirname(resolve(path))), basename(resolve(path)))
    } catch {
      // The parent does not exist either; the lexical path is the fallback.
    }
  }
  const rel = relative(realRoot, realPath)
  if (!rel || rel.startsWith("..") || isAbsolute(rel)) return false
  // Exactly one segment: a nested path must not inherit its parent's trust.
  const parts = rel.split(sep)
  return parts.length === 1 && /^review-[A-Za-z0-9._-]+$/.test(parts[0] ?? "")
}

/** Resolve a worktree's HEAD; bounded and never a shell string. */
export function resolveHead(worktree: string): string | undefined {
  const result = spawnSync("git", ["-C", worktree, "rev-parse", "HEAD"], {
    encoding: "utf8",
    timeout: 1_000,
    stdio: ["ignore", "pipe", "ignore"],
  })
  const sha = result.stdout?.trim()
  return result.status === 0 && /^[0-9a-f]{40}$/.test(sha ?? "") ? sha : undefined
}

/** A record is usable only with every field the writer promises; a partial file is ignored. */
function validRecord(record: unknown): record is TrustRecord {
  if (!record || typeof record !== "object") return false
  const entry = record as Partial<TrustRecord>
  if (entry.schema !== TRUST_SCHEMA) return false
  if (typeof entry.commit !== "string" || !/^[0-9a-f]{40}$/.test(entry.commit)) return false
  if (typeof entry.worktree !== "string" || !isAbsolute(entry.worktree) || !isReviewWorktree(entry.worktree))
    return false
  if (typeof entry.ref !== "string" || !/^refs\/rig-review\/pr-\d+-[0-9a-f]{12}$/.test(entry.ref)) return false
  if (entry.words !== TRUST_WORDS) return false
  if (typeof entry.created_at !== "string" || !Number.isFinite(Date.parse(entry.created_at))) return false
  if (typeof entry.expires_at !== "string" || !Number.isFinite(Date.parse(entry.expires_at))) return false
  // A record may already be expired (negative test TTLs), but it cannot claim a
  // lifetime longer than the tool issues.
  return Date.parse(entry.expires_at) - Date.parse(entry.created_at) <= 25 * 60 * 60 * 1_000
}

function readRecords(artifactRoot: string): TrustRecord[] {
  const directory = trustDirectory(artifactRoot)
  if (!existsSync(directory)) return []
  const records: TrustRecord[] = []
  for (const name of readdirSync(directory)) {
    if (!name.endsWith(".json")) continue
    const record = readJsonIfExists<unknown>(join(directory, name))
    if (validRecord(record)) records.push(record)
  }
  return records
}

/** Read the record set for a commit, newest first. */
export function readTrust(artifactRoot: string, commit?: string): TrustRecord[] {
  const records = readRecords(artifactRoot)
  return commit ? records.filter((record) => record.commit === commit) : records
}

/** Write one record after the user's explicit answer. */
export function writeTrust(
  artifactRoot: string,
  input: { commit: string; worktree: string; ref: string; words: string; runId?: string; ttlMs?: number },
): TrustRecord {
  if (!/^[0-9a-f]{40}$/.test(input.commit)) throw new Error("trust commit must be a literal 40-hex SHA")
  if (!isAbsolute(input.worktree))
    throw new Error("trust worktree must be an absolute path under the host temp review root")
  if (!isReviewWorktree(input.worktree)) throw new Error("trust worktree must be under <temp>/opencode/review-*")
  const head = resolveHead(input.worktree)
  if (head !== input.commit)
    throw new Error(`trust worktree HEAD ${head ?? "<unresolved>"} does not match ${input.commit}`)
  if (!/^refs\/rig-review\/pr-\d+-[0-9a-f]{12}$/.test(input.ref)) throw new Error("trust ref is not the review ref")
  if (input.words !== TRUST_WORDS) throw new Error(`trust requires the user's literal '${TRUST_WORDS}' answer`)
  const created = new Date()
  const record: TrustRecord = {
    schema: TRUST_SCHEMA,
    commit: input.commit,
    worktree: realpathSync(input.worktree),
    ref: input.ref,
    words: input.words,
    created_at: created.toISOString(),
    expires_at: new Date(created.getTime() + (input.ttlMs ?? 24 * 60 * 60 * 1_000)).toISOString(),
    ...(input.runId ? { run_id: input.runId } : {}),
  }
  writeJsonAtomic(trustPath(artifactRoot, input.commit), record)
  return record
}

/** Decide whether a verification may run on the host for this directory. */
export function trustFor(artifactRoot: string, directory: string, now: number = Date.now()): TrustStatus {
  let real: string
  try {
    real = realpathSync(directory)
  } catch {
    return { status: "absent", reason: "directory does not exist" }
  }
  const records = readRecords(artifactRoot)
  const matching = records.filter((record) => {
    try {
      return realpathSync(record.worktree) === real
    } catch {
      return false
    }
  })
  if (matching.length === 0) return { status: "absent", reason: "no trust record for this worktree" }
  const live = matching.filter((record) => Date.parse(record.expires_at) > now)
  if (live.length === 0) return { status: "expired", reason: "every trust record for this worktree has expired" }
  const head = resolveHead(real)
  if (!head) return { status: "invalid", reason: "cannot resolve the worktree HEAD" }
  const record = live.find((entry) => entry.commit === head)
  if (!record) return { status: "mismatched-commit", reason: `worktree HEAD ${head} has no trust record` }
  return { status: "trusted", record }
}

/** Parse one leading `cd <dir> &&|;` and return the effective directory. */
export interface EffectiveCommand {
  readonly directory: string
  readonly command: string
  readonly prefixed: boolean
}

export function effectiveCommand(command: string, projectRoot: string): EffectiveCommand | undefined {
  const text = command.trim()
  const match = /^cd\s+(?:"([^"]+)"|'([^']+)'|([^\s;&|<>]+))\s*(?:&&|;)\s*([\s\S]+)$/.exec(text)
  // A malformed cd prefix is not a plain command in the project root.
  if (!match) return /^cd(?:\s|$)/.test(text) ? undefined : { directory: projectRoot, command: text, prefixed: false }
  const target = match[1] ?? match[2] ?? match[3]
  const rest = match[4]?.trim() ?? ""
  if (!target || !rest || !isAbsolute(target)) return undefined
  return { directory: resolve(target), command: rest, prefixed: true }
}

/** True when the worktree HEAD is an ancestor of the user's own HEAD. */
export function isOwnWorktree(projectRoot: string, directory: string): boolean {
  // A foreign repository can share objects (and therefore ancestors) with the
  // project; only a worktree of this repository's common dir counts as own.
  const projectCommon = resolveGitCommonDir(projectRoot)
  const directoryCommon = resolveGitCommonDir(directory)
  if (!projectCommon || !directoryCommon || projectCommon !== directoryCommon) return false
  const directoryHead = resolveHead(directory)
  const projectHead = resolveHead(projectRoot)
  if (!directoryHead || !projectHead) return false
  if (directoryHead === projectHead) return true
  const result = spawnSync("git", ["-C", projectRoot, "merge-base", "--is-ancestor", directoryHead, projectHead], {
    timeout: 1_000,
    stdio: "ignore",
  })
  return result.status === 0
}
