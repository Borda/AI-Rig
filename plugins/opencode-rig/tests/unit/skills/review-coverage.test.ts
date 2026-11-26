/** Review instructions for coverage, local verification, and axis selection. */
import { readFileSync } from "node:fs"
import { join } from "node:path"
import { describe, expect, it } from "vitest"
import { RESOURCES_DIR } from "../../helpers/harness.ts"
import { affirmedSentence, ruleSentence } from "../../helpers/instructions.ts"

const review = readFileSync(join(RESOURCES_DIR, "skills", "code-review", "SKILL.md"), "utf8").split("\n")
const handoff = readFileSync(join(RESOURCES_DIR, "references", "handoff.md"), "utf8").split("\n")
const readme = readFileSync(join(RESOURCES_DIR, "..", "..", "README.md"), "utf8").split("\n")
const changelog = readFileSync(join(RESOURCES_DIR, "..", "..", "CHANGELOG.md"), "utf8").split("\n")
const missingClass = "`not reproducible on this host: requires <CLASS>`"

/** Read the physical lines of a named Markdown section. */
function section(lines: string[], heading: string): string[] {
  const start = lines.indexOf(`## ${heading}`)
  expect(start, heading).toBeGreaterThan(-1)
  const end = lines.findIndex((line, index) => index > start && line.startsWith("## "))
  return lines.slice(start + 1, end < 0 ? undefined : end)
}

describe("review coverage instructions", () => {
  it("bases confidence on named gaps and excludes unusable contributions without substitute scores", () => {
    const output = section(review, "Output")
    const confidence = output.find((line) => line.startsWith("`Confidence`"))
    expect(affirmedSentence(confidence, "`Confidence` reflects the weakest accepted evidence")).toContain(
      "adjusts only for named gaps",
    )
    expect(confidence).toContain("state what it is and what closes it")
    const exclusions = section(review, "Rules").find((line) => line.includes("blocked or failed contribution"))
    expect(affirmedSentence(exclusions, "Exclude each null, blocked or failed contribution")).toContain(
      "name its gap once",
    )
    expect(ruleSentence(exclusions, "Never substitute a constant score", ["never", "no"])).toContain(
      "reviewer or check with no score of its own",
    )
    const row = section(handoff, "Fields").find((line) => line.startsWith("| Confidence |"))
    expect(row?.split("|")[3]).toContain("each named gap and what closes it")
    const notes = section(handoff, "Rules")
    const environment = notes.find((line) => line.includes("operating system and architecture"))
    expect(environment).toContain("facts of the run")
    expect(environment).toContain("packages that can be provisioned")
    expect(environment).toContain("never confidence gaps")
    expect(notes.some((line) => line.includes(missingClass))).toBe(true)
  })

  it("provisions eligible missing dependencies through the verification route and compares base with head", () => {
    const claims = section(review, "Improvement claim evidence")
    const provisioning = claims.find((line) => line.startsWith("Provision a missing Python or Node package"))
    expect(affirmedSentence(provisioning, "Provision a missing Python or Node package")).toContain(
      "in a throwaway verification environment",
    )
    expect(provisioning).toContain("outside the project")
    expect(review.find((line) => line.startsWith("> Read [Evidence rules]"))).toContain(
      "dependency provisioning in a throwaway verification environment outside the project",
    )
    expect(provisioning).toContain("only packages the base branch already declares and the package index supplies")
    expect(provisioning).toContain("names added by the change, direct URL or VCS references, and index overrides")
    const routes = section(review, "Rules")
    expect(routes.find((line) => line.includes("Local writes are limited"))).toContain(
      "throwaway verification environment outside the project",
    )
    expect(routes.find((line) => line.includes('Under `execution: "fork-sandbox"`'))).toContain("through `rig_sandbox`")
    const sandbox = routes.findIndex((line) => line.includes('Under `execution: "all-sandbox"`'))
    const host = routes.findIndex((line) => line.includes("A same-repository PR runs on the host"))
    expect(sandbox).toBeGreaterThan(-1)
    expect(host).toBeGreaterThan(sandbox)
    expect(routes[sandbox]).toContain("including workspace, commit and same-repository PR checks")
    const receipt = routes.find((line) => line.includes("follow the receipt"))
    expect(receipt).toContain("`disposition` and `next_action`")
    expect(receipt).toContain("`network: true`")
    const reproduction = claims.find((line) => line.startsWith("Reproduce benchmark and memory claims"))
    expect(reproduction).toContain("base against head on this host with the same method")
    expect(reproduction).toContain("relative effect")
    expect(claims.find((line) => line.includes("failed provisioning"))).toContain(
      "network, resolver conflict, unsupported platform or timeout",
    )
    expect(claims.find((line) => line.includes(missingClass))).toContain("give the owner and next check")
  })

  it.each([
    ["supported", "evidence supports the claim within the tested scope"],
    ["contradicted", "reliable counterevidence contradicts the claim"],
    ["inconclusive", "completed verification cannot resolve the claim"],
    ["unverified", "a required hardware or OS class is absent from the host"],
    ["incomplete", "verification remains undone"],
  ])("assigns %s to its evidence or execution outcome", (disposition, outcome) => {
    const line = section(review, "Improvement claim evidence").find((line) => line.startsWith(`- **${disposition}**`))
    expect(line).toContain(outcome)
  })

  it("assigns failed provisioning and unavailable execution authority to incomplete verification", () => {
    const incomplete = section(review, "Improvement claim evidence").find((line) => line.startsWith("- **incomplete**"))
    expect(incomplete).toContain("failed provisioning")
    expect(incomplete).toContain("declined or unanswered trust request")
    const rules = section(review, "Rules")
    expect(rules.find((line) => line.includes("declined or unanswered"))).toContain("mark the check incomplete")
  })

  it("keeps the handoff confidence rule subordinate to documented check exemptions", () => {
    const confidence = section(handoff, "Rules").find((line) => line.includes("unexecuted check"))
    expect(confidence).toContain("an unexecuted check required by the applicable skill")
    expect(affirmedSentence(confidence, "Honor the skill's documented check exemptions")).toBeDefined()
    expect(affirmedSentence(confidence, "Count an unexecuted manual substitute")).toContain(
      "when the exemption requires it",
    )
  })

  it("documents each confidence gap and its closing evidence in the README", () => {
    const summary = readme.find((line) => line.includes("Every handoff leads"))
    expect(summary).toContain("each named confidence gap and what would close it")
  })

  it("describes the changelog trigger as a performance or memory claim", () => {
    const pending = changelog.find((line) => line.startsWith("## "))
    const axes = section(changelog, pending?.slice(3) ?? "").find((line) => line.includes("review axes"))
    expect(axes).toContain("stated performance or memory claims")
  })

  it("promotes the axes triggered by the diff or claim within the cap and names any admitted gap", () => {
    const workflow = section(review, "Workflow")
    const selection = workflow.find((line) => line.startsWith("4. **Select axes.**"))
    expect(selection).toContain("`correctness` is mandatory and must be run")
    expect(selection).toContain("Cap at four axes below `BROAD`")
    expect(selection).toContain("Promote `docs` when the diff changes a changelog or migration guide")
    expect(selection).toContain("`security` when it changes a CI workflow file")
    expect(selection).toContain("`performance` for a stated performance or memory claim")
    expect(selection).toContain("promoted axes ahead of the other optional axes within the same cap")
    expect(selection).not.toContain("--full")
    expect(selection).toContain("does not change who may run what")
    const report = workflow.find((line) => line.startsWith("7. **Report.**"))
    expect(report).toContain("selected axis that could not run")
    expect(report).toContain("`Unreviewed axis: <axis> — <reason>` on its own line")
    expect(report).toContain("confidence gap")
  })
})
