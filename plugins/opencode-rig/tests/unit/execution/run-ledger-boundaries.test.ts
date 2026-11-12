/** Run evidence keeps source obligations, reviewed plans, and storage identities bound. */
import { symlinkSync } from "node:fs"
import { expect, it } from "vitest"
import { createRun, finalizeRun, loadRun, runDirectory, saveRun, type RunRecord } from "../../../src/run.ts"
import { resolvePaths } from "../../../src/paths.ts"
import { sha256 } from "../../../src/digest.ts"
import { activeRun, loadState, rememberRun, saveState } from "../../../src/state.ts"
import { Project } from "../../helpers/harness.ts"

function allocated(skill = "rig-investigate") {
  const project = new Project()
  const paths = resolvePaths(project.root, ".opencode-rig")
  const run = createRun(paths, {
    skill,
    scope: { base: null, paths: [], entries: [], dirty: false, digest: sha256("") },
  })
  return { project, paths, run }
}

const closedFinding = {
  id: "F-first",
  severity: "medium",
  dimension: "correctness",
  file: "src/a.ts",
  title: "Guard empty input",
  evidence: "Empty input throws",
  status: "fixed",
  disposition: "Added the guard",
} as const

function remediation(run: RunRecord): RunRecord {
  return {
    ...run,
    gate_exemption: "Manual reproduction passed",
    source_run_id: "source-review",
    source_findings: { digest: sha256("source"), ids: ["F-first", "F-second"] },
    findings: {
      path: "findings.json",
      digest: sha256("findings"),
      open: 0,
      fixed: 1,
      rejected: 0,
      deferred: 0,
      accepted_risk: 0,
    },
    review: { run_id: run.run_id, reviewer: "peer", source_digest: run.scope.digest },
  } as RunRecord
}

it("does not complete remediation when a source finding was omitted", () => {
  const { run } = allocated("rig-remediate")
  const result = finalizeRun(remediation(run), {
    drift: false,
    source_findings_digest: sha256("source"),
    findings: [closedFinding],
  } as any)
  expect(result.ok).toBe(false)
  expect(result.problems.join("\n")).toContain("F-second")
})

it("does not complete remediation after its source inventory changes", () => {
  const { run } = allocated("rig-remediate")
  const result = finalizeRun(remediation(run), {
    drift: false,
    source_findings_digest: sha256("replacement"),
    findings: [closedFinding, { ...closedFinding, id: "F-second" }],
  } as any)
  expect(result.ok).toBe(false)
  expect(result.problems.join("\n")).toMatch(/source findings.*changed/i)
})

it("does not treat an undisposed terminal status as source closure", () => {
  const { run } = allocated("rig-remediate")
  const result = finalizeRun(remediation(run), {
    drift: false,
    source_findings_digest: sha256("source"),
    findings: [closedFinding, { ...closedFinding, id: "F-second", disposition: "" }],
  } as any)
  expect(result.ok).toBe(false)
  expect(result.problems.join("\n")).toMatch(/F-second.*disposition/)
})

it("accepts complete terminal dispositions from the bound source inventory", () => {
  const { run } = allocated("rig-remediate")
  expect(
    finalizeRun(remediation(run), {
      drift: false,
      source_findings_digest: sha256("source"),
      findings: [
        closedFinding,
        { ...closedFinding, id: "F-second", status: "deferred", disposition: "Separate API migration" },
      ],
    } as any).ok,
  ).toBe(true)
})

it("requires remediation source evidence even when its summary has zero open findings", () => {
  const { run } = allocated("rig-remediate")
  const bound = remediation(run)
  delete bound.source_run_id
  delete bound.source_findings
  const result = finalizeRun(bound, { drift: false })
  expect(result.ok).toBe(false)
  expect(result.problems.join("\n")).toMatch(/source.*inventory/i)
})

it("rejects a plan reviewed before the final plan bytes were bound", () => {
  const { run } = allocated("rig-plan")
  const result = finalizeRun(
    {
      ...run,
      plan: { path: "plan.md", digest: sha256("revision") },
      review: {
        run_id: run.run_id,
        reviewer: "peer",
        source_digest: run.scope.digest,
        plan_digest: sha256("original"),
      },
    } as RunRecord,
    { drift: false, plan_digest: sha256("revision") } as any,
  )
  expect(result.ok).toBe(false)
  expect(result.problems.join("\n")).toMatch(/review.*plan digest/i)
})

it("rejects implementation when its plan changes outside the source scope", () => {
  const { run } = allocated("rig-implement")
  const result = finalizeRun(
    {
      ...run,
      gate_exemption: "Manual check",
      plan: { path: "plan.md", digest: sha256("original"), run_id: "plan-run" },
    } as RunRecord,
    { drift: false, plan_digest: sha256("replacement") } as any,
  )
  expect(result.ok).toBe(false)
  expect(result.problems.join("\n")).toMatch(/plan.*changed/i)
})

it("requires reviewed plan provenance or an inline trivial plan for implementation", () => {
  const { run } = allocated("rig-implement")
  const result = finalizeRun({ ...run, gate_exemption: "Manual check" }, { drift: false })
  expect(result.ok).toBe(false)
  expect(result.problems.join("\n")).toMatch(/reviewed plan|trivial plan/)
})

it("retains the inline trivial implementation path", () => {
  const { run } = allocated("rig-implement")
  expect(
    finalizeRun(
      { ...run, gate_exemption: "Manual check", trivial_plan: "Fix the label; inspect the output" } as RunRecord,
      { drift: false },
    ).ok,
  ).toBe(true)
})

it.each(["../outside", "..\\outside", "/outside", "__proto__", "constructor"])(
  "rejects an unsafe run id before a directory can be used: %s",
  (id) => {
    const { paths, run } = allocated()
    expect(() => runDirectory(paths, run.skill, id)).toThrow(/invalid run/)
    expect(() => loadRun(paths, run.skill, id)).toThrow(/invalid run/)
    expect(() => saveRun(paths, { ...run, run_id: id })).toThrow(/invalid run/)
  },
)

it.each(["../outside", "..\\outside", "/outside", "__proto__", "constructor"])(
  "rejects an unsafe skill before a directory can be used: %s",
  (skill) => {
    const { paths, run } = allocated()
    expect(() => runDirectory(paths, skill, run.run_id)).toThrow(/invalid skill/)
    expect(() => saveRun(paths, { ...run, skill })).toThrow(/invalid skill/)
  },
)

it.each([
  { schema: 99 },
  { status: "complete" },
  { confidence: "high" },
  { gates: [{}] },
  { outputs: [{ path: "src/a.ts" }] },
  { scope: { digest: "forged" } },
])("rejects malformed persisted evidence: %j", (replacement) => {
  const { paths, run, project } = allocated()
  project.write(`.opencode-rig/runs/${run.skill}/${run.run_id}/run.json`, JSON.stringify({ ...run, ...replacement }))
  expect(() => loadRun(paths, run.skill, run.run_id)).toThrow(/invalid run record/)
})

it("rejects an otherwise valid record stored under the wrong identity", () => {
  const { paths, run, project } = allocated()
  project.write(`.opencode-rig/runs/${run.skill}/other/run.json`, JSON.stringify(run))
  expect(() => loadRun(paths, run.skill, "other")).toThrow(/identity/)
})

it("rejects malformed run records before saving them", () => {
  const { paths, run } = allocated()
  expect(() => saveRun(paths, { ...run, gates: [{}] } as RunRecord)).toThrow(/invalid run record/)
  expect(loadRun(paths, run.skill, run.run_id).gates).toEqual([])
})

it("does not load run evidence through a linked skill directory", () => {
  const { paths, project } = allocated()
  const external = new Project()
  external.write("linked/run.json", "{}")
  symlinkSync(
    external.root,
    project.path(".opencode-rig/runs/rig-review"),
    process.platform === "win32" ? "junction" : "dir",
  )
  expect(() => loadRun(paths, "rig-review", "linked")).toThrow(/symbolic|outside/)
})

it("does not write run evidence through a linked artifact ancestor outside the project", () => {
  const { project, run } = allocated()
  const external = new Project()
  external.write("artifacts/keep.txt", "original")
  const paths = resolvePaths(project.root, "linked/artifacts")
  symlinkSync(external.root, project.path("linked"), process.platform === "win32" ? "junction" : "dir")
  expect(() => saveRun(paths, run)).toThrow(/outside/)
  expect(external.exists(`artifacts/runs/${run.skill}/${run.run_id}/run.json`)).toBe(false)
  expect(external.read("artifacts/keep.txt")).toBe("original")
})

it("does not trust malformed nested evidence during active-run lookup or recovery", () => {
  const { paths, run, project } = allocated()
  rememberRun(paths.artifactRoot, "session", { runId: run.run_id, skill: run.skill })
  project.write(`.opencode-rig/runs/${run.skill}/${run.run_id}/run.json`, JSON.stringify({ ...run, gates: [{}] }))
  expect(() => activeRun(paths.artifactRoot, "session")).toThrow(/invalid run record/)
  project.write(".opencode-rig/state.json", "{broken")
  expect(loadState(paths.artifactRoot).runs).toEqual({})
})

it("refuses a linked artifact root before terminal-session cleanup can write outside the project", () => {
  const project = new Project()
  const { paths, run } = allocated()
  rememberRun(paths.artifactRoot, "session", { runId: run.run_id, skill: run.skill })
  saveRun(paths, { ...run, status: "completed" })
  const state = loadState(paths.artifactRoot)
  const linkedRoot = project.path("linked-artifacts")
  symlinkSync(paths.artifactRoot, linkedRoot, process.platform === "win32" ? "junction" : "dir")
  expect(() => activeRun(linkedRoot, "session")).toThrow(/symbolic|symlink/)
  expect(() => loadState(linkedRoot)).toThrow(/symbolic|symlink/)
  expect(() => saveState(linkedRoot, { schema: 1, runs: {}, sessions: {} })).toThrow(/symbolic|symlink/)
  expect(loadState(paths.artifactRoot)).toEqual(state)
})

it("refuses a linked state file while preserving its target and local evidence", () => {
  const { paths, run, project } = allocated()
  const external = new Project()
  const state = {
    schema: 1,
    runs: { [run.run_id]: run.skill },
    sessions: { session: { runId: run.run_id, skill: run.skill } },
  }
  const target = external.write("state.json", JSON.stringify(state))
  symlinkSync(target, project.path(".opencode-rig/state.json"))
  expect(() => loadState(paths.artifactRoot)).toThrow(/symbolic|symlink/)
  expect(() => activeRun(paths.artifactRoot, "session")).toThrow(/symbolic|symlink/)
  expect(() => saveState(paths.artifactRoot, state)).toThrow(/symbolic|symlink/)
  expect(external.read("state.json")).toBe(JSON.stringify(state))
  expect(project.read(`.opencode-rig/runs/${run.skill}/${run.run_id}/run.json`)).toContain(run.run_id)
})
