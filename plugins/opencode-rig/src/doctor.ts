/**
 * Install and project health checks.
 *
 * Doctor is read-only by design: it reports the exact remediation command and
 * leaves mutation to `opencode-rig init` or an explicit user action.
 */

import { existsSync, lstatSync, readFileSync, readdirSync } from "node:fs"
import { homedir } from "node:os"
import { join } from "node:path"
import { detectGates } from "./gates.ts"
import { readGrant } from "./git-grant.ts"
import { PLUGIN_VERSION } from "./version.ts"
import type { RigOptions } from "./options.ts"

export const AGENT_NAMES = [
  "rig-investigator",
  "rig-implementer",
  "rig-reviewer",
  "rig-challenger",
  "rig-verifier",
] as const

export interface AgentDirs {
  readonly global: string
  readonly project: string
}

/**
 * Resolve where OpenCode discovers agent Markdown. The project directory is
 * preferred when it exists; otherwise the user-wide directory applies.
 */
export function resolveAgentDirs(
  projectRoot: string,
  env: NodeJS.ProcessEnv = process.env,
  home: string = homedir(),
): AgentDirs {
  const configRoot = env.OPENCODE_CONFIG_DIR?.trim() || join(home, ".config", "opencode")
  return { global: join(configRoot, "agents"), project: join(projectRoot, ".opencode", "agents") }
}

export interface DoctorInput {
  readonly packageRoot: string
  readonly projectRoot: string
  readonly artifactRoot: string
  readonly pluginListed: boolean
  readonly commonDir?: string
  readonly agentDir?: string
  readonly options: RigOptions
}

export interface DoctorReport {
  readonly ok: boolean
  readonly lines: readonly string[]
}

function readJsonSafe(path: string): Record<string, unknown> | undefined {
  try {
    return JSON.parse(readFileSync(path, "utf8")) as Record<string, unknown>
  } catch {
    return undefined
  }
}

export function readDeclaredVersion(root: string): string | undefined {
  const manifest = readJsonSafe(join(root, "package.json"))
  return typeof manifest?.version === "string" ? manifest.version : undefined
}

/**
 * Compare the version-bearing files. Messages name the file, not just the
 * value, so the fix is mechanical.
 */
export function checkVersionSync(root: string): string[] {
  const problems: string[] = []
  const pkg = readDeclaredVersion(root)
  const rig = readJsonSafe(join(root, "rig.json"))
  const versionTs = existsSync(join(root, "src", "version.ts"))
    ? readFileSync(join(root, "src", "version.ts"), "utf8")
    : ""
  const readme = existsSync(join(root, "README.md")) ? readFileSync(join(root, "README.md"), "utf8") : ""

  if (!pkg) problems.push("package.json: missing or unreadable version")
  if (rig?.version !== pkg) problems.push(`rig.json: version ${String(rig?.version)} != package.json ${String(pkg)}`)
  const tsMatch = /PLUGIN_VERSION = "([^"]+)"/.exec(versionTs)
  if (!tsMatch) problems.push("src/version.ts: PLUGIN_VERSION is missing")
  else if (tsMatch[1] !== pkg) problems.push(`src/version.ts: ${tsMatch[1]} != package.json ${String(pkg)}`)
  const marker = /Current version:\s*`([^`]+)`/.exec(readme)
  if (!marker) problems.push("README.md: missing 'Current version: `<x.y.z>`' marker")
  else if (marker[1] !== pkg) problems.push(`README.md: ${marker[1]} != package.json ${String(pkg)}`)
  return problems
}

export function installedAgentStatus(agentDir: string, version: string): string[] {
  const problems: string[] = []
  if (!existsSync(agentDir)) return [`agents: ${agentDir} does not exist (run opencode-rig init)`]
  for (const name of AGENT_NAMES) {
    const path = join(agentDir, `${name}.md`)
    if (!existsSync(path)) {
      problems.push(`agents: ${name} is missing (run opencode-rig init)`)
      continue
    }
    const content = readFileSync(path, "utf8")
    const stamp = /<!-- opencode-rig: v([^ ]+) sha256:([a-f0-9]{64}) -->/.exec(content)
    if (!stamp) problems.push(`agents: ${name} has no opencode-rig stamp`)
    else if (stamp[1] !== version)
      problems.push(`agents: ${name} is v${stamp[1]}, expected v${version} (run opencode-rig init --upgrade)`)
  }
  return problems
}

export function staleRuns(runsRoot: string, ttlDays: number, now: Date = new Date()): string[] {
  if (!existsSync(runsRoot)) return []
  const stale: string[] = []
  const cutoff = now.getTime() - ttlDays * 24 * 60 * 60 * 1000
  for (const skill of readdirSync(runsRoot, { withFileTypes: true })) {
    if (!skill.isDirectory()) continue
    for (const run of readdirSync(join(runsRoot, skill.name), { withFileTypes: true })) {
      if (!run.isDirectory()) continue
      const path = join(runsRoot, skill.name, run.name)
      const info = lstatSync(path, { throwIfNoEntry: false })
      if (info && info.mtimeMs < cutoff) stale.push(path)
    }
  }
  return stale
}

function isIgnored(projectRoot: string, artifactRoot: string): boolean {
  const gitignore = join(projectRoot, ".gitignore")
  if (!existsSync(gitignore)) return false
  const text = readFileSync(gitignore, "utf8")
  const relative = artifactRoot.startsWith(projectRoot)
    ? artifactRoot.slice(projectRoot.length).replace(/^[/\\]/, "")
    : artifactRoot
  return text
    .split("\n")
    .some((line) => line.trim() === `${relative}/` || line.trim() === relative || line.trim() === `/${relative}/`)
}

export function doctorReport(input: DoctorInput): DoctorReport {
  const lines: string[] = []
  let ok = true
  const pass = (message: string) => lines.push(`PASS  ${message}`)
  const warn = (message: string) => lines.push(`WARN  ${message}`)
  const fail = (message: string) => {
    ok = false
    lines.push(`FAIL  ${message}`)
  }

  if (input.pluginListed) pass(`plugin loaded (opencode-rig ${PLUGIN_VERSION})`)
  else fail("plugin is not in the active plugin list")

  const versionProblems = checkVersionSync(input.packageRoot)
  if (versionProblems.length === 0) pass("version sources agree")
  for (const problem of versionProblems) fail(problem)

  if (input.agentDir) {
    const agentProblems = installedAgentStatus(input.agentDir, PLUGIN_VERSION)
    if (agentProblems.length === 0) pass(`agents installed in ${input.agentDir}`)
    for (const problem of agentProblems) warn(problem)
  } else {
    warn("agent directory not resolved; native subagents may be unavailable (inline dispatch still works)")
  }

  if (!input.commonDir) fail("not inside a Git repository; approval grant and diff evidence are unavailable")
  else {
    const grant = readGrant(input.commonDir)
    if (grant.status === "granted") pass(`local Git grant present (${input.commonDir})`)
    else lines.push(`INFO  local Git grant ${grant.status}${grant.reason ? `: ${grant.reason}` : ""}`)
  }

  if (existsSync(input.artifactRoot)) pass(`artifact root exists (${input.artifactRoot})`)
  else warn(`artifact root does not exist yet (${input.artifactRoot}); it is created on first run`)

  if (isIgnored(input.projectRoot, input.artifactRoot)) pass("artifact root is gitignored")
  else warn(`add '${input.options.artifacts}/' to .gitignore`)

  const gates = detectGates(input.projectRoot, input.options.gates)
  if (gates.length > 0) pass(`gates: ${gates.map((gate) => gate.id).join(", ")} (${gates[0]?.source})`)
  else warn("no configured gates detected; completion will require an explicit gate exemption")

  const stale = staleRuns(input.artifactRoot ? join(input.artifactRoot, "runs") : "", input.options.ttlDays)
  if (stale.length > 0)
    warn(`${stale.length} run(s) older than ${input.options.ttlDays} days; inspect or remove manually`)

  return { ok, lines }
}
