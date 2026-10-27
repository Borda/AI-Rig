import { describe, expect, it } from "vitest"
import { classifyCommand, decide } from "../../src/guardrails.ts"
import type { GrantState } from "../../src/git-grant.ts"

const GRANTED: GrantState = { status: "granted", path: "/repo/.git/opencode-rig-approval.json" }
const ABSENT: GrantState = { status: "absent" }
const NO_GIT: GrantState = { status: "no-git" }

describe("classifyCommand", () => {
  it.each([
    ["git push origin main", "remote-mutation"],
    ["git push --force origin main", "remote-mutation"],
    ["gh pr create --fill", "remote-mutation"],
    ["npm publish", "remote-mutation"],
    ["docker push image:tag", "remote-mutation"],
  ])("classifies %s as %s", (command, kind) => {
    expect(classifyCommand(command).kind).toBe(kind)
  })

  it.each([
    ["git reset --hard HEAD~1", "destructive-local"],
    ["git clean -fd", "destructive-local"],
    ["git restore src/a.ts", "destructive-local"],
    ["rm -rf build", "destructive-local"],
  ])("classifies %s as %s", (command, kind) => {
    expect(classifyCommand(command).kind).toBe(kind)
  })

  it.each([
    ["git rebase -i HEAD~3", "history-rewrite"],
    ["git commit --amend --no-edit", "history-rewrite"],
    ["git filter-repo --path src", "history-rewrite"],
  ])("classifies %s as %s", (command, kind) => {
    expect(classifyCommand(command).kind).toBe(kind)
  })

  it("classifies a plain local commit", () => {
    expect(classifyCommand("git commit -m 'feat: add gate runner'").kind).toBe("plain-commit")
  })

  it("classifies read-only Git", () => {
    expect(classifyCommand("git status --short").kind).toBe("read-only-git")
    expect(classifyCommand("git diff HEAD~1").kind).toBe("read-only-git")
    expect(classifyCommand("git log --oneline -5").kind).toBe("read-only-git")
  })

  it("lets the most severe segment win", () => {
    expect(classifyCommand("git status && git push").kind).toBe("remote-mutation")
    expect(classifyCommand("git commit -m x; git reset --hard").kind).toBe("destructive-local")
  })

  it("ignores environment assignments and sudo prefixes", () => {
    expect(classifyCommand("GIT_AUTHOR_NAME=x git commit -m msg").kind).toBe("plain-commit")
    expect(classifyCommand("FF=1 sudo git push").kind).toBe("remote-mutation")
  })

  it("treats merge and cherry-pick as guarded only in strict mode", () => {
    expect(classifyCommand("git merge feature").kind).toBe("other")
    expect(classifyCommand("git merge feature", "strict").kind).toBe("destructive-local")
    expect(classifyCommand("git cherry-pick abc123", "strict").kind).toBe("destructive-local")
  })

  it("leaves unrelated commands unclassified", () => {
    expect(classifyCommand("npm run test").kind).toBe("other")
    expect(classifyCommand("ls -la").kind).toBe("other")
  })
})

describe("decide", () => {
  it("always escalates remote mutation, even with a grant", () => {
    const decision = decide(classifyCommand("git push"), "standard", GRANTED)
    expect(decision?.effect).toBe("ask")
    expect(decision?.message).toMatch(/Publication stays human-owned/)
  })

  it("always escalates destructive and history-rewriting commands", () => {
    expect(decide(classifyCommand("git reset --hard"), "standard", GRANTED)?.effect).toBe("ask")
    expect(decide(classifyCommand("git commit --amend"), "standard", GRANTED)?.effect).toBe("ask")
  })

  it("escalates a plain commit without a grant", () => {
    const decision = decide(classifyCommand("git commit -m x"), "standard", ABSENT)
    expect(decision?.effect).toBe("ask")
    expect(decision?.message).toMatch(/no local Git grant/)
  })

  it("leaves the configured effect alone for a granted plain commit", () => {
    expect(decide(classifyCommand("git commit -m x"), "standard", GRANTED)).toBeUndefined()
  })

  it("does not treat an invalid or absent Git directory as a grant", () => {
    expect(decide(classifyCommand("git commit -m x"), "standard", NO_GIT)?.effect).toBe("ask")
    expect(decide(classifyCommand("git commit -m x"), "standard", { status: "invalid" })?.effect).toBe("ask")
  })

  it("never escalates read-only or unrelated commands", () => {
    expect(decide(classifyCommand("git status"), "standard", ABSENT)).toBeUndefined()
    expect(decide(classifyCommand("npm test"), "standard", ABSENT)).toBeUndefined()
  })

  it("does nothing in off mode", () => {
    expect(decide(classifyCommand("git push"), "off", ABSENT)).toBeUndefined()
  })

  it("escalates integration commands in strict mode", () => {
    expect(decide(classifyCommand("git merge feature", "strict"), "strict", GRANTED)?.effect).toBe("ask")
  })
})
