/** Planning, optimization, and release instructions bind usable ledger evidence. */
import { spawnSync } from "node:child_process"
import { readFileSync } from "node:fs"
import { join } from "node:path"
import { describe, expect, it } from "vitest"
import { sha256 } from "../../src/digest.ts"
import { SKILLS } from "../../src/skill-manifest.ts"
import { createHarness, Project, RESOURCES_DIR } from "../helpers/harness.ts"

/** Read a named section as separate instruction lines. */
function section(skill: string, heading: string): string[] {
  const lines = readFileSync(join(RESOURCES_DIR, "skills", skill, "SKILL.md"), "utf8").split("\n")
  const start = lines.indexOf(`## ${heading}`)
  expect(start, `${skill}: ${heading}`).toBeGreaterThan(-1)
  const end = lines.findIndex((line, index) => index > start && line.startsWith("## "))
  return lines.slice(start + 1, end < 0 ? undefined : end)
}

describe("optimization evidence", () => {
  it("records benchmark inputs after starting the run so their bytes are hashed", async () => {
    const declare = section("rig-optimize", "Workflow").find((line) => line.includes("**Declare the contract.**"))
    expect(declare).toContain('action: "start"')
    expect(declare).toContain('skill: "rig-optimize"')
    expect(declare).toContain('action: "record"')
    expect(declare).toContain("then")
    expect(declare).toContain("`inputs`")
    expect(declare).toContain("`run_id`")
    expect(declare!.indexOf('action: "start"')).toBeLessThan(declare!.indexOf('action: "record"'))

    const project = new Project()
    project.write("src/a.ts", "source")
    project.write("bench/run.js", "benchmark")
    project.write("bench/fixture.txt", "fixture")
    const harness = await createHarness(project)
    const started = await harness.call("rig_evidence", {
      action: "start",
      skill: "rig-optimize",
      paths: ["src"],
    })
    const run_id = started.metadata.run_id
    await harness.call("rig_evidence", {
      action: "record",
      run_id,
      inputs: ["bench/run.js", "bench/fixture.txt"],
    })
    const run = JSON.parse((await harness.call("rig_evidence", { action: "inspect", run_id })).content)
    expect(run.inputs).toEqual([
      { path: "bench/run.js", sha256: sha256("benchmark") },
      { path: "bench/fixture.txt", sha256: sha256("fixture") },
    ])
  })

  it.each(["kept", "reverted"])("refreshes a %s round before recording review and completing", async (outcome) => {
    const workflow = section("rig-optimize", "Workflow")
    const decide = workflow.findIndex((line) => line.includes("**Decide and record.**"))
    const review = workflow.findIndex((line) => line.includes("**Review independently.**"))
    expect(workflow[decide]).toContain('action: "refresh"')
    expect(workflow[decide]).toContain("kept or reverted")
    expect(workflow[decide]).toContain("`message`")
    expect(decide).toBeLessThan(review)
    expect(workflow[review]).toContain("refreshed")

    const project = new Project()
    project.write("src/a.ts", "baseline")
    const harness = await createHarness(project, { gates: ['node -e "process.exit(0)"'] })
    const started = await harness.call("rig_evidence", {
      action: "start",
      skill: "rig-optimize",
      paths: ["src"],
    })
    const run_id = started.metadata.run_id
    project.write("src/a.ts", "earlier kept round")
    await harness.call("rig_evidence", { action: "refresh", run_id, message: "earlier kept round" })
    project.write("src/a.ts", "candidate round")
    await expect(
      harness.call("rig_evidence", {
        action: "record",
        run_id,
        reviewer: "independent-optimizer",
      }),
    ).rejects.toThrow(/refresh the scope before recording independent review/)
    project.write("src/a.ts", outcome === "kept" ? "candidate round" : "earlier kept round")
    await harness.call("rig_evidence", { action: "refresh", run_id, message: `${outcome} round` })
    await harness.call("rig_evidence", { action: "record", run_id, reviewer: "independent-optimizer" })
    await harness.call("rig_gate", { action: "run", run_id })
    const finalized = await harness.call("rig_evidence", { action: "finalize", run_id })
    expect(finalized.metadata.ok).toBe(true)
    const run = JSON.parse((await harness.call("rig_evidence", { action: "inspect", run_id })).content)
    expect(run.status).toBe("completed")
    expect(run.review.source_digest).toBe(run.scope.digest)
    expect(run.scope.entries).toContainEqual(
      expect.objectContaining({
        path: "src/a.ts",
        sha256: sha256(outcome === "kept" ? "candidate round" : "earlier kept round"),
      }),
    )
  })
})

describe("planning evidence", () => {
  it("starts before collecting inputs and supplies a missing-gate exemption at start", async () => {
    const workflow = section("rig-plan", "Workflow")
    const start = workflow.findIndex((line) => line.includes("**Start the run.**"))
    const collect = workflow.findIndex((line) => line.includes("**Collect evidence.**"))
    expect(start).toBeGreaterThan(-1)
    expect(start).toBeLessThan(collect)
    expect(workflow[start]).toContain('action: "detect"')
    expect(workflow[start]).toContain("`gate_exemption`")
    expect(workflow[start]).toContain("manual check")
    expect(workflow[collect]).toContain('action: "record"')
    expect(workflow[collect]).toContain("`inputs`")
    expect(workflow[collect]).toContain("`run_id`")

    const project = new Project()
    project.write("src/a.ts", "authoritative source")
    const harness = await createHarness(project)
    const previous = await harness.call("rig_evidence", { action: "start", skill: "rig-distill", paths: ["src"] })
    await harness.call("rig_evidence", { action: "finalize", run_id: previous.metadata.run_id })
    const started = await harness.call("rig_evidence", {
      action: "start",
      skill: "rig-plan",
      paths: ["src"],
      gate_exemption: "Review source manually",
    })
    const run_id = started.metadata.run_id
    await harness.call("rig_evidence", { action: "record", run_id, inputs: ["src/a.ts"] })
    const run = JSON.parse((await harness.call("rig_evidence", { action: "inspect", run_id })).content)
    const old = JSON.parse(
      (
        await harness.call("rig_evidence", {
          action: "inspect",
          run_id: previous.metadata.run_id,
        })
      ).content,
    )
    expect(run.gate_exemption).toBe("Review source manually")
    expect(run.inputs).toEqual([{ path: "src/a.ts", sha256: sha256("authoritative source") }])
    expect(old.inputs).toEqual([])
    expect(old.status).toBe("completed")
  })
})

describe("release evidence", () => {
  it("records a release range as a decision rather than an ignored commit argument", () => {
    const draft = section("rig-release", "Workflow").find((line) => line.includes("**Draft the release pack.**"))
    expect(draft).toContain('action: "record"')
    expect(draft).toContain("`decision`")
    expect(draft).toContain("commit range")
    expect(draft).toContain("resolved endpoint")
    expect(draft).not.toContain("`commit`")
  })

  it("uses the package's last release tag in the workflow and routing description", () => {
    const workflow = section("rig-release", "Workflow")
    const baseline = workflow.find((line) => line.includes("**Read the baseline"))
    expect(baseline).toContain("last release tag")
    expect(baseline).toContain("version manifest")
    expect(baseline).not.toContain("this plugin family")
    expect(baseline).not.toContain("Record")
    const start = workflow.find((line) => line.includes("**Start the run.**"))
    expect(start).toContain("returned `run_id`")
    expect(start).toContain('action: "record"')
    expect(start).toContain("baseline ref and version")
    expect(start!.indexOf('action: "start"')).toBeLessThan(start!.indexOf('action: "record"'))
    const compare = workflow.find((line) => line.includes("**Compare versions.**"))
    expect(compare).toContain("baseline")
    expect(compare).not.toContain("`HEAD` plus")
    const description = readFileSync(join(RESOURCES_DIR, "skills/rig-release/SKILL.md"), "utf8")
      .split("\n")
      .find((line) => line.startsWith("description:"))
    expect(description).toContain("last release tag")
    expect(SKILLS.find((skill) => skill.id === "rig-release")?.description).toContain(
      "version baseline from the last release tag",
    )
  })

  it("prefers a declared baseline over HEAD when no release tag matches", () => {
    const baseline = section("rig-release", "Workflow").find((line) => line.includes("**Read the baseline"))
    expect(baseline).toContain("If no release tag matches")
    expect(baseline).toContain("the request or the project's declared release policy")
    expect(baseline).toContain("takes precedence over `HEAD`")
  })

  it("records the no-tag fallback as a limit and identifies previously shipped packages", () => {
    const baseline = section("rig-release", "Workflow").find((line) => line.includes("**Read the baseline"))
    expect(baseline).toContain("no release tag found, using HEAD as the first-release baseline")
    expect(baseline).toContain("limit")
    expect(baseline).toContain("a package that shipped before without tags needs the user's declared baseline")
    expect(baseline).not.toContain("stop")
    const start = section("rig-release", "Workflow").find((line) => line.includes("**Start the run.**"))
    expect(start).toContain("`limits`")
  })

  it("records package verification and explicitly finalizes the gated release", async () => {
    const workflow = section("rig-release", "Workflow")
    const verify = workflow.findIndex((line) => line.includes("**Verify the package.**"))
    const finalize = workflow.findIndex((line) => line.includes("**Finalize.**"))
    expect(verify).toBeLessThan(finalize)
    expect(workflow[verify]).toContain("configured")
    expect(workflow[verify]).toContain("`rig_gate`")
    expect(workflow[verify]).toContain('action: "record"')
    expect(workflow[verify]).toContain("`outputs`")
    expect(workflow[finalize]).toContain("`rig_evidence`")
    expect(workflow[finalize]).toContain('action: "finalize"')
    expect(workflow[finalize]).toContain("fails")

    const project = new Project()
    project.write("package.json", '{"name":"sample","version":"1.0.1"}')
    const harness = await createHarness(project, { gates: ["node -e \"console.log('package.json')\""] })
    const started = await harness.call("rig_evidence", {
      action: "start",
      skill: "rig-release",
      paths: ["package.json"],
    })
    const run_id = started.metadata.run_id
    const gate = await harness.call("rig_gate", { action: "run", run_id })
    expect(gate.content).toContain("All recorded gates passed")
    project.write(".opencode-rig/package-files.txt", "package.json\n")
    await harness.call("rig_evidence", {
      action: "record",
      run_id,
      outputs: [".opencode-rig/package-files.txt"],
    })
    const finalized = await harness.call("rig_evidence", { action: "finalize", run_id })
    expect(finalized.metadata.ok).toBe(true)
    const run = JSON.parse((await harness.call("rig_evidence", { action: "inspect", run_id })).content)
    expect(run.status).toBe("completed")
    expect(run.gates).toMatchObject([{ status: "passed", exit_code: 0 }])
    expect(run.outputs).toEqual([{ path: ".opencode-rig/package-files.txt", sha256: sha256("package.json\n") }])
  })

  it("finalizes with detected correctness gates and a directly recorded package dry-run", async () => {
    const verify = section("rig-release", "Workflow").find((line) => line.includes("**Verify the package.**"))
    expect(verify).toContain("prefer it")
    expect(verify).toContain("otherwise run `npm pack --dry-run` (or the project equivalent) directly")
    expect(verify).toContain("lint, format, typecheck, and test")
    expect(verify).not.toContain("stop and ask")
    expect(verify).toContain("run directory")
    expect(verify).toContain('action: "record"')
    expect(verify).toContain("`outputs`")

    const project = new Project()
    project.write(
      "package.json",
      JSON.stringify({
        name: "sample",
        version: "1.0.1",
        files: ["src"],
        scripts: { test: 'node -e "process.exit(0)"', pack: 'node -e "process.exit(37)"' },
      }),
    )
    project.write("src/index.js", "export const value = 1\n")
    project.write("tests/private.js", "private test fixture\n")
    const harness = await createHarness(project)
    const detected = await harness.call("rig_gate", { action: "detect" })
    expect(detected.metadata.gates).toEqual(["test"])
    const started = await harness.call("rig_evidence", {
      action: "start",
      skill: "rig-release",
      paths: ["package.json", "src"],
    })
    const run_id = started.metadata.run_id
    const gate = await harness.call("rig_gate", { action: "run", run_id })
    expect(gate.content).toContain("All recorded gates passed")
    const packed = spawnSync("npm", ["pack", "--dry-run", "--json", "--offline", "--ignore-scripts"], {
      cwd: project.root,
      encoding: "utf8",
      shell: process.platform === "win32",
      env: { ...process.env, npm_config_cache: project.path(".opencode-rig/npm-cache") },
      timeout: 10_000,
    })
    expect(packed.status, packed.stderr).toBe(0)
    const files = JSON.parse(packed.stdout)[0].files.map((file: { path: string }) => file.path)
    expect(files).toEqual(["package.json", "src/index.js"])
    const fileList = `${files.join("\n")}\n`
    const output = `.opencode-rig/runs/rig-release/${run_id}/package-files.txt`
    project.write(output, fileList)
    await harness.call("rig_evidence", { action: "record", run_id, outputs: [output] })
    const finalized = await harness.call("rig_evidence", { action: "finalize", run_id })
    expect(finalized.metadata.ok).toBe(true)
    const run = JSON.parse((await harness.call("rig_evidence", { action: "inspect", run_id })).content)
    expect(run.status).toBe("completed")
    expect(run.gates).toMatchObject([{ id: "test", source: "package.json", status: "passed", exit_code: 0 }])
    expect(run.outputs).toEqual([{ path: output, sha256: sha256(fileList) }])
  })

  it("prohibits skipped SemVer steps and versions below the selected baseline", () => {
    expect(section("rig-release", "Rules")).toContain(
      "- Never skip a SemVer step or move the version below the baseline.",
    )
  })
})
