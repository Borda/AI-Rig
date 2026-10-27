/**
 * Tool-call journaling.
 *
 * Journals are evidence of activity, not a transcript: no file contents, no
 * environment dumps, and common credential shapes are redacted before writing.
 */

const SECRET_PATTERNS: readonly RegExp[] = [
  /gh[pousr]_[A-Za-z0-9]{20,}/g,
  /github_pat_[A-Za-z0-9_]{20,}/g,
  /sk-[A-Za-z0-9]{20,}/g,
  /AKIA[0-9A-Z]{16}/g,
  /(?<=Bearer\s)[A-Za-z0-9._-]{20,}/gi,
  /(?<=(?:token|password|secret|api[_-]?key)=)[^\s&"']+/gi,
]

export function redact(value: string): string {
  let output = value
  for (const pattern of SECRET_PATTERNS) output = output.replace(pattern, "[redacted]")
  return output
}

export function redactValue(value: unknown, depth = 0): unknown {
  if (depth > 4) return "[depth-limit]"
  if (typeof value === "string") return redact(value).slice(0, 2_000)
  if (Array.isArray(value)) return value.slice(0, 50).map((entry) => redactValue(entry, depth + 1))
  if (typeof value === "object" && value !== null) {
    const out: Record<string, unknown> = {}
    for (const [key, entry] of Object.entries(value).slice(0, 50)) out[key] = redactValue(entry, depth + 1)
    return out
  }
  return value
}
