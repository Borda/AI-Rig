/**
 * Runtime hooks.
 *
 * Only the permission hook can refuse or escalate an action. The journal and
 * context hooks are observational; they run in addition to configured rules and
 * never grant authority.
 */

import { appendFileSync, chmodSync, realpathSync } from "node:fs"
import { isAbsolute, join, relative, sep } from "node:path"
import { classifyCommand, decide, dirtyPaths, type Classification, type DiscardContext } from "../guardrails.ts"
import { blueprintSafe, splitFragments, type BlueprintManifest } from "../blueprint.ts"
import { designedWriteSafe } from "../designed-writes.ts"
import { inspectionSequence, type InspectionSequence } from "../inspection.ts"
import { effectiveCommand, isOwnWorktree, isReviewWorktree, trustFor } from "../trust.ts"
import { readGrant } from "../git-grant.ts"
import { sha256 } from "../digest.ts"
import { redact, redactValue } from "../journal.ts"
import { resolveGitCommonDir, type RigPaths } from "../paths.ts"
import { loadRun } from "../run.ts"
import { activeRun } from "../state.ts"
import type { RigOptions } from "../options.ts"
import { ensureDir, readJsonIfExists } from "../util/fsx.ts"
import { checkoutMayDiscard } from "../util/git.ts"

export interface HookRegistration {
  dispose(): Promise<void>
}

export interface HookDeps {
  readonly ctx: any
  readonly paths: RigPaths
  readonly options: RigOptions
  readonly resourcesDir: string
}

function journal(paths: RigPaths, sessionID: string, entry: Record<string, unknown>): void {
  let directory = paths.artifactRoot
  try {
    const run = activeRun(paths.artifactRoot, sessionID)
    if (run) directory = join(paths.runsRoot, run.skill, run.runId)
  } catch {
    // Keep observations available while the active-run state needs repair.
  }
  ensureDir(directory)
  const path = join(directory, "journal.jsonl")
  appendFileSync(path, `${JSON.stringify({ ts: new Date().toISOString(), ...entry })}\n`, {
    encoding: "utf8",
    mode: 0o600,
  })
  chmodSync(path, 0o600)
}

/** Check containment using real paths before looking up a worktree's Git identity. */
function projectContains(projectRoot: string, directory: string): boolean {
  const rel = relative(realpathSync(projectRoot), realpathSync(directory))
  return rel === "" || (!rel.startsWith("..") && !isAbsolute(rel))
}

/** Permit host verification only in the checkout, an own worktree, or an approved review. */
function trustedRoot(paths: RigPaths, directory: string): boolean {
  try {
    return (
      projectContains(paths.projectRoot, directory) ||
      isOwnWorktree(paths.projectRoot, directory) ||
      trustFor(paths.artifactRoot, directory).status === "trusted"
    )
  } catch {
    return false
  }
}

/** Allow inspection in known review directories without granting verification. */
function allowedDirectory(projectRoot: string, directory: string): boolean {
  try {
    return (
      projectContains(projectRoot, directory) || isReviewWorktree(directory) || isOwnWorktree(projectRoot, directory)
    )
  } catch {
    return false
  }
}

/** Resolve otherwise ambiguous checkout operands using tracked worktree changes. */
function classifyResource(command: string, mode: RigOptions["guardrails"], projectRoot: string): Classification {
  const classification = classifyCommand(command, mode)
  if (!["other", "read-only-git"].includes(classification.kind) || !command.includes("checkout")) return classification
  const effective = effectiveCommand(command, projectRoot)
  if (
    effective &&
    splitFragments(effective.command).some((fragment) => checkoutMayDiscard(fragment, effective.directory))
  )
    return { ...classification, kind: "destructive-local", reason: "destructive local operation" }
  return classification
}

/** Prove each resource using the same directory model for inspection and sandbox enforcement. */
function inspectResources(resources: readonly string[], paths: RigPaths, mode: RigOptions["guardrails"]) {
  return resources.map((command) =>
    inspectionSequence(command, paths.projectRoot, mode, (directory) => allowedDirectory(paths.projectRoot, directory)),
  )
}

/** Require positive proof that a host event is inspection or a designed Git write. */
function permittedInSandboxMode(sequences: readonly InspectionSequence[], designedWrite: boolean): boolean {
  return designedWrite || (sequences.length > 0 && sequences.every(({ ok, kind }) => ok && kind === "inspection"))
}

/** Read the host's extracted commands without assuming their runtime shape. */
function commandResources(value: unknown): string[] | undefined {
  if (!Array.isArray(value)) return undefined
  const resources: unknown[] = Array.from(value)
  return resources.every((item) => typeof item === "string") ? resources : undefined
}

/** Prove the narrow Git-write exception for preparing reviews or staging owned paths. */
function isDesignedWriteEvent(action: string, resources: readonly string[] | undefined, deps: HookDeps): boolean {
  return Boolean(
    action === "shell" &&
    deps.options.designedWrites &&
    resources?.length &&
    resources.every((command) => designedWriteSafe(command, { projectRoot: deps.paths.projectRoot }).ok),
  )
}

/** Prove exact shipped text without granting the final permission effect. */
function isShippedBlock(
  action: string,
  resources: readonly string[] | undefined,
  blueprint: BlueprintManifest | undefined,
  deps: HookDeps,
): boolean {
  return Boolean(
    action === "shell" &&
    deps.options.blueprint &&
    blueprint &&
    resources?.length &&
    resources.every((command) => blueprintSafe(command, blueprint, (directory) => trustedRoot(deps.paths, directory))),
  )
}

/** Allow proven shell inspection and verification in trusted worktrees. */
function allowInspection(event: any, sequences: readonly InspectionSequence[], deps: HookDeps): boolean {
  if (event.action !== "shell" || !deps.options.inspection || !sequences.every((sequence) => sequence.ok)) return false
  const verifications = sequences.flatMap((sequence) => sequence.verifications)
  if (!verifications.every((directory) => trustedRoot(deps.paths, directory))) return false
  event.effect = "allow"
  event.message = verifications.length
    ? "opencode-rig: verification in a trusted checkout or worktree."
    : "opencode-rig: read-only inspection (positive model)."
  return true
}

/** Escalate commands that need confirmation under the configured Git guardrails. */
function escalateGuardrails(event: any, resources: string[], { options, paths }: HookDeps): void {
  const classifications = resources.map((command) => classifyResource(command, options.guardrails, paths.projectRoot))
  const changedDirectory =
    classifications.some((classification) => classification.changesDirectory) ||
    resources.some((command) => {
      const normalized = command.replace(/\\\r?\n/g, "").replace(/['"\\]/g, "")
      return /(?:^|[\s;&|()])(?:cd|chdir|pushd|popd|set-location|push-location|pop-location)(?=[\s;&|()]|$)/i.test(
        normalized,
      )
    })
  let grant: ReturnType<typeof readGrant> = { status: "absent" }
  let grantResolved = changedDirectory

  let discard: DiscardContext | undefined
  let discardResolved = false
  const discardContext = (kind: string): DiscardContext | undefined => {
    if (kind !== "destructive-local" && kind !== "history-rewrite") return undefined
    if (discardResolved) return discard
    discardResolved = true
    // Resolve the run scope first: an unreadable scope already suppresses every
    // path claim, so the bounded Git lookup would only block the host for nothing.
    let scopePaths: readonly string[] | undefined
    try {
      const active = typeof event.sessionID === "string" ? activeRun(paths.artifactRoot, event.sessionID) : undefined
      if (active) {
        scopePaths = loadRun(paths, active.skill, active.runId).scope.paths
      }
    } catch {
      // An unreadable run scope cannot prove a path belongs to someone else.
      discard = { dirtyPaths: [], scopeUnknown: true }
      return discard
    }
    const artifactPath = relative(paths.projectRoot, paths.artifactRoot).split(sep).join("/")
    const dirty = dirtyPaths(paths.projectRoot, artifactPath ? [artifactPath] : [])
    if (dirty === undefined) return undefined
    discard = scopePaths === undefined ? { dirtyPaths: dirty } : { dirtyPaths: dirty, scopePaths }
    return discard
  }

  let escalate: { message: string } | undefined
  for (const classification of classifications) {
    if (classification.kind === "plain-commit" && !grantResolved) {
      grantResolved = true
      try {
        grant = readGrant(resolveGitCommonDir(paths.projectRoot))
      } catch {
        grant = { status: "invalid", reason: "unable to read local Git grant" }
      }
    }
    const decision = decide(classification, options.guardrails, grant, discardContext(classification.kind))
    // Only escalation is allowed: a configured `ask` never becomes `allow`.
    if (decision?.effect === "ask") escalate = { message: decision.message }
  }
  if (!escalate) return
  event.effect = "ask"
  event.message = escalate.message
}

/** Apply the optional inspection allow and Git guardrails after execution policy. */
function evaluateGuardrails(
  event: any,
  resources: string[] | undefined,
  sandboxSequences: readonly InspectionSequence[] | undefined,
  deps: HookDeps,
): void {
  if (deps.options.guardrails === "off") return
  if (resources === undefined) {
    event.effect = "ask"
    event.message = "opencode-rig: shell resources must be strings; confirmation required."
    return
  }
  if (resources.length === 0) return
  const sequences = sandboxSequences ?? inspectResources(resources, deps.paths, deps.options.guardrails)
  if (allowInspection(event, sequences, deps)) return
  escalateGuardrails(event, resources, deps)
}

export async function registerHooks(deps: HookDeps): Promise<HookRegistration[]> {
  const { ctx, paths, options, resourcesDir } = deps
  const registrations: HookRegistration[] = []
  const blueprint = readJsonIfExists<BlueprintManifest>(join(resourcesDir, "blueprint-manifest.json"))
  const shippedBlueprint = blueprint?.schema === 1 && blueprint.entries ? blueprint : undefined

  registrations.push(
    await ctx.permission.hook("evaluate", (event: any) => {
      // v2 reports shell commands as `shell` and Code Mode commands as
      // `execute`; both carry extractable subcommands that need classifying.
      if (event.action !== "shell" && event.action !== "execute") return
      if (event.effect === "deny") return
      const resources = commandResources(event.resources)
      const designedWrite = isDesignedWriteEvent(event.action, resources, deps)
      const sandboxSequences =
        options.execution === "all-sandbox" ? inspectResources(resources ?? [], paths, options.guardrails) : undefined
      if (sandboxSequences && !permittedInSandboxMode(sandboxSequences, designedWrite)) {
        event.effect = "deny"
        event.message =
          "opencode-rig: all-sandbox requires rig_sandbox for verification. Host commands must be proven inspection or designed Git writes."
        return
      }
      // A shipped command block is exact-text provenance; the always-ask classes
      // still run below, so this only sets the effect and continues.
      if (isShippedBlock(event.action, resources, shippedBlueprint, deps)) {
        event.effect = "allow"
        event.message = "opencode-rig: shipped command block (exact text)."
      }
      if (designedWrite) {
        event.effect = "allow"
        event.message = "opencode-rig: designed Git write (review fetch, review worktree, owned staging)."
        return
      }
      evaluateGuardrails(event, resources, sandboxSequences, deps)
    }),
  )

  if (options.journal) {
    registrations.push(
      await ctx.tool.hook("execute.before", (event: any) => {
        const input = event.input
        const plainObject =
          typeof input === "object" && input !== null && [Object.prototype, null].includes(Object.getPrototypeOf(input))
        const summary = plainObject
          ? Object.entries(input)
              .slice(0, 50)
              .map(([key, value]) => {
                const keyPolicy = redactValue({ [key]: null }) as Record<string, unknown>
                return Object.values(keyPolicy)[0] === "[redacted]"
                  ? { key: redact(key), value: "[redacted]" }
                  : { key: redact(key), sha256: sha256(JSON.stringify(value) ?? "undefined") }
              })
          : { sha256: sha256(JSON.stringify(input) ?? "undefined") }
        journal(paths, event.sessionID, {
          event: "tool.before",
          tool: event.tool,
          call_id: String(event.id),
          input: summary,
        })
      }),
    )
    registrations.push(
      await ctx.tool.hook("execute.after", (event: any) => {
        journal(paths, event.sessionID, {
          event: "tool.after",
          tool: event.tool,
          call_id: String(event.id),
          status: event.status,
          ...(event.status === "error"
            ? { error_sha256: sha256(String(event.error?.message ?? "unknown error")) }
            : {}),
        })
      }),
    )
  }

  if (options.compactionPreserve) {
    registrations.push(
      await ctx.session.hook("context", (event: any) => {
        let run
        try {
          run = activeRun(paths.artifactRoot, event.sessionID)
        } catch {
          return
        }
        if (!run) return
        const text = `Active opencode-rig run: ${run.runId} (${run.skill}). Preserve the run ID, plan digest, open findings, and gate state in any summary.`
        if (Array.isArray(event.system)) event.system.push({ type: "text", text })
      }),
    )
  }

  return registrations
}
