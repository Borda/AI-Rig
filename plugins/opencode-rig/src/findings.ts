/**
 * Finding records and bucket accounting.
 *
 * A finding is only closed by a status plus a disposition. Validation rejects
 * duplicate IDs and empty evidence so a remediation report cannot silently
 * drop an obligation.
 */

import { ValidationError, requireEnum, requireString } from "./util/schema.ts"

export const SEVERITIES = ["security", "critical", "high", "medium", "low", "nit"] as const
export const DIMENSIONS = ["correctness", "security", "tests", "api", "performance", "docs", "data"] as const
export const STATUSES = ["open", "fixed", "rejected", "deferred", "accepted-risk"] as const

export type Severity = (typeof SEVERITIES)[number]
export type Dimension = (typeof DIMENSIONS)[number]
export type FindingStatus = (typeof STATUSES)[number]

export interface Finding {
  readonly id: string
  readonly severity: Severity
  readonly dimension: Dimension
  readonly file: string
  readonly line?: number
  readonly title: string
  readonly evidence: string
  readonly status: FindingStatus
  readonly disposition?: string
  readonly owner?: string
  readonly verified_by?: string
}

export interface FindingsDocument {
  readonly schema: number
  readonly run_id: string
  readonly target_digest: string
  readonly axes: readonly string[]
  readonly coverage: readonly string[]
  readonly findings: readonly Finding[]
}

export interface BucketCounts {
  readonly open: number
  readonly fixed: number
  readonly rejected: number
  readonly deferred: number
  readonly accepted_risk: number
}

const ID_PATTERN = /^F-[A-Za-z0-9-]+$/

export function validateFinding(raw: unknown): Finding {
  if (typeof raw !== "object" || raw === null) throw new ValidationError("finding must be an object")
  const record = raw as Record<string, unknown>
  const id = requireString(record.id, "id")
  if (!ID_PATTERN.test(id)) throw new ValidationError("finding 'id' must match F-<alphanumeric>")
  const line = record.line === undefined ? undefined : Number(record.line)
  if (line !== undefined && (!Number.isInteger(line) || line < 1)) {
    throw new ValidationError("finding 'line' must be a positive integer")
  }
  return {
    id,
    severity: requireEnum(record.severity, "severity", SEVERITIES),
    dimension: requireEnum(record.dimension, "dimension", DIMENSIONS),
    file: requireString(record.file, "file"),
    ...(line === undefined ? {} : { line }),
    title: requireString(record.title, "title"),
    evidence: requireString(record.evidence, "evidence"),
    status: requireEnum(record.status, "status", STATUSES),
    ...(typeof record.disposition === "string" && record.disposition.trim() ? { disposition: record.disposition } : {}),
    ...(typeof record.owner === "string" && record.owner.trim() ? { owner: record.owner } : {}),
    ...(typeof record.verified_by === "string" && record.verified_by.trim() ? { verified_by: record.verified_by } : {}),
  }
}

export function validateFindings(raw: unknown): Finding[] {
  if (!Array.isArray(raw)) throw new ValidationError("findings must be an array")
  const seen = new Set<string>()
  return raw.map((entry) => {
    const finding = validateFinding(entry)
    if (seen.has(finding.id)) throw new ValidationError(`duplicate finding id: ${finding.id}`)
    seen.add(finding.id)
    return finding
  })
}

export function bucketCounts(findings: readonly Finding[]): BucketCounts {
  const counts = { open: 0, fixed: 0, rejected: 0, deferred: 0, accepted_risk: 0 }
  for (const finding of findings) {
    if (finding.status === "accepted-risk") counts.accepted_risk += 1
    else counts[finding.status] += 1
  }
  return counts
}

/**
 * Merge a corrected finding into a prior inventory. A later record replaces the
 * earlier one by ID; unrelated findings are preserved.
 */
export function mergeFindings(existing: readonly Finding[], incoming: readonly Finding[]): Finding[] {
  const merged = new Map(existing.map((finding) => [finding.id, finding]))
  for (const finding of incoming) merged.set(finding.id, finding)
  return [...merged.values()].sort((left, right) => left.id.localeCompare(right.id))
}

export function openBlocking(findings: readonly Finding[]): Finding[] {
  return findings.filter(
    (finding) => finding.status === "open" && (finding.severity === "security" || finding.severity === "critical"),
  )
}
