#!/usr/bin/env node
/**
 * opencode-rig CLI.
 *
 * Installs, checks, upgrades, and removes the role agents that OpenCode cannot
 * register from a package, and prints the recommended project configuration.
 * Agent files are hash-stamped so a check can tell our own output from a file
 * the user edited.
 */

import { spawnSync } from "node:child_process"
import { createHash } from "node:crypto"
import { existsSync, lstatSync, mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs"
import { homedir } from "node:os"
import { delimiter, dirname, isAbsolute, join, relative, resolve, sep } from "node:path"
import { fileURLToPath } from "node:url"
import { applyEdits, modify, parse } from "jsonc-parser"

const HERE = dirname(fileURLToPath(import.meta.url))
const PACKAGE_ROOT = resolve(HERE, "..")
const RESOURCES = join(PACKAGE_ROOT, "src", "resources")
const STAMP = /^<!-- opencode-rig: v(\S+) sha256:([a-f0-9]{64}) -->$/
const BLOCK_BEGIN_PREFIX = "<!-- opencode-rig:delivery-principles begin sha256="
const BLOCK_BEGIN_PATTERN = /<!-- opencode-rig:delivery-principles begin sha256=([a-f0-9]{64}) -->\r?\n/
const BLOCK_END = "<!-- opencode-rig:delivery-principles end -->"
const MAX_CLAUDE_INSTRUCTIONS_BYTES = 1024 * 1024
const INSTRUCTIONS_MODE = 0o600

const packageJson = JSON.parse(readFileSync(join(PACKAGE_ROOT, "package.json"), "utf8"))
const VERSION = packageJson.version
const NAME = packageJson.name

const HELP = `Usage: opencode-rig <command> [options]

Commands:
  setup           Install the rig-* role agents, merge the shipped permission rules,
                  and write the global delivery-principles instructions block (recommended)
  init            Alias for setup
  check           Verify the installed agents and report the permission and instructions state
  upgrade         Alias for setup --upgrade
  remove          Remove agent files and the global instructions block that carry an opencode-rig stamp
  doctor          Report install and environment health (install checks only)
  print-config    Print the recommended opencode.jsonc snippet without writing it
  version         Print the package version

Options:
  --project       Target .opencode/agents and opencode.json(c) at the Git root (cwd outside Git)
  --global        Target the user-wide config directory (default)
  --check         Verify only; never write
  --upgrade       Rewrite stamped files whose content changed
  --force         Overwrite a conflicting agent file (setup only)
  --adopt-claude-instructions
                  Setup: copy ~/.claude/CLAUDE.md into the global OpenCode instructions file
                  so the existing global Claude instructions keep applying
  --agents-only   Install or verify the role agents only; skip the permission merge and the
                  global delivery-principles instructions block
  --assets        Doctor: also curate shipped skill and role cards
  --version       Print the package version
  -h, --help      Show this help
`

const SETUP_FLAGS = [
  "--project",
  "--global",
  "--check",
  "--upgrade",
  "--force",
  "--adopt-claude-instructions",
  "--agents-only",
]
const COMMAND_FLAGS = {
  setup: SETUP_FLAGS,
  init: SETUP_FLAGS,
  upgrade: SETUP_FLAGS,
  check: ["--project", "--global", "--agents-only", "--adopt-claude-instructions"],
  remove: ["--project", "--global"],
  doctor: ["--project", "--global", "--assets"],
  "print-config": [],
  version: [],
  help: [],
}

function parseArgs(argv) {
  const flags = new Set()
  let command = ""
  for (const arg of argv) {
    if (arg === "-h") flags.add("--help")
    else if (arg === "--version" && !command) command = "version"
    else if (arg.startsWith("-")) flags.add(arg)
    else if (!command) command = arg
    else throw new Error(`unexpected argument: ${arg}`)
  }
  const allowed = Object.hasOwn(COMMAND_FLAGS, command) ? COMMAND_FLAGS[command] : []
  for (const flag of flags) {
    if (flag !== "--help" && !allowed.includes(flag)) {
      throw new Error(`unknown option '${flag}' for ${command || "help"}`)
    }
  }
  return { command, flags }
}

function configRoot() {
  const override = process.env.OPENCODE_CONFIG_DIR?.trim()
  return override || join(homedir(), ".config", "opencode")
}

/**
 * The home directory that anchors the global OpenCode instruction file.
 *
 * OpenCode reads its global rules from the literal `~/.config/opencode/AGENTS.md` and ignores
 * `OPENCODE_CONFIG_DIR` for that path, so the home is a separate seam. `OPENCODE_RIG_HOME` is a
 * test-only override: it lets the suite point setup at a disposable directory without touching
 * the developer's home. It fails closed when the directory does not exist, so a wrong value can
 * never make setup test one home and then create another.
 */
function homeDir() {
  const override = process.env.OPENCODE_RIG_HOME?.trim()
  if (!override) return homedir()
  if (!existsSync(override)) {
    throw new Error(`OPENCODE_RIG_HOME points at a directory that does not exist: ${override}`)
  }
  return override
}

function globalInstructionsPath() {
  return join(homeDir(), ".config", "opencode", "AGENTS.md")
}

/** A label for a failure that may itself come from an unusable home override. */
function instructionsPathLabel() {
  try {
    return globalInstructionsPath()
  } catch {
    return "GLOBAL instructions"
  }
}

function claudeInstructionsPath() {
  return join(homeDir(), ".claude", "CLAUDE.md")
}

/**
 * Refuse a target whose parent chain leaves the home directory or passes through a symlink.
 *
 * lstat on the leaf alone is not enough: `mkdir` and `writeFile` follow a symlinked directory,
 * so a link planted above the target lands the privileged write somewhere else.
 */
function assertSafeTarget(path, root = homeDir()) {
  const base = resolve(root)
  const parent = resolve(dirname(path))
  const rel = relative(base, parent)
  if (rel === ".." || rel.startsWith(`..${sep}`) || isAbsolute(rel)) {
    throw new Error(`target is outside the base directory: ${parent}`)
  }
  let current = base
  for (const part of ["", ...(rel === "" ? [] : rel.split(sep))]) {
    current = join(current, part)
    let stats
    try {
      stats = lstatSync(current)
    } catch {
      // The rest of the chain does not exist yet and will be created normally.
      break
    }
    if (stats.isSymbolicLink()) throw new Error(`${current} is a symlink; refusing to write through it`)
    if (!stats.isDirectory()) throw new Error(`${current} is not a directory; refusing to write through it`)
  }
  if (targetKind(path) === "symlink") throw new Error(`${path} is a symlink; refusing to write through it`)
}

/** Classify a target path without following a final symlink. */
function targetKind(path) {
  try {
    const stats = lstatSync(path)
    if (stats.isSymbolicLink()) return "symlink"
    return stats.isFile() ? "file" : "other"
  } catch {
    return "missing"
  }
}

function countOccurrences(text, needle) {
  let count = 0
  let index = text.indexOf(needle)
  while (index !== -1) {
    count += 1
    index = text.indexOf(needle, index + needle.length)
  }
  return count
}

/** Locate the single authenticated managed block, if the target carries one. */
function findManagedBlock(text) {
  const begins = countOccurrences(text, BLOCK_BEGIN_PREFIX)
  const ends = countOccurrences(text, BLOCK_END)
  if (begins === 0 && ends === 0) return { state: "absent" }
  if (begins !== 1 || ends !== 1) return { state: "malformed" }
  const match = BLOCK_BEGIN_PATTERN.exec(text)
  if (!match) return { state: "malformed" }
  const bodyStart = match.index + match[0].length
  const endIndex = text.indexOf(BLOCK_END, bodyStart)
  if (endIndex < 0) return { state: "malformed" }
  const body = text.slice(bodyStart, endIndex)
  if (sha256(body.replace(/\r\n/g, "\n")) !== match[1]) return { state: "modified" }
  const afterEnd = endIndex + BLOCK_END.length
  const newline = /^\r?\n/.exec(text.slice(afterEnd))?.[0] ?? ""
  return {
    state: "present",
    start: match.index,
    end: afterEnd + newline.length,
    body,
  }
}

/**
 * The shipped delivery-principles text as the managed block body.
 *
 * The body carries no plugin version and no install path, so a release bump never rewrites the
 * user's file and running setup twice is byte-identical.
 */
function principlesBody() {
  const path = join(RESOURCES, "references", "delivery-principles.md")
  const text = readFileSync(path, "utf8").replace(/\r\n/g, "\n")
  const body = text.endsWith("\n") ? text : `${text}\n`
  if (body.trim() === "") throw new Error(`delivery principles are empty: ${path}`)
  if (body.includes(BLOCK_BEGIN_PREFIX) || body.includes(BLOCK_END)) {
    throw new Error(`delivery principles contain the managed-block markers: ${path}`)
  }
  return body
}

function managedBlock(body) {
  return `${BLOCK_BEGIN_PREFIX}${sha256(body)} -->\n${body}${BLOCK_END}\n`
}

/**
 * Read the global Claude instructions for adoption.
 *
 * Returns a state instead of following a symlink or reading an unbounded file: a symlink would
 * launder another file's bytes into the OpenCode rules, and an oversized file would be copied
 * into a privileged global write. Callers decide whether the state is a refusal.
 */
function readClaudeInstructions() {
  const path = claudeInstructionsPath()
  let stats
  try {
    stats = lstatSync(path)
  } catch {
    return { state: "missing", path }
  }
  if (stats.isSymbolicLink()) return { state: "refused", path, reason: "it is a symlink" }
  if (!stats.isFile()) return { state: "refused", path, reason: "it is not a regular file" }
  if (stats.size > MAX_CLAUDE_INSTRUCTIONS_BYTES) {
    return { state: "refused", path, reason: `it is larger than ${MAX_CLAUDE_INSTRUCTIONS_BYTES} bytes` }
  }
  return { state: "file", path, text: readFileSync(path, "utf8") }
}

/** Append adopted Claude content to a post-block body without losing its newline boundary. */
function withAdopted(body, adopted) {
  if (adopted === "") return body
  const found = findManagedBlock(body)
  const suffix = found.state === "present" ? body.slice(found.end) : body
  if (suffix.includes(adopted)) return body
  const separator = body === "" || body.endsWith("\n") ? "" : "\n"
  return `${body}${separator}${adopted}`
}

/**
 * Write the managed block into the global OpenCode instructions file.
 *
 * Returns the number of problems for the caller's aggregate exit status. Every refusal leaves
 * the user's bytes untouched: OpenCode reads this file instead of `~/.claude/CLAUDE.md`, so a
 * refused write must never silently stop the user's existing global instructions from applying.
 */
function installGlobalInstructions(flags) {
  const path = globalInstructionsPath()
  assertSafeTarget(path)
  const checkOnly = flags.has("--check")
  const adopt = flags.has("--adopt-claude-instructions")
  const claude = readClaudeInstructions()
  const kind = targetKind(path)

  if (kind === "symlink" || kind === "other") {
    const reason = kind === "symlink" ? "the target is a symlink" : "the target is not a regular file"
    console.log(`GLOBAL   ${path}: conflict: ${reason}; refusing to write`)
    return 1
  }

  if (adopt && claude.state === "refused") {
    console.log(`GLOBAL   ${path}: conflict: ${claude.path} cannot be adopted because ${claude.reason}`)
    console.log("         refusing to write")
    return 1
  }

  const adoptedText = claude.state === "file" ? claude.text : ""

  if (kind === "missing") {
    if (claude.state !== "missing" && !adopt) {
      const detail = `${claude.path} exists and OpenCode reads this file instead of it`
      if (checkOnly) {
        console.log(`GLOBAL   ${path}: conflict: ${detail}`)
        console.log(
          "         creating it would stop your global Claude instructions from applying; pass --adopt-claude-instructions to copy them in",
        )
        return 1
      }
      // A decision only the user can make. Report it loudly and let the rest of the install
      // finish, so a routine sync is not red for a choice this step cannot take on their behalf.
      console.log(`WARN     ${path}: skipped, ${detail}`)
      console.log(
        "         creating it would stop your global Claude instructions from applying; run `opencode-rig setup --adopt-claude-instructions` to copy them in",
      )
      return 0
    }
    if (checkOnly) {
      console.log(`GLOBAL   ${path}: absent (run setup)`)
      return 1
    }
    mkdirSync(dirname(path), { recursive: true })
    writeFileSync(path, `${managedBlock(principlesBody())}${adoptedText}`, {
      encoding: "utf8",
      mode: INSTRUCTIONS_MODE,
    })
    console.log(`GLOBAL   ${path}: created${adoptedText === "" ? "" : " and adopted the global Claude instructions"}`)
    return 0
  }

  const existing = readFileSync(path, "utf8")
  const found = findManagedBlock(existing)
  if (found.state === "malformed" || found.state === "modified") {
    const reason =
      found.state === "modified" ? "the managed block was edited by hand" : "the managed markers are malformed"
    console.log(`GLOBAL   ${path}: conflict: ${reason}; refusing to write`)
    return 1
  }

  if (found.state === "absent") {
    if (checkOnly) {
      console.log(`GLOBAL   ${path}: absent (run setup to append the delivery-principles block)`)
      return 1
    }
    // Setup inserts exactly one newline before an appended block, and remove strips exactly that
    // one, so a file without a final newline gets its exact bytes back.
    const separator = existing === "" ? "" : "\n"
    const updated = withAdopted(`${existing}${separator}${managedBlock(principlesBody())}`, adopt ? adoptedText : "")
    writeFileSync(path, updated, "utf8")
    console.log(
      `GLOBAL   ${path}: appended the delivery-principles block${adopt ? " and the global Claude instructions" : ""}`,
    )
    return 0
  }

  const newline = existing.slice(found.start, found.end).includes("\r\n") ? "\r\n" : "\n"
  const replacement = managedBlock(principlesBody()).replace(/\n/g, newline)
  const base = `${existing.slice(0, found.start)}${replacement}${existing.slice(found.end)}`
  // An existing block must not make the opt-in a no-op: a missing or changed Claude copy is
  // appended after the user's content instead of being silently ignored.
  const updated = withAdopted(base, adopt ? adoptedText : "")
  if (updated === existing) {
    console.log(`GLOBAL   ${path}: delivery-principles block current`)
    return 0
  }
  if (checkOnly) {
    console.log(`GLOBAL   ${path}: stale delivery-principles block (run setup)`)
    return 1
  }
  writeFileSync(path, updated, "utf8")
  console.log(`GLOBAL   ${path}: updated the delivery-principles block`)
  return 0
}

/** Strip the managed block, restoring the user's bytes and deleting a file setup created. */
function removeGlobalInstructions() {
  const path = globalInstructionsPath()
  try {
    assertSafeTarget(path)
  } catch (error) {
    console.log(`GLOBAL   ${path}: left untouched (${error.message})`)
    return
  }
  const kind = targetKind(path)
  if (kind === "missing") {
    console.log(`GLOBAL   ${path}: no managed block`)
    return
  }
  if (kind !== "file") {
    console.log(`GLOBAL   ${path}: left untouched (${kind === "symlink" ? "symlink" : "not a regular file"})`)
    return
  }
  const existing = readFileSync(path, "utf8")
  const found = findManagedBlock(existing)
  if (found.state === "absent") {
    console.log(`GLOBAL   ${path}: no managed block`)
    return
  }
  if (found.state === "malformed" || found.state === "modified") {
    console.log(`GLOBAL   ${path}: left untouched (the managed block was edited by hand)`)
    return
  }
  let prefix = existing.slice(0, found.start)
  let suffix = existing.slice(found.end)
  // Setup inserts exactly one newline before an appended block; strip only that byte.
  if (prefix.endsWith("\r\n")) prefix = prefix.slice(0, -2)
  else if (prefix.endsWith("\n")) prefix = prefix.slice(0, -1)
  const claude = readClaudeInstructions()
  const adopted = claude.state === "file" ? claude.text : undefined
  if (adopted !== undefined && suffix === adopted) suffix = ""
  const updated = `${prefix}${suffix}`
  if (updated.trim() === "" || (adopted !== undefined && updated === adopted)) {
    rmSync(path)
    console.log(`GLOBAL   ${path}: removed the file setup created`)
    return
  }
  writeFileSync(path, updated, "utf8")
  console.log(`GLOBAL   ${path}: removed the delivery-principles block`)
}

/** True when an executable with this name resolves on PATH. */
function commandExists(name) {
  const extensions = process.platform === "win32" ? (process.env.PATHEXT ?? ".EXE;.CMD;.BAT;.COM").split(";") : [""]
  for (const directory of (process.env.PATH ?? "").split(delimiter)) {
    if (!directory) continue
    for (const extension of extensions) {
      if (existsSync(join(directory, `${name}${extension}`))) return true
      if (existsSync(join(directory, `${name}${extension.toLowerCase()}`))) return true
    }
  }
  return false
}

/** Resolve the project the host discovers without accepting Git environment redirects. */
function projectRoot() {
  const env = { ...process.env }
  for (const key of Object.keys(env)) {
    if (key.startsWith("GIT_")) delete env[key]
  }
  const result = spawnSync("git", ["rev-parse", "--show-toplevel"], { encoding: "utf8", env })
  return result.status === 0 && result.stdout.trim() ? result.stdout.trim() : process.cwd()
}

function targetDir(flags) {
  if (flags.has("--project")) return join(projectRoot(), ".opencode", "agents")
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

/**
 * Render an agent frontmatter block.
 *
 * The agent's own rules come first, then the shipped ruleset in its shipped order.
 * The host resolves the last matching rule and appends agent rules after the global
 * ones, so the shared rules have to land after the agent's own `shell: *` catch-all
 * to take effect; anything the ruleset does not name still asks. The shipped order is
 * kept so the `--output` asks stay after the read-only allows they override.
 */
function frontmatter(config, sharedRules) {
  const lines = ["---", `description: ${quote(config.description)}`, `mode: ${config.mode}`]
  if (config.steps !== undefined) lines.push(`steps: ${config.steps}`)
  lines.push("permissions:")
  for (const permission of config.permissions ?? []) {
    lines.push(`  - action: ${permission.action}`)
    lines.push(`    resource: ${quote(permission.resource)}`)
    lines.push(`    effect: ${permission.effect}`)
  }
  for (const rule of sharedRules) {
    lines.push(`  - action: ${rule.action}`)
    lines.push(`    resource: ${quote(rule.resource)}`)
    lines.push(`    effect: ${rule.effect}`)
  }
  lines.push("---")
  return lines.join("\n")
}

function quote(value) {
  const text = String(value)
  return /^[A-Za-z0-9][A-Za-z0-9 ._/-]*$/.test(text) ? text : JSON.stringify(text)
}

function renderAgent(spec, sharedRules) {
  const withoutStamp = `${frontmatter(spec.config, sharedRules)}\n\n${spec.body}\n`
  const digest = sha256(withoutStamp)
  const stamped = `${frontmatter(spec.config, sharedRules)}\n<!-- opencode-rig: v${VERSION} sha256:${digest} -->\n\n${spec.body}\n`
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
  const base = flags.has("--project") ? projectRoot() : configRoot()
  assertSafeTarget(dir, base)
  const specs = loadAgentSpecs()
  // One ruleset drives both the merged configuration and the rules every agent carries.
  const shipped = loadPermissions()
  const checkOnly = flags.has("--check")
  const upgrade = flags.has("--upgrade")
  const force = flags.has("--force")
  const agentsOnly = flags.has("--agents-only")
  let problems = 0
  let changed = 0

  console.log(`opencode-rig ${VERSION}`)
  console.log(`target: ${dir}`)
  console.log(`mode: ${checkOnly ? "check" : upgrade ? "upgrade" : "init"}`)

  if (!checkOnly) mkdirSync(dir, { recursive: true })

  for (const spec of specs) {
    const path = join(dir, `${spec.name}.md`)
    try {
      assertSafeTarget(path, base)
    } catch (error) {
      problems += 1
      console.log(`CONFLICT ${spec.name}: ${error.message}`)
      continue
    }
    const rendered = renderAgent(spec, shipped)
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
    // The stamp records the body digest captured at install time, so a mismatch against it is a
    // local edit. Comparing against the package instead reported every shipped card revision as
    // a user edit, which failed a routine sync; a package-only change is safe to apply.
    const userEdited = digest !== stamp.digest
    const packageMoved = rendered.digest !== stamp.digest

    if (!userEdited && current === rendered.content) {
      console.log(`OK       ${spec.name}`)
      continue
    }

    if (userEdited && !upgrade && !force) {
      problems += 1
      console.log(`CONFLICT ${spec.name}: stamped file was edited after install`)
      console.log("         pass --upgrade to restore the packaged role card, or edit the package instead")
      continue
    }

    console.log(
      `STALE    ${spec.name}: stamped v${stamp.version}${packageMoved ? ", content differs from this package" : ""}`,
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
        .filter((file) => targetKind(join(dir, file)) === "file")
        .filter((file) => !specs.some((spec) => `${spec.name}.md` === file))
        .filter((file) => {
          const content = readFileSync(join(dir, file), "utf8")
          return readStamp(content) !== undefined
        })
    : []
  for (const file of extra) {
    console.log(`EXTRA    ${file}: stamped by opencode-rig but not in this package (remove manually)`)
  }

  // The approval rules ship inside the package so they can be reviewed before install;
  // merging them here is what makes them take effect without hand-editing a config file.
  // Reported separately from the agent counts above, which describe role cards only.
  // `check` only reports the permission state: a user who read the guide and declined the
  // merge has not broken anything, so the report never becomes a failure of its own.
  // `--agents-only` stops before this stage so `make sync-opencode` can install agents and
  // leave the configuration and global instructions to the stage that owns their refusals.
  if (!agentsOnly) {
    const configPath = configTarget(flags)
    try {
      assertSafeTarget(configPath, base)
      if (!existsSync(configPath)) {
        if (checkOnly) {
          console.log(`PERMS    ${configPath}: not merged (run init)`)
        } else {
          const initial = `${JSON.stringify({ $schema: "https://opencode.ai/config.json", permissions: shipped }, null, 2)}\n`
          mkdirSync(dirname(configPath), { recursive: true })
          writeFileSync(configPath, initial, "utf8")
          console.log(`PERMS    created ${configPath} with ${shipped.length} rule(s)`)
          reportRules(shipped)
        }
      } else {
        const result = mergePermissions(readFileSync(configPath, "utf8"), shipped)
        if (result.state === "legacy") {
          console.log(`PERMS    ${configPath}: uses the deprecated 'permission' key, left untouched`)
          console.log("         merge manually with: opencode-rig print-config")
        } else if (result.state === "present") {
          console.log(`PERMS    ${configPath}: shipped rules already present`)
        } else if (checkOnly) {
          console.log(`PERMS    ${configPath}: ${result.added} shipped rule(s) missing (run init)`)
        } else {
          writeFileSync(configPath, result.text, "utf8")
          console.log(`PERMS    merged ${result.added} rule(s) into ${configPath}`)
          reportRules(result.rules)
        }
      }
    } catch (error) {
      problems += 1
      console.log(`CONFIG   ${configPath}: ${error.message}`)
    }
  }

  // The managed block is global-only: `--project` must neither write it nor require it.
  if (!agentsOnly && !flags.has("--project")) {
    try {
      problems += installGlobalInstructions(flags)
    } catch (error) {
      problems += 1
      console.log(`GLOBAL   ${instructionsPathLabel()}: ${error.message}`)
    }
  }

  if (checkOnly && problems > 0) {
    console.log(
      `\n${problems} problem(s). Run: opencode-rig setup${flags.has("--project") ? " --project" : ""} --upgrade`,
    )
    return 1
  }
  if (!checkOnly) console.log(`\n${changed} file(s) written, ${problems} problem(s).`)
  return problems > 0 ? 1 : 0
}

function remove(flags) {
  const dir = targetDir(flags)
  const base = flags.has("--project") ? projectRoot() : configRoot()
  assertSafeTarget(dir, base)
  let removed = 0
  let skipped = 0
  if (existsSync(dir)) {
    for (const file of readdirSync(dir).filter((entry) => entry.endsWith(".md"))) {
      const path = join(dir, file)
      assertSafeTarget(path, base)
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
  } else {
    console.log(`opencode-rig: no agent files to remove (${dir} does not exist)`)
  }
  console.log(`\n${removed} removed, ${skipped} kept.`)
  // The managed block lives outside the agents directory, so an absent directory never strands it.
  if (!flags.has("--project")) removeGlobalInstructions()
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
  const base = flags.has("--project") ? projectRoot() : configRoot()
  assertSafeTarget(dir, base)
  const specs = loadAgentSpecs()
  const lines = []
  let ok = true
  const pass = (text) => lines.push(`PASS  ${text}`)
  const fail = (text) => {
    ok = false
    lines.push(`FAIL  ${text}`)
  }

  lines.push(`opencode-rig ${VERSION}`)
  lines.push("INFO  install checks only; use rig_doctor for full project and runtime health")
  lines.push("INFO  --assets checks shipped files only; rig_doctor also checks the runtime skill manifest")
  lines.push(`node ${process.version}`)
  lines.push(`package ${PACKAGE_ROOT}`)
  if (commandExists("rtk")) {
    lines.push("RTK   ok: rtk is on PATH; optionally prefix read-heavy commands with it to reduce output")
  } else {
    lines.push("RTK   warn: rtk is not on PATH (optional); it can reduce read-heavy command output")
  }
  if (existsSync(join(RESOURCES, "agents.json"))) pass("shipped resources present")
  else fail("shipped resources are missing; reinstall the package")

  if (!existsSync(dir)) fail(`agents: ${dir} does not exist (run opencode-rig setup)`)
  else {
    for (const spec of specs) {
      const path = join(dir, `${spec.name}.md`)
      assertSafeTarget(path, base)
      if (!existsSync(path)) {
        fail(`agents: ${spec.name} is missing`)
        continue
      }
      const content = readFileSync(path, "utf8")
      const stamp = readStamp(content)
      if (!stamp) fail(`agents: ${spec.name} has no opencode-rig stamp`)
      else if (stamp.version !== VERSION) fail(`agents: ${spec.name} is v${stamp.version}, expected v${VERSION}`)
      else if (sha256(stripStamp(content)) !== stamp.digest) fail(`agents: ${spec.name} has local edits`)
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

  const root = projectRoot()
  const artifacts = configuredArtifacts(root)
  const artifactPath = relative(root, resolve(root, artifacts)).split(sep).join("/") + "/"
  const ignored = spawnSync(
    "git",
    ["-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false", "check-ignore", "-q", "--", artifactPath],
    {
      cwd: root,
      encoding: "utf8",
      timeout: 5000,
      env: Object.fromEntries(Object.entries(process.env).filter(([key]) => !key.toUpperCase().startsWith("GIT_"))),
    },
  )
  if (ignored.status === 0) pass(`artifact root is gitignored: ${artifactPath}`)
  else lines.push(`WARN  add '${artifactPath}' to .gitignore`)

  console.log([ok ? "opencode-rig doctor: healthy" : "opencode-rig doctor: problems found", ...lines].join("\n"))
  return ok ? 0 : 1
}

/** Read the artifact option from global configuration, then project overrides. */
function configuredArtifacts(root) {
  let artifacts = ".opencode-rig"
  for (const directory of [configRoot(), root]) {
    const path = ["opencode.jsonc", "opencode.json"].map((name) => join(directory, name)).find(existsSync)
    if (!path) continue
    const errors = []
    const config = parse(readFileSync(path, "utf8"), errors, { allowTrailingComma: true, disallowComments: false })
    if (errors.length) throw new Error(`invalid OpenCode configuration: ${path}`)
    for (const plugin of config?.plugins ?? []) {
      if (
        typeof plugin?.package === "string" &&
        /^opencode-rig(?:@|$)/.test(plugin.package) &&
        plugin.options?.artifacts !== undefined
      ) {
        if (typeof plugin.options.artifacts !== "string" || !plugin.options.artifacts.trim())
          throw new Error("artifacts must be a non-empty string")
        artifacts = plugin.options.artifacts
      }
    }
  }
  const rel = relative(root, resolve(root, artifacts))
  if (rel === ".." || rel.startsWith(`..${sep}`) || isAbsolute(rel))
    throw new Error("artifacts must stay inside the project")
  return artifacts
}

/** Read the shipped ruleset: the single source for `print-config` and the `init` merge. */
function loadPermissions() {
  const manifest = JSON.parse(readFileSync(join(RESOURCES, "permissions.json"), "utf8"))
  if (manifest.schema !== 1) throw new Error(`permissions.json: unsupported schema ${String(manifest.schema)}`)
  if (!Array.isArray(manifest.permissions)) throw new Error("permissions.json: permissions must be an array")
  for (const rule of manifest.permissions) {
    if (typeof rule?.action !== "string" || typeof rule?.resource !== "string" || typeof rule?.effect !== "string") {
      throw new Error("permissions.json: every rule needs string action, resource, and effect")
    }
    if (!["allow", "ask", "deny"].includes(rule.effect)) {
      throw new Error(`permissions.json: unknown effect ${JSON.stringify(rule.effect)}`)
    }
  }
  return manifest.permissions
}

/** A rule is identified by its action and resource so a user-chosen effect is never replaced. */
function ruleKey(rule) {
  return `${rule.action} ${rule.resource}`
}

/** The shipped recovery allows, which the `git rebase` family rule would otherwise shadow. */
const RECOVERY_RESOURCES = ["git rebase --abort", "git rebase --continue", "git rebase --skip", "git rebase --quit"]

/**
 * True when a rule governs the whole `git rebase` family.
 *
 * The command prefix decides, so `git rebase`, `git rebase*`, `git rebase --abort`, and
 * `git rebase -i` all belong to the family; a trailing wildcard on that prefix does not
 * change it, and a different command such as `git push` does not belong to it.
 */
function governsRebaseFamily(rule) {
  const prefix = rule.resource.trim().split(/\s+/).slice(0, 2).join(" ").replace(/\*+$/, "")
  return prefix === "git rebase"
}

/**
 * Insert the shipped rules into a JSONC configuration without disturbing what is already there.
 *
 * Shipped rules go first: OpenCode resolves the last matching rule, so any rule the user wrote
 * keeps the final say. Only rules whose action and resource are absent are added, which keeps the
 * merge idempotent and leaves a user's stricter effect untouched.
 */
function mergePermissions(text, shipped) {
  const errors = []
  const parsed = parse(text, errors, { allowTrailingComma: true, disallowComments: false })
  if (errors.length > 0) throw new Error(`configuration has ${errors.length} JSONC error(s); fix them before merging`)
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("configuration must be a JSON object")
  }
  // The deprecated singular key and the ordered list cannot both be honored without guessing precedence.
  if (Object.hasOwn(parsed, "permission")) return { text, state: "legacy", added: 0 }
  const existing = parsed.permissions
  if (existing !== undefined && !Array.isArray(existing)) {
    throw new Error("configuration: 'permissions' must be an array of rules")
  }
  const present = new Set((existing ?? []).map(ruleKey))
  // A rule the user wrote for the `git rebase` family governs that family, and it is matched by
  // the blanket `git rebase*` rule too. Adding the shipped recovery allows in front of it would
  // only shadow them, so they are left out and the user's rule decides.
  const userGovernsRebase = (existing ?? []).some(governsRebaseFamily)
  const missing = shipped.filter((rule) => {
    if (present.has(ruleKey(rule))) return false
    if (userGovernsRebase && RECOVERY_RESOURCES.includes(rule.resource)) return false
    return true
  })
  if (missing.length === 0) return { text, state: "present", added: 0, rules: [] }
  const merged = [...missing, ...(existing ?? [])]
  const edits = modify(text, ["permissions"], merged, {
    allowTrailingComma: true,
    formattingOptions: { insertSpaces: true, tabSize: 2 },
  })
  return { text: applyEdits(text, edits), state: "merged", added: missing.length, rules: missing }
}

/**
 * The configuration file `init` merges into: global by default, project with `--project`.
 *
 * A file the user already keeps always wins over the name this package would create, so the
 * merge lands in the configuration the host is already reading. Only when neither name exists
 * is a new file created, and then it is the `opencode.jsonc` the host's own writer prefers.
 */
function configTarget(flags) {
  const base = flags.has("--project") ? projectRoot() : configRoot()
  const jsonc = join(base, "opencode.jsonc")
  const json = join(base, "opencode.json")
  if (existsSync(jsonc)) return jsonc
  if (existsSync(json)) return json
  return jsonc
}

/**
 * Print the rules that were written. A merge into someone's configuration is never
 * silent: the rules are listed and the guide that explains them is pointed at.
 */
function reportRules(rules) {
  console.log(`         applied ${rules.length} rule(s):`)
  for (const rule of rules) {
    console.log(`           ${rule.effect.padEnd(5)} ${rule.action} ${rule.resource}`)
  }
  console.log(`         review and rationale: ${join(RESOURCES, "permissions-guide.md")}`)
}

function printConfig() {
  const snippet = {
    plugins: [{ package: NAME, options: { guardrails: "standard" } }],
    permissions: loadPermissions(),
  }
  console.log(JSON.stringify(snippet, null, 2))
  console.log(
    "\n// Merge into opencode.jsonc. Plain local commits are governed by the guardrail hook:\n" +
      "// with no grant it escalates to ask, and a recorded grant leaves your configured effect alone.\n" +
      "// `opencode-rig init` writes the same rules for you; this is the manual form.",
  )
  return 0
}

function main(argv) {
  let parsed
  try {
    parsed = parseArgs(argv)
  } catch (error) {
    console.error(`opencode-rig: ${error.message}`)
    console.error(HELP)
    return 2
  }
  const { command, flags } = parsed
  if (flags.has("--help") || command === "help" || command === "") {
    console.log(HELP)
    return 0
  }
  try {
    if (command === "version") {
      console.log(VERSION)
      return 0
    }
    if (command === "setup" || command === "init" || command === "upgrade") {
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
