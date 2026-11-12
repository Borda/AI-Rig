/** Atomic JSON writes cannot truncate a planted temporary-file alias. */
import { linkSync, readdirSync } from "node:fs"
import { expect, it, vi } from "vitest"
import { Project } from "../../helpers/harness.ts"
import { writeJsonAtomic } from "../../../src/util/fsx.ts"

vi.mock("node:crypto", async (importOriginal) => ({
  ...(await importOriginal<typeof import("node:crypto")>()),
  randomUUID: () => "12345678-1234-1234-1234-123456789abc",
}))

it("refuses an existing alias at the exact UUID temporary filename", () => {
  const project = new Project()
  const target = project.write("outside.json", "original bytes")
  const temporary = project.path("state.json.tmp-12345678-1234-1234-1234-123456789abc")
  linkSync(target, temporary)
  expect(() => writeJsonAtomic(project.path("state.json"), { safe: true })).toThrowError(
    expect.objectContaining({ code: "EEXIST" }),
  )
  expect(project.read("outside.json")).toBe("original bytes")
  expect(project.read("state.json.tmp-12345678-1234-1234-1234-123456789abc")).toBe("original bytes")
  expect(project.exists("state.json")).toBe(false)
})

it("leaves a planted predictable temporary hard link and its target untouched", () => {
  const project = new Project()
  const target = project.write("outside.json", "original bytes")
  const path = project.path("state.json")
  const clock = vi.spyOn(Date, "now").mockReturnValue(12345)
  const planted = `${path}.tmp-${process.pid}-12345`
  linkSync(target, planted)
  try {
    writeJsonAtomic(path, { safe: true })
    expect(project.read("outside.json")).toBe("original bytes")
    expect(project.read("state.json")).toBe('{\n  "safe": true\n}\n')
    expect(readdirSync(project.root)).toContain("state.json.tmp-" + process.pid + "-12345")
  } finally {
    clock.mockRestore()
  }
})

it("cleans its own temporary file when rename fails", () => {
  const project = new Project()
  project.write("state.json/keep.txt", "keep")
  expect(() => writeJsonAtomic(project.path("state.json"), {})).toThrow()
  expect(readdirSync(project.root)).toEqual(["state.json"])
  expect(project.read("state.json/keep.txt")).toBe("keep")
})
