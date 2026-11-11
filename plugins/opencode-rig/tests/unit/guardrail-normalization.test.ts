/** Guarded command recognition retains executable text across normalization. */
import { describe, expect, it } from "vitest"
import { classifyCommand, decide } from "../../src/guardrails.ts"

const GRANTED = { status: "granted", path: "approval.json" } as const

describe("executable text boundaries", () => {
  it.each([
    'VALUE="$(git push)"',
    'sudo -u "$(git push)" printf safe',
    'command -v "$(git push)"',
    "VALUE=\"$(g'i't push)\"",
    'command -v "$(GIT push)"',
    'echo "$(g=gi; ${g}t status)"',
    'echo "`g=gi; ${g}t status`"',
    'echo "$(printf \'%s\' "$(g=gi; ${g}t status)")"',
    'echo "$(g=gi; ${g}t status"',
    'echo safe > "$(g=gi; ${g}t status)"',
    "cat <<EOF\n$(g=gi; ${g}t status)\nEOF\n",
    "cat <<EOF\n`g=gi; ${g}t status`\nEOF\n",
    "printf safe | bash -s -- arg",
    "sh -c -- 'git status'",
    "sh -c -o pipefail 'git status'",
    "printf '%s' 'git status' | xargs -I{} sh -c '{}'",
    "eval 'g=gi; ${g}t status'",
    "su -c 'g=gi; ${g}t status'",
    "script -c 'g=gi; ${g}t status'",
    "bash -o pipefail -c 'g=gi; ${g}t status'",
    "source /dev/stdin <<'EOF'\ng=gi; ${g}t status\nEOF\n",
    "git commit -F - <<E\\\nOF\n$(git push)\nEOF\n",
  ])("asks for executable text it cannot account for: %s", (command) => {
    expect(classifyCommand(command).kind).toBe("unparsed-git")
    expect(decide(classifyCommand(command), "standard", GRANTED)?.effect).toBe("ask")
  })

  it.each([
    ["git checkout --pathspec-from=paths.txt", "destructive-local"],
    ["git remote -v update", "remote-mutation"],
    ["git branch topic -D", "destructive-local"],
    ["git branch -vD topic", "destructive-local"],
    ["git branch topic --del --for", "destructive-local"],
    ["git stash -q drop", "destructive-local"],
    ["git worktree -v remove checkout --for", "destructive-local"],
    ["rm --rec build", "destructive-local"],
    ["rm --for build", "destructive-local"],
    ["GIT status", "read-only-git"],
    ["GH pr view", "read-only-git"],
    ["GiT push", "remote-mutation"],
    ["env -x operand GIT push", "unparsed-git"],
  ])("normalizes guarded names and options in %s", (command, kind) => {
    expect(classifyCommand(command).kind).toBe(kind)
  })

  it.each([
    "git -c remote.origin.uploadpack=hook fetch",
    "git -c remote.origin.receivepack=hook fetch",
    "git -c uploadpack.packObjectsHook=hook fetch",
    "git -c include.path=settings status",
    "git config includeIf.gitdir:repo.path settings",
    "git config set uploadpack.packObjectsHook hook",
    "git config --add remote.origin.uploadpack hook",
    "git --config-env=remote.origin.uploadpack=HOOK fetch",
    "git -c unknown.setting=value status",
    "git config unknown.setting value",
  ])("asks for configuration outside the safe keys: %s", (command) => {
    expect(classifyCommand(command).kind).toBe("unparsed-git")
  })

  it.each([
    ["bash task.sh | tee output.log", "other"],
    ["printf safe | bash task.sh", "other"],
    ["printf safe | sh", "unparsed-git"],
    ["git --no-optional-locks status", "read-only-git"],
    ["git apply < x.patch", "unparsed-git"],
    ["git config user.name developer", "unparsed-git"],
    ["git config set color.ui never", "unparsed-git"],
    ["git config --global user.email dev@example.test", "unparsed-git"],
    ["git branch --list topic", "read-only-git"],
    ["git branch -- topic -D", "unparsed-git"],
    ["git checkout --branch=topic main", "unparsed-git"],
    ["git commit -m '--amend'", "plain-commit"],
    ["git status 2>&1", "read-only-git"],
    ["git diff HEAD > x.patch", "read-only-git"],
    ["git rev-parse --show-toplevel 2>/dev/null", "read-only-git"],
    ["git commit -F - <<'EOF'\n$(git push)\nEOF\n", "plain-commit"],
    ["git add src/*.ts", "other"],
    ['echo "$(printf safe)"', "other"],
    ["echo '$(g=gi; ${g}t status)'", "other"],
    ["cat <<'EOF'\n$(g=gi; ${g}t status)\nEOF\n", "other"],
    ["cat <<EOF\n$(printf safe)\nEOF\n", "other"],
    ['eval "$(ssh-agent)"', "other"],
  ])("retains everyday behavior in %s", (command, kind) => {
    expect(classifyCommand(command).kind).toBe(kind)
  })
})
