/** Shell functions retain input provenance across nested execution. */
import { describe, expect, it } from "vitest"
import { classifyCommand, decide } from "../../../src/guardrails.ts"

const GRANTED = { status: "granted", path: "approval.json" } as const

describe("inherited shell function input", () => {
  it.each([
    "a-b() { bash; }; printf '%s' 'git push' | a-b",
    "a.b() { bash; }; printf '%s' 'git push' | a.b",
    "a:b() { bash; }; printf '%s' 'git push' | a:b",
    "run() { bash; }; printf '%s' 'git push' | echo $(run)",
    "run() { bash; }; export -f run; printf '%s' 'git push' | bash -c 'run'",
  ])("asks before executing fed stdin through %s", (command) => {
    expect(classifyCommand(command).kind).toBe("unparsed-git")
  })

  // Grammar gate: a function definition is outside the grammar, so this asks.
  it("asks for a literal function with a script operand", () => {
    const result = classifyCommand("run() { bash script.sh; }; printf '%s' 'git push' | bash -c 'run'")
    expect(decide(result, "standard", GRANTED)?.effect).toBe("ask")
  })
})
