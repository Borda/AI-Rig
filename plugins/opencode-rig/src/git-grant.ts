/**
 * Local Git approval grant.
 *
 * The grant lives in the repository's common Git directory, which Git never
 * tracks. A symlink, nonregular file, changed file identity, or unknown schema
 * counts as no grant. The common directory itself remains a trusted boundary.
 */

import { randomUUID } from "node:crypto"
import {
  closeSync,
  constants,
  fstatSync,
  lstatSync,
  openSync,
  readFileSync,
  realpathSync,
  renameSync,
  statSync,
  unlinkSync,
  writeFileSync,
} from "node:fs"
import { join } from "node:path"
import { PLUGIN_VERSION, SCHEMA_VERSION } from "./version.ts"
import { sha256 } from "./digest.ts"

export const GRANT_FILENAME = "opencode-rig-approval.json"
const GRANT_LIFETIME_MS = 30 * 24 * 60 * 60 * 1_000

export interface GrantRecord {
  readonly schema: number
  readonly scope: "local-git"
  readonly source: "user-approve-always"
  readonly plugin_version: string
  readonly created_at: string
  readonly confirmation: "Approve always"
  readonly approval_id: string
  readonly repository_binding: string
  readonly expires_at: string
}

export type GrantStatus = "granted" | "absent" | "invalid" | "no-git"

export interface GrantState {
  readonly status: GrantStatus
  readonly path?: string
  readonly record?: GrantRecord
  readonly reason?: string
}

export function grantPath(commonDir: string): string {
  return join(commonDir, GRANT_FILENAME)
}

/**
 * Bind a grant to the physical repository directory that received approval.
 * The native resolver expands Windows 8.3 short names and normalizes case, so
 * every spelling of one directory yields the same binding.
 */
function repositoryBinding(commonDir: string): string {
  const directory = realpathSync.native(commonDir)
  const stats = statSync(directory)
  return sha256(JSON.stringify([directory, stats.dev, stats.ino]))
}

/** Check persisted approval provenance and its bounded lifetime. */
function approvalProblem(record: Record<string, unknown>): string | undefined {
  if (record.confirmation !== "Approve always") return "missing literal Approve always confirmation"
  if (typeof record.approval_id !== "string" || !/^[a-f0-9-]{36}$/.test(record.approval_id))
    return "invalid approval id"
  if (typeof record.repository_binding !== "string" || !/^[a-f0-9]{64}$/.test(record.repository_binding))
    return "invalid repository binding"
  if (typeof record.expires_at !== "string") return "missing expires_at"
  const created = Date.parse(String(record.created_at))
  const expires = Date.parse(record.expires_at)
  if (!Number.isFinite(created) || !Number.isFinite(expires)) return "invalid approval timestamps"
  if (created > Date.now()) return "approval creation is in the future"
  if (expires <= created || expires - created > GRANT_LIFETIME_MS) return "invalid approval lifetime"
  if (expires <= Date.now()) return "approval expired"
  return undefined
}

export function validateGrant(raw: unknown): { ok: true; record: GrantRecord } | { ok: false; reason: string } {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
    return { ok: false, reason: "grant record must be an object" }
  }
  const record = raw as Record<string, unknown>
  if (record.schema !== SCHEMA_VERSION) return { ok: false, reason: `unknown schema ${String(record.schema)}` }
  if (record.scope !== "local-git") return { ok: false, reason: `unknown scope ${String(record.scope)}` }
  if (record.source !== "user-approve-always") return { ok: false, reason: "unknown source" }
  if (typeof record.created_at !== "string") return { ok: false, reason: "missing created_at" }
  const problem = approvalProblem(record)
  if (problem) return { ok: false, reason: problem }
  return {
    ok: true,
    record: {
      schema: SCHEMA_VERSION,
      scope: "local-git",
      source: "user-approve-always",
      plugin_version: typeof record.plugin_version === "string" ? record.plugin_version : "unknown",
      created_at: record.created_at,
      confirmation: "Approve always",
      approval_id: record.approval_id as string,
      repository_binding: record.repository_binding as string,
      expires_at: record.expires_at as string,
    },
  }
}

export function readGrant(commonDir: string | undefined): GrantState {
  if (!commonDir) return { status: "no-git" }
  const path = grantPath(commonDir)
  let descriptor: number | undefined
  try {
    const stats = lstatSync(path, { throwIfNoEntry: false })
    if (!stats) return { status: "absent", path }
    if (stats.isSymbolicLink()) return { status: "invalid", path, reason: "grant path is a symlink" }
    if (!stats.isFile()) return { status: "invalid", path, reason: "grant path is not a regular file" }
    // Bind validation and reading to one file. Identity checks also cover hosts
    // without O_NOFOLLOW; nonblocking open prevents a swapped FIFO from hanging.
    descriptor = openSync(path, constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0) | (constants.O_NONBLOCK ?? 0))
    const opened = fstatSync(descriptor)
    const current = lstatSync(path)
    if (current.isSymbolicLink()) return { status: "invalid", path, reason: "grant path is a symlink" }
    if (!opened.isFile() || !current.isFile())
      return { status: "invalid", path, reason: "grant path is not a regular file" }
    if (
      opened.dev !== stats.dev ||
      opened.ino !== stats.ino ||
      current.dev !== opened.dev ||
      current.ino !== opened.ino
    )
      return { status: "invalid", path, reason: "grant file changed while opening" }
    const parsed = validateGrant(JSON.parse(readFileSync(descriptor, "utf8")))
    if (parsed.ok && parsed.record.repository_binding !== repositoryBinding(commonDir))
      return { status: "invalid", path, reason: "grant belongs to a different repository" }
    return parsed.ok
      ? { status: "granted", path, record: parsed.record }
      : { status: "invalid", path, reason: parsed.reason }
  } catch (error) {
    return { status: "invalid", path, reason: error instanceof Error ? error.message : "unreadable grant" }
  } finally {
    if (descriptor !== undefined) closeSync(descriptor)
  }
}

/**
 * Write a grant record. Callers must have obtained an explicit user answer;
 * the permission hook never calls this function.
 */
export function writeGrant(commonDir: string, confirmation = "Approve always"): GrantRecord {
  if (confirmation !== "Approve always")
    throw new Error("persistent grants require literal Approve always confirmation")
  const path = grantPath(commonDir)
  const stats = lstatSync(path, { throwIfNoEntry: false })
  if (stats && !stats.isFile()) throw new Error("grant path is not a regular file")
  const created = Date.now()
  const record: GrantRecord = {
    schema: SCHEMA_VERSION,
    scope: "local-git",
    source: "user-approve-always",
    plugin_version: PLUGIN_VERSION,
    created_at: new Date(created).toISOString(),
    confirmation,
    approval_id: randomUUID(),
    repository_binding: repositoryBinding(commonDir),
    expires_at: new Date(created + GRANT_LIFETIME_MS).toISOString(),
  }
  // An exclusive temporary file plus rename never follows a destination
  // symlink or modifies another hard link to a previous grant.
  const temporary = join(commonDir, `.${GRANT_FILENAME}.${randomUUID()}.tmp`)
  const descriptor = openSync(temporary, "wx", 0o600)
  try {
    try {
      writeFileSync(descriptor, `${JSON.stringify(record, null, 2)}\n`, "utf8")
    } finally {
      closeSync(descriptor)
    }
    renameSync(temporary, path)
  } finally {
    try {
      unlinkSync(temporary)
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error
    }
  }
  return record
}

export function revokeGrant(commonDir: string): boolean {
  try {
    unlinkSync(grantPath(commonDir))
    return true
  } catch {
    return false
  }
}
