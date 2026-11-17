/** The shipped blueprint manifest stays byte-derived from the published blocks. */
import { readFileSync, readdirSync, writeFileSync } from "node:fs"
import { join, relative } from "node:path"
import { describe, expect, it } from "vitest"
import { blueprintSafe, buildManifest, type BlueprintManifest } from "../../src/blueprint.ts"
import { PACKAGE_ROOT, RESOURCES_DIR } from "../helpers/harness.ts"

const MANIFEST = join(RESOURCES_DIR, "blueprint-manifest.json")

function markdownFiles(): string[] {
  const files: string[] = []
  for (const root of ["skills", "roles", "references"]) {
    const directory = join(RESOURCES_DIR, root)
    const walk = (path: string) => {
      for (const entry of readdirSync(path, { withFileTypes: true })) {
        const child = join(path, entry.name)
        if (entry.isDirectory()) walk(child)
        else if (entry.name.endsWith(".md")) files.push(child)
      }
    }
    walk(directory)
  }
  return files.sort()
}

function publishedBlocks(): { src: string; text: string }[] {
  const blocks: { src: string; text: string }[] = []
  for (const file of markdownFiles()) {
    const text = readFileSync(file, "utf8").replace(/\r\n/g, "\n")
    const src = relative(PACKAGE_ROOT, file).split("\\").join("/")
    for (const match of text.matchAll(/^```(bash|sh)[ \t]*\n([\s\S]*?)^```[ \t]*$/gm)) {
      const line = text.slice(0, match.index).split("\n").length
      blocks.push({ src: `${src}:${line}`, text: match[2]! })
    }
  }
  return blocks
}

describe("blueprint manifest", () => {
  it("matches the published blocks", () => {
    const built = buildManifest("opencode-rig", publishedBlocks())
    if (process.env.BLUEPRINT_UPDATE === "1") {
      writeFileSync(MANIFEST, `${JSON.stringify(built, null, 2)}\n`)
      return
    }
    const shipped = JSON.parse(readFileSync(MANIFEST, "utf8")) as BlueprintManifest
    expect(built).toEqual(shipped)
  })

  it("finds every fenced block an independent line counter sees", () => {
    let fences = 0
    for (const file of markdownFiles()) {
      const lines = readFileSync(file, "utf8").replace(/\r\n/g, "\n").split("\n")
      for (const line of lines) if (/^```(?:bash|sh)[ \t]*$/.test(line)) fences += 1
    }
    expect(publishedBlocks()).toHaveLength(fences)
  })

  it("accounts for every fenced block by content", async () => {
    const shipped = JSON.parse(readFileSync(MANIFEST, "utf8")) as BlueprintManifest
    const { analyzeBlock } = await import("../../src/blueprint.ts")
    const { sha256 } = await import("../../src/digest.ts")
    for (const block of publishedBlocks()) {
      const analyzed = analyzeBlock(block.text)
      if (!analyzed.safe) {
        expect(
          shipped.skipped.map((skip) => skip.src),
          block.src,
        ).toContain(block.src)
        continue
      }
      const covered =
        analyzed.fragments.every((fragment) => shipped.entries[sha256(fragment)]?.kind === "command") ||
        shipped.entries[sha256(analyzed.text)]?.kind === "block"
      expect(covered, block.src).toBe(true)
    }
  })

  it("allows a shipped command and refuses a modified or unshipped one", () => {
    const shipped = JSON.parse(readFileSync(MANIFEST, "utf8")) as BlueprintManifest
    expect(blueprintSafe("git status --porcelain", shipped)).toBe(true)
    expect(blueprintSafe("git status --porcelain  ", shipped)).toBe(true)
    expect(blueprintSafe("git status --porcelain && git push", shipped)).toBe(false)
    expect(blueprintSafe("git push", shipped)).toBe(false)
    expect(blueprintSafe("git status  --porcelain", shipped)).toBe(false)
  })
})
