/** Benign filter and encoded argument forms avoid asks without hiding executable payloads. */
import { describe, expect, it } from "vitest"
import { classifyCommand, decide } from "../../../src/guardrails.ts"

const GRANTED = { status: "granted", path: "approval.json" } as const

describe("bounded data-filter grammar", () => {
  it.each([
    "head -20",
    "tail -20",
    "head -n 20",
    "tail -n 20",
    "sed -e 's/a/b/g'",
    "sed --expression='s/a/b/g'",
    "sed -e p -e 's/a/b/g'",

    "sed -n '5,10p'",
    "sed 's|a|b|g'",
    "sed 's#a#b#'",
    "sed 's,a,b,'",

    String.raw`sed 's|a\|b|c|g'`,
    "sed 's/e/w/g'",
  ])("keeps a Git read through plain data unguarded: %s", (consumer) => {
    const result = classifyCommand(`git log --oneline | ${consumer}`)
    expect(result.kind).toBe("read-only-git")
    expect(decide(result, "standard", GRANTED)).toBeUndefined()
  })

  it.each([
    "sed 'e git push'",
    "sed 's/a/b/e'",
    "sed 'w file'",
    "sed 'r file'",
    "sed 'R file'",
    "sed 'W file'",
    "sed 's/a/b/w file'",
    "sed -i p",
    "sed -f file",
    "sed --execute='git push'",
    "sed -e 'p;e git push'",
    "sed --expression='s/a/b/e'",
    "sed -e p -e 'w file'",
    "sed -ne 'e git push' -e p",
    "sed -ep -e'w file'",
    "sed --expression 'e git push' -e p",
    "sed -e 's|a|b|e'",
    "sed 's#a#b#w file'",
    "sed 's,a,b,;e git push'",
    "sed '/foo/,/bar/e git push'",
    "sed '/^#/w file'",
    "sed '5,10r file'",
    "sort --compress-program=helper",
  ])("keeps execution, writes and unmodelled scripts guarded: %s", (consumer) => {
    const result = classifyCommand(`git log --oneline | ${consumer}`)
    expect(result.kind).toBe("unparsed-git")
    expect(decide(result, "standard", GRANTED)?.effect).toBe("ask")
  })
})

describe("encoded payload relevance", () => {
  it.each([
    String.raw`echo $'a\tb'`,
    String.raw`printf $'%s\n' x`,
    "ls $'dir name'",
    "env -S 'FOO=1 true'",
    "env --split-string='printf safe'",
    String.raw`ls $'\x64ir name'`,
  ])("asks for encoded data and split strings outside the grammar gate: %s", (command) => {
    // ANSI-C quoting and env split strings leave the modelled grammar.
    expect(decide(classifyCommand(command), "standard", GRANTED)?.effect).toBe("ask")
  })

  it.each([
    String.raw`$'\x67it' push`,
    String.raw`env -S $'FOO=1\ttrue'`,
    "env -S 'git push'",
    "$'git' push origin main",
    String.raw`$'\147it' push`,
    String.raw`$'\u0067it' push`,
    String.raw`g$'\x69't push`,
    String.raw`$'\x62ash' -c 'git push'`,
    String.raw`$'\x73h' script`,
    String.raw`$'\U00000073h' script`,
    "$'g'${PART}t push",
    String.raw`$'g'$PART push`,
    String.raw`launcher $'\x73h' script`,
    String.raw`launcher $'\x65val' payload`,
    String.raw`launcher $'\x67h' pr merge`,
    "env -S 'sh script'",
    "launcher env -S 'bash script'",
    String.raw`launcher env -S $'\x67it push'`,
  ])("keeps decoded Git, shell and opaque launchers guarded: %s", (command) => {
    const result = classifyCommand(command)
    expect(result.kind).toBe("unparsed-git")
    expect(decide(result, "standard", GRANTED)?.effect).toBe("ask")
  })
})

describe("bounded sed data forms", () => {
  it.each(["sed 's:x:y:g'", "sed -s p", "sed 'p;d'", "sed 's:x:y:g;p'"])("admits a Git read through %s", (consumer) => {
    const result = classifyCommand(`git log --oneline | ${consumer}`)
    expect(result.kind).toBe("read-only-git")
    expect(decide(result, "standard", GRANTED)).toBeUndefined()
  })

  it.each(["sed -z p", "sed -sz 's:x:y:g'"])("asks for unlisted sed modes through a pipe: %s", (consumer) => {
    expect(decide(classifyCommand(`git log --oneline | ${consumer}`), "standard", GRANTED)?.effect).toBe("ask")
  })

  it.each([
    "sed 'e git push'",
    "sed 'w file'",
    "sed 'r file'",
    "sed 'R file'",
    "sed 'W file'",
    "sed 's:x:y:e'",
    "sed 's:x:y:w file'",
    "sed -i p",
    "sed -f file",
  ])("keeps execution and writes guarded: %s", (consumer) => {
    expect(decide(classifyCommand(`git log --oneline | ${consumer}`), "standard", GRANTED)?.effect).toBe("ask")
  })
})
