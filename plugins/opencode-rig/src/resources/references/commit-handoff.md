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
- A plain local commit runs when the user asked for it, a documented workflow step defines it, or a valid local grant covers it. Without a grant, use the runtime shell permission prompt for the one-time **Approve** or **Deny** decision on the exact proposed commit; do not request a conversational **Approve** first. If the user wants persistent authority, offer **Approve always** separately with its scope before recording the grant. A conversational answer does not suppress a host-required permission prompt.
- **Approve always** records `.git/opencode-rig-approval.json` in the common Git directory; it covers plain commits in every worktree of that repository for 30 days or until revoked. The record stores the user's literal `Approve always` confirmation, a creation ID, physical repository binding, and creation and expiry times. Incomplete older records are invalid and require fresh approval; writers to the common Git directory remain trusted because the record is not cryptographically signed.
- The grant never covers push, force operations, history rewriting, destructive operations, or authentication changes.
- Never push. Publication is human-owned.
- After committing, verify the commit contents with `git show --stat` and report the commit ID.
