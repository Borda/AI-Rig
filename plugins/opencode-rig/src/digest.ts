/**
 * Content digests used by run evidence.
 *
 * Digests are always lowercase SHA-256 hex so evidence produced by different
 * hosts can be compared without normalization.
 */

import { createHash } from "node:crypto"
import { closeSync, openSync, readFileSync, readSync, statSync } from "node:fs"

export function sha256(input: string | Buffer): string {
  return createHash("sha256").update(input).digest("hex")
}

/** Hash a regular file; a non-regular path would block or stream forever. */
export function sha256File(path: string): string {
  if (!statSync(path).isFile()) throw new Error(`not a regular file: ${String(path)}`)
  return sha256Path(path)
}

/** Hash a path in bounded chunks; a non-regular path still blocks the open. */
export function sha256Path(path: string | Buffer): string {
  const hash = createHash("sha256")
  // Read at most the size seen at open: a file another process keeps appending
  // to cannot make this loop unbounded.
  const expected = statSync(path).size
  const handle = openSync(path, "r")
  try {
    const buffer = Buffer.alloc(1024 * 1024)
    let total = 0
    while (total < expected) {
      const read = readSync(handle, buffer, 0, Math.min(buffer.length, expected - total), null)
      if (read <= 0) break
      hash.update(buffer.subarray(0, read))
      total += read
    }
  } finally {
    closeSync(handle)
  }
  return hash.digest("hex")
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
