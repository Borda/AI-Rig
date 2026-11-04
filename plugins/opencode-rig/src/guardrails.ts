/**
 * Shell command classification for Git guardrails.
 *
 * The permission `evaluate` hook runs after configured rules. This module only
 * decides when to escalate an `allow` to `ask`; it never auto-allows a command
 * the user configured as `ask`, and a configured `deny` stays final before the
 * hook is consulted. Escalation messages may name the working-tree paths a
 * destructive command would discard; that lookup is bounded and optional, and
 * a failure there leaves the configured effect and the plain message intact.
 */

import { spawnSync } from "node:child_process"
import { devNull } from "node:os"
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

/** Evidence for naming what a destructive command would discard. */
export interface DiscardContext {
  /** Working-tree paths with uncommitted changes. */
  readonly dirtyPaths: readonly string[]
  /** Paths recorded by the active run; absent when no run is active. */
  readonly scopePaths?: readonly string[]
  /** Set when the active run scope could not be read, so no path claim is made. */
  readonly scopeUnknown?: boolean
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

// Mirrors the denylist in `paths.ts`: a planted override must not redirect the
// working-tree lookup away from the repository the hook is guarding.
const GIT_OVERRIDE_ENV = [
  "GIT_DIR",
  "GIT_WORK_TREE",
  "GIT_COMMON_DIR",
  "GIT_INDEX_FILE",
  "GIT_OBJECT_DIRECTORY",
  "GIT_ALTERNATE_OBJECT_DIRECTORIES",
  "GIT_NAMESPACE",
  "GIT_PREFIX",
  "GIT_CONFIG_GLOBAL",
  "GIT_CONFIG_SYSTEM",
  "GIT_CONFIG_COUNT",
  "GIT_CONFIG_PARAMETERS",
] as const

// `GIT_CONFIG_KEY_<n>` / `GIT_CONFIG_VALUE_<n>` inject arbitrary config, including
// `core.fsmonitor`, whose path Git executes. They are stripped by prefix.
const GIT_CONFIG_ENV_PREFIXES = ["GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"] as const

// Repository-controlled settings that can execute a program during `git status`.
// `-c` wins over the repository, the user, and system config.
const STATUS_CONFIG_OVERRIDES = [
  "-c",
  "core.fsmonitor=false",
  "-c",
  "core.untrackedCache=false",
  "-c",
  `core.excludesFile=${devNull}`,
]

const STATUS_TIMEOUT_MS = 1_000
const STATUS_MAX_BYTES = 4_000_000
const MAX_NAMED_PATHS = 10
const MAX_NAMED_PATH_CHARS = 120
const MAX_NAMED_TOTAL_CHARS = 600

/** Strip environment overrides that let a planted value redirect or execute code. */
function cleanGitEnv(): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = { ...process.env }
  for (const key of GIT_OVERRIDE_ENV) delete env[key]
  for (const key of Object.keys(env)) {
    if (GIT_CONFIG_ENV_PREFIXES.some((prefix) => key.startsWith(prefix))) delete env[key]
  }
  return env
}

/**
 * List working-tree paths with uncommitted changes. Bounded and failure-safe:
 * `undefined` means the escalation message stays unchanged instead of the
 * permission decision waiting on or failing with a broken repository lookup.
 * `ignored` holds plugin-owned relative paths, which are never someone else's
 * work.
 */
export function dirtyPaths(cwd: string, ignored: readonly string[] = []): string[] | undefined {
  const result = spawnSync(
    "git",
    [...STATUS_CONFIG_OVERRIDES, "status", "--porcelain=v1", "-z", "--untracked-files=all"],
    {
      cwd,
      env: cleanGitEnv(),
      encoding: "utf8",
      timeout: STATUS_TIMEOUT_MS,
      maxBuffer: STATUS_MAX_BYTES,
    },
  )
  if (result.error || result.status !== 0) return undefined
  const paths = parseStatusPaths(result.stdout)
  return ignored.length > 0 ? paths.filter((path) => !underAny(path, ignored)) : paths
}

function parseStatusPaths(output: string): string[] {
  const records = output.split("\0")
  const paths: string[] = []
  for (let index = 0; index < records.length; index += 1) {
    const record = records[index]
    if (!record || record.length < 4) continue
    const path = record.slice(3)
    if (!path) continue
    paths.push(path)
    // A rename or copy report carries the source path in the next NUL record.
    const code = record.slice(0, 2)
    if (code.startsWith("R") || code.startsWith("C")) {
      const source = records[index + 1]
      if (source) {
        paths.push(source)
        index += 1
      }
    }
  }
  return paths
}

function underAny(path: string, prefixes: readonly string[]): boolean {
  return prefixes.some((prefix) => path === prefix || path.startsWith(`${prefix}/`))
}

function foreignDirtyPaths(dirty: readonly string[], scope: readonly string[] | undefined): string[] {
  if (scope === undefined) return [...dirty]
  // An empty recorded filter means the run covers every changed path.
  if (scope.length === 0) return []
  return dirty.filter((path) => !underAny(path, scope))
}

/**
 * Render one untrusted filename for the approval prompt.
 *
 * Git reports whatever bytes the filesystem holds, including newlines, ANSI
 * escapes, and text shaped like the prompt's own delimiters. Flattening control
 * characters and whitespace keeps a filename from forging extra lines, and the
 * length caps keep a single path from crowding out the rest of the message.
 */
function sanitizePath(path: string): string {
  const cleaned = path
    .replace(/[\u0000-\u001f\u007f-\u009f]/g, " ")
    .replace(/\s+/g, " ")
    .trim()
  if (cleaned === "") return "<unprintable path>"
  return cleaned.length > MAX_NAMED_PATH_CHARS ? `${cleaned.slice(0, MAX_NAMED_PATH_CHARS - 1)}…` : cleaned
}

function formatPaths(paths: readonly string[]): string {
  const shown: string[] = []
  let total = 0
  for (const raw of paths) {
    if (shown.length >= MAX_NAMED_PATHS) break
    const path = sanitizePath(raw)
    if (shown.length > 0 && total + path.length + 2 > MAX_NAMED_TOTAL_CHARS) break
    shown.push(path)
    total += path.length + 2
  }
  const remaining = paths.length - shown.length
  return remaining > 0 ? `${shown.join(", ")}, and ${remaining} more` : shown.join(", ")
}

function discardWarning(context: DiscardContext | undefined): string {
  if (!context || context.scopeUnknown || context.dirtyPaths.length === 0) return ""
  const foreign = foreignDirtyPaths(context.dirtyPaths, context.scopePaths)
  if (foreign.length === 0) return ""
  const named = formatPaths(foreign)
  if (context.scopePaths === undefined) return ` This would discard uncommitted changes: ${named}.`
  return ` This would discard changes that are not part of this run: ${named}.`
}

/**
 * Decide whether to escalate a permission decision. `undefined` means the
 * caller must leave the configured effect untouched.
 */
export function decide(
  classification: Classification,
  mode: GuardrailMode,
  grant: GrantState,
  discard?: DiscardContext,
): Decision | undefined {
  if (mode === "off") return undefined
  if (classification.kind === "remote-mutation") {
    return {
      effect: "ask",
      kind: classification.kind,
      message: `opencode-rig: remote mutations are never automatic (${classification.command}). Publication stays human-owned.`,
    }
  }
  if (classification.kind === "destructive-local" || classification.kind === "history-rewrite") {
    const warning = discardWarning(discard)
    return {
      effect: "ask",
      kind: classification.kind,
      message: `opencode-rig: ${classification.reason} requires confirmation (${classification.command}). No approval grant covers it.${warning}`,
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
