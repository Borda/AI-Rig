/** Invocation parsing preserves argv boundaries and execution-capable settings. */
import { describe, expect, it } from "vitest"
import { classifyCommand, decide } from "../../../src/guardrails.ts"

const GRANTED = { status: "granted", path: "approval.json" } as const

describe("nested invocation boundaries", () => {
  it.each([
    "watch -x sh -c 'git push'",
    "watch --exec --interval 2 sh -c 'git push'",
    "watch -xn 2 sh -c 'git push'",
    "eval \"g'i't push\"",
    "eval git push",
    "shopt -s expand_aliases; alias deploy=\"g'i't push\"; deploy",
    'alias deploy="g\'i\'t push" other="echo safe"',
    "watch -x sh -c 'git status'",
    "watch -x echo git push",
  ])("asks for unsupported nested shell programs: %s", (command) => {
    expect(classifyCommand(command).kind).toBe("unparsed-git")
  })

  it.each(['cmd /c sh -c "git push"', 'pwsh -Command sh -c "git push"', 'powershell -Command sh -c "git push"'])(
    "asks when a carrier joins words with interpreter-specific quoting: %s",
    (command) => {
      expect(decide(classifyCommand(command), "standard", GRANTED)?.effect).toBe("ask")
    },
  )

  it.each(["watch --exec printf 'rm -rf build'"])("asks for unmodelled wrapper operands: %s", (command) => {
    expect(decide(classifyCommand(command), "standard", GRANTED)?.effect).toBe("ask")
  })

  // Grammar gate: eval and alias are uncatalogued builtins, so these ask.
  it.each(["eval 'printf safe'", "alias greet='printf safe'", 'eval "$(ssh-agent)"'])(
    "asks for builtins outside the grammar: %s",
    (command) => {
      expect(decide(classifyCommand(command), "standard", GRANTED)?.effect).toBe("ask")
    },
  )
})

describe("guarded executable options", () => {
  it.each([
    "git checkout --pathspec-from-file=paths.txt",
    "git checkout --pathspec-from-file paths.txt",
    "git checkout --pathspec-file-nul --pathspec-from-file=paths.txt",
    "git checkout --pathspec-file-nul",
  ])("guards checkout path-file restoration: %s", (command) => {
    expect(classifyCommand(command).kind).toBe("destructive-local")
  })

  it.each([
    "gh issue create --title hello",
    "gh issue comment 2 --body hello",
    "gh gist create notes.txt",
    "gh label clone source/repo",
    "gh secret set TOKEN",
    "gh variable delete KEY",
    "gh workflow run build",
    "gh run cancel 123",
    "gh run rerun 123",
    "gh api repos/owner/repo/issues -X POST",
    "gh api repos/owner/repo/issues -XPOST",
    "gh api repos/owner/repo/issues --method=PATCH",
    "gh api repos/owner/repo/issues --method DELETE",
    "gh api repos/owner/repo/issues -f title=hello",
    "gh api repos/owner/repo/issues -Ftitle=hello",
    "gh api repos/owner/repo/issues --field title=hello",
    "gh api repos/owner/repo/issues --raw-field=title=hello",
    "gh api repos/owner/repo/issues --input payload.json",
    "gh api repos/owner/repo/issues --method GET -f title=hello",
    "gh variable get KEY",
    "gh api repos/owner/repo -X GET",
    "gh api repos/owner/repo --method=GET",
  ])("guards GitHub writes: %s", (command) => {
    expect(classifyCommand(command).kind).toBe("remote-mutation")
  })

  it.each([
    "gh issue view 2",
    "gh gist list",
    "gh label list",
    "gh secret list",
    "gh workflow view build",
    "gh run view 123",
    "gh api repos/owner/repo",
  ])("keeps GitHub reads unguarded: %s", (command) => {
    expect(decide(classifyCommand(command), "standard", GRANTED)).toBeUndefined()
  })

  it.each([
    "git -c core.fsmonitor=hook status",
    "git -ccore.fsmonitor=hook status",
    "git -c diff.external=hook diff",
    "git -c diff.custom.command=hook diff",
    "git -c diff.custom.textconv=hook diff",
    "git -c core.sshCommand=hook fetch",
    "git -c core.editor=hook commit",
    "git -c sequence.editor=hook rebase --continue",
    "git -c core.pager=hook log",
    "git -c pager.log=hook log",
    "git -c credential.helper=hook status",
    "git -c credential.https://example.org.helper=hook status",
    "git --config-env=core.fsmonitor=HOOK status",
    "git --config-env core.fsmonitor=HOOK status",
    "GIT_SSH_COMMAND=hook git status",
    "env GIT_EXTERNAL_DIFF=hook git diff",
    "GIT_EDITOR=hook git commit",
    "GIT_SEQUENCE_EDITOR=hook git status",
    "GIT_PAGER=hook git log",
    "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.fsmonitor GIT_CONFIG_VALUE_0=hook git status",
    "GIT_CONFIG_PARAMETERS=hook git status",
    "git -c core.quotepath=false status",
    "git --config-env=user.name=AUTHOR status",
    "env GIT_TRACE=1 git status",
    "git -c color.ui=never status",
    "AUTHOR=developer git status",
  ])("asks for execution-capable Git settings: %s", (command) => {
    expect(classifyCommand(command).kind).toBe("unparsed-git")
  })

  it.each(["git -c user.name=developer status", "git -c user.email=dev@example.test status"])(
    "keeps non-executable settings readable: %s",
    (command) => {
      expect(classifyCommand(command).kind).toBe("read-only-git")
    },
  )
})

describe("file deletion wrappers", () => {
  it.each([
    "nice -n 5 rm -rf build",
    "nice -2 rm -rf build",
    "GIT_EDITOR=vim rm -rf build",
    "env GIT_CONFIG_COUNT=0 rm -rf build",
    "ionice -c 3 rm -rf build",
    "xargs -I '{}' rm -rf '{}'",
    "xargs -n 1 rm -rf",
    "xargs -rn 1 rm -rf build",
    "xargs -0n1 rm -rf build",
    "flock lockfile rm -rf build",
    "flock -w 5 lockfile rm -rf build",
    "setsid --wait rm -rf build",
    "stdbuf -o L rm -rf build",
    "doas -u developer rm -rf build",
    "strace -o trace.log rm -rf build",
    "find . -exec rm -rf '{}' \\;",
    "find . -execdir rm --recursive '{}' +",
    "nice xargs rm -rf",
  ])("guards file removal executed by a known wrapper: %s", (command) => {
    expect(classifyCommand(command).kind).toBe("destructive-local")
  })

  it.each([
    "grep rm -rf docs",
    "nice echo rm -rf build",
    "xargs printf rm -rf",
    "flock lockfile echo rm -rf build",
    "find . -name rm -print",
    "find . -exec echo rm -rf '{}' \\;",
    "strace -o rm printf -- -rf",
    "xargs -I rm echo rm -rf",
    "xargs -rI rm echo rm -rf",
  ])("keeps rm data and non-file operations unguarded: %s", (command) => {
    expect(decide(classifyCommand(command), "standard", GRANTED)).toBeUndefined()
  })
})

describe.each(["top level", "saved script"])("restricted tool forms: %s", (scope) => {
  const wrap = (body: string) =>
    scope === "top level" ? body : `cat > x.sh <<'SAVED_END'\n${body}\nSAVED_END\nsh x.sh`
  it.each(["docker rm -f container"])("asks for the complete invocation: %s", (body) => {
    expect(decide(classifyCommand(wrap(body)), "standard", GRANTED)?.effect).toBe("ask")
  })
})
