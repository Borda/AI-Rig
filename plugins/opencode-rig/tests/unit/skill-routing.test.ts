import { describe, expect, it } from "vitest"
import { SKILLS } from "../../src/skill-manifest.ts"

describe("doctor routing", () => {
  it("routes asset curation and excludes file-changing requests", () => {
    const description = SKILLS.find((skill) => skill.id === "rig-doctor")?.description
    expect(description).toContain("Also curates shipped cards (assets)")
    expect(description).toContain("NOT for deleting runs, editing configuration or fixing files")
  })
})
