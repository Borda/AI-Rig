#!/usr/bin/env node
/**
 * opencode-rig CLI.
 *
 * Installs, checks, upgrades, and removes the role agents that OpenCode cannot
 * register from a package, and prints the recommended project configuration.
 * Agent files are hash-stamped so a check can tell our own output from a file
 * the user edited.
 */

import { createHash } from "node:crypto"
import { existsSync, mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs"
import { homedir } from "node:os"
import { dirname, join, resolve } from "node:path"
import { fileURLToPath } from "node:url"

const HERE = dirname(fileURLToPath(import.meta.url))
const PACKAGE_ROOT = resolve(HERE, "..")
const RESOURCES = join(PACKAGE_ROOT, "src", "resources")
const STAMP = /^<!-- opencode-rig: v(\S+) sha256:([a-f0-9]{64}) -->$/

const packageJson = JSON.parse(readFileSync(join(PACKAGE_ROOT, "package.json"), "utf8"))
const VERSION = packageJson.version
const NAME = packageJson.name

const HELP = `Usage: opencode-rig <command> [options]

Commands:
  init            Install the rig-* role agents (default: global)
  check           Verify the installed agents match this package
  upgrade         Alias for init --upgrade
  remove          Remove agent files that carry the opencode-rig stamp
  doctor          Report install and environment health
  print-config    Print the recommended opencode.jsonc snippet
  version         Print the package version

Options:
  --project       Target .opencode/agents in the current directory
  --global        Target the user-wide config directory (default)
  --check         Verify only; never write
  --upgrade       Rewrite stamped files whose content changed
  --force         Overwrite a conflicting agent file (init only)
  --assets        Doctor: also curate shipped skill and role cards
  -h, --help      Show this help
`

function parseArgs(argv) {
  const flags = new Set()
  let command = ""
  for (const arg of argv) {
    if (arg === "-h") flags.add("--help")
    else if (arg.startsWith("--")) flags.add(arg)
    else if (!command) command = arg
    else throw new Error(`unexpected argument: ${arg}`)
  }
  return { command, flags }
}

function configRoot() {
  const override = process.env.OPENCODE_CONFIG_DIR?.trim()
  return override || join(homedir(), ".config", "opencode")
}

function targetDir(flags) {
  if (flags.has("--project")) return join(process.cwd(), ".opencode", "agents")
  return join(configRoot(), "agents")
}

function sha256(value) {
  return createHash("sha256").update(value).digest("hex")
}

function loadAgentSpecs() {
  const manifest = JSON.parse(readFileSync(join(RESOURCES, "agents.json"), "utf8"))
  if (manifest.schema !== 1) throw new Error(`agents.json: unsupported schema ${String(manifest.schema)}`)
  const specs = []
  for (const [name, config] of Object.entries(manifest.agents)) {
    const bodyPath = join(RESOURCES, "roles", `${name}.md`)
    if (!existsSync(bodyPath)) throw new Error(`missing role card: ${bodyPath}`)
    specs.push({ name, config, body: readFileSync(bodyPath, "utf8").trim() })
  }
  return specs
}

function frontmatter(config) {
  const lines = ["---", `description: ${quote(config.description)}`, `mode: ${config.mode}`]
  if (config.steps !== undefined) lines.push(`steps: ${config.steps}`)
  lines.push("permissions:")
  for (const permission of config.permissions ?? []) {
    lines.push(`  - action: ${permission.action}`)
    lines.push(`    resource: ${quote(permission.resource)}`)
    lines.push(`    effect: ${permission.effect}`)
  }
  lines.push("---")
  return lines.join("\n")
}

function quote(value) {
  const text = String(value)
  return /^[A-Za-z0-9][A-Za-z0-9 ._/-]*$/.test(text) ? text : JSON.stringify(text)
}

function renderAgent(spec) {
  const withoutStamp = `${frontmatter(spec.config)}\n\n${spec.body}\n`
  const digest = sha256(withoutStamp)
  const stamped = `${frontmatter(spec.config)}\n<!-- opencode-rig: v${VERSION} sha256:${digest} -->\n\n${spec.body}\n`
  return { content: stamped, digest, withoutStamp }
}

function readStamp(content) {
  for (const line of content.split("\n")) {
    const match = STAMP.exec(line.trim())
    if (match) return { version: match[1], digest: match[2] }
  }
  return undefined
}

function stripStamp(content) {
  return content
    .split("\n")
    .filter((line) => !STAMP.test(line.trim()))
    .join("\n")
}

function install(flags) {
  const dir = targetDir(flags)
  const specs = loadAgentSpecs()
  const checkOnly = flags.has("--check")
  const upgrade = flags.has("--upgrade")
  const force = flags.has("--force")
  let problems = 0
  let changed = 0

  console.log(`opencode-rig ${VERSION}`)
  console.log(`target: ${dir}`)
  console.log(`mode: ${checkOnly ? "check" : upgrade ? "upgrade" : "init"}`)

  if (!checkOnly) mkdirSync(dir, { recursive: true })

  for (const spec of specs) {
    const path = join(dir, `${spec.name}.md`)
    const rendered = renderAgent(spec)
    const exists = existsSync(path)
    const current = exists ? readFileSync(path, "utf8") : undefined
    const stamp = current ? readStamp(current) : undefined

    if (!exists) {
      if (checkOnly) {
        problems += 1
        console.log(`MISSING  ${spec.name}`)
        continue
      }
      writeFileSync(path, rendered.content, "utf8")
      changed += 1
      console.log(`INSTALL  ${spec.name}`)
      continue
    }

    if (!stamp) {
      if (checkOnly) problems += 1
      console.log(`CONFLICT ${spec.name}: file exists without an opencode-rig stamp`)
      if (!checkOnly && force) {
        writeFileSync(path, rendered.content, "utf8")
        changed += 1
        console.log("         overwritten with --force")
      } else if (!checkOnly) {
        problems += 1
        console.log("         left untouched; pass --force to replace it")
      }
      continue
    }

    const digest = sha256(stripStamp(current).replace(/\n$/, "\n"))
    const edited = digest !== rendered.digest
    const versionMatches = stamp.version === VERSION

    if (versionMatches && !edited && current === rendered.content) {
      console.log(`OK       ${spec.name}`)
      continue
    }

    if (edited && versionMatches && !upgrade && !force) {
      problems += 1
      console.log(`CONFLICT ${spec.name}: stamped file was edited after install`)
      console.log("         pass --upgrade to restore the packaged role card, or edit the package instead")
      continue
    }

    console.log(
      `STALE    ${spec.name}: stamped v${stamp.version}${edited ? ", content differs from this package" : ""}`,
    )
    if (checkOnly) {
      problems += 1
      continue
    }
    writeFileSync(path, rendered.content, "utf8")
    changed += 1
    console.log(`         updated to v${VERSION}`)
  }

  const extra = existsSync(dir)
    ? readdirSync(dir)
        .filter((file) => file.endsWith(".md"))
        .filter((file) => !specs.some((spec) => `${spec.name}.md` === file))
        .filter((file) => {
          const content = readFileSync(join(dir, file), "utf8")
          return readStamp(content) !== undefined
        })
    : []
  for (const file of extra) {
    console.log(`EXTRA    ${file}: stamped by opencode-rig but not in this package (remove manually)`)
  }

  if (checkOnly && problems > 0) {
    console.log(
      `\n${problems} problem(s). Run: opencode-rig init${flags.has("--project") ? " --project" : ""} --upgrade`,
    )
    return 1
  }
  if (!checkOnly) console.log(`\n${changed} file(s) written, ${problems} problem(s).`)
  return problems > 0 ? 1 : 0
}

function remove(flags) {
  const dir = targetDir(flags)
  if (!existsSync(dir)) {
    console.log(`opencode-rig: nothing to remove (${dir} does not exist)`)
    return 0
  }
  let removed = 0
  let skipped = 0
  for (const file of readdirSync(dir).filter((entry) => entry.endsWith(".md"))) {
    const path = join(dir, file)
    const content = readFileSync(path, "utf8")
    if (readStamp(content)) {
      rmSync(path)
      removed += 1
      console.log(`removed  ${file}`)
    } else {
      skipped += 1
      console.log(`kept     ${file} (no opencode-rig stamp)`)
    }
  }
  console.log(`\n${removed} removed, ${skipped} kept.`)
  return 0
}

const MAX_SKILL_LINES = 160
const MAX_ROLE_LINES = 80
const DUPLICATE_MIN_LENGTH = 60
const MAX_REPORTED_DUPLICATES = 3

function rosterDiff(expected, actual) {
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

function assetReport() {
  const rig = JSON.parse(readFileSync(join(PACKAGE_ROOT, "rig.json"), "utf8"))
  const failures = []
  const warnings = []
  const skillsDir = join(RESOURCES, "skills")
  const rolesDir = join(RESOURCES, "roles")
  const skills = readdirSync(skillsDir, { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .map((entry) => entry.name)
    .sort()
  const roles = readdirSync(rolesDir)
    .filter((file) => file.endsWith(".md"))
    .map((file) => file.slice(0, -3))
    .sort()
  const agents = Object.keys(JSON.parse(readFileSync(join(RESOURCES, "agents.json"), "utf8")).agents).sort()

  const compare = (label, expected, actual) => {
    const diff = rosterDiff(expected, actual)
    if (diff) failures.push(`assets: ${label} do not match shipped files (${diff})`)
  }
  compare("skills", rig.skills, skills)
  compare("agents", rig.agents, agents)
  const roleDiff = rosterDiff(agents, roles)
  if (roleDiff) failures.push(`assets: role files do not match agents.json (${roleDiff})`)

  const prose = new Map()
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
      const key = line.trim()
      if (key.length < DUPLICATE_MIN_LENGTH || /^[>#|`]/.test(key)) continue
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

function doctor(flags) {
  const dir = targetDir(flags)
  const specs = loadAgentSpecs()
  const lines = []
  let ok = true
  const pass = (text) => lines.push(`PASS  ${text}`)
  const fail = (text) => {
    ok = false
    lines.push(`FAIL  ${text}`)
  }

  lines.push(`opencode-rig ${VERSION}`)
  lines.push(`node ${process.version}`)
  lines.push(`package ${PACKAGE_ROOT}`)
  if (existsSync(join(RESOURCES, "agents.json"))) pass("shipped resources present")
  else fail("shipped resources are missing; reinstall the package")

  if (!existsSync(dir)) fail(`agents: ${dir} does not exist (run opencode-rig init)`)
  else {
    for (const spec of specs) {
      const path = join(dir, `${spec.name}.md`)
      if (!existsSync(path)) {
        fail(`agents: ${spec.name} is missing`)
        continue
      }
      const stamp = readStamp(readFileSync(path, "utf8"))
      if (!stamp) fail(`agents: ${spec.name} has no opencode-rig stamp`)
      else if (stamp.version !== VERSION) fail(`agents: ${spec.name} is v${stamp.version}, expected v${VERSION}`)
      else pass(`agents: ${spec.name} v${stamp.version}`)
    }
  }

  if (flags.has("--assets")) {
    const assets = assetReport()
    if (assets.failures.length === 0) pass(assets.summary)
    else fail(assets.summary)
    for (const failure of assets.failures) fail(failure)
    for (const warning of assets.warnings) lines.push(`WARN  ${warning}`)
  }

  const configPath = join(configRoot(), "opencode.jsonc")
  const configJson = join(configRoot(), "opencode.json")
  if (existsSync(configPath) || existsSync(configJson)) pass("global OpenCode config exists")
  else lines.push("INFO  no global opencode.json(c); add the plugin with: opencode plugin add " + NAME)

  const gitignore = join(process.cwd(), ".gitignore")
  if (
    existsSync(gitignore) &&
    readFileSync(gitignore, "utf8")
      .split("\n")
      .some((line) => line.trim() === ".opencode-rig/")
  ) {
    pass("artifact root is gitignored")
  } else lines.push("WARN  add '.opencode-rig/' to .gitignore")

  console.log([ok ? "opencode-rig doctor: healthy" : "opencode-rig doctor: problems found", ...lines].join("\n"))
  return ok ? 0 : 1
}

function printConfig() {
  const snippet = {
    plugins: [{ package: NAME, options: { guardrails: "standard" } }],
    permissions: [
      { action: "shell", resource: "git push *", effect: "ask" },
      { action: "shell", resource: "git reset --hard*", effect: "ask" },
      { action: "shell", resource: "git clean*", effect: "ask" },
      { action: "shell", resource: "git rebase*", effect: "ask" },
    ],
  }
  console.log(JSON.stringify(snippet, null, 2))
  console.log(
    "\n// Merge into opencode.jsonc. Plain local commits are governed by the guardrail hook:\n// with no grant it escalates to ask, and a recorded grant leaves your configured effect alone.",
  )
  return 0
}

function main(argv) {
  let parsed
  try {
    parsed = parseArgs(argv)
  } catch (error) {
    console.error(`opencode-rig: ${error.message}`)
    return 2
  }
  const { command, flags } = parsed
  if (flags.has("--help") || command === "help" || command === "") {
    console.log(HELP)
    return 0
  }
  try {
    if (command === "version" || command === "--version") {
      console.log(VERSION)
      return 0
    }
    if (command === "init" || command === "upgrade") {
      if (command === "upgrade") flags.add("--upgrade")
      return install(flags)
    }
    if (command === "check") return install(new Set([...flags, "--check"]))
    if (command === "remove") return remove(flags)
    if (command === "doctor") return doctor(flags)
    if (command === "print-config") return printConfig()
    console.error(`opencode-rig: unknown command '${command}'`)
    console.error(HELP)
    return 2
  } catch (error) {
    console.error(`opencode-rig: ${error instanceof Error ? error.message : String(error)}`)
    return 1
  }
}

process.exitCode = main(process.argv.slice(2))
