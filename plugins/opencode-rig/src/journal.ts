/**
 * Tool-call journaling.
 *
 * Journals are evidence of activity, not a transcript: no file contents, no
 * environment dumps, and common credential shapes are redacted before writing.
 */

const SECRET_PATTERNS: readonly RegExp[] = [
  /gh[pousr]_[A-Za-z0-9]{20,}/g,
  /github_pat_[A-Za-z0-9_]{20,}/g,
  /(?<![A-Za-z0-9])sk-(?:(?:ant-|proj-)[A-Za-z0-9_-]{20,}|[A-Za-z0-9]{20,})/g,
  /xox[a-z]-[A-Za-z0-9-]{10,}/gi,
  /npm_[A-Za-z0-9]{10,}/g,
  /glpat-[A-Za-z0-9_-]{10,}/g,
  /AKIA[0-9A-Z]{16}/g,
  /\b(?:[Bb]earer|Basic)\s+(?=[A-Za-z0-9._~+/-]*[0-9._~+/-=])[A-Za-z0-9._~+/-]{8,}={0,2}/g,
  /-----BEGIN ([A-Z ]*PRIVATE KEY)-----[\s\S]*?(?:-----END \1-----|$)/g,
]

const SENSITIVE_KEY =
  /^(?:token|apiKey|clientSecret|privateKey)$|(?:^|[_-])(?:password|passwd|secret|api[_-]key|authorization|private[_-]key|credential|cookie|session)(?:$|[_-])|(?:^|[_-])(?:access|refresh|auth|bearer|session|id|github)[_-]token$/i
const SECRET_ASSIGNMENT =
  /(?<![\w/.-])(["']?[A-Za-z][A-Za-z0-9_-]*["']?)\s*[:=]\s*(?:"(?:\\.|[^"\\])*(?:"|$)|'(?:\\.|[^'\\])*(?:'|$)|[^\s&,}\]]+)/g

export function redact(value: string): string {
  let output = value
    .slice(0, 2_000)
    .replace(/\b((?:authorization|proxy-authorization|cookie|set-cookie)\s*:)\s*[^\r\n]*/gi, "$1 [redacted]")
    .replace(SECRET_ASSIGNMENT, (match, key: string) =>
      SENSITIVE_KEY.test(key.replace(/^["']|["']$/g, "")) ? `${key}=[redacted]` : match,
    )
  if (value.length > 2_000) {
    // A cut token or authorization value must not leak its remaining visible prefix.
    output = output.replace(
      /(?<![A-Za-z0-9])(?:gh[pousr]_|github_pat_|sk-(?:ant-|proj-)?|xox[a-z]-|npm_|glpat-|AKIA|(?:[Bb]earer|Basic)\s+)[A-Za-z0-9_.~+/-]*={0,2}$/g,
      "[redacted]",
    )
  }
  for (const pattern of SECRET_PATTERNS) output = output.replace(pattern, "[redacted]")
  return output
}

export function redactValue(value: unknown, depth = 0): unknown {
  if (depth > 4) return "[depth-limit]"
  if (typeof value === "string") return redact(value)
  if (Array.isArray(value)) return value.slice(0, 50).map((entry) => redactValue(entry, depth + 1))
  if (typeof value === "object" && value !== null) {
    const out: Record<string, unknown> = {}
    for (const [key, entry] of Object.entries(value).slice(0, 50)) {
      const redacted = redact(key)
      let unique = redacted
      let suffix = 2
      while (Object.hasOwn(out, unique)) unique = `${redacted}#${suffix++}`
      Object.defineProperty(out, unique, {
        value: SENSITIVE_KEY.test(key.slice(0, 2_000)) ? "[redacted]" : redactValue(entry, depth + 1),
        enumerable: true,
        configurable: true,
        writable: true,
      })
    }
    return out
  }
  return value
}
