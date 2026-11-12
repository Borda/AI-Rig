/** Staged rename evidence retains the removed source path. */
import { describe, expect, it } from "vitest"
import { collectScope } from "../../../src/evidence.ts"
import { sha256 } from "../../../src/digest.ts"
import { Project } from "../../helpers/harness.ts"

describe("staged rename origin evidence", () => {
  it("records both the destination bytes and removed origin", () => {
    const project = Project.withCommit({ "origin.ts": "export const value = 1\n" })
    project.git("mv", "origin.ts", "destination.ts")
    expect(() => collectScope(project.root, ["origin.ts", "destination.ts"])).toThrow(
      "scope path was renamed away: origin.ts",
    )
    const scope = collectScope(project.root)
    expect(scope.entries).toEqual([
      { path: "destination.ts", status: "R ", sha256: sha256("export const value = 1\n") },
      { path: "origin.ts", status: "D", sha256: sha256("") },
    ])
  })

  it("rejects an explicitly selected renamed-away origin", () => {
    const project = Project.withCommit({ "origin.ts": "export const value = 1\n" })
    project.git("mv", "origin.ts", "destination.ts")
    expect(() => collectScope(project.root, ["origin.ts"])).toThrow("scope path was renamed away: origin.ts")
  })
})
