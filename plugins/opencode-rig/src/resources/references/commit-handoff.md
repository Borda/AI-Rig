# Commit handoff

Propose the commit before running it.

## Message

```text
<area>: <imperative summary>

<Why the change exists and what behavior changed.>

- <Concrete change with its user-visible impact>
- <Gate or verification that proves it>

Verification: <commands and results>
Limits: <what was not checked>
```

## Rules

- Stage exact paths, not `git add -A`.
- Preserve unrelated changes; if the tree mixes concerns, commit only owned paths.
- A plain local commit runs when the user asked for it, a documented workflow step defines it, or a valid local grant covers it. Otherwise ask once with **Approve**, **Approve always**, and **Deny**.
- **Approve always** records `.git/opencode-rig-approval.json` in the common Git directory; it covers plain commits in every worktree of that repository until revoked.
- The grant never covers push, force operations, history rewriting, destructive operations, or authentication changes.
- Never push. Publication is human-owned.
- After committing, verify the commit contents with `git show --stat` and report the commit ID.
