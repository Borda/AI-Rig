/**
 * Shipped command blocks: exact-text provenance for the commands the workflows
 * publish.
 *
 * A block is eligible only when it is literal at run time. Its top-level
 * fragments are normalized the same way at build and run time, and each
 * fragment's SHA-256 names the manifest entry. A compound command is allowed
 * only when every fragment is shipped; the whole-block digest is audit only.
 */

import { isAbsolute, resolve } from "node:path"
import { sha256 } from "./digest.ts"
import { classifyCommand } from "./guardrails.ts"

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

/** Identify quote boundaries shared by normalization and fragment splitting. */
function isShellQuote(char: string): char is "'" | '"' | "`" {
  return char === "'" || char === '"' || char === "`"
}

/** Remove `#` comments outside quotes without touching quoted text. */
function stripComments(text: string): string {
  let out = ""
  let quote: "'" | '"' | "`" | undefined
  let escaped = false
  let wordStart = true
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
      wordStart = false
      continue
    }
    if (quote) {
      out += char
      if (char === quote) quote = undefined
      continue
    }
    if (isShellQuote(char)) {
      quote = char
      out += char
      wordStart = false
      continue
    }
    if (char === "#" && wordStart) {
      index = text.indexOf("\n", index)
      if (index === -1) break
      out += "\n"
      continue
    }
    out += char
    wordStart = /[ \t\n]/.test(char)
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
    if (isShellQuote(char)) quote = char
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

interface ShellFragment {
  readonly text: string
  readonly next?: string
}

const SHELL_SEPARATORS = new Set(["\n", ";", "|", "&"])
const DOUBLE_SEPARATORS = new Set(["&&", "||"])
const DESCRIPTOR_REDIRECTS = new Set(["<&", ">&"])

/** Keep shell separators alongside their fragments so consumers can prove control flow. */
function shellFragments(text: string): ShellFragment[] {
  const fragments: ShellFragment[] = []
  let current = ""
  let quote: "'" | '"' | "`" | undefined
  let escaped = false
  const push = (next?: string) => {
    const fragment = current.trim()
    if (fragment || next !== "\n") fragments.push({ text: fragment, next })
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
    if (isShellQuote(char)) {
      quote = char
      current += char
      continue
    }
    const pair = text.slice(index, index + 2)
    if (DESCRIPTOR_REDIRECTS.has(pair)) {
      current += pair
      index += 1
      continue
    }
    if (SHELL_SEPARATORS.has(char)) {
      const operator = DOUBLE_SEPARATORS.has(pair) ? pair : char
      push(operator)
      index += operator.length - 1
      continue
    }
    current += char
  }
  push()
  return fragments
}

/** Return the text fragments used by the manifest, omitting empty pieces. */
export function splitFragments(text: string): string[] {
  return shellFragments(text)
    .map((fragment) => fragment.text)
    .filter(Boolean)
}

/** Read the only directory-changing form eligible for automatic permission. */
function literalCd(text: string): string | undefined {
  const match = /^cd[ \t]+(?:"([^"\\]+)"|'([^']+)'|([^\s"'\\;&|<>]+))$/.exec(text)
  const target = match?.[1] ?? match?.[2] ?? match?.[3]
  if (!target || !isAbsolute(target) || /[$`\r\n*?[\]{}()<>]/.test(target)) return undefined
  if (target.split(/[\\/]/).some((segment) => segment === "." || segment === "..")) return undefined
  return resolve(target)
}

/** Prove that every command runs from one directory, with an optional leading literal cd. */
function commandLayout(text: string): { fragments: ShellFragment[]; directory?: string } | undefined {
  const fragments = shellFragments(text)
  if (!fragments.at(-1)?.text && [";", "\n"].includes(fragments.at(-2)?.next ?? "")) fragments.pop()
  if (fragments.some((fragment) => !fragment.text || fragment.next === "&")) return undefined
  const first = fragments[0]!
  const directory = first.next === "&&" ? literalCd(first.text) : undefined
  const commands = directory === undefined ? fragments : fragments.slice(1)
  if (commands.some((fragment) => classifyCommand(fragment.text).changesDirectory)) return undefined
  // A fallback branch or independent list item could run even when cd fails.
  if (directory !== undefined && commands.some((fragment) => ![undefined, "&&", "|"].includes(fragment.next)))
    return undefined
  return { fragments, directory }
}

export interface EffectiveCommand {
  readonly directory: string
  readonly command: string
  readonly prefixed: boolean
}

/** Resolve a shell sequence whose directory is established before any command runs. */
export function effectiveCommand(command: string, projectRoot: string): EffectiveCommand | undefined {
  const normalized = normalizeCommand(command)
  if (!normalized.safe) return undefined
  const layout = commandLayout(normalized.text)
  if (!layout) return undefined
  if (layout.directory === undefined) return { directory: projectRoot, command: normalized.text, prefixed: false }
  const rest = layout.fragments
    .slice(1)
    .map(({ text, next }) => (next ? `${text} ${next}` : text))
    .join(" ")
  return { directory: layout.directory, command: rest, prefixed: true }
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

/** Require shipped fragments and a proven directory, checking prefix trust when supplied. */
export function blueprintSafe(
  command: string,
  manifest: BlueprintManifest,
  trustedDirectory?: (directory: string) => boolean,
): boolean {
  const normalized = normalizeCommand(command)
  if (!normalized.safe) return false
  const layout = commandLayout(normalized.text)
  if (!layout || !layout.fragments.every(({ text }) => manifest.entries[sha256(text)]?.kind === "command")) return false
  return layout.directory === undefined || (trustedDirectory?.(layout.directory) ?? true)
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
