/**
 * Minimal validation helpers.
 *
 * The plugin ships no runtime validation dependency: schemas are small and
 * hand-checked at the write boundary, which keeps the published package lean.
 */

export class ValidationError extends Error {
  constructor(message: string) {
    super(message)
    this.name = "ValidationError"
  }
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
}

export function requireString(value: unknown, field: string): string {
  if (typeof value !== "string" || value.trim() === "") {
    throw new ValidationError(`'${field}' must be a non-empty string`)
  }
  return value
}

export function optionalString(value: unknown, field: string): string | undefined {
  if (value === undefined) return undefined
  return requireString(value, field)
}

export function requireEnum<T extends string>(value: unknown, field: string, allowed: readonly T[]): T {
  if (typeof value !== "string" || !(allowed as readonly string[]).includes(value)) {
    throw new ValidationError(`'${field}' must be one of ${allowed.join(", ")}`)
  }
  return value as T
}

export function requireNumber(value: unknown, field: string, min: number, max: number): number {
  if (typeof value !== "number" || !Number.isFinite(value) || value < min || value > max) {
    throw new ValidationError(`'${field}' must be a number between ${min} and ${max}`)
  }
  return value
}

export function requireArray(value: unknown, field: string): unknown[] {
  if (!Array.isArray(value)) throw new ValidationError(`'${field}' must be an array`)
  return value
}
