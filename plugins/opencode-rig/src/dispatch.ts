/**
 * Inline role dispatch fallback.
 *
 * Native subagents are the preferred route. This fallback creates a child
 * session, injects a bounded role card, waits for completion, and returns the
 * final assistant text. It exists so workflows still run when agent files have
 * not been installed with `opencode-rig init`.
 */

import { spawnSync } from "node:child_process"
import { isUtf8 } from "node:buffer"
import { existsSync, lstatSync, readdirSync, readFileSync, readlinkSync } from "node:fs"
import { sha256 } from "./digest.ts"
import { isAbsolute, join, relative, resolve, sep } from "node:path"
import { parseOptions } from "./options.ts"
import { resolvePaths } from "./paths.ts"
import { loadRun, type RunRecord } from "./run.ts"
import { activeRun } from "./state.ts"

export const ROLE_IDS = [
  "rig-investigator",
  "rig-implementer",
  "rig-reviewer",
  "rig-challenger",
  "rig-verifier",
  "rig-scientist",
  "rig-optimizer",
] as const

export type RoleId = (typeof ROLE_IDS)[number]

export interface DispatchInput {
  readonly role: RoleId
  readonly task: string
  readonly paths?: readonly string[]
  readonly read_only?: boolean
  readonly keep_session?: boolean
  /** Session that called the tool; its own active run owns the scope check. */
  readonly sessionID?: string
}

export interface DispatchResult {
  readonly session_id: string
  readonly role: RoleId
  readonly text: string
  readonly scope_violation: boolean
  readonly violations: readonly string[]
  readonly workspace_unverified: boolean
  readonly observed_concurrently: readonly string[]
}

function buildPrompt(roleCard: string, input: DispatchInput): string {
  const paths = input.paths?.length ? input.paths.map((path) => `- ${path}`).join("\n") : "- none supplied"
  const accounting =
    input.read_only === false
      ? "\n- List every file you changed as its own line `changed: <path>`, using project-relative paths; write `changed: none` when nothing changed."
      : ""
  return `${roleCard}

## Task

${input.task}

## Scope

${paths}

## Rules

- Return one final answer with concrete evidence and file references.
- Do not run project gates or commit; the parent owns gates and integration.
- ${input.read_only === false ? "You may edit only the paths listed above." : "Read-only: do not edit files."}
- Do not create further subagents.${accounting}`
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

/** A `changed: <path>` report line, optionally prefixed by a list bullet. */
const CHANGED_LINE = /^\s*(?:[-*+]\s*)?changed(?:\s+paths?)?\s*[:=]\s*(.+?)\s*$/i

/** A value the child uses to say nothing was changed, not a path. */
const NOTHING_CHANGED = /^(?:none|nothing|no changes?)$/i

/** One unquoted project-relative path: no whitespace, commas, semicolons, or brackets. */
const BARE_PATH = /^[^\s,;()[\]{}]+$/

/** One quoted path, which may contain spaces but never prose punctuation. */
const QUOTED_PATH = /^(["'`])(.+)\1$/

/**
 * Read one accounting value as a path, or reject it as prose.
 *
 * The child is asked for one `changed: <path>` line per file. A line such as
 * `changed: 2 files (src/a.ts, tests/b.test.ts)` is a sentence about paths, not
 * a list, so it is ignored instead of being split into bogus entries that would
 * reject an otherwise successful report.
 */
function accountingPath(value: string): string | undefined {
  const quoted = QUOTED_PATH.exec(value)
  const candidate = quoted ? quoted[2] : value
  if (!candidate) return undefined
  if (quoted) {
    if (/[,;()]/.test(candidate)) return undefined
  } else if (!BARE_PATH.test(candidate)) {
    return undefined
  }
  const path = candidate.trim()
  if (!path || NOTHING_CHANGED.test(path)) return undefined
  return path
}

function reportedChangedPaths(text: string): string[] {
  const reported: string[] = []
  for (const line of text.split(/\r?\n/)) {
    const match = CHANGED_LINE.exec(line)
    const value = match?.[1]
    if (!value) continue
    const path = accountingPath(value)
    if (path) reported.push(path)
  }
  return [...new Set(reported)]
}

/** True when `reportedPath` is the scope path itself or sits below it. */
function resolvesUnder(projectRoot: string, scopePath: string, reportedPath: string): boolean {
  const base = resolve(projectRoot, scopePath)
  const target = resolve(projectRoot, reportedPath)
  const rel = relative(base, target)
  return rel === "" || (rel !== ".." && !rel.startsWith(`..${sep}`) && !isAbsolute(rel))
}

interface RecordedScope {
  readonly projectRoot: string
  readonly paths: readonly string[]
}

/** Read the caller's scope before dispatch; unreadable active evidence fails closed. */
function recordedScope(ctx: any, sessionID: string | undefined): RecordedScope | undefined {
  if (!sessionID) return undefined
  const options = parseOptions(ctx.options)
  const paths = resolvePaths(ctx.location?.directory ?? process.cwd(), options.artifacts)
  const statePath = join(paths.artifactRoot, "state.json")
  if (existsSync(statePath)) {
    try {
      const state = JSON.parse(readFileSync(statePath, "utf8"))
      if (!state || typeof state.sessions !== "object" || state.sessions === null || Array.isArray(state.sessions)) {
        throw new Error("invalid session state")
      }
    } catch (error) {
      throw new Error(`cannot read active run scope state: ${String(error)}`)
    }
  }
  const active = activeRun(paths.artifactRoot, sessionID)
  if (!active) return undefined
  try {
    const run: RunRecord = loadRun(paths, active.skill, active.runId)
    if (!Array.isArray(run.scope?.paths) || !run.scope.paths.every((path) => typeof path === "string")) {
      throw new Error("invalid recorded paths")
    }
    return run.status === "in_progress" && run.scope.paths.length > 0
      ? { projectRoot: paths.projectRoot, paths: run.scope.paths }
      : undefined
  } catch (error) {
    throw new Error(`cannot read active run scope: ${String(error)}`)
  }
}

/** Run buffered Git commands without environment redirection or executable monitors. */
function workspaceGit(projectRoot: string, args: string[], optional = false): Buffer | undefined {
  const env = Object.fromEntries(Object.entries(process.env).filter(([key]) => !key.toUpperCase().startsWith("GIT_")))
  const result = spawnSync("git", ["-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false", ...args], {
    cwd: projectRoot,
    env: { ...env, LC_ALL: "C" },
    maxBuffer: 64 * 1024 * 1024,
  })
  if ((result.error as NodeJS.ErrnoException | undefined)?.code === "ENOBUFS")
    throw new Error("cannot verify dispatch workspace: Git output exceeded the 64 MiB limit")
  if (result.error) throw new Error(`cannot verify dispatch workspace: ${result.error.message}`)
  if (result.status !== 0) {
    if (optional && (args[1] !== "--is-inside-work-tree" || /not a git repository/i.test(result.stderr.toString())))
      return undefined
    throw new Error(
      `cannot verify dispatch workspace: Git ${args[0]} failed: ${result.stderr.toString().slice(0, 200)}`,
    )
  }
  return result.stdout
}

interface SnapshotEntry {
  readonly path: string
  readonly stamp: string
  readonly value: string
}
type Snapshot = Map<string, SnapshotEntry>

/** Keep Git filenames byte-exact; reject truncated path output. */
function pathRecords(value: Buffer): Buffer[] {
  const records: Buffer[] = []
  let start = 0
  for (let end = value.indexOf(0); end !== -1; end = value.indexOf(0, start)) {
    records.push(value.subarray(start, end))
    start = end + 1
  }
  if (start !== value.length) throw new Error("cannot verify dispatch workspace: truncated Git path inventory")
  return records
}

/** Snapshot one entry without following symbolic directories or rehashing unchanged bytes. */
function snapshotEntry(root: string, raw: Buffer, state: string, previous?: SnapshotEntry): SnapshotEntry {
  const path = isUtf8(raw) ? raw.toString("utf8") : `git-bytes:${raw.toString("hex")}`
  const base = Buffer.from(root + sep)
  for (let slash = raw.indexOf(47); slash !== -1; slash = raw.indexOf(47, slash + 1)) {
    const parent = Buffer.concat([base, raw.subarray(0, slash)])
    if (lstatSync(parent, { throwIfNoEntry: false })?.isSymbolicLink()) {
      const value = `${state}:symbolic directory:${sha256(readlinkSync(parent, { encoding: "buffer" }))}`
      return { path, stamp: value, value }
    }
  }
  const absolute = Buffer.concat([base, raw])
  const info = lstatSync(absolute, { throwIfNoEntry: false, bigint: true })
  const stamp = `${state}:${info?.mode}:${info?.dev}:${info?.ino}:${info?.size}:${info?.mtimeNs}:${info?.ctimeNs}`
  if (previous?.stamp === stamp) return previous
  const bytes = !info
    ? "deleted"
    : info.isSymbolicLink()
      ? sha256(readlinkSync(absolute, { encoding: "buffer" }))
      : info.isFile()
        ? sha256(readFileSync(absolute))
        : "directory"
  return { path, stamp, value: `${state}:${info?.mode}:${bytes}` }
}

/** Add common Git hooks, config and info bytes without following symlinks. */
function administrativeSnapshot(
  common: string,
  snapshot: Snapshot,
  previous?: Snapshot,
  current = Buffer.alloc(0),
): void {
  const names = current.length
    ? readdirSync(Buffer.concat([Buffer.from(common + sep), current]), { encoding: "buffer" })
    : [Buffer.from("config"), Buffer.from("hooks"), Buffer.from("info")]
  for (const name of names) {
    const raw = Buffer.concat([current, ...(current.length ? [Buffer.from("/")] : []), name])
    const key = `admin:${raw.toString("hex")}`
    const entry = snapshotEntry(common, raw, "git-admin", previous?.get(key))
    snapshot.set(key, {
      ...entry,
      path: `.git/${isUtf8(raw) ? raw.toString("utf8") : `git-bytes:${raw.toString("hex")}`}`,
    })
    const absolute = Buffer.concat([Buffer.from(common + sep), raw])
    if (lstatSync(absolute, { throwIfNoEntry: false })?.isDirectory()) {
      administrativeSnapshot(common, snapshot, previous, raw)
    }
  }
}

/** Snapshot tracked/nonignored files, Git administration and gitlink HEADs; not ignored trees or submodule contents. */
function workspaceSnapshot(projectRoot: string, artifactRoot: string, previous?: Snapshot): Snapshot {
  const status = workspaceGit(projectRoot, ["status", "--porcelain=v1", "-z", "--untracked-files=all"])!
  const codes = new Map<string, string>()
  const records = pathRecords(status)
  for (let index = 0; index < records.length; index += 1) {
    const record = records[index]
    if (!record?.length) continue
    const code = record.subarray(0, 2).toString("ascii")
    codes.set(record.subarray(3).toString("hex"), code)
    if (/[RC]/.test(code)) index += 1
  }
  const tracked = workspaceGit(projectRoot, ["ls-files", "--stage", "-z"])!
  const untracked = workspaceGit(projectRoot, ["ls-files", "--others", "--exclude-standard", "-z"])!
  const index = new Map<string, string>()
  const names = new Map<string, Buffer>()
  for (const entry of pathRecords(tracked)) {
    const tab = entry.indexOf(9)
    if (tab >= 0) {
      const raw = entry.subarray(tab + 1)
      const key = raw.toString("hex")
      names.set(key, raw)
      index.set(key, `${index.get(key) ?? ""}${entry.subarray(0, tab).toString("ascii")};`)
    }
  }
  for (const raw of pathRecords(untracked)) names.set(raw.toString("hex"), raw)
  const snapshot: Snapshot = new Map()
  for (const [key, raw] of names) {
    const path = raw.toString("utf8")
    if (artifactRoot !== projectRoot && resolvesUnder(projectRoot, artifactRoot, path)) continue
    const state = `${codes.get(key) ?? "clean"}:${index.get(key) ?? "untracked"}`
    const entry = snapshotEntry(projectRoot, raw, state, previous?.get(key))
    const value = entry.value.split(":submodule-head:")[0]!
    if (index.get(key)?.startsWith("160000 ") && value.endsWith(":directory")) {
      const head = workspaceGit(join(projectRoot, path), ["rev-parse", "--verify", "HEAD"], true)
      snapshot.set(key, {
        ...entry,
        value: `${value}:submodule-head:${head?.toString("ascii").trim() ?? "unreadable"}`,
      })
    } else {
      snapshot.set(key, entry)
    }
  }
  const common = workspaceGit(projectRoot, ["rev-parse", "--git-common-dir"])!.toString("utf8").trim()
  administrativeSnapshot(resolve(projectRoot, common), snapshot, previous)
  return snapshot
}

interface Flight {
  readonly root: string
  readonly owned: readonly string[]
  readonly overlaps: Set<Flight>
}
const flights = new Set<Flight>()

/** Retain overlapping ownership even when the other child finishes first. */
function registerFlight(root: string, input: DispatchInput): Flight {
  const flight: Flight = { root, owned: input.read_only === false ? input.paths! : [], overlaps: new Set() }
  for (const other of flights) {
    if (other.root !== root) continue
    flight.overlaps.add(other)
    other.overlaps.add(flight)
  }
  flights.add(flight)
  return flight
}

/** Only a confirmed non-repository permits dispatch without Git verification. */
function workspaceAvailable(root: string): boolean {
  const result = workspaceGit(root, ["rev-parse", "--is-inside-work-tree"], true)
  return result?.toString("ascii").trim() === "true"
}

/** Merge the shipped permission boundary into the child session itself. */
function childPermissions(resourcesDir: string, input: DispatchInput): unknown[] {
  const roles = JSON.parse(readFileSync(join(resourcesDir, "agents.json"), "utf8"))
  const shared = JSON.parse(readFileSync(join(resourcesDir, "permissions.json"), "utf8"))
  const permissions = [...roles.agents[input.role].permissions, ...shared.permissions]
  if (input.read_only !== false) permissions.push({ action: "edit", resource: "*", effect: "deny" })
  return permissions
}

export async function dispatchRole(ctx: any, input: DispatchInput, resourcesDir: string): Promise<DispatchResult> {
  if (!ROLE_IDS.includes(input.role)) throw new Error(`unknown role: ${String(input.role)}`)
  if (!input.task.trim()) throw new Error("dispatch task must not be empty")
  if (input.read_only === false && !input.paths?.length) throw new Error("a writing child needs explicit owned paths")
  const options = parseOptions(ctx.options)
  const paths = resolvePaths(ctx.location?.directory ?? process.cwd(), options.artifacts)
  const scope = recordedScope(ctx, input.sessionID)
  const verified = workspaceAvailable(paths.projectRoot)
  const flight = registerFlight(paths.projectRoot, input)
  try {
    const before = verified ? workspaceSnapshot(paths.projectRoot, paths.artifactRoot) : undefined
    const roleCard = readFileSync(join(resourcesDir, "roles", `${input.role}.md`), "utf8")
    const session = await ctx.session.create({
      title: `opencode-rig ${input.role}`,
      permissions: childPermissions(resourcesDir, input),
    })
    let retain = input.keep_session === true
    try {
      const violations: string[] = []
      let failed = false
      let failure: unknown
      try {
        await ctx.session.prompt({ sessionID: session.id, text: buildPrompt(roleCard, input) })
        await ctx.session.wait({ sessionID: session.id })
      } catch (error) {
        failed = true
        failure = error
      }
      let text = ""
      try {
        text = lastAssistantText(await ctx.session.context({ sessionID: session.id }))
      } catch (error) {
        if (!failed) failure = error
        failed = true
      }
      let observed: string[] = []
      try {
        if (before) {
          const after = workspaceSnapshot(paths.projectRoot, paths.artifactRoot, before)
          observed = [...new Set([...before.keys(), ...after.keys()])]
            .filter((key) => before.get(key)?.value !== after.get(key)?.value)
            .map((key) => (after.get(key) ?? before.get(key))!.path)
        }
      } catch (error) {
        violations.push(`workspace verification failed: ${String(error)}`)
      }
      // Disk evidence covers tracked/nonignored files, selected Git administration and gitlink HEADs.
      // Ignored untracked trees, submodule working-tree bytes and external processes remain unverified.
      const concurrent = observed.filter((path) =>
        [...flight.overlaps].some((other) =>
          other.owned.some((candidate) => resolvesUnder(other.root, candidate, path)),
        ),
      )
      const unexplained = observed.filter((path) => !concurrent.includes(path))
      const changed =
        input.read_only === false ? [...new Set([...unexplained, ...reportedChangedPaths(text)])] : unexplained
      for (const path of changed) {
        if (input.read_only !== false) {
          violations.push(`read-only child changed path: ${path}`)
        } else if (!input.paths?.some((candidate) => resolvesUnder(paths.projectRoot, candidate, path))) {
          violations.push(`changed path outside the input scope: ${path}; input scope: ${input.paths?.join(", ")}`)
        }
        if (scope && !scope.paths.some((candidate) => resolvesUnder(scope.projectRoot, candidate, path))) {
          violations.push(
            `changed path outside the recorded run scope: ${path}; recorded scope: ${scope.paths.join(", ")}`,
          )
        }
      }
      if (violations.length > 0 || failed) retain = true
      if (failed)
        throw new Error(
          `child session failed: ${String(failure)}${violations.length ? `\n${violations.join("\n")}` : ""}`,
        )
      if (!text.trim() && violations.length === 0) throw new Error("child session produced no final assistant text")
      const coverage =
        "\nWorkspace coverage: tracked/nonignored files, Git config/hooks/info, gitlink OIDs and HEADs; ignored trees and submodule working-tree content unverified."
      const observations = [
        ...(!verified ? ["workspace_unverified: true"] : []),
        ...concurrent.map((path) => `observed_concurrently: ${path}`),
      ].join("\n")
      const warning =
        violations.length > 0
          ? `\n\nSCOPE VIOLATION: true\n${violations.join("\n")}\nChild session retained: ${session.id}`
          : ""
      return {
        session_id: session.id,
        role: input.role,
        text: text + coverage + (observations ? `\n${observations}` : "") + warning,
        scope_violation: violations.length > 0,
        violations,
        workspace_unverified: !verified,
        observed_concurrently: concurrent,
      }
    } finally {
      if (!retain) await ctx.session.remove({ sessionID: session.id }).catch(() => undefined)
    }
  } finally {
    flights.delete(flight)
    flight.overlaps.clear()
  }
}
