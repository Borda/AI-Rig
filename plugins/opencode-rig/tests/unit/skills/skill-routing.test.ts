import { describe, expect, it } from "vitest"
import { SKILLS } from "../../../src/skill-manifest.ts"

describe("doctor routing", () => {
  it("routes asset curation and confirmed cleanup while excluding unrelated edits", () => {
    const description = SKILLS.find((skill) => skill.id === "rig-doctor")?.description
    expect(description).toContain("Also curates shipped cards (assets)")
    expect(description).toContain("deletes stale runs only through explicitly confirmed cleanup")
    expect(description).toContain("NOT for editing configuration or fixing files")
  })
})
