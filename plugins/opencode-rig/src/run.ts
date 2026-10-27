/**
 * Run allocation, persistence, and finalization.
 *
 * A run directory is the authoritative record. Finalization fails closed on
 * source drift, a failed gate, or missing independent review, so a "completed"
 * run is always backed by checked evidence.
 */

import { appendFileSync, existsSync, writeFileSync } from "node:fs"
import { join } from "node:path"
import { sha256, type ScopeEntry } from "./digest.ts"
import { PLUGIN_VERSION, SCHEMA_VERSION } from "./version.ts"
import { ensureDir, readJson, writeJsonAtomic } from "./util/fsx.ts"
import { createRunId, type RigPaths } from "./paths.ts"

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
  plan?: { readonly path: string; readonly digest: string }
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

export function runDirectory(paths: RigPaths, skill: string, runId: string): string {
  return join(paths.runsRoot, skill, runId)
}

export function createRun(
  paths: RigPaths,
  input: { skill: string; scope: ScopeRecord; plan?: { path: string; digest: string } },
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

export function loadRun(paths: RigPaths, skill: string, runId: string): RunRecord {
  return readJson<RunRecord>(join(runDirectory(paths, skill, runId), "run.json"))
}

export function saveRun(paths: RigPaths, run: RunRecord): void {
  writeJsonAtomic(join(runDirectory(paths, run.skill, run.run_id), "run.json"), {
    ...run,
    updated_at: new Date().toISOString(),
  })
}

export function appendJournal(paths: RigPaths, run: RunRecord, record: Record<string, unknown>): void {
  const directory = runDirectory(paths, run.skill, run.run_id)
  const path = join(directory, "journal.jsonl")
  ensureDir(directory)
  appendFileSync(path, `${JSON.stringify({ schema: SCHEMA_VERSION, run_id: run.run_id, ...record })}\n`, "utf8")
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
}

export interface FinalizeResult {
  readonly ok: boolean
  readonly problems: string[]
  readonly status: RunStatus
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
      status: "stale",
    }
  }

  if (requirements.gates) {
    const failed = run.gates.filter((gate) => gate.status === "failed")
    const passed = run.gates.filter((gate) => gate.status === "passed")
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
      loopBlocked = true
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
  const gates = run.gates.length
    ? run.gates
        .map((gate) => `| \`${gate.id}\` | ${gate.status} | ${gate.exit_code ?? "-"} | \`${gate.command}\` |`)
        .join("\n")
    : "| - | not-run | - | no gate receipts |"

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

| Gate | Status | Exit | Command |
| --- | --- | --- | --- |
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
