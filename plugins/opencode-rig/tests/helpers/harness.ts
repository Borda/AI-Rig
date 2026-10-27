/**
 * Shared test harness: isolated projects, a mock OpenCode context, and plugin
 * bootstrapping through the real `setup` entrypoint.
 *
 * Tests must exercise the same wiring OpenCode runs. The mock context records
 * every registration instead of replacing business logic, so a broken
 * transform surfaces here rather than only in a live session.
 */

import { execFileSync } from "node:child_process"
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { dirname, join, resolve } from "node:path"
import { fileURLToPath } from "node:url"
import { afterEach } from "vitest"

export const PACKAGE_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..")
export const RESOURCES_DIR = join(PACKAGE_ROOT, "src", "resources")

const disposers: Array<() => void | Promise<void>> = []

afterEach(async () => {
  for (const dispose of disposers.splice(0).reverse()) {
    try {
      await dispose()
    } catch {
      // Test cleanup is best-effort; a leaked temp directory is not a failure.
    }
  }
})

export function track(dispose: () => void | Promise<void>): void {
  disposers.push(dispose)
}

export class Project {
  readonly root: string

  constructor() {
    this.root = mkdtempSync(join(tmpdir(), "opencode-rig-test-"))
    track(() => rmSync(this.root, { recursive: true, force: true }))
  }

  path(relative: string): string {
    return join(this.root, relative)
  }

  write(relative: string, content: string): string {
    const absolute = this.path(relative)
    mkdirSync(dirname(absolute), { recursive: true })
    writeFileSync(absolute, content, "utf8")
    return absolute
  }

  read(relative: string): string {
    return readFileSync(this.path(relative), "utf8")
  }

  exists(relative: string): boolean {
    return existsSync(this.path(relative))
  }

  exec(file: string, args: readonly string[], env: NodeJS.ProcessEnv = {}): string {
    return execFileSync(file, [...args], {
      cwd: this.root,
      encoding: "utf8",
      env: { ...process.env, ...env },
      stdio: ["ignore", "pipe", "pipe"],
    }).trim()
  }

  git(...args: string[]): string {
    return this.exec("git", args)
  }

  initGit(): this {
    this.git("init", "-q")
    this.git("config", "user.email", "rig@example.com")
    this.git("config", "user.name", "Rig Test")
    this.git("config", "commit.gpgsign", "false")
    return this
  }

  commit(message: string): string {
    this.git("add", "-A")
    this.git("commit", "-qm", message)
    return this.git("rev-parse", "HEAD")
  }

  /** A repository with one clean commit that later tests can modify. */
  static withCommit(files: Record<string, string> = { "src/a.ts": "export const a = 1\n" }): Project {
    const project = new Project()
    for (const [relative, content] of Object.entries(files)) project.write(relative, content)
    project.initGit()
    project.commit("initial")
    return project
  }
}

export interface ToolCall {
  content: string
  metadata: Record<string, any>
}

export interface ChildPrompt {
  sessionID: string
  text: string
}

export interface Harness {
  ctx: any
  readonly tools: Map<string, any>
  readonly skills: any[]
  readonly commands: any[]
  readonly assertions: Array<{ action: string; resources: string[] }>
  readonly permissionHooks: Map<string, (event: any) => unknown>
  readonly toolHooks: Map<string, Array<(event: any) => unknown>>
  readonly sessionHooks: Map<string, Array<(event: any) => unknown>>
  readonly childPrompts: ChildPrompt[]
  readonly createdSessions: string[]
  readonly removedSessions: string[]
  childReply: string
  call(name: string, input: unknown): Promise<ToolCall>
  dispose(): Promise<void>
}

/**
 * Boot the plugin against a mock host. The returned harness invokes tools the
 * way the server does: one tool context per call, with a stable session ID.
 */
export async function createHarness(
  project: Project,
  options: Record<string, unknown> = {},
  sessionID = "session-1",
): Promise<Harness> {
  const tools = new Map<string, any>()
  const skills: any[] = []
  const commands: any[] = []
  const assertions: Array<{ action: string; resources: string[] }> = []
  const permissionHooks = new Map<string, (event: any) => unknown>()
  const toolHooks = new Map<string, Array<(event: any) => unknown>>()
  const sessionHooks = new Map<string, Array<(event: any) => unknown>>()
  const childPrompts: ChildPrompt[] = []
  const createdSessions: string[] = []
  const removedSessions: string[] = []

  let child = 0
  let cleanup: (() => Promise<void>) | undefined

  const harness: Harness = {
    ctx: undefined as any,
    tools,
    skills,
    commands,
    assertions,
    permissionHooks,
    toolHooks,
    sessionHooks,
    childPrompts,
    createdSessions,
    removedSessions,
    childReply: "the child reports a concrete result",
    async call(name, input) {
      const tool = tools.get(name)
      if (!tool) throw new Error(`tool not registered: ${name}`)
      return tool.execute(input, {
        sessionID,
        agent: "build",
        messageID: "message-1",
        id: "call-1",
        signal: new AbortController().signal,
        progress: async () => {},
      })
    },
    async dispose() {
      if (cleanup) await cleanup()
    },
  }

  const registration = { dispose: async () => {} }
  const hook = (map: Map<string, Array<(event: any) => unknown>>) => ({
    hook: async (name: string, callback: (event: any) => unknown) => {
      const list = map.get(name) ?? []
      list.push(callback)
      map.set(name, list)
      return registration
    },
  })

  const ctx = {
    app: { name: "opencode", version: "2.1.4", channel: "stable" },
    options,
    location: { directory: project.root },
    tool: {
      transform: async (callback: (editor: any) => void) => {
        callback({
          add: (tool: any) => void tools.set(tool.name, tool),
          namespace: () => {},
          list: () => [...tools.values()],
          get: (id: string) => tools.get(id),
          update: () => {},
          remove: (id: string) => void tools.delete(id),
        })
        return registration
      },
      ...hook(toolHooks),
      list: async () => [...tools.entries()].map(([id, tool]) => ({ ...tool, id })),
    },
    skill: {
      transform: async (callback: (editor: any) => void) => {
        callback({
          add: (skill: any) => void skills.push(skill),
          list: () => skills,
          get: (id: string) => skills.find((skill) => skill.id === id),
          update: () => {},
          remove: () => {},
        })
        return registration
      },
      list: async () => skills,
    },
    command: {
      transform: async (callback: (editor: any) => void) => {
        callback({
          add: (command: any) => void commands.push(command),
          list: () => commands,
          get: (id: string) => commands.find((command) => command.name === id),
          update: () => {},
          remove: () => {},
        })
        return registration
      },
    },
    permission: {
      hook: async (name: string, callback: (event: any) => unknown) => {
        permissionHooks.set(name, callback)
        return registration
      },
      assert: async (input: { action: string; resources: string[] }) => {
        assertions.push({ action: input.action, resources: [...input.resources] })
      },
    },
    session: {
      ...hook(sessionHooks),
      create: async () => {
        child += 1
        const id = `child-${child}`
        createdSessions.push(id)
        return { id }
      },
      prompt: async (input: ChildPrompt) => {
        childPrompts.push({ sessionID: input.sessionID, text: input.text })
      },
      wait: async () => {},
      context: async () => [{ info: { role: "assistant" }, parts: [{ type: "text", text: harness.childReply }] }],
      remove: async ({ sessionID }: { sessionID: string }) => {
        removedSessions.push(sessionID)
      },
    },
    plugin: {
      list: async () => ({ data: [{ id: "opencode-rig" }] }),
    },
  }

  harness.ctx = ctx
  const plugin = (await import("../../src/index.ts")).default as any
  cleanup = await plugin.setup(ctx)
  const teardown = cleanup
  track(async () => {
    await teardown?.()
  })
  return harness
}
