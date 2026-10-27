/**
 * Adversarial convergence ledger.
 *
 * The ledger is append-only: correcting a round means appending a new record,
 * never rewriting history. Convergence is a strict decrease in the open
 * weighted total after a validated baseline.
 */

import { appendFileSync } from "node:fs"
import { readLines } from "./util/fsx.ts"
import { SEVERITIES, type Severity } from "./findings.ts"

export const WEIGHTS: Record<Severity, number> = {
  security: 20,
  critical: 10,
  high: 6,
  medium: 4,
  low: 2,
  nit: 1,
}

export const STOP_REASONS = [
  "converged",
  "plateau",
  "non-converging",
  "round-limit",
  "unavailable-reviewer",
  "authority-required",
  "drift",
] as const

export type StopReason = (typeof STOP_REASONS)[number]

export interface LoopFinding {
  readonly signature: string
  readonly severity: Severity
  readonly status: "open" | "fixed" | "rejected"
}

export interface LoopRecord {
  readonly schema: number
  readonly run_id: string
  readonly round: number
  readonly reviewer: string
  readonly source_digest: string
  readonly findings: readonly LoopFinding[]
  readonly weighted: number
  readonly stop_reason?: StopReason
  readonly note?: string
}

export interface RoundScore {
  readonly round: number
  readonly weighted: number
  readonly bySeverity: Record<Severity, { old: number; new: number }>
  readonly open: number
}

function isSeverity(value: string): value is Severity {
  return (SEVERITIES as readonly string[]).includes(value)
}

export interface Residue {
  readonly weighted: number
  readonly open: number
  readonly security: number
  readonly critical: number
}

/**
 * Score one set of findings by severity. Only open findings weigh anything.
 */
export function score(findings: readonly LoopFinding[]): Residue {
  let weighted = 0
  let open = 0
  let security = 0
  let critical = 0
  for (const finding of findings) {
    if (finding.status !== "open") continue
    open += 1
    weighted += WEIGHTS[finding.severity]
    if (finding.severity === "security") security += 1
    if (finding.severity === "critical") critical += 1
  }
  return { weighted, open, security, critical }
}

/**
 * The latest record for each round. Finalization appends a terminal marker with
 * the same round number, so a later record supersedes an earlier one without
 * rewriting history.
 */
export function latestPerRound(records: readonly LoopRecord[]): LoopRecord[] {
  const byRound = new Map<number, LoopRecord>()
  for (const record of records) byRound.set(record.round, record)
  return [...byRound.values()].sort((left, right) => left.round - right.round)
}

export function latestRecord(records: readonly LoopRecord[]): LoopRecord | undefined {
  return latestPerRound(records).at(-1)
}

export function appendLoopRecord(path: string, record: LoopRecord): void {
  appendFileSync(path, `${JSON.stringify(record)}\n`, "utf8")
}

export function readLoop(path: string): LoopRecord[] {
  return readLines(path)
    .map((line) => {
      try {
        return JSON.parse(line) as LoopRecord
      } catch {
        return undefined
      }
    })
    .filter((record): record is LoopRecord => record !== undefined)
    .filter((record) => isSeverityRecord(record))
}

function isSeverityRecord(record: LoopRecord): boolean {
  return (
    Number.isInteger(record.round) &&
    Array.isArray(record.findings) &&
    record.findings.every((finding) => isSeverity(finding.severity))
  )
}

/**
 * Build the cumulative progress rows. Old counts a signature seen in an earlier
 * round; new counts a signature that first appears in this round. A terminal
 * marker on the same round supersedes the scored record.
 */
export function progress(records: readonly LoopRecord[]): RoundScore[] {
  const seen = new Set<string>()
  const rows: RoundScore[] = []
  for (const record of latestPerRound(records)) {
    const bySeverity = Object.fromEntries(SEVERITIES.map((severity) => [severity, { old: 0, new: 0 }])) as Record<
      Severity,
      { old: number; new: number }
    >
    let weighted = 0
    let open = 0
    for (const finding of record.findings) {
      if (finding.status !== "open") continue
      open += 1
      weighted += WEIGHTS[finding.severity]
      if (seen.has(finding.signature)) bySeverity[finding.severity].old += 1
      else bySeverity[finding.severity].new += 1
    }
    for (const finding of record.findings) seen.add(finding.signature)
    rows.push({ round: record.round, weighted, bySeverity, open })
  }
  return rows
}

export interface Convergence {
  readonly converged: boolean
  readonly stop?: StopReason
  readonly reason: string
}

/**
 * Evaluate convergence after at least one validated round.
 *
 * A clean round (no open findings) converges immediately; otherwise any
 * decrease converges. An equal total is a plateau and an increase is
 * non-converging; both stop the loop rather than starting another fix round.
 */
export function convergence(rows: readonly RoundScore[]): Convergence {
  if (rows.length === 0) return { converged: false, stop: "unavailable-reviewer", reason: "no validated rounds" }
  const latest = rows[rows.length - 1]
  if (!latest) return { converged: false, reason: "incomplete history" }
  if (latest.weighted === 0) return { converged: true, stop: "converged", reason: "no open findings" }
  if (rows.length === 1)
    return { converged: false, reason: "baseline only; convergence needs a second validated round" }
  const previous = rows[rows.length - 2]
  if (!previous) return { converged: false, reason: "incomplete history" }
  if (latest.weighted < previous.weighted)
    return { converged: true, stop: "converged", reason: `${previous.weighted} -> ${latest.weighted}` }
  if (latest.weighted === previous.weighted)
    return { converged: false, stop: "plateau", reason: `weight stayed at ${latest.weighted}` }
  return {
    converged: false,
    stop: "non-converging",
    reason: `weight rose from ${previous.weighted} to ${latest.weighted}`,
  }
}

export function renderProgressTable(rows: readonly RoundScore[]): string {
  if (rows.length === 0) return ""
  const header = "| Iteration | Security | Critical | High | Medium | Low | Nits | Weighted score |"
  const divider = "| --- | --- | --- | --- | --- | --- | --- | --- |"
  const body = rows.map((row) => {
    const cell = (severity: Severity) => `${row.bySeverity[severity].old} + ${row.bySeverity[severity].new}`
    return `| ${row.round} | ${cell("security")} | ${cell("critical")} | ${cell("high")} | ${cell("medium")} | ${cell("low")} | ${cell("nit")} | ${row.weighted} |`
  })
  return [header, divider, ...body].join("\n")
}
