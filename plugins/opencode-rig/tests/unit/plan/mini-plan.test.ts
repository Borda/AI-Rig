/**
 * A trivial implementation starts from a structured mini-plan, so "this is
 * small and understood" is a checked statement rather than an assumption.
 */
import { describe, expect, it } from "vitest"
import { MAX_MINI_PLAN_PATHS, miniPlanProblems, parseMiniPlan } from "../../../src/mini-plan.ts"

const valid = {
  goal: "Return the corrected value",
  owned_paths: ["src/a.ts"],
  acceptance_check: "npx vitest run tests/a.test.ts",
  feasibility: "One known return value covered by an existing test.",
}

describe("mini-plan validation", () => {
  it("accepts a complete plan with one or two owned files", () => {
    expect(miniPlanProblems(JSON.stringify(valid))).toEqual([])
    expect(miniPlanProblems(JSON.stringify({ ...valid, owned_paths: ["src/a.ts", "tests/a.test.ts"] }))).toEqual([])
  })

  it("accepts the plan in a json fence and reads the last block", () => {
    const text = ["Some prose.", "", "```json", JSON.stringify(valid, null, 2), "```"].join("\n")
    expect(parseMiniPlan(text)).toMatchObject({ goal: valid.goal })
    expect(miniPlanProblems(text)).toEqual([])
    const twoBlocks = [
      "```json",
      JSON.stringify({ ...valid, goal: "A documentation example" }),
      "```",
      "The real plan:",
      "```json",
      JSON.stringify(valid, null, 2),
      "```",
    ].join("\n")
    expect(parseMiniPlan(twoBlocks)).toMatchObject({ goal: valid.goal })
  })

  it("rejects free text, because nothing can be checked", () => {
    expect(miniPlanProblems("Update the constant and run the test")).toEqual([
      "mini-plan must be a JSON object, optionally in a ```json fence",
    ])
    expect(miniPlanProblems("")).toEqual(["a trivial plan needs a mini-plan JSON object"])
  })

  it("requires every field that makes the change checkable", () => {
    const problems = miniPlanProblems(JSON.stringify({}))
    expect(problems).toContain("goal is required")
    expect(problems).toContain("acceptance_check is required")
    expect(problems).toContain("feasibility is required")
    expect(problems).toContain("owned_paths must list at least one path")
  })

  it("caps the change at the trivial file count", () => {
    const paths = ["a.ts", "b.ts", "c.ts"].slice(0, MAX_MINI_PLAN_PATHS + 1)
    const problems = miniPlanProblems(JSON.stringify({ ...valid, owned_paths: paths }))
    expect(problems.join("\n")).toContain("at most 2 files")
  })

  it("rejects a path that is not an ordinary project-relative file", () => {
    for (const path of [
      "/etc/passwd",
      "C:\\repo\\a.ts",
      "~/a.ts",
      "src/../a.ts",
      "src/*.ts",
      "src/",
      ".",
      ".git/config",
      ".git/hooks/pre-commit",
    ]) {
      const problems = miniPlanProblems(JSON.stringify({ ...valid, owned_paths: [path] }))
      expect(problems.length, path).toBeGreaterThan(0)
    }
  })

  it("rejects a repeated path and an unknown schema", () => {
    expect(miniPlanProblems(JSON.stringify({ ...valid, owned_paths: ["src/a.ts", "src/a.ts"] }))).toContain(
      "owned_paths repeats a path",
    )
    expect(miniPlanProblems(JSON.stringify({ ...valid, schema: 2 }))).toContain("mini-plan schema must be 1")
  })
})
