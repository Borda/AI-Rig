/**
 * Plugin options: defaults, validation, and normalization.
 *
 * Options arrive through the object form of the `plugins` config entry. Invalid
 * values fail setup instead of being silently coerced, because a mistyped
 * guardrail mode would otherwise disable a safety class without notice.
 */

export type GuardrailMode = "standard" | "strict" | "off"
export type CodemapMode = "auto" | "off" | "require"

export interface RigOptions {
  readonly guardrails: GuardrailMode
  readonly artifacts: string
  readonly gates: readonly string[]
  readonly gateTimeoutMs: number
  readonly maxParallel: number
  readonly maxLoopRounds: number
  readonly ttlDays: number
  readonly journal: boolean
  readonly codemap: CodemapMode
  readonly compactionPreserve: boolean
}

export const DEFAULT_OPTIONS: RigOptions = {
  guardrails: "standard",
  artifacts: ".opencode-rig",
  gates: [],
  gateTimeoutMs: 600_000,
  maxParallel: 4,
  maxLoopRounds: 3,
  ttlDays: 30,
  journal: true,
  codemap: "auto",
  compactionPreserve: true,
}

const GUARDRAIL_MODES: readonly GuardrailMode[] = ["standard", "strict", "off"]
const CODEMAP_MODES: readonly CodemapMode[] = ["auto", "off", "require"]

export class OptionError extends Error {
  constructor(message: string) {
    super(`opencode-rig options: ${message}`)
    this.name = "OptionError"
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
}

function readEnum<T extends string>(raw: Record<string, unknown>, key: string, allowed: readonly T[], fallback: T): T {
  const value = raw[key]
  if (value === undefined) return fallback
  if (typeof value !== "string" || !(allowed as readonly string[]).includes(value)) {
    throw new OptionError(`'${key}' must be one of ${allowed.join(", ")}`)
  }
  return value as T
}

function readNumber(raw: Record<string, unknown>, key: string, fallback: number, min: number, max: number): number {
  const value = raw[key]
  if (value === undefined) return fallback
  if (typeof value !== "number" || !Number.isFinite(value) || value < min || value > max) {
    throw new OptionError(`'${key}' must be a number between ${min} and ${max}`)
  }
  return value
}

function readString(raw: Record<string, unknown>, key: string, fallback: string): string {
  const value = raw[key]
  if (value === undefined) return fallback
  if (typeof value !== "string" || value.trim() === "") {
    throw new OptionError(`'${key}' must be a non-empty string`)
  }
  return value
}

function readBoolean(raw: Record<string, unknown>, key: string, fallback: boolean): boolean {
  const value = raw[key]
  if (value === undefined) return fallback
  if (typeof value !== "boolean") throw new OptionError(`'${key}' must be a boolean`)
  return value
}

function readStringArray(raw: Record<string, unknown>, key: string, fallback: readonly string[]): readonly string[] {
  const value = raw[key]
  if (value === undefined) return fallback
  if (!Array.isArray(value) || value.some((entry) => typeof entry !== "string" || entry.trim() === "")) {
    throw new OptionError(`'${key}' must be an array of non-empty strings`)
  }
  return value as string[]
}

const KNOWN_KEYS = new Set([
  "guardrails",
  "artifacts",
  "gates",
  "gateTimeoutMs",
  "maxParallel",
  "maxLoopRounds",
  "ttlDays",
  "journal",
  "codemap",
  "compactionPreserve",
])

/**
 * Normalize raw plugin options. Unknown keys are rejected so a typo cannot
 * silently leave a setting at its default.
 */
export function parseOptions(raw: unknown): RigOptions {
  if (raw === undefined || raw === null) return DEFAULT_OPTIONS
  if (!isRecord(raw)) throw new OptionError("options must be an object")

  for (const key of Object.keys(raw)) {
    if (!KNOWN_KEYS.has(key)) throw new OptionError(`unknown option '${key}'`)
  }

  return {
    guardrails: readEnum(raw, "guardrails", GUARDRAIL_MODES, DEFAULT_OPTIONS.guardrails),
    artifacts: readString(raw, "artifacts", DEFAULT_OPTIONS.artifacts),
    gates: readStringArray(raw, "gates", DEFAULT_OPTIONS.gates),
    gateTimeoutMs: readNumber(raw, "gateTimeoutMs", DEFAULT_OPTIONS.gateTimeoutMs, 1_000, 3_600_000),
    maxParallel: Math.trunc(readNumber(raw, "maxParallel", DEFAULT_OPTIONS.maxParallel, 1, 16)),
    maxLoopRounds: Math.trunc(readNumber(raw, "maxLoopRounds", DEFAULT_OPTIONS.maxLoopRounds, 1, 10)),
    ttlDays: Math.trunc(readNumber(raw, "ttlDays", DEFAULT_OPTIONS.ttlDays, 1, 3_650)),
    journal: readBoolean(raw, "journal", DEFAULT_OPTIONS.journal),
    codemap: readEnum(raw, "codemap", CODEMAP_MODES, DEFAULT_OPTIONS.codemap),
    compactionPreserve: readBoolean(raw, "compactionPreserve", DEFAULT_OPTIONS.compactionPreserve),
  }
}
