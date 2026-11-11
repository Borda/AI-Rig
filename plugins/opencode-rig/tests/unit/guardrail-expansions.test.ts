/** Shell expansion boundaries retain executable substitutions and literal data. */
import { describe, expect, it } from "vitest"
import { classifyCommand, decide } from "../../src/guardrails.ts"

const GRANTED = { status: "granted", path: "approval.json" } as const

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

  it.each([
    "{ echo one,two; }",
    "git status &>/dev/null",
    "git status &>>/dev/null",
    "cat <<'EOF'\n$(git push) `git push` ${value:-$(git push)}\nEOF\n",
    'cat <<"EOF"\n$(git push)\nEOF\n',
    "cat <<E'OF'\n$(git push)\nEOF\n",
    "cat <<\\EOF\n$(git push)\nEOF\n",
    "(cat <<'EOF')\ngit push\nEOF\n",
    "{ cat <<'EOF'; }\ngit push\nEOF\n",
    "(cat <<'EOF')\nit's prose\nEOF\n",
    "git commit -F - <<'EOF'\n$(git push)\nEOF\n",
  ])("keeps filename expansions and quoted data readable: %s", (command) => {
    expect(decide(classifyCommand(command), "standard", GRANTED)).toBeUndefined()
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
