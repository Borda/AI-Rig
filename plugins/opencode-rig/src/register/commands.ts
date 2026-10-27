/**
 * Slash commands.
 *
 * Commands are thin entrypoints: they load the matching skill and pass the
 * user's text through unchanged, so there is one workflow contract, not two.
 */

import { SKILLS } from "../skill-manifest.ts"

export interface Registration {
  dispose(): Promise<void>
}

export async function registerCommands(ctx: any): Promise<Registration> {
  return ctx.command.transform((editor: any) => {
    for (const skill of SKILLS) {
      editor.add({
        name: skill.id,
        description: skill.description,
        execute: async ({ sessionID, prompt, delivery }: any) => {
          const task = typeof prompt.text === "string" ? prompt.text.trim() : ""
          await ctx.session.prompt({
            ...prompt,
            sessionID,
            text: `Load the ${skill.id} skill and follow it exactly.\n\n${task || "No additional arguments were supplied; use the current working tree."}`,
            delivery,
          })
        },
      })
    }
  })
}
