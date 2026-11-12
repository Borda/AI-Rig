/** Grants retain approval provenance and expire inside their bound repository. */
import { copyFileSync, readFileSync } from "node:fs"
import { afterEach, describe, expect, it, vi } from "vitest"
import { grantPath, readGrant, writeGrant } from "../../../src/git-grant.ts"
import { Project } from "../../helpers/harness.ts"

afterEach(() => vi.useRealTimers())

describe("grant approval binding", () => {
  it("records the exact confirmation and creation identity", () => {
    const project = new Project()
    const record = writeGrant(project.root, "Approve always")
    expect(record.confirmation).toBe("Approve always")
    expect(record.approval_id).toMatch(/^[a-f0-9-]{36}$/)
    expect(record.repository_binding).toMatch(/^[a-f0-9]{64}$/)
    expect(Date.parse(record.expires_at)).toBeGreaterThan(Date.parse(record.created_at))
    expect(JSON.parse(readFileSync(grantPath(project.root), "utf8"))).toEqual(record)
  })

  it("rejects a copied grant from another repository", () => {
    const source = new Project()
    const destination = new Project()
    writeGrant(source.root)
    copyFileSync(grantPath(source.root), grantPath(destination.root))
    expect(readGrant(destination.root)).toMatchObject({
      status: "invalid",
      reason: expect.stringMatching(/repository/),
    })
  })

  it("expires a grant after thirty days", () => {
    vi.useFakeTimers({ toFake: ["Date"] })
    vi.setSystemTime(new Date("2026-11-12T00:00:00Z"))
    const project = new Project()
    writeGrant(project.root)
    vi.setSystemTime(new Date("2026-12-12T00:00:00.001Z"))
    expect(readGrant(project.root)).toMatchObject({ status: "invalid", reason: expect.stringMatching(/expired/) })
  })

  it("refuses confirmation text other than the literal persistent approval", () => {
    expect(() => writeGrant(new Project().root, "Approve")).toThrow(/Approve always/)
  })
})
