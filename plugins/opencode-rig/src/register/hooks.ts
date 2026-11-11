/**
 * Runtime hooks.
 *
 * Only the permission hook can refuse or escalate an action. The journal and
 * context hooks are observational; they run in addition to configured rules and
 * never grant authority.
 */

import { appendFileSync, chmodSync } from "node:fs"
import { join, relative, sep } from "node:path"
import { classifyCommand, decide, dirtyPaths, type DiscardContext } from "../guardrails.ts"
import { readGrant } from "../git-grant.ts"
import { sha256 } from "../digest.ts"
import { redact, redactValue } from "../journal.ts"
import { resolveGitCommonDir, type RigPaths } from "../paths.ts"
import { loadRun } from "../run.ts"
import { activeRun } from "../state.ts"
import type { RigOptions } from "../options.ts"
import { ensureDir } from "../util/fsx.ts"

export interface HookRegistration {
  dispose(): Promise<void>
}

export interface HookDeps {
  readonly ctx: any
  readonly paths: RigPaths
  readonly options: RigOptions
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

export async function registerHooks(deps: HookDeps): Promise<HookRegistration[]> {
  const { ctx, paths, options } = deps
  const registrations: HookRegistration[] = []

  registrations.push(
    await ctx.permission.hook("evaluate", (event: any) => {
      // v2 reports shell commands as `shell` and Code Mode commands as
      // `execute`; both carry extractable subcommands that need classifying.
      if (event.action !== "shell" && event.action !== "execute") return
      if (options.guardrails === "off") return
      if (!Array.isArray(event.resources) || !event.resources.every((value: unknown) => typeof value === "string")) {
        event.effect = "ask"
        event.message = "opencode-rig: shell resources must be strings; confirmation required."
        return
      }
      const resources: string[] = event.resources
      if (resources.length === 0) return
      const classifications = resources.map((command) => classifyCommand(command, options.guardrails))
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
        if (options.guardrails === "off") return undefined
        if (discardResolved) return discard
        discardResolved = true
        // Resolve the run scope first: an unreadable scope already suppresses every
        // path claim, so the bounded Git lookup would only block the host for nothing.
        let scopePaths: readonly string[] | undefined
        try {
          const active =
            typeof event.sessionID === "string" ? activeRun(paths.artifactRoot, event.sessionID) : undefined
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
