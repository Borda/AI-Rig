/**
 * Inline role dispatch fallback.
 *
 * Native subagents are the preferred route. This fallback creates a child
 * session, injects a bounded role card, waits for completion, and returns the
 * final assistant text. It exists so workflows still run when agent files have
 * not been installed with `opencode-rig init`.
 */

import { readFileSync } from "node:fs"
import { isAbsolute, join, relative, resolve, sep } from "node:path"
import { parseOptions } from "./options.ts"
import { resolvePaths } from "./paths.ts"
import { loadRun, type RunRecord } from "./run.ts"
import { activeRun } from "./state.ts"

export const ROLE_IDS = [
  "rig-investigator",
  "rig-implementer",
  "rig-reviewer",
  "rig-challenger",
  "rig-verifier",
  "rig-scientist",
  "rig-optimizer",
] as const

export type RoleId = (typeof ROLE_IDS)[number]

export interface DispatchInput {
  readonly role: RoleId
  readonly task: string
  readonly paths?: readonly string[]
  readonly read_only?: boolean
  readonly keep_session?: boolean
  /** Session that called the tool; its own active run owns the scope check. */
  readonly sessionID?: string
}

export interface DispatchResult {
  readonly session_id: string
  readonly role: RoleId
  readonly text: string
}

function buildPrompt(roleCard: string, input: DispatchInput): string {
  const paths = input.paths?.length ? input.paths.map((path) => `- ${path}`).join("\n") : "- none supplied"
  const accounting =
    input.read_only === false
      ? "\n- List every file you changed as its own line `changed: <path>`, using project-relative paths; write `changed: none` when nothing changed."
      : ""
  return `${roleCard}

## Task

${input.task}

## Scope

${paths}

## Rules

- Return one final answer with concrete evidence and file references.
- Do not run project gates or commit; the parent owns gates and integration.
- ${input.read_only === false ? "You may edit only the paths listed above." : "Read-only: do not edit files."}
- Do not create further subagents.${accounting}`
}

function lastAssistantText(messages: unknown): string {
  if (!Array.isArray(messages)) return ""
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index] as Record<string, unknown> | undefined
    if (!message) continue
    const info = (message.info ?? message) as Record<string, unknown>
    if (info.role !== "assistant") continue
    const parts = (message.parts ?? info.parts) as unknown
    if (!Array.isArray(parts)) continue
    const text = parts
      .map((part) => (typeof part === "object" && part !== null ? (part as Record<string, unknown>).text : undefined))
      .filter((value): value is string => typeof value === "string" && value.trim() !== "")
      .join("\n")
    if (text) return text
  }
  return ""
}

/** Skills whose run records the scope a plan's verdict table produced. */
const SCOPE_SKILLS = new Set(["rig-plan", "rig-implement"])

/** A `changed: <path>` report line, optionally prefixed by a list bullet. */
const CHANGED_LINE = /^\s*(?:[-*+]\s*)?changed(?:\s+paths?)?\s*[:=]\s*(.+?)\s*$/i

/** A value the child uses to say nothing was changed, not a path. */
const NOTHING_CHANGED = /^(?:none|nothing|no changes?)$/i

/** One unquoted project-relative path: no whitespace, commas, semicolons, or brackets. */
const BARE_PATH = /^[^\s,;()[\]{}]+$/

/** One quoted path, which may contain spaces but never prose punctuation. */
const QUOTED_PATH = /^(["'`])(.+)\1$/

/**
 * Read one accounting value as a path, or reject it as prose.
 *
 * The child is asked for one `changed: <path>` line per file. A line such as
 * `changed: 2 files (src/a.ts, tests/b.test.ts)` is a sentence about paths, not
 * a list, so it is ignored instead of being split into bogus entries that would
 * reject an otherwise successful report.
 */
function accountingPath(value: string): string | undefined {
  const quoted = QUOTED_PATH.exec(value)
  const candidate = quoted ? quoted[2] : value
  if (!candidate) return undefined
  if (quoted) {
    if (/[,;()]/.test(candidate)) return undefined
  } else if (!BARE_PATH.test(candidate)) {
    return undefined
  }
  const path = candidate.trim()
  if (!path || NOTHING_CHANGED.test(path)) return undefined
  return path
}

function reportedChangedPaths(text: string): string[] {
  const reported: string[] = []
  for (const line of text.split(/\r?\n/)) {
    const match = CHANGED_LINE.exec(line)
    const value = match?.[1]
    if (!value) continue
    const path = accountingPath(value)
    if (path) reported.push(path)
  }
  return [...new Set(reported)]
}

/** True when `reportedPath` is the scope path itself or sits below it. */
function resolvesUnder(projectRoot: string, scopePath: string, reportedPath: string): boolean {
  const base = resolve(projectRoot, scopePath)
  const target = resolve(projectRoot, reportedPath)
  const rel = relative(base, target)
  return rel === "" || (rel !== ".." && !rel.startsWith(`..${sep}`) && !isAbsolute(rel))
}

interface RecordedScope {
  readonly projectRoot: string
  readonly paths: readonly string[]
}

/**
 * Scope paths recorded by the calling session's own active run.
 *
 * Inline dispatch only sees the plugin context, so the caller passes its
 * session ID and the check reads that session's run rather than every run in the
 * project. An unknown or non-scoping caller run leaves the result unchecked, as
 * before, but one session's scope can no longer admit a path another session
 * recorded.
 */
function recordedScope(ctx: any, sessionID: string | undefined): RecordedScope | undefined {
  if (!sessionID) return undefined
  try {
    const options = parseOptions(ctx.options)
    const paths = resolvePaths(ctx.location?.directory ?? process.cwd(), options.artifacts)
    const active = activeRun(paths.artifactRoot, sessionID)
    if (!active || !SCOPE_SKILLS.has(active.skill)) return undefined
    const run: RunRecord = loadRun(paths, active.skill, active.runId)
    if (run.status !== "in_progress") return undefined
    // An empty recorded filter means the run covers every changed path.
    return run.scope.paths.length > 0 ? { projectRoot: paths.projectRoot, paths: run.scope.paths } : undefined
  } catch {
    // An unreadable run record cannot prove a path belongs to someone else.
    return undefined
  }
}

/**
 * Refuse a reported change that the calling session's recorded scope does not cover.
 * No active run, no recorded filter, or no reported path leaves the result as is.
 */
function assertReportedInScope(ctx: any, text: string, sessionID: string | undefined): void {
  const reported = reportedChangedPaths(text)
  if (reported.length === 0) return
  const scope = recordedScope(ctx, sessionID)
  if (!scope) return
  for (const path of reported) {
    if (scope.paths.some((candidate) => resolvesUnder(scope.projectRoot, candidate, path))) continue
    throw new Error(`changed path outside the recorded run scope: ${path}; recorded scope: ${scope.paths.join(", ")}`)
  }
}

export async function dispatchRole(ctx: any, input: DispatchInput, resourcesDir: string): Promise<DispatchResult> {
  if (!ROLE_IDS.includes(input.role)) throw new Error(`unknown role: ${String(input.role)}`)
  if (!input.task.trim()) throw new Error("dispatch task must not be empty")

  const roleCard = readFileSync(join(resourcesDir, "roles", `${input.role}.md`), "utf8")
  const session = await ctx.session.create({ title: `opencode-rig ${input.role}` })
  try {
    await ctx.session.prompt({ sessionID: session.id, text: buildPrompt(roleCard, input) })
    await ctx.session.wait({ sessionID: session.id })
    const messages = await ctx.session.context({ sessionID: session.id })
    const text = lastAssistantText(messages)
    if (!text.trim()) throw new Error("child session produced no final assistant text")
    if (input.role === "rig-implementer") assertReportedInScope(ctx, text, input.sessionID)
    return { session_id: session.id, role: input.role, text }
  } finally {
    if (input.keep_session !== true) {
      await ctx.session.remove({ sessionID: session.id }).catch(() => undefined)
    }
  }
}
