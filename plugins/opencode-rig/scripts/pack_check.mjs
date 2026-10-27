#!/usr/bin/env node
/**
 * Verify the published tarball contains only intended files.
 *
 * The package ships TypeScript sources and resources, not tests or local
 * artifacts. A leaked test, plan, or run directory is a packaging defect, so
 * this check fails on both a missing required file and a forbidden path.
 */

import { spawnSync } from "node:child_process"
import { readFileSync } from "node:fs"
import { dirname, resolve } from "node:path"
import { fileURLToPath } from "node:url"

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..")
const rig = JSON.parse(readFileSync(resolve(ROOT, "rig.json"), "utf8"))

const REQUIRED = [
  "package.json",
  "README.md",
  "CHANGELOG.md",
  "LICENSE",
  "NOTICE",
  "rig.json",
  "bin/opencode-rig.mjs",
  "src/index.ts",
  "src/version.ts",
  "src/resources/agents.json",
  "src/resources/references/commit-handoff.md",
  "src/resources/references/convergence.md",
  "src/resources/references/evidence.md",
  ...rig.skills.map((skill) => `src/resources/skills/${skill}/SKILL.md`),
  ...rig.agents.map((role) => `src/resources/roles/${role}.md`),
]

const FORBIDDEN = [
  /(^|\/)node_modules\//,
  /(^|\/)tests\//,
  /(^|\/)\.plans\//,
  /(^|\/)\.reports\//,
  /(^|\/)\.opencode-rig\//,
  /(^|\/)coverage\//,
  /(^|\/)dist\//,
  /(^|\/)package-lock\.json$/,
  /(^|\/)tsconfig\.json$/,
  /\.tsbuildinfo$/,
  /\.log$/,
  /(^|\/)\.env/,
]

function pack() {
  const result = spawnSync("npm", ["pack", "--dry-run", "--json"], {
    cwd: ROOT,
    encoding: "utf8",
    shell: process.platform === "win32",
  })
  if (result.status !== 0) {
    throw new Error(`npm pack failed:\n${result.stderr || result.stdout}`)
  }
  const parsed = JSON.parse(result.stdout)
  const entry = Array.isArray(parsed) ? parsed[0] : parsed
  if (!entry?.files) throw new Error("npm pack --json returned no file list")
  return entry.files.map((file) => file.path)
}

function check() {
  const files = new Set(pack())
  const problems = []

  for (const path of REQUIRED) {
    if (!files.has(path)) problems.push(`missing from tarball: ${path}`)
  }
  for (const path of files) {
    if (FORBIDDEN.some((pattern) => pattern.test(path))) problems.push(`forbidden in tarball: ${path}`)
  }

  const skills = [...files].filter((path) => /^src\/resources\/skills\/[^/]+\/SKILL\.md$/.test(path))
  if (skills.length !== rig.skills.length) {
    problems.push(`expected ${rig.skills.length} skill cards, found ${skills.length}`)
  }
  const roles = [...files].filter((path) => /^src\/resources\/roles\/[^/]+\.md$/.test(path))
  if (roles.length !== rig.agents.length) {
    problems.push(`expected ${rig.agents.length} role cards, found ${roles.length}`)
  }

  if (problems.length > 0) {
    console.error("opencode-rig package check failed:")
    for (const problem of problems) console.error(`- ${problem}`)
    console.error(`\nFiles (${files.size}):\n${[...files].sort().join("\n")}`)
    return 1
  }
  console.log(
    `opencode-rig package check passed: ${files.size} files, ${rig.skills.length} skills, ${rig.agents.length} role cards, no tests or local artifacts`,
  )
  return 0
}

try {
  process.exitCode = check()
} catch (error) {
  console.error(`opencode-rig package check: ${error instanceof Error ? error.message : String(error)}`)
  process.exitCode = 1
}
