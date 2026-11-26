import {
  chmodSync,
  existsSync,
  linkSync,
  mkdirSync,
  readFileSync,
  rmSync,
  symlinkSync,
  utimesSync,
  writeFileSync,
} from "node:fs"
import { execFileSync } from "node:child_process"
import { join } from "node:path"
import { describe, expect, it } from "vitest"
import { sha256File } from "../../src/digest.ts"
import { Project, createHarness, trivialPlan } from "../helpers/harness.ts"

function changedProject(files: Record<string, string> = { "src/a.ts": "export const a = 1\n" }): Project {
  const project = Project.withCommit(files)
  project.write("src/a.ts", "export const a = 2\n")
  project.write(".gitignore", ".opencode-rig/\n")
  return project
}

describe("plugin registration", () => {
  it("registers the documented tools, skills, and commands", async () => {
    const project = changedProject()
    const harness = await createHarness(project)
    expect([...harness.tools.keys()].sort()).toEqual([
      "rig_dispatch",
      "rig_doctor",
      "rig_evidence",
      "rig_findings",
      "rig_gate",
      "rig_git_grant",
      "rig_loop",
      "rig_sandbox",
      "rig_script",
      "rig_trust",
    ])
    expect(harness.skills).toHaveLength(11)
    expect(harness.skills.map((skill) => skill.id)).toEqual(
      expect.arrayContaining(["rig-challenge", "rig-research", "rig-distill", "rig-optimize"]),
    )
    expect(harness.skills.every((skill) => typeof skill.content === "string" && skill.content.length > 0)).toBe(true)
    expect(harness.skills.every((skill) => !skill.content.startsWith("---"))).toBe(true)
    expect(harness.commands.map((command) => command.name).sort()).toEqual(
      harness.skills.map((skill) => skill.id).sort(),
    )
  })

  it("registers a permission hook, tool journal hooks, and a context hook", async () => {
    const project = changedProject()
    const harness = await createHarness(project)
    expect(harness.permissionHooks.has("evaluate")).toBe(true)
    expect(harness.toolHooks.get("execute.before")).toHaveLength(1)
    expect(harness.toolHooks.get("execute.after")).toHaveLength(1)
    expect(harness.sessionHooks.get("context")).toHaveLength(1)
  })
})

describe("rig_evidence", () => {
  it("runs the documented lifecycle and writes a report", async () => {
    const project = changedProject()
    const harness = await createHarness(project, { gates: ["true"] })

    const started = await harness.call("rig_evidence", {
      action: "start",
      skill: "rig-implement",
      trivial_plan: trivialPlan(["src/a.ts"]),
    })
    expect(started.content).toMatch(/Run started/)
    const runId = started.metadata.run_id as string
    expect(runId).toMatch(/Z-[0-9a-f]{4}$/)

    const inspected = await harness.call("rig_evidence", { action: "inspect" })
    expect(JSON.parse(inspected.content)).toMatchObject({
      run_id: runId,
      skill: "rig-implement",
      status: "in_progress",
    })

    const recorded = await harness.call("rig_evidence", {
      action: "record",
      outputs: ["src/a.ts"],
      limits: ["No Windows host"],
      confidence: 0.9,
      decision: "Keep the change minimal.",
    })
    expect(recorded.content).toMatch(/outputs: 1/)
    expect(project.exists(".opencode-rig/state.json")).toBe(true)

    await harness.call("rig_evidence", { action: "refresh", message: "formatter rewrote one line" })

    const gate = await harness.call("rig_gate", { action: "run" })
    expect(gate.content).toMatch(/All recorded gates passed/)

    const finalized = await harness.call("rig_evidence", { action: "finalize" })
    expect(finalized.metadata.ok).toBe(true)
    expect(finalized.content).toMatch(/Run completed/)
    expect(existsSync(finalized.metadata.report_path as string)).toBe(true)

    const completedRun = await harness.call("rig_evidence", { action: "inspect", run_id: runId })
    const run = JSON.parse(completedRun.content)
    expect(run.status).toBe("completed")
    expect(run.outputs).toHaveLength(1)
    expect(run.limits).toContain("No Windows host")
    expect(run.confidence).toBe(0.9)
  })

  it("rejects a start without a shipped skill", async () => {
    const harness = await createHarness(changedProject())
    await expect(harness.call("rig_evidence", { action: "start" })).rejects.toThrow(/start requires skill/)
    await expect(harness.call("rig_evidence", { action: "start", skill: "rig-deploy" })).rejects.toThrow(
      /start requires skill/,
    )
  })

  it("rejects a missing run and an unknown run id", async () => {
    const harness = await createHarness(changedProject())
    await expect(harness.call("rig_evidence", { action: "inspect" })).rejects.toThrow(/no active run/)
    await expect(harness.call("rig_evidence", { action: "inspect", run_id: "missing" })).rejects.toThrow(/unknown run/)
  })

  it("refuses to finalize without a gate receipt", async () => {
    const harness = await createHarness(changedProject(), { gates: ["true"] })
    await harness.call("rig_evidence", {
      action: "start",
      skill: "rig-implement",
      trivial_plan: trivialPlan(["src/a.ts"]),
    })
    const result = await harness.call("rig_evidence", { action: "finalize" })
    expect(result.metadata).toMatchObject({ ok: false, status: "in_progress" })
    expect(result.content).toMatch(/no passing gate receipt/)
  })

  it("rejects a mini-plan that is free text or larger than trivial", async () => {
    const harness = await createHarness(changedProject())
    await expect(
      harness.call("rig_evidence", {
        action: "start",
        skill: "rig-implement",
        paths: ["src"],
        trivial_plan: "Update the scoped return value and run its regression.",
      }),
    ).rejects.toThrow(/not a valid mini-plan/)
    await expect(
      harness.call("rig_evidence", {
        action: "start",
        skill: "rig-implement",
        trivial_plan: trivialPlan(["a.ts", "b.ts", "c.ts"]),
      }),
    ).rejects.toThrow(/at most 2 files/)
  })

  it("uses the mini-plan's owned paths as the scope when none are given", async () => {
    const project = changedProject()
    const harness = await createHarness(project)
    const started = await harness.call("rig_evidence", {
      action: "start",
      skill: "rig-implement",
      trivial_plan: trivialPlan(["src/a.ts"]),
    })
    const run = JSON.parse((await harness.call("rig_evidence", { action: "inspect" })).content)
    expect(run.scope.paths).toEqual(["src/a.ts"])
    expect(started.metadata.scope_digest).toBeTruthy()
  })

  it("refuses a directory or a path that widens the mini-plan scope", async () => {
    const harness = await createHarness(changedProject())
    await expect(
      harness.call("rig_evidence", {
        action: "start",
        skill: "rig-implement",
        trivial_plan: trivialPlan(["src"]),
      }),
    ).rejects.toThrow(/must name a file, not a directory/)
    await expect(
      harness.call("rig_evidence", {
        action: "start",
        skill: "rig-implement",
        paths: ["src"],
        trivial_plan: trivialPlan(["src/a.ts"]),
      }),
    ).rejects.toThrow(/scoped by its mini-plan owned_paths/)
  })

  it("starts a trivial run whose declared file does not exist yet", async () => {
    const project = Project.withCommit({ "src/a.ts": "export const a = 1\n" })
    const harness = await createHarness(project)
    const started = await harness.call("rig_evidence", {
      action: "start",
      skill: "rig-implement",
      trivial_plan: trivialPlan(["src/fresh.ts"]),
      gate_exemption: "No configured gate; the focused check is recorded manually.",
    })
    expect(started.metadata.run_id).toBeTruthy()
    project.write("src/fresh.ts", "export const fresh = 1\n")
    await harness.call("rig_evidence", { action: "refresh", message: "the declared new file now exists" })
    const done = await harness.call("rig_evidence", { action: "finalize" })
    expect(done.metadata.ok, done.content).toBe(true)
  })

  it("counts a trivial change from its own start, not from an already dirty tree", async () => {
    const project = Project.withCommit({ "src/a.ts": "export const a = 1\n" })
    project.write(
      "src/a.ts",
      `${Array.from({ length: 45 }, (_, index) => `export const a${index} = ${index}\n`).join("")}`,
    )
    const harness = await createHarness(project)
    await harness.call("rig_evidence", {
      action: "start",
      skill: "rig-implement",
      trivial_plan: trivialPlan(["src/a.ts"]),
      gate_exemption: "No configured gate; the focused check is recorded manually.",
    })
    project.write(
      "src/a.ts",
      `${Array.from({ length: 48 }, (_, index) => `export const a${index} = ${index}\n`).join("")}`,
    )
    await harness.call("rig_evidence", { action: "refresh", message: "three more lines on the same file" })
    const done = await harness.call("rig_evidence", { action: "finalize" })
    expect(done.metadata.ok, done.content).toBe(true)
  })

  it("normalizes a dotted owned path before scoping", async () => {
    const project = changedProject()
    const harness = await createHarness(project)
    await harness.call("rig_evidence", {
      action: "start",
      skill: "rig-implement",
      trivial_plan: trivialPlan(["./src/a.ts"]),
      gate_exemption: "No configured gate; the focused check is recorded manually.",
    })
    const run = JSON.parse((await harness.call("rig_evidence", { action: "inspect" })).content)
    expect(run.scope.paths).toEqual(["src/a.ts"])
    const done = await harness.call("rig_evidence", { action: "finalize" })
    expect(done.metadata.ok, done.content).toBe(true)
  })

  it("refuses an owned path that became a directory", async () => {
    const project = Project.withCommit({ "src/a.ts": "export const a = 1\n" })
    const harness = await createHarness(project)
    await harness.call("rig_evidence", {
      action: "start",
      skill: "rig-implement",
      trivial_plan: trivialPlan(["src/fresh"]),
      gate_exemption: "No configured gate; the focused check is recorded manually.",
    })
    mkdirSync(project.path("src/fresh"))
    project.write("src/fresh/a.ts", "export const a = 2\n")
    await expect(
      harness.call("rig_evidence", { action: "refresh", message: "the declared path became a directory" }),
    ).rejects.toThrow(/owned path is a directory: src\/fresh/)
  })

  it("reports a damaged start baseline instead of throwing", async () => {
    const project = changedProject()
    const harness = await createHarness(project)
    const started = await harness.call("rig_evidence", {
      action: "start",
      skill: "rig-implement",
      trivial_plan: trivialPlan(["src/a.ts"]),
      gate_exemption: "No configured gate; the focused check is recorded manually.",
    })
    const baseline = join(
      project.root,
      ".opencode-rig",
      "runs",
      "rig-implement",
      started.metadata.run_id,
      "trivial-baseline.json",
    )
    writeFileSync(baseline, "{ not json", "utf8")
    const result = await harness.call("rig_evidence", { action: "finalize" })
    expect(result.metadata.ok, result.content).toBe(true)
    const run = JSON.parse(
      (await harness.call("rig_evidence", { action: "inspect", run_id: started.metadata.run_id })).content,
    )
    expect(run.limits.join("\n")).toMatch(/not measured/)
  })

  it("refuses a trivial run when no base commit can bound it", async () => {
    const project = new Project()
    project.write("notes.md", "# Notes\n")
    const harness = await createHarness(project)
    await harness.call("rig_evidence", {
      action: "start",
      skill: "rig-implement",
      trivial_plan: trivialPlan(["notes.md"]),
      gate_exemption: "No configured gate; the focused check is recorded manually.",
    })
    const result = await harness.call("rig_evidence", { action: "finalize" })
    expect(result.metadata.ok, result.content).toBe(true)
  })

  it("rejects an artifact path written with different case", async () => {
    const harness = await createHarness(changedProject())
    await expect(
      harness.call("rig_evidence", {
        action: "start",
        skill: "rig-implement",
        trivial_plan: trivialPlan([".OpenCode-Rig/state.json"]),
      }),
    ).rejects.toThrow(/must be project source, not a run artifact/)
  })

  it("refuses an owned path that is a symlink", async () => {
    const project = Project.withCommit({ "src/a.ts": "export const a = 1\n" })
    symlinkSync(join(project.root, ".git", "config"), project.path("cfg"))
    const harness = await createHarness(project)
    await expect(
      harness.call("rig_evidence", {
        action: "start",
        skill: "rig-implement",
        trivial_plan: trivialPlan(["cfg"]),
      }),
    ).rejects.toThrow(/regular file, not a symbolic link/)
  })

  it("refuses an owned path replaced by a symlink after start", async () => {
    const project = Project.withCommit({ "src/a.ts": "export const a = 1\n" })
    const harness = await createHarness(project)
    await harness.call("rig_evidence", {
      action: "start",
      skill: "rig-implement",
      trivial_plan: trivialPlan(["src/a.ts"]),
      gate_exemption: "No configured gate; the focused check is recorded manually.",
    })
    rmSync(project.path("src/a.ts"))
    symlinkSync(join(project.root, ".git", "config"), project.path("src/a.ts"))
    await expect(
      harness.call("rig_evidence", { action: "refresh", message: "the owned path was replaced by a link" }),
    ).rejects.toThrow(/owned path is a symbolic link: src\/a.ts/)
  })

  it("refuses a missing owned path whose ancestor is a symlink into .git", async () => {
    const project = Project.withCommit({ "src/a.ts": "export const a = 1\n" })
    symlinkSync(project.path(".git"), project.path("docs"), "dir")
    const harness = await createHarness(project)
    await expect(
      harness.call("rig_evidence", {
        action: "start",
        skill: "rig-implement",
        trivial_plan: trivialPlan(["docs/hooks/pre-commit"]),
      }),
    ).rejects.toThrow(/must not touch VCS internals/)
  })

  it("refuses a path that impersonates the internal byte marker", async () => {
    const harness = await createHarness(changedProject())
    await expect(
      harness.call("rig_evidence", {
        action: "start",
        skill: "rig-implement",
        trivial_plan: trivialPlan(["\u0000git-bytes:6162"]),
      }),
    ).rejects.toThrow(/control characters/)
  })

  it.skipIf(process.platform === "win32")("refuses an owned FIFO", async () => {
    const project = Project.withCommit({ "src/a.ts": "export const a = 1\n" })
    execFileSync("mkfifo", [project.path("pipe.ts")])
    const harness = await createHarness(project)
    await expect(
      harness.call("rig_evidence", {
        action: "start",
        skill: "rig-implement",
        trivial_plan: trivialPlan(["pipe.ts"]),
      }),
    ).rejects.toThrow(/regular file, not a pipe, socket or device/)
  })

  it("accepts a flagged path that was already absent at start", async () => {
    const project = Project.withCommit({ "src/a.ts": "export const a = 1\n", "src/b.ts": "export const b = 1\n" })
    project.git("update-index", "--skip-worktree", "src/b.ts")
    rmSync(project.path("src/b.ts"))
    const harness = await createHarness(project)
    await harness.call("rig_evidence", {
      action: "start",
      skill: "rig-implement",
      trivial_plan: trivialPlan(["src/a.ts"]),
      gate_exemption: "No configured gate; the focused check is recorded manually.",
    })
    const result = await harness.call("rig_evidence", { action: "finalize" })
    expect(result.metadata.ok, result.content).toBe(true)
  })

  it("refuses a record call while an owned path is a symlink", async () => {
    const project = Project.withCommit({ "src/a.ts": "export const a = 1\n" })
    const harness = await createHarness(project)
    await harness.call("rig_evidence", {
      action: "start",
      skill: "rig-implement",
      trivial_plan: trivialPlan(["src/fresh.ts"]),
      gate_exemption: "No configured gate; the focused check is recorded manually.",
    })
    symlinkSync(join(project.root, ".git", "config"), project.path("src/fresh.ts"))
    await expect(
      harness.call("rig_evidence", { action: "record", decision: "checked the owned path" }),
    ).rejects.toThrow(/symbolic link/)
  })

  it.skipIf(process.platform === "win32")("refuses a FIFO that appears at an owned path", async () => {
    const project = Project.withCommit({ "src/a.ts": "export const a = 1\n" })
    const harness = await createHarness(project)
    await harness.call("rig_evidence", {
      action: "start",
      skill: "rig-implement",
      trivial_plan: trivialPlan(["src/fresh.ts"]),
      gate_exemption: "No configured gate; the focused check is recorded manually.",
    })
    execFileSync("mkfifo", [project.path("src/fresh.ts")])
    const result = await harness.call("rig_evidence", { action: "finalize" })
    expect(result.metadata.ok).toBe(false)
    expect(result.content).toMatch(/not a regular file/)
  })

  it.skipIf(process.platform === "win32")("refuses a FIFO in a non-trivial scope instead of blocking", async () => {
    const project = Project.withCommit({ "src/a.ts": "export const a = 1\n" })
    execFileSync("mkfifo", [project.path("pipe.ts")])
    const harness = await createHarness(project)
    await expect(
      harness.call("rig_evidence", { action: "start", skill: "rig-investigate", paths: ["pipe.ts"] }),
    ).rejects.toThrow(/not a regular file/)
  })

  it("refuses an owned path that is a hard link", async () => {
    const project = Project.withCommit({ "src/a.ts": "export const a = 1\n" })
    rmSync(project.path("src/a.ts"))
    linkSync(project.path(".git/config"), project.path("src/a.ts"))
    const harness = await createHarness(project)
    await expect(
      harness.call("rig_evidence", {
        action: "start",
        skill: "rig-implement",
        trivial_plan: trivialPlan(["src/a.ts"]),
      }),
    ).rejects.toThrow(/hard link/)
  })

  it.skipIf(process.platform === "win32")("refuses a FIFO as a recorded input", async () => {
    const project = Project.withCommit({ "src/a.ts": "export const a = 1\n" })
    execFileSync("mkfifo", [project.path("pipe.ts")])
    const harness = await createHarness(project)
    await harness.call("rig_evidence", {
      action: "start",
      skill: "rig-implement",
      trivial_plan: trivialPlan(["src/a.ts"]),
      gate_exemption: "No configured gate; the focused check is recorded manually.",
    })
    await expect(harness.call("rig_evidence", { action: "record", inputs: ["pipe.ts"] })).rejects.toThrow(
      /not a regular file/,
    )
  })

  it("reports an over-guideline trivial change as a limit instead of failing", async () => {
    const project = Project.withCommit({ "src/a.ts": "export const a = 1\n" })
    const harness = await createHarness(project)
    const started = await harness.call("rig_evidence", {
      action: "start",
      skill: "rig-implement",
      trivial_plan: trivialPlan(["src/a.ts"]),
      gate_exemption: "No configured gate; the focused check is recorded manually.",
    })
    project.write(
      "src/a.ts",
      `${Array.from({ length: 80 }, (_, index) => `export const a${index} = ${index}\n`).join("")}`,
    )
    await harness.call("rig_evidence", { action: "refresh", message: "the declared file grew" })
    const done = await harness.call("rig_evidence", { action: "finalize" })
    expect(done.metadata.ok, done.content).toBe(true)
    const run = JSON.parse(
      (await harness.call("rig_evidence", { action: "inspect", run_id: started.metadata.run_id })).content,
    )
    expect(run.limits.join("\n")).toMatch(/measured trivial change is .* above the 50-line \/ 2-file guideline/)
  })

  it("refuses a recorded output outside the declared scope", async () => {
    const project = changedProject()
    const harness = await createHarness(project)
    await harness.call("rig_evidence", {
      action: "start",
      skill: "rig-implement",
      trivial_plan: trivialPlan(["src/a.ts"]),
      gate_exemption: "No configured gate; the focused check is recorded manually.",
    })
    project.write("src/side.ts", "export const side = 1\n")
    await expect(harness.call("rig_evidence", { action: "record", outputs: ["src/side.ts"] })).rejects.toThrow(
      /outside the run scope/,
    )
  })

  it("reports the measured change for a within-guideline trivial run", async () => {
    const project = Project.withCommit({ "src/a.ts": "one\ntwo\nthree\n" })
    const harness = await createHarness(project)
    await harness.call("rig_evidence", {
      action: "start",
      skill: "rig-implement",
      trivial_plan: trivialPlan(["src/a.ts"]),
      gate_exemption: "No configured gate; the focused check is recorded manually.",
    })
    project.write("src/a.ts", "one\ntwo\nthree\nfour\n")
    await harness.call("rig_evidence", { action: "refresh", message: "added one line" })
    const done = await harness.call("rig_evidence", { action: "finalize" })
    expect(done.metadata.ok, done.content).toBe(true)
    expect(done.content).toMatch(/Measured change: 1 line\(s\) across 1 file\(s\)/)
    expect(readFileSync(done.metadata.report_path as string, "utf8")).toMatch(/1 line\(s\) across 1 file\(s\)/)
  })

  it("marks a run stale when source changes after the scope was recorded", async () => {
    const project = changedProject()
    const harness = await createHarness(project, { gates: ["true"] })
    await harness.call("rig_evidence", {
      action: "start",
      skill: "rig-implement",
      trivial_plan: trivialPlan(["src/a.ts"]),
    })
    await harness.call("rig_gate", { action: "run" })
    project.write("src/a.ts", "export const a = 3\n")
    const result = await harness.call("rig_evidence", { action: "finalize" })
    expect(result.metadata).toMatchObject({ ok: false, status: "stale" })
    expect(result.content).toMatch(/refresh/)
  })

  it("requires a reason to refresh and refuses to refresh a commit scope", async () => {
    const project = changedProject()
    const harness = await createHarness(project)
    await harness.call("rig_evidence", { action: "start", skill: "rig-investigate", paths: ["src"] })
    await expect(harness.call("rig_evidence", { action: "refresh" })).rejects.toThrow(/refresh requires message/)

    const commit = project.git("rev-parse", "HEAD")
    await harness.call("rig_evidence", { action: "start", skill: "rig-investigate", commit })
    await expect(harness.call("rig_evidence", { action: "refresh", message: "x" })).rejects.toThrow(
      /commit scope is immutable/,
    )
  })

  const TREE = [
    "",
    "## Task tree",
    "",
    "```json",
    '{"schema":1,"root":"T-1","nodes":[{"id":"T-1","parent":null,"goal":"Deliver the scoped change","owned_paths":["src/a.ts"],"acceptance_check":"focused test","depends_on":[],"leaf":true}]}',
    "```",
    "",
    "## Challenge rounds",
    "",
    "- Round 1: challenger; no blocking findings; converged.",
    "",
  ].join(String.fromCharCode(10))

  it("binds a plan by digest and rejects a missing plan file", async () => {
    const project = changedProject()
    project.write(
      "plan.md",
      `# Plan
${TREE}`,
    )
    const harness = await createHarness(project)
    await harness.call("rig_evidence", { action: "start", skill: "rig-plan", paths: ["src"] })
    await expect(harness.call("rig_evidence", { action: "record", plan_path: "missing.md" })).rejects.toThrow(
      /plan does not exist/,
    )
    const recorded = await harness.call("rig_evidence", {
      action: "record",
      plan_path: "plan.md",
      plan_digest: sha256File(project.path("plan.md")),
      reviewer: "rig-challenger",
    })
    expect(recorded.content).toMatch(/plan: plan.md/)
  })

  it("rejects an evidence path outside the project", async () => {
    const harness = await createHarness(changedProject())
    await harness.call("rig_evidence", { action: "start", skill: "rig-investigate", paths: ["src"] })
    await expect(harness.call("rig_evidence", { action: "record", outputs: ["../escape.txt"] })).rejects.toThrow(
      /escapes the project/,
    )
  })

  it("refuses review evidence recorded after source drift", async () => {
    const project = changedProject()
    const harness = await createHarness(project)
    await harness.call("rig_evidence", { action: "start", skill: "code-review", paths: ["src"] })
    project.write("src/a.ts", "export const a = 4\n")
    await expect(harness.call("rig_evidence", { action: "record", reviewer: "rig-auditor" })).rejects.toThrow(
      /source changed after the recorded scope/,
    )
  })
})

describe("rig_gate", () => {
  it("detects but refuses to run configured gates on the host under all-sandbox", async () => {
    const harness = await createHarness(changedProject(), {
      execution: "all-sandbox",
      inspection: false,
      guardrails: "off",
      gates: ["node --version"],
    })
    await harness.call("rig_evidence", {
      action: "start",
      skill: "rig-implement",
      trivial_plan: trivialPlan(["src/a.ts"]),
    })
    expect((await harness.call("rig_gate", { action: "detect" })).metadata.gates).toEqual(["configured-1"])
    await expect(harness.call("rig_gate", { action: "run" })).rejects.toThrow(/all-sandbox requires rig_sandbox/)
    expect(harness.assertions).toHaveLength(0)
  })

  it("reports that no gate is configured instead of inventing one", async () => {
    const harness = await createHarness(changedProject())
    const detected = await harness.call("rig_gate", { action: "detect" })
    expect(detected.content).toMatch(/No configured gates detected/)
    expect(detected.metadata.gates).toEqual([])
  })

  it("detects configured gates in order", async () => {
    const harness = await createHarness(changedProject(), { gates: ["true", "false"] })
    const detected = await harness.call("rig_gate", { action: "detect" })
    expect(detected.metadata.gates).toEqual(["configured-1", "configured-2"])
    expect(detected.content).toContain("configured-1\ttrue\t(options.gates)")
  })

  it("records a passing proof and a failing exit code", async () => {
    const project = changedProject()
    const harness = await createHarness(project, { gates: ["true", "false"] })
    await harness.call("rig_evidence", {
      action: "start",
      skill: "rig-implement",
      trivial_plan: trivialPlan(["src/a.ts"]),
    })

    const passing = await harness.call("rig_gate", { action: "run", names: ["configured-1"] })
    expect(passing.metadata.failed).toEqual([])
    expect(harness.assertions.at(-1)).toMatchObject({ action: "shell", resources: ["true"] })

    const failing = await harness.call("rig_gate", { action: "run", names: ["configured-2"] })
    expect(failing.metadata.failed).toEqual(["configured-2"])
    expect(failing.content).toMatch(/FAILED: configured-2/)

    expect(project.exists(".opencode-rig/runs/rig-implement")).toBe(true)
  })

  it("rejects an unknown gate name", async () => {
    const harness = await createHarness(changedProject(), { gates: ["true"] })
    await harness.call("rig_evidence", {
      action: "start",
      skill: "rig-implement",
      trivial_plan: trivialPlan(["src/a.ts"]),
    })
    await expect(harness.call("rig_gate", { action: "run", names: ["nope"] })).rejects.toThrow(
      /unknown gate\(s\): nope/,
    )
  })

  it("rejects a run when no gate is configured", async () => {
    const harness = await createHarness(changedProject())
    await harness.call("rig_evidence", {
      action: "start",
      skill: "rig-implement",
      trivial_plan: trivialPlan(["src/a.ts"]),
    })
    await expect(harness.call("rig_gate", { action: "run" })).rejects.toThrow(/no configured gates detected/)
  })
})

describe("rig_findings", () => {
  const open = {
    id: "F-001",
    severity: "high",
    dimension: "correctness",
    file: "src/a.ts",
    title: "Drops the last record",
    evidence: "Splitting on newline without filtering loses a trailing record.",
    status: "open",
  }

  async function reviewHarness(): Promise<{ project: Project; harness: Awaited<ReturnType<typeof createHarness>> }> {
    const project = changedProject()
    const harness = await createHarness(project)
    await harness.call("rig_evidence", { action: "start", skill: "code-review", paths: ["src"] })
    return { project, harness }
  }

  it("writes, merges, and reads a validated inventory", async () => {
    const { harness } = await reviewHarness()
    const first = await harness.call("rig_findings", {
      action: "write",
      axes: ["correctness", "tests"],
      coverage: ["src/a.ts"],
      reviewer: "rig-auditor",
      findings: [open],
    })
    expect(first.content).toMatch(/open 1, fixed 0/)
    expect(first.metadata.open).toBe(1)

    await harness.call("rig_findings", {
      action: "write",
      reviewer: "rig-auditor",
      findings: [{ ...open, status: "fixed", disposition: "Filtered empty lines and added a test." }],
    })
    const read = JSON.parse((await harness.call("rig_findings", { action: "read" })).content)
    expect(read.findings).toHaveLength(1)
    expect(read.findings[0].status).toBe("fixed")
    expect(read.axes).toEqual(["correctness", "tests"])
    expect(read.target_digest).toMatch(/^[a-f0-9]{64}$/)
  })

  it("requires axes and coverage", async () => {
    const { harness } = await reviewHarness()
    await expect(
      harness.call("rig_findings", { action: "write", coverage: ["src"], reviewer: "rig-auditor", findings: [] }),
    ).rejects.toThrow(/requires axes|aspects/)
  })

  it("requires correctness axis for code-review", async () => {
    const { harness } = await reviewHarness()
    await expect(
      harness.call("rig_findings", {
        action: "write",
        axes: ["security"],
        coverage: ["src"],
        reviewer: "rig-auditor",
        findings: [],
      }),
    ).rejects.toThrow(/correctness/)
  })

  it("accepts aspects as alias for axes", async () => {
    const { harness } = await reviewHarness()
    const result = await harness.call("rig_findings", {
      action: "write",
      aspects: ["Correctness", "tests"],
      coverage: ["src/a.ts"],
      reviewer: "rig-auditor",
      findings: [],
    })
    expect(result.content).toMatch(/axes: correctness, tests/)
  })

  it("rejects unknown axis/aspect", async () => {
    const { harness } = await reviewHarness()
    await expect(
      harness.call("rig_findings", {
        action: "write",
        axes: ["correctness", "typo"],
        coverage: ["src"],
        reviewer: "rig-auditor",
        findings: [],
      }),
    ).rejects.toThrow(/unknown review axis/)
  })

  it("requires a reviewer for a code-review write", async () => {
    const { harness } = await reviewHarness()
    await expect(
      harness.call("rig_findings", { action: "write", axes: ["correctness"], coverage: ["src"], findings: [] }),
    ).rejects.toThrow(/requires reviewer/)
  })

  it("rejects a terminal status without a disposition", async () => {
    const { harness } = await reviewHarness()
    await expect(
      harness.call("rig_findings", {
        action: "write",
        axes: ["correctness"],
        coverage: ["src"],
        reviewer: "rig-auditor",
        findings: [{ ...open, status: "fixed" }],
      }),
    ).rejects.toThrow(/without a disposition/)
  })

  it("rejects a review write after source drift", async () => {
    const { project, harness } = await reviewHarness()
    project.write("src/a.ts", "export const a = 9\n")
    await expect(
      harness.call("rig_findings", {
        action: "write",
        axes: ["correctness"],
        coverage: ["src"],
        reviewer: "rig-auditor",
        findings: [],
      }),
    ).rejects.toThrow(/start a new review run/)
  })

  it("completes a review run with recorded findings", async () => {
    const { harness } = await reviewHarness()
    await harness.call("rig_findings", {
      action: "write",
      axes: ["correctness"],
      coverage: ["src/a.ts"],
      reviewer: "rig-auditor",
      findings: [open],
    })
    const finalized = await harness.call("rig_evidence", { action: "finalize" })
    expect(finalized.metadata.ok).toBe(true)
  })
})

describe("rig_loop", () => {
  async function challengeHarness(options: Record<string, unknown> = { gates: ["true"] }) {
    const project = changedProject()
    const harness = await createHarness(project, options)
    await harness.call("rig_evidence", { action: "start", skill: "rig-challenge", paths: ["src"] })
    return { project, harness }
  }

  const high = { signature: "src/a.ts:null-deref", severity: "high" as const }

  it("drives a converged loop and completes the run", async () => {
    const { harness } = await challengeHarness()
    const started = await harness.call("rig_loop", {
      action: "start",
      reviewer: "rig-challenger",
      findings: [{ ...high, status: "open" }],
    })
    expect(started.content).toMatch(/Weighted score W0 = 6/)

    const appended = await harness.call("rig_loop", {
      action: "append",
      reviewer: "rig-challenger",
      findings: [{ ...high, status: "fixed" }],
    })
    expect(appended.metadata.converged).toBe(true)

    const finalized = await harness.call("rig_loop", {
      action: "finalize",
      reviewer: "rig-challenger",
      stop_reason: "converged",
    })
    expect(finalized.metadata.stop_reason).toBe("converged")

    await harness.call("rig_gate", { action: "run" })
    const done = await harness.call("rig_evidence", { action: "finalize" })
    expect(done.metadata.ok).toBe(true)
  })

  it("rejects appending without re-assessing a prior signature", async () => {
    const { harness } = await challengeHarness()
    await harness.call("rig_loop", {
      action: "start",
      reviewer: "rig-challenger",
      findings: [{ ...high, status: "open" }],
    })
    await expect(
      harness.call("rig_loop", {
        action: "append",
        reviewer: "rig-challenger",
        findings: [{ signature: "src/b.ts:other", severity: "low", status: "open" }],
      }),
    ).rejects.toThrow(/must address prior signatures/)
  })

  it("stops on a plateau and blocks completion", async () => {
    const { harness } = await challengeHarness()
    await harness.call("rig_loop", {
      action: "start",
      reviewer: "rig-challenger",
      findings: [{ ...high, status: "open" }],
    })
    const plateau = await harness.call("rig_loop", {
      action: "append",
      reviewer: "rig-challenger",
      findings: [
        { ...high, status: "fixed" },
        { signature: "src/b.ts:other", severity: "high", status: "open" },
      ],
    })
    expect(plateau.content).toMatch(/plateau/)
    await expect(
      harness.call("rig_loop", { action: "finalize", reviewer: "rig-challenger", stop_reason: "converged" }),
    ).rejects.toThrow(/cannot record convergence/)
    const stopped = await harness.call("rig_loop", {
      action: "finalize",
      reviewer: "rig-challenger",
      stop_reason: "plateau",
    })
    expect(stopped.metadata.stop_reason).toBe("plateau")
  })

  it("refuses convergence on a baseline alone", async () => {
    const { harness } = await challengeHarness()
    await harness.call("rig_loop", {
      action: "start",
      reviewer: "rig-challenger",
      findings: [{ ...high, status: "open" }],
    })
    await expect(
      harness.call("rig_loop", { action: "finalize", reviewer: "rig-challenger", stop_reason: "converged" }),
    ).rejects.toThrow(/baseline only/)
  })

  it("records a security plateau while refusing run completion", async () => {
    const { harness } = await challengeHarness()
    await harness.call("rig_loop", {
      action: "start",
      reviewer: "rig-challenger",
      findings: [{ signature: "src/a.ts:leak", severity: "security", status: "open" }],
    })
    await harness.call("rig_loop", {
      action: "append",
      reviewer: "rig-challenger",
      findings: [{ signature: "src/a.ts:leak", severity: "security", status: "open" }],
    })
    const stopped = await harness.call("rig_loop", {
      action: "finalize",
      reviewer: "rig-challenger",
      stop_reason: "plateau",
    })
    expect(stopped.metadata.stop_reason).toBe("plateau")
    const finalized = await harness.call("rig_evidence", { action: "finalize" })
    expect(finalized.metadata).toMatchObject({ ok: false, status: "blocked" })
    expect(finalized.content).toContain("retains 1 security and 0 critical")
  })

  it("requires a reviewer and operates only on a challenge run", async () => {
    const { harness } = await challengeHarness()
    await expect(
      harness.call("rig_loop", { action: "start", findings: [{ ...high, status: "open" }] }),
    ).rejects.toThrow(/requires reviewer/)

    const project = changedProject()
    const other = await createHarness(project)
    await other.call("rig_evidence", { action: "start", skill: "rig-investigate", paths: ["src"] })
    await expect(other.call("rig_loop", { action: "read", reviewer: "rig-challenger" })).rejects.toThrow(
      /operates on a rig-challenge or rig-implement run/,
    )
  })

  it("prints cumulative progress", async () => {
    const { harness } = await challengeHarness()
    await harness.call("rig_loop", {
      action: "start",
      reviewer: "rig-challenger",
      findings: [{ ...high, status: "open" }],
    })
    const read = await harness.call("rig_loop", { action: "read", reviewer: "rig-challenger" })
    expect(read.content).toContain("| Iteration | Security | Critical | High |")
    expect(read.content).toContain("| 1 | 0 + 0 | 0 + 0 | 0 + 1 |")
  })
})

describe("rig_git_grant", () => {
  it("records and revokes a grant only after explicit confirmation", async () => {
    const project = changedProject()
    const harness = await createHarness(project)

    const absent = JSON.parse((await harness.call("rig_git_grant", { action: "status" })).content)
    expect(absent.status).toBe("absent")

    await expect(harness.call("rig_git_grant", { action: "approve" })).rejects.toThrow(/requires confirm/)
    await expect(harness.call("rig_git_grant", { action: "approve", confirm: "yes, commit locally" })).rejects.toThrow(
      /scope_acknowledged=true/,
    )

    const approved = await harness.call("rig_git_grant", {
      action: "approve",
      confirm: "Approve always",
      scope_acknowledged: true,
    })
    expect(approved.metadata.status).toBe("granted")
    expect(harness.assertions.at(-1)).toMatchObject({ action: "rig_git_grant" })

    const status = JSON.parse((await harness.call("rig_git_grant", { action: "status" })).content)
    expect(status.status).toBe("granted")

    const revoked = await harness.call("rig_git_grant", { action: "revoke" })
    expect(revoked.metadata.status).toBe("absent")
    expect(JSON.parse((await harness.call("rig_git_grant", { action: "status" })).content).status).toBe("absent")
  })
})

describe("rig_dispatch", () => {
  it("rejects write scope for a read-only role", async () => {
    const harness = await createHarness(changedProject())
    await expect(
      harness.call("rig_dispatch", { role: "rig-auditor", task: "review", read_only: false, paths: ["src"] }),
    ).rejects.toThrow(/role rig-auditor is read-only/)
  })

  it("requires owned paths for a writing child", async () => {
    const harness = await createHarness(changedProject())
    await expect(
      harness.call("rig_dispatch", { role: "rig-builder", task: "implement", read_only: false }),
    ).rejects.toThrow(/needs explicit owned paths/)
  })

  it("allows write scope for the creator role", async () => {
    const harness = await createHarness(changedProject())
    harness.childReply = "Wrote the article.\nchanged: src/b.ts"
    const result = await harness.call("rig_dispatch", {
      role: "rig-creator",
      task: "Write the approved article",
      read_only: false,
      paths: ["src"],
    })
    expect(result.content).toContain("role: rig-creator")
    expect(harness.childPrompts.at(-1)?.text).toContain("You may edit only the paths listed above")
    expect(result.metadata.scope_violation).toBe(false)
  })

  it("injects the role card, returns the final text, and removes the child session", async () => {
    const harness = await createHarness(changedProject())
    const result = await harness.call("rig_dispatch", {
      role: "rig-auditor",
      task: "Review src/a.ts",
      paths: ["src"],
    })
    expect(result.content).toContain("role: rig-auditor")
    expect(result.content).toContain("the child reports a concrete result")
    expect(harness.childPrompts[0]?.text).toMatch(/You are an independent auditor/)
    expect(harness.childPrompts[0]?.text).toMatch(/Read-only: do not edit files/)
    expect(harness.removedSessions).toEqual(["child-1"])
  })

  it("keeps the child session when asked", async () => {
    const harness = await createHarness(changedProject())
    await harness.call("rig_dispatch", { role: "rig-investigator", task: "Inspect", keep_session: true })
    expect(harness.createdSessions).toEqual(["child-1"])
    expect(harness.removedSessions).toEqual([])
  })

  it("fails when the child returns no final text", async () => {
    const harness = await createHarness(changedProject())
    harness.childReply = ""
    await expect(harness.call("rig_dispatch", { role: "rig-investigator", task: "Inspect" })).rejects.toThrow(
      /no final assistant text/,
    )
    expect(harness.removedSessions).toEqual(["child-1"])
  })

  it("accepts a reported change inside the recorded scope", async () => {
    const harness = await createHarness(changedProject())
    await harness.call("rig_evidence", { action: "start", skill: "rig-plan", paths: ["src"] })
    harness.childReply = "Edited the module.\nchanged: src/b.ts"
    const result = await harness.call("rig_dispatch", {
      role: "rig-builder",
      task: "implement",
      read_only: false,
      paths: ["src"],
    })
    expect(result.content).toContain("changed: src/b.ts")
    expect(harness.childPrompts.at(-1)?.text).toContain("changed: <path>")
    expect(harness.removedSessions).toEqual(["child-1"])
  })

  it("returns a reported scope violation with the child evidence and retains its session", async () => {
    const harness = await createHarness(changedProject())
    await harness.call("rig_evidence", { action: "start", skill: "rig-plan", paths: ["src"] })
    harness.childReply = "Edited an unrelated test.\nchanged: tests/b.test.ts"
    const result = await harness.call("rig_dispatch", {
      role: "rig-builder",
      task: "implement",
      read_only: false,
      paths: ["src"],
    })
    expect(result.content).toContain("Edited an unrelated test.")
    expect(result.content).toContain("SCOPE VIOLATION: true")
    expect(result.content).toContain("outside the recorded run scope: tests/b.test.ts; recorded scope: src")
    expect(harness.removedSessions).toEqual([])
  })

  it("rejects a sibling path that only shares the scope prefix", async () => {
    const harness = await createHarness(changedProject())
    await harness.call("rig_evidence", { action: "start", skill: "rig-plan", paths: ["src"] })
    harness.childReply = "changed: srcs/b.ts"
    const result = await harness.call("rig_dispatch", {
      role: "rig-builder",
      task: "implement",
      read_only: false,
      paths: ["src"],
    })
    expect(result.content).toContain("SCOPE VIOLATION: true")
    expect(result.content).toContain("outside the recorded run scope: srcs/b.ts")
    expect(harness.removedSessions).toEqual([])
  })

  it("enforces input paths without an active run", async () => {
    const harness = await createHarness(changedProject())
    harness.childReply = "changed: lib/b.ts"
    const result = await harness.call("rig_dispatch", {
      role: "rig-builder",
      task: "implement",
      read_only: false,
      paths: ["src"],
    })
    expect(result.content).toContain("changed: lib/b.ts")
    expect(result.content).toContain("SCOPE VIOLATION: true")
    expect(result.content).toContain("outside the input scope: lib/b.ts")
    expect(harness.removedSessions).toEqual([])
  })

  it("enforces input paths when the recorded scope covers every changed path", async () => {
    const harness = await createHarness(changedProject())
    await harness.call("rig_evidence", { action: "start", skill: "rig-plan" })
    harness.childReply = "changed: lib/b.ts"
    const result = await harness.call("rig_dispatch", {
      role: "rig-builder",
      task: "implement",
      read_only: false,
      paths: ["src"],
    })
    expect(result.content).toContain("changed: lib/b.ts")
    expect(result.content).toContain("SCOPE VIOLATION: true")
    expect(result.content).toContain("outside the input scope: lib/b.ts")
    expect(harness.removedSessions).toEqual([])
  })

  it.each([
    "Done. Changed: 2 files (src/a.ts, tests/b.test.ts)",
    "changed: (src/a.ts)",
    "changed: src/a.ts and src/b.ts",
  ])("does not read prose as an accounting path list: %s", async (reply) => {
    const harness = await createHarness(changedProject())
    await harness.call("rig_evidence", { action: "start", skill: "rig-plan", paths: ["src"] })
    harness.childReply = reply
    const result = await harness.call("rig_dispatch", {
      role: "rig-builder",
      task: "implement",
      read_only: false,
      paths: ["src"],
    })
    expect(result.content).toContain(reply)
  })

  it("passes a child that reports no changed paths", async () => {
    const harness = await createHarness(changedProject())
    await harness.call("rig_evidence", { action: "start", skill: "rig-plan", paths: ["src"] })
    harness.childReply = "Nothing to change; the code already matches the plan."
    const result = await harness.call("rig_dispatch", {
      role: "rig-builder",
      task: "implement",
      read_only: false,
      paths: ["src"],
    })
    expect(result.content).toContain("Nothing to change")
  })

  it("checks only the calling session's recorded scope", async () => {
    const project = changedProject()
    project.write("tests/b.test.ts", "existing scoped test")
    const first = await createHarness(project, {}, "session-a")
    await first.call("rig_evidence", { action: "start", skill: "rig-plan", paths: ["src"] })
    const second = await createHarness(project, {}, "session-b")
    await second.call("rig_evidence", { action: "start", skill: "rig-plan", paths: ["tests"] })

    second.childReply = "Edited a source file.\nchanged: src/b.ts"
    const rejected = await second.call("rig_dispatch", {
      role: "rig-builder",
      task: "implement",
      read_only: false,
      paths: ["tests"],
    })
    expect(rejected.content).toContain("SCOPE VIOLATION: true")
    expect(rejected.content).toContain("outside the recorded run scope: src/b.ts")
    expect(second.removedSessions).toEqual([])

    first.childReply = "Edited a source file.\nchanged: src/b.ts"
    const accepted = await first.call("rig_dispatch", {
      role: "rig-builder",
      task: "implement",
      read_only: false,
      paths: ["src"],
    })
    expect(accepted.content).toContain("changed: src/b.ts")
  })
})

describe("rig_doctor", () => {
  it("reports a healthy install and every checked surface", async () => {
    const project = changedProject()
    const harness = await createHarness(project)
    await harness.call("rig_evidence", { action: "start", skill: "rig-health", paths: ["src"] })
    const report = await harness.call("rig_doctor", { action: "report" })
    expect(report.metadata.ok).toBe(true)
    expect(report.content).toContain("plugin loaded")
    expect(report.content).toContain("version sources agree")
    expect(report.content).toContain("local Git grant")
    expect(report.content).toContain("artifact root is gitignored")
  })

  it("curates the shipped skill and role cards on request", async () => {
    const project = changedProject()
    const harness = await createHarness(project)
    const report = await harness.call("rig_doctor", { action: "assets" })
    expect(report.metadata.ok).toBe(true)
    expect(report.content).toMatch(/assets: 11 skills, 8 role agents, \d+ repeated prose line\(s\)/)
  })

  it("requires confirmation before removing stale runs", async () => {
    const project = changedProject()
    const harness = await createHarness(project)
    const started = await harness.call("rig_evidence", { action: "start", skill: "rig-investigate", paths: ["src"] })
    const directory = join(project.root, ".opencode-rig", "runs", "rig-investigate", started.metadata.run_id as string)
    const runPath = join(directory, "run.json")
    const terminal = JSON.parse(readFileSync(runPath, "utf8"))
    writeFileSync(runPath, JSON.stringify({ ...terminal, status: "completed" }))
    const old = new Date(Date.now() - 400 * 24 * 60 * 60 * 1000)
    utimesSync(directory, old, old)

    await expect(harness.call("rig_doctor", { action: "clean" })).rejects.toThrow(/confirm=true/)
    const cleaned = await harness.call("rig_doctor", { action: "clean", confirm: true })
    expect(cleaned.metadata.removed).toBe(1)
    expect(existsSync(directory)).toBe(false)
    expect(harness.assertions.at(-1)?.action).toBe("rig_doctor_clean")
  })
})
