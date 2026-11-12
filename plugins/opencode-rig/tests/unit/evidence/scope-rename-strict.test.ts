/** Explicit scope paths reject rename origins while retaining other inventories. */
import { describe, expect, it } from "vitest"
import { sha256 } from "../../../src/digest.ts"
import { collectScope, detectDrift, refreshScope } from "../../../src/evidence.ts"
import { Project } from "../../helpers/harness.ts"

describe("explicit scope rename boundaries", () => {
  it("rejects an explicit file renamed away during a run", () => {
    const project = Project.withCommit()
    const scope = collectScope(project.root, ["src/a.ts"])
    project.git("mv", "src/a.ts", "src/renamed.ts")
    const error = "scope path was renamed away: src/a.ts"
    expect(() => collectScope(project.root, ["src/a.ts"])).toThrow(error)
    expect(() => refreshScope(project.root, scope)).toThrow(error)
    expect(() => detectDrift(project.root, scope)).toThrow(error)
  })

  it("retains a plain deletion of an explicit tracked file", () => {
    const project = Project.withCommit()
    project.git("rm", "-q", "src/a.ts")
    expect(collectScope(project.root, ["src/a.ts"]).entries).toEqual([
      { path: "src/a.ts", status: "D ", sha256: sha256("") },
    ])
  })

  it("ignores a rename outside the explicit scope", () => {
    const project = Project.withCommit({ "src/a.ts": "selected\n", "other.ts": "other\n" })
    project.git("mv", "other.ts", "renamed.ts")
    expect(collectScope(project.root, ["src/a.ts"]).entries).toEqual([
      { path: "src/a.ts", status: "M", sha256: sha256("selected\n") },
    ])
  })

  it("retains renamed children under an explicit directory scope", () => {
    const project = Project.withCommit()
    project.git("mv", "src/a.ts", "src/renamed.ts")
    expect(collectScope(project.root, ["src"]).entries).toEqual([
      { path: "src/a.ts", status: "D", sha256: sha256("") },
      { path: "src/renamed.ts", status: "M", sha256: sha256("export const a = 1\n") },
    ])
  })

  it("collects a recreated file at the rename origin", () => {
    const project = Project.withCommit()
    project.git("mv", "src/a.ts", "src/renamed.ts")
    project.write("src/a.ts", "replacement\n")
    expect(collectScope(project.root, ["src/a.ts"]).entries).toEqual([
      { path: "src/a.ts", status: "M", sha256: sha256("replacement\n") },
    ])
  })
})
