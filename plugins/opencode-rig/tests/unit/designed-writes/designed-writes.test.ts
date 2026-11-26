/** Designed Git writes: the review fetch/worktree forms and owned-path staging. */
import { describe, expect, it } from "vitest"
import { designedWriteSafe } from "../../../src/designed-writes.ts"
import { Project } from "../../helpers/harness.ts"

const CONTEXT = { projectRoot: "/repo", tempRoot: "/tmp" }
const SHA = "0123456789abcdef0123456789abcdef01234567"
const FETCH =
  "git fetch --no-tags --no-write-fetch-head --no-auto-maintenance https://example.invalid/o/r.git refs/pull/563/head:refs/rig-review/pr-563-0123456789ab"

describe("designed Git writes", () => {
  it.each([
    FETCH,
    `git worktree add --detach /tmp/opencode/review-pr563 ${SHA}`,
    "git worktree remove /tmp/opencode/review-pr563",
    "git worktree remove --force /tmp/opencode/review-other",
  ])("accepts %s", (command) => {
    expect(designedWriteSafe(command, CONTEXT)).toEqual({ ok: true })
  })

  it.each(["git add src/a.ts tests/b.ts", "git add -- src/a.ts", "git add src/a..b.ts", "git add src"])(
    "accepts ordinary paths within a repository: %s",
    (command) => {
      const project = Project.withCommit({ "src/a.ts": "a", "src/a..b.ts": "b", "tests/b.ts": "c" })
      expect(designedWriteSafe(command, { projectRoot: project.root })).toEqual({ ok: true })
    },
  )

  it.each([
    "src/.env",
    "src/nested/.env.local",
    "src/nested/.ssh/config",
    "src/nested/.aws/config",
    "src/nested/.docker/config.json",
    "src/nested/.npmrc",
    "src/nested/.pypirc",
    "src/nested/.netrc",
    "src/nested/.envrc",
    "src/nested/tls.key",
    "src/nested/client.PEM",
    "src/nested/credentials.json",
    "src/nested/id_ed25519",
  ])("refuses to stage a directory containing %s", (path) => {
    const project = Project.withCommit()
    project.write(path, "placeholder\n")
    expect(designedWriteSafe("git add -- src", { projectRoot: project.root }).ok).toBe(false)
  })

  it("allows an ignored untracked file but refuses an already tracked credential", () => {
    const project = Project.withCommit({ "src/a.ts": "a", "src/.env.tracked": "old" })
    project.write(".gitignore", ".env*\n")
    project.write("src/.env.tracked", "changed")
    project.write("src/nested/.env.ignored", "ignored")
    project.write("src/nested/a.ts", "a")
    const context = { projectRoot: project.root }
    expect(designedWriteSafe("git add src/nested", context).ok).toBe(true)
    expect(designedWriteSafe("git add src", context).ok).toBe(false)
  })

  it.each([
    // Not the exact fetch form.
    "git fetch https://example.invalid/o/r.git main",
    "git fetch --no-tags https://example.invalid/o/r.git refs/pull/563/head:refs/rig-review/pr-563-0123456789ab",
    "git fetch --no-tags --no-write-fetch-head --no-auto-maintenance https://example.invalid/o/r.git +refs/pull/563/head:refs/rig-review/pr-563-0123456789ab",
    "git fetch --no-tags --no-write-fetch-head --no-auto-maintenance https://example.invalid/o/r.git refs/pull/563/head:refs/rig-review/pr-564-0123456789ab",
    "git fetch --no-tags --no-write-fetch-head --no-auto-maintenance /etc/passwd refs/pull/563/head:refs/rig-review/pr-563-0123456789ab",
    "git fetch --no-tags --no-write-fetch-head --no-auto-maintenance file:///etc/passwd refs/pull/563/head:refs/rig-review/pr-563-0123456789ab",
    // Worktree outside the review temp root.
    `git worktree add --detach /repo/review-x ${SHA}`,
    `git worktree add --detach /tmp/opencode/other ${SHA}`,
    "git worktree remove /tmp/evil",
    "git worktree remove /tmp/opencode/other",
    `git worktree add --detach /tmp/opencode/review-a/b ${SHA}`,
    "git worktree remove",
    // Staging forms the plan rejects.
    "git add -A",
    "git add --all",
    "git add .",
    "git add ..",
    "git add ../x",
    "git add /etc/passwd",
    "git add src/*.ts",
    "git add ~/.ssh/id_rsa",
    "git add .ssh/id_rsa",
    "git add .env",
    "git add .aws/credentials",
    "git add cert.pem",
    "git add id_ed25519",
    "git add ../x",
    "git add src/../x",
    "git add :(glob)src",
    "git add",
    // Shell composition and expansion.
    "git add src/a.ts && rm -rf x",
    "git add src/a.ts; git push",
    'git add "src/a b.ts"',
    "git add $(echo src/a.ts)",
    // Other writes.
    "git commit -m x",
    "git fetch origin",
  ])("rejects %s", (command) => {
    expect(designedWriteSafe(command, CONTEXT).ok, command).toBe(false)
  })
})
