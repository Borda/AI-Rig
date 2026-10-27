import { describe, expect, it } from "vitest"
import { collectCommitScope, collectScope, detectDrift, refreshScope, scopeBase } from "../../src/evidence.ts"
import { scopeDigest } from "../../src/digest.ts"
import { Project } from "../helpers/harness.ts"

describe("collectScope", () => {
  it("records modified bytes under the requested path", () => {
    const project = Project.withCommit({ "src/a.ts": "export const a = 1\n", "docs/b.md": "b\n" })
    project.write("src/a.ts", "export const a = 2\n")
    const scope = collectScope(project.root, ["src"])
    expect(scope.entries.map((entry) => entry.path)).toEqual(["src/a.ts"])
    expect(scope.entries[0]?.status).toBe("M")
    expect(scope.digest).toBe(scopeDigest(scope.entries))
    expect(scope.base).toBe(scopeBase(project.root))
  })

  it("filters out changes outside the requested paths", () => {
    const project = Project.withCommit({ "src/a.ts": "1\n", "docs/b.md": "b\n" })
    project.write("src/a.ts", "2\n")
    project.write("docs/b.md", "changed\n")
    expect(collectScope(project.root, ["src"]).entries.map((entry) => entry.path)).toEqual(["src/a.ts"])
  })

  it("scopes to every changed path when no filter is supplied", () => {
    const project = Project.withCommit({ "src/a.ts": "1\n", "docs/b.md": "b\n" })
    project.write("src/a.ts", "2\n")
    project.write("docs/b.md", "changed\n")
    expect(
      collectScope(project.root)
        .entries.map((entry) => entry.path)
        .sort(),
    ).toEqual(["docs/b.md", "src/a.ts"])
  })

  it("hashes an explicitly named unchanged file", () => {
    const project = Project.withCommit()
    const scope = collectScope(project.root, ["src/a.ts"])
    expect(scope.entries).toHaveLength(1)
    expect(scope.entries[0]?.path).toBe("src/a.ts")
  })

  it("includes untracked files", () => {
    const project = Project.withCommit()
    project.write("src/new.ts", "export const n = 1\n")
    expect(collectScope(project.root, ["src"]).entries.map((entry) => entry.path)).toContain("src/new.ts")
  })

  it("marks a deleted file with the empty digest", () => {
    const project = Project.withCommit()
    project.git("rm", "-q", "src/a.ts")
    const scope = collectScope(project.root, ["src"])
    expect(scope.entries[0]?.status).toContain("D")
    expect(scope.entries[0]?.sha256).toBe(scopeDigest([]))
  })

  it("skips dependency and artifact directories in an explicit walk", () => {
    const project = new Project()
    project.write("src/a.ts", "a\n")
    project.write("src/node_modules/pkg/index.js", "x\n")
    project.write("src/.opencode-rig/run.json", "{}\n")
    const scope = collectScope(project.root, ["src"])
    expect(scope.entries.map((entry) => entry.path)).toEqual(["src/a.ts"])
  })

  it("records no base outside Git", () => {
    const project = new Project()
    project.write("a.txt", "a\n")
    const scope = collectScope(project.root, ["a.txt"])
    expect(scope.base).toBeNull()
    expect(scope.entries).toHaveLength(1)
  })
})

describe("refreshScope and detectDrift", () => {
  it("adopts edited bytes only when refreshed", () => {
    const project = Project.withCommit()
    project.write("src/a.ts", "export const a = 2\n")
    const scope = collectScope(project.root, ["src"])

    project.write("src/a.ts", "export const a = 3\n")
    expect(detectDrift(project.root, scope)).toBe(true)

    const refreshed = refreshScope(project.root, scope)
    expect(refreshed.digest).not.toBe(scope.digest)
    expect(detectDrift(project.root, refreshed)).toBe(false)
  })

  it("does not drift when nothing changed", () => {
    const project = Project.withCommit()
    project.write("src/a.ts", "export const a = 2\n")
    const scope = collectScope(project.root, ["src"])
    expect(detectDrift(project.root, scope)).toBe(false)
  })
})

describe("collectCommitScope", () => {
  it("reads bytes from the commit, not the working tree", () => {
    const project = Project.withCommit()
    project.write("src/a.ts", "export const a = 2\n")
    const commit = project.commit("change a")
    project.write("src/a.ts", "working tree edit\n")
    const scope = collectCommitScope(project.root, commit, ["src"])
    expect(scope.commit).toBe(commit)
    expect(scope.entries.map((entry) => entry.path)).toEqual(["src/a.ts"])
    expect(detectDrift(project.root, scope)).toBe(false)
  })

  it("keeps the recorded digest after later edits", () => {
    const project = Project.withCommit()
    project.write("src/a.ts", "export const a = 2\n")
    const commit = project.commit("change a")
    const scope = collectCommitScope(project.root, commit, ["src"])
    project.write("src/a.ts", "later\n")
    expect(refreshScope(project.root, scope).digest).toBe(scope.digest)
    expect(detectDrift(project.root, scope)).toBe(false)
  })

  it("rejects an unknown commit", () => {
    const project = Project.withCommit()
    expect(() => collectCommitScope(project.root, "0".repeat(40), [])).toThrow(/cannot read commit/)
  })
})
