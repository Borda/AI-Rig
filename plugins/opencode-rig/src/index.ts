/**
 * Plugin entrypoint.
 *
 * Wiring only: validate host compatibility and options, resolve paths, then
 * register tools, hooks, skills, and commands. Registration errors surface at
 * load time instead of failing later inside a workflow.
 */

import { fileURLToPath } from "node:url"
import { Plugin } from "@opencode/plugin"
import { parseOptions } from "./options.ts"
import { resolvePaths } from "./paths.ts"
import { registerCommands } from "./register/commands.ts"
import { registerHooks, type HookRegistration } from "./register/hooks.ts"
import { registerSkills } from "./register/skills.ts"
import { registerTools, type Registration } from "./register/tools.ts"
import { MIN_OPENCODE_VERSION, PLUGIN_ID } from "./version.ts"

const RESOURCES_DIR = fileURLToPath(new URL("./resources", import.meta.url))

interface Version {
  readonly major: number
  readonly minor: number
  readonly patch: number
}

export function parseVersion(value: string): Version | undefined {
  const match = /^(\d+)\.(\d+)\.(\d+)/.exec(value.trim())
  if (!match?.[1] || !match[2] || !match[3]) return undefined
  return { major: Number(match[1]), minor: Number(match[2]), patch: Number(match[3]) }
}

export function satisfiesMinimum(actual: string, minimum: string): boolean {
  const left = parseVersion(actual)
  const right = parseVersion(minimum)
  if (!left || !right) return false
  if (left.major !== right.major) return left.major > right.major
  if (left.minor !== right.minor) return left.minor > right.minor
  return left.patch >= right.patch
}

export function assertCompatibleHost(version: string): void {
  if (!satisfiesMinimum(version, MIN_OPENCODE_VERSION)) {
    throw new Error(
      `${PLUGIN_ID} requires OpenCode ${MIN_OPENCODE_VERSION} or newer; this host reports ${version}. Upgrade OpenCode instead of disabling the compatibility check.`,
    )
  }
}

async function disposeAll(registrations: readonly Registration[]): Promise<void> {
  for (const registration of [...registrations].reverse()) {
    try {
      await registration.dispose()
    } catch {
      // Cleanup is best-effort; the host unloads the plugin regardless.
    }
  }
}

export default Plugin.define({
  id: PLUGIN_ID,
  async setup(ctx) {
    assertCompatibleHost(ctx.app.version)
    const options = parseOptions(ctx.options)
    const paths = resolvePaths(ctx.location.directory, options.artifacts)

    const tools = await registerTools({ ctx, paths, options, resourcesDir: RESOURCES_DIR })
    const hooks: HookRegistration[] = await registerHooks({ ctx, paths, options })
    const skills = await registerSkills(ctx, RESOURCES_DIR)
    const commands = await registerCommands(ctx)

    return () => disposeAll([...tools, ...hooks, skills, commands])
  },
})
