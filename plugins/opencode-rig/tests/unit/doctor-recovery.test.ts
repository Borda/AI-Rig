/** Doctor expires abandoned evidence and identifies unavailable ignore checks. */
import { utimesSync } from "node:fs"
import { afterEach, expect, it, vi } from "vitest"
import { doctorReport, staleRuns } from "../../src/doctor.ts"
import { DEFAULT_OPTIONS } from "../../src/options.ts"
import { PACKAGE_ROOT, Project } from "../helpers/harness.ts"

afterEach(() => vi.unstubAllEnvs())

it("expires abandoned active and unreadable runs beyond the hard cap", () => {
  const project = new Project()
  const now = new Date("2026-11-11")
  const old = new Date("2020-01-01")
  project.write("runs/review/active/run.json", JSON.stringify({ status: "in_progress" }))
  project.write("runs/review/corrupt/run.json", "{bad")
  project.write("runs/review/missing/other", "no record")
  project.write("runs/review/recent/run.json", JSON.stringify({ status: "in_progress" }))
  for (const name of ["active", "corrupt", "missing"]) {
    utimesSync(project.path(`runs/review/${name}`), old, old)
    if (name !== "missing") utimesSync(project.path(`runs/review/${name}/run.json`), old, old)
  }
  expect(staleRuns(project.path("runs"), 30, now)).toEqual([
    project.path("runs/review/active"),
    project.path("runs/review/corrupt"),
    project.path("runs/review/missing"),
  ])
})

it("does not expire an active record recently updated inside an old directory", () => {
  const project = new Project()
  project.write("runs/review/live/run.json", JSON.stringify({ status: "in_progress" }))
  utimesSync(project.path("runs/review/live"), new Date("2000-01-01"), new Date("2000-01-01"))
  expect(staleRuns(project.path("runs"), 30)).toEqual([])
})

it.each(["non-repository", "git unavailable", "outside project"])(
  "explains an unavailable ignore check: %s",
  (reason) => {
    const project = reason === "non-repository" ? new Project() : Project.withCommit()
    const external = new Project()
    if (reason === "git unavailable") vi.stubEnv("PATH", external.root)
    const artifactRoot = reason === "outside project" ? external.root : project.path("artifacts")
    const result = doctorReport({
      packageRoot: PACKAGE_ROOT,
      projectRoot: project.root,
      artifactRoot,
      pluginListed: true,
      options: DEFAULT_OPTIONS,
    })
    expect(result.lines.join("\n")).toContain(`artifact ignore check unavailable: ${reason}`)
    expect(result.lines.join("\n")).not.toContain("add '.opencode-rig/'")
  },
)
