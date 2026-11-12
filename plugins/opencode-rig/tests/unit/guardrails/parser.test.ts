import { describe, expect, it, vi } from "vitest"
import { classifyCommand, decide } from "../../../src/guardrails.ts"

const GRANTED = { status: "granted", path: "approval.json" } as const

describe("shell-aware guardrails", () => {
  it.each([
    "git -C /repo push --force origin main",
    "/usr/bin/git --no-pager -c core.quotePath=false push origin main",
    "command git push",
    "sudo --prompt hello git push --force",
    "sudo --chdir /repo git push --force",
    "time git push --force",
    "builtin command git push --force",
    "git.exe push --force origin main",
    "git push --force $destination",
    "C:/tools/git.exe push",
    String.raw`'C:\tools\git.exe' push`,
    "exec git push",
    "sudo --user developer git push",
    "nohup git push",
    "env -u GIT_DIR KEY=value git push",
    "timeout --signal=TERM 5 sudo -u developer git push",
    "sh -c 'git push --force origin main'",
    "bash -lc 'command git push'",
    "echo ready & git push",
    "git status; git push; echo 'unterminated",
    "(git push)",
    "{ git push; }",
    "if git push; then echo done; fi",
    "(".repeat(40) + "g'i't push --force" + ")".repeat(40),
  ])("guards executable remote mutation: %s", (command) => {
    expect(classifyCommand(command).kind).toBe("remote-mutation")
    expect(decide(classifyCommand(command), "standard", GRANTED)?.effect).toBe("ask")
  })

  it.each([
    "su -c 'git push'",
    "script -c 'git push' /dev/null",
    "watch 'git push'",
    "watch -n 2 git push",
    'eval "git push"',
    "shopt -s expand_aliases; alias deploy='git push'; deploy",
    "pwsh -Command 'git push'",
    "powershell -Command 'git push'",
    'cmd /c "git push"',
    "cmd /c git push",
    "pwsh -Command git push",
    "cat <(git push)",
    "cat >(git push)",
    "git status >$(git push)",
    "sh <<< 'git push'",
    "sh <<'EOF'\ngit push\nEOF\n",
    "env bash <<-EOF\n\tgit push\n\tEOF\n",
    "echo $(git push)",
    'echo "$(git push)"',
    "echo `git push`",
    "cat <<FIRST <<'SECOND'\ngit push\nFIRST\nit's prose\nSECOND\n",
  ])("asks for executable Git syntax outside recognised shapes: %s", (command) => {
    expect(classifyCommand(command).kind).toBe("unparsed-git")
    expect(decide(classifyCommand(command), "standard", GRANTED)?.effect).toBe("ask")
  })

  it.each([
    "git checkout .",
    "git checkout HEAD file.txt",
    "git checkout -b feature/parser --force",
    "git branch --force --delete branch",
    "git branch -d -f branch",
    "git worktree remove -f worktree",
    "git checkout -- src/a.ts",
    "git restore --staged --worktree .",
    "git restore --staged -W .",
    "git restore -SW .",
    "git switch --discard-changes main",
    "git switch -f main",
    "git reset HEAD --hard",
    "git reset --har HEAD",
    "git switch --discard main",
    "git restore --stag --work .",
    "git checkout --for main",
    "git checkout -q HEAD file.txt",
    "git checkout src/a.ts",
    "rm --recursive --force build",
    "rm -Rf build",
    "nice rm -rf build",
    "xargs rm --recursive build",
  ])("guards worktree destruction: %s", (command) => {
    expect(classifyCommand(command).kind).toBe("destructive-local")
    expect(decide(classifyCommand(command), "standard", GRANTED)?.effect).toBe("ask")
  })

  it.each([
    ["unclosed Git quote", "git 'unterminated"],
    ["fragmented executable option", "g'i't -p push --force origin main"],
    ["fragmented oversized command", "g'i't status " + "x".repeat(70_000) + "; g'i't push --force"],
    ["fragmented command beyond parse limit", " ".repeat(70_000) + "g'i't push --force"],
    ["unknown wrapper option", "sudo --unknown-option operand git push --force"],
    ["unknown wrapper fragmented option", "sudo --unknown-option operand g'i't push --force"],
    ["unknown invocation", "unrecognized-wrapper git push --force"],
    ["unknown fragmented invocation", "unrecognized-wrapper g'i't push --force"],
    ["oversized command", "git status " + "x".repeat(70_000)],
    ["excessive nesting", "(".repeat(40) + "git status" + ")".repeat(40)],
    ["dynamic executable", "tool=git; $tool push"],
    ["split dynamic executable", "g=gi; ${g}t push"],
    ["dynamic commit options", "git commit $opts"],
    ["configured alias", "git -c alias.publish=\'!git push\' publish"],
    ["unknown alias", "git publish"],
    ["Windows executable ambiguity", String.raw`C:\tools\git.exe push`],
    ["parameter executable", "${tool:-git} push"],
    ["unknown Git option", "git --unknown-global-option push"],
    ["unclosed gh quote", "gh 'unterminated"],
    ["unsupported gh option", "gh --repo owner/repo pr merge"],
    ["split env command", 'env -S "git push"'],
    ["dynamic shell string", 'cmd="git push"; sh -c "$cmd"'],
    ["brace command", "{git,push}"],
    ["question glob command", "/usr/bin/g?t push"],
    ["star glob command", "/usr/bin/g*t push"],
    ["bracket glob command", "/usr/bin/g[i]t push"],
    ["dot source", "printf '%s' 'git push' > task.sh; . task.sh"],
    ["source command", "printf '%s' 'git push' > task.sh; source task.sh"],
    ["shell input redirect", "printf '%s' 'git push' > task.sh; sh < task.sh"],
    ["global directory override", "git -C /repo status --short"],
    ["remote ref discovery", "git ls-remote"],
    ["remote integration", "git pull"],
    ["help command", "git --help"],
    ["bare invocation", "git"],
    ["uncatalogued read", "git check-ignore"],
    ["submodule update", "git submodule update"],
  ])("asks when Git syntax cannot be resolved: %s", (_label, command) => {
    const decision = decide(classifyCommand(command), "standard", GRANTED)
    expect(decision?.effect).toBe("ask")
    expect(decision?.message).toMatch(/cannot safely classify/)
  })

  it.each(["git status --short", "g'i't status --short"])("asks if the lexer throws while reading %s", (command) => {
    const slice = String.prototype.slice
    const spy = vi.spyOn(String.prototype, "slice").mockImplementation(function (this: string, start, end) {
      if (String(this) === command) throw new Error("lexer boundary failure")
      return slice.call(this, start, end)
    })
    try {
      const decision = decide(classifyCommand(command), "standard", GRANTED)
      expect(decision?.effect).toBe("ask")
      expect(decision?.message).toMatch(/cannot safely classify/)
    } finally {
      spy.mockRestore()
    }
  })

  it.each([
    "echo 'git push; rm -rf build'",
    'printf "%s" "git push"',
    "git commit -m 'fix; git push'",
    "git restore --staged .",
    "git restore -S .",
    "git switch main",
    "git checkout main",
    "git checkout -b feature/parser",
    "git checkout feature/foo",
    "git checkout -t origin/feature",
    "git commit -m 'document --amend behavior'",
    "git commit -m '--amend'",
    "echo '$(git push)'",
    "grep -r 'git push' docs",
    "grep -r git docs",
    "echo '{git,push} /usr/bin/g?t /usr/bin/g[i]t'",
    "git status 2>&1",
    "git diff HEAD > x.patch",
    "git diff HEAD >x.patch 2>&1",
    "git log --grep='> push'",

    "git rev-list",
    "git reflog",
    "git merge-base",
    "git --version",
    "git diff HEAD >&2",
    "git ls-tree",
    "cat <<'EOF'\nit's prose\nEOF\n",
    "cat <<-EOF\n\tit's prose\n\tEOF\n",
  ])("keeps quoted data and safe operations unguarded: %s", (command) => {
    expect(decide(classifyCommand(command), "standard", GRANTED)).toBeUndefined()
  })

  // Grammar gate: uncatalogued builtins, case and a comment with an unbalanced quote are outside
  // the grammar, so these ask.
  it.each(["case x in x) echo yes;; esac", "cat <<'EOF'\nit's prose\nEOF\ngit status\n# '\n"])(
    "asks for syntax outside the grammar: %s",
    (command) => {
      expect(decide(classifyCommand(command), "standard", GRANTED)?.effect).toBe("ask")
    },
  )

  it("classifies an abbreviated amend as a history rewrite", () => {
    expect(classifyCommand("git commit --amen").kind).toBe("history-rewrite")
  })

  it("keeps the discard warning when destructive syntax also contains uncertainty", () => {
    const classification = classifyCommand("git reset --hard $target 2>&1")
    expect(classification.kind).toBe("destructive-local")
    expect(decide(classification, "standard", GRANTED, { dirtyPaths: ["notes.txt"] })?.message).toContain(
      "discard uncommitted changes: notes.txt",
    )
  })

  it.each([
    ["git add src/*.ts", "unparsed-git"],
    ["git log -- *.ts", "read-only-git"],
    // Grammar gate: a leading glob before `--` can match a file named like a Git option.
    ["git rm *.log", "unparsed-git"],
    ["git diff HEAD -- 'src/?.ts'", "read-only-git"],
  ])("classifies a glob in a git argument: %s", (command, kind) => {
    expect(classifyCommand(command).kind).toBe(kind)
  })

  it("reads executable commands after a heredoc without letting prose quotes span its boundary", () => {
    expect(classifyCommand("cat <<'EOF'\nit's prose\nEOF\ngit push\n# '\n").kind).toBe("remote-mutation")
  })
})
