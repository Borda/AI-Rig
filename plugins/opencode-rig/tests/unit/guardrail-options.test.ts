/** Git option forms retain their read, commit, and destructive classifications. */
import { describe, expect, it } from "vitest"
import { classifyCommand } from "../../src/guardrails.ts"

describe("literal option semantics", () => {
  it.each([
    'env -iS"git push"',
    'env -vS"git push"',
    'env -ivS"git push"',
    'env --split="git push"',
    'env --s="git push"',
    'env -iS"git status"',
    "git config edit",
    "git config --local edit",
    "git config --worktree edit",
    "git config --global edit",
    "git config --system edit",
    "git config edit --local",
    "git config set",
    "git config unset",
    "git config rename-section",
    "git config remove-section",
    "git config nonsense",
    "git status -sZ",
    "git log -Zn5",
    "git commit -aZm message",
    "git commit -am",
    "git branch --points-at",
    "git tag --contains",
    "git grep -nOtool pattern",
    "git grep --open-files-in-pager pattern",
    "git show-ref --unknown",
  ])("asks for an unsupported or incomplete form: %s", (command) => {
    expect(classifyCommand(command).kind).toBe("unparsed-git")
  })

  it.each([
    "git rm -f file.txt",
    "git rm -r -f directory",
    "git rm --force file.txt",
    "git rm --fo file.txt",
    "git rm -rf directory",
    "git rm -fr directory",
    "git checkout -B topic main",
    "git checkout -Btopic main",
    "git switch -C topic main",
    "git switch -Ctopic main",
    "git switch --force-create topic main",
    "git switch --force-create=topic main",
    "git switch --force-c topic main",
    "git checkout src/a.ts",
    "git checkout '*.ts'",
    "git checkout src/?.ts",
    "git checkout '[ab]'",
    "git checkout ./Makefile",
    "git checkout ':(literal)Makefile'",
  ])("guards destructive local options and path restoration: %s", (command) => {
    expect(classifyCommand(command).kind).toBe("destructive-local")
  })

  it.each([
    "git log -n5",
    "git status -uno",
    "git diff -U0",
    "git status -sb",
    "git log -S needle",
    "git log -Sneedle",
    "git diff -M",
    "git diff -M50%",
    "git diff --diff-filter=AM",
    "git diff --color-words",
    "git branch --merged main",
    "git branch --merged",
    "git branch --no-merged",
    "git branch --no-merged main",
    "git branch --contains main",
    "git branch --no-contains main",
    "git branch --points-at main",
    "git tag --merged main",
    "git tag --merged",
    "git tag --no-merged",
    "git tag --no-merged main",
    "git tag --contains main",
    "git tag --no-contains main",
    "git tag --points-at main",
    "git grep pattern",
    "git grep -ni pattern",
    "git grep -e needle -- src",
    "git show-ref",
    "git show-ref --verify refs/heads/main",
    "git show-ref --hash=8 --heads",
  ])("keeps everyday reads readable: %s", (command) => {
    expect(classifyCommand(command).kind).toBe("read-only-git")
  })

  it.each(['git commit -am "msg"', 'git commit -am"msg"', "git commit -m--amend"])(
    "keeps clustered and attached message commits plain: %s",
    (command) => {
      expect(classifyCommand(command).kind).toBe("plain-commit")
    },
  )

  it.each([
    "git config remote.origin.url",
    "git config --local user.name",
    "git checkout feature/x",
    "git checkout -",
    "git checkout -b topic main",
    "git switch -c topic main",
    "git rm --cached file.txt",
    "git add -fn src/a.ts",
    "git checkout -bfeature main",
    "git restore --staged -sWithW .",
  ])("keeps non-destructive local operations unguarded: %s", (command) => {
    expect(classifyCommand(command).kind).toBe("other")
  })
})
