/**
 * Tool registration.
 *
 * Tools are the only place where a workflow action becomes a durable artifact.
 * Every mutating action loads the run from disk first, validates its input, and
 * writes through the atomic helpers, so a rejected call leaves no trace.
 */

import { randomUUID } from "node:crypto"
import {
  appendFileSync,
  chmodSync,
  existsSync,
  closeSync,
  lstatSync,
  openSync,
  readFileSync,
  readSync,
  realpathSync,
  renameSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs"
import { dirname, isAbsolute, join, relative, resolve, sep } from "node:path"
import type { Plugin } from "@opencode/plugin"
import { sha256, sha256File, scopeDigest, type ScopeEntry } from "../digest.ts"
import { dispatchRole, ROLE_IDS, type RoleId } from "../dispatch.ts"
import { doctorReport, installedAgentStatus, resolveAgentDirs, staleRuns } from "../doctor.ts"
import {
  collectCommitScope,
  collectScope,
  detectDrift,
  lineDiffCount,
  lineHashes,
  refreshScope,
  scopeBase,
} from "../evidence.ts"
import {
  bucketCounts,
  mergeFindings,
  validateFindings,
  SEVERITIES,
  type Finding,
  type FindingsDocument,
} from "../findings.ts"
import { detectGates, runGate, type GateResult, type GateSpec } from "../gates.ts"
import { executeScript, INTERPRETERS, resolveScript, type ScriptInterpreter, type ScriptSource } from "../script.ts"
import { detectRuntime, isPinnedImage, runInSandbox } from "../sandbox.ts"
import { isReviewWorktree, readTrust, TRUST_WORDS, writeTrust } from "../trust.ts"
import { grantPath, readGrant, revokeGrant, writeGrant } from "../git-grant.ts"
import { markdownSection, planTreeState } from "../plan.ts"
import { MAX_MINI_PLAN_LINES, MAX_MINI_PLAN_PATHS, miniPlanProblems, parseMiniPlan } from "../mini-plan.ts"
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
  currentGateReceipts,
  finalizeRun,
  loadRun,
  saveRun,
  writeReport,
  type FileRecord,
  type FindingsSummary,
  type RunRecord,
  type ScopeRecord,
} from "../run.ts"
import { SKILLS } from "../skill-manifest.ts"
import { PLUGIN_VERSION } from "../version.ts"
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
const WRITE_ROLES: ReadonlySet<string> = new Set(["rig-builder", "rig-creator"])
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
  let ancestor = absolute
  while (!lstatSync(ancestor, { throwIfNoEntry: false })) ancestor = dirname(ancestor)
  const physical = relative(realpathSync(projectRoot), realpathSync(ancestor))
  if (physical === ".." || physical.startsWith(`..${sep}`) || isAbsolute(physical)) {
    throw new Error(`path resolves outside the project: ${path}`)
  }
  return absolute
}

function fileRecords(projectRoot: string, paths: readonly string[], allowDeleted = false): FileRecord[] {
  return paths.map((path) => {
    const absolute = resolveInsideProject(projectRoot, path)
    if (!existsSync(absolute)) {
      const canonical = relative(projectRoot, absolute).split(sep).join("/")
      if (
        allowDeleted &&
        collectScope(projectRoot, [path]).entries.some(
          (entry) => entry.path === canonical && entry.status.includes("D"),
        )
      ) {
        return { path, sha256: sha256("") }
      }
      throw new Error(`evidence path does not exist: ${path}`)
    }
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

/** Read and validate the inventory whose exact bytes the run recorded. */
function readFindings(paths: RigPaths, run: RunRecord): FindingsDocument {
  if (!run.findings) throw new Error(`findings artifact is missing for run ${run.run_id}`)
  const path = resolveInsideProject(paths.projectRoot, run.findings.path)
  const document = readJsonIfExists<FindingsDocument>(path)
  if (
    !document ||
    document.schema !== 1 ||
    document.run_id !== run.run_id ||
    document.target_digest !== run.scope.digest
  ) {
    throw new Error(`invalid findings artifact for run ${run.run_id}`)
  }
  if (sha256File(path) !== run.findings.digest) throw new Error(`findings artifact changed for run ${run.run_id}`)
  if (
    run.skill === "code-remediate" &&
    (document.source_run_id !== run.source_run_id || document.source_digest !== run.source_findings?.digest)
  ) {
    throw new Error("remediation findings do not match the bound source inventory")
  }
  return { ...document, findings: validateFindings(document.findings) }
}

/** Resolve a completed review and its immutable inventory for remediation. */
function sourceInventory(
  paths: RigPaths,
  sessionID: string,
  runId: string,
): { run: RunRecord; document: FindingsDocument } {
  const run = resolveRun(paths, sessionID, runId)
  if (run.skill !== "code-review" || run.status !== "completed" || run.review?.source_digest !== run.scope.digest)
    throw new Error("source_run_id must name a completed code-review run")
  return { run, document: readFindings(paths, run) }
}

/** Bind implementation to the bytes independently reviewed by a completed planning run. */
function reviewedPlan(
  paths: RigPaths,
  sessionID: string,
  runId: string,
  planPath: string,
): NonNullable<RunRecord["plan"]> {
  const source = resolveRun(paths, sessionID, runId)
  if (source.skill !== "rig-plan" || source.status !== "completed" || !source.plan || !source.review) {
    throw new Error("plan_run_id must name a completed independently reviewed rig-plan run")
  }
  const path = resolveInsideProject(paths.projectRoot, planPath)
  if (
    path !== resolveInsideProject(paths.projectRoot, source.plan.path) ||
    source.review.plan_digest !== source.plan.digest ||
    source.review.source_digest !== source.scope.digest ||
    sha256File(path) !== source.plan.digest
  ) {
    throw new Error("plan bytes or independent review no longer match the completed plan run")
  }
  return { path: planPath, digest: source.plan.digest, run_id: source.run_id }
}

/**
 * Scope a trivial run by its mini-plan's owned paths. A declared file may not
 * exist yet, because a small new file is a normal trivial change, so a missing
 * path enters the inventory as an empty pending entry; the ordinary collection
 * hashes it as soon as the file exists.
 */
function trivialScope(projectRoot: string, owned: readonly string[]): ScopeRecord {
  const present = owned.filter((path) => existsSync(resolveInsideProject(projectRoot, path)))
  const missing = owned.filter((path) => !present.includes(path))
  const collected = present.length > 0 ? collectScope(projectRoot, present) : undefined
  const entries: ScopeEntry[] = [
    ...(collected?.entries ?? []),
    ...missing.map((path) => ({ path, status: "?", sha256: sha256("") })),
  ].sort((left, right) => left.path.localeCompare(right.path))
  return {
    base: scopeBase(projectRoot),
    paths: [...owned],
    dirty: entries.length > 0,
    entries,
    digest: scopeDigest(entries),
  }
}

/** Reject a plan whose task tree is missing or invalid at the binding point. */
function assertPlanTree(paths: RigPaths, planPath: string, owner: string): void {
  const absolute = resolveInsideProject(paths.projectRoot, planPath)
  if (!existsSync(absolute)) throw new Error(`plan does not exist: ${planPath}`)
  const { problems } = planTreeState(readFileSync(absolute, "utf8"))
  if (problems.length > 0) throw new Error(`${owner} plan task tree invalid: ${problems.join("; ")}`)
}

/** The run's own artifacts are never part of the change it certifies. */
function withoutArtifacts(paths: RigPaths, values: readonly string[]): string[] {
  const root = relative(paths.projectRoot, paths.artifactRoot).split(sep).join("/")
  return values.filter((path) => path !== root && !path.startsWith(`${root}/`))
}

interface TrivialBaseline {
  /** Per-line hashes of every owned file at start; the run reports its own delta from these. */
  readonly lineHashes?: Readonly<Record<string, readonly string[]>>
}

type TrivialMeasurement = {
  readonly lines: number
  readonly files: number
  readonly unmeasured: readonly string[]
}

/**
 * Measure the run's own edit from the line hashes recorded at start. This is
 * accounting, not a boundary: Git, its index, attributes, filters and config
 * are not consulted, and the result is reported rather than enforced.
 */
function measureTrivial(paths: RigPaths, run: RunRecord): TrivialMeasurement | undefined {
  const baseline = readBaseline(join(paths.runsRoot, run.skill, run.run_id, "trivial-baseline.json"))
  let lines = 0
  let files = 0
  const unmeasured: string[] = []
  for (const path of run.scope.paths) {
    const absolute = resolveInsideProject(paths.projectRoot, path)
    const current = existsSync(absolute) ? lineHashes(absolute) : []
    const start = baseline?.lineHashes?.[path]
    if (current === undefined || start === undefined) {
      unmeasured.push(path)
      continue
    }
    const changed = lineDiffCount(start, current)
    if (changed > 0) {
      files += 1
      lines += changed
    }
  }
  return { lines, files, unmeasured }
}

/** Read the start baseline without trusting its shape; a damaged file is no measurement. */
function readBaseline(path: string): TrivialBaseline | undefined {
  try {
    const raw = readJsonIfExists<TrivialBaseline>(path)
    if (!raw || raw.lineHashes === undefined) return undefined
    if (typeof raw.lineHashes !== "object" || raw.lineHashes === null || Array.isArray(raw.lineHashes)) return undefined
    const entries = Object.values(raw.lineHashes)
    if (entries.some((entry) => !Array.isArray(entry) || entry.some((value) => typeof value !== "string")))
      return undefined
    return raw
  } catch {
    return undefined
  }
}

/** Real path of the nearest existing ancestor of a declared path. */
function physicalAncestor(projectRoot: string, path: string): string {
  let current = resolveInsideProject(projectRoot, path)
  for (;;) {
    if (existsSync(current)) return realpathSync(current)
    const parent = dirname(current)
    if (parent === current) return ""
    current = parent
  }
}

/** True when a recorded path sits inside the run's declared scope. */
function insideScope(projectRoot: string, path: string, scope: readonly string[]): boolean {
  const relativePath = relative(projectRoot, resolveInsideProject(projectRoot, path)).split(sep).join("/")
  return scope.some((entry) => relativePath === entry || relativePath.startsWith(`${entry}/`))
}

/** Refuse a current owned-path state that would escape the project or its source. */
function assertOwnedPathsSafe(paths: RigPaths, run: RunRecord): void {
  if (!run.trivial_plan?.trim() || run.plan) return
  const realArtifactRoot = existsSync(paths.artifactRoot) ? realpathSync(paths.artifactRoot).toLowerCase() : ""
  const gitDirs = [resolve(paths.projectRoot, ".git"), resolveGitCommonDir(paths.projectRoot)]
    .filter((value): value is string => value !== undefined && existsSync(value))
    .map((value) => realpathSync(value).toLowerCase())
  for (const path of run.scope.paths) {
    const absolute = resolveInsideProject(paths.projectRoot, path)
    const stat = lstatSync(absolute, { throwIfNoEntry: false })
    if (stat?.isSymbolicLink()) throw new Error(`owned path is a symbolic link: ${path}`)
    if (stat?.isDirectory()) throw new Error(`owned path is a directory: ${path}`)
    if (stat && stat.nlink > 1) throw new Error(`owned path is a hard link: ${path}`)
    if (stat && !stat.isFile()) throw new Error(`owned path is not a regular file: ${path}`)
    const real = physicalAncestor(paths.projectRoot, path).toLowerCase()
    if (
      real &&
      ((realArtifactRoot.length > 0 && (real === realArtifactRoot || real.startsWith(`${realArtifactRoot}${sep}`))) ||
        gitDirs.some((dir) => real === dir || real.startsWith(`${dir}${sep}`)))
    )
      throw new Error(`owned path resolves outside project source: ${path}`)
  }
}

/** Keep evidence mutations inside the active phase of a run. */
function requireInProgress(run: RunRecord): void {
  if (run.status !== "in_progress") throw new Error(`run status is '${run.status}', not 'in_progress'`)
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
  const current = new Set(currentGateReceipts(run))
  const latest = new Map(run.gates.map((gate) => [gate.id, gate]))
  return [
    "| Gate | Status | Exit | Command | Currency |",
    "| --- | --- | --- | --- | --- |",
    ...run.gates.map((gate) => {
      const currency = current.has(gate) ? "current" : latest.get(gate.id) === gate ? "stale" : "superseded"
      return `| \`${gate.id}\` | ${gate.status} | ${gate.exit_code ?? "-"} | \`${gate.command}\` | ${currency} |`
    }),
  ].join("\n")
}

interface EvidenceInput {
  readonly action: "start" | "record" | "refresh" | "finalize" | "inspect"
  readonly run_id?: string
  readonly skill?: string
  readonly paths?: readonly string[]
  readonly commit?: string
  readonly plan_path?: string
  readonly plan_run_id?: string
  readonly plan_digest?: string
  readonly trivial_plan?: string
  readonly source_run_id?: string
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
  readonly source_run_id?: string
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
    plan_run_id: {
      type: "string",
      description:
        "Completed rig-plan run that reviewed this unchanged plan. Required for nontrivial implementation start.",
    },
    plan_digest: {
      type: "string",
      description:
        "Exact plan digest assessed by the independent rig-plan reviewer. Required when recording plan review.",
    },
    trivial_plan: {
      type: "string",
      description:
        "Structured mini-plan JSON for a trivial implementation: {goal, owned_paths (at most 2 project-relative files), acceptance_check, feasibility}. Alternative to plan_path and plan_run_id at start; anything larger uses rig-plan.",
    },
    source_run_id: {
      type: "string",
      description: "Completed code-review inventory to reconcile. Required for remediation start.",
    },
    gate_exemption: {
      type: "string",
      description:
        "Reason no configured gate applies. Only for a project with no detectable gate, or a documentation-only code-remediate scope whose detected gates all run the test suite.",
    },
    inputs: { ...strArray, description: "Files read as authoritative evidence. Must exist." },
    outputs: {
      ...strArray,
      description: "Files created, modified, or deleted by the run. Missing paths must be tracked deletions.",
    },
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
    source_run_id: {
      type: "string",
      description: "Source review run bound at remediation start. Required for remediation writes; cannot change.",
    },
    axes: { ...strArray, description: "Review axes that actually ran." },
    aspects: { ...strArray, description: "Alias for axes (review aspects). Use axes/aspects as appropriate." },
    coverage: { ...strArray, description: "Files or areas actually covered." },
    reviewer: { type: "string", description: "Independent reviewer identity. Required for a code-review write." },
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
    run_id: { type: "string", description: "Run that owns the ledger (rig-challenge or rig-implement)." },
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
        "The user's literal Approve always answer after the plain-local-commit scope and exclusions were shown. Required by approve.",
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
      description: "Defaults to true. Only rig-builder and rig-creator may write; other roles are always read-only.",
    },
    keep_session: { type: "boolean", description: "Keep the child session for inspection. Defaults to false." },
  },
  required: ["role", "task"],
  additionalProperties: false,
} as const

const SCRIPT_INPUT = {
  type: "object",
  properties: {
    source: {
      type: "string",
      enum: ["run", "project", "plugin"],
      description:
        "run: the active run's scripts/ directory; project: scripts/, bin/ or tools/; plugin: the installed package's bin/ or scripts/.",
    },
    path: { type: "string", description: "Script file path inside the chosen source root." },
    interpreter: { type: "string", enum: [...INTERPRETERS], description: "Fixed interpreter used to run the file." },
    argv: {
      type: "array",
      items: { type: "string" },
      description: "Arguments after the script path; never a shell string.",
    },
    timeout_ms: { type: "number", description: "Bounded runtime, between 1000 and 3600000 milliseconds." },
  },
  required: ["source", "path", "interpreter"],
  additionalProperties: false,
} as const

const TRUST_INPUT = {
  type: "object",
  properties: {
    action: {
      type: "string",
      enum: ["approve", "status"],
      description: "approve records one commit; status lists records.",
    },
    commit: { type: "string", description: "Fetched PR head commit (40-hex); required by approve." },
    worktree: { type: "string", description: "Absolute review worktree path under <temp>/opencode/review-*." },
    ref: { type: "string", description: "The dedicated refs/rig-review/pr-<n>-<sha12> ref." },
    confirm: { type: "string", description: "The user's literal 'Trust this PR' answer; required by approve." },
  },
  required: ["action"],
  additionalProperties: false,
} as const

const SANDBOX_INPUT = {
  type: "object",
  properties: {
    worktree: { type: "string", description: "Absolute worktree to copy into the sandbox." },
    argv: {
      type: "array",
      items: { type: "string" },
      minItems: 1,
      description: "Command and arguments, never a shell string.",
    },
    timeout_ms: { type: "number", description: "Bounded runtime, between 1000 and 3600000 milliseconds." },
    network: { type: "boolean", description: "Opt in to container networking; the default is off." },
    image: { type: "string", description: "Pinned image overriding options.sandboxImage." },
  },
  required: ["worktree", "argv"],
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
          let scopePaths = input.paths ?? []
          let trivialOwned: string[] | undefined
          if (input.skill === "rig-implement" && input.trivial_plan?.trim()) {
            if (input.plan_path || input.plan_run_id)
              throw new Error("choose a trivial_plan or a reviewed plan, not both")
            if (input.commit)
              throw new Error("a trivial run binds working-tree bytes; pass a frozen plan for commit evidence")
            const problems = miniPlanProblems(input.trivial_plan)
            if (problems.length > 0)
              throw new Error(
                `trivial_plan is not a valid mini-plan: ${problems.join("; ")}. Expected {"goal","owned_paths","acceptance_check","feasibility"} with at most ${MAX_MINI_PLAN_PATHS} owned paths.`,
              )
            const miniPlan = parseMiniPlan(input.trivial_plan)!
            const normalize = (path: string) =>
              relative(paths.projectRoot, resolveInsideProject(paths.projectRoot, path)).split(sep).join("/") || "."
            trivialOwned = miniPlan.owned_paths.map(normalize)
            if (scopePaths.length > 0) {
              const supplied = scopePaths.map(normalize).sort().join("\n")
              if (supplied !== [...trivialOwned].sort().join("\n"))
                throw new Error(
                  "a trivial run is scoped by its mini-plan owned_paths; pass exactly those paths or none at all",
                )
            }
            scopePaths = trivialOwned
            const artifactRoot = relative(paths.projectRoot, paths.artifactRoot).split(sep).join("/").toLowerCase()
            const realArtifactRoot = existsSync(paths.artifactRoot)
              ? realpathSync(paths.artifactRoot).toLowerCase()
              : ""
            const gitDirs = [resolve(paths.projectRoot, ".git"), resolveGitCommonDir(paths.projectRoot)]
              .filter((value): value is string => value !== undefined && existsSync(value))
              .map((value) => realpathSync(value))
            for (const path of trivialOwned) {
              const lowered = path.toLowerCase()
              const absolute = resolveInsideProject(paths.projectRoot, path)
              const stat = lstatSync(absolute, { throwIfNoEntry: false })
              const real = physicalAncestor(paths.projectRoot, path).toLowerCase()
              if (
                lowered === artifactRoot ||
                lowered.startsWith(`${artifactRoot}/`) ||
                (real &&
                  realArtifactRoot.length > 0 &&
                  (real === realArtifactRoot || real.startsWith(`${realArtifactRoot}${sep}`)))
              )
                throw new Error(`owned path must be project source, not a run artifact: ${path}`)
              if (stat?.isSymbolicLink())
                throw new Error(`owned path must be a regular file, not a symbolic link: ${path}`)
              if (
                real &&
                gitDirs.some((dir) => {
                  const loweredDir = dir.toLowerCase()
                  return real === loweredDir || real.startsWith(`${loweredDir}${sep}`)
                })
              )
                throw new Error(`owned path must not touch VCS internals: ${path}`)
              if (stat?.isDirectory()) throw new Error(`owned path must name a file, not a directory: ${path}`)
              if (stat && stat.nlink > 1) throw new Error(`owned path must not be a hard link: ${path}`)
              if (stat && !stat.isFile())
                throw new Error(`owned path must be a regular file, not a pipe, socket or device: ${path}`)
              if (stat && lineHashes(absolute) === undefined)
                throw new Error(`owned path must be a small regular text file: ${path}`)
            }
          }
          const scope = trivialOwned
            ? trivialScope(paths.projectRoot, trivialOwned)
            : input.commit
              ? collectCommitScope(paths.projectRoot, input.commit, scopePaths)
              : collectScope(paths.projectRoot, scopePaths)
          let plan: RunRecord["plan"]
          if (input.plan_path) {
            const absolute = resolveInsideProject(paths.projectRoot, input.plan_path)
            if (!existsSync(absolute)) throw new Error(`plan does not exist: ${input.plan_path}`)
            plan = { path: input.plan_path, digest: sha256File(absolute) }
          }
          if (input.skill === "rig-implement" && !input.trivial_plan?.trim()) {
            if (!input.plan_run_id || !input.plan_path)
              throw new Error(
                "implementation starts from a frozen plan (plan_path and plan_run_id) or a structured trivial_plan; use rig-plan for anything larger",
              )
            plan = reviewedPlan(paths, context.sessionID, input.plan_run_id, input.plan_path)
          }
          if (plan && (input.skill === "rig-plan" || input.skill === "rig-implement")) {
            assertPlanTree(paths, plan.path, "start")
          }
          let source: Pick<RunRecord, "source_run_id" | "source_findings"> = {}
          if (input.skill === "code-remediate") {
            if (!input.source_run_id) throw new Error("remediation start requires source_run_id")
            const inventory = sourceInventory(paths, context.sessionID, input.source_run_id)
            source = {
              source_run_id: inventory.run.run_id,
              source_findings: {
                digest: inventory.run.findings!.digest,
                ids: inventory.document.findings.map((finding) => finding.id),
              },
            }
          }
          const run = createRun(paths, { skill: input.skill, scope, ...(plan ? { plan } : {}) })
          const baseline = trivialOwned
            ? {
                lineHashes: Object.fromEntries(
                  trivialOwned.map((path) => {
                    const absolute = resolveInsideProject(paths.projectRoot, path)
                    return [path, existsSync(absolute) ? (lineHashes(absolute) ?? []) : []]
                  }),
                ),
              }
            : undefined
          const created: RunRecord = {
            ...run,
            ...source,
            ...(input.trivial_plan?.trim() ? { trivial_plan: input.trivial_plan.trim() } : {}),
            ...(input.gate_exemption ? { gate_exemption: input.gate_exemption } : {}),
          }
          try {
            save(created)
            rememberRun(paths.artifactRoot, context.sessionID, { runId: created.run_id, skill: created.skill })
            if (baseline)
              writeJsonAtomic(join(paths.runsRoot, created.skill, created.run_id, "trivial-baseline.json"), baseline)
          } catch (error) {
            save({ ...created, status: "failed", stop_reason: "session registration failed" })
            throw error
          }
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
          assertOwnedPathsSafe(paths, run)
          if (run.status === "completed" || run.status === "failed") {
            throw new Error(`cannot refresh a run with status '${run.status}'`)
          }
          if (run.loop && run.loop.stop_reason !== "converged") {
            throw new Error(
              `cannot refresh a run whose loop stopped with '${run.loop.stop_reason}'; a replacement run is needed`,
            )
          }
          if (!input.message?.trim()) throw new Error("refresh requires message: the reason source was recollected")
          if (run.scope.commit) throw new Error("a commit scope is immutable and cannot be refreshed")
          const previous = run.scope.digest
          const next = refreshScope(paths.projectRoot, run.scope)
          const updated: RunRecord = {
            ...run,
            status: "in_progress",
            scope: next,
          }
          if (next.digest !== previous) {
            delete updated.review
            delete updated.findings
          }
          // A finalized ledger cannot be appended to, so archive it and let the
          // next challenge start a fresh one; an in-progress ledger keeps
          // appending because its rounds remain the honest history.
          if (run.loop?.stop_reason) {
            const ledger = join(paths.runsRoot, run.skill, run.run_id, "loop.jsonl")
            if (existsSync(ledger)) renameSync(ledger, `${ledger}.${Date.now()}.closed`)
          }
          delete updated.loop
          appendJournal(paths, updated, {
            event: "scope.refresh",
            previous_status: run.status,
            status: updated.status,
            previous_digest: previous,
            scope_digest: next.digest,
            reason: input.message,
          })
          save(updated)
          rememberRun(paths.artifactRoot, context.sessionID, { runId: run.run_id, skill: run.skill })
          return {
            content: `Scope refreshed.\nprevious_digest: ${previous}\ncurrent_digest: ${next.digest}\nentries: ${next.entries.length}\nreason: ${input.message}`,
            metadata: { run_id: run.run_id, scope_digest: next.digest },
          }
        }

        if (input.action === "record") {
          requireInProgress(run)
          assertOwnedPathsSafe(paths, run)
          const outputRecords = fileRecords(paths.projectRoot, input.outputs ?? [], true)
          const outside = outputRecords
            .filter((record) => !insideScope(paths.projectRoot, record.path, run.scope.paths))
            .map((record) => record.path)
          if (run.skill === "rig-implement" && run.scope.paths.length > 0 && outside.length > 0)
            throw new Error(
              `recorded outputs outside the run scope: ${outside.join(", ")}; the run certifies only its declared paths`,
            )
          if (input.reviewer && detectDrift(paths.projectRoot, run.scope)) {
            throw new Error(
              "source changed after the recorded scope; refresh the scope before recording independent review so the reviewer evidence binds to current bytes",
            )
          }
          let plan = run.plan
          if (input.plan_path) {
            if (run.skill === "rig-implement")
              throw new Error("an implementation plan is frozen at start; begin a replacement run to change it")
            const absolute = resolveInsideProject(paths.projectRoot, input.plan_path)
            if (!existsSync(absolute)) throw new Error(`plan does not exist: ${input.plan_path}`)
            plan = { path: input.plan_path, digest: sha256File(absolute) }
          }
          if (run.skill === "rig-plan" && input.reviewer && !input.plan_digest?.trim()) {
            throw new Error("recording plan review requires plan_digest: the exact bytes the reviewer assessed")
          }
          if (run.skill === "rig-plan" && input.plan_path) {
            assertPlanTree(paths, input.plan_path, "record")
          }
          const updated: RunRecord = {
            ...run,
            ...(plan ? { plan } : {}),
            inputs: [...run.inputs, ...fileRecords(paths.projectRoot, input.inputs ?? [])],
            outputs: [...run.outputs, ...outputRecords],
            limits: [...run.limits, ...(input.limits ?? [])],
            confidence: input.confidence ?? run.confidence,
            ...(input.reviewer
              ? {
                  review: {
                    run_id: run.run_id,
                    reviewer: input.reviewer,
                    source_digest: run.scope.digest,
                    ...(run.skill === "rig-plan" ? { plan_digest: input.plan_digest } : {}),
                  },
                }
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
        let drift = false
        let planDigest: string | undefined
        let sourceDigest: string | undefined
        let actualFindings: readonly Finding[] | undefined
        const bindingProblems: string[] = []
        try {
          assertOwnedPathsSafe(paths, run)
          drift = detectDrift(paths.projectRoot, run.scope)
          if (run.plan) {
            planDigest = sha256File(resolveInsideProject(paths.projectRoot, run.plan.path))
            if (run.skill === "rig-implement") {
              if (!run.plan.run_id) throw new Error("implementation plan provenance is missing")
              const current = reviewedPlan(paths, context.sessionID, run.plan.run_id, run.plan.path)
              if (current.digest !== run.plan.digest) throw new Error("implementation plan binding changed")
            }
            if (run.skill === "rig-plan") {
              const text = readFileSync(resolveInsideProject(paths.projectRoot, run.plan.path), "utf8")
              const lines = text.split(/\r?\n/)
              const start = lines.findIndex((line) => /^##\s+Challenge rounds\s*$/.test(line))
              const end = start < 0 ? -1 : lines.findIndex((line, index) => index > start && /^##\s/.test(line))
              const body =
                start < 0
                  ? ""
                  : lines
                      .slice(start + 1, end < 0 ? undefined : end)
                      .join("\n")
                      .trim()
              if (!body)
                throw new Error("plan has no Challenge rounds section; record the challenged rounds before finalizing")
            }
          }
          if (run.skill === "code-remediate" && run.source_run_id) {
            const inventory = sourceInventory(paths, context.sessionID, run.source_run_id)
            sourceDigest = inventory.run.findings!.digest
            if (
              run.source_findings &&
              json([...run.source_findings.ids].sort()) !==
                json(inventory.document.findings.map((finding) => finding.id).sort())
            ) {
              throw new Error("source findings inventory IDs changed")
            }
          }
          if (run.findings) actualFindings = readFindings(paths, run).findings
        } catch (error) {
          bindingProblems.push(error instanceof Error ? error.message : String(error))
        }
        let measurement: ReturnType<typeof measureTrivial>
        try {
          measurement =
            run.skill === "rig-implement" && run.trivial_plan?.trim() && !run.plan
              ? measureTrivial(paths, run)
              : undefined
        } catch (error) {
          bindingProblems.push(error instanceof Error ? error.message : String(error))
        }
        const loopSignatures =
          run.skill === "rig-implement" && run.loop
            ? [
                ...new Set(
                  readLoop(join(paths.runsRoot, run.skill, run.run_id, "loop.jsonl")).flatMap((record) =>
                    record.findings.map((finding) => finding.signature),
                  ),
                ),
              ]
            : undefined
        const extraLimits: string[] = []
        if (measurement) {
          if (measurement.unmeasured.length > 0)
            extraLimits.push(
              `trivial change not measured for: ${measurement.unmeasured.join(", ")} (binary, oversized or unreadable)`,
            )
          if (measurement.lines > MAX_MINI_PLAN_LINES || measurement.files > MAX_MINI_PLAN_PATHS)
            extraLimits.push(
              `measured trivial change is ${measurement.lines} lines across ${measurement.files} files, above the ${MAX_MINI_PLAN_LINES}-line / ${MAX_MINI_PLAN_PATHS}-file guideline; use rig-plan for work this size`,
            )
        }
        const finalRun = extraLimits.length > 0 ? { ...run, limits: [...run.limits, ...extraLimits] } : run
        const checked = finalizeRun(finalRun, {
          drift,
          plan_digest: planDigest,
          source_findings_digest: sourceDigest,
          findings: actualFindings,
          ...(loopSignatures ? { loop_signatures: loopSignatures } : {}),
        })
        const result = bindingProblems.length
          ? {
              ...checked,
              ok: false,
              status: checked.ok ? ("in_progress" as const) : checked.status,
              problems: [...checked.problems, ...bindingProblems],
            }
          : checked
        if (!result.ok) {
          const failed: RunRecord = { ...run, status: result.status }
          if (run.status === "in_progress") save(failed)
          return {
            content: [
              `FINALIZE FAILED (status: ${result.status})`,
              ...result.problems.map((problem) => `- ${problem}`),
              run.loop && run.loop.stop_reason !== "converged"
                ? "A recorded non-converged loop stop cannot be reopened; a replacement run is needed."
                : drift
                  ? `Source changed after the last recorded scope. Call rig_evidence action=refresh run_id=${run.run_id} with a reason, then rerun gates and required review.`
                  : result.status === "blocked"
                    ? "The challenge loop blocks completion; a replacement run is needed."
                    : "",
            ]
              .filter(Boolean)
              .join("\n"),
            metadata: { run_id: run.run_id, ok: false, status: result.status },
          }
        }
        const completed: RunRecord = { ...finalRun, status: "completed" }
        const reportPath = writeReport(paths, completed, measurement ?? undefined)
        const withReport: RunRecord = { ...completed, report_path: reportPath }
        save(withReport)
        const measured = measurement
          ? `\n\nMeasured change: ${measurement.lines} line(s) across ${measurement.files} file(s) from the start bytes${measurement.unmeasured.length ? `; not measured: ${measurement.unmeasured.join(", ")}` : ""}`
          : ""
        return {
          content: `Run completed.\n${runSummary(withReport)}\nreport: ${reportPath}${measured}\n\n${gateTable(withReport)}`,
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
        let run = resolveRun(paths, context.sessionID, input.run_id)
        requireInProgress(run)
        assertOwnedPathsSafe(paths, run)
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

        if (detectDrift(paths.projectRoot, run.scope)) {
          throw new Error("source changed after the recorded scope; refresh the scope before running gates")
        }
        const approvedScope = run.scope
        const fileArgs = files
          .map((file) => (file.startsWith("-") ? `./${file}` : file))
          .map((arg) => (/^[a-zA-Z0-9_./:-]+$/.test(arg) ? arg : `'${arg.replaceAll("'", `'"'"'`)}'`))
          .join(" ")
        await ctx.permission.assert({
          sessionID: context.sessionID,
          agent: context.agent,
          source: { type: "tool", messageID: context.messageID, id: context.id },
          action: "shell",
          resources: selected.map((spec) => (spec.needsFiles ? `${spec.command} ${fileArgs}` : spec.command)),
          metadata: { gates: selected.map((spec) => spec.id) },
        })

        run = loadRun(paths, run.skill, run.run_id)
        requireInProgress(run)
        if (detectDrift(paths.projectRoot, approvedScope) || run.scope.digest !== approvedScope.digest)
          throw new Error("run scope changed during approval; refresh the scope and request gate approval again")
        const timeout = input.timeout_ms ?? options.gateTimeoutMs
        const samples: GateResult[] = []
        const directory = ensureDir(join(paths.runsRoot, run.skill, run.run_id, "gates"))
        let drift = false
        for (const spec of selected) {
          samples.push(await runGate(spec, paths.projectRoot, files, timeout, directory))
          // Retain intermediate drift even if a later gate restores the original bytes.
          drift = detectDrift(paths.projectRoot, run.scope) || drift
        }
        // A rewriting batch may validate different bytes in each gate. Preserve
        // its samples without certifying any scope, including a later reversion.
        const results = samples.map((result) => ({
          ...result,
          receipt: {
            ...result.receipt,
            log_path: result.log_path,
            ...(drift ? {} : { source_digest: run.scope.digest }),
          },
        }))
        for (const result of results) {
          const sample = `${safeName(result.receipt.id)}-${randomUUID()}`
          writeFileSync(join(directory, `${sample}.json`), `${json(result.receipt)}\n`, {
            encoding: "utf8",
            flag: "wx",
            mode: 0o600,
          })
          appendFileSync(join(directory, "receipts.jsonl"), `${JSON.stringify(result.receipt)}\n`, {
            encoding: "utf8",
            mode: 0o600,
          })
          chmodSync(join(directory, "receipts.jsonl"), 0o600)
        }
        // A rerun after a fix supersedes the earlier receipt for completion, preserving history.
        const gates = [...run.gates, ...results.map((result) => result.receipt)]
        const updated: RunRecord = { ...run, gates }
        save(updated)
        writeJsonAtomic(join(directory, "summary.json"), gates)

        const failed = currentGateReceipts(updated).filter((receipt) => receipt.status === "failed")
        return {
          content: [
            gateTable(updated),
            "",
            drift
              ? "Source changed while gates ran. Receipts do not certify a source digest; refresh the scope and rerun gates."
              : failed.length === 0
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
        "Write or read a validated findings inventory with bucket accounting. Findings are untrusted until verified against source; every finding must end in one terminal bucket. For code-review, correctness is mandatory and axes/aspects must be recorded with coverage.",
      input: FINDINGS_INPUT,
      options: { codemode: true },
      execute: async (raw: unknown, context: any) => {
        const input = raw as FindingsInput
        const run = resolveRun(paths, context.sessionID, input.run_id)
        const directory = join(paths.runsRoot, run.skill, run.run_id)
        const path = join(directory, "findings.json")
        const stored = readJsonIfExists<FindingsDocument>(path)
        // A refreshed scope invalidates the inventory: merging it would re-certify
        // findings and coverage that describe different bytes.
        const existing = stored?.target_digest === run.scope.digest ? stored : undefined

        if (input.action === "read") {
          if (!existing)
            throw new Error(
              stored
                ? `findings artifact describes bytes that no longer match the current scope; rewrite it for the current scope`
                : `no findings artifact for run ${run.run_id}`,
            )
          return { content: json(readFindings(paths, run)), metadata: { run_id: run.run_id } }
        }

        requireInProgress(run)
        if (run.skill === "code-remediate") {
          if (!input.source_run_id || input.source_run_id !== run.source_run_id) {
            throw new Error("remediation write requires the source_run_id bound at start")
          }
          const source = sourceInventory(paths, context.sessionID, input.source_run_id)
          if (source.run.findings!.digest !== run.source_findings?.digest) {
            throw new Error("source findings changed after remediation start; begin a replacement run")
          }
        }
        if (!input.findings)
          throw new Error("write requires findings: an array, empty only when review genuinely found nothing")
        const incoming = validateFindings(input.findings)
        const rawAxes = (input.axes ?? input.aspects ?? existing?.axes ?? []) as readonly string[]
        if (run.skill === "code-review" && rawAxes.length === 0)
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
        if (run.skill === "code-review" && normalizedAxes.length === 0)
          throw new Error("write requires axes/aspects: the review axes that actually ran (include 'correctness')")
        if (run.skill === "code-review" && !normalizedAxes.includes("correctness")) {
          throw new Error(
            "code-review requires correctness axis/aspect (must run correctness review). Add 'correctness' to axes or aspects.",
          )
        }
        const coverage = (input.coverage ?? existing?.coverage ?? []) as readonly string[]
        if (run.skill === "code-review" && coverage.length === 0)
          throw new Error("write requires coverage: the files or areas actually reviewed")
        if (run.skill === "code-review" && normalizedAxes.includes("correctness") && coverage.length === 0) {
          throw new Error(
            "code-review with correctness axis requires non-empty coverage of files/areas actually inspected",
          )
        }
        if (run.skill === "code-review" && !input.reviewer) {
          throw new Error("a code-review findings write requires reviewer: the independent reviewer identity")
        }
        if (run.skill === "rig-implement" && !input.reviewer) {
          throw new Error("a final-challenge findings write requires reviewer: the independent challenger identity")
        }
        if (run.skill === "code-review" && detectDrift(paths.projectRoot, run.scope)) {
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
          ...(run.source_run_id
            ? { source_run_id: run.source_run_id, source_digest: run.source_findings?.digest }
            : {}),
        }
        ensureDir(directory)
        writeJsonAtomic(path, document)

        const summary = findingsSummary(path, merged)
        const updated: RunRecord = {
          ...run,
          findings: summary,
          ...((run.skill === "code-review" || run.skill === "rig-implement") && input.reviewer
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
        if (run.skill !== "rig-challenge" && run.skill !== "rig-implement")
          throw new Error("rig_loop operates on a rig-challenge or rig-implement run")
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
          requireInProgress(run)
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
          requireInProgress(run)
          const last = latestRecord(records)
          if (!last) throw new Error("loop not started; use action=start for the baseline round")
          if (last.stop_reason && run.loop) throw new Error(`loop already finalized with '${last.stop_reason}'`)
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
        requireInProgress(run)
        if (!input.stop_reason) throw new Error(`finalize requires stop_reason: one of ${STOP_REASONS.join(", ")}`)
        const last = latestRecord(records)
        if (!last) throw new Error("loop not started; cannot finalize")
        const reviewer = input.stop_reason === "unavailable-reviewer" ? last.reviewer : requireReviewer(input.reviewer)
        if (last.stop_reason) throw new Error(`loop already finalized with '${last.stop_reason}'`)
        const rows = progress(records)
        const decision = convergence(rows)
        validateStop(input.stop_reason, decision, rows.length, options.maxLoopRounds, input.note)
        if (input.stop_reason !== "drift" && !run.scope.commit && detectDrift(paths.projectRoot, run.scope)) {
          throw new Error(
            "source changed after the recorded scope; refresh and run one more assessed round before finalizing the loop",
          )
        }
        if (input.stop_reason !== "drift" && last.source_digest !== run.scope.digest) {
          throw new Error(
            `the last round assessed ${last.source_digest} but the current scope is ${run.scope.digest}; refresh and append a re-assessment first`,
          )
        }
        const residue = score(last.findings)
        if (input.stop_reason === "converged" && (residue.security > 0 || residue.critical > 0)) {
          throw new Error(
            `cannot finalize with ${residue.security} security and ${residue.critical} critical finding(s) open`,
          )
        }
        if (input.stop_reason === "converged" && run.skill === "rig-implement") {
          const openHigh = last.findings.filter((finding) => finding.severity === "high" && finding.status === "open")
          if (openHigh.length > 0)
            throw new Error(
              `cannot converge an implementation loop with ${openHigh.length} open high finding(s): ${openHigh
                .map((finding) => finding.signature)
                .join(", ")}`,
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
          ...(input.stop_reason !== "unavailable-reviewer"
            ? { review: { run_id: run.run_id, reviewer, source_digest: last.source_digest } }
            : {}),
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
          if (input.confirm !== "Approve always")
            throw new Error("persistent grants require literal Approve always confirmation")
          await ctx.permission.assert({
            sessionID: context.sessionID,
            agent: context.agent,
            source: { type: "tool", messageID: context.messageID, id: context.id },
            action: "rig_git_grant",
            resources: [path],
            metadata: { scope: "local-git" },
          })
          const record = writeGrant(commonDir, input.confirm)
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
            metadata: { status: readGrant(commonDir).status },
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
      execute: async (raw: unknown, context: any) => {
        const input = raw as DispatchInputTool
        if (!WRITE_ROLES.has(input.role) && input.read_only === false) {
          throw new Error(`role ${input.role} is read-only; only rig-builder and rig-creator may receive write scope`)
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
              sessionID: context.sessionID,
            },
            resourcesDir,
          ),
        )
        return {
          content: `role: ${result.role}\nsession: ${result.session_id}\n\n${result.text}\n\nWorkspace checks do not verify ignored untracked trees or submodule working-tree content.${result.scope_violation ? "\nScope violation: stop dependent work and resolve the reported changes before continuing." : ""}`,
          metadata: {
            role: result.role,
            session_id: result.session_id,
            scope_violation: result.scope_violation,
            violations: result.violations,
            workspace_unverified: result.workspace_unverified,
            observed_concurrently: result.observed_concurrently,
          },
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
        const agentDir =
          [dirs.project, dirs.global].find(
            (directory) => installedAgentStatus(directory, PLUGIN_VERSION).length === 0,
          ) ?? (existsSync(dirs.project) ? dirs.project : dirs.global)
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

  const script = await ctx.tool.transform((editor: any) => {
    editor.add({
      name: "rig_script",
      description:
        "Run a declared script file with a fixed interpreter and a receipt: source class, digest, argv, exit code, stream digests and a redacted tail. A run probe lives under the active run's scripts/, a project tool under scripts/, bin/ or tools/, and a plugin helper must match the shipped listing. It proves what ran, not that it is safe.",
      input: SCRIPT_INPUT,
      options: { codemode: true, permission: "rig_script" },
      execute: async (raw: unknown, context: any) => {
        const input = raw as {
          source: ScriptSource
          path: string
          interpreter: ScriptInterpreter
          argv?: string[]
          timeout_ms?: number
        }
        if (!["run", "project", "plugin"].includes(input.source))
          throw new Error(`unknown script source: ${String(input.source)}`)
        if (!(INTERPRETERS as readonly string[]).includes(input.interpreter))
          throw new Error(`unknown interpreter: ${String(input.interpreter)}`)
        const active =
          typeof context.sessionID === "string" ? activeRun(paths.artifactRoot, context.sessionID) : undefined
        const runDirectory = active ? join(paths.runsRoot, safeName(active.skill), safeName(active.runId)) : undefined
        const logDirectory = runDirectory ? join(runDirectory, "scripts") : join(paths.artifactRoot, "scripts")
        const packageRoot = resolve(resourcesDir, "..", "..")
        const pluginFiles =
          input.source === "plugin"
            ? readJsonIfExists<{ files?: Record<string, string> }>(join(resourcesDir, "plugin-files.json"))?.files
            : undefined
        const request = {
          source: input.source,
          path: input.path,
          interpreter: input.interpreter,
          argv: input.argv ?? [],
          timeoutMs: input.timeout_ms ?? options.gateTimeoutMs,
          projectRoot: paths.projectRoot,
          packageRoot,
          runsRoot: paths.runsRoot,
          ...(runDirectory ? { runDirectory } : {}),
          ...(pluginFiles ? { pluginFiles } : {}),
          logDirectory,
        }
        // Resolve and validate before the permission prompt names a path.
        const resolved = resolveScript(request)

        await ctx.permission.assert({
          sessionID: context.sessionID,
          agent: context.agent,
          source: { type: "tool", messageID: context.messageID, id: context.id },
          action: "rig_script",
          resources: [resolved.absolute],
          metadata: { source: input.source, interpreter: input.interpreter },
        })

        const receipt = await executeScript(request)
        const receiptPath = `${receipt.log_path}.json`
        writeJsonAtomic(receiptPath, receipt)
        return {
          content: [input.source === "run" ? "Run probe executed." : "Script executed.", json(receipt)].join("\n"),
          metadata: {
            status: receipt.status,
            exit_code: receipt.exit_code,
            digest: receipt.digest,
            log_path: receipt.log_path,
            receipt_path: receiptPath,
          },
        }
      },
    })
  })
  registrations.push(script)

  const trust = await ctx.tool.transform((editor: any) => {
    editor.add({
      name: "rig_trust",
      description:
        "Read or record the per-commit trust decision for a forked pull request. A record allows that worktree's verification on the host only at the recorded commit, expires, and is never a general allow. Recording requires the user's literal 'Trust this PR' answer.",
      input: TRUST_INPUT,
      options: { codemode: true, permission: "rig_trust" },
      execute: async (raw: unknown, context: any) => {
        const input = raw as {
          action: "approve" | "status"
          commit?: string
          worktree?: string
          ref?: string
          confirm?: string
        }
        if (input.action === "status") {
          return {
            content: json({ words: TRUST_WORDS, records: readTrust(paths.artifactRoot, input.commit) }),
            metadata: { count: readTrust(paths.artifactRoot, input.commit).length },
          }
        }
        if (input.action !== "approve") throw new Error(`unknown trust action: ${String(input.action)}`)
        if (!input.commit || !input.worktree || !input.ref) throw new Error("approve requires commit, worktree and ref")
        if (!isAbsolute(input.worktree) || !isReviewWorktree(input.worktree))
          throw new Error("worktree must be an absolute path under <temp>/opencode/review-*")
        const realWorktree = realpathSync(input.worktree)
        if (input.confirm !== TRUST_WORDS)
          throw new Error(`approve requires the user's literal '${TRUST_WORDS}' answer in confirm`)

        await ctx.permission.assert({
          sessionID: context.sessionID,
          agent: context.agent,
          source: { type: "tool", messageID: context.messageID, id: context.id },
          action: "rig_trust",
          resources: [realWorktree],
          metadata: { commit: input.commit },
        })
        const active =
          typeof context.sessionID === "string" ? activeRun(paths.artifactRoot, context.sessionID) : undefined
        const record = writeTrust(paths.artifactRoot, {
          commit: input.commit,
          worktree: realWorktree,
          ref: input.ref,
          words: input.confirm,
          ...(active ? { runId: active.runId } : {}),
        })
        return {
          content: `Trusted the fetched commit for host verification.\n${json(record)}\nThe record covers this commit only and expires; a different commit asks again.`,
          metadata: { commit: record.commit, expires_at: record.expires_at },
        }
      },
    })
  })
  registrations.push(trust)

  const sandbox = await ctx.tool.transform((editor: any) => {
    editor.add({
      name: "rig_sandbox",
      description:
        "Run a forked pull request's verification in a container: the worktree is copied, the image is pinned, the network is off by default, and the receipt records image, command, exit and log. It does not run host code; without a container runtime it reports the verification as unavailable.",
      input: SANDBOX_INPUT,
      options: { codemode: true, permission: "rig_sandbox" },
      execute: async (raw: unknown, context: any) => {
        const input = raw as {
          worktree: string
          argv: string[]
          timeout_ms?: number
          network?: boolean
          image?: string
        }
        if (!Array.isArray(input.argv) || input.argv.length === 0 || input.argv.some((arg) => typeof arg !== "string"))
          throw new Error("argv must be a nonempty array of strings")
        if (!isAbsolute(input.worktree)) throw new Error("worktree must be an absolute path")
        const image = input.image?.trim() || options.sandboxImage
        if (!image)
          throw new Error(
            "no sandbox image is configured; set sandboxImage to <repository>@sha256:<64 hex> (see docker image inspect --format '{{index .RepoDigests 0}}'), or ask the user to trust this commit with rig_trust",
          )
        if (!isPinnedImage(image))
          throw new Error("sandbox image must be digest-pinned as <repository>@sha256:<64 hex>")
        let realWorktree: string
        try {
          realWorktree = realpathSync(input.worktree)
        } catch {
          // A missing path is not a project root or review worktree; report that
          // boundary instead of leaking a platform-specific ENOENT.
          throw new Error("worktree must be the project root or a review worktree under <temp>/opencode/review-*")
        }
        if (realWorktree !== realpathSync(paths.projectRoot) && !isReviewWorktree(realWorktree))
          throw new Error("worktree must be the project root or a review worktree under <temp>/opencode/review-*")
        const active =
          typeof context.sessionID === "string" ? activeRun(paths.artifactRoot, context.sessionID) : undefined
        const logDirectory = active
          ? join(paths.runsRoot, safeName(active.skill), safeName(active.runId), "sandbox")
          : join(paths.artifactRoot, "sandbox")
        await ctx.permission.assert({
          sessionID: context.sessionID,
          agent: context.agent,
          source: { type: "tool", messageID: context.messageID, id: context.id },
          action: "rig_sandbox",
          resources: [realWorktree],
          metadata: { image },
        })

        if (!detectRuntime()) {
          const receiptPath = join(ensureDir(logDirectory), `unavailable-${randomUUID().slice(0, 8)}.json`)
          const record = {
            status: "unavailable",
            reason: "no container runtime (docker or podman) is available",
            worktree: resolve(input.worktree),
            argv: input.argv,
            image,
            requested_at: new Date().toISOString(),
          }
          writeJsonAtomic(receiptPath, record)
          return {
            content: `No container runtime (docker or podman) is available. The verification did not run; record it as unverified.\n${json(record)}`,
            metadata: { status: "unavailable", receipt_path: receiptPath },
          }
        }

        const receipt = await runInSandbox({
          worktree: input.worktree,
          argv: input.argv,
          image,
          timeoutMs: input.timeout_ms ?? options.gateTimeoutMs,
          ...(input.network === true ? { network: true } : {}),
          logDirectory,
        })
        const receiptPath = `${receipt.log_path}.json`
        writeJsonAtomic(receiptPath, receipt)
        return {
          content: ["Sandbox verification executed.", json(receipt)].join("\n"),
          metadata: {
            status: receipt.status,
            exit_code: receipt.exit_code,
            image: receipt.image,
            log_path: receipt.log_path,
            receipt_path: receiptPath,
          },
        }
      },
    })
  })
  registrations.push(sandbox)

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
