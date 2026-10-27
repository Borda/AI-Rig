import { lstatSync, readFileSync, symlinkSync, writeFileSync } from "node:fs"
import { describe, expect, it } from "vitest"
import { GRANT_FILENAME, grantPath, readGrant, revokeGrant, validateGrant, writeGrant } from "../../src/git-grant.ts"
import { resolveGitCommonDir } from "../../src/paths.ts"
import { Project } from "../helpers/harness.ts"

function commonDir(project: Project): string {
  const resolved = resolveGitCommonDir(project.root)
  if (!resolved) throw new Error("test project is not a Git repository")
  return resolved
}

describe("validateGrant", () => {
  const valid = {
    schema: 1,
    scope: "local-git",
    source: "user-approve-always",
    plugin_version: "0.1.0",
    created_at: "2026-01-01T00:00:00.000Z",
  }

  it("accepts a complete record", () => {
    expect(validateGrant(valid)).toEqual({ ok: true, record: valid })
  })

  it.each([
    ["a non-object", []],
    ["an unknown schema", { ...valid, schema: 2 }],
    ["an unknown scope", { ...valid, scope: "global" }],
    ["an unknown source", { ...valid, source: "config" }],
    ["a missing timestamp", { ...valid, created_at: undefined }],
  ])("rejects %s", (_label, raw) => {
    expect(validateGrant(raw).ok).toBe(false)
  })
})

describe("readGrant", () => {
  it("reports no-git outside a repository", () => {
    expect(readGrant(undefined).status).toBe("no-git")
  })

  it("reports absent when no grant file exists", () => {
    const project = Project.withCommit()
    const state = readGrant(commonDir(project))
    expect(state.status).toBe("absent")
    expect(state.path).toBe(grantPath(commonDir(project)))
  })

  it("reads a written grant", () => {
    const project = Project.withCommit()
    const record = writeGrant(commonDir(project))
    const state = readGrant(commonDir(project))
    expect(state.status).toBe("granted")
    expect(state.record).toEqual(record)
  })

  it("writes the grant inside the common Git directory", () => {
    const project = Project.withCommit()
    const path = grantPath(commonDir(project))
    writeGrant(commonDir(project))
    expect(path.endsWith(`/${GRANT_FILENAME}`)).toBe(true)
    expect(lstatSync(path).isFile()).toBe(true)
    expect(JSON.parse(readFileSync(path, "utf8")).scope).toBe("local-git")
  })

  it("rejects malformed JSON", () => {
    const project = Project.withCommit()
    writeFileSync(grantPath(commonDir(project)), "{not json", "utf8")
    const state = readGrant(commonDir(project))
    expect(state.status).toBe("invalid")
  })

  it("rejects an unknown schema in place", () => {
    const project = Project.withCommit()
    writeFileSync(
      grantPath(commonDir(project)),
      JSON.stringify({ schema: 99, scope: "local-git", source: "user-approve-always", created_at: "x" }),
      "utf8",
    )
    expect(readGrant(commonDir(project))).toMatchObject({ status: "invalid", reason: "unknown schema 99" })
  })

  it("rejects a symlinked grant file", () => {
    const project = Project.withCommit()
    const elsewhere = project.path("planted.json")
    writeFileSync(
      elsewhere,
      JSON.stringify({
        schema: 1,
        scope: "local-git",
        source: "user-approve-always",
        plugin_version: "0.1.0",
        created_at: "2026-01-01T00:00:00.000Z",
      }),
      "utf8",
    )
    symlinkSync(elsewhere, grantPath(commonDir(project)))
    expect(readGrant(commonDir(project))).toMatchObject({ status: "invalid", reason: "grant path is a symlink" })
  })

  it("rejects a grant in a bare repository because no common dir resolves", () => {
    const project = new Project()
    project.git("init", "-q", "--bare")
    expect(resolveGitCommonDir(project.root)).toBeUndefined()
    expect(readGrant(resolveGitCommonDir(project.root)).status).toBe("no-git")
  })
})

describe("revokeGrant", () => {
  it("removes a written grant and reports a second revoke as absent", () => {
    const project = Project.withCommit()
    writeGrant(commonDir(project))
    expect(revokeGrant(commonDir(project))).toBe(true)
    expect(readGrant(commonDir(project)).status).toBe("absent")
    expect(revokeGrant(commonDir(project))).toBe(false)
  })
})
