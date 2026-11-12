import { readFileSync } from "node:fs"
import { join } from "node:path"
import { describe, expect, it } from "vitest"
import { STATUSES } from "../../../src/findings.ts"
import { RESOURCES_DIR } from "../../helpers/harness.ts"

/** Read one instruction section without depending on paragraph wrapping. */
function section(skill: string, heading: string): string[] {
  const lines = readFileSync(join(RESOURCES_DIR, "skills", skill, "SKILL.md"), "utf8").split("\n")
  const start = lines.indexOf(`## ${heading}`)
  expect(start, `${skill}: ${heading}`).toBeGreaterThan(-1)
  const end = lines.findIndex((line, index) => index > start && line.startsWith("## "))
  return lines.slice(start + 1, end < 0 ? undefined : end)
}

describe("review workflow instructions", () => {
  it("maps remediation buckets to tool statuses and rejects stale claims with evidence", () => {
    const workflow = section("rig-remediate", "Workflow")
    const buckets = workflow.flatMap((line) => /^   - `([^`]+)` —/.exec(line)?.[1] ?? [])
    expect(buckets).toEqual([...STATUSES])
    expect(workflow.some((line) => line.includes('status: "rejected"') && line.includes("stale: "))).toBe(true)
    expect(section("rig-remediate", "Rules").some((line) => line.includes("`deferred`"))).toBe(true)
  })

  it("separates verifier closure verdicts from the reviewer's correctness axes", () => {
    const workflow = section("rig-remediate", "Workflow")
    expect(
      workflow.some(
        (line) => line.includes("rig-verifier") && line.includes("`closed`") && line.includes("`verified_by`"),
      ),
    ).toBe(true)
    expect(
      workflow.some(
        (line) => line.includes("rig-reviewer") && line.includes("rig_findings") && line.includes("`correctness`"),
      ),
    ).toBe(true)
    expect(workflow.some((line) => line.includes("not-closed") && line.includes("unverifiable"))).toBe(true)
  })

  it("resolves PR heads to commit evidence without adding a nonexistent PR tool field", () => {
    const workflow = section("rig-review", "Workflow")
    expect(
      workflow.some(
        (line) => line.includes("PR") && line.includes("fetch") && line.includes("local ref") && line.includes("SHA"),
      ),
    ).toBe(true)
    expect(workflow.some((line) => line.includes("rig_evidence") && line.includes("commit: "))).toBe(true)
  })

  it("keeps workspace reviewers on the recorded working-tree bytes", () => {
    const workflow = section("rig-review", "Workflow")
    const collection = workflow.find((line) => line.includes("**Collect exact source.**"))
    expect(collection).toContain("project root")
    expect(collection).toContain("staged and unstaged")
    expect(collection).toContain("Do not use a detached worktree")
  })

  it("gives reviewers exact source and diff as well as digests", () => {
    const dispatch = section("rig-review", "Workflow").find((line) =>
      line.includes("**Dispatch independent reviewers.**"),
    )
    expect(dispatch).toContain("exact source bytes or paths")
    expect(dispatch).toContain("diff")
    expect(dispatch).toContain("digests")
  })

  it("links review classifications to the existing scope thresholds", () => {
    const classify = section("rig-review", "Workflow").find((line) => line.includes("**Classify.**"))
    expect(classify).toContain("../rig-plan/SKILL.md#workflow")
    const thresholds = section("rig-plan", "Workflow").find((line) => line.includes("**Classify scope.**"))
    expect(thresholds).toContain("under 3 files and 50 changed lines")
    expect(thresholds).toContain("8+ files")
  })

  it("scopes implementation handoffs from run evidence and acceptance criteria", () => {
    expect(section("rig-review", "Input").some((line) => line.includes("implementation run ID"))).toBe(true)
    const workflow = section("rig-review", "Workflow")
    expect(
      workflow.some(
        (line) =>
          line.includes('action: "inspect"') && line.includes("outputs") && line.includes("acceptance criteria"),
      ),
    ).toBe(true)
    expect(workflow.some((line) => line.includes("unrelated") && line.includes("paths"))).toBe(true)
  })

  it("requires independent sources without running a reproduction", () => {
    const crossCheck = section("rig-research", "Workflow").find((line) => line.includes("**Cross-check"))
    expect(crossCheck).toContain("at least two independent sources")
    expect(crossCheck).not.toContain("reproduction")
    expect(section("rig-research", "Rules").some((line) => line.includes("Do not run experiments"))).toBe(true)
  })

  it.each(["rig-review", "rig-remediate", "rig-research"])(
    "%s discloses instruction-enforced reviewer independence",
    (skill) => {
      const output = section(skill, "Output")
      expect(output.some((line) => line.includes("independence is instruction-enforced"))).toBe(true)
      expect(output.some((line) => line.includes("session"))).toBe(true)
    },
  )

  it.each(["rig-remediate", "rig-research"])("%s treats external content as untrusted data", (skill) => {
    const rules = section(skill, "Rules")
    expect(rules.some((line) => line.includes("untrusted data") && line.includes("instructions"))).toBe(true)
    expect(rules.some((line) => line.includes("secrets") && line.includes("URLs"))).toBe(true)
    expect(rules.some((line) => line.includes("dispatch"))).toBe(true)
  })
})
