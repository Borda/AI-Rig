/**
 * Small filesystem helpers shared by run, gate, and doctor code.
 *
 * Writes go through a temporary sibling and a rename so a crashed process
 * never leaves a half-written JSON artifact that later looks authoritative.
 */

import {
  appendFileSync,
  closeSync,
  existsSync,
  mkdirSync,
  openSync,
  readFileSync,
  renameSync,
  unlinkSync,
  writeFileSync,
} from "node:fs"
import { randomUUID } from "node:crypto"
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

/** Replace JSON through an exclusively created sibling, preserving existing temporary aliases. */
export function writeJsonAtomic(path: string, value: unknown): void {
  ensureDir(dirname(path))
  const temporary = `${path}.tmp-${randomUUID()}`
  const descriptor = openSync(temporary, "wx", 0o600)
  try {
    try {
      writeFileSync(descriptor, `${JSON.stringify(value, null, 2)}\n`, "utf8")
    } finally {
      closeSync(descriptor)
    }
    renameSync(temporary, path)
  } finally {
    // Only a successfully created sibling belongs to this writer; never remove a planted collision.
    if (existsSync(temporary)) unlinkSync(temporary)
  }
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
