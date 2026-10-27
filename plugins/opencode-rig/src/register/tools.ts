/**
 * Tool registration.
 *
 * Tools are the only place where a workflow action becomes a durable artifact.
 * Every mutating action loads the run from disk first, validates its input, and
 * writes through the atomic helpers, so a rejected call leaves no trace.
 */

import { existsSync, rmSync, writeFileSync } from "node:fs"
import { isAbsolute, join, relative, resolve, sep } from "node:path"
import type { Plugin } from "@opencode/plugin"
import { sha256File } from "../digest.ts"
import { dispatchRole, ROLE_IDS, type RoleId } from "../dispatch.ts"
import { doctorReport, resolveAgentDirs, staleRuns } from "../doctor.ts"
import { collectCommitScope, collectScope, detectDrift, refreshScope } from "../evidence.ts"
import {
  bucketCounts,
  mergeFindings,
  validateFindings,
  SEVERITIES,
  type Finding,
  type FindingsDocument,
} from "../findings.ts"
import { detectGates, runGates, type GateSpec } from "../gates.ts"
import { grantPath, readGrant, revokeGrant, writeGrant } from "../git-grant.ts"
import {
  appendLoopRecord,
  convergence,
  latestRecord,
  progress,
  readLoop,
  renderProgressTable,
  score,
  STOP_REASONS,
  type LoopFinding,
  type StopReason,
} from "../loop.ts"
import { resolveGitCommonDir, type RigPaths } from "../paths.ts"
import {
  appendJournal,
  createRun,
  finalizeRun,
  loadRun,
  saveRun,
  writeReport,
  type FileRecord,
  type FindingsSummary,
  type RunRecord,
} from "../run.ts"
import { SKILLS } from "../skill-manifest.ts"
import { activeRun, rememberRun, skillForRun } from "../state.ts"
import { ensureDir, readJsonIfExists, writeJsonAtomic } from "../util/fsx.ts"
import type { RigOptions } from "../options.ts"

type Ctx = Plugin.Context

export interface ToolDeps {
  readonly ctx: Ctx
  readonly paths: RigPaths
  readonly options: RigOptions
  readonly resourcesDir: string
}

export interface Registration {
  dispose(): Promise<void>
}

const SKILL_IDS = SKILLS.map((skill) => skill.id)
const MAX_FILES = 5_000

const str = { type: "string" } as const
const strArray = { type: "array", items: str } as const

function json(value: unknown): string {
  return JSON.stringify(value, null, 2)
}

function resolveInsideProject(projectRoot: string, path: string): string {
  const absolute = isAbsolute(path) ? resolve(path) : resolve(projectRoot, path)
  const rel = relative(projectRoot, absolute)
  if (rel === ".." || rel.startsWith(`..${sep}`) || isAbsolute(rel)) {
    throw new Error(`path escapes the project: ${path}`)
  }
  return absolute
}

function fileRecords(projectRoot: string, paths: readonly string[]): FileRecord[] {
  return paths.map((path) => {
    const absolute = resolveInsideProject(projectRoot, path)
    if (!existsSync(absolute)) throw new Error(`evidence path does not exist: ${path}`)
    return { path, sha256: sha256File(absolute) }
  })
}

function resolveRun(paths: RigPaths, sessionID: string, runId: string | undefined): RunRecord {
  const id = runId ?? activeRun(paths.artifactRoot, sessionID)?.runId
  if (!id) throw new Error("no active run; start one with rig_evidence action=start or pass run_id")
  const skill = skillForRun(paths.artifactRoot, id)
  if (!skill) throw new Error(`unknown run: ${id}`)
  return loadRun(paths, skill, id)
}

function runSummary(run: RunRecord): string {
  return [
    `run_id: ${run.run_id}`,
    `skill: ${run.skill}`,
    `status: ${run.status}`,
    `scope_digest: ${run.scope.digest}`,
    `scope_paths: ${run.scope.paths.length ? run.scope.paths.join(", ") : "(all changed paths)"}`,
    run.plan ? `plan: ${run.plan.path} (${run.plan.digest})` : "",
    run.review ? `review: ${run.review.reviewer} @ ${run.review.source_digest}` : "",
  ]
    .filter(Boolean)
    .join("\n")
}

function findingsSummary(path: string, findings: readonly Finding[]): FindingsSummary {
  const counts = bucketCounts(findings)
  return {
    path,
    digest: sha256File(path),
    ...counts,
  }
}

function gateTable(run: RunRecord): string {
  if (run.gates.length === 0) return "No gate receipts."
  return [
    "| Gate | Status | Exit | Command |",
    "| --- | --- | --- | --- |",
    ...run.gates.map((gate) => `| \`${gate.id}\` | ${gate.status} | ${gate.exit_code ?? "-"} | \`${gate.command}\` |`),
  ].join("\n")
}

interface EvidenceInput {
  readonly action: "start" | "record" | "refresh" | "finalize" | "inspect"
  readonly run_id?: string
  readonly skill?: string
  readonly paths?: readonly string[]
  readonly commit?: string
  readonly plan_path?: string
  readonly gate_exemption?: string
  readonly inputs?: readonly string[]
  readonly outputs?: readonly string[]
  readonly limits?: readonly string[]
  readonly decision?: string
  readonly confidence?: number
  readonly reviewer?: string
  readonly message?: string
}

interface GateInput {
  readonly action: "detect" | "run"
  readonly run_id?: string
  readonly names?: readonly string[]
  readonly files?: readonly string[]
  readonly timeout_ms?: number
}

interface FindingsInput {
  readonly action: "write" | "read"
  readonly run_id?: string
  readonly axes?: readonly string[]
  readonly aspects?: readonly string[]
  readonly coverage?: readonly string[]
  readonly reviewer?: string
  readonly findings?: readonly unknown[]
}

interface LoopInput {
  readonly action: "start" | "append" | "read" | "finalize"
  readonly run_id?: string
  readonly reviewer?: string
  readonly source_digest?: string
  readonly findings?: readonly LoopFinding[]
  readonly stop_reason?: StopReason
  readonly note?: string
}

interface GrantInput {
  readonly action: "status" | "approve" | "revoke"
  readonly confirm?: string
  readonly scope_acknowledged?: boolean
}

interface DispatchInputTool {
  readonly role: RoleId
  readonly task: string
  readonly paths?: readonly string[]
  readonly read_only?: boolean
  readonly keep_session?: boolean
}

interface DoctorInputTool {
  readonly action: "report" | "clean" | "assets"
  readonly confirm?: boolean
}

const EVIDENCE_INPUT = {
  type: "object",
  properties: {
    action: {
      type: "string",
      enum: ["start", "record", "refresh", "finalize", "inspect"],
      description:
        "start allocates a run; record binds files and decisions; refresh adopts edited source; finalize validates completion; inspect reads the record.",
    },
    run_id: { type: "string", description: "Run to operate on. Defaults to the active run of this session." },
    skill: { type: "string", enum: SKILL_IDS, description: "Workflow that owns the run. Required for start." },
    paths: {
      ...strArray,
      description: "Paths that define the scope. Omit to scope the run to every changed path.",
    },
    commit: { type: "string", description: "Collect scope from this commit instead of the working tree. Immutable." },
    plan_path: { type: "string", description: "Plan file to bind by digest. The plan text itself is not copied." },
    gate_exemption: {
      type: "string",
      description: "Reason no configured gate applies. Only for a project with no detectable gate.",
    },
    inputs: { ...strArray, description: "Files read as authoritative evidence. Must exist." },
    outputs: { ...strArray, description: "Files created or modified by the run. Must exist." },
    limits: { ...strArray, description: "Explicit coverage limits, for example an unavailable environment." },
    decision: { type: "string", description: "Decision worth preserving in the journal." },
    confidence: { type: "number", minimum: 0, maximum: 1, description: "Confidence in the recorded result." },
    reviewer: {
      type: "string",
      description: "Independent reviewer identity for the current scope digest. Never the implementing agent.",
    },
    message: { type: "string", description: "Reason for a scope refresh. Required by refresh." },
  },
  required: ["action"],
  additionalProperties: false,
} as const

const GATE_INPUT = {
  type: "object",
  properties: {
    action: {
      type: "string",
      enum: ["detect", "run"],
      description: "detect lists configured gates; run executes them.",
    },
    run_id: { type: "string", description: "Run that receives the receipts. Required by run." },
    names: { ...strArray, description: "Gate IDs to run. Omit for every configured gate." },
    files: { ...strArray, description: "File list for a gate that requires one. Defaults to the run scope." },
    timeout_ms: { type: "number", minimum: 1000, maximum: 3600000, description: "Per-gate timeout in milliseconds." },
  },
  required: ["action"],
  additionalProperties: false,
} as const

const FINDINGS_INPUT = {
  type: "object",
  properties: {
    action: {
      type: "string",
      enum: ["write", "read"],
      description: "write merges a validated inventory; read returns it.",
    },
    run_id: { type: "string", description: "Run that owns the findings artifact." },
    axes: { ...strArray, description: "Review axes that actually ran." },
    aspects: { ...strArray, description: "Alias for axes (review aspects). Use axes/aspects as appropriate." },
    coverage: { ...strArray, description: "Files or areas actually covered." },
    reviewer: { type: "string", description: "Independent reviewer identity. Required for a rig-review write." },
    findings: {
      type: "array",
      description: "Finding records. A later record with the same id replaces the earlier one.",
      items: {
        type: "object",
        properties: {
          id: { type: "string", description: "Stable identifier such as F-001." },
          severity: { type: "string", enum: ["security", "critical", "high", "medium", "low", "nit"] },
          dimension: {
            type: "string",
            enum: ["correctness", "security", "tests", "api", "performance", "docs", "data"],
          },
          file: { type: "string" },
          line: { type: "number", minimum: 1 },
          title: { type: "string" },
          evidence: { type: "string", description: "Observed behavior and the source that supports it." },
          status: { type: "string", enum: ["open", "fixed", "rejected", "deferred", "accepted-risk"] },
          disposition: { type: "string", description: "What was done and why. Required for a terminal status." },
          owner: { type: "string" },
          verified_by: { type: "string", description: "Independent check that closed the finding." },
        },
        required: ["id", "severity", "dimension", "file", "title", "evidence", "status"],
        additionalProperties: false,
      },
    },
  },
  required: ["action"],
  additionalProperties: false,
} as const

const LOOP_INPUT = {
  type: "object",
  properties: {
    action: {
      type: "string",
      enum: ["start", "append", "read", "finalize"],
      description:
        "start writes the baseline round; append adds an assessed round; read prints the ledger; finalize records the stop reason.",
    },
    run_id: { type: "string", description: "Challenge run that owns the ledger." },
    reviewer: { type: "string", description: "Independent challenger identity for this round." },
    source_digest: { type: "string", description: "Digest the round assessed. Defaults to the run scope digest." },
    findings: {
      type: "array",
      items: {
        type: "object",
        properties: {
          signature: { type: "string", description: "Stable file plus normalized invariant, not a line number." },
          severity: { type: "string", enum: ["security", "critical", "high", "medium", "low", "nit"] },
          status: { type: "string", enum: ["open", "fixed", "rejected"] },
        },
        required: ["signature", "severity", "status"],
        additionalProperties: false,
      },
    },
    stop_reason: { type: "string", enum: [...STOP_REASONS], description: "Terminal reason. Required by finalize." },
    note: { type: "string", description: "Why the loop stopped, especially for non-convergence and authority stops." },
  },
  required: ["action"],
  additionalProperties: false,
} as const

const GRANT_INPUT = {
  type: "object",
  properties: {
    action: {
      type: "string",
      enum: ["status", "approve", "revoke"],
      description: "status reads the grant; approve records one after an explicit user answer; revoke removes it.",
    },
    confirm: {
      type: "string",
      description:
        "The user's own approval words, naming local commits and the excluded operations. Required by approve.",
    },
    scope_acknowledged: {
      type: "boolean",
      description:
        "True only when the user explicitly accepted that the grant excludes push, force, history rewrite, and destructive operations.",
    },
  },
  required: ["action"],
  additionalProperties: false,
} as const

const DISPATCH_INPUT = {
  type: "object",
  properties: {
    role: { type: "string", enum: [...ROLE_IDS], description: "Role card to inject into the child session." },
    task: { type: "string", description: "One bounded responsibility, with acceptance criteria." },
    paths: { ...strArray, description: "Paths the child may read, and may edit only when read_only is false." },
    read_only: {
      type: "boolean",
      description: "Defaults to true. Only rig-implementer may write; other roles are always read-only.",
    },
    keep_session: { type: "boolean", description: "Keep the child session for inspection. Defaults to false." },
  },
  required: ["role", "task"],
  additionalProperties: false,
} as const

const DOCTOR_INPUT = {
  type: "object",
  properties: {
    action: {
      type: "string",
      enum: ["report", "clean", "assets"],
      description:
        "report is read-only; assets also curates shipped skill and role cards; clean removes stale run directories.",
    },
    confirm: { type: "boolean", description: "Required by clean: true after the user approves deleting stale runs." },
  },
  required: ["action"],
  additionalProperties: false,
} as const

export async function registerTools(deps: ToolDeps): Promise<Registration[]> {
  const { ctx, paths, options, resourcesDir } = deps
  const registrations: Registration[] = []
  const save = (run: RunRecord) => saveRun(paths, run)

  // Concurrency ceiling for inline dispatch. Options are immutable per plugin
  // instance, so one counter is enough.
  let activeDispatches = 0
  const dispatchWaiters: Array<() => void> = []
  async function withDispatchSlot<T>(work: () => Promise<T>): Promise<T> {
    while (activeDispatches >= options.maxParallel) {
      await new Promise<void>((resolveWaiter) => dispatchWaiters.push(resolveWaiter))
    }
    activeDispatches += 1
    try {
      return await work()
    } finally {
      activeDispatches -= 1
      dispatchWaiters.shift()?.()
    }
  }

  const evidence = await ctx.tool.transform((editor: any) => {
    editor.add({
      name: "rig_evidence",
      description:
        "Create and finalize an evidence run: bind scope bytes, record inputs and outputs, refresh edited source, and validate completion requirements for the owning skill.",
      input: EVIDENCE_INPUT,
      options: { codemode: true },
      execute: async (raw: unknown, context: any) => {
        const input = raw as EvidenceInput

        if (input.action === "start") {
          if (!input.skill || !SKILL_IDS.includes(input.skill)) {
            throw new Error(`start requires skill: one of ${SKILL_IDS.join(", ")}`)
          }
          const scope = input.commit
            ? collectCommitScope(paths.projectRoot, input.commit, input.paths ?? [])
            : collectScope(paths.projectRoot, input.paths ?? [])
          let plan: { path: string; digest: string } | undefined
          if (input.plan_path) {
            const absolute = resolveInsideProject(paths.projectRoot, input.plan_path)
            if (!existsSync(absolute)) throw new Error(`plan does not exist: ${input.plan_path}`)
            plan = { path: input.plan_path, digest: sha256File(absolute) }
          }
          const run = createRun(paths, { skill: input.skill, scope, ...(plan ? { plan } : {}) })
          const created: RunRecord = {
            ...run,
            ...(input.gate_exemption ? { gate_exemption: input.gate_exemption } : {}),
          }
          save(created)
          rememberRun(paths.artifactRoot, context.sessionID, { runId: created.run_id, skill: created.skill })
          return {
            content: `Run started.\n${runSummary(created)}\n\nScope entries: ${created.scope.entries.length}`,
            metadata: { run_id: created.run_id, scope_digest: created.scope.digest },
          }
        }

        const run = resolveRun(paths, context.sessionID, input.run_id)

        if (input.action === "inspect") {
          return { content: json(run), metadata: { run_id: run.run_id } }
        }

        if (input.action === "refresh") {
          if (!input.message?.trim()) throw new Error("refresh requires message: the reason source was recollected")
          if (run.scope.commit) throw new Error("a commit scope is immutable and cannot be refreshed")
          const previous = run.scope.digest
          const next = refreshScope(paths.projectRoot, run.scope)
          const updated: RunRecord = { ...run, scope: next }
          save(updated)
          return {
            content: `Scope refreshed.\nprevious_digest: ${previous}\ncurrent_digest: ${next.digest}\nentries: ${next.entries.length}\nreason: ${input.message}`,
            metadata: { run_id: run.run_id, scope_digest: next.digest },
          }
        }

        if (input.action === "record") {
          if (input.reviewer && detectDrift(paths.projectRoot, run.scope)) {
            throw new Error(
              "source changed after the recorded scope; refresh the scope before recording independent review so the reviewer evidence binds to current bytes",
            )
          }
          let plan = run.plan
          if (input.plan_path) {
            const absolute = resolveInsideProject(paths.projectRoot, input.plan_path)
            if (!existsSync(absolute)) throw new Error(`plan does not exist: ${input.plan_path}`)
            plan = { path: input.plan_path, digest: sha256File(absolute) }
          }
          const updated: RunRecord = {
            ...run,
            ...(plan ? { plan } : {}),
            inputs: [...run.inputs, ...fileRecords(paths.projectRoot, input.inputs ?? [])],
            outputs: [...run.outputs, ...fileRecords(paths.projectRoot, input.outputs ?? [])],
            limits: [...run.limits, ...(input.limits ?? [])],
            confidence: input.confidence ?? run.confidence,
            ...(input.reviewer
              ? { review: { run_id: run.run_id, reviewer: input.reviewer, source_digest: run.scope.digest } }
              : {}),
          }
          save(updated)
          if (input.decision) {
            appendJournal(paths, updated, { event: "decision", text: input.decision })
          }
          return {
            content: `Recorded.\n${runSummary(updated)}\ninputs: ${updated.inputs.length}\noutputs: ${updated.outputs.length}\nlimits: ${updated.limits.length}`,
            metadata: { run_id: updated.run_id },
          }
        }

        // finalize
        const drift = detectDrift(paths.projectRoot, run.scope)
        const result = finalizeRun(run, { drift })
        if (!result.ok) {
          const failed: RunRecord = { ...run, status: result.status }
          save(failed)
          return {
            content: [
              `FINALIZE FAILED (status: ${result.status})`,
              ...result.problems.map((problem) => `- ${problem}`),
              drift
                ? "Source changed after the last recorded scope. Call rig_evidence action=refresh with a reason, then rerun gates."
                : "",
            ]
              .filter(Boolean)
              .join("\n"),
            metadata: { run_id: run.run_id, ok: false, status: result.status },
          }
        }
        const completed: RunRecord = { ...run, status: "completed" }
        const reportPath = writeReport(paths, completed)
        const withReport: RunRecord = { ...completed, report_path: reportPath }
        save(withReport)
        return {
          content: `Run completed.\n${runSummary(withReport)}\nreport: ${reportPath}\n\n${gateTable(withReport)}`,
          metadata: { run_id: run.run_id, ok: true, report_path: reportPath },
        }
      },
    })
  })
  registrations.push(evidence)

  const gate = await ctx.tool.transform((editor: any) => {
    editor.add({
      name: "rig_gate",
      description:
        "Detect and run the project's configured gates, record real exit codes and log digests, and store receipts on a run. Never invents a command.",
      input: GATE_INPUT,
      options: { codemode: true },
      execute: async (raw: unknown, context: any) => {
        const input = raw as GateInput
        const specs = detectGates(paths.projectRoot, options.gates)

        if (input.action === "detect") {
          if (specs.length === 0) {
            return {
              content:
                "No configured gates detected. Do not invent one: configure a gate or record rig_evidence gate_exemption with the manual check that replaces it.",
              metadata: { gates: [] },
            }
          }
          return {
            content: specs.map((spec) => `${spec.id}\t${spec.command}\t(${spec.source})`).join("\n"),
            metadata: { gates: specs.map((spec) => spec.id) },
          }
        }

        if (input.action !== "run") throw new Error(`unknown gate action: ${String(input.action)}`)
        const run = resolveRun(paths, context.sessionID, input.run_id)
        if (specs.length === 0) {
          throw new Error("no configured gates detected; record gate_exemption instead of inventing a command")
        }
        const selected = selectGates(specs, input.names)
        const files = input.files ?? run.scope.entries.map((entry) => entry.path).slice(0, MAX_FILES)
        for (const spec of selected) {
          if (spec.needsFiles && files.length === 0) {
            throw new Error(`gate '${spec.id}' needs files, but the run scope is empty`)
          }
        }

        await ctx.permission.assert({
          sessionID: context.sessionID,
          agent: context.agent,
          source: { type: "tool", messageID: context.messageID, id: context.id },
          action: "shell",
          resources: selected.map((spec) => spec.command),
          metadata: { gates: selected.map((spec) => spec.id) },
        })

        const timeout = input.timeout_ms ?? options.gateTimeoutMs
        const results = runGates(selected, paths.projectRoot, files, timeout)
        const directory = ensureDir(join(paths.runsRoot, run.skill, run.run_id, "gates"))
        for (const result of results) {
          const log = [
            `# ${result.receipt.id}`,
            `command: ${result.receipt.command}`,
            `source: ${result.receipt.source}`,
            `status: ${result.receipt.status}`,
            `exit_code: ${String(result.receipt.exit_code)}`,
            `duration_ms: ${result.receipt.duration_ms}`,
            `stdout_sha256: ${result.receipt.stdout_sha256}`,
            `stderr_sha256: ${result.receipt.stderr_sha256}`,
            "",
            result.receipt.tail,
            "",
          ].join("\n")
          writeJsonAtomic(join(directory, `${safeName(result.receipt.id)}.json`), result.receipt)
          writeFileSync(join(directory, `${safeName(result.receipt.id)}.log`), log, "utf8")
        }
        // A rerun after a fix replaces the earlier receipt for the same gate.
        const replaced = new Set(results.map((result) => result.receipt.id))
        const gates = [
          ...run.gates.filter((receipt) => !replaced.has(receipt.id)),
          ...results.map((result) => result.receipt),
        ]
        const updated: RunRecord = { ...run, gates }
        save(updated)
        writeJsonAtomic(join(directory, "summary.json"), gates)

        const failed = gates.filter((receipt) => receipt.status === "failed")
        return {
          content: [
            gateTable(updated),
            "",
            failed.length === 0
              ? "All recorded gates passed."
              : `FAILED: ${failed.map((receipt) => receipt.id).join(", ")}. Fix the cause and rerun the same gate; do not retry unchanged.`,
          ].join("\n"),
          metadata: { run_id: run.run_id, failed: failed.map((receipt) => receipt.id) },
        }
      },
    })
  })
  registrations.push(gate)

  const findings = await ctx.tool.transform((editor: any) => {
    editor.add({
      name: "rig_findings",
      description:
        "Write or read a validated findings inventory with bucket accounting. Findings are untrusted until verified against source; every finding must end in one terminal bucket. For rig-review, correctness is mandatory and axes/aspects must be recorded with coverage.",
      input: FINDINGS_INPUT,
      options: { codemode: true },
      execute: async (raw: unknown, context: any) => {
        const input = raw as FindingsInput
        const run = resolveRun(paths, context.sessionID, input.run_id)
        const directory = join(paths.runsRoot, run.skill, run.run_id)
        const path = join(directory, "findings.json")
        const existing = readJsonIfExists<FindingsDocument>(path)

        if (input.action === "read") {
          if (!existing) throw new Error(`no findings artifact for run ${run.run_id}`)
          return { content: json(existing), metadata: { run_id: run.run_id } }
        }

        if (!input.findings)
          throw new Error("write requires findings: an array, empty only when review genuinely found nothing")
        const incoming = validateFindings(input.findings)
        const rawAxes = (input.axes ?? input.aspects ?? existing?.axes ?? []) as readonly string[]
        if (rawAxes.length === 0)
          throw new Error("write requires axes/aspects: the review axes that actually ran (include 'correctness')")
        const normalizedAxes = [...new Set(rawAxes.map((a) => String(a).trim().toLowerCase()))].filter(Boolean)
        const allowed = new Set(["correctness", "security", "tests", "api", "performance", "docs", "data"])
        for (const axis of normalizedAxes) {
          if (!allowed.has(axis)) {
            throw new Error(
              `unknown review axis/aspect '${axis}'; allowed: correctness, security, tests, api, performance, docs, data`,
            )
          }
        }
        if (normalizedAxes.length === 0)
          throw new Error("write requires axes/aspects: the review axes that actually ran (include 'correctness')")
        if (run.skill === "rig-review" && !normalizedAxes.includes("correctness")) {
          throw new Error(
            "rig-review requires correctness axis/aspect (must run correctness review). Add 'correctness' to axes or aspects.",
          )
        }
        const coverage = (input.coverage ?? existing?.coverage ?? []) as readonly string[]
        if (coverage.length === 0) throw new Error("write requires coverage: the files or areas actually reviewed")
        if (run.skill === "rig-review" && normalizedAxes.includes("correctness") && coverage.length === 0) {
          throw new Error(
            "rig-review with correctness axis requires non-empty coverage of files/areas actually inspected",
          )
        }
        if (run.skill === "rig-review" && !input.reviewer) {
          throw new Error("a rig-review findings write requires reviewer: the independent reviewer identity")
        }
        if (run.skill === "rig-review" && detectDrift(paths.projectRoot, run.scope)) {
          throw new Error(
            "source changed after the review scope was recorded; start a new review run instead of certifying changed bytes",
          )
        }

        for (const finding of incoming) {
          if (finding.status !== "open" && !finding.disposition) {
            throw new Error(`finding ${finding.id} is '${finding.status}' without a disposition`)
          }
        }

        const merged = mergeFindings(existing?.findings ?? [], incoming)
        const document: FindingsDocument = {
          schema: 1,
          run_id: run.run_id,
          target_digest: run.scope.digest,
          axes: normalizedAxes,
          coverage,
          findings: merged,
        }
        ensureDir(directory)
        writeJsonAtomic(path, document)

        const summary = findingsSummary(path, merged)
        const updated: RunRecord = {
          ...run,
          findings: summary,
          ...(run.skill === "rig-review" && input.reviewer
            ? { review: { run_id: run.run_id, reviewer: input.reviewer, source_digest: run.scope.digest } }
            : {}),
        }
        save(updated)

        return {
          content: [
            `Findings written: ${path}`,
            `axes: ${normalizedAxes.join(", ")}`,
            `coverage: ${coverage.length} area(s)`,
            `counts: open ${summary.open}, fixed ${summary.fixed}, rejected ${summary.rejected}, deferred ${summary.deferred}, accepted-risk ${summary.accepted_risk}`,
          ].join("\n"),
          metadata: { run_id: run.run_id, open: summary.open },
        }
      },
    })
  })
  registrations.push(findings)

  const loop = await ctx.tool.transform((editor: any) => {
    editor.add({
      name: "rig_loop",
      description:
        "Drive the adversarial convergence ledger: append scored rounds, print the old+new progress table, and finalize with a validated stop reason.",
      input: LOOP_INPUT,
      options: { codemode: true },
      execute: async (raw: unknown, context: any) => {
        const input = raw as LoopInput
        const run = resolveRun(paths, context.sessionID, input.run_id)
        if (run.skill !== "rig-challenge") throw new Error("rig_loop only operates on a rig-challenge run")
        const path = join(paths.runsRoot, run.skill, run.run_id, "loop.jsonl")
        const records = readLoop(path)

        if (input.action === "read") {
          const rows = progress(records)
          return {
            content: [`Rounds: ${rows.length}`, renderProgressTable(rows), json(rows)].filter(Boolean).join("\n\n"),
            metadata: { run_id: run.run_id, rounds: rows.length },
          }
        }

        if (input.action === "start") {
          if (records.length > 0) throw new Error("loop already started; use action=append")
          const reviewer = requireReviewer(input.reviewer)
          const findingList = validateLoopFindings(input.findings)
          const residue = score(findingList)
          appendLoopRecord(path, {
            schema: 1,
            run_id: run.run_id,
            round: 1,
            reviewer,
            source_digest: input.source_digest ?? run.scope.digest,
            findings: findingList,
            weighted: residue.weighted,
          })
          const rows = progress(readLoop(path))
          return {
            content: [`Baseline recorded. Weighted score W0 = ${residue.weighted}.`, renderProgressTable(rows)].join(
              "\n\n",
            ),
            metadata: { run_id: run.run_id, weighted: residue.weighted },
          }
        }

        if (input.action === "append") {
          const last = latestRecord(records)
          if (!last) throw new Error("loop not started; use action=start for the baseline round")
          if (last.stop_reason) throw new Error(`loop already finalized with '${last.stop_reason}'`)
          const reviewer = requireReviewer(input.reviewer)
          if (input.findings === undefined)
            throw new Error("append requires findings: every prior signature must be assessed")
          const findingList = validateLoopFindings(input.findings)
          const assessed = new Set(findingList.map((finding) => finding.signature))
          const missing = last.findings
            .map((finding) => finding.signature)
            .filter((signature) => !assessed.has(signature))
          if (missing.length > 0) throw new Error(`re-assessment must address prior signatures: ${missing.join(", ")}`)
          const residue = score(findingList)
          appendLoopRecord(path, {
            schema: 1,
            run_id: run.run_id,
            round: last.round + 1,
            reviewer,
            source_digest: input.source_digest ?? run.scope.digest,
            findings: findingList,
            weighted: residue.weighted,
          })
          const rows = progress(readLoop(path))
          const decision = convergence(rows)
          return {
            content: [
              `Round ${last.round + 1} recorded. Weighted score = ${residue.weighted}.`,
              renderProgressTable(rows),
              `Convergence: ${decision.converged ? "yes" : "no"}${decision.stop ? ` (${decision.stop})` : ""} — ${decision.reason}`,
            ].join("\n\n"),
            metadata: { run_id: run.run_id, weighted: residue.weighted, converged: decision.converged },
          }
        }

        // finalize
        if (!input.stop_reason) throw new Error(`finalize requires stop_reason: one of ${STOP_REASONS.join(", ")}`)
        const reviewer = requireReviewer(input.reviewer)
        const last = latestRecord(records)
        if (!last) throw new Error("loop not started; cannot finalize")
        if (last.stop_reason) throw new Error(`loop already finalized with '${last.stop_reason}'`)
        const rows = progress(records)
        const decision = convergence(rows)
        validateStop(input.stop_reason, decision, rows.length, options.maxLoopRounds, input.note)
        if (!run.scope.commit && detectDrift(paths.projectRoot, run.scope)) {
          throw new Error(
            "source changed after the recorded scope; refresh and run one more assessed round before finalizing the loop",
          )
        }
        if (last.source_digest !== run.scope.digest) {
          throw new Error(
            `the last round assessed ${last.source_digest} but the current scope is ${run.scope.digest}; refresh and append a re-assessment first`,
          )
        }
        const residue = score(last.findings)
        if (residue.security > 0 || residue.critical > 0) {
          throw new Error(
            `cannot finalize with ${residue.security} security and ${residue.critical} critical finding(s) open`,
          )
        }
        appendLoopRecord(path, {
          schema: 1,
          run_id: run.run_id,
          round: last.round,
          reviewer,
          source_digest: last.source_digest,
          findings: last.findings,
          weighted: residue.weighted,
          stop_reason: input.stop_reason,
          ...(input.note ? { note: input.note } : {}),
        })
        const updated: RunRecord = {
          ...run,
          review: { run_id: run.run_id, reviewer, source_digest: last.source_digest },
          loop: {
            path,
            rounds: rows.length,
            weighted: residue.weighted,
            stop_reason: input.stop_reason,
            open_security: residue.security,
            open_critical: residue.critical,
          },
        }
        save(updated)
        const finalRows = progress(readLoop(path))
        return {
          content: [
            `Loop finalized: ${input.stop_reason}. Weighted score = ${residue.weighted}.`,
            renderProgressTable(finalRows),
            input.stop_reason === "converged" ? "Converged." : "Not converged: completion will remain blocked.",
          ].join("\n\n"),
          metadata: { run_id: run.run_id, stop_reason: input.stop_reason },
        }
      },
    })
  })
  registrations.push(loop)

  const grant = await ctx.tool.transform((editor: any) => {
    editor.add({
      name: "rig_git_grant",
      description:
        "Read, record, or revoke the local Git approval grant. A grant covers plain local commits only; push, force, history rewrite, and destructive operations always ask.",
      input: GRANT_INPUT,
      options: { codemode: true, permission: "rig_git_grant" },
      execute: async (raw: unknown, context: any) => {
        const input = raw as GrantInput
        const commonDir = resolveGitCommonDir(paths.projectRoot)

        if (input.action === "status") {
          const state = readGrant(commonDir)
          return {
            content: json({ ...state, ...(commonDir ? { expected_path: grantPath(commonDir) } : {}) }),
            metadata: { status: state.status },
          }
        }

        if (!commonDir)
          throw new Error("not inside a Git repository (or inside a bare repository); no grant can be stored")
        const path = grantPath(commonDir)

        if (input.action === "approve") {
          if (!input.confirm?.trim()) {
            throw new Error("approve requires confirm: the user's own approval words for local commits")
          }
          if (input.scope_acknowledged !== true) {
            throw new Error("approve requires scope_acknowledged=true after the user accepted the grant exclusions")
          }
          await ctx.permission.assert({
            sessionID: context.sessionID,
            agent: context.agent,
            source: { type: "tool", messageID: context.messageID, id: context.id },
            action: "rig_git_grant",
            resources: [path],
            metadata: { scope: "local-git" },
          })
          const record = writeGrant(commonDir)
          return {
            content: `Local Git grant recorded at ${path}.\n${json(record)}\nIt covers plain local commits in every worktree of this repository. It never covers push, force, history rewriting, destructive operations, or authentication changes.`,
            metadata: { status: "granted", path },
          }
        }

        if (input.action === "revoke") {
          await ctx.permission.assert({
            sessionID: context.sessionID,
            agent: context.agent,
            source: { type: "tool", messageID: context.messageID, id: context.id },
            action: "rig_git_grant",
            resources: [path],
            metadata: { scope: "local-git" },
          })
          const removed = revokeGrant(commonDir)
          return {
            content: removed ? `Grant revoked: ${path}` : `No grant to revoke at ${path}.`,
            metadata: { status: removed ? "absent" : "granted" },
          }
        }

        throw new Error(`unknown grant action: ${String(input.action)}`)
      },
    })
  })
  registrations.push(grant)

  const dispatch = await ctx.tool.transform((editor: any) => {
    editor.add({
      name: "rig_dispatch",
      description:
        "Run one bounded role in a child session when native rig-* subagents are unavailable. The child never runs gates, commits, or declares completion.",
      input: DISPATCH_INPUT,
      options: { codemode: true },
      execute: async (raw: unknown) => {
        const input = raw as DispatchInputTool
        if (input.role !== "rig-implementer" && input.read_only === false) {
          throw new Error(`role ${input.role} is read-only; only rig-implementer may receive write scope`)
        }
        if (input.read_only === false && (!input.paths || input.paths.length === 0)) {
          throw new Error("a writing child needs explicit owned paths")
        }
        const result = await withDispatchSlot(() =>
          dispatchRole(
            ctx,
            {
              role: input.role,
              task: input.task,
              ...(input.paths ? { paths: input.paths } : {}),
              ...(input.read_only === undefined ? {} : { read_only: input.read_only }),
              ...(input.keep_session === undefined ? {} : { keep_session: input.keep_session }),
            },
            resourcesDir,
          ),
        )
        return {
          content: `role: ${result.role}\nsession: ${result.session_id}\n\n${result.text}`,
          metadata: { role: result.role, session_id: result.session_id },
        }
      },
    })
  })
  registrations.push(dispatch)

  const doctor = await ctx.tool.transform((editor: any) => {
    editor.add({
      name: "rig_doctor",
      description:
        "Check opencode-rig install health: plugin load, version agreement, agent installation, Git grant, artifact ignore, configured gates, and stale runs. action=assets also curates shipped skill and role cards for roster drift, dead links, duplicated prose, and size.",
      input: DOCTOR_INPUT,
      options: { codemode: true },
      execute: async (raw: unknown, context: any) => {
        const input = raw as DoctorInputTool
        const commonDir = resolveGitCommonDir(paths.projectRoot)
        const dirs = resolveAgentDirs(paths.projectRoot)
        const agentDir = existsSync(dirs.project) ? dirs.project : dirs.global
        const listed = await ctx.plugin.list()
        const pluginListed = listed.data.some((plugin: any) => plugin.id === "opencode-rig")

        const report = doctorReport({
          packageRoot: resolve(resourcesDir, "..", ".."),
          projectRoot: paths.projectRoot,
          artifactRoot: paths.artifactRoot,
          pluginListed,
          commonDir,
          agentDir,
          options,
          assets: input.action === "assets",
        })

        if (input.action === "report" || input.action === "assets") {
          return {
            content: [
              report.ok ? "opencode-rig doctor: healthy" : "opencode-rig doctor: problems found",
              ...report.lines,
            ].join("\n"),
            metadata: { ok: report.ok },
          }
        }

        if (input.action !== "clean") throw new Error(`unknown doctor action: ${String(input.action)}`)
        if (input.confirm !== true)
          throw new Error("clean requires confirm=true after the user approves deleting stale runs")
        const stale = staleRuns(join(paths.artifactRoot, "runs"), options.ttlDays)
        if (stale.length === 0) return { content: "No stale runs.", metadata: { removed: 0 } }
        await ctx.permission.assert({
          sessionID: context.sessionID,
          agent: context.agent,
          source: { type: "tool", messageID: context.messageID, id: context.id },
          action: "rig_doctor_clean",
          resources: stale,
        })
        for (const path of stale) rmSync(path, { recursive: true, force: true })
        return {
          content: [
            `Removed ${stale.length} stale run director${stale.length === 1 ? "y" : "ies"}.`,
            ...stale.map((path) => `- ${path}`),
          ].join("\n"),
          metadata: { removed: stale.length },
        }
      },
    })
  })
  registrations.push(doctor)

  return registrations
}

function selectGates(specs: readonly GateSpec[], names: readonly string[] | undefined): GateSpec[] {
  if (!names || names.length === 0) return [...specs]
  const byId = new Map(specs.map((spec) => [spec.id, spec]))
  const unknown = names.filter((name) => !byId.has(name))
  if (unknown.length > 0) {
    throw new Error(`unknown gate(s): ${unknown.join(", ")}; available: ${specs.map((spec) => spec.id).join(", ")}`)
  }
  return names.map((name) => byId.get(name)!).filter(Boolean)
}

function safeName(value: string): string {
  return value.replace(/[^a-zA-Z0-9._-]+/g, "-")
}

function validateLoopFindings(findings: readonly LoopFinding[] | undefined): LoopFinding[] {
  if (!findings) throw new Error("findings are required")
  const seen = new Set<string>()
  return findings.map((finding) => {
    if (!finding.signature?.trim()) throw new Error("every loop finding needs a signature")
    if (seen.has(finding.signature)) throw new Error(`duplicate loop signature: ${finding.signature}`)
    if (!(SEVERITIES as readonly string[]).includes(finding.severity)) {
      throw new Error(`unknown severity: ${String(finding.severity)}`)
    }
    if (finding.status !== "open" && finding.status !== "fixed" && finding.status !== "rejected") {
      throw new Error(`unknown loop status: ${String(finding.status)}`)
    }
    seen.add(finding.signature)
    return finding
  })
}

function requireReviewer(reviewer: string | undefined): string {
  if (!reviewer?.trim()) throw new Error("this loop action requires reviewer: the independent challenger identity")
  return reviewer
}

function validateStop(
  stop: StopReason,
  decision: ReturnType<typeof convergence>,
  rounds: number,
  maxRounds: number,
  note: string | undefined,
): void {
  if (!(STOP_REASONS as readonly string[]).includes(stop)) throw new Error(`unknown stop reason: ${stop}`)
  if (stop === "converged" && !decision.converged) {
    throw new Error(`cannot record convergence: ${decision.reason}`)
  }
  if (stop === "plateau" && decision.stop !== "plateau") {
    throw new Error(`cannot record a plateau: ${decision.reason}`)
  }
  if (stop === "non-converging" && decision.stop !== "non-converging") {
    throw new Error(`cannot record non-convergence: ${decision.reason}`)
  }
  if (stop === "round-limit" && rounds < maxRounds) {
    throw new Error(`round-limit needs ${maxRounds} assessed rounds, found ${rounds}`)
  }
  if ((stop === "authority-required" || stop === "drift" || stop === "unavailable-reviewer") && !note?.trim()) {
    throw new Error(`${stop} requires a note explaining the stop`)
  }
}
