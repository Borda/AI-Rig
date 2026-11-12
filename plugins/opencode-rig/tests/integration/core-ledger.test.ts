/** Durable receipts, honest loop stops, and explicit run recovery. */
import { chmodSync, existsSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync } from "node:fs"
import { join } from "node:path"
import { afterEach, describe, expect, it, vi } from "vitest"
import { sha256 } from "../../src/digest.ts"
import { collectScope } from "../../src/evidence.ts"
import type { RunRecord } from "../../src/run.ts"
import { createHarness, Project } from "../helpers/harness.ts"

afterEach(() => vi.unstubAllEnvs())

async function started(skill = "rig-challenge", gates = ['node -e "process.exit(0)"']) {
  const project = Project.withCommit()
  project.write("src/a.ts", "export const a = 2\n")
  const harness = await createHarness(project, { gates })
  let sourceRunId: string | undefined
  if (skill === "rig-remediate") {
    const source = await harness.call("rig_evidence", { action: "start", skill: "rig-review", paths: ["src"] })
    sourceRunId = source.metadata.run_id
    await harness.call("rig_findings", {
      action: "write",
      run_id: sourceRunId,
      axes: ["correctness"],
      coverage: ["src"],
      reviewer: "reviewer",
      findings: [],
    })
    await harness.call("rig_evidence", { action: "finalize", run_id: sourceRunId })
  }
  const result = await harness.call("rig_evidence", {
    action: "start",
    skill,
    paths: ["src"],
    ...(skill === "rig-implement" ? { trivial_plan: "Change one source constant; verify with configured gates." } : {}),
    ...(sourceRunId ? { source_run_id: sourceRunId } : {}),
  })
  const call = harness.call.bind(harness)
  harness.call = (name, input) => call(name, { run_id: result.metadata.run_id, ...(input as object) })
  const directory = project.path(`.opencode-rig/runs/${skill}/${result.metadata.run_id}`)
  return { project, harness, directory, runPath: join(directory, "run.json") }
}

function runAt(path: string): RunRecord {
  return JSON.parse(readFileSync(path, "utf8"))
}

const security = { signature: "src/a.ts:secret", severity: "security", status: "open" }

describe("gate history", () => {
  it.each([true, false])("retains three immutable samples and judges the latest result: %s", async (lastPass) => {
    const command =
      "node -e \"const fs=require('fs');process.stdout.write(fs.readFileSync('metric.txt'));process.exit(fs.existsSync('ready')?0:1)\""
    const { project, harness, directory, runPath } = await started("rig-implement", [command])
    project.write("metric.txt", "10")
    await harness.call("rig_gate", { action: "run" })
    const gateDir = join(directory, "gates")
    const first = Object.fromEntries(
      readdirSync(gateDir)
        .filter((name) => name !== "summary.json" && name !== "receipts.jsonl")
        .map((name) => [name, readFileSync(join(gateDir, name), "utf8")]),
    )
    project.write("ready", "yes")
    project.write("metric.txt", "20")
    expect((await harness.call("rig_gate", { action: "run" })).metadata.failed).toEqual([])
    if (!lastPass) rmSync(project.path("ready"))
    project.write("metric.txt", "30")
    await harness.call("rig_gate", { action: "run" })

    const run = runAt(runPath)
    expect(run.gates.map((receipt) => receipt.tail)).toEqual(["10", "20", "30"])
    expect(run.gates.map((receipt) => receipt.status)).toEqual(["failed", "passed", lastPass ? "passed" : "failed"])
    expect(readdirSync(gateDir).filter((name) => name.endsWith(".log"))).toHaveLength(3)
    expect(readdirSync(gateDir).filter((name) => name.endsWith(".json") && name !== "summary.json")).toHaveLength(3)
    for (const [name, bytes] of Object.entries(first)) expect(readFileSync(join(gateDir, name), "utf8")).toBe(bytes)
    const ledger = readFileSync(join(gateDir, "receipts.jsonl"), "utf8")
      .trim()
      .split("\n")
      .map((line) => JSON.parse(line))
    expect(ledger).toEqual(run.gates)
    expect((await harness.call("rig_evidence", { action: "finalize" })).metadata.ok).toBe(lastPass)
  })

  it("shows the file arguments to the approver before running a file-based gate", async () => {
    const { project, harness } = await started("rig-implement", [])
    project.write(".pre-commit-config.yaml", "repos: []\n")
    harness.ctx.permission.assert = async (input: unknown) => {
      expect(input).toMatchObject({
        action: "shell",
        resources: [`pre-commit run --files src/a.ts 'odd name.ts' ./-flag.ts 'quote'"'"'s.ts'`],
      })
      throw new Error("approval declined")
    }
    await expect(
      harness.call("rig_gate", { action: "run", files: ["src/a.ts", "odd name.ts", "-flag.ts", "quote's.ts"] }),
    ).rejects.toThrow("approval declined")
  })

  it("refuses to certify a different scope refreshed during gate approval", async () => {
    const { project, harness, directory, runPath } = await started("rig-implement")
    harness.ctx.permission.assert = async () => {
      project.write("src/new.ts", "new source\n")
      await harness.call("rig_evidence", { action: "refresh", message: "Scope expanded during approval" })
    }
    await expect(harness.call("rig_gate", { action: "run" })).rejects.toThrow(/scope changed/)
    expect(runAt(runPath).gates).toEqual([])
    expect(existsSync(join(directory, "gates"))).toBe(false)
  })

  it("refuses live source drift before requesting gate approval", async () => {
    const { project, harness, runPath } = await started("rig-implement")
    project.write("src/a.ts", "changed before gates\n")
    await expect(harness.call("rig_gate", { action: "run" })).rejects.toThrow(/refresh/)
    expect(harness.assertions).toEqual([])
    expect(runAt(runPath).gates).toEqual([])
  })

  it("refuses an approval-time edit before executing the gate", async () => {
    const { project, harness, runPath } = await started("rig-implement", [
      `node -e "require('fs').writeFileSync('executed', 'yes')"`,
    ])
    harness.ctx.permission.assert = async () => {
      project.write("src/a.ts", "changed during approval\n")
    }
    await expect(harness.call("rig_gate", { action: "run" })).rejects.toThrow(/refresh/)
    expect(project.exists("executed")).toBe(false)
    expect(runAt(runPath).gates).toEqual([])
  })

  it("leaves a source-rewriting gate receipt unbound to any source digest", async () => {
    const { project, harness, runPath } = await started("rig-implement", [
      `node -e "require('fs').writeFileSync('src/a.ts', 'changed by gate')"`,
    ])
    const saved = runAt(runPath).scope.digest
    const result = await harness.call("rig_gate", { action: "run" })
    const run = runAt(runPath)
    expect(run.scope.digest).toBe(saved)
    expect(run.gates[0]!.source_digest).toBeUndefined()
    expect(collectScope(project.root, ["src"]).digest).not.toBe(saved)
    expect(result.content).toMatch(/source changed.*refresh/is)
    expect(result.content).not.toContain("All recorded gates passed")
    expect((await harness.call("rig_evidence", { action: "finalize" })).metadata.ok).toBe(false)
  })

  it("restores owner-only permissions on an existing receipt journal", async () => {
    const { harness, directory } = await started("rig-implement")
    await harness.call("rig_gate", { action: "run" })
    const ledger = join(directory, "gates", "receipts.jsonl")
    chmodSync(ledger, 0o666)
    await harness.call("rig_gate", { action: "run" })
    expect(statSync(ledger).mode & (process.platform === "win32" ? 0o200 : 0o777)).toBe(
      process.platform === "win32" ? 0o200 : 0o600,
    )
  })

  it("marks replaced failures as superseded in gate output and the completed report", async () => {
    const { project, harness } = await started("rig-implement", [
      `node -e "process.exit(require('fs').existsSync('ready')?0:1)"`,
    ])
    await harness.call("rig_gate", { action: "run" })
    project.write("ready", "yes")
    const result = await harness.call("rig_gate", { action: "run" })
    expect(result.content).toMatch(/failed.*superseded/)
    expect(result.content).toMatch(/passed.*current/)
    const final = await harness.call("rig_evidence", { action: "finalize" })
    expect(final.metadata.ok).toBe(true)
    const report = readFileSync(final.metadata.report_path as string, "utf8")
    expect(report).toMatch(/failed.*superseded/)
    expect(report).toMatch(/passed.*current/)
  })
})

describe("run recovery", () => {
  it("requires findings to be rewritten after refreshing changed review source", async () => {
    const { project, harness, runPath } = await started("rig-review")
    const inventory = { action: "write", findings: [], axes: ["correctness"], coverage: ["src"], reviewer: "reviewer" }
    await harness.call("rig_findings", inventory)
    project.write("src/a.ts", "export const a = 3\n")
    await harness.call("rig_evidence", { action: "refresh", message: "Source changed" })
    await harness.call("rig_evidence", { action: "record", reviewer: "new-reviewer" })
    expect(runAt(runPath).findings).toBeUndefined()
    const missing = await harness.call("rig_evidence", { action: "finalize" })
    expect(missing.metadata.ok).toBe(false)
    expect(missing.content).toContain("findings artifact is missing")
    await harness.call("rig_findings", inventory)
    expect((await harness.call("rig_evidence", { action: "finalize" })).metadata.ok).toBe(true)
  })

  it("permits loop start after premature finalization of a commit-scoped challenge", async () => {
    const { harness } = await started()
    const result = await harness.call("rig_evidence", { action: "start", skill: "rig-challenge", commit: "HEAD" })
    const run_id = result.metadata.run_id
    expect((await harness.call("rig_evidence", { action: "finalize", run_id })).metadata.status).toBe("in_progress")
    await harness.call("rig_loop", { action: "start", run_id, reviewer: "reviewer", findings: [] })
    expect((await harness.call("rig_loop", { action: "read", run_id })).metadata.rounds).toBe(1)
  })

  it("leaves unstamped legacy receipts stale across refresh and requires rerunning gates", async () => {
    const { harness, runPath } = await started("rig-implement")
    await harness.call("rig_gate", { action: "run" })
    const legacy = runAt(runPath)
    legacy.gates = legacy.gates.map(({ source_digest, ...receipt }) => receipt)
    writeFileSync(runPath, JSON.stringify(legacy))
    await harness.call("rig_evidence", { action: "refresh", message: "Refresh legacy run" })
    expect(runAt(runPath).gates[0]!.source_digest).toBeUndefined()
    const missing = await harness.call("rig_evidence", { action: "finalize" })
    expect(missing.metadata.ok).toBe(false)
    expect(missing.content).toContain("gate receipts do not match the current scope")
    await harness.call("rig_gate", { action: "run" })
    expect((await harness.call("rig_evidence", { action: "finalize" })).metadata.ok).toBe(true)
  })
  it("refreshes a stale run without reusing old gates or review", async () => {
    const { project, harness, directory, runPath } = await started("rig-optimize")
    await harness.call("rig_gate", { action: "run" })
    await harness.call("rig_evidence", { action: "record", reviewer: "before" })
    const before = runAt(runPath)
    project.write("src/a.ts", "export const a = 3\n")
    expect((await harness.call("rig_evidence", { action: "finalize" })).metadata.status).toBe("stale")
    await harness.call("rig_evidence", { action: "refresh", message: "Source corrected" })
    const refreshed = runAt(runPath)
    expect(refreshed.status).toBe("in_progress")
    expect(refreshed.gates).toHaveLength(1)
    expect(refreshed.review).toBeUndefined()
    const missing = await harness.call("rig_evidence", { action: "finalize" })
    expect(missing.content).toContain("no passing gate receipt")
    expect(missing.content).toContain("independent review evidence is missing")
    expect(readFileSync(join(directory, "journal.jsonl"), "utf8")).toContain('"previous_status":"stale"')
    expect(readFileSync(join(directory, "journal.jsonl"), "utf8")).toContain(before.scope.digest)
    await harness.call("rig_gate", { action: "run" })
    await harness.call("rig_evidence", { action: "record", reviewer: "after" })
    expect((await harness.call("rig_evidence", { action: "finalize" })).metadata.ok).toBe(true)
  })

  it("keeps a missing loop ledger in progress and permits its first assessed round", async () => {
    const { harness, runPath } = await started()
    expect((await harness.call("rig_evidence", { action: "finalize" })).metadata.status).toBe("in_progress")
    await harness.call("rig_evidence", { action: "refresh", message: "Reviewer available" })
    expect(runAt(runPath).status).toBe("in_progress")
    await harness.call("rig_loop", { action: "start", reviewer: "reviewer", findings: [] })
    await harness.call("rig_loop", { action: "finalize", reviewer: "reviewer", stop_reason: "converged" })
    await harness.call("rig_gate", { action: "run" })
    expect((await harness.call("rig_evidence", { action: "finalize" })).metadata.ok).toBe(true)
  })

  it("refuses refresh of a stopped loop and preserves its recorded evidence", async () => {
    const { harness, directory, runPath } = await started()
    await harness.call("rig_loop", { action: "start", reviewer: "reviewer", findings: [security] })
    await harness.call("rig_loop", { action: "append", reviewer: "reviewer", findings: [security] })
    await harness.call("rig_loop", { action: "finalize", reviewer: "reviewer", stop_reason: "plateau" })
    const stopped = readFileSync(join(directory, "loop.jsonl"), "utf8")
    await expect(harness.call("rig_loop", { action: "append", reviewer: "next", findings: [] })).rejects.toThrow(
      /already finalized/,
    )
    await harness.call("rig_evidence", { action: "finalize" })
    const before = readFileSync(runPath, "utf8")
    await expect(harness.call("rig_evidence", { action: "refresh", message: "Try recovery" })).rejects.toThrow(
      /replacement run/,
    )
    expect(readFileSync(runPath, "utf8")).toBe(before)
    expect(readFileSync(join(directory, "loop.jsonl"), "utf8")).toBe(stopped)
  })
})

describe("honest loop stops", () => {
  it.each(["security", "critical"])(
    "records a plateau with open %s residue and blocks completion",
    async (severity) => {
      const { harness, directory, runPath } = await started()
      const findings = [{ ...security, severity }]
      await harness.call("rig_loop", { action: "start", reviewer: "reviewer", findings })
      await harness.call("rig_loop", { action: "append", reviewer: "reviewer", findings })
      const stop = await harness.call("rig_loop", { action: "finalize", reviewer: "reviewer", stop_reason: "plateau" })
      expect(stop.metadata.stop_reason).toBe("plateau")
      expect(runAt(runPath).loop?.stop_reason).toBe("plateau")
      expect(readFileSync(join(directory, "loop.jsonl"), "utf8")).toContain('"stop_reason":"plateau"')
      const final = await harness.call("rig_evidence", { action: "finalize" })
      expect(final.metadata).toMatchObject({ ok: false, status: "blocked" })
      expect(final.content).toContain(`1 ${severity}`)
      expect(final.content).not.toContain("ledger is missing")
    },
  )

  it.each([false, true])("records a drift stop even when the recorded digest also changed: %s", async (refresh) => {
    const { project, harness, runPath } = await started()
    await harness.call("rig_loop", { action: "start", reviewer: "reviewer", findings: [security] })
    const assessed = runAt(runPath).scope.digest
    project.write("src/a.ts", "export const a = 3\n")
    if (refresh) await harness.call("rig_evidence", { action: "refresh", message: "Recollected" })
    await harness.call("rig_loop", {
      action: "finalize",
      reviewer: "reviewer",
      stop_reason: "drift",
      note: "Source changed",
    })
    expect(runAt(runPath).loop?.stop_reason).toBe("drift")
    expect(runAt(runPath).review?.source_digest).toBe(assessed)
    const final = await harness.call("rig_evidence", { action: "finalize" })
    expect(final.metadata).toMatchObject({ ok: false, status: "blocked" })
    expect(final.content).not.toContain("ledger is missing")
    await expect(harness.call("rig_evidence", { action: "refresh", message: "Try recovery" })).rejects.toThrow(
      /replacement run/,
    )
  })

  it("records unavailable-reviewer without inventing independent review evidence", async () => {
    const { harness, runPath, directory } = await started()
    await harness.call("rig_loop", { action: "start", reviewer: "prior-reviewer", findings: [security] })
    await harness.call("rig_loop", {
      action: "finalize",
      stop_reason: "unavailable-reviewer",
      note: "Reviewer offline",
    })
    expect(runAt(runPath).loop?.stop_reason).toBe("unavailable-reviewer")
    expect(runAt(runPath).review).toBeUndefined()
    const last = JSON.parse(readFileSync(join(directory, "loop.jsonl"), "utf8").trim().split("\n").at(-1)!)
    expect(last.reviewer).toBe("prior-reviewer")
    expect(last.findings).toEqual([security])
    expect((await harness.call("rig_evidence", { action: "finalize" })).metadata.status).toBe("blocked")
  })
})

describe("run write boundaries", () => {
  const writes = [
    ["rig_evidence", { action: "record", decision: "changed" }],
    ["rig_gate", { action: "run" }],
    ["rig_findings", { action: "write", findings: [], axes: ["correctness"], coverage: ["src"] }],
    ["rig_loop", { action: "start", reviewer: "reviewer", findings: [] }],
    ["rig_loop", { action: "append", reviewer: "reviewer", findings: [security] }],
    ["rig_loop", { action: "finalize", reviewer: "reviewer", stop_reason: "converged" }],
  ] as const
  it.each(["completed", "stale", "blocked", "failed"])("rejects all writes into a %s run", async (status) => {
    const { harness, runPath, directory } = await started()
    await harness.call("rig_loop", { action: "start", reviewer: "reviewer", findings: [security] })
    writeFileSync(runPath, JSON.stringify({ ...runAt(runPath), status }))
    const before = readFileSync(runPath, "utf8")
    const loop = readFileSync(join(directory, "loop.jsonl"), "utf8")
    for (const [tool, input] of writes) await expect(harness.call(tool, input)).rejects.toThrow(/in_progress/)
    expect((await harness.call("rig_evidence", { action: "finalize" })).metadata).toMatchObject({ ok: false, status })
    expect(readFileSync(runPath, "utf8")).toBe(before)
    expect(readFileSync(join(directory, "loop.jsonl"), "utf8")).toBe(loop)
    expect(existsSync(join(directory, "gates"))).toBe(false)
    expect(existsSync(join(directory, "findings.json"))).toBe(false)
    expect(harness.assertions).toHaveLength(0)
    expect((await harness.call("rig_evidence", { action: "inspect" })).content).toContain(status)
    expect((await harness.call("rig_loop", { action: "read" })).metadata.rounds).toBe(1)
  })

  it.each(["completed", "failed"])("rejects refresh of a %s run", async (status) => {
    const { harness, runPath } = await started()
    writeFileSync(runPath, JSON.stringify({ ...runAt(runPath), status }))
    const before = readFileSync(runPath, "utf8")
    await expect(harness.call("rig_evidence", { action: "refresh", message: "Try reopening" })).rejects.toThrow(
      /status/,
    )
    expect(readFileSync(runPath, "utf8")).toBe(before)
  })

  it("records a tracked deletion as an output while refusing a missing input or invented output", async () => {
    const { project, harness, runPath } = await started("rig-implement")
    rmSync(project.path("src/a.ts"))
    await harness.call("rig_evidence", { action: "record", outputs: ["src/a.ts"] })
    expect(runAt(runPath).outputs).toEqual([{ path: "src/a.ts", sha256: sha256("") }])
    await expect(harness.call("rig_evidence", { action: "record", inputs: ["src/a.ts"] })).rejects.toThrow(
      /does not exist/,
    )
    await expect(harness.call("rig_evidence", { action: "record", outputs: ["typo.ts"] })).rejects.toThrow(
      /does not exist/,
    )
    rmSync(project.path("src"), { recursive: true })
    await expect(harness.call("rig_evidence", { action: "record", outputs: ["src"] })).rejects.toThrow(/does not exist/)
  })

  it("permits remediation findings without review-only axes or coverage", async () => {
    const { harness, runPath } = await started("rig-remediate")
    await harness.call("rig_findings", { action: "write", source_run_id: runAt(runPath).source_run_id, findings: [] })
    const artifact = JSON.parse((await harness.call("rig_findings", { action: "read" })).content)
    expect(artifact).toMatchObject({ axes: [], coverage: [], findings: [] })
  })

  it("reports absent when revoking an already absent grant", async () => {
    const { harness } = await started("rig-doctor")
    const result = await harness.call("rig_git_grant", { action: "revoke" })
    expect(result.metadata.status).toBe("absent")
    expect(result.content).toContain("No grant to revoke")
  })
})
