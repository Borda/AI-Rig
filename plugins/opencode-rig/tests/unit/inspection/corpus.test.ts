/** Replays the generic inspection corpus with one pinned expected effect per call. */
import { readFileSync } from "node:fs"
import { join } from "node:path"
import { describe, expect, it } from "vitest"
import { inspectionSequence } from "../../../src/inspection.ts"

interface CorpusEntry {
  readonly command: string
  readonly effect: "inspection" | "verification" | "ask"
}

const corpus = JSON.parse(readFileSync(join(import.meta.dirname, "../../fixtures/inspection-corpus.json"), "utf8")) as {
  entries: CorpusEntry[]
}

describe("inspection corpus", () => {
  const allowed = (directory: string) => directory === "/repo" || directory.startsWith("/repo/")
  it.each(corpus.entries.map((entry) => [entry.effect, entry.command] as const))("%s: %s", (effect, command) => {
    const verdict = inspectionSequence(command, "/repo", "standard", allowed)
    if (effect === "ask") expect(verdict.ok, command).toBe(false)
    else expect(verdict, command).toMatchObject({ ok: true, kind: effect })
  })

  it("names every remaining ask with a reason", () => {
    for (const entry of corpus.entries) {
      const verdict = inspectionSequence(entry.command, "/repo", "standard", allowed)
      if (verdict.ok) continue
      expect(verdict.reason, entry.command).toBeTruthy()
    }
  })
})
