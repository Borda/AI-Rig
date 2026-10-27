#!/usr/bin/env node
/**
 * Keep every version-bearing file aligned with package.json.
 *
 * OpenCode resolves a package version from package.json, while rig.json and the
 * README marker are the discovery and documentation surfaces. A mismatch is a
 * release blocker, so this script fails rather than repairing silently.
 */

import { existsSync, readFileSync, writeFileSync } from "node:fs"
import { dirname, join, resolve } from "node:path"
import { fileURLToPath } from "node:url"

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..")
const README_MARKER = /(Current version:\s*`)([^`]+)(`)/

function read(path) {
  if (!existsSync(path)) throw new Error(`missing file: ${path}`)
  return readFileSync(path, "utf8")
}

export function declaredVersion() {
  const manifest = JSON.parse(read(join(ROOT, "package.json")))
  if (typeof manifest.version !== "string" || !/^\d+\.\d+\.\d+$/.test(manifest.version)) {
    throw new Error("package.json: version must be a plain MAJOR.MINOR.PATCH")
  }
  return manifest.version
}

function inspect() {
  const version = declaredVersion()
  const rig = JSON.parse(read(join(ROOT, "rig.json")))
  const versionTs = read(join(ROOT, "src", "version.ts"))
  const readme = read(join(ROOT, "README.md"))
  const tsMatch = /export const PLUGIN_VERSION = "([^"]+)"/.exec(versionTs)
  const marker = README_MARKER.exec(readme)
  return { version, rig, versionTs, readme, tsMatch, marker }
}

function check() {
  const problems = []
  const { version, rig, tsMatch, marker } = inspect()

  if (rig.version !== version) problems.push(`rig.json: ${JSON.stringify(rig.version)} != package.json ${version}`)
  if (!tsMatch) problems.push("src/version.ts: PLUGIN_VERSION is missing")
  else if (tsMatch[1] !== version) problems.push(`src/version.ts: ${tsMatch[1]} != package.json ${version}`)
  if (!marker) problems.push("README.md: missing 'Current version: `<x.y.z>`' marker")
  else if (marker[2] !== version) problems.push(`README.md: ${marker[2]} != package.json ${version}`)
  if (rig.name !== "opencode-rig")
    problems.push(`rig.json: name must be opencode-rig, found ${JSON.stringify(rig.name)}`)
  if (rig.entrypoint !== "./src/index.ts")
    problems.push(`rig.json: unexpected entrypoint ${JSON.stringify(rig.entrypoint)}`)

  if (problems.length > 0) {
    console.error("opencode-rig version check failed:")
    for (const problem of problems) console.error(`- ${problem}`)
    console.error("Fix with: node scripts/sync_version.mjs --write")
    return 1
  }
  console.log(`opencode-rig version sources agree: ${version}`)
  return 0
}

function write() {
  const { version, rig, versionTs, readme } = inspect()
  writeFileSync(join(ROOT, "rig.json"), `${JSON.stringify({ ...rig, version }, null, 2)}\n`, "utf8")
  writeFileSync(
    join(ROOT, "src", "version.ts"),
    versionTs.replace(/(export const PLUGIN_VERSION = ")[^"]+(")/, `$1${version}$2`),
    "utf8",
  )
  if (!README_MARKER.test(readme)) throw new Error("README.md: no version marker to update")
  writeFileSync(join(ROOT, "README.md"), readme.replace(README_MARKER, `$1${version}$3`), "utf8")
  console.log(`opencode-rig version sources written: ${version}`)
  return 0
}

const args = new Set(process.argv.slice(2))
try {
  process.exitCode = args.has("--write") ? write() : check()
} catch (error) {
  console.error(`opencode-rig version check: ${error instanceof Error ? error.message : String(error)}`)
  process.exitCode = 1
}
