/** Shell expansion boundaries retain executable substitutions and literal data. */
import { describe, expect, it } from "vitest"
import { classifyCommand, decide } from "../../../src/guardrails.ts"

const GRANTED = { status: "granted", path: "approval.json" } as const

describe.each([
  ["top level", (body: string) => body],
  ["saved script", (body: string) => `cat > x.sh <<'SAVED_END'\n${body}\nSAVED_END\nsh x.sh`],
] as const)("unbraced parameter modifiers: %s", (_, wrap) => {
  it.each([
    "echo $HOME:F:PATH=7:h; ls",
    "echo $HOME:F{PATH=7}h; git status",
    "echo $PWD:F:PATH=7:h && make",
    "ls $PWD:F:PATH=7:h",
    "cp mk.sh 7/git && echo $HOME:F:PATH=7:h && git status",
    'echo "$HOME:F:PATH=7:h"',
    "echo $HOME:h",
    "echo $HOME:F:x=1:h",
    "cat > .temp/x.sh <<'E'\necho $HOME:F:PATH=7:h; ls\nE\nsh .temp/x.sh",
    'echo "$HOME:F:PATH=7:h"; ls',
    "ls $PWD:F{PATH=7}h && make",
    ...["1", "9", "@", "*", "#", "?", "$"].flatMap((name) => [
      `echo $${name}:F:PATH=7:h`,
      `echo "$${name}:F{PATH=7}h"`,
    ]),
    "echo $HOME::h",
    'echo "$HOME:t"',
  ])("asks for modifier-capable suffixes: %s", (command) => {
    expect(decide(classifyCommand(wrap(command)), "standard", GRANTED)?.effect).toBe("ask")
  })

  it.each([
    'echo "$PATH:/usr/bin"',
    "echo $HOME/x",
    "echo $HOME:",
    'echo "$HOME:"',
    "echo $HOME:./x",
    "echo $HOME:-x",
    "echo $HOME:~/x",
    "echo $HOME:123",
    "echo ${HOME}:h",
    "echo '$HOME:F:PATH=7:h'",
    "echo $HOME:; echo ok",
  ])("keeps plain suffixes and braced parameters quiet: %s", (command) => {
    expect(classifyCommand(wrap(command)).kind).toBe("other")
    expect(decide(classifyCommand(wrap(command)), "standard", GRANTED)).toBeUndefined()
  })
  it("checks a nonliteral equality word even when braces make its suffix inert", () => {
    expect(decide(classifyCommand(wrap('echo "${HOME}:F:PATH=7:h"')), "standard", GRANTED)?.effect).toBe("ask")
  })
})

describe("shell expansion boundaries", () => {
  it.each([
    "git &>/dev/null push",
    "git &>>/dev/null push",
    "git commit &>/dev/null --amend",
    "git commit &>>/dev/null --amend",
  ])("retains arguments across combined redirection: %s", (command) => {
    expect(classifyCommand(command).kind).toBe(command.includes("commit") ? "history-rewrite" : "remote-mutation")
  })

  it.each([
    "cat <<EOF\n$(git push)\nEOF\n",
    "git commit -F - <<EOF\n$(git push)\nEOF\n",
    "cat <<EOF\n`git push`\nEOF\n",
    "cat <<EOF\n${value:-$(git push)}\nEOF\n",
    "cat <<EOF\n${value:-`git push`}\nEOF\n",
    "cat <<EOF\n'$(git push)'\nEOF\n",
    "cat <<EOF\n$(printf '%s' \"$(git push)\")\nEOF\n",
    "cat <<-EOF\n\t$(git push)\n\tEOF\n",
    "(cat <<EOF)\n$(git push)\nEOF\n",
    "echo {safe,$(git push)}",
    "echo {safe,`git push`}",
  ])("asks for unsupported executable substitutions inside data: %s", (command) => {
    expect(classifyCommand(command).kind).toBe("unparsed-git")
    expect(decide(classifyCommand(command), "standard", GRANTED)?.effect).toBe("ask")
  })

  it("classifies a recursive deletion inside a brace alternative", () => {
    expect(classifyCommand("echo {safe,$(rm -rf build)}").kind).toBe("destructive-local")
  })

  it.each([
    "{g..g}it push",
    "g{i..i}t push",
    "git p{u..u}sh",
    "git {status,push}",
    "git add {safe,--force}",
    "git add {--force,safe}",
    "git add src/{safe,$options}",
    "git add {safe,${options}}",
    "git commit -{-,x}amend",
    "git commit {,}--amend",
    "git commit {foo,}--amend",
    "git commit {,foo}--amend",
    "git commit {{,},foo}--amend",
    "git add src/{one,two}.ts",
    "git diff -- src/{one,two}.ts",
    "git add src/{1..3}.ts",
    "git add src/{one,{two,three}}.ts",
    "git add src/{one,'--two'}.ts",
    "git add src/{,one}--two.ts",
    "cat <<EOF\n\\$(git push) \\`git push\\`\nEOF\n",
  ])("asks for runtime command words and unsupported argument syntax: %s", (command) => {
    expect(classifyCommand(command).kind).toBe("unparsed-git")
  })

  it.each(["cat <<'EOF'\n$(git push) `git push` ${value:-$(git push)}\nEOF\n", 'cat <<"EOF"\n$(git push)\nEOF\n'])(
    "keeps filename expansions and quoted data readable: %s",
    (command) => {
      expect(decide(classifyCommand(command), "standard", GRANTED)).toBeUndefined()
    },
  )

  // Grammar gate: groups, `&>` and partly quoted or escaped heredoc delimiters are outside the
  // modelled grammar, so these ask.
  it.each([
    "{ echo one,two; }",
    "git status &>/dev/null",
    "git status &>>/dev/null",
    "cat <<E'OF'\n$(git push)\nEOF\n",
    "cat <<\\EOF\n$(git push)\nEOF\n",
    "(cat <<'EOF')\ngit push\nEOF\n",
    "{ cat <<'EOF'; }\ngit push\nEOF\n",
    "(cat <<'EOF')\nit's prose\nEOF\n",
  ])("asks for syntax outside the grammar: %s", (command) => {
    expect(decide(classifyCommand(command), "standard", GRANTED)?.effect).toBe("ask")
  })

  it("reads executable commands after a nested heredoc", () => {
    expect(classifyCommand("(cat <<'EOF')\ngit push is data\nEOF\ngit push\n").kind).toBe("remote-mutation")
  })

  it.each(["git rebase $target", "git commit --amend $options"])(
    "preserves history warnings with uncertain arguments: %s",
    (command) => {
      const classification = classifyCommand(command)
      expect(classification.kind).toBe("history-rewrite")
      expect(decide(classification, "standard", GRANTED, { dirtyPaths: ["notes.txt"] })?.message).toContain(
        "discard uncommitted changes: notes.txt",
      )
    },
  )

  it.each([
    ["GIT_EDITOR=vim git reset --hard", "destructive-local"],
    ["git -c core.fsmonitor=hook commit --amend", "history-rewrite"],
  ])("preserves known discard warnings with executable settings: %s", (command, kind) => {
    const classification = classifyCommand(command)
    expect(classification.kind).toBe(kind)
    expect(decide(classification, "standard", GRANTED, { dirtyPaths: ["notes.txt"] })?.message).toContain(
      "discard uncommitted changes: notes.txt",
    )
  })
})

describe("quote and arithmetic boundaries", () => {
  it.each([
    String.raw`echo 'a\' ; git push ; echo \'`,
    'echo $(( true << "1" ))\ngit push\n1',
    'for (( i=0; i << "1"; i++ )); do git push; done\n1',
    '(( (n + 1) << "1" ))\ngit push\n1',
    "diff a <(git push)",
    "(( n + $(git push) ))",
    "[[ $(git push) < b ]]",
    '(( n < 5 && (n << "1") > 0 ))',
    "cat > notes.txt <<'EOF'\n{ git is great }\nEOF",
    "cat > notes.txt <<'EOF'\nif git is installed, run it\nEOF",
  ])("keeps executable text visible: %s", (command) => {
    expect(decide(classifyCommand(command), "standard", GRANTED)?.effect).toBe("ask")
  })

  it.each(["python3 x.py < in.txt", "node tool.js < data.json", "bash f"])(
    "admits safe syntax or prose: %s",
    (command) => {
      expect(classifyCommand(command).kind).toBe("other")
      expect(decide(classifyCommand(command), "standard", GRANTED)).toBeUndefined()
    },
  )

  // Grammar gate: ANSI-C quotes, arithmetic, `[[`, process substitution and `if` are outside the
  // modelled grammar, so these ask.
  it.each([
    String.raw`echo $'a\'b'`,
    "echo $(( (n + 1) << 1 ))",
    '[[ "a < b" < c ]] && echo x',
    "diff a <(sort b)",
    "if (( $# < 1 )); then echo x; fi",
  ])("asks for syntax outside the grammar: %s", (command) => {
    expect(decide(classifyCommand(command), "standard", GRANTED)?.effect).toBe("ask")
  })

  it.each(["bash < f", "env bash < f", "eval < f", "python3 - <<'EOF'\nprint(1)\nEOF"])(
    "retains launcher or opaque input gates: %s",
    (command) => {
      expect(decide(classifyCommand(command), "standard", GRANTED)?.effect).toBe("ask")
    },
  )
})
