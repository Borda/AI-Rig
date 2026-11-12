/**
 * Run allocation, persistence, and finalization.
 *
 * A run directory is the authoritative record. Finalization fails closed on
 * source drift, a failed gate, or missing independent review, so a "completed"
 * run is always backed by checked evidence.
 */

import { appendFileSync, chmodSync, existsSync, lstatSync, realpathSync, writeFileSync } from "node:fs"
import { dirname, isAbsolute, join, relative, sep } from "node:path"
import { sha256, type ScopeEntry } from "./digest.ts"
import { PLUGIN_VERSION, SCHEMA_VERSION } from "./version.ts"
import { ensureDir, readJson, writeJsonAtomic } from "./util/fsx.ts"
import { createRunId, type RigPaths } from "./paths.ts"
import { reconciliationProblems, type Finding } from "./findings.ts"

export type RunStatus = "in_progress" | "completed" | "blocked" | "failed" | "stale"
export type GateStatus = "passed" | "failed" | "skipped"

export interface FileRecord {
  readonly path: string
  readonly sha256: string
}

export interface ScopeRecord {
  readonly base: string | null
  readonly commit?: string
  readonly paths: readonly string[]
  readonly dirty: boolean
  readonly entries: readonly ScopeEntry[]
  readonly digest: string
}

export interface GateReceipt {
  readonly id: string
  readonly command: string
  readonly source: string
  readonly status: GateStatus
  readonly exit_code: number | null
  readonly duration_ms: number
  readonly stdout_sha256: string
  readonly stderr_sha256: string
  readonly tail: string
  readonly reason?: string
  readonly source_digest?: string
  readonly log_path?: string
}

export interface FindingsSummary {
  readonly path: string
  readonly digest: string
  readonly open: number
  readonly fixed: number
  readonly rejected: number
  readonly deferred: number
  readonly accepted_risk: number
}

export interface LoopSummary {
  readonly path: string
  readonly rounds: number
  readonly weighted: number
  readonly stop_reason: string
  readonly open_security: number
  readonly open_critical: number
}

export interface ReviewEvidence {
  readonly run_id: string
  readonly reviewer: string
  readonly source_digest: string
  readonly plan_digest?: string
}

export interface RunRecord {
  readonly schema: number
  readonly run_id: string
  readonly skill: string
  readonly created_at: string
  readonly updated_at: string
  status: RunStatus
  readonly plugin_version: string
  scope: ScopeRecord
  plan?: { readonly path: string; readonly digest: string; readonly run_id?: string }
  trivial_plan?: string
  source_run_id?: string
  source_findings?: { readonly digest: string; readonly ids: readonly string[] }
  inputs: FileRecord[]
  outputs: FileRecord[]
  gates: GateReceipt[]
  findings?: FindingsSummary
  loop?: LoopSummary
  review?: ReviewEvidence
  limits: string[]
  confidence: number
  report_path?: string
  stop_reason?: string
  gate_exemption?: string
}

const SKILL_PATTERN = /^[a-z0-9]+(-[a-z0-9]+)*$/
const RUN_ID_PATTERN = /^[a-zA-Z0-9][a-zA-Z0-9_-]*$/

/** Reject path syntax and inherited-object keys before using run storage identities. */
export function validateRunIdentity(skill: string, runId: string): void {
  if (typeof skill !== "string" || !SKILL_PATTERN.test(skill) || Object.hasOwn(Object.prototype, skill)) {
    throw new Error(`invalid skill id: ${skill}`)
  }
  if (typeof runId !== "string" || !RUN_ID_PATTERN.test(runId) || Object.hasOwn(Object.prototype, runId)) {
    throw new Error(`invalid run id: ${runId}`)
  }
}

function object(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
}

function strings(record: Record<string, unknown>, keys: readonly string[]): boolean {
  return keys.every((key) => typeof record[key] === "string")
}

function stringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((entry) => typeof entry === "string")
}

function counts(record: Record<string, unknown>, keys: readonly string[]): boolean {
  return keys.every((key) => Number.isSafeInteger(record[key]) && (record[key] as number) >= 0)
}

function validScope(value: unknown): boolean {
  if (!object(value)) return false
  return (
    (value.base === null || typeof value.base === "string") &&
    (value.commit === undefined || typeof value.commit === "string") &&
    typeof value.digest === "string" &&
    typeof value.dirty === "boolean" &&
    stringArray(value.paths) &&
    Array.isArray(value.entries) &&
    value.entries.every((entry) => object(entry) && strings(entry, ["path", "status", "sha256"]))
  )
}

function validGate(value: unknown): boolean {
  if (!object(value)) return false
  return (
    strings(value, ["id", "command", "source", "status", "stdout_sha256", "stderr_sha256", "tail"]) &&
    ["passed", "failed", "skipped"].includes(value.status as string) &&
    (value.exit_code === null || Number.isInteger(value.exit_code)) &&
    typeof value.duration_ms === "number" &&
    Number.isFinite(value.duration_ms) &&
    value.duration_ms >= 0 &&
    ["reason", "source_digest"].every((key) => value[key] === undefined || typeof value[key] === "string")
  )
}

function validPlan(value: unknown): boolean {
  return (
    object(value) &&
    strings(value, ["path", "digest"]) &&
    (value.run_id === undefined || (typeof value.run_id === "string" && RUN_ID_PATTERN.test(value.run_id)))
  )
}

function validFindingsSummary(value: unknown): boolean {
  return (
    object(value) &&
    strings(value, ["path", "digest"]) &&
    counts(value, ["open", "fixed", "rejected", "deferred", "accepted_risk"])
  )
}

function validLoopSummary(value: unknown): boolean {
  return (
    object(value) &&
    strings(value, ["path", "stop_reason"]) &&
    counts(value, ["rounds", "weighted", "open_security", "open_critical"])
  )
}

function validReview(value: unknown, runId: unknown): boolean {
  return (
    object(value) &&
    strings(value, ["run_id", "reviewer", "source_digest"]) &&
    value.run_id === runId &&
    (value.plan_digest === undefined || typeof value.plan_digest === "string")
  )
}

function validOptionalEvidence(run: Record<string, unknown>): boolean {
  if (run.plan !== undefined && !validPlan(run.plan)) return false
  if (run.findings !== undefined && !validFindingsSummary(run.findings)) return false
  if (run.loop !== undefined && !validLoopSummary(run.loop)) return false
  if (run.review !== undefined && !validReview(run.review, run.run_id)) return false
  return ["trivial_plan", "source_run_id", "report_path", "stop_reason", "gate_exemption"].every(
    (key) => run[key] === undefined || typeof run[key] === "string",
  )
}

function validSourceFindings(value: unknown): boolean {
  return (
    object(value) &&
    typeof value.digest === "string" &&
    stringArray(value.ids) &&
    value.ids.every((id) => /^F-[A-Za-z0-9-]+$/.test(id)) &&
    new Set(value.ids).size === value.ids.length
  )
}

function validRunCollections(run: Record<string, unknown>): boolean {
  return (
    validScope(run.scope) &&
    [run.inputs, run.outputs].every(
      (files) => Array.isArray(files) && files.every((file) => object(file) && strings(file, ["path", "sha256"])),
    ) &&
    Array.isArray(run.gates) &&
    run.gates.every(validGate) &&
    stringArray(run.limits)
  )
}

/** Validate persisted evidence before its fields can authorize writes or completion. */
export function validateRunRecord(raw: unknown): RunRecord {
  if (!object(raw)) throw new Error("invalid run record: expected an object")
  validateRunIdentity(raw.skill as string, raw.run_id as string)
  if (
    raw.schema !== SCHEMA_VERSION ||
    !strings(raw, ["created_at", "updated_at", "plugin_version", "status"]) ||
    !["in_progress", "completed", "blocked", "failed", "stale"].includes(raw.status as string) ||
    typeof raw.confidence !== "number" ||
    !Number.isFinite(raw.confidence) ||
    raw.confidence < 0 ||
    raw.confidence > 1 ||
    !validRunCollections(raw) ||
    !validOptionalEvidence(raw)
  )
    throw new Error("invalid run record schema or evidence")
  if (raw.source_run_id !== undefined) validateRunIdentity("rig-review", raw.source_run_id as string)
  if (raw.source_findings !== undefined && !validSourceFindings(raw.source_findings)) {
    throw new Error("invalid run record source inventory")
  }
  return raw as unknown as RunRecord
}

/** Resolve a validated run identity without following storage directories outside the project. */
export function runDirectory(paths: RigPaths, skill: string, runId: string): string {
  validateRunIdentity(skill, runId)
  const directory = join(paths.runsRoot, skill, runId)
  for (const path of [paths.artifactRoot, paths.runsRoot, join(paths.runsRoot, skill), directory]) {
    if (lstatSync(path, { throwIfNoEntry: false })?.isSymbolicLink()) {
      throw new Error(`symbolic run directory: ${path}`)
    }
  }
  let ancestor = directory
  while (!lstatSync(ancestor, { throwIfNoEntry: false })) ancestor = dirname(ancestor)
  const physical = relative(realpathSync(paths.projectRoot), realpathSync(ancestor))
  if (physical === ".." || physical.startsWith(`..${sep}`) || isAbsolute(physical)) {
    throw new Error("run directory resolves outside the project")
  }
  return directory
}

export function createRun(
  paths: RigPaths,
  input: { skill: string; scope: ScopeRecord; plan?: { path: string; digest: string; run_id?: string } },
): RunRecord {
  if (!SKILL_PATTERN.test(input.skill)) throw new Error(`invalid skill id: ${input.skill}`)
  let runId = createRunId()
  for (
    let attempt = 0;
    attempt < 5 && existsSync(join(runDirectory(paths, input.skill, runId), "run.json"));
    attempt += 1
  ) {
    runId = createRunId()
  }
  const directory = runDirectory(paths, input.skill, runId)
  ensureDir(directory)
  const now = new Date().toISOString()
  const run: RunRecord = {
    schema: SCHEMA_VERSION,
    run_id: runId,
    skill: input.skill,
    created_at: now,
    updated_at: now,
    status: "in_progress",
    plugin_version: PLUGIN_VERSION,
    scope: input.scope,
    inputs: [],
    outputs: [],
    gates: [],
    limits: [],
    confidence: 0.5,
    ...(input.plan ? { plan: input.plan } : {}),
  }
  saveRun(paths, run)
  return run
}

/** Load checked evidence whose stored identity agrees with its storage directory. */
export function loadRun(paths: RigPaths, skill: string, runId: string): RunRecord {
  const path = join(runDirectory(paths, skill, runId), "run.json")
  if (lstatSync(path).isSymbolicLink()) throw new Error("symbolic run record")
  const run = validateRunRecord(readJson<unknown>(path))
  if (run.skill !== skill || run.run_id !== runId) throw new Error("run record identity does not match its directory")
  return run
}

/** Persist only records that satisfy the same schema enforced on reads. */
export function saveRun(paths: RigPaths, run: RunRecord): void {
  validateRunIdentity(run.skill, run.run_id)
  validateRunRecord(run)
  writeJsonAtomic(join(runDirectory(paths, run.skill, run.run_id), "run.json"), {
    ...run,
    updated_at: new Date().toISOString(),
  })
}

export function appendJournal(paths: RigPaths, run: RunRecord, record: Record<string, unknown>): void {
  const directory = runDirectory(paths, run.skill, run.run_id)
  const path = join(directory, "journal.jsonl")
  ensureDir(directory)
  appendFileSync(path, `${JSON.stringify({ schema: SCHEMA_VERSION, run_id: run.run_id, ...record })}\n`, {
    encoding: "utf8",
    mode: 0o600,
  })
  chmodSync(path, 0o600)
}

/** Select the latest receipt per gate that still applies to the run's source. */
export function currentGateReceipts(run: RunRecord): GateReceipt[] {
  const latest = new Map(run.gates.map((receipt) => [receipt.id, receipt]))
  return [...latest.values()].filter((receipt) => receipt.source_digest === run.scope.digest)
}

export interface SkillRequirements {
  readonly gates: boolean
  readonly review: boolean
  readonly findings: boolean
  readonly closedFindings: boolean
  readonly loop: boolean
}

/**
 * Per-skill completion contract. Review is allowed to finish with open
 * findings; remediation is not, because its whole purpose is closure.
 */
export const SKILL_REQUIREMENTS: Readonly<Record<string, SkillRequirements>> = {
  "rig-plan": { gates: false, review: true, findings: false, closedFindings: false, loop: false },
  "rig-investigate": { gates: false, review: false, findings: false, closedFindings: false, loop: false },
  "rig-research": { gates: false, review: true, findings: false, closedFindings: false, loop: false },
  "rig-implement": { gates: true, review: false, findings: false, closedFindings: false, loop: false },
  "rig-review": { gates: false, review: true, findings: true, closedFindings: false, loop: false },
  "rig-remediate": { gates: true, review: true, findings: true, closedFindings: true, loop: false },
  "rig-challenge": { gates: true, review: true, findings: false, closedFindings: false, loop: true },
  "rig-optimize": { gates: true, review: true, findings: false, closedFindings: false, loop: false },
  "rig-distill": { gates: false, review: false, findings: false, closedFindings: false, loop: false },
  "rig-release": { gates: true, review: false, findings: false, closedFindings: false, loop: false },
  "rig-doctor": { gates: false, review: false, findings: false, closedFindings: false, loop: false },
}

export function requirementsFor(skill: string): SkillRequirements {
  const requirements = SKILL_REQUIREMENTS[skill]
  if (!requirements) throw new Error(`unknown skill: ${skill}`)
  return requirements
}

export interface FinalizeInput {
  readonly drift: boolean
  readonly plan_digest?: string
  readonly source_findings_digest?: string
  readonly findings?: readonly Finding[]
}

export interface FinalizeResult {
  readonly ok: boolean
  readonly problems: string[]
  readonly status: RunStatus
}

/** Check the frozen plan separately from the source inventory that excludes artifact directories. */
function planProblems(run: RunRecord, input: FinalizeInput): string[] {
  if (run.skill !== "rig-plan" && run.skill !== "rig-implement") return []
  if (!run.plan) {
    if (run.skill === "rig-implement" && run.trivial_plan?.trim()) return []
    return [
      run.skill === "rig-plan"
        ? "plan artifact is missing"
        : "implementation requires a reviewed plan or a trivial plan",
    ]
  }
  const problems: string[] = []
  if (input.plan_digest !== run.plan.digest)
    problems.push("plan bytes changed or could not be verified against the frozen plan digest")
  if (run.skill === "rig-plan" && run.review?.plan_digest !== run.plan.digest) {
    problems.push("review evidence does not match the bound plan digest")
  }
  if (run.skill === "rig-implement" && !run.plan.run_id)
    problems.push("implementation plan is missing reviewed plan-run provenance")
  return problems
}

/** Reconcile actual remediation records with the immutable source inventory captured at start. */
function remediationProblems(run: RunRecord, input: FinalizeInput): string[] {
  if (run.skill !== "rig-remediate") return []
  if (!run.source_run_id || !run.source_findings) return ["remediation source findings inventory is missing"]
  const problems: string[] = []
  if (input.source_findings_digest !== run.source_findings.digest) {
    problems.push("source findings inventory changed or could not be verified")
  }
  if (!input.findings) problems.push("remediation findings inventory could not be verified")
  else problems.push(...reconciliationProblems(run.source_findings.ids, input.findings))
  return problems
}

/**
 * Validate a run and describe the terminal status. `ok` flips the run to
 * completed; otherwise `status` is the honest outcome to record.
 */
export function finalizeRun(run: RunRecord, input: FinalizeInput): FinalizeResult {
  const requirements = requirementsFor(run.skill)
  const problems: string[] = []

  if (run.status !== "in_progress") {
    return { ok: false, problems: [`run status is '${run.status}', not 'in_progress'`], status: run.status }
  }

  if (input.drift) {
    return {
      ok: false,
      problems: ["source drift: the scope digest no longer matches the recorded inventory"],
      status: requirements.loop && run.loop && run.loop.stop_reason !== "converged" ? "blocked" : "stale",
    }
  }

  problems.push(...planProblems(run, input), ...remediationProblems(run, input))

  if (requirements.gates) {
    const current = currentGateReceipts(run)
    const currentIds = new Set(current.map((gate) => gate.id))
    const staleIds = [...new Set(run.gates.map((gate) => gate.id))].filter((id) => !currentIds.has(id))
    if (staleIds.length > 0) problems.push(`gate receipts do not match the current scope: ${staleIds.join(", ")}`)
    const failed = current.filter((gate) => gate.status === "failed")
    const passed = current.filter((gate) => gate.status === "passed")
    if (failed.length > 0) problems.push(`failed gates: ${failed.map((gate) => gate.id).join(", ")}`)
    if (passed.length === 0 && !run.gate_exemption) {
      problems.push("no passing gate receipt; run rig_gate or record gate_exemption")
    }
  }

  if (requirements.review) {
    if (!run.review) problems.push("independent review evidence is missing")
    else if (run.review.source_digest !== run.scope.digest) {
      problems.push("review evidence does not match the current scope digest")
    }
  }

  if (requirements.findings && !run.findings) {
    problems.push("findings artifact is missing; write it with rig_findings")
  }

  if (requirements.closedFindings && run.findings && run.findings.open > 0) {
    problems.push(`remediation still has ${run.findings.open} open finding(s)`)
  }

  let loopBlocked = false
  if (requirements.loop) {
    if (!run.loop) {
      problems.push("challenge loop ledger is missing; use rig_loop")
    } else {
      if (run.loop.stop_reason !== "converged") {
        problems.push(`challenge loop stopped with '${run.loop.stop_reason}', not convergence`)
        loopBlocked = true
      }
      if (run.loop.open_security > 0 || run.loop.open_critical > 0) {
        problems.push(
          `challenge loop retains ${run.loop.open_security} security and ${run.loop.open_critical} critical finding(s)`,
        )
        loopBlocked = true
      }
    }
  }

  if (problems.length > 0) return { ok: false, problems, status: loopBlocked ? "blocked" : "in_progress" }
  return { ok: true, problems: [], status: "completed" }
}

export function renderReport(run: RunRecord): string {
  const current = new Set(currentGateReceipts(run))
  const latest = new Map(run.gates.map((gate) => [gate.id, gate]))
  const gates = run.gates.length
    ? run.gates
        .map((gate) => {
          const currency = current.has(gate) ? "current" : latest.get(gate.id) === gate ? "stale" : "superseded"
          return `| \`${gate.id}\` | ${gate.status} | ${gate.exit_code ?? "-"} | \`${gate.command}\` | ${currency} |`
        })
        .join("\n")
    : "| - | not-run | - | no gate receipts | - |"

  const findings = run.findings
    ? `| open | fixed | rejected | deferred | accepted-risk |\n| --- | --- | --- | --- | --- |\n| ${run.findings.open} | ${run.findings.fixed} | ${run.findings.rejected} | ${run.findings.deferred} | ${run.findings.accepted_risk} |`
    : "No findings artifact."

  const limits = run.limits.length ? run.limits.map((limit) => `- ${limit}`).join("\n") : "- none recorded"

  const review = run.review
    ? `- Reviewer: \`${run.review.reviewer}\`\n- Bound digest: \`${run.review.source_digest}\``
    : "- none recorded"

  const loop = run.loop
    ? `- Rounds: ${run.loop.rounds}\n- Weighted open score: ${run.loop.weighted}\n- Stop reason: \`${run.loop.stop_reason}\`\n- Open security: ${run.loop.open_security}\n- Open critical: ${run.loop.open_critical}`
    : "- no challenge loop"

  return `# Run ${run.run_id}

- Skill: \`${run.skill}\`
- Status: \`${run.status}\`
- Plugin: \`${run.plugin_version}\`
- Scope digest: \`${run.scope.digest}\`
- Scope filter: ${run.scope.paths.length ? run.scope.paths.map((path) => `\`${path}\``).join(", ") : "every changed path"}
- Created: ${run.created_at}
- Updated: ${run.updated_at}
- Confidence: ${run.confidence.toFixed(2)}
${run.plan ? `- Plan: \`${run.plan.path}\` (\`${run.plan.digest}\`)` : ""}

## Gates

| Gate | Status | Exit | Command | Currency |
| --- | --- | --- | --- | --- |
${gates}

## Findings

${findings}

## Independent review

${review}

## Challenge loop

${loop}

## Changes

${run.outputs.length ? run.outputs.map((file) => `- \`${file.path}\` — \`${file.sha256}\``).join("\n") : "- none recorded"}

## Limits

${limits}
${run.stop_reason ? `\n## Stop reason\n\n\`${run.stop_reason}\`\n` : ""}`
}

export function writeReport(paths: RigPaths, run: RunRecord): string {
  const path = join(runDirectory(paths, run.skill, run.run_id), "report.md")
  writeFileSync(path, `${renderReport(run)}\n`, "utf8")
  return path
}

/**
 * Record the plan file digest. The plan itself stays in the project; only its
 * digest and path are bound to the run.
 */
export function bindPlan(run: RunRecord, planPath: string, content: string): RunRecord {
  return { ...run, plan: { path: planPath, digest: sha256(content) } }
}
