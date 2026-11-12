/** Fed consumers and encoded launcher payloads retain conservative guard decisions. */
import { describe, expect, it } from "vitest"
import { classifyCommand, decide } from "../../../src/guardrails.ts"

const GRANTED = { status: "granted", path: "approval.json" } as const

describe("fed shell programs", () => {
  it.each([
    "cat(){ sh; }; export -f cat; bash -c 'cat; : \"{}\"'",
    "cat(){ sh; }; export -f cat; READY=1 bash -c 'cat'",
    "cat(){ sh; }; export -f cat; bash -o errexit -c 'cat'",
    "bash -c 'sh; : \"{}\"'",
    "READY=1 bash -c 'sh'",
    "bash -o errexit -c 'sh'",
    "bash -ce 'sh'",
    "bash -ec 'sh; : \"{}\"'",
  ])("asks for a fed program outside literal recursion: %s", (program) => {
    const result = classifyCommand(`${program} <<'EOF'\ngit push\nEOF\n`)
    expect(result.kind).toBe("unparsed-git")
    expect(decide(result, "standard", GRANTED)?.effect).toBe("ask")
  })
})

describe("fed filter arguments", () => {
  it.each([
    "sed e",
    "sed -e 'e'",
    "sed -f /dev/stdin",
    "sed --file=/dev/stdin",
    "sed 's/a/b/e'",
    "sed 's/a/b/ge'",
    "sed 's/a/b/w output'",
    "sed 'p;e'",
    "sed 's/a/b/;e'",
    String.raw`sed 's/a\/b/c/e'`,
    "sed --unknown p",
    'sed "$script"',
    "sort --compress-program=./helper -S 1b",
    "sort --compress-program ./helper -S1b",
    "sort --compress=./helper",
    "sort --comp ./helper",
    "sort --unknown",
    "cat --unknown",
    "tee --unknown",
    "grep --unknown pattern",
    "head --unknown",
    "tail --unknown",
    "wc --unknown",
    "jq --unknown .",
    "cut --unknown",
    "tr --unknown x y",
    "uniq --unknown",
    "nl --unknown",
    "column --unknown",
    "paste --unknown",
    "fold --unknown",
    "rev --unknown",
    "comm --unknown - -",
  ])("asks before an unmodelled consumer form reads input: %s", (consumer) => {
    const result = classifyCommand(`${consumer} <<'EOF'\ngit push\nEOF\n`)
    expect(result.kind).toBe("unparsed-git")
    expect(decide(result, "standard", GRANTED)?.effect).toBe("ask")
  })

  it("guards a stdin script file with an executable sed command", () => {
    const result = classifyCommand("sed -f /dev/stdin <<'EOF'\ne git push\nEOF\n")
    expect(result.kind).toBe("unparsed-git")
    expect(decide(result, "standard", GRANTED)?.effect).toBe("ask")
  })

  it.each([
    "sed -n 1p",
    "sed -n '1,3p'",
    "sed '1d'",
    "sed 's/a/b/g'",
    String.raw`sed 's/a\/b/c/p'`,
    "sort -u",
    "sort -nr",
    "sort -k1,1 -t :",
    "cut -c1-8",
    "tr -d x",
    "uniq",
    "nl -ba",
    "column -t",
    "paste -s -d ,",
    "fold -w 80",
    "rev",
  ])("keeps a Git read through a supported data filter readable: %s", (consumer) => {
    const result = classifyCommand(`git log --oneline | ${consumer}`)
    expect(result.kind).toBe("read-only-git")
    expect(decide(result, "standard", GRANTED)).toBeUndefined()
  })

  it.each(["awk '{print $1}'", "less"])("keeps executable consumers guarded: %s", (consumer) => {
    expect(classifyCommand(`git status | ${consumer}`).kind).toBe("unparsed-git")
  })
})

describe("encoded launcher payloads", () => {
  it.each([
    String.raw`caffeinate env -S 'printf\tgit status'`,
    String.raw`arch /usr/bin/env --split='printf\_git status'`,
    String.raw`taskset env -iS 'printf\tgit status'`,
    String.raw`chrt bash -c $'\x67\x69\x74 status'`,
    String.raw`caffeinate bash -c g$'\x69't' status'`,
  ])("asks for opaque words behind an uncatalogued launcher: %s", (command) => {
    const result = classifyCommand(command)
    expect(result.kind).toBe("unparsed-git")
    expect(decide(result, "standard", GRANTED)?.effect).toBe("ask")
  })

  it.each([String.raw`printf '%s' 'env -S printf\tgit status'`])("keeps inert output data unguarded: %s", (command) => {
    expect(classifyCommand(command).kind).toBe("other")
  })

  // Grammar gate: ANSI-C quoting is outside the modelled quotes, so this asks.
  it.each([String.raw`printf $'a\n'`])("asks for ANSI-C quoted output data: %s", (command) => {
    expect(decide(classifyCommand(command), "standard", GRANTED)?.effect).toBe("ask")
  })
})

describe("stash option position", () => {
  it.each(["git stash -p list", "git stash --patch show", "git stash -u list", "git stash -p show drop"])(
    "asks when options precede a stash verb: %s",
    (command) => {
      const result = classifyCommand(command)
      expect(result.kind).toBe("unparsed-git")
      expect(decide(result, "standard", GRANTED)?.effect).toBe("ask")
    },
  )
})

describe("encoded executable and split-string boundaries", () => {
  it.each([
    String.raw`$'\547\551\564' push`,
    String.raw`env -S $'g\151t\_push'`,
    String.raw`env -S $'g\150\_issue\_create\_--title\_x\_--body\_y'`,
    "env -S $'${EXE} push'",
    "env --split-string=$'${EXE} push'",
    "$'true'",
    "command $'true'",
    "launcher $'safe'",
    "env -S $'true'",
    "env --split-string=$'true'",
    String.raw`env -S 'echo\_hi'`,
    String.raw`env --split-string='echo\_hi'`,
    "env -S '${EXE} push'",
    "env --split-string='${EXE} push'",
    String.raw`env -S 'echo\thi'`,
    String.raw`env --split-string='echo\thi'`,
    String.raw`ls $'\547\551\564'`,
  ])("asks for executable encodings or a second decoding boundary: %s", (command) => {
    const result = classifyCommand(command)
    expect(result.kind).toBe("unparsed-git")
    expect(decide(result, "standard", GRANTED)?.effect).toBe("ask")
  })

  // Grammar gate: ANSI-C quoting and env split strings are outside the modelled grammar, so these ask.
  it.each([
    String.raw`echo $'a\tb'`,
    String.raw`printf $'%s\n' x`,
    "ls $'dir name'",
    "env -S 'FOO=1 true'",
    "env --split-string='printf safe'",
  ])("asks for encoded data and split strings outside the grammar: %s", (command) => {
    expect(decide(classifyCommand(command), "standard", GRANTED)?.effect).toBe("ask")
  })
})
