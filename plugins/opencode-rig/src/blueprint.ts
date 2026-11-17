/**
 * Shipped command blocks: exact-text provenance for the commands the workflows
 * publish.
 *
 * A block is eligible only when it is literal at run time. Its top-level
 * fragments are normalized the same way at build and run time, and each
 * fragment's SHA-256 names the manifest entry. A compound command is allowed
 * only when every fragment is shipped; the whole-block digest is audit only.
 */

import { sha256 } from "./digest.ts"

export interface BlueprintEntry {
  readonly kind: "command" | "block"
  /** Representative source; an identical command in another block shares this entry. */
  readonly src: string
}

export interface BlueprintManifest {
  readonly schema: 1
  readonly plugin: string
  readonly entries: Readonly<Record<string, BlueprintEntry>>
  readonly skipped: readonly { src: string; reason: string }[]
}

export interface NormalizedCommand {
  readonly text: string
  readonly safe: boolean
  readonly reason?: string
}

/** Remove `#` comments outside quotes without touching quoted text. */
function stripComments(text: string): string {
  let out = ""
  let quote: "'" | '"' | "`" | undefined
  let escaped = false
  for (let index = 0; index < text.length; index += 1) {
    const char = text[index]!
    if (escaped) {
      out += char
      escaped = false
      continue
    }
    if (char === "\\" && quote !== "'") {
      out += char
      escaped = true
      continue
    }
    if (quote) {
      out += char
      if (char === quote) quote = undefined
      continue
    }
    if (char === "'" || char === '"' || char === "`") {
      quote = char
      out += char
      continue
    }
    if (char === "#" && (index === 0 || /[ \t\n]/.test(text[index - 1] ?? ""))) {
      while (index < text.length && text[index] !== "\n") index += 1
      if (index < text.length) out += "\n"
      continue
    }
    out += char
  }
  return out
}

function quoteBalanced(text: string): boolean {
  let quote: "'" | '"' | "`" | undefined
  let escaped = false
  for (const char of text) {
    if (escaped) {
      escaped = false
      continue
    }
    if (char === "\\" && quote !== "'") {
      escaped = true
      continue
    }
    if (quote) {
      if (char === quote) quote = undefined
      continue
    }
    if (char === "'" || char === '"' || char === "`") quote = char
  }
  return quote === undefined
}

/** Normalize one block: CRLF, right-strip, comments, blank runs; no rewrites. */
export function normalizeCommand(text: string): NormalizedCommand {
  // Bash splits only on ASCII blanks and newlines; any other Unicode space or a
  // control byte would let a shipped prefix hide a second command.
  if (
    /[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F\u0085\u00A0\u1680\u2000-\u200A\u2028\u2029\u202F\u205F\u3000\uFEFF]/.test(
      text,
    )
  )
    return { text, safe: false, reason: "non-ASCII whitespace or control character" }
  const stripped = stripComments(text.replace(/\r\n?/g, "\n"))
  const lines = stripped.split("\n").map((line) => line.replace(/[ \t]+$/, ""))
  const normalized = lines
    .join("\n")
    .replace(/\n{3,}/g, "\n\n")
    .replace(/^[ \t\n]+|[ \t\n]+$/g, "")
  if (!normalized) return { text: normalized, safe: false, reason: "empty block" }
  if (normalized.includes("<<")) return { text: normalized, safe: false, reason: "heredoc" }
  if (/\\\n/.test(normalized)) return { text: normalized, safe: false, reason: "line continuation" }
  if (!quoteBalanced(normalized)) return { text: normalized, safe: false, reason: "unterminated quote" }
  return { text: normalized, safe: true }
}

/** Split on top-level `;`, `&&`, `||`, `|` and newline, respecting quotes. */
export function splitFragments(text: string): string[] {
  const fragments: string[] = []
  let current = ""
  let quote: "'" | '"' | "`" | undefined
  let escaped = false
  const push = () => {
    const fragment = current.trim()
    if (fragment) fragments.push(fragment)
    current = ""
  }
  for (let index = 0; index < text.length; index += 1) {
    const char = text[index]!
    if (escaped) {
      current += char
      escaped = false
      continue
    }
    if (char === "\\" && quote !== "'") {
      current += char
      escaped = true
      continue
    }
    if (quote) {
      current += char
      if (char === quote) quote = undefined
      continue
    }
    if (char === "'" || char === '"' || char === "`") {
      quote = char
      current += char
      continue
    }
    if (char === "\n" || char === ";") {
      push()
      continue
    }
    if (char === "&" && text[index + 1] === "&") {
      push()
      index += 1
      continue
    }
    if (char === "|") {
      push()
      if (text[index + 1] === "|") index += 1
      continue
    }
    current += char
  }
  push()
  return fragments
}

export interface AnalyzedBlock {
  readonly safe: boolean
  readonly text: string
  readonly fragments: readonly string[]
  readonly reason?: string
}

/** Analyze a published block for eligibility and its fragments. */
export function analyzeBlock(text: string): AnalyzedBlock {
  const normalized = normalizeCommand(text)
  if (!normalized.safe) return { safe: false, text: normalized.text, fragments: [], reason: normalized.reason }
  const fragments = splitFragments(normalized.text)
  if (fragments.length === 0) return { safe: false, text: normalized.text, fragments: [], reason: "empty block" }
  // A literal block cannot contain an expansion, substitution or redirect: the
  // shell would change the text this manifest claims to prove.
  if (fragments.some((fragment) => /[<>$`]/.test(fragment)))
    return { safe: false, text: normalized.text, fragments, reason: "runtime value or redirect" }
  return { safe: true, text: normalized.text, fragments }
}

/** True only when every top-level fragment is a shipped command entry. */
export function blueprintSafe(command: string, manifest: BlueprintManifest): boolean {
  const normalized = normalizeCommand(command)
  if (!normalized.safe) return false
  const fragments = splitFragments(normalized.text)
  if (fragments.length === 0) return false
  return fragments.every((fragment) => manifest.entries[sha256(fragment)]?.kind === "command")
}

/** Build a manifest from every analyzed block; pure so the harness can diff it. */
export function buildManifest(plugin: string, blocks: readonly { src: string; text: string }[]): BlueprintManifest {
  const entries: Record<string, BlueprintEntry> = {}
  const skipped: { src: string; reason: string }[] = []
  for (const block of blocks) {
    const analyzed = analyzeBlock(block.text)
    if (!analyzed.safe) {
      skipped.push({ src: block.src, reason: analyzed.reason ?? "ineligible" })
      continue
    }
    // A single-command block has one digest; the command view is the one the
    // runtime looks up, so it wins over the audit-only block view.
    for (const fragment of analyzed.fragments) {
      const digest = sha256(fragment)
      if (entries[digest]?.kind !== "command") entries[digest] = { kind: "command", src: block.src }
    }
    const blockDigest = sha256(analyzed.text)
    if (!entries[blockDigest]) entries[blockDigest] = { kind: "block", src: block.src }
  }
  return { schema: 1, plugin, entries, skipped }
}
