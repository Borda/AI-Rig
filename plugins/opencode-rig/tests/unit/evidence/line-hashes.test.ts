/** The trivial path reports its own edit from line hashes, not from Git. */
import { execFileSync } from "node:child_process"
import { writeFileSync } from "node:fs"
import { describe, expect, it } from "vitest"
import { lineDiffCount, lineHashes } from "../../../src/evidence.ts"
import { Project } from "../../helpers/harness.ts"

describe("lineDiffCount", () => {
  it("counts additions, removals and replacements", () => {
    expect(lineDiffCount(["a", "b"], ["a", "b"])).toBe(0)
    expect(lineDiffCount(["a", "b"], ["a", "b", "c"])).toBe(1)
    expect(lineDiffCount(["a", "b", "c"], ["a", "b"])).toBe(1)
    expect(lineDiffCount(["a", "b"], ["a", "c"])).toBe(2)
  })

  it("treats a reordering as unchanged content", () => {
    expect(lineDiffCount(["a", "b", "c"], ["c", "a", "b"])).toBe(0)
  })
})

describe("lineHashes", () => {
  it("hashes each line and ignores the trailing newline", () => {
    const project = Project.withCommit({ "src/a.ts": "one\ntwo\n" })
    expect(lineHashes(project.path("src/a.ts"))).toHaveLength(2)
    project.write("src/a.ts", "one\ntwo")
    expect(lineHashes(project.path("src/a.ts"))).toHaveLength(2)
  })

  it("refuses binary content and a missing file", () => {
    const project = Project.withCommit({ "src/a.ts": "a\n" })
    writeFileSync(project.path("src/blob.bin"), Buffer.from([0, 1, 2, 3]))
    expect(lineHashes(project.path("src/blob.bin"))).toBeUndefined()
    expect(lineHashes(project.path("src/missing.ts"))).toBeUndefined()
  })

  it.skipIf(process.platform === "win32")("refuses a FIFO instead of blocking on it", () => {
    const project = Project.withCommit({ "src/a.ts": "a\n" })
    execFileSync("mkfifo", [project.path("pipe.ts")])
    expect(lineHashes(project.path("pipe.ts"))).toBeUndefined()
  })
})
