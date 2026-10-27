/**
 * Shell command classification for Git guardrails.
 *
 * The permission `evaluate` hook runs after configured rules. This module only
 * decides when to escalate an `allow` to `ask`; it never auto-allows a command
 * the user configured as `ask`, and a configured `deny` stays final before the
 * hook is consulted.
 */

import type { GrantState } from "./git-grant.ts"
import type { GuardrailMode } from "./options.ts"

export type GuardrailClass =
  "remote-mutation" | "destructive-local" | "history-rewrite" | "plain-commit" | "read-only-git" | "other"

export interface Classification {
  readonly kind: GuardrailClass
  readonly command: string
  readonly reason: string
}

export interface Decision {
  readonly effect: "ask"
  readonly message: string
  readonly kind: GuardrailClass
}

const SEVERITY: Record<GuardrailClass, number> = {
  "remote-mutation": 5,
  "destructive-local": 4,
  "history-rewrite": 3,
  "plain-commit": 2,
  "read-only-git": 1,
  other: 0,
}

const REMOTE_PATTERNS: readonly RegExp[] = [
  /^git\s+push\b/,
  /^git\s+remote\s+(add|remove|rm|set-url|prune|update)\b/,
  /^gh\s+pr\s+(create|merge|close|reopen|edit|comment|review|ready)\b/,
  /^gh\s+release\s+(create|upload|delete|edit)\b/,
  /^gh\s+repo\s+(create|delete|edit|rename|archive)\b/,
  /^npm\s+publish\b/,
  /^uv\s+publish\b/,
  /^twine\s+upload\b/,
  /^docker\s+push\b/,
  /^git\s+tag\b.*\s+push\b/,
]

const HISTORY_PATTERNS: readonly RegExp[] = [
  /^git\s+rebase\b(?!\s+--(abort|continue|skip|quit))/,
  /^git\s+commit\b.*\s--amend\b/,
  /^git\s+filter-repo\b/,
  /^git\s+filter-branch\b/,
  /^git\s+replace\b/,
]

const DESTRUCTIVE_PATTERNS: readonly RegExp[] = [
  /^git\s+reset\s+--hard\b/,
  /^git\s+clean\b.*-[a-z]*[fdx]/,
  /^git\s+branch\s+(-D|--delete\s+--force)\b/,
  /^git\s+checkout\s+(-f|--force|--\s+\.?)/,
  /^git\s+restore\b(?!.*--staged)/,
  /^git\s+stash\s+(drop|clear)\b/,
  /^git\s+worktree\s+remove\b.*--force\b/,
  /^rm\s+-[a-z]*[rf][a-z]*\s/,
  /^find\b.*\s-delete\b/,
  /^truncate\s+-s\s+0\b/,
]

const STRICT_EXTRA_PATTERNS: readonly RegExp[] = [/^git\s+merge\b/, /^git\s+cherry-pick\b/]

const READ_ONLY_PATTERNS: readonly RegExp[] = [
  /^git\s+(status|diff|log|show|fetch|rev-parse|ls-files|blame|grep|shortlog|describe|cat-file|show-ref|for-each-ref)\b/,
  /^git\s+branch\s+(--list|-l|-a|-r|-v|-vv)\b/,
  /^git\s+remote\s+(-v|--verbose|show|get-url)\b/,
  /^git\s+worktree\s+list\b/,
  /^git\s+stash\s+list\b/,
  /^git\s+pull\s+--ff-only\b/,
  /^gh\s+(pr|issue|repo|run)\s+(view|list|status|diff|checks)\b/,
]

const PLAIN_COMMIT = /^git\s+commit\b/

function segments(command: string): string[] {
  return command
    .split(/\n|&&|\|\||;|\|/)
    .map((segment) =>
      segment
        .trim()
        .replace(/^(?:env\s+)?(?:[A-Za-z_][A-Za-z0-9_]*=[^\s]*\s+)+/, "")
        .replace(/^sudo\s+/, ""),
    )
    .filter(Boolean)
}

/**
 * Classify one shell command string. The most severe segment wins.
 */
export function classifyCommand(command: string, mode: GuardrailMode = "standard"): Classification {
  let winner: Classification = { kind: "other", command, reason: "no guarded pattern matched" }
  for (const segment of segments(command)) {
    const candidate = classifySegment(segment, command, mode)
    if (SEVERITY[candidate.kind] > SEVERITY[winner.kind]) winner = candidate
  }
  return winner
}

function classifySegment(segment: string, original: string, mode: GuardrailMode): Classification {
  if (REMOTE_PATTERNS.some((pattern) => pattern.test(segment))) {
    return { kind: "remote-mutation", command: original, reason: "remote mutation" }
  }
  if (HISTORY_PATTERNS.some((pattern) => pattern.test(segment))) {
    return { kind: "history-rewrite", command: original, reason: "history rewrite" }
  }
  if (DESTRUCTIVE_PATTERNS.some((pattern) => pattern.test(segment))) {
    return { kind: "destructive-local", command: original, reason: "destructive local operation" }
  }
  if (mode === "strict" && STRICT_EXTRA_PATTERNS.some((pattern) => pattern.test(segment))) {
    return { kind: "destructive-local", command: original, reason: "strict mode: integration operation" }
  }
  if (PLAIN_COMMIT.test(segment)) {
    return { kind: "plain-commit", command: original, reason: "plain local commit" }
  }
  if (READ_ONLY_PATTERNS.some((pattern) => pattern.test(segment))) {
    return { kind: "read-only-git", command: original, reason: "read-only Git" }
  }
  return { kind: "other", command: original, reason: "no guarded pattern matched" }
}

/**
 * Decide whether to escalate a permission decision. `undefined` means the
 * caller must leave the configured effect untouched.
 */
export function decide(classification: Classification, mode: GuardrailMode, grant: GrantState): Decision | undefined {
  if (mode === "off") return undefined
  if (classification.kind === "remote-mutation") {
    return {
      effect: "ask",
      kind: classification.kind,
      message: `opencode-rig: remote mutations are never automatic (${classification.command}). Publication stays human-owned.`,
    }
  }
  if (classification.kind === "destructive-local" || classification.kind === "history-rewrite") {
    return {
      effect: "ask",
      kind: classification.kind,
      message: `opencode-rig: ${classification.reason} requires confirmation (${classification.command}). No approval grant covers it.`,
    }
  }
  if (classification.kind === "plain-commit") {
    // A grant restores the permission the user already configured; it never
    // upgrades a configured `ask`, and `allow` is not ours to grant.
    if (grant.status === "granted") return undefined
    return {
      effect: "ask",
      kind: classification.kind,
      message:
        "opencode-rig: no local Git grant. Approve this commit, record a project grant, or deny it. Grants never cover push, force, destructive operations, or history rewrites.",
    }
  }
  return undefined
}
