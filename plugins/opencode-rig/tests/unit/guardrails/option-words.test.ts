/** Words that could expand to an option never reach the option positions of option-reading programs. */
import { describe, expect, it } from "vitest"
import { classifyCommand, decide } from "../../../src/guardrails.ts"

const GRANTED = { status: "granted", path: "approval.json" } as const

/** Wrap a command as a saved script that the same command line then runs with sh. */
const savedThenSh = (body: string) => `cat > x.sh <<'SAVED_END'\n${body}\nSAVED_END\nsh x.sh`

describe("dynamic option spellings ask", () => {
  it.each([
    // find predicates spelled with an expansion, quotes, a glob or a brace group.
    "find . -maxdepth 0 -ex$9ec gi$9t push \\;",
    "find . -ex$9ec sh -c 'gi$9t push' \\;",
    "find . -maxdepth 0 -execdir$9 gi$9t push \\;",
    "find . -maxdepth 0 -ok$9 sh -c 'gi$9t push' \\;",
    "find . -maxdepth 0 \"-exec\"$9 sh -c 'gi$9t push' \\;",
    "find . -maxdepth 0 ?exec sh -c 'gi$9t push' \\;",
    "find . -maxdepth 0 -e{x.$9.x}ec gi$9t push \\;",
    "find . $X sh -c 'gi$9t push' \\;",
    'find "$dir" -name x',
    "find . -name x -exec$9 echo {} \\;",
    // A word that may expand to the -exec terminator lets the following words act as predicates.
    "find . -exec echo {} $x -exec sh -c 'gi$9t push' \\;",
    "find . -exec echo * -exec gi$9t push \\;",
    // Wrapper and launcher option slots.
    "xargs $X echo",
    'xargs "$opt" echo',
    "env $X git status",
    "sudo $X make",
    "nice $X make",
    "timeout $X make",
    "bash $X x.sh",
    'sh "$flag" x.sh',
    "git $X status",
    "git log $range",
    "gh $X list",
    "rsync $X a b",
    "tar $X f",
    "ssh $X host",
    'env NODE_ENV=production node "$file"',
    'sudo -u "$user" make',
  ])("asks for %s", (command) => {
    expect(decide(classifyCommand(command), "standard", GRANTED)?.effect).toBe("ask")
    expect(decide(classifyCommand(savedThenSh(command)), "standard", GRANTED)?.effect).toBe("ask")
  })

  it.each([
    'find . -name "$pattern" -print',
    'find . -type f -newer "$ref"',
    "find src -name '*.ts' -exec grep -l \"$pat\" {} +",
    'timeout 60 ls "$dir"',
    "find . -name '*.log' -exec wc -l {} \\;",
    "find . -maxdepth 1 -type d",
    "xargs -n 1 echo",
    "grep -rn TODO . --include=*.py",
    "rsync -av src/ dest/",
  ])("keeps %s quiet", (command) => {
    const result = classifyCommand(command)
    expect(["other", "read-only-git"]).toContain(result.kind)
    expect(decide(result, "standard", GRANTED)).toBeUndefined()
  })
})
