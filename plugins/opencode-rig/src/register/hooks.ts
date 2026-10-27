/**
 * Runtime hooks.
 *
 * Only the permission hook can refuse or escalate an action. The journal and
 * context hooks are observational; they run in addition to configured rules and
 * never grant authority.
 */

import { appendFileSync } from "node:fs"
import { join } from "node:path"
import { classifyCommand, decide } from "../guardrails.ts"
import { readGrant } from "../git-grant.ts"
import { redactValue } from "../journal.ts"
import { resolveGitCommonDir, type RigPaths } from "../paths.ts"
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
  const run = activeRun(paths.artifactRoot, sessionID)
  const directory = run ? join(paths.runsRoot, run.skill, run.runId) : paths.artifactRoot
  ensureDir(directory)
  appendFileSync(
    join(directory, "journal.jsonl"),
    `${JSON.stringify({ ts: new Date().toISOString(), ...entry })}\n`,
    "utf8",
  )
}

export async function registerHooks(deps: HookDeps): Promise<HookRegistration[]> {
  const { ctx, paths, options } = deps
  const registrations: HookRegistration[] = []

  registrations.push(
    await ctx.permission.hook("evaluate", (event: any) => {
      if (event.action !== "shell") return
      const resources: string[] = Array.isArray(event.resources) ? event.resources : []
      if (resources.length === 0) return
      const commonDir = resolveGitCommonDir(paths.projectRoot)
      const grant = readGrant(commonDir)

      let escalate: { message: string } | undefined
      for (const command of resources) {
        const decision = decide(classifyCommand(command, options.guardrails), options.guardrails, grant)
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
        journal(paths, event.sessionID, {
          event: "tool.before",
          tool: event.tool,
          call_id: String(event.id),
          input: redactValue(event.input),
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
          ...(event.status === "error" ? { error: redactValue(event.error?.message ?? "unknown error") } : {}),
        })
      }),
    )
  }

  if (options.compactionPreserve) {
    registrations.push(
      await ctx.session.hook("context", (event: any) => {
        const run = activeRun(paths.artifactRoot, event.sessionID)
        if (!run) return
        const text = `Active opencode-rig run: ${run.runId} (${run.skill}). Preserve the run ID, plan digest, open findings, and gate state in any summary.`
        if (Array.isArray(event.system)) event.system.push({ type: "text", text })
      }),
    )
  }

  return registrations
}
