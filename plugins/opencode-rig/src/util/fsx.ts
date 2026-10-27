/**
 * Small filesystem helpers shared by run, gate, and doctor code.
 *
 * Writes go through a temporary sibling and a rename so a crashed process
 * never leaves a half-written JSON artifact that later looks authoritative.
 */

import { appendFileSync, existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs"
import { dirname, join } from "node:path"

export function ensureDir(path: string): string {
  mkdirSync(path, { recursive: true })
  return path
}

export function readJson<T>(path: string): T {
  return JSON.parse(readFileSync(path, "utf8")) as T
}

export function readJsonIfExists<T>(path: string): T | undefined {
  if (!existsSync(path)) return undefined
  return readJson<T>(path)
}

export function writeJsonAtomic(path: string, value: unknown): void {
  ensureDir(dirname(path))
  const temporary = `${path}.tmp-${process.pid}-${Date.now()}`
  writeFileSync(temporary, `${JSON.stringify(value, null, 2)}\n`, "utf8")
  renameSync(temporary, path)
}

export function appendLine(path: string, line: string): void {
  ensureDir(dirname(path))
  const suffix = line.endsWith("\n") ? "" : "\n"
  appendFileSync(path, `${line}${suffix}`, "utf8")
}

export function readLines(path: string): string[] {
  if (!existsSync(path)) return []
  return readFileSync(path, "utf8")
    .split("\n")
    .filter((line) => line.trim() !== "")
}

export function readTextIfExists(path: string): string | undefined {
  if (!existsSync(path)) return undefined
  return readFileSync(path, "utf8")
}

export function joinPath(...parts: string[]): string {
  return join(...parts)
}
