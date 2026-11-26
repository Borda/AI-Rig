/** Blueprint normalization, fragment splitting and exact-text lookup. */
import { readFileSync } from "node:fs"
import { join } from "node:path"
import { describe, expect, it } from "vitest"
import { blueprintSafe, normalizeCommand, splitFragments, type BlueprintManifest } from "../../../src/blueprint.ts"
import { sha256 } from "../../../src/digest.ts"
import { RESOURCES_DIR } from "../../helpers/harness.ts"

interface Vectors {
  normalize: { in: string; out: string }[]
  fragments: { in: string; out: string[] }[]
  unsafe: { in: string; reason: string }[]
}

const vectors = JSON.parse(readFileSync(join(RESOURCES_DIR, "blueprint-vectors.json"), "utf8")) as Vectors
const manifest = JSON.parse(readFileSync(join(RESOURCES_DIR, "blueprint-manifest.json"), "utf8")) as BlueprintManifest

describe("blueprint normalization", () => {
  it.each(vectors.normalize)("normalizes %j", ({ in: input, out }) => {
    expect(normalizeCommand(input).text).toBe(out)
  })

  it.each(vectors.fragments)("splits %j", ({ in: input, out }) => {
    expect(splitFragments(normalizeCommand(input).text)).toEqual(out)
  })

  it.each(vectors.unsafe)("marks %j unsafe", ({ in: input, reason }) => {
    expect(normalizeCommand(input)).toMatchObject({ safe: false, reason })
  })

  it("keeps intra-line whitespace and quotes unchanged", () => {
    expect(normalizeCommand("echo  'a  b'").text).toBe("echo  'a  b'")
  })

  it("preserves a comment marker attached to an escaped space", () => {
    const literal = "echo word\\ #literal && echo next"
    expect(splitFragments(normalizeCommand(literal).text)).toEqual(["echo word\\ #literal", "echo next"])
  })
})

describe("blueprint lookup", () => {
  it("allows every shipped command fragment", () => {
    for (const digest of Object.keys(manifest.entries)) expect(digest).toMatch(/^[0-9a-f]{64}$/)
    expect(blueprintSafe("git status --porcelain", manifest)).toBe(true)
    expect(blueprintSafe("git diff", manifest)).toBe(true)
    expect(blueprintSafe("git diff --stat", manifest)).toBe(true)
  })

  it("allows a compound only when every fragment is shipped", () => {
    expect(blueprintSafe("git status --porcelain\ngit diff --stat", manifest)).toBe(true)
    expect(blueprintSafe("git status --porcelain && git push", manifest)).toBe(false)
    expect(blueprintSafe("git status --porcelain\tgit diff", manifest)).toBe(false)
  })

  it.each(["|", "||", ";", "\n", "&"])("does not prove a directory change joined by %j", (operator) => {
    const directory = process.cwd().replaceAll("\\", "/")
    const cd = `cd "${directory}"`
    const entries = Object.fromEntries(
      [cd, "git status"].map((fragment) => [sha256(fragment), { kind: "command", src: "test" } as const]),
    )
    const blueprint = { ...manifest, entries }
    expect(blueprintSafe(`${cd} && git status`, blueprint)).toBe(true)
    expect(blueprintSafe(`${cd} && git status`, blueprint, () => false)).toBe(false)
    expect(blueprintSafe(`${cd} ${operator} git status`, blueprint)).toBe(false)
  })

  it("fails closed on an empty, unsafe or unshipped command", () => {
    expect(blueprintSafe("", manifest)).toBe(false)
    expect(blueprintSafe("cat <<'E'\nx\nE\n", manifest)).toBe(false)
    expect(blueprintSafe(sha256("nope"), manifest)).toBe(false)
  })
})
