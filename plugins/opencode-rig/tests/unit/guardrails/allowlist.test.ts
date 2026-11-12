/** Git reads require literal arguments and exact, explicitly supported options. */
import { describe, expect, it } from "vitest"
import { classifyCommand, decide } from "../../../src/guardrails.ts"

const GRANTED = { status: "granted", path: "approval.json" } as const

describe("literal Git allowlist", () => {
  it.each([
    'env -S"git push"',
    "env -Sgit push",
    'env --split-string="git push"',
    'env -S"git reset --hard"',
    'env --split-string="gh pr merge"',
    'env -S"GIT push"',
    "git fetch --upload-pack=hook .",
    "git ls-remote --upload-pack=hook .",
    "git clone --upload-pack=hook . copy",
    "git pull --exec=hook",
    "git fetch -u hook .",
    "git grep -Ohook pattern",
    "git grep --open-files-in-pager=hook pattern",
    "git difftool -x hook",
    "git mergetool --tool=hook",
    "git submodule foreach '${tool} status'",
    "git bisect run hook",
    "git send-pack origin main",
    "git send-email patch.txt",
    "git http-push origin main",
    "git request-pull main origin",
    "git remote-ext origin command",
    "git remote-fd origin 3",
    "git receive-pack .",
    "git archive HEAD",
    "git bundle create copy.bundle HEAD",
    "git daemon",
    "git instaweb",
    "git web--browse page",
    "git help status",
    "git gc",
    "git fast-import",
    "git am patch.txt",
    "git apply patch.txt",
    "git merge topic",
    "git cherry-pick abc123",
    "git status --sho",
    "git diff --ext-diff",
    "git show --textconv HEAD",
    "git log --exec=hook",
    "git add --pathspec-from-file=paths.txt",
    "git branch topic",
    "git branch --create-reflog topic",
    "git branch -d topic",
    "git branch -m old new",
    "git branch --set-upstream-to=origin/main topic",
    "git tag v1",
    "git tag -d v1",
    "git reflog expire --all",
    "git symbolic-ref HEAD refs/heads/topic",
    "git symbolic-ref -d HEAD",
    "git config --edit",
    "git config --get user.name --edit",
    "git config user.name developer",
    "git config set color.ui never",
    "git config --global user.email dev@example.test",
    "git -c color.ui=never status",
    "git -c user.signingkey=key status",
    "git -cuser.name=developer status",
    "git --config-env=user.name=AUTHOR status",
    "git -C repo status",
    "git --git-dir=repo status",
    "git --help",
    "git",
    "AUTHOR=developer git status",
    "git commit --unknown",
    "git checkout --branch=feature main",
  ])("asks outside explicit forms: %s", (command) => {
    const classification = classifyCommand(command)
    expect(classification.kind).toBe("unparsed-git")
    expect(decide(classification, "standard", GRANTED)?.effect).toBe("ask")
  })

  it.each([
    ["git reset --ha HEAD", "destructive-local"],
    ["git checkout --fo main", "destructive-local"],
    ["git commit --am", "history-rewrite"],
    ["rm --re build", "destructive-local"],
    ["git restore -sHEADWithS .", "destructive-local"],
    ["git restore --source -S .", "destructive-local"],
    ["git restore --source=-S .", "destructive-local"],
    ["git branch -t -D topic", "destructive-local"],
    ["git branch --track -D topic", "destructive-local"],
    ["git checkout -b feature main", "other"],
    ["git restore --staged --source HEADWithW .", "other"],
    ["git commit -m '--am'", "plain-commit"],
    ["git status -sb", "read-only-git"],
    ["git add -fn src/a.ts", "other"],
    ["git commit -am message", "plain-commit"],
    ["git restore --staged -sWithW .", "other"],
    ["git checkout -bfeature main", "other"],
  ])("preserves guarded semantics without interpreting operands as flags: %s", (command, kind) => {
    expect(classifyCommand(command).kind).toBe(kind)
  })

  it.each([
    "git status 2>&1",
    "git diff HEAD > x.patch",

    "git log --oneline -5",
    "git diff --stat",
    "git rev-list --count HEAD",
    "git describe --tags",
    "git branch",
    "git branch --list topic",
    "git branch -l topic",
    "git branch -vv",
    "git ls-files --stage",
    "git ls-tree HEAD",
    "git cat-file -p HEAD",
    "git blame src/a.ts",
    "git shortlog -s",
    "git remote",
    "git remote -v",
    "git remote show origin",
    "git remote get-url origin",
    "git stash list",
    "git stash show",
    "git tag --list 'v*'",
    "git reflog show",
    "git worktree list",
    "git name-rev HEAD",
    "git merge-base HEAD topic",
    "git for-each-ref --format='%(refname)'",
    "git symbolic-ref HEAD",
    "git var GIT_AUTHOR_IDENT",
    "git version",
    "git --version",
    "git -c user.name=developer -c user.email=dev@example.test status",
    "git diff -u HEAD",
  ])("keeps an explicit read readable: %s", (command) => {
    expect(classifyCommand(command).kind).toBe("read-only-git")
  })

  // Grammar gate: combined and descriptor-closing redirections are outside the modelled forms.
  it.each(["git status &>>log", "git status 2>&-"])(
    "asks for a read behind an unmodelled redirection: %s",
    (command) => {
      expect(decide(classifyCommand(command), "standard", GRANTED)?.effect).toBe("ask")
    },
  )

  it.each([
    "git config --get user.name",
    "git config -l",
    "git config --list",
    "git config remote.origin.url",
    "git config --get-all core.editor",
    "git config core.editor --get",
    "git config get core.editor",

    "git add -u",
  ])("keeps supported data and config reads unguarded: %s", (command) => {
    expect(decide(classifyCommand(command), "standard", GRANTED)).toBeUndefined()
  })
})
