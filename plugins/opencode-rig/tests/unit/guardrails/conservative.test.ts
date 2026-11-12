/** Conservative command recognition rejects unsupported executable syntax. */
import { describe, expect, it } from "vitest"
import { classifyCommand, decide } from "../../../src/guardrails.ts"

const GRANTED = { status: "granted", path: "approval.json" } as const

describe("conservative shell recognition", () => {
  it.each([
    ["parameter process substitution", "echo ${v:-<(git status)}"],
    ["quoted parameter delimiter", "echo ${v:-'}'$(git status)}"],
    ["eval delimiter", "eval -- 'git status'"],
    ["piped shell program", "printf '%s' 'g\"it\" status' | sh"],
    ["encoded shell program", "pwsh -EncodedCommand ZwBpAHQAIABzAHQAYQB0AHUAcwA="],
    ["abbreviated shell carrier", "pwsh -c 'git status'"],
    ["uppercase shell option", "bash -Lc 'git status'"],
    ["process substitution source", "source <(printf '%s' 'git status')"],
    ["named descriptor", "git {output}>out status"],
    ["unquoted heredoc expansion", "cat <<EOF\n$(git status)\nEOF\n"],
    ["unquoted heredoc git consumer", "git commit -F - <<EOF\nmessage\nEOF\n"],
    ["brace embedded substitution", "echo {safe,$(git status)}"],
    ["brace sequence executable", "{g..g}it status"],
    ["brace sequence option", "git commit {-1..1}"],
    ["brace alternatives", "git add src/{a,b}.ts"],
    ["split alias executable", "alias inspect=\"g'i't status\"; inspect"],
    ["shell option builtin", "shopt -s expand_aliases; git status"],
    ["watch argv", "watch -x sh -c 'git status'"],
    ["login shell carrier", "su -c 'git status'"],
    ["terminal shell carrier", "script -c 'git status'"],
    ["Windows command carrier", "cmd /c 'git status'"],
    ["command substitution", "echo $(git status)"],
    ["backtick substitution", "echo `git status`"],
    ["here string", "sh <<< 'git status'"],
    ["subshell", "(git status)"],
    ["group", "{ git status; }"],
    ["dynamic command", "$tool status"],
    ["glob executable", "/usr/bin/g?t status"],
    ["glob subcommand", "git st?tus"],
    ["unknown wrapper flag", "sudo --unknown operand git status"],
    ["env unknown flag", "env -k git status"],
    ["nohup unknown flag", "nohup -x git status"],
    ["command unknown flag", "command -i git status"],
    ["builtin unknown flag", "builtin --foreground git status"],
    ["unknown Git flag", "git --unknown status"],
    ["sourced file", "printf '%s' 'git status' >task.sh; . task.sh"],
    ["xargs execution", "xargs git status"],
    ["find execution", "find . -exec git status \\;"],
    ["dynamic redirection", "git status >$output"],
    ["unknown GitHub flag", "gh --unknown pr view"],
    ["clustered API flags", "gh api repos/o/r -iXPOST"],
    ["quoted shell input", "sh <<'EOF'\ngit status\nEOF\n"],
    ["fragmented substituted command", 'echo $(g"it" status)'],
    ["fragmented backtick command", 'echo `g"it" status`'],
    ["fragmented parameter command", 'echo ${v:-$(g"it" status)}'],
    ["fragmented process command", 'echo <(g"it" status)'],
    ["fragmented heredoc command", 'cat <<EOF\n$(g"it" status)\nEOF\n'],
  ])("asks for %s", (_label, command) => {
    expect(classifyCommand(command).kind).toBe("unparsed-git")
    expect(decide(classifyCommand(command), "standard", GRANTED)?.effect).toBe("ask")
  })

  it.each([
    "git -c core.askPass=hook status",
    "git -c core.gitProxy=hook fetch",
    "git -c gpg.program=hook log",
    "git -c protocol.ext.allow=always fetch",
    "git -c user.name=$author status",
    "git --config-env=user.name=AUTHOR status",
    "git config alias.inspect status",
    "git config set core.editor hook",
    "git config --add include.path settings",
    "git config --unset credential.helper",
    "git config core.editor get",
    "git config set alias.inspect get",
    "git config set core.editor -- --get",
    "git config core.editor -- --get",
    "git config --edit",
    "PAGER=hook; git status",
    "export PAGER=hook; git status",
    "declare -x EDITOR=hook; git status",
    "typeset -x GIT_EXEC_PATH=tools; git status",
    "GIT_ASKPASS=hook git status",
    "SSH_ASKPASS=hook git status",
    "env GIT_TRACE=1 git status",
    "GIT_PAGER=hook sh -c 'git status'",
  ])("asks for executable or dynamic settings: %s", (command) => {
    expect(classifyCommand(command).kind).toBe("unparsed-git")
  })

  it.each([
    "gh pr lock 1",
    "gh repo sync owner/repo",
    "gh repo fork owner/repo",
    "gh pr update-branch 1",
    "gh project item-add 1",
    "gh alias set inspect 'pr view'",
    "gh variable get KEY",
    "gh unknown view",
    "gh auth view",
    "gh api repos/o/r -X GET",
    "gh api repos/o/r --method=GET -f page=1",
    "gh api graphql -f 'query=query { viewer { login } }'",
  ])("guards every GitHub form outside the read allowlist: %s", (command) => {
    expect(classifyCommand(command).kind).toBe("remote-mutation")
  })

  it.each([
    "git status 2>&1",
    "git status >&2",
    "git diff HEAD > x.patch",

    "git >out status",
    "git status </input",
    "git show HEAD@{1}",
    "git show stash@{0}",
    "git diff @{u}",
    "git log -- @{upstream}",

    "git add 'src/{a,b}.ts'",
    "git -c user.name=developer -c user.email=dev@example.test status",
    "git config --get core.editor",
    "git config get core.editor",
    "gh auth status",
    "gh repo view",
    "gh issue list",

    "git commit -m 'document setting EDITOR=vim'",
    "echo $(printf safe)",
    "echo 'git push'",
    "printf '%s' 'git push'",
    "grep git docs",

    "pwsh -Command Write-Output 'hello world'",
    "grep rm -rf docs",
  ])("keeps literal data and recognised reads readable: %s", (command) => {
    expect(decide(classifyCommand(command), "standard", GRANTED)).toBeUndefined()
  })

  // Grammar gate: `&>`, groups, uncatalogued builtins (which, type, where, command, eval, exec) and
  // assignments outside the allowed names leave the modelled grammar, so these ask.
  it.each([
    "git &>out status",
    "git &>>out status",
    "{ echo safe; }",
    "where git",
    "eval 'printf safe'",
    'eval "$(ssh-agent)"',
    "exec -a label docker rm -f container",
    "env GIT_TRACE=1 echo git",
    "PAGER=hook; printf '%s' git",
  ])("asks for syntax outside the grammar: %s", (command) => {
    expect(decide(classifyCommand(command), "standard", GRANTED)?.effect).toBe("ask")
  })

  it.each([
    "setsid -w rm -rf build",
    "sudo -A rm -rf build",
    "exec -a label rm -rf build",
    "strace -ff rm -rf build",
    "eval -- 'rm -rf build'",
    "custom-launcher rm -rf build",
  ])("preserves destructive classification despite unsupported wrappers: %s", (command) => {
    expect(classifyCommand(command).kind).toBe("destructive-local")
  })

  it("retains publication behind an unknown wrapper option", () => {
    expect(classifyCommand("setsid -w npm publish").kind).toBe("remote-mutation")
  })

  it("retains the digit operand before combined output redirection", () => {
    expect(classifyCommand("git checkout HEAD 2&>out").kind).toBe("destructive-local")
  })

  it("retains remote severity when unsupported syntax also contains deletion", () => {
    expect(classifyCommand("git push; echo $(rm -rf build)").kind).toBe("remote-mutation")
  })

  it("skips quoted heredoc data before reading the next command", () => {
    expect(classifyCommand("cat <<'EOF'\nit's git push prose\nEOF\ngit status\n").kind).toBe("read-only-git")
  })
})

describe.each(["top level", "saved script"])("restricted tool forms: %s", (scope) => {
  const wrap = (body: string) =>
    scope === "top level" ? body : `cat > x.sh <<'SAVED_END'\n${body}\nSAVED_END\nsh x.sh`
  it.each([
    "docker rm -f container",
    "npm rm package",
    "sudo -A docker rm -f container",
    "sudo -A npm rm -f package",
    "strace -ff docker rm -f container",
  ])("asks for the complete invocation: %s", (body) => {
    expect(decide(classifyCommand(wrap(body)), "standard", GRANTED)?.effect).toBe("ask")
  })
})
