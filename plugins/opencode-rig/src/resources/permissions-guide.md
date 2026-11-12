# Permission reference

> Ship this file with the package so the approval rules can be read before the plugin is installed, not only after it has already changed a session.

The permission hook never grants authority on its own: it runs after the rules OpenCode has already resolved and can only escalate an `allow` to `ask`; it never turns a configured `ask` or `deny` into `allow`. The ruleset this package ships is a separate thing: ordinary OpenCode configuration that you read before it is merged, with `ask` and `allow` entries the host resolves the same way it resolves your own rules.

## Shipped rules

[`permissions.json`](permissions.json) holds the ruleset that `opencode-rig init` merges into your configuration, that `opencode-rig check` reports on, and that `opencode-rig print-config` prints.

### Rules that stop

| Rule | Effect | Why it is here |
| -- | -- | -- |
| `git push*` | `ask` | Publication is human-owned. Bare `git push` and any refspec both stop. |
| `git reset --hard*` | `ask` | Discards staged and working-tree changes without recovery. |
| `git clean*` | `ask` | Deletes untracked files. The whole class stops, `git clean -n` included. |
| `git rebase*` | `ask` | Rewrites commits that other branches may already contain. |
| `* --output=*` | `ask` | `git log`, `git show`, and `git rev-list` write a file when they are given `--output=FILE`. |
| `* --output *` | `ask` | The same option in the space-separated form. |

The two `--output` rules are the only ones that name no command. They exist because `git log --output=FILE`, `git show --output=FILE`, and `git rev-list --output=FILE` each create that file, and the read-only allowlist below would otherwise match them. They sit at the end of the ruleset so the last matching rule is the one that asks: a read-only inspection command that accepts `--output` now asks for an answer instead of writing silently.

A rule of your own still wins over them, exactly as it wins over every other shipped rule: the shipped rules are inserted ahead of yours in the file you are merged into.

### Rules that allow

| Rule | Effect | Why it is here |
| -- | -- | -- |
| `git rebase --abort` | `allow` | Configuration allows recovery; the hook still asks through its unparsed-Git fallback. |
| `git rebase --continue` | `allow` | Configuration allows recovery after resolving a conflict; the hook still asks. |
| `git rebase --skip` | `allow` | Configuration allows dropping the current patch; the hook still asks. |
| `git rebase --quit` | `allow` | Configuration allows abandoning the rebase state; the hook still asks. |

The four `allow` entries override the blanket `git rebase*` configuration rule for recovery. The hook still asks because these forms are outside its literal Git allowlist. OpenCode resolves the last matching rule, so they must stay after `git rebase*`; the ordering is documented in the [OpenCode v2 permission documentation](https://opencode.ai/v2/docs/permissions/).

A plain local commit is deliberately absent. `git commit` is governed by the guardrail hook instead of a blanket rule, so a recorded grant can take effect; adding `git commit*` here would pin every commit to `ask` and make the grant useless.

## The read-only shell allowlist

Every rule below is `allow`, and every one of them names a command that only reads. They exist because each role agent ships its own `shell: *` catch-all that asks, so without them every inspection command in a role agent stops for an answer.

The list lives in one place. `opencode-rig init` appends the shipped ruleset to each role agent, after that agent's own rules, so the list cannot drift per agent and a command that is not on it still asks. The shipped order is kept, so the two `--output` rules land after the allows they override and an agent resolves `git log --output=FILE` to `ask` as well.

Git inspection:

```text
git show *
git log *
git status *
git blame *
git rev-parse *
git rev-list *
git ls-files *
git ls-tree *
git cat-file *
git describe *
git shortlog *
git show-ref *
git for-each-ref *
git merge-base *
git check-ignore *
git check-attr *
git count-objects *
git worktree list *
git stash list *
git config --get *
git var *
```

File and text inspection:

```text
cat *
head *
tail *
grep *
wc *
cut *
tr *
comm *
cmp *
diff *
nl *
basename *
dirname *
realpath *
which *
type *
stat *
file *
du *
df *
date
ls *
pwd *
jq *
shasum *
sha256sum *
uniq *
echo *
printf *
```

A command whose wildcard also admits a writing or an executing form is not on the list: `git diff` (`--output=FILE` writes a file), `git grep` (`--open-files-in-pager=<command>` executes a command), `git remote -v` (git accepts `-v` before a mutating subcommand), `rg` (`--pre <command>` runs an arbitrary program before the search), `sort` (`-o FILE`), `tree` (`-o DIR`), `sed` and `awk` (`-i`, or a redirection inside the program), `find` (`-exec`, `-delete`), `tee`, `xargs`, `env`, and every interpreter or package manager. The Git families `git branch`, `git tag`, `git stash`, `git config`, and `git remote` are excluded for the same reason: their wildcard also matches their mutating forms. Two read-only forms of those families are on the list because their wildcard cannot reach a mutating form: `git stash list *` and `git config --get *`. `git pull` and `git fetch` write to the repository.

`date` is on the list without a wildcard on purpose: GNU `date -s` sets the system clock on Linux, so only the bare command is allowed and `date -u` still asks.

### Two channels the allowlist does not close

Both come from how the host resolves a rule, not from a defect in this list.

- **A redirection attached to an allowed command still matches.** `cat a > b` matches `cat *`, and the shell performs the write. The list matches a command string, not its effect. Six of the seven role agents declare `edit: *` as `deny`, and that rule does not cover a redirection, so inside those agents a redirection is a write path their own read-only contract does not close.
- **An agent rule is appended after the global rules.** The [OpenCode v2 permission documentation](https://opencode.ai/v2/docs/permissions/) states that OpenCode combines rules in order and uses the last match: lower-priority configuration first, global rules next, agent rules last. An agent `allow` therefore outranks a global `deny` for the same command. That consequence is the documented one; the composition order was not confirmed against the installed host binary, so read it as documented behaviour rather than a verified one. The host's own shipped agents append their permissions the same way, which is why this package does it too. The shipped ruleset contains no `deny` rule, so the list contains no command that a shipped rule denies, and this only matters if you add a global `deny` for a command on the list.

## What the hook escalates

These run regardless of the rules above, because the hook classifies the command itself. They are listed so the shipped file can be reviewed against them rather than trusted on its own.

- **Remote mutation** — `git push`, `git remote add|remove|set-url|prune|update`, `gh pr create|merge|close|reopen|edit|comment|review|ready`, `gh release create|upload|delete|edit`, `gh repo create|delete|edit|rename|archive`, `npm publish`, `uv publish`, `twine upload`, `docker push`, and a `git tag` that is pushed.
- **History rewrite** — `git rebase` outside `--abort|continue|skip|quit`, `git commit --amend`, `git filter-repo`, `git filter-branch`, `git replace`.
- **Destructive local** — `git reset --hard`, a `git clean` carrying `-f`, `-d`, or `-x`, `git branch -D`, `git branch --delete --force`, a forced `git checkout` (`-f` or `--force`), a `git checkout -- <path>` that overwrites the working tree, `git restore` outside `--staged`, `git stash drop|clear`, a forced `git worktree remove`, recursive `rm`, `find -delete`, and `truncate -s 0`.
- **Unreviewed commit** — `git commit` with no recorded grant.

In `standard` mode, any Git form outside the hook's literal command-and-option allowlist asks through the `unparsed-git` fallback, including unreadable shell syntax that names Git or GitHub. Readable guarded operations retain their stronger classifications. In `strict` mode, `git merge` and `git cherry-pick` receive the destructive-local classification; they already ask through the fallback in standard mode.

The `git clean` rule stops the whole class. The hook classifies `git clean` carrying `-f`, `-d`, or `-x` as destructive-local and asks for other forms through its unparsed-Git fallback, so `git clean -n`, `git clean -i`, and bare `git clean` also ask.

With `guardrails` set to `off` the hook stops escalating, and the shipped `ask` rules become the only stops for push, hard reset, clean, rebase, and the `--output` writes. Turning the hook off therefore removes the hook's stops and keeps the configuration's; the shipped rules still ask.

## What still asks

Outside a role agent, OpenCode starts permissive, so most actions default to `allow`. You do not need an allow list for routine work there:

- Reading, globbing, and searching the project.
- Editing project files.
- Shell commands, including supported literal forms of `git status`, `git diff`, and `git log`, test runners, linters, and build tools. Git or GitHub forms outside the hook's literal allowlist still ask.
- `external_directory` is allowed by default, not asked. The [v2 documentation](https://opencode.ai/v2/docs/permissions/) says no default rule asks for it, so a read or an edit outside the Location and worktree proceeds without a prompt. Add `{ "action": "external_directory", "resource": "*", "effect": "ask" }` to your own configuration to be asked. `doom_loop` and `lsp` are not v2 Core permission actions, so a rule naming either of them does nothing.

Inside a role agent the shell is different: each role agent declares `shell: *` as `ask`, so anything outside the allowlist above asks, including test runners, linters, and build tools. The allowlist covers inspection, not execution.

## Plain commits and the grant

`git commit` asks until a valid grant exists. Use the runtime shell permission prompt for one-time consent; do not first ask for a conversational **Approve** answer. The plugin's **Approve always** grant is one JSON file in the repository's common Git directory, outside version control and shared by every worktree, valid for 30 days or until revoked. It is distinct from the host's own saved permission rules.

```text
rig_git_grant action=approve confirm="Approve always" scope_acknowledged=true
```

`confirm` must preserve the user's literal **Approve always** answer after the scope was explained. The record stores that confirmation, a creation ID, physical repository binding, and creation and expiry times. Expired, differently bound, or incomplete older records are invalid and require fresh approval. Writers to the common Git directory remain trusted; the record is not cryptographically signed. The grant covers local commits only — never push, force, destructive operations, or history rewrites — and `rig_git_grant action=revoke` removes it. `rig_git_grant action=status` reports `granted`, `absent`, `invalid`, or `no-git`.

## How `init` merges

`opencode-rig init` writes these rules into one configuration file. Without `--project` it targets the global configuration directory: `~/.config/opencode/opencode.jsonc` when that file exists, otherwise `~/.config/opencode/opencode.json` when that one exists, otherwise it creates `~/.config/opencode/opencode.jsonc`. With `--project` it targets the current directory: `opencode.jsonc` when it exists, otherwise `opencode.json` when it exists, otherwise it creates `opencode.jsonc`. `opencode-rig check` resolves the same file, so the report and the merge always name the same one.

- Rules already present under the same action and resource are left as they are; your effect for that resource wins.
- Rules you wrote yourself are never removed, reordered, or weakened. Shipped rules are inserted ahead of yours, and OpenCode evaluates the last matching rule, so within that file your configuration keeps the final say.
- If you already declare a rule in the `git rebase` family — `git rebase*`, `git rebase --abort`, or any other rule whose command starts with `git rebase` — the four shipped recovery allows are not added. Your rule governs that whole family, so `git rebase --abort` follows your effect rather than the shipped one. The blanket `git rebase*` stop is still added unless you declared that exact rule yourself.
- That guarantee covers the rules in the file being merged. A `deny` you keep in another configuration file is not protected by it: cross-file precedence is your own configuration, and an agent's own rules are appended after all of them.
- Comments outside the `permissions` array, every other key, and the content of every rule survive. Comments inside the `permissions` array are rewritten away, because the array is re-serialized.
- Re-running `init` is a no-op once the rules are present.
- If the file still uses the deprecated singular `permission` key, `init` leaves it untouched and prints the snippet instead, because mixing the two forms makes precedence ambiguous.

`opencode-rig print-config` prints the same snippet without writing anything, for a configuration you would rather edit by hand. `opencode-rig check` reports whether the shipped rules are present and never fails on the report, so a configuration you have reviewed and declined is not a blocker.

`init` rewrites the configuration file in place and keeps no backup of the previous content, so keep your own copy before the first merge. A file you already keep under version control already has one.
