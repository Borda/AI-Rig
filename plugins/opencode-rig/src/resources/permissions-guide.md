# Permission reference

> Ship this file with the package so the approval rules can be read before the plugin is installed, not only after it has already changed a session.

The permission hook runs after the rules OpenCode has already resolved. It escalates dangerous commands and, for positively proven inspection and the designed Git writes, can answer an `ask` with `allow`; it never turns a configured `deny` into `allow`. The ruleset this package ships is a separate thing: ordinary OpenCode configuration that you read before it is merged, with `ask` and `allow` entries the host resolves the same way it resolves your own rules.

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
| `* --pre *` | `ask` | `rg` runs an arbitrary program before the search when it is given `--pre <command>`, and an option may follow another option, so the rule names the option in any position. |
| `* --pre=*` | `ask` | The same option in the equals form. |

The four rules that name no command are the two `--output` rules and the two `* --pre` rules. The `--output` rules exist because `git log --output=FILE`, `git show --output=FILE`, and `git rev-list --output=FILE` each create that file, and the read-only allowlist below would otherwise match them. The `--pre` rules cover the one executing option of the `rg *` allow; they name no command because `rg --passthru --pre <command>` is the same option after another one, and any command line that contains the space-delimited `--pre` token asks, not only `rg`. All four sit after the allows they override, so the last matching rule is the one that asks: a read-only command that accepts `--output` or `--pre` asks instead of writing or executing silently.

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

Every rule below is `allow`, and every one of them names a command whose own arguments only read; the channels below list the effects no rule can see. They exist because each role agent ships its own `shell: *` catch-all that asks, so without them every inspection command in a role agent stops for an answer.

The list lives in one place. `opencode-rig init` appends the shipped ruleset to each role agent, after that agent's own rules, so the list cannot drift per agent and a command that is not on it still asks. The shipped order is kept, so the `rg --pre` and `--output` asks land after the allows they override and an agent resolves `git log --output=FILE` or `rg --pre cat` to `ask` as well.

Git inspection:

```text
git show *
git log *
git status *
git diff *
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
rg *
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

A command whose wildcard also admits a writing or an executing form is not on the list: `git grep` (`--open-files-in-pager=<command>` executes a command), `git remote -v` (git accepts `-v` before a mutating subcommand), `sort` (`-o FILE`), `tree` (`-o DIR`), `sed` and `awk` (`-i`, or a redirection inside the program), `find` (`-exec`, `-delete`), `tee`, `xargs`, `env`, and every interpreter or package manager. The Git families `git branch`, `git tag`, `git stash`, `git config`, and `git remote` are excluded for the same reason: their wildcard also matches their mutating forms. Two read-only forms of those families are on the list because their wildcard cannot reach a mutating form: `git stash list *` and `git config --get *`. `git pull` and `git fetch` write to the repository.

`git diff` and `rg` are on the list with their known writing or executing options closed by a later `ask`: `git diff --output=FILE` is stopped by the `* --output=*` rules, and `rg --pre <command>` and `rg --pre=<command>` are stopped by the `* --pre *` and `* --pre=*` rules before them, in any position. Those two patterns require a single space between `--pre` and its value; a tab-separated spelling is outside the rule and is left to the hook. `git diff --no-index <a> <b>` reads two arbitrary paths and a configured textconv or external driver still runs (see the channels below); the hook asks for the literal driver options but cannot see a driver configured for a path. Neither option reorders past its stop: the last matching rule wins.

`date` is on the list without a wildcard on purpose: GNU `date -s` sets the system clock on Linux, so only the bare command is allowed and `date -u` still asks.

## Workflow execution allows

The rules below are `allow` too, but they are a different class from the inspection list: they run project-defined code, so they can execute whatever the repository's test, lint, or package script contains. They are here because every shipped workflow needs them, they are reviewed in this file before `init` writes anything, and asking again in each project only blocks the same work. They are inserted before the trailing `ask` rules, so no `--output` or `--pre` form becomes quiet through them.

| Rule | Effect | Why it is here |
| -- | -- | -- |
| `pytest *` | `allow` | The review, investigation, and remediation workflows reproduce a reported failure or check a claim with the project's test runner. |
| `python -m pytest *` | `allow` | The same runner through the interpreter. |
| `python3 -m pytest *` | `allow` | The same runner through the Python 3 launcher. |
| `py -m pytest *` | `allow` | The same runner through the Windows launcher. |
| `.venv/bin/python -m pytest *` | `allow` | The same runner in a project virtual environment, the documented form in this repository. |
| `.venv/bin/python3 -m pytest *` | `allow` | The same runner through the versioned interpreter name a virtual environment also provides. |
| `.venv/Scripts/python.exe -m pytest *` | `allow` | The same runner in a native Windows virtual environment. |
| `npm test` | `allow` | The JavaScript test gate detected from `package.json`. |
| `npm run test` | `allow` | The same gate in its `run` spelling. |
| `npm run lint` | `allow` | The JavaScript lint gate detected from `package.json`. |
| `npm run typecheck` | `allow` | The JavaScript typecheck gate detected from `package.json`. |
| `npm run check` | `allow` | An aggregate check script. |
| `make test` | `allow` | The Makefile test gate detected by `rig_gate`. |
| `make lint` | `allow` | The Makefile lint gate detected by `rig_gate`. |
| `make typecheck` | `allow` | The Makefile typecheck gate detected by `rig_gate`. |
| `npm pack --dry-run` | `allow` | The release workflow's package check; `npm pack` without `--dry-run` still asks. |

> Acceptance: these entries can run project code. Read the list before installing; if a project must not run code from its own checkout, remove the entries you do not want. They never cover `git push`, a Git write, a history rewrite, a destructive operation, a package publication, or a form outside the listed prefix. An allowed script can also write: a test target may emit coverage or caches, and a project that defines `test` to format will format. Formatter, build and pre-commit-hook grants are deliberately absent because their normal job is to rewrite the working tree.

> The `pytest` grants are hook-bounded: the rule names a prefix and the hook checks the arguments. With `guardrails: off` the hook does not run, so `pytest --basetemp=<dir>` (pytest deletes that directory) and `pytest -p` or `-c` (load a plugin or configuration) are no longer bounded. Keep the hook on for these forms; the `exact` rules (`npm test`, `npm run <script>`, `make <target>`, `npm pack --dry-run`) have no tail to bound.

`npx <tool>` can download and then run a package, so the workflows use `npm test`, `npm run <script>`, `make <target>`, or the project interpreter instead. `.venv/bin/python *` (any script) is broader than the gate needs and stays asking; only the `-m pytest` form is listed. Raw `.venv/bin/ruff`, `mypy`, `uv run <tool>`, and `npm --prefix <dir> run <script>` stay asking in a role agent: their option tails cannot be bounded by a rule, and the workflows use `make lint`, `make typecheck`, `npm run <script>` after a `cd`, or the gate instead. `npm run format`, `make format`, `npm run build`, and `pre-commit run --files` are not granted to a role agent at all; they run through `rig_gate` in the parent, where the gate approval covers them.

### Hook-bound inspection allow (default on)

The option `inspection: false` turns off one hook allow; the default is `true`. It is the one path where the hook can answer a configured `ask` with `allow`: every role agent declares `shell: *` → `ask`, and this allow replaces that catch-all for commands the hook positively proves are inspection or a configured verification form. A configured `deny` is final; `guardrails: off` disables the allow with the rest of the hook.

- **Positive model, not a denylist.** The command must be inside the shell grammar and its program must be in a shipped positive set: the read-only readers (`cat`, `head`, `tail`, `grep`, `sed`, `sort`, `find`, `ls`, `wc`, `cut`, `tr`, `comm`, `cmp`, `diff`, `nl`, `basename`, `dirname`, `realpath`, `stat`, `file`, `du`, `df`, `date`, `pwd`, `jq`, `shasum`, `sha256sum`, `uniq`, `echo`, `printf`, `rg`, and the additions below), a read-only Git verb, a read-only `gh` view, or the verification family (`pytest`, an environment-prefixed `python -m pytest`, `uv run pytest|ruff|mypy`, `npm test`, `npm run test|lint|typecheck|check`, `make test|lint|typecheck`). A program outside the set asks.
- **Structural write and exec refusal.** The grammar exposes two signals on every classification: `writes` for an output redirect, a write-valued option (`sort -o`, `sort --output=`, `uniq` output, `find -fls/-fprintf/-fprint/-fprint0`, a Git write verb) and `executes` for a shell, script interpreter or exec channel (`find -exec/-ok/-execdir`, `grep --open-files-in-pager`, `rg --pre`). Either signal makes the allow refuse. `git add`, `sort -o`, `sed -i`, `bash script.sh` and `python script.py` therefore stay prompts even though some of them classify as `other`.
- **Grammar gaps this closes.** `2>/dev/null` (and `/dev/stdout`, `/dev/stderr`) is a supported sink; a word made only of `=` is data in a data command (`echo ====`, `grep ==== file`); `uv run pytest|ruff|mypy` resolves to the inner tool; an environment-prefixed pytest was already grammar-quiet and now reaches the allow. `/dev/tty`, a general `=`-prefixed word and `uv run python script.py` still ask.
- **Every fragment is proven.** A compound is split into top-level fragments and every fragment must pass on its own; a literal `cd <absolute path>` sets the effective directory, and a relative `cd` asks. A path-qualified program is refused because the bytes at that path are not the named program; `.venv/bin/python -m pytest` is the sole documented exception.
- **Every resource must pass.** One failing resource in a multi-command event leaves the whole event to the rules. The allow applies to `shell` only; an `execute` event keeps the existing classification.
- **The hook has no agent identity.** It cannot tell a role agent from the parent session, so the allow applies to any `shell` event. In the parent session the rules usually already allow these forms, so the visible change is inside a role agent; if you configured an `ask` for one of these forms in the parent, this allow answers it too.
- **Stateless.** The decision is a pure function of the command text and the shipped tables: no approval file, no run, no session, no lock, no ordering. Two parallel reviews and nested subagents resolve independently.
- **A verification command runs code.** The allow proves the command shape, not the code it runs: a project's `conftest.py`, test dependency or build hook can do anything the agent's account can do. A fetched pull-request worktree is third-party code; the review skill routes it to `rig_sandbox` or asks for the one per-commit trust answer. The sandbox is the boundary, not the command name.
- **Safe readers are broad, writers are not.** Besides the original readers, the set covers `egrep`/`fgrep`/`rgrep`, the checksum tools, `rev`/`tac`/`fold`/`fmt`/`expand`/`unexpand`/`column`/`paste`/`join`/`tsort`, `od`/`hexdump`/`strings`/`base64`/`xxd` read form/`zcat`, and `git config --get`. Pagers (`less`, `more`, `bat`), `tree` (`-o`), `gunzip`/`xz`/`bzip2` without a stdout guarantee, archive extraction, `awk`, and `sed -i` remain outside the positive set.
- **Host paths and runtime values are refused.** A reader fragment with an absolute or home operand (`cat /etc/passwd`, `cat ~/.ssh/id_rsa`) or a runtime value (`$VAR`, a backtick) asks; inspection is for paths inside the reviewed tree.

### Hook-bound designed Git writes (default on)

`designedWrites: false` turns off a second hook allow; the default is `true`. These are the Git writes the workflows perform by design, so they are not read-only and are not covered by the inspection allow. Each is a whole simple command in one literal form, independent of the `guardrails` mode (a host `deny` stays final):

- **Review fetch.** Exactly `git fetch --no-tags --no-write-fetch-head --no-auto-maintenance <url> refs/pull/<n>/head:refs/rig-review/pr-<n>-<sha12>`, with no leading `+`, a literal URL and matching `<n>`. This is the sole fetch the review performs. Any other fetch keeps its normal classification and asks.
- **Review worktree.** `git worktree add --detach <path> <sha40>`, `git worktree remove [--force] <path>` and `git worktree prune`, where `<path>` is an absolute path under the host temp root's `opencode/review-*` and the remove path is deliberately allowed ahead of the destructive class. A worktree anywhere else keeps the destructive stop. `git worktree add -b <branch>` is not admitted.
- **Owned staging.** `git add <literal relative paths>`, with no option, no `-A`/`--all`, no `.`/`..` segment, no glob, no absolute or parent path, no pathspec magic and no shell expansion. Staging stays inside the project root.
- **Still asks:** every other Git write, a compound command, a quoted operand, a substitution, a path outside the two roots, and a commit without a grant. The allow proves the command shape, not the content staged or fetched.

### Three channels the allowlist does not close

They come from how the host resolves a rule, or from a driver the command text does not name, not from a defect in this list.

- **A redirection attached to an allowed command still matches.** `cat a > b` matches `cat *`, and the shell performs the write. The list matches a command string, not its effect. Six of the eight role agents declare `edit: *` as `deny`, and that rule does not cover a redirection, so inside those agents a redirection is a write path their own read-only contract does not close.
- **A configured diff driver still runs on a diff.** `git diff`, `git log -p`, and `git show` run a `textconv` or external diff driver when the user's Git config defines one for a path. The driver name is in `.gitattributes`, not in the command text, so no shell rule can name it. The hook asks for any Git option it does not model, `--ext-diff` and `--textconv` included, yet a driver configured for a path still runs; review only repositories whose attributes and Git configuration you trust.
- **An abbreviated option spelling reaches the same effect.** Git accepts an unambiguous long-option prefix, so `git status --porc` runs `--porcelain` and an abbreviation of a writing option reaches the same write while no shipped rule names it. The hook asks for an option it does not know, so the write is stopped while guardrails are on; with `guardrails: off` only the shipped rules apply and the abbreviation is not named. Enumerating every abbreviation is the spelling list this package deliberately avoids.
- **An agent rule is appended after the global rules.** The [OpenCode v2 permission documentation](https://opencode.ai/v2/docs/permissions/) states that OpenCode combines rules in order and uses the last match: lower-priority configuration first, global rules next, agent rules last. An agent `allow` therefore outranks a global `deny` for the same command. That consequence is the documented one; the composition order was not confirmed against the installed host binary, so read it as documented behaviour rather than a verified one. The host's own shipped agents append their permissions the same way, which is why this package does it too. The shipped ruleset contains no `deny` rule, so the list contains no command that a shipped rule denies, and this only matters if you add a global `deny` for a command on the list.

### Reading another checkout

`cd <directory> && <command>` is the quiet form: the host checks the `cd` target as its own resource through `external_directory`, allowed by default, and checks the following command against the rules above. `git -C <directory> <command>` asks instead — the hook cannot prove where `-C` points, so it classifies the whole invocation as unparsed Git. Both forms leave a Git write behind the rules; neither is a sandbox. A `cd` into a fetched PR worktree runs that worktree's code; the review skill names this trust boundary before any gate runs there.

### Permissive role access (opt-in)

The default above keeps every role agent at `shell: *` → `ask` plus the shipped lists. If you would rather have the six read-only agents inherit the host's permissive default and let the hook be their only shell bound, remove the `shell: *` rule from `~/.config/opencode/agents/rig-*.md` (or the project copies) and keep the rest of the shipped rules. That posture is **hook-bounded, not sandboxed**: it removes the per-read prompt but lets an agent run any non-Git command the hook does not stop. `opencode-rig init` renders the default again unless you re-apply the edit, so treat it as a deliberate local choice rather than a package option.

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

Inside a role agent the shell is different: each role agent declares `shell: *` as `ask`, so a command outside the new inspection allow asks. That includes `tree`, `awk`, `tee`, `xargs`, `npx <tool>`, a script (`python script.py`), `git -C`, a make target outside the listed set, and any command with an absolute/home operand or a runtime value. The allow covers per-fragment inspection and the standard verification forms (including `npm test -- <script args>` and `uv run python -m pytest`), not arbitrary execution.

Two hook forms also stop asking with this release, because every workflow was tripping on them: a project tool run after a `cd` outside the project now stays quiet when its operands are plain relative paths (`cd <worktree> && pytest tests/x.py -q`), and the canonical gate commands are modelled directly — `make <test|lint|typecheck>`, `pre-commit run --files <paths>`, `npm pack --dry-run`, and `node --check <script>`. An absolute operand, a `..` segment, an unlisted make target, `pre-commit run --files` in a role agent, or `npm pack` without `--dry-run` still asks.

### Shipped command blocks

`blueprint` (default on) is a second hook allow that proves exact text rather than command shape. The plugin ships `src/resources/blueprint-manifest.json`, built from the fenced `bash`/`sh` blocks in its shipped skills, roles and references. A block is eligible only when it is literal at run time; a block with an expansion, redirect, heredoc, unterminated quote, line continuation or non-ASCII whitespace is recorded as skipped. A command is allowed only when every top-level fragment (`;`, `&&`, `||`, `|`, newline) matches a `kind: "command"` entry. The whole-block digest is audit only. A tampered manifest can add a read-only entry that the danger re-check cannot see; that is the stated limit. The allow runs before the `guardrails: off` return, but the always-ask classes and a configured `deny` still win, and `blueprint: false` disables it.

### Forked pull request trust

`rig_trust` is a custom tool action, not a shell command. A forked pull request's verification runs on the host only after the user's literal **Trust this PR** answer; the tool records that answer for one commit and one review worktree for 24 hours. A different commit or a re-created worktree asks again, and the record expires. A record missing the words, ref, worktree, or timestamps is ignored. The shipped rule below asks for that decision; its action is `rig_trust`, not shell.

`rig_sandbox` is a custom tool action with no prompt rule of its own, like `rig_script`: install acceptance covers it, and the container is the boundary. It accepts the project root or a review worktree, copies it without `.git` or credential-shaped files (`.env*`, `*.pem`, `*.key`, `id_rsa`-family, `.ssh`, `.aws`, `.npmrc`, `.pypirc`), and records an unavailable receipt when no runtime exists. Its image must be digest-pinned. Inspection is allowed in any directory. A verification command (`pytest`, `uv run`, `npm test`, `make test`) is allowed by the hook only when its effective directory is the project root, an own worktree whose HEAD is an ancestor of your HEAD, or a worktree with a matching, unexpired trust record. A literal `cd <absolute path>` sets the effective directory for the verification fragments after it; a verification in any other directory falls through to the role's shell rule and asks. Under `execution: "all-sandbox"` a host verification asks even in the project root and must run through `rig_sandbox`. `rig_sandbox` is the container alternative: it copies the worktree, runs a pinned image with the network off by default, and records the receipt.

| Rule | Effect | Why it is here |
| -- | -- | -- |
| `*` | `ask` | The `rig_trust` tool's per-commit decision for a forked PR's host verification (`action: "rig_trust"`). |

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
