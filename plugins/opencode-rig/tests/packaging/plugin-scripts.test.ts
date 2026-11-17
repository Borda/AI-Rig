/** The shipped plugin-script listing stays bound to the installed files. */
import { readFileSync, readdirSync } from "node:fs"
import { join } from "node:path"
import { describe, expect, it } from "vitest"
import { sha256File } from "../../src/digest.ts"
import { PACKAGE_ROOT } from "../helpers/harness.ts"

const listing = JSON.parse(readFileSync(join(PACKAGE_ROOT, "src/resources/plugin-files.json"), "utf8")) as {
  files: Record<string, string>
}

describe("shipped plugin scripts", () => {
  it("matches every listed digest", () => {
    for (const [path, digest] of Object.entries(listing.files)) {
      expect(sha256File(join(PACKAGE_ROOT, path)), path).toBe(digest)
    }
  })

  it("lists every shipped file under bin/ and nothing from the source-only scripts/", () => {
    const found = new Set<string>()
    for (const name of readdirSync(join(PACKAGE_ROOT, "bin"))) found.add(`bin/${name}`)
    expect(new Set(Object.keys(listing.files))).toEqual(found)
    expect(Object.keys(listing.files).some((path) => path.startsWith("scripts/"))).toBe(false)
  })
})
