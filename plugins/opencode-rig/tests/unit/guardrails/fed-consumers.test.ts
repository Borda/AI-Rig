/** Input consumers and literal option forms preserve conservative guard decisions. */
import { describe, expect, it } from "vitest"
import { classifyCommand, decide } from "../../../src/guardrails.ts"

const GRANTED = { status: "granted", path: "approval.json" } as const

describe("fed input consumers", () => {
  it.each([
    "fish",
    "mksh",
    "busybox sh",
    "arch sh",
    "caffeinate sh",
    "find . -exec sh \\;",
    "(trap sh EXIT)",
    "python3",
    "node",
    "custom-launcher",
  ])("asks before an unverified consumer can execute stdin: %s", (consumer) => {
    const result = classifyCommand(`${consumer} <<'EOF'\ngit push\nEOF\n`)
    expect(result.kind).toBe("unparsed-git")
    expect(decide(result, "standard", GRANTED)?.effect).toBe("ask")
  })

  it.each(["cat", "tee", "grep pattern", "sed -n p", "head", "tail", "wc", "sort", "jq ."])(
    "keeps data consumers readable: %s",
    (consumer) => {
      expect(classifyCommand(`${consumer} <<'EOF'\ngit push\nEOF\n`).kind).toBe("other")
    },
  )

  it.each(["tee sh", "cat bash", "grep eval", "sort source", "jq trap"])(
    "asks when a fed data consumer contains an interpreter name: %s",
    (consumer) => {
      expect(classifyCommand(`${consumer} <<'EOF'\ngit push\nEOF\n`).kind).toBe("unparsed-git")
    },
  )

  it("keeps a literal fed Git read classified", () => {
    expect(classifyCommand("git status <<'EOF'\ngit push\nEOF\n").kind).toBe("read-only-git")
  })

  it.each([
    String.raw`env -S 'g\it status'`,
    String.raw`env -iS 'g\it status'`,
    String.raw`env -S 'printf\_git status'`,
    String.raw`env -S 'printf\tgit status'`,
    String.raw`env -iS 'printf\_git status'`,
    String.raw`env -iS 'printf\tgit status'`,
    String.raw`env --split-string='g\it status'`,
    String.raw`env --split='g\it status'`,
    String.raw`env --s='g\it status'`,
    "env -S '${EXECUTABLE} status'",
  ])("asks for split strings that can reach guarded executables: %s", (command) => {
    expect(classifyCommand(command).kind).toBe("unparsed-git")
  })
})

describe("literal Git option boundaries", () => {
  it("asks for a revision spelling ambiguous with a Windows short path", () => {
    expect(decide(classifyCommand("git checkout HEAD~1"), "standard", GRANTED)?.effect).toBe("ask")
  })
  it.each(["feature/x"])("keeps checkout revisions unguarded: %s", (operand) => {
    expect(classifyCommand(`git checkout ${operand}`).kind).toBe("other")
  })

  it.each(["src/", "/absolute", "../Makefile", "src/../Makefile", "~/Makefile", "'some file'"])(
    "guards checkout path shapes: %s",
    (operand) => {
      expect(classifyCommand(`git checkout ${operand}`).kind).toBe("destructive-local")
    },
  )

  it.each(["git stash -S list drop", "git stash -U list drop", "git stash show -S list", "git stash show -U list"])(
    "asks for stash options that do not match its supported grammar: %s",
    (command) => {
      expect(classifyCommand(command).kind).toBe("unparsed-git")
    },
  )

  it.each(["git stash show -p", "git stash show --include-untracked", "git stash list --oneline"])(
    "keeps supported stash reads readable: %s",
    (command) => {
      expect(classifyCommand(command).kind).toBe("read-only-git")
    },
  )

  it.each([
    "git config get --all core.x",
    "git config get --show-names core.x",
    "git config get --regexp 'core.*'",
    "git config get --value=enabled core.x",
    "git config --local get --all core.x",
    "git config list --show-names",
  ])("keeps config subcommand reads unguarded: %s", (command) => {
    expect(classifyCommand(command).kind).toBe("other")
  })

  it.each(["git config set --value=old core.x new", "git config get --edit core.x", "git config core.x --all"])(
    "does not extend subcommand read options to writes or legacy forms: %s",
    (command) => {
      expect(classifyCommand(command).kind).toBe("unparsed-git")
    },
  )
})
