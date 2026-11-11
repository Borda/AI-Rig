import { linkSync, lstatSync, mkdirSync, readFileSync, renameSync, symlinkSync } from "node:fs"
import { afterEach, describe, expect, it, vi } from "vitest"
import { grantPath, readGrant, writeGrant } from "../../src/git-grant.ts"
import { Project } from "../helpers/harness.ts"

const boundary = vi.hoisted(() => ({
  beforeOpen: undefined as (() => void) | undefined,
  noFollowUnavailable: false,
}))

vi.mock("node:fs", async (importOriginal) => {
  const actual = await importOriginal<typeof import("node:fs")>()
  return {
    ...actual,
    constants: {
      ...actual.constants,
      get O_NOFOLLOW() {
        return boundary.noFollowUnavailable ? undefined : actual.constants.O_NOFOLLOW
      },
    },
    openSync: (...args: Parameters<typeof actual.openSync>) => {
      const callback = boundary.beforeOpen
      boundary.beforeOpen = undefined
      callback?.()
      return actual.openSync(...args)
    },
  }
})

afterEach(() => {
  boundary.beforeOpen = undefined
  boundary.noFollowUnavailable = false
})

describe("grant filesystem boundaries", () => {
  it("refuses to overwrite a symlink target", () => {
    const project = new Project()
    const victim = project.write("victim.json", "private data")
    symlinkSync(victim, grantPath(project.root))
    expect(() => writeGrant(project.root)).toThrow(/symlink|regular file/)
    expect(readFileSync(victim, "utf8")).toBe("private data")
    expect(lstatSync(grantPath(project.root)).isSymbolicLink()).toBe(true)
  })

  it("refuses a nonregular destination", () => {
    const project = new Project()
    mkdirSync(grantPath(project.root))
    expect(() => writeGrant(project.root)).toThrow(/regular file/)
    expect(lstatSync(grantPath(project.root)).isDirectory()).toBe(true)
  })

  it("replaces a regular grant without modifying other hard links", () => {
    const project = new Project()
    writeGrant(project.root)
    const original = project.path("original.json")
    linkSync(grantPath(project.root), original)
    const before = readFileSync(original, "utf8")
    const inode = lstatSync(grantPath(project.root)).ino
    writeGrant(project.root)
    expect(lstatSync(grantPath(project.root)).ino).not.toBe(inode)
    expect(readFileSync(original, "utf8")).toBe(before)
    expect(readGrant(project.root).status).toBe("granted")
  })

  it.each([false, true])("rejects a symlink substituted before opening (without nofollow: %s)", (unavailable) => {
    const project = new Project()
    writeGrant(project.root)
    boundary.noFollowUnavailable = unavailable
    const other = project.path("other.json")
    boundary.beforeOpen = () => {
      renameSync(grantPath(project.root), other)
      symlinkSync(other, grantPath(project.root))
    }
    expect(readGrant(project.root).status).toBe("invalid")
  })

  it.each([false, true])(
    "rejects a different file substituted before opening (without nofollow: %s)",
    (unavailable) => {
      const project = new Project()
      writeGrant(project.root)
      boundary.noFollowUnavailable = unavailable
      boundary.beforeOpen = () => {
        renameSync(grantPath(project.root), project.path("original.json"))
        writeGrant(project.root)
      }
      expect(readGrant(project.root).status).toBe("invalid")
    },
  )
})
