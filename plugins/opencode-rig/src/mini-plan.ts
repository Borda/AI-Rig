/**
 * Inline mini-plan parsing for a trivial implementation.
 *
 * A `TRIVIAL` change may start from a prompt instead of a frozen plan, but only
 * with a stated goal, owned paths, acceptance check and feasibility. Those four
 * fields make "start small" a checked decision rather than an assumption, so an
 * unclear or larger change stops before any editing. This module reads text
 * only; it never executes anything.
 */

export interface MiniPlan {
  readonly schema?: number
  readonly goal: string
  readonly owned_paths: readonly string[]
  readonly acceptance_check: string
  readonly feasibility: string
}

export const MINI_PLAN_SCHEMA = 1
/** "Under 3 files" means at most two owned paths. */
export const MAX_MINI_PLAN_PATHS = 2
/** A trivial change stays under 50 changed lines. */
export const MAX_MINI_PLAN_LINES = 50

function fencedJson(text: string): string | undefined {
  const lines = text.split(/\r?\n/)
  let fence: string | undefined
  let open = -1
  let last: string | undefined
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index]!
    if (open < 0) {
      const marker = /^\s*(`{3,}|~{3,})\s*json\s*$/i.exec(line)
      if (marker) {
        fence = marker[1]![0]
        open = index + 1
      }
      continue
    }
    if (new RegExp(`^\\s*${fence}{3,}\\s*$`).test(line)) {
      last = lines.slice(open, index).join("\n")
      fence = undefined
      open = -1
    }
  }
  return last
}

/** Parse the mini-plan's JSON object, preferring the last complete fenced block. */
export function parseMiniPlan(text: string): MiniPlan | undefined {
  const block = fencedJson(text) ?? text
  try {
    const value = JSON.parse(block) as MiniPlan
    return value && typeof value === "object" && !Array.isArray(value) ? value : undefined
  } catch {
    return undefined
  }
}

/** Reject paths that are not ordinary project-relative source files. */
function pathProblem(path: unknown): string | undefined {
  if (typeof path !== "string" || !path.trim()) return "an owned path is empty"
  const value = path.trim()
  if (/[\u0000-\u001f\u007f]/.test(value)) return "an owned path must not contain control characters"
  if (value === "." || value === "./") return "owned path must name a file, not the project root"
  if (value.startsWith("/") || /^[A-Za-z]:[\\/]/.test(value) || value.startsWith("~"))
    return `owned path must be project-relative: ${value}`
  const segments = value.split(/[\\/]/)
  if (segments.includes("..")) return `owned path must not contain '..': ${value}`
  if (segments.some((segment) => segment.toLowerCase() === ".git"))
    return `owned path must not touch VCS internals: ${value}`
  if (/[*?[\]{}]/.test(value)) return `owned path must name a file, not a glob: ${value}`
  if (value.endsWith("/") || value.endsWith("\\")) return `owned path must name a file, not a directory: ${value}`
  return undefined
}

/** Validate a mini-plan and return every problem; an empty list means valid. */
export function miniPlanProblems(text: string | undefined): string[] {
  if (!text?.trim()) return ["a trivial plan needs a mini-plan JSON object"]
  const plan = parseMiniPlan(text)
  if (!plan) return ["mini-plan must be a JSON object, optionally in a ```json fence"]
  const problems: string[] = []
  if (plan.schema !== undefined && plan.schema !== MINI_PLAN_SCHEMA)
    problems.push(`mini-plan schema must be ${MINI_PLAN_SCHEMA}`)
  if (typeof plan.goal !== "string" || !plan.goal.trim()) problems.push("goal is required")
  if (typeof plan.acceptance_check !== "string" || !plan.acceptance_check.trim())
    problems.push("acceptance_check is required")
  if (typeof plan.feasibility !== "string" || !plan.feasibility.trim()) problems.push("feasibility is required")
  if (!Array.isArray(plan.owned_paths) || plan.owned_paths.length === 0) {
    problems.push("owned_paths must list at least one path")
  } else {
    for (const path of plan.owned_paths) {
      const problem = pathProblem(path)
      if (problem) problems.push(problem)
    }
    if (new Set(plan.owned_paths).size !== plan.owned_paths.length) problems.push("owned_paths repeats a path")
    if (plan.owned_paths.length > MAX_MINI_PLAN_PATHS)
      problems.push(
        `owned_paths lists ${plan.owned_paths.length} paths; a trivial change owns at most ${MAX_MINI_PLAN_PATHS} files, otherwise use rig-plan`,
      )
  }
  return problems
}
