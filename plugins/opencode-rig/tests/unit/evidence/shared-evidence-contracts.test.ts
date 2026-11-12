/** Shared evidence contracts remain reachable from every owning workflow. */
import { readFileSync } from "node:fs"
import { join } from "node:path"
import { describe, expect, it } from "vitest"
import { RESOURCES_DIR } from "../../helpers/harness.ts"

function lines(path: string): string[] {
  return readFileSync(join(RESOURCES_DIR, path), "utf8").split("\n")
}

describe("shared evidence rules", () => {
  it.each([
    "rig-plan",
    "rig-investigate",
    "rig-research",
    "rig-implement",
    "rig-review",
    "rig-remediate",
    "rig-challenge",
    "rig-optimize",
  ])("routes %s dispatches through the fallback and violation-stop contract", (skill) => {
    expect(
      lines(`skills/${skill}/SKILL.md`).some((line) => line.includes("../../references/evidence.md#role-dispatch")),
    ).toBe(true)
  })

  it("defines the fallback and stops accepting violating child results", () => {
    const rule = lines("references/evidence.md").find((line) => line.startsWith("Use an installed native"))!
    expect(rule).toContain("otherwise call `rig_dispatch`")
    expect(rule).toContain("`read_only: true`")
    expect(rule).toContain("`scope_violation: true`")
    expect(rule).toContain("nonempty `violations`")
    expect(rule).toContain("do not accept that child result or finalize")
  })

  it.each([
    "rig-investigator",
    "rig-implementer",
    "rig-reviewer",
    "rig-challenger",
    "rig-verifier",
    "rig-scientist",
    "rig-optimizer",
  ])("makes %s report a scope violation instead of certifying the result", (role) => {
    const rule = lines(`roles/${role}.md`).find((line) => line.includes("`scope_violation: true`"))!
    expect(rule).toContain("nonempty `violations`")
    expect(rule).toContain("stops the handoff")
    expect(rule).toContain("instead of accepting the affected result")
  })

  it.each(["rig-implement", "rig-investigate", "rig-review"])("shares optional RTK guidance with %s", (skill) => {
    const card = lines(`skills/${skill}/SKILL.md`)
    expect(card.some((line) => line.includes("evidence.md#optional-command-compression"))).toBe(true)
    expect(card.some((line) => line.includes("60-99%"))).toBe(false)
    const guidance = lines("references/evidence.md").find((line) => line.includes("command -v rtk"))!
    expect(guidance).toContain("rtk git status --short")
    expect(guidance).toContain("never install it during a run")
  })

  it("records external inventory identifiers without passing outside paths to evidence readers", () => {
    const start = lines("skills/rig-distill/SKILL.md").find((line) => line.includes("**Start the run.**"))!
    expect(start).toContain("project-local inventory record")
    expect(start).toContain("External paths never enter `paths` or `inputs`")
    expect(start).toContain("`decision` and `limits`")
  })

  it("records manual gate substitutes at start and executes them before finalization", () => {
    const rule = lines("references/evidence.md").find((line) => line.includes("pass `gate_exemption`"))!
    expect(rule).toContain('`action: "start"`')
    expect(rule).toContain("reason and manual substitute")
    expect(rule).toContain("execute and record that substitute before finalization")
  })
})
