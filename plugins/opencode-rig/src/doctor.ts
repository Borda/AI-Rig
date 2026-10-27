/**
 * Install and project health checks.
 *
 * Doctor is read-only by design: it reports the exact remediation command and
 * leaves mutation to `opencode-rig init` or an explicit user action.
 */

import { existsSync, lstatSync, readFileSync, readdirSync } from "node:fs"
import { homedir } from "node:os"
import { join, relative, sep } from "node:path"
import { detectGates } from "./gates.ts"
import { readGrant } from "./git-grant.ts"
import { SKILLS } from "./skill-manifest.ts"
import { PLUGIN_VERSION } from "./version.ts"
import type { RigOptions } from "./options.ts"

export const AGENT_NAMES = [
  "rig-investigator",
  "rig-implementer",
  "rig-reviewer",
  "rig-challenger",
  "rig-verifier",
  "rig-scientist",
  "rig-optimizer",
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
  readonly assets?: boolean
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

const MAX_SKILL_LINES = 160
const MAX_ROLE_LINES = 80
const DUPLICATE_MIN_LENGTH = 60
const MAX_REPORTED_DUPLICATES = 3

export interface AssetCheck {
  readonly failures: readonly string[]
  readonly warnings: readonly string[]
  readonly summary: string
}

function diffRoster(expected: readonly string[], actual: readonly string[]): string | undefined {
  const left = [...new Set(expected)].sort()
  const right = [...new Set(actual)].sort()
  const missing = left.filter((name) => !right.includes(name))
  const extra = right.filter((name) => !left.includes(name))
  if (missing.length === 0 && extra.length === 0) return undefined
  return [
    missing.length > 0 ? `missing ${missing.join(", ")}` : "",
    extra.length > 0 ? `unlisted ${extra.join(", ")}` : "",
  ]
    .filter(Boolean)
    .join("; ")
}

function rosterFailure(label: string, declared: unknown, actual: readonly string[]): string | undefined {
  if (!Array.isArray(declared)) return `assets: ${label} is missing from rig.json`
  const diff = diffRoster(declared.map(String), actual)
  return diff ? `assets: ${label} do not match shipped files (${diff})` : undefined
}

function skillProseKey(line: string): string | undefined {
  const normalized = line.trim()
  if (normalized.length < DUPLICATE_MIN_LENGTH) return undefined
  if (/^[>#|`]/.test(normalized)) return undefined
  return normalized
}

/**
 * Check the shipped skill and role assets as a curator would: roster agreement,
 * frontmatter, relative cross-references, duplicated prose, and card size.
 * Failures block health; duplication and size are warnings, because they are
 * maintenance signals rather than defects.
 */
export function checkAssets(resourcesRoot: string): AssetCheck {
  const packageRoot = join(resourcesRoot, "..", "..")
  const rig = readJsonSafe(join(packageRoot, "rig.json")) ?? {}
  const skillsDir = join(resourcesRoot, "skills")
  const rolesDir = join(resourcesRoot, "roles")
  const failures: string[] = []
  const warnings: string[] = []

  if (!existsSync(skillsDir) || !existsSync(rolesDir)) {
    return {
      failures: ["assets: shipped skills or roles directory is missing; reinstall the package"],
      warnings: [],
      summary: "assets unavailable",
    }
  }

  const skills = readdirSync(skillsDir, { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .map((entry) => entry.name)
    .sort()
  const roles = readdirSync(rolesDir)
    .filter((file) => file.endsWith(".md"))
    .map((file) => file.slice(0, -3))
    .sort()
  const manifest = readJsonSafe(join(resourcesRoot, "agents.json"))
  const declaredAgents =
    manifest && typeof manifest.agents === "object" && manifest.agents !== null
      ? Object.keys(manifest.agents as Record<string, unknown>)
      : []

  for (const failure of [
    rosterFailure("skills", rig.skills, skills),
    rosterFailure("agents", rig.agents, declaredAgents),
  ]) {
    if (failure) failures.push(failure)
  }
  const manifestDiff = diffRoster(
    SKILLS.map((skill) => skill.id),
    skills,
  )
  if (manifestDiff) failures.push(`assets: skill manifest does not match shipped cards (${manifestDiff})`)
  const roleDiff = diffRoster(declaredAgents, roles)
  if (roleDiff) failures.push(`assets: role files do not match agents.json (${roleDiff})`)

  const prose = new Map<string, string[]>()
  for (const skill of skills) {
    const path = join(skillsDir, skill, "SKILL.md")
    if (!existsSync(path)) {
      failures.push(`assets: ${skill}/SKILL.md is missing`)
      continue
    }
    const text = readFileSync(path, "utf8")
    const frontmatter = /^---\r?\n([\s\S]*?)\r?\n---/.exec(text)?.[1]
    if (!frontmatter) {
      failures.push(`assets: ${skill}/SKILL.md has no frontmatter`)
    } else {
      const name = /^name: (.+)$/m.exec(frontmatter)?.[1]?.trim()
      if (!name) failures.push(`assets: ${skill}/SKILL.md has no frontmatter name`)
      if (!/^description: .+/m.test(frontmatter)) failures.push(`assets: ${skill}/SKILL.md has no description`)
      if (name && !new RegExp(`^# ${name}$`, "m").test(text)) {
        failures.push(`assets: ${skill}/SKILL.md heading does not match '${name}'`)
      }
    }
    if (!SKILLS.some((definition) => definition.id === skill)) {
      failures.push(`assets: ${skill} is missing from the skill manifest`)
    }
    for (const match of text.matchAll(/\]\((\.[^)#]+)\)/g)) {
      const target = match[1]
      if (target && !existsSync(join(skillsDir, skill, target))) {
        failures.push(`assets: ${skill}/SKILL.md references missing ${target}`)
      }
    }
    const lineCount = text.split("\n").length
    if (lineCount > MAX_SKILL_LINES) {
      warnings.push(`assets: ${skill}/SKILL.md has ${lineCount} lines (soft limit ${MAX_SKILL_LINES})`)
    }
    for (const line of text.split("\n")) {
      const key = skillProseKey(line)
      if (!key) continue
      const cards = prose.get(key) ?? []
      if (!cards.includes(skill)) cards.push(skill)
      prose.set(key, cards)
    }
  }

  for (const role of roles) {
    const text = readFileSync(join(rolesDir, `${role}.md`), "utf8")
    if (!text.trim()) failures.push(`assets: roles/${role}.md is empty`)
    const lineCount = text.split("\n").length
    if (lineCount > MAX_ROLE_LINES) {
      warnings.push(`assets: roles/${role}.md has ${lineCount} lines (soft limit ${MAX_ROLE_LINES})`)
    }
  }

  const duplicates = [...prose.entries()].filter(([, cards]) => cards.length > 1)
  for (const [line, cards] of duplicates.slice(0, MAX_REPORTED_DUPLICATES)) {
    const excerpt = line.length > 80 ? `${line.slice(0, 80)}…` : line
    warnings.push(`assets: repeated prose in ${cards.join(", ")}: "${excerpt}"`)
  }
  if (duplicates.length > MAX_REPORTED_DUPLICATES) {
    warnings.push(`assets: ${duplicates.length - MAX_REPORTED_DUPLICATES} more repeated prose line(s)`)
  }

  return {
    failures,
    warnings,
    summary: `assets: ${skills.length} skills, ${roles.length} role agents, ${duplicates.length} repeated prose line(s)`,
  }
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
  const artifactPath = relative(projectRoot, artifactRoot).split(sep).join("/")
  return text
    .split("\n")
    .some(
      (line) =>
        line.trim() === `${artifactPath}/` || line.trim() === artifactPath || line.trim() === `/${artifactPath}/`,
    )
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

  if (input.assets) {
    const assets = checkAssets(join(input.packageRoot, "src", "resources"))
    if (assets.failures.length === 0) pass(assets.summary)
    else fail(assets.summary)
    for (const failure of assets.failures) fail(failure)
    for (const warning of assets.warnings) warn(warning)
  }

  const stale = staleRuns(input.artifactRoot ? join(input.artifactRoot, "runs") : "", input.options.ttlDays)
  if (stale.length > 0)
    warn(`${stale.length} run(s) older than ${input.options.ttlDays} days; inspect or remove manually`)

  return { ok, lines }
}
