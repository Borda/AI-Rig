/**
 * Inline role dispatch fallback.
 *
 * Native subagents are the preferred route. This fallback creates a child
 * session, injects a bounded role card, waits for completion, and returns the
 * final assistant text. It exists so workflows still run when agent files have
 * not been installed with `opencode-rig init`.
 */

import { readFileSync } from "node:fs"
import { join } from "node:path"

export const ROLE_IDS = [
  "rig-investigator",
  "rig-implementer",
  "rig-reviewer",
  "rig-challenger",
  "rig-verifier",
] as const

export type RoleId = (typeof ROLE_IDS)[number]

export interface DispatchInput {
  readonly role: RoleId
  readonly task: string
  readonly paths?: readonly string[]
  readonly read_only?: boolean
  readonly keep_session?: boolean
}

export interface DispatchResult {
  readonly session_id: string
  readonly role: RoleId
  readonly text: string
}

function buildPrompt(roleCard: string, input: DispatchInput): string {
  const paths = input.paths?.length ? input.paths.map((path) => `- ${path}`).join("\n") : "- none supplied"
  return `${roleCard}

## Task

${input.task}

## Scope

${paths}

## Rules

- Return one final answer with concrete evidence and file references.
- Do not run project gates or commit; the parent owns gates and integration.
- ${input.read_only === false ? "You may edit only the paths listed above." : "Read-only: do not edit files."}
- Do not create further subagents.`
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
    return { session_id: session.id, role: input.role, text }
  } finally {
    if (input.keep_session !== true) {
      await ctx.session.remove({ sessionID: session.id }).catch(() => undefined)
    }
  }
}
