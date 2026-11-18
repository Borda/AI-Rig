/** Plan, implement, and challenge share one delivery loop: tree, challenged plan, final challenge. */
import { readFileSync } from "node:fs"
import { join } from "node:path"
import { describe, expect, it } from "vitest"
import { RESOURCES_DIR } from "../../helpers/harness.ts"

function skill(name: string): string {
  return readFileSync(join(RESOURCES_DIR, "skills", name, "SKILL.md"), "utf8")
}

describe("delivery loop", () => {
  it("makes the plan a task tree and a challenge convergence", () => {
    const plan = skill("rig-plan")
    expect(plan).toMatch(/task tree/i)
    expect(plan).toMatch(/depth-first|coherent, independently verifiable unit/i)
    expect(plan).toMatch(/Challenge until the plan is ready|Challenge the plan/i)
    expect(plan).toMatch(/round-limit|authority-required|converged/)
    expect(plan).toMatch(/maxLoopRounds/)
  })

  it("executes leaves and requires a final independent challenge", () => {
    const implement = skill("rig-implement")
    expect(implement).toMatch(/task tree/i)
    expect(implement).toMatch(/depth-first/i)
    expect(implement).toMatch(/final independent challenge|final-challenge/i)
    expect(implement).toMatch(/resolve every finding/i)
    expect(implement).toMatch(/security, critical or high/i)
    expect(implement).toMatch(/early exit/i)
  })

  it("keeps the challenge loop distinct from the mandatory final pass", () => {
    const challenge = skill("rig-challenge")
    expect(challenge).toMatch(/non-trivial `rig-implement` run performs one mandatory final challenge/i)
  })

  it("reports the lifecycle in the handoff", () => {
    const handoff = readFileSync(join(RESOURCES_DIR, "references", "handoff.md"), "utf8")
    expect(handoff).toMatch(/plan outcome and rounds/i)
    expect(handoff).toMatch(/task-tree leaves/i)
    expect(handoff).toMatch(/final-challenge reviewer and verdict/i)
  })
})
