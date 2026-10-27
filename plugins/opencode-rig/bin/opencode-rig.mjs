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
