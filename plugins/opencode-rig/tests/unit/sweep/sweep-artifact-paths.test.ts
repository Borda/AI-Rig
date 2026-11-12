/** Artifact settings cannot cross external symlink ancestors. */
import { mkdirSync, symlinkSync } from "node:fs"
import { describe, expect, it } from "vitest"
import { resolvePaths } from "../../../src/paths.ts"
import { Project } from "../../helpers/harness.ts"

describe("physical artifact containment", () => {
  it("rejects a missing artifact root beneath an external symlink", () => {
    const project = new Project()
    const outside = new Project()
    symlinkSync(outside.root, project.path("linked"), "junction")
    expect(() => resolvePaths(project.root, "linked/artifacts")).toThrow(/artifact.*project/)
    expect(outside.exists("artifacts")).toBe(false)
  })

  it("accepts an ancestor symlink that stays within the physical project", () => {
    const project = new Project()
    mkdirSync(project.path("owned"))
    symlinkSync(project.path("owned"), project.path("linked"), "junction")
    expect(resolvePaths(project.root, "linked/artifacts").artifactRoot).toBe(project.path("linked/artifacts"))
  })

  it("accepts a missing nested artifact directory under a canonical temporary root", () => {
    const project = new Project()
    expect(resolvePaths(project.root, "nested/artifacts").artifactRoot).toBe(project.path("nested/artifacts"))
  })
})
