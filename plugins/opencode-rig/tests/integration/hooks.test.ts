import { readFileSync } from "node:fs"
import { join } from "node:path"
import { describe, expect, it } from "vitest"
import { writeGrant } from "../../src/git-grant.ts"
import { resolveGitCommonDir } from "../../src/paths.ts"
import { Project, createHarness } from "../helpers/harness.ts"

function shellEvent(resources: string[], effect: "allow" | "ask" = "allow"): any {
  return { action: "shell", resources, effect }
}

async function evaluate(harness: Awaited<ReturnType<typeof createHarness>>, event: any): Promise<any> {
  const hook = harness.permissionHooks.get("evaluate")
  if (!hook) throw new Error("no evaluate hook registered")
  await hook(event)
  return event
}

describe("permission evaluate hook", () => {
  it("escalates a remote mutation that the configured rules allowed", async () => {
    const harness = await createHarness(Project.withCommit())
    const event = await evaluate(harness, shellEvent(["git push origin main"]))
    expect(event.effect).toBe("ask")
    expect(event.message).toMatch(/remote mutations are never automatic/)
  })

  it("escalates a destructive command and a history rewrite", async () => {
    const harness = await createHarness(Project.withCommit())
    expect((await evaluate(harness, shellEvent(["git reset --hard HEAD~1"]))).effect).toBe("ask")
    expect((await evaluate(harness, shellEvent(["git commit --amend"]))).effect).toBe("ask")
  })

  it("escalates a plain commit without a grant", async () => {
    const harness = await createHarness(Project.withCommit())
    const event = await evaluate(harness, shellEvent(["git commit -m 'feat: x'"]))
    expect(event.effect).toBe("ask")
    expect(event.message).toMatch(/no local Git grant/)
  })

  it("leaves the configured effect alone once a grant exists", async () => {
    const project = Project.withCommit()
    const commonDir = resolveGitCommonDir(project.root)
    if (!commonDir) throw new Error("expected a Git repository")
    writeGrant(commonDir)
    const harness = await createHarness(project)

    const allowed = await evaluate(harness, shellEvent(["git commit -m 'feat: x'"], "allow"))
    expect(allowed.effect).toBe("allow")
    expect(allowed.message).toBeUndefined()

    const asked = await evaluate(harness, shellEvent(["git commit -m 'feat: x'"], "ask"))
    expect(asked.effect).toBe("ask")
  })

  it("never downgrades a configured ask", async () => {
    const harness = await createHarness(Project.withCommit())
    const event = await evaluate(harness, shellEvent(["git status"], "ask"))
    expect(event.effect).toBe("ask")
  })

  it("ignores non-shell actions and empty resource lists", async () => {
    const harness = await createHarness(Project.withCommit())
    const edit = await evaluate(harness, { action: "edit", resources: ["src/a.ts"], effect: "allow" })
    expect(edit.effect).toBe("allow")

    const empty = await evaluate(harness, { action: "shell", resources: [], effect: "allow" })
    expect(empty.effect).toBe("allow")
  })

  it("escalates when any resource in a batch is guarded", async () => {
    const harness = await createHarness(Project.withCommit())
    const event = await evaluate(harness, shellEvent(["npm test", "git push"]))
    expect(event.effect).toBe("ask")
  })

  it("does nothing when guardrails are off", async () => {
    const harness = await createHarness(Project.withCommit(), { guardrails: "off" })
    const event = await evaluate(harness, shellEvent(["git push"]))
    expect(event.effect).toBe("allow")
  })

  it("escalates integration operations in strict mode", async () => {
    const harness = await createHarness(Project.withCommit(), { guardrails: "strict" })
    const event = await evaluate(harness, shellEvent(["git merge feature"]))
    expect(event.effect).toBe("ask")
  })
})

describe("journal hooks", () => {
  it("append redacted before/after records", async () => {
    const project = Project.withCommit()
    const harness = await createHarness(project)
    const before = harness.toolHooks.get("execute.before")?.[0]
    const after = harness.toolHooks.get("execute.after")?.[0]
    if (!before || !after) throw new Error("journal hooks are not registered")

    await before({
      sessionID: "session-1",
      tool: "bash",
      id: "call-9",
      input: {
        command: "curl -H 'Authorization: Bearer abcdefghijklmnopqrstuvwxyz'",
        token: "sk-abcdefghijklmnopqrstuvwxyz",
      },
    })
    await after({ sessionID: "session-1", tool: "bash", id: "call-9", status: "error", error: { message: "failed" } })

    const lines = readFileSync(join(project.root, ".opencode-rig", "journal.jsonl"), "utf8")
      .trim()
      .split("\n")
      .map((line) => JSON.parse(line))
    expect(lines).toHaveLength(2)
    expect(lines[0]).toMatchObject({ event: "tool.before", tool: "bash", call_id: "call-9" })
    expect(lines[0].input.command).toContain("[redacted]")
    expect(lines[0].input.token).toBe("[redacted]")
    expect(lines[1]).toMatchObject({ event: "tool.after", status: "error" })
  })

  it("write into the active run directory when a run is active", async () => {
    const project = Project.withCommit()
    project.write("src/a.ts", "export const a = 2\n")
    const harness = await createHarness(project)
    const started = await harness.call("rig_evidence", { action: "start", skill: "rig-investigate", paths: ["src"] })
    const before = harness.toolHooks.get("execute.before")?.[0]
    if (!before) throw new Error("journal hook is not registered")
    await before({ sessionID: "session-1", tool: "read", id: "call-1", input: { path: "src/a.ts" } })

    const directory = join(project.root, ".opencode-rig", "runs", "rig-investigate", started.metadata.run_id as string)
    const lines = readFileSync(join(directory, "journal.jsonl"), "utf8").trim().split("\n")
    expect(lines).toHaveLength(1)
    expect(JSON.parse(lines[0]!).tool).toBe("read")
  })

  it("are not registered when journaling is disabled", async () => {
    const harness = await createHarness(Project.withCommit(), { journal: false, compactionPreserve: false })
    expect(harness.toolHooks.size).toBe(0)
    expect(harness.sessionHooks.size).toBe(0)
  })
})

describe("compaction context hook", () => {
  it("preserves the active run identity in a summary", async () => {
    const project = Project.withCommit()
    project.write("src/a.ts", "export const a = 2\n")
    const harness = await createHarness(project)
    const started = await harness.call("rig_evidence", { action: "start", skill: "rig-investigate", paths: ["src"] })
    const hook = harness.sessionHooks.get("context")?.[0]
    if (!hook) throw new Error("context hook is not registered")

    const event: any = { sessionID: "session-1", system: [] }
    await hook(event)
    expect(event.system).toHaveLength(1)
    expect(event.system[0].text).toContain(started.metadata.run_id)
    expect(event.system[0].text).toContain("rig-investigate")
    expect(event.system[0].text).toMatch(/Preserve the run ID, plan digest, open findings, and gate state/)
  })

  it("adds nothing without an active run", async () => {
    const harness = await createHarness(Project.withCommit())
    const hook = harness.sessionHooks.get("context")?.[0]
    if (!hook) throw new Error("context hook is not registered")
    const event: any = { sessionID: "unrelated", system: [] }
    await hook(event)
    expect(event.system).toEqual([])
  })
})
