/** Bodies persisted by a heredoc or fed to a shell keep their executable relevance. */
import { describe, expect, it } from "vitest"
import { classifyCommand, decide } from "../../../src/guardrails.ts"

const GRANTED = { status: "granted", path: "approval.json" } as const

const compounds = [
  "{ BODY; }",
  "( BODY )",
  "if BODY; then :; fi",
  "if true; then BODY; fi",
  "for i in 1; do BODY; done",
  "while true; do BODY; break; done",
  "case a in a) BODY;; esac",
  "! BODY",
  "f() { BODY; }; f",
  "function f { BODY; }; f",
] as const

describe("persisted non-shell data", () => {
  it.each([
    "cat > notes.md <<'EOF2'\nIt's a note\nEOF2\n",
    "cat > out.json <<'EOF'\n{\"a\":1}\nEOF\n",
    "tee out.txt <<'EOF'\nhello (world)\nEOF\n",
    "cat > out.sh <<'EOF'\nfor f in *.txt; do\n  echo hello\ndone\nEOF\n",
  ])("admits persisted data without a guarded command: %s", (command) => {
    expect(classifyCommand(command).kind).toBe("other")
    expect(decide(classifyCommand(command), "standard", GRANTED)).toBeUndefined()
  })

  it.each(["git push", "gh issue create --title x --body y", "echo 'unterminated git push"])(
    "keeps persisted guarded or unparsable Git bodies asking: %s",
    (body) => {
      expect(decide(classifyCommand(`cat >out.sh <<'EOF'\n${body}\nEOF`), "standard", GRANTED)?.effect).toBe("ask")
    },
  )
})

describe("shell script operands still inspect fed bodies", () => {
  it.each([
    "bash script.sh <<'EOF'\ngit push\nEOF",
    "sh script.sh <<EOF\ngit push\nEOF",
    "zsh x <<'EOF'\ngit push\nEOF",
    "dash x <<'EOF'\ngit push\nEOF",
    "ksh x <<'EOF'\ngit push\nEOF",
    "bash x.sh <<<'git push'",
  ])("asks for executable Git input despite a script operand: %s", (command) => {
    expect(classifyCommand(command).kind).toBe("unparsed-git")
    expect(decide(classifyCommand(command), "standard", GRANTED)?.effect).toBe("ask")
  })

  it.each([
    "bash script.sh <<'EOF'\nSee the git docs.\nEOF",
    "bash script.sh <<EOF\nSee the git docs.\nEOF",
    "bash script.sh",
    "printf safe | bash -eu script.sh",
  ])("admits script operands without executable fed input: %s", (command) => {
    expect(classifyCommand(command).kind).toBe("other")
  })
})

describe("persisted bodies require executable relevance", () => {
  it.each([
    "cat > CHANGELOG.md <<'EOF'\n- Fixed handling of git worktrees\nEOF",
    "tee notes.md <<'EOF'\nSee the git docs.\nEOF",
    "cat > notes.md <<'EOF'\nThe gh CLI is required.\nEOF",
    "cat > notes.md <<'EOF'\n# git push\nEOF",
    "cat > notes.md <<'EOF'\necho git push\nEOF",
    "cat > notes.md <<EOF\nSee the git docs.\nEOF",
    "",
  ])("admits mentions, comments and the documented other-language limit: %s", (command) => {
    expect(classifyCommand(command).kind).toBe("other")
    expect(decide(classifyCommand(command), "standard", GRANTED)).toBeUndefined()
  })

  it.each([
    "git push",
    "gh pr merge",
    "bash -c 'echo safe'",
    "eval 'echo safe'",
    "echo 'unterminated git push",
    String.raw`$'\x67it' push`,
    "echo $(git status)",
  ])("asks for guarded commands, launchers, encodings and malformed Git data: %s", (body) => {
    expect(decide(classifyCommand(`cat >out.sh <<'EOF'\n${body}\nEOF`), "standard", GRANTED)?.effect).toBe("ask")
  })
})

describe.each([
  "cat > x.sh <<'EOF'\nBODY\nEOF\nbash x.sh",
  "cat > x.sh <<'EOF'\nBODY\nEOF",
  "bash x <<'EOF'\nBODY\nEOF",
])("compound executable body in %s", (consumer) => {
  it.each(
    compounds.flatMap((compound) =>
      ["git push", "gh pr merge 1", "bash -c 'echo safe'", "eval x", "timeout 5 git push"].map((payload) =>
        compound.replace("BODY", payload),
      ),
    ),
  )("asks for %s", (body) => {
    expect(decide(classifyCommand(consumer.replace("BODY", body)), "standard", GRANTED)?.effect).toBe("ask")
  })
})

describe("compound consumer controls", () => {
  it.each(["cat > x.sh <<'EOF'\nBODY\nEOF"])("admits a safe compound in %s", (consumer) => {
    expect(classifyCommand(consumer.replace("BODY", "{ echo safe; }")).kind).toBe("other")
  })

  // Grammar gate: a body run or fed by a shell in the same text must satisfy the grammar, and a
  // `{ }` group does not, so these ask.
  it.each(["cat > x.sh <<'EOF'\nBODY\nEOF\nbash x.sh", "bash x <<'EOF'\nBODY\nEOF"])(
    "asks for a compound a same-text shell runs in %s",
    (consumer) => {
      expect(decide(classifyCommand(consumer.replace("BODY", "{ echo safe; }")), "standard", GRANTED)?.effect).toBe(
        "ask",
      )
    },
  )

  it.each(["echo safe", "{ git push; }"])("retains the stdin launcher gate for %s", (body) => {
    expect(decide(classifyCommand(`bash <<'EOF'\n${body}\nEOF`), "standard", GRANTED)?.effect).toBe("ask")
  })
})

describe("body relevance negative controls", () => {
  it.each([
    ...compounds.map((compound) => compound.replace("BODY", "echo safe")),
    "echo 'unterminated",
    "echo $(printf safe)",
    "find . -exec echo safe \\;",
    "echo `npm test`",
    "parallel git push ::: a",
    ...["bash", "sh", "python", "javascript", "text"].map((tag) => `\`\`\`${tag}\nnpm install\n\`\`\``),
    '{"x": "the git docs"}',
    '{"cmd": "git status"}',
    '{ "cmd": "git push" }',
  ])("admits persisted data or a documented limit: %s", (body) => {
    expect(classifyCommand(`cat > x.sh <<'EOF'\n${body}\nEOF`).kind).toBe("other")
  })

  it.each([
    // caffeinate is a catalogued wrapper, so the Git command after it is visible.
    "caffeinate git push",
    "echo 'unterminated git push",
    "echo $(git push",
    "echo `git push",
    "echo $(bash",
    "echo `eval",
    "Use `git push` to publish.",
    "find . -exec git push \\;",
    "find . -execdir gh pr merge 1 \\;",
    "find . -exec echo safe \\; -exec git push \\;",
    "find . -exec sh -c 'git push' \\;",
    "find . -exec timeout 5 git push \\;",
    "$(printf git) push",
    "$EXE$(printf safe) push",
    "$(printf safe){g..g}it push",
    "echo $(bash -c 'echo safe')",
    "echo `eval x`",
    "$(printf bash) -c 'git push'",
    String.raw`{ $'\x67it' push; }`,
  ])("retains executable and malformed relevant input: %s", (body) => {
    expect(decide(classifyCommand(`cat > x.sh <<'EOF'\n${body}\nEOF`), "standard", GRANTED)?.effect).toBe("ask")
  })

  it.each(["bash", "bash x"])("asks for find execution fed to %s", (consumer) => {
    expect(
      decide(classifyCommand(`${consumer} <<'EOF'\nfind . -exec git push \\;\nEOF`), "standard", GRANTED)?.effect,
    ).toBe("ask")
  })
})

// Attached word groups are conservatively treated as zsh qualifiers, including saved language calls.
it.each([
  "cat > out.py <<'EOF'\ndef f(x):\n    return x\nEOF\n",
  "cat > script.py <<'EOF'\nimport subprocess\nsubprocess.run(['git', 'status'])\nEOF",
  "cat > x.sh <<'EOF'\nimport subprocess\nsubprocess.run(['git', 'push'])\nEOF",
])("asks for attached parenthesis groups in saved text: %s", (command) => {
  expect(decide(classifyCommand(command), "standard", GRANTED)?.effect).toBe("ask")
})

const savedScript = (body: string) => `cat > x.sh <<'EOF'\n${body}\nEOF`
const COPULA_CONTEXTS = [
  { name: "top level", wrap: (body: string) => body },
  { name: "saved", wrap: savedScript },
  { name: "saved then sh", wrap: (body: string) => `${savedScript(body)}\nsh x.sh` },
  { name: "saved then bash", wrap: (body: string) => `${savedScript(body)}\nbash x.sh` },
  { name: "script fed", wrap: (body: string) => `bash x.sh <<'EOF'\n${body}\nEOF` },
] as const

// A `git is ...` line reads as prose only when every word is plain; option or ref shaped words execute.
describe.each(COPULA_CONTEXTS)("copula prose with option or ref shaped words at $name", ({ wrap }) => {
  it.each([
    "git is --force origin main",
    "git is origin topic/main",
    "gh is repo:branch",
    "if true; then :; else git is origin topic/main; fi",
  ])("retains option or ref shaped words: %s", (body) => {
    expect(decide(classifyCommand(wrap(body)), "standard", GRANTED)?.effect).toBe("ask")
  })
})

it.each([
  "env GIT_FOO=1 true\ngit is fine",
  "GIT_FOO=1\ngit is fine",
  "export GIT_AUTHOR_NAME=x",
  "x=1 git is fine",
  "env git is fine",
  "command git is fine",
  "sudo git is fine",
  "git is +main",
  "gh is +branch",
])("keeps configured or wrapped copulas executable: %s", (body) => {
  expect(decide(classifyCommand(`cat > x.sh <<'EOF'\n${body}\nEOF\nsh x.sh`), "standard", GRANTED)?.effect).toBe("ask")
})
