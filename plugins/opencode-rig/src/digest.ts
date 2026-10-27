/**
 * Content digests used by run evidence.
 *
 * Digests are always lowercase SHA-256 hex so evidence produced by different
 * hosts can be compared without normalization.
 */

import { createHash } from "node:crypto"
import { readFileSync } from "node:fs"

export function sha256(input: string | Buffer): string {
  return createHash("sha256").update(input).digest("hex")
}

export function sha256File(path: string): string {
  return sha256(readFileSync(path))
}

export interface ScopeEntry {
  readonly path: string
  readonly status: string
  readonly sha256: string
}

/**
 * Digest a scope inventory. Entries are sorted by path so filesystem order
 * never changes the result.
 */
export function scopeDigest(entries: readonly ScopeEntry[]): string {
  const normalized = [...entries]
    .sort((left, right) => left.path.localeCompare(right.path))
    .map((entry) => `${entry.status}\t${entry.path}\t${entry.sha256}`)
    .join("\n")
  return sha256(normalized)
}

export function isDigest(value: unknown): value is string {
  return typeof value === "string" && /^[a-f0-9]{64}$/.test(value)
}
