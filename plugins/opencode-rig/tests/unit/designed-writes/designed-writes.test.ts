/** Designed Git writes: the review fetch/worktree forms and owned-path staging. */
import { describe, expect, it } from "vitest"
import { designedWriteSafe } from "../../../src/designed-writes.ts"

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
    "git add src/a.ts tests/b.ts",
    "git add -- src/a.ts",
    "git add src/a..b.ts",
  ])("accepts %s", (command) => {
    expect(designedWriteSafe(command, CONTEXT)).toEqual({ ok: true })
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
