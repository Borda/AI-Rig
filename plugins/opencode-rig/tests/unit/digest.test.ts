import { describe, expect, it } from "vitest"
import { isDigest, scopeDigest, sha256, sha256File } from "../../src/digest.ts"
import { Project } from "../helpers/harness.ts"

describe("sha256", () => {
  it("hashes the documented empty-string vector", () => {
    expect(sha256("")).toBe("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")
  })

  it("hashes the documented abc vector", () => {
    expect(sha256("abc")).toBe("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
  })

  it("hashes files byte-for-byte", () => {
    const project = new Project()
    project.write("a.txt", "abc")
    expect(sha256File(project.path("a.txt"))).toBe(sha256("abc"))
  })
})

describe("scopeDigest", () => {
  const entries = [
    { path: "b.ts", status: "M", sha256: sha256("b") },
    { path: "a.ts", status: "M", sha256: sha256("a") },
  ]

  it("is independent of inventory order", () => {
    expect(scopeDigest(entries)).toBe(scopeDigest([...entries].reverse()))
  })

  it("changes when a byte changes", () => {
    const changed = [{ ...entries[0]!, sha256: sha256("other") }, entries[1]!]
    expect(scopeDigest(changed)).not.toBe(scopeDigest(entries))
  })

  it("changes when a path is added", () => {
    expect(scopeDigest([...entries, { path: "c.ts", status: "A", sha256: sha256("c") }])).not.toBe(scopeDigest(entries))
  })

  it("digests the empty inventory deterministically", () => {
    expect(scopeDigest([])).toBe(sha256(""))
  })
})

describe("isDigest", () => {
  it("accepts only lowercase 64-character hex", () => {
    expect(isDigest(sha256("x"))).toBe(true)
    expect(isDigest(sha256("x").toUpperCase())).toBe(false)
    expect(isDigest("abc")).toBe(false)
    expect(isDigest(42)).toBe(false)
  })
})
