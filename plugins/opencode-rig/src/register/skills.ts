/**
 * Skill registration from package resources.
 *
 * Descriptions come from the shipped manifest; bodies are read from disk before
 * the transform runs, so a package update changes behavior without regenerating
 * code and the transform itself stays synchronous.
 */

import { readFileSync } from "node:fs"
import { join } from "node:path"
import { Skill } from "@opencode/plugin"
import { SKILLS } from "../skill-manifest.ts"

export interface Registration {
  dispose(): Promise<void>
}

function stripFrontmatter(markdown: string): string {
  return markdown.replace(/^---\r?\n[\s\S]*?\r?\n---\r?\n/, "")
}

export async function registerSkills(ctx: any, resourcesDir: string): Promise<Registration> {
  const loaded = SKILLS.map((skill) => {
    const directory = join(resourcesDir, "skills", skill.id)
    return {
      ...skill,
      directory,
      path: join(directory, "SKILL.md"),
      content: stripFrontmatter(readFileSync(join(directory, "SKILL.md"), "utf8")),
    }
  })

  return ctx.skill.transform((editor: any) => {
    for (const skill of loaded) {
      editor.add({
        id: Skill.ID.make(skill.id),
        name: Skill.Name.make(skill.name),
        description: skill.description,
        path: Skill.Info.fields.path.make(skill.path),
        content: skill.content,
      })
    }
  })
}

export { stripFrontmatter }
