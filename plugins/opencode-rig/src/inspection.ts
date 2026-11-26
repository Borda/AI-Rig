/**
 * Positive read-only inspection model for role agents.
 *
 * The guardrail hook may turn the role's `shell: *` ask into allow only for a
 * command this module positively proves to be inspection or a configured
 * verification form, with no write or exec channel. It is stateless and reads
 * no configuration; every check is a function of the command text.
 */

import { realpathSync } from "node:fs"
import { isAbsolute, relative, resolve } from "node:path"
import { normalizeCommand, splitFragments } from "./blueprint.ts"
import { classifyCommand, programIdentity } from "./guardrails.ts"
import type { GuardrailMode } from "./options.ts"
import { effectiveCommand } from "./trust.ts"

export interface InspectionVerdict {
  readonly ok: boolean
  readonly reason?: string
  /** A read-only inspection or a command that runs project code. */
  readonly kind?: "inspection" | "verification"
}

/** Read-only programs already modelled by the grammar; their options are checked there. */
const READERS = new Set(
  (
    "cat head tail grep egrep fgrep rgrep rg sed sort find ls wc cut tr comm cmp diff nl basename dirname " +
    "realpath stat file du df date pwd jq shasum sha256sum sha384sum sha512sum md5sum sha1sum cksum uniq " +
    "echo printf rev tac fold fmt expand unexpand column paste join tsort od hexdump strings base64 xxd zcat"
  ).split(" "),
)

const ASSIGNMENTS = /^(?:[A-Za-z_][A-Za-z0-9_]*=(?:"[^"]*"|'[^']*'|\S+)\s+)+/
const VENV_PYTEST = /^(?:\.venv\/bin\/python[0-9.]*|\.venv\/Scripts\/python[0-9.]*)\s+-m\s+pytest\b/
const SAFE_REDIRECT = /^\/dev\/(?:null|stdout|stderr)$/
const JQ_AMBIENT =
  /\b(?:env|input|inputs|input_filename|debug|stderr|halt|halt_error|system|include|import|module)\b|\$ENV|(?:^|\s)(?:-f|--from-file|--rawfile|--slurpfile)\b|-f\S/

/** A redirect other than the supported /dev sinks can read or write a host path. */
function unsafeRedirect(text: string): boolean {
  for (const match of text.matchAll(/(\d*)(>>?|<<?|>&|<&)([^\s]+)/g)) {
    const target = match[3]!
    // A stream duplication such as `2>&1` is not a path.
    if (match[2]!.endsWith("&") && /^\d+$/.test(target)) continue
    if (!SAFE_REDIRECT.test(target)) return true
  }
  return false
}

/** Decode a shell word's quotes and return the operand value, if it names one. */
function operandPath(token: string): string | undefined {
  const raw = token.replace(/['"]/g, "")
  // A Windows drive or UNC path is a host path; keep the marker for the check.
  if (/^[A-Za-z]:[\\/]/.test(raw) || /^\\\\/.test(raw)) return raw
  // A shell strips one backslash from an escaped operand; decode the same way
  // so `\/etc/passwd` cannot look relative.
  const clean = raw.replace(/\\(.)/g, "$1")
  if (clean.startsWith("--")) {
    const equals = clean.indexOf("=")
    return equals >= 0 ? clean.slice(equals + 1) : undefined
  }
  if (clean.startsWith("-")) {
    // An attached short-option value (`-f../x`) is the text after the option cluster.
    const match = /^-[A-Za-z0-9]+(.*)$/.exec(clean)
    const rest = match?.[1]
    return rest ? rest : undefined
  }
  return clean
}

/** Indices that are pattern expressions, not file operands. */
function skippedOperands(tokens: string[]): Set<number> {
  const skip = new Set<number>()
  const program = tokens[0] ?? ""
  if (!["sed", "grep", "egrep", "fgrep", "rgrep", "rg"].includes(program)) return skip
  const hasExpression = tokens.some((token) => ["-e", "--expression", "--regexp"].includes(token.split("=")[0]!))
  if (!tokens.some((token) => ["-f", "--file"].includes(token.split("=")[0]!))) {
    for (let index = 1; index < tokens.length; index += 1) {
      if (!tokens[index]!.startsWith("-")) {
        if (!hasExpression) skip.add(index)
        break
      }
    }
  }
  for (let index = 1; index < tokens.length; index += 1) {
    const token = tokens[index]!
    if (["-e", "--expression", "--regexp"].includes(token)) skip.add(index + 1)
    if (/^-e./.test(token) || token.startsWith("--expression=") || token.startsWith("--regexp=")) skip.add(index)
  }
  return skip
}

/** Reject an operand that names an absolute/home path or a parent path. */
function hostPathOperand(text: string): boolean {
  const tokens = text.split(/\s+/)
  const skip = skippedOperands(tokens)
  return tokens.slice(1).some((token, offset) => {
    if (skip.has(offset + 1)) return false
    const clean = operandPath(token)
    if (clean === undefined) return false
    if (/^\/dev\/(?:null|stderr|stdout)$/.test(clean)) return false
    if (/^[A-Za-z]:[\\/]/.test(clean) || /^\\\\/.test(clean)) return true
    if (clean.startsWith("/") || clean.startsWith("~")) return true
    // A parent segment could leave the reviewed tree. A Git range such as
    // `a..b` is not a path segment and stays allowed.
    return clean.split(/[\\/]/).includes("..")
  })
}

/** True when a relative operand in a fragment resolves outside the directory. */
function escapesDirectory(text: string, directory: string): boolean {
  const tokens = text.split(/\s+/)
  const skip = skippedOperands(tokens)
  for (let index = 1; index < tokens.length; index += 1) {
    if (skip.has(index)) continue
    const clean = operandPath(tokens[index]!)
    if (clean === undefined || /^\/dev\//.test(clean)) continue
    if (clean.startsWith("/") || clean.startsWith("~") || /^[A-Za-z]:[\\/]/.test(clean) || /^\\\\/.test(clean))
      return true
    const target = resolve(directory, clean)
    try {
      const rel = relative(realpathSync(directory), realpathSync(target))
      if (rel.startsWith("..") || isAbsolute(rel)) return true
      continue
    } catch {
      // A non-existent operand keeps the lexical containment check below.
    }
    if (!clean.includes("/")) continue
    const rel = relative(directory, target)
    if (rel.startsWith("..") || isAbsolute(rel)) return true
  }
  return false
}

/** Return ok only for a positively proven inspection or verification command. */
export function inspectionSafe(command: string, mode: GuardrailMode = "standard"): InspectionVerdict {
  const normalized = normalizeCommand(command)
  if (!normalized.safe) return { ok: false, reason: normalized.reason ?? "not a literal command" }
  // One command only: the sequence checker splits compounds.
  const fragments = splitFragments(normalized.text)
  if (fragments.length !== 1) return { ok: false, reason: "compound command" }
  const text = normalized.text
  if (!text) return { ok: false, reason: "empty command" }
  // A runtime value or a host path is not provable inspection.
  if (/[`$]/.test(text)) return { ok: false, reason: "runtime value" }
  if (unsafeRedirect(text)) return { ok: false, reason: "redirect outside the /dev sinks" }
  if (hostPathOperand(text)) return { ok: false, reason: "host path operand" }

  const plain = text.replace(ASSIGNMENTS, "")
  const first = plain.split(/\s+/, 1)[0] ?? ""
  if (!first) return { ok: false, reason: "no program" }
  if ((/[\\/]/.test(first) || first.startsWith("~")) && !VENV_PYTEST.test(plain))
    return { ok: false, reason: "path-qualified program" }

  // A test runner with a script-argument tail is still the project's own
  // verification; a watch/interactive mode is refused.
  if (/^npm\s+(?:test|run\s+(?:test|lint|typecheck|check))\s+--\s+.*--(?:watch|watchAll|ui|serve)\b/.test(plain))
    return { ok: false, reason: "watch or interactive test mode" }
  if (
    /^npm\s+(?:test|run\s+(?:test|lint|typecheck|check))\s+--\s+[A-Za-z0-9 _.,:=/@+-]+$/.test(plain) ||
    /^uv\s+run\s+python[0-9.]*\s+-m\s+pytest\b/.test(plain)
  )
    return { ok: true, kind: "verification" }
  if (/^make\b/.test(plain) && !/^make\s+(?:test|lint|typecheck)\b/.test(plain))
    return { ok: false, reason: "make target not in the allowed set" }

  const classification = classifyCommand(text, mode)
  if (classification.kind !== "other" && classification.kind !== "read-only-git")
    return { ok: false, reason: classification.reason }
  if (classification.writes) return { ok: false, reason: "file write" }
  if (classification.executes) return { ok: false, reason: "shell or exec channel" }

  // Git and GitHub reads are proven by the classification itself; a config read
  // is classified `other` for quietness but is still inspection.
  if (classification.kind === "read-only-git") return { ok: true, kind: "inspection" }
  if (/^git\s+config\s+--get(?:-all|-regexp)?\b/.test(plain)) {
    if (
      /\b(?:http\.|credential|url\.|proxy|core\.sshcommand|core\.pager|remote\.[^.]+\.[a-z]+url|include\.|includeif\.)\S*/i.test(
        plain,
      )
    )
      return { ok: false, reason: "credential-bearing or executing config key" }
    return { ok: true, kind: "inspection" }
  }

  if (programIdentity(first) === "jq" && JQ_AMBIENT.test(plain))
    return { ok: false, reason: "jq ambient or input builtin" }
  if (READERS.has(programIdentity(first))) return { ok: true, kind: "inspection" }
  if (VENV_PYTEST.test(plain) || /^(?:python3?|py)\s+-m\s+pytest\b/.test(plain))
    return { ok: true, kind: "verification" }
  if (programIdentity(first) === "pytest") return { ok: true, kind: "verification" }
  if (/^uv\s+run\s+(?:python[0-9.]*\s+-m\s+)?(?:pytest|ruff|mypy)\b/.test(plain))
    return { ok: true, kind: "verification" }
  if (/^npm\s+(?:test|run\s+(?:test|lint|typecheck|check)|pack\s+--dry-run)\b/.test(plain))
    return { ok: true, kind: "verification" }
  if (/^make\s+(?:test|lint|typecheck)\b/.test(plain)) return { ok: true, kind: "verification" }
  return { ok: false, reason: `program not in the positive set: ${programIdentity(first)}` }
}

export interface InspectionSequence {
  readonly ok: boolean
  readonly reason?: string
  readonly kind?: "inspection" | "verification"
  readonly directory?: string
  /** Effective directory of every verification fragment, in order. */
  readonly verifications: readonly string[]
}

/**
 * Prove a whole shell event: each top-level fragment must be inspection or
 * verification, all in one positively established directory.
 */
export function inspectionSequence(
  command: string,
  projectRoot: string,
  mode: GuardrailMode = "standard",
  allowedDirectory: (directory: string) => boolean,
): InspectionSequence {
  const effective = effectiveCommand(command, projectRoot)
  if (!effective) return { ok: false, reason: "unproven shell syntax or directory", verifications: [] }
  const { directory, prefixed } = effective
  if (prefixed && !allowedDirectory(directory))
    return { ok: false, reason: "cd outside the reviewed tree", verifications: [] }
  let kind: "inspection" | "verification" | undefined
  const verifications: string[] = []
  for (const fragment of splitFragments(effective.command)) {
    const verdict = inspectionSafe(fragment, mode)
    if (!verdict.ok) return { ok: false, reason: verdict.reason, verifications: [] }
    if (escapesDirectory(fragment, directory))
      return { ok: false, reason: "operand outside the reviewed tree", verifications: [] }
    if (verdict.kind === "verification") {
      kind = "verification"
      verifications.push(directory)
    } else kind ??= "inspection"
  }
  if (!kind) return { ok: false, reason: "no command after cd", verifications: [] }
  return { ok: true, kind, directory, verifications }
}
