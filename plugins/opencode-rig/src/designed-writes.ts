/**
 * Positive model for the Git writes the workflows perform by design.
 *
 * Slice 2 of the approval-free plan: the review fetches a pull request into its
 * own ref, creates and removes a worktree under the host temp root, and the
 * builder stages owned paths. Each form is a whole simple command with a
 * literal shape; anything else falls back to the normal guardrail decision.
 *
 * Staging expands literal operands against the index and current working tree.
 */

import { tmpdir } from "node:os"
import { isAbsolute, relative, resolve, sep } from "node:path"
import { isReviewWorktree } from "./trust.ts"
import { runGit } from "./util/git.ts"

export interface DesignedWriteVerdict {
  readonly ok: boolean
  readonly reason?: string
}

export interface DesignedWriteContext {
  readonly projectRoot: string
  /** Host temp root; defaults to the system temporary directory. */
  readonly tempRoot?: string
}

/** A remote URL form only; a local path or a file:// URL is not the review fetch. */
const REMOTE_URL = /^(?:(?:https?|ssh|git):\/\/[^/\s]+\/\S*|[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:\S+)$/

/** Reject text that a shell would expand or split before the program runs. */
function literal(text: string): boolean {
  return !/[$`\\*?[\]{}()<>|;&\n]/.test(text) && !text.startsWith("-")
}

/**
 * A Windows absolute path uses `\` as its native separator, so it is literal
 * there rather than an escape. Other characters that a shell expands stay
 * rejected, and containment is still checked after the fact.
 */
const WINDOWS_ABSOLUTE = /^(?:[A-Za-z]:[\\/]|\\\\)/

function literalPath(text: string): boolean {
  return WINDOWS_ABSOLUTE.test(text) ? literal(text.replaceAll("\\", "/")) : literal(text)
}

/** Prove the path is a real location under `<temp>/opencode/review-*`. */
function reviewPath(value: string, context: DesignedWriteContext): boolean {
  return isAbsolute(value) && literalPath(value) && isReviewWorktree(value, context.tempRoot)
}

/** Identify credential-shaped names anywhere within a staging path. */
function protectedPath(path: string): boolean {
  return path
    .split("/")
    .some((name) =>
      /^(?:\.ssh|\.aws|\.docker|\.npmrc|\.pypirc|\.netrc|\.envrc|\.env(?:\..*)?)$|\.(?:pem|key|p12|pfx|jks)$|credential|^id_(?:rsa|ed25519|ecdsa|dsa)/i.test(
        name,
      ),
    )
}

/** Prove a staging operand is an ordinary literal path inside the project. */
function stagedPath(value: string, context: DesignedWriteContext): boolean {
  if (!value || value.startsWith("~") || value.startsWith(":") || /^[A-Za-z]:/.test(value)) return false
  if (!literal(value) || isAbsolute(value) || value.split("/").includes("..")) return false
  if (protectedPath(value)) return false
  const path = resolve(context.projectRoot, value)
  const rel = relative(context.projectRoot, path)
  return Boolean(rel) && !rel.startsWith("..") && !isAbsolute(rel)
}

function fetchSafe(tokens: string[]): DesignedWriteVerdict {
  const exact = ["--no-tags", "--no-write-fetch-head", "--no-auto-maintenance"]
  if (tokens.length !== 5 || exact.some((option, index) => tokens[index] !== option))
    return { ok: false, reason: "not the review fetch form" }
  const url = tokens[3]!
  const refspec = tokens[4]!
  if (!literal(url) || !REMOTE_URL.test(url)) return { ok: false, reason: "fetch URL is not a remote URL" }
  const match = /^refs\/pull\/(\d+)\/head:refs\/rig-review\/pr-(\d+)-([0-9a-f]{12})$/.exec(refspec)
  if (!match || match[1] !== match[2]) return { ok: false, reason: "fetch refspec is not the review form" }
  return { ok: true }
}

function worktreeSafe(tokens: string[], context: DesignedWriteContext): DesignedWriteVerdict {
  const [verb, ...args] = tokens
  if (verb === "add") {
    if (args.length !== 3 || args[0] !== "--detach") return { ok: false, reason: "not the review worktree add form" }
    if (!reviewPath(args[1]!, context)) return { ok: false, reason: "worktree path is outside the review temp root" }
    if (!/^[0-9a-f]{40}$/.test(args[2]!)) return { ok: false, reason: "worktree commit is not a literal SHA-1" }
    return { ok: true }
  }
  if (verb === "remove") {
    const path = args[0] === "--force" ? args[1] : args[0]
    if (args.length === (args[0] === "--force" ? 2 : 1) && path && reviewPath(path, context)) return { ok: true }
    return { ok: false, reason: "not the review worktree remove form" }
  }
  return { ok: false, reason: "not a designed worktree form" }
}

function addSafe(tokens: string[], context: DesignedWriteContext): DesignedWriteVerdict {
  const operands = tokens[0] === "--" ? tokens.slice(1) : tokens
  if (operands.length === 0 || operands.some((token) => token.startsWith("-")))
    return { ok: false, reason: "not the owned-path staging form" }
  if (!operands.every((token) => stagedPath(token, context)))
    return { ok: false, reason: "staging operand is not a literal project path" }
  // Git supplies the same recursive pathspec coverage as add. Include tracked
  // files even when a later ignore rule matches, and omit ignored untracked files.
  const candidates = runGit(
    context.projectRoot,
    ["--literal-pathspecs", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", ...operands],
    { maxBuffer: 4_000_000 },
  )
  if (candidates.error || candidates.status !== 0)
    return { ok: false, reason: "cannot prove the paths selected for staging" }
  if (candidates.stdout.toString("utf8").split("\0").some(protectedPath))
    return { ok: false, reason: "staging would select a protected path" }
  return { ok: true }
}

/**
 * Accept only a designed Git write. The command must be one whole simple
 * command: no separator, redirect, substitution or quoting is allowed.
 */
export function designedWriteSafe(command: string, context: DesignedWriteContext): DesignedWriteVerdict {
  const text = command.trim()
  if (!text || text.includes("\n") || text.includes("\\\n")) return { ok: false, reason: "not a single command" }
  if (/\s{2,}/.test(text)) return { ok: false, reason: "not a single command" }
  const tokens = text.split(" ")
  if (tokens.some((token) => token === "" || /\s|["'`$();<>|&]/.test(token)))
    return { ok: false, reason: "not a literal command" }
  if (tokens[0] !== "git") return { ok: false, reason: "not a Git write" }
  if (tokens[1] === "fetch") return fetchSafe(tokens.slice(2))
  if (tokens[1] === "worktree") return worktreeSafe(tokens.slice(2), context)
  if (tokens[1] === "add") return addSafe(tokens.slice(2), context)
  return { ok: false, reason: "not a designed Git write" }
}
