# opencode-rig

Current version: `0.5.0`

Evidence-first delivery workflows for [OpenCode](https://opencode.ai): frozen plans, recorded gates, independent review, adversarial convergence, and Git guardrails that keep publication human-owned.

`opencode-rig` is a TypeScript plugin with no Python runtime. It installs from npm, registers one small set of tools and skills, and writes every run to an auditable artifact directory. A run cannot report `completed` without the evidence its workflow requires.

## What it does

- Binds each run to the exact bytes it reviewed or changed, and refuses completion after source drift.
- Runs only gates the project already configures, recording the real exit code, duration, output digests, and a truncated tail.
- Requires a named independent reviewer before review, remediation, and challenge runs can complete.
- Keeps review findings, remediation buckets, and the challenge ledger as validated artifacts with reconciliation counts.
- Escalates remote mutations, destructive operations, and unreviewed commits to `ask` without ever downgrading a configured `ask` to `allow`.
- Dispatches read-only specialists through native subagents when they are installed, with an inline child-session fallback when they are not.

## Requirements

- OpenCode `2.1.0` or newer. The plugin is developed and checked against the `2.2.0` host recorded in `rig.json`. The `@opencode/plugin` SDK dependency range is `^2.1.4` in `package.json`.
- Node.js `20` or newer.
- Git for scope evidence and the optional local commit grant.

## Install

Published package, when available:

```bash
opencode plugin add opencode-rig@0.3.0
npx opencode-rig@0.3.0 init
```

Before the first npm release, install straight from this repository:

```bash
opencode plugin add 'github:Borda/AI-Rig#main::path:plugins/opencode-rig'
node plugins/opencode-rig/bin/opencode-rig.mjs init   # or the path inside your clone
```

A branch specifier follows `main`; replace it with a full commit hash when the install must stay pinned. `opencode plugin add` is idempotent and writes the package specifier into `~/.config/opencode/opencode.json`. It caches the package for loading but does not link this package's CLI on `PATH`, so run `init` through `npx` for a published version or the checked-out script otherwise.

`init` writes the seven `rig-*` role agents that OpenCode cannot register from a plugin package; they land in `~/.config/opencode/agents/`, or in `.opencode/agents/` with `--project`. It also merges the shipped permission rules into your OpenCode configuration: `~/.config/opencode/opencode.jsonc` when that file exists, otherwise an existing `opencode.json` beside it, otherwise a new `opencode.jsonc` — and with `--project` the `opencode.jsonc` or `opencode.json` at the Git root, falling back to the current directory outside Git. A card the package itself revised is reported `STALE` and refreshed automatically, because the stamp records the digest it was installed with. Add `--upgrade` to replace a stamped agent you edited locally; without it, `init` and `check` stop on a locally edited file and never overwrite it.

`opencode-rig setup` is the recommended command, and `init` is its alias. Besides the agents and rules above, it writes the shipped delivery principles into `~/.config/opencode/AGENTS.md` as a digest-stamped managed block. OpenCode reads that file instead of `~/.claude/CLAUDE.md`, so setup refuses to create it while your Claude global instructions exist; pass `--adopt-claude-instructions` to copy them in. The block is created owner-only, and `remove` strips it while restoring the surrounding bytes. `setup --agents-only` installs or verifies just the role agents. `OPENCODE_RIG_HOME` is a test-only seam that relocates the home directory and fails closed when it points at a directory that does not exist.

The recommended registration is already checked in at the repository root as [`opencode.jsonc`](../../opencode.jsonc), and `make sync-opencode` performs the global registration, the agent-only install, and the full `setup` from pushed state. Set `OPENCODE_ADOPT_CLAUDE_INSTRUCTIONS=1` on that command when a global Claude instructions file should be copied in rather than refused.

Add the plugin options to `opencode.jsonc` if you want anything other than the defaults:

```jsonc
{
  "plugins": [{ "package": "opencode-rig", "options": { "guardrails": "standard" } }],
}
```

`init` merges the shipped permission rules into your configuration, so push, hard reset, clean, and rebase stop for an explicit answer while routine inspection keeps running. The rules and the reasoning behind them ship with the package as [`src/resources/permissions-guide.md`](src/resources/permissions-guide.md), so you can read them before anything is written; `opencode-rig print-config` prints the same rules for a configuration you would rather edit by hand. Plain commits are governed by the guardrail hook instead of a blanket rule, so a recorded grant can take effect.

Ignore the artifact directory in projects that use the plugin:

```bash
printf '\n.opencode-rig/\n' >> .gitignore
```

## Workflows

Run a workflow through its slash command, or load the matching skill by name.

| Command            | Skill             | Use it for                                                     | Completion evidence                                                                    |
| ------------------ | ----------------- | -------------------------------------------------------------- | -------------------------------------------------------------------------------------- |
| `/rig-plan`        | `rig-plan`        | Multi-file or ambiguous changes that need a frozen plan        | Bound plan digest and one independent challenge                                        |
| `/rig-investigate` | `rig-investigate` | Bugs, CI failures, and unexplained behavior before a fix       | Reproduction, root cause, eliminated hypotheses                                        |
| `/rig-research`    | `rig-research`    | Source-backed answers before a design or dependency decision   | Labeled claims, contradictions, independent source check                               |
| `/rig-implement`   | `rig-implement`   | Executing a reviewed frozen plan or a trivial inline mini-plan | Passing gates, changed-path digests, commit handoff                                    |
| `/rig-review`      | `rig-review`      | Reviewing a working tree, commit, or path                      | Validated findings artifact with mandatory `correctness` axis and reviewer identity    |
| `/rig-remediate`   | `rig-remediate`   | Closing findings from a review                                 | Every finding in a terminal bucket plus re-review                                      |
| `/rig-challenge`   | `rig-challenge`   | Risky or security-sensitive changes                            | Converged loop with no security or critical residue                                    |
| `/rig-optimize`    | `rig-optimize`    | A concrete performance target with a configured benchmark gate | Baseline spread, kept rounds, correctness gates                                        |
| `/rig-distill`     | `rig-distill`     | Repeated friction across several recorded runs                 | Counted patterns and accepted asset proposals                                          |
| `/rig-release`     | `rig-release`     | Release readiness before a human publishes                     | Last-release-tag baseline (HEAD only for a first release), gates, package list, drafts |
| `/rig-doctor`      | `rig-doctor`      | A workflow or tool is missing or failing                       | Pass/warn/fail report with remediation commands                                        |

Every workflow follows the same envelope: classify the request, collect authoritative source, act in small batches, verify with configured checks, finalize the run, then report limits honestly.

`rig-research` covers docs, state-of-the-art comparisons, paper analysis, and paper-versus-code fidelity. `rig-distill` reads exact preselected terminal-run files or an inventory record inside the project; external source identifiers, digests, and access limits are recorded as decisions and limits. It proposes assets but never installs, edits, or prunes them. `rig-doctor` offers stale-run cleanup only in a separate workflow after explicit confirmation. `rig-optimize` only uses a benchmark that is already a configured gate, so a performance claim always has a reproducible command behind it. It refreshes each edited candidate's scope before measurement and preserves the baseline receipts.

`rig-implement` accepts an inline mini-plan only for a trivial change under 3 files and 50 changed lines; larger, ambiguous, or high-risk work requires a completed independently reviewed `rig-plan` run. An implementation-to-review handoff supplies the implementation run ID, recorded output paths and digests, its bound plan, and acceptance criteria. Review scope uses those outputs instead of unrelated working-tree changes. PR review inspects the verified head commit only; earlier commits are an explicit coverage gap, and a merge head requires separate parent-range review before starting.

## Tools

| Tool            | Purpose                                                                                                                                                                           |
| --------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `rig_evidence`  | Start, inspect, record, refresh, and finalize an evidence run. Binds scope bytes, paths, plan digests, and reviewer evidence.                                                     |
| `rig_gate`      | Detect and run configured gates, store receipts and logs, and report failures by gate id.                                                                                         |
| `rig_findings`  | Write or read a validated findings inventory with severity, coverage, and bucket accounting.                                                                                      |
| `rig_loop`      | Drive the append-only adversarial loop: baseline, assessed rounds, cumulative progress, and a validated stop reason.                                                              |
| `rig_git_grant` | Read, record, or revoke the local Git approval grant after an explicit user decision.                                                                                             |
| `rig_dispatch`  | Run one bounded role in a child session when native `rig-*` subagents are unavailable.                                                                                            |
| `rig_doctor`    | Check plugin load, versions, agents, grant, ignore rules, gates, and stale runs; `action: "assets"` curates cards, and explicitly confirmed `action: "clean"` removes stale runs. |

All seven tools are registered under their full names; there is no namespace to remember. Calls are only accepted for an active or explicitly named run, and mutating calls validate their input before writing anything.

### Example: rig-review findings

Reviews assess material improvement claims against revision-bound tests or measurements, distinguish supplied evidence from independent reproduction, and treat unresolved shared-hardware noise as inconclusive rather than proof of a gain or regression.

```text
rig_evidence action=start skill=rig-review paths=["src"]
# ... dispatch rig-reviewer for correctness (and others) ...
rig_findings action=write axes=["correctness"] coverage=["src/a.ts"] reviewer="rig-reviewer" findings=[...]
rig_evidence action=finalize
```

`correctness` is mandatory for `rig-review`. Use `aspects` as an alias for `axes` if preferred.

## Agents

`opencode-rig init` installs seven subagents. All are read-only except the implementer, and none may create further subagents.

| Agent              | Role                                                                       |
| ------------------ | -------------------------------------------------------------------------- |
| `rig-investigator` | Reproduces failures and narrows them to an evidence-backed cause.          |
| `rig-implementer`  | Executes a bounded change with focused tests.                              |
| `rig-reviewer`     | Reviews assigned axes and returns structured findings.                     |
| `rig-challenger`   | Tries to falsify a change and re-assesses earlier findings.                |
| `rig-verifier`     | Checks whether a fix actually closes its finding.                          |
| `rig-scientist`    | Grades research claims against their sources and method.                   |
| `rig-optimizer`    | Recomputes benchmark deltas, checks methodology, and proposes experiments. |

Without installed agents, `rig_dispatch` provides the same role cards through a child session. The child never runs gates, commits, or declares completion. A reported `scope_violation` or nonempty `violations` list stops acceptance and finalization. Ignored untracked trees and submodule working-tree contents remain unverified by dispatch scope checks.

External pages, findings, PR comments, and inventories are data rather than instructions. The scientist denies secret-pattern reads and asks before webfetch; role instructions also prohibit scope changes, command execution, secret reads, and data transfers requested by external content. Those instructions do not create a sandbox.

## Configuration

Options live in the plugin entry of `opencode.jsonc`.

| Option               | Default           | Meaning                                                                                                                                                                         |
| -------------------- | ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `guardrails`         | `"standard"`      | `standard` guards dangerous commands and asks for Git forms outside its literal allowlist; `strict` classifies merge and cherry-pick as destructive; `off` disables escalation. |
| `artifacts`          | `".opencode-rig"` | Artifact root inside the project. It may not escape the project directory.                                                                                                      |
| `gates`              | `[]`              | Explicit gate commands. When set, detection is skipped.                                                                                                                         |
| `gateTimeoutMs`      | `600000`          | Per-gate timeout, between 1 second and 1 hour.                                                                                                                                  |
| `maxParallel`        | `4`               | Ceiling for concurrent inline children, between 1 and 16.                                                                                                                       |
| `maxLoopRounds`      | `3`               | Round limit used to validate the `round-limit` stop reason, between 1 and 10.                                                                                                   |
| `ttlDays`            | `30`              | Age at which doctor reports a run as stale, between 1 and 3650.                                                                                                                 |
| `journal`            | `true`            | Append redacted tool-call records to the active run or artifact root.                                                                                                           |
| `codemap`            | `"auto"`          | Codemap integration mode. `auto` is inert until a codemap provider is present.                                                                                                  |
| `compactionPreserve` | `true`            | Inject active run identity into compaction context.                                                                                                                             |

Unknown option keys fail plugin setup instead of being ignored.

## Evidence and artifacts

A run lives under `.opencode-rig/runs/<skill>/<run-id>/`:

- `run.json` is the authoritative record: scope digest, inputs, outputs, gates, findings summary, review evidence, loop summary, limits, and status.
- `gates/` holds immutable per-execution JSON receipts and logs, a summary, and append-only `receipts.jsonl` history; rerunning a gate preserves its earlier samples.
- `findings.json` holds the validated inventory and its coverage map.
- `loop.jsonl` is append-only; a terminal record supersedes the scored record of its round without rewriting history.
- `report.md` is the human-readable result, written only when a run completes.

Finalization fails closed. Source drift marks the run `stale`; a failed gate, missing review, open remediation finding, or non-converged loop leaves it `in_progress` or `blocked`. Gate receipts certify the recorded scope only when every post-gate snapshot matches it; a batch with observed source changes retains its samples without certifying any digest. Refreshing or reverting source cannot make those samples current. Changes made and reverted within a single gate remain outside these boundary checks. To adopt edited source you must call `rig_evidence` with `action: "refresh"` and a reason, then rerun the affected gates. Deleted output files use the same empty-bytes SHA-256 as their scope entries, so implementation handoffs preserve their digest through review.

Findings must be validated against source. Every finding ends in exactly one bucket: `fixed`, `rejected`, `deferred`, or `accepted-risk`. A terminal bucket requires a disposition. Remediation binds `source_run_id` to the source inventory and must reconcile every source finding ID before finalization. Reviewer identities and `verified_by` are supplied strings rather than authenticated session attestations; independent review and fixed-finding verification remain instruction-enforced.

Refresh may resume a stale run or a run blocked only by missing evidence: name its explicit `run_id`, provide a reason, and rerun evidence invalidated by the new scope. A recorded non-converged loop stop cannot be reopened by refresh; start a replacement run citing the stopped run ID. A project without configured gates passes `gate_exemption` and its manual substitute at run start, then executes and records that substitute. A documentation-only remediation, where every changed path is user documentation such as a README, `docs/`, or prose `.md`, `.rst`, or `.txt` files, runs only the gates that check those files, or that exemption with a docs check when every detected gate runs the test suite, so the full test suite does not run; the skipped suite is recorded as a limit that never lowers confidence. Any other changed path runs every configured gate.

## Git guardrails

The permission hook runs after configured rules and can only escalate. It never turns a configured `deny` or `ask` into `allow`.

- Push, remote configuration, `gh` publication, package publishing, and container pushes always ask.
- Hard reset, forced clean, forced checkout, restore, stash drop, forced worktree removal, and recursive file deletion always ask.
- Rebase, amend, filter-repo, and replace always ask.
- A plain local commit asks unless a grant exists.

Standard mode also asks for Git and GitHub forms outside the literal command-and-option allowlist, including unreadable shell syntax that names either tool. Strict mode classifies merge and cherry-pick as destructive-local rather than the standard unparsed-Git fallback; both modes ask. Configured rebase recovery allows and non-destructive clean forms still ask through that fallback.

A grant is a single JSON file in the repository's common Git directory, outside version control and shared by every worktree. Record it only after the user explicitly accepts that it covers local commits and excludes push, force, history rewriting, and destructive operations:

```text
rig_git_grant action=approve confirm="Approve always" scope_acknowledged=true
```

Revoke it with `rig_git_grant action=revoke`. `rig_git_grant action=status` reports `granted`, `absent`, `invalid`, or `no-git`. A grant lasts 30 days or until revoked and stores the literal user confirmation, creation ID, physical common-directory binding, and creation and expiry times. A symlink, an unknown schema, an expired or differently bound record, or an incomplete older record counts as no grant and requires a fresh **Approve always** answer. Writers to the common Git directory remain trusted; the record is not cryptographically signed. One-time commit consent uses the runtime shell permission prompt, without an additional conversational **Approve** question.

Nothing in this plugin pushes, tags, or publishes. Release and publication remain human-owned.

### What the shell guard does not promise

The shell guard is a text classifier that decides whether a shell command line asks the user or runs. Catalogued tools use bounded safe forms; unlisted programs can stay quiet. Privilege wrappers require a positively admitted target form and reject its output options, output operands and file-creating wrappers. `su` always asks because its command is a shell string. It is a speed bump against accidental and obvious misuse by an agent, not a sandbox or a security boundary. A determined or tricked agent can still get code run through forms the classifier cannot see. For a hard boundary, use OS-level sandboxing or isolation around the agent, such as a container, VM, restricted user and network policy.

#### Limits kept by design

Evidence names below refer to files under `tests/unit/guardrails/` or to the named review check class.

- **Catalogued tool forms ask.** Listed package managers, project tools, runners and containers ask unless they match an exact safe form; unlisted programs can stay quiet without a privilege wrapper. `awk` and `tree` are not on the read-only list; every awk-family invocation asks, including apparently harmless programs. Why it stays: bounded tables avoid guessing an unfamiliar tool's operand language. In practice: a quiet unknown command is not proof of a harmless read. Evidence: `project-tools.test.ts`, `read-only-programs.test.ts`, `program-identity.test.ts`.
- **The safe forms are bounded and can run project code.** They cover `pytest` and `python -m pytest` with `-q`, `-x`, `-v` (including repetitions and supported clusters), `-k VALUE`, `-m VALUE`, `--co` or `--collect-only`; `ruff check`, `ruff format --check` or `ruff format --diff`; option-free `mypy`; exactly `tsc --noEmit`; exactly `npm test` or `npm run test|lint|typecheck|build`; and option-free `cargo check|test|clippy`. Path operands must be ordinary literal relative project paths; the ruff forms accept only those paths after the shown words. Why it stays: users need their chosen test, lint and build tooling. In practice: these commands can run the project's own code, and a test can do anything the agent's account can do; approve the project accordingly. Evidence: `project-tools.test.ts`, `program-identity.test.ts`.
- **Root-changing and configuration options ask.** This includes `pytest -c ../pytest.ini` and options that move a tool's root or select another configuration. Why it stays: an input file can load code or redirect writes, so a filename is not proof of a harmless read. In practice: users approve tests and checks with custom configurations even when they trust the file. Evidence: `project-tools.test.ts`, `write-targets.test.ts`.
- **Shell syntax outside the small grammar asks.** Loops, same-text functions, arithmetic, most assignments and builtins, background jobs, complex parameter expansions and ambiguous comments are outside the quiet grammar. Why it stays: rejecting an unfamiliar construct avoids guessing how a shell will evaluate it. In practice: users approve ordinary shell scripts and even comments with unmatched quotes; a small literal assignment allowlist does not grant arbitrary environment changes. Evidence: `dynamic-code.test.ts`, `grammar-allowlist.test.ts`.
- **Writes need ordinary project paths.** Quiet write targets must be literal ASCII relative paths without a parent segment or a leading-dot segment outside `.github`, `.temp`, `.reports`, `.plans`, `.notes`, `.cache`, `.experiments` and `.developments`; moving to an unknown directory makes later writes ask. Why it stays: the guard cannot prove that an arbitrary path is an ordinary project file. In practice: users approve absolute, home, parent, non-ASCII and hidden-file writes, even legitimate ones, and cannot assume that an accepted relative path resolves without symlinks. Evidence: `write-targets.test.ts`.
- **Interpreter and debugger options need a positive match.** PHP, Julia and ts-node admit only `-h`, `-v`, `--help` and `--version`; Perl additionally admits `-V`, `-w` and `-X`, Ruby additionally admits `-w` and `-c`; Lua admits only `-v`; R/Rscript, IPython, tsx, Deno, Bun and gdb admit only `--help` and `--version`; lldb additionally admits `-v`, `-h`, `-x` and `--no-lldbinit`; osascript admits no options before its script. All other options ask, including abbreviations, attached values, module loads, load paths, initialization files and inline code. Python and Node retain their existing rules. Plain literal script-file runs retain their existing content-visibility limit; osascript words after its script are script arguments. Why it stays: understanding arbitrary programs would exceed a shell-text classifier's scope. In practice: even harmless programs and unsupported diagnostic flags need approval; an approved program can write files, run commands or use the network. Evidence: `positive-options.test.ts`, `launchers.test.ts`, `program-identity.test.ts`.
- **Saved prose can be treated as shell text.** A saved README, fenced command, Markdown bullet or other prose that reads as shell asks; a saved read-only Git script also asks, and unquoted attached parentheses have no file-extension exemption. Why it stays: the guard cannot reliably distinguish instructions from executable text by filename, and parentheses can execute shell code. In practice: saving documentation, Python or JavaScript examples, or a harmless Git inspection script may need approval. Evidence: `dynamic-code.test.ts`, `write-targets.test.ts`, `overasks.test.ts`.
- **Uncatalogued wrapper and launcher options ask when recognised as command channels.** Why it stays: guessing where an unfamiliar option ends and a program begins can hide execution. In practice: users approve unfamiliar launch syntax; unrecognised wrappers remain a separate open gap below. Evidence: `wrappers.test.ts`.
- **File-fed shells cannot be checked from their contents.** `bash < file` asks. A remote shell fed from a file is an accepted content-visibility limit: the current `ssh h < script.sh` is quiet, a regression from the previous commit's ask, retained because the classifier sees the path but not the script. Why it stays: the guard does not open script files or inspect the remote environment. In practice: local file-fed shell use needs approval, while the remote form can run unseen commands without a question; its remaining risk is also listed below. Evidence: `write-targets.test.ts`, static local and remote file-input checks.
- **Indirect options ask; literal reader filenames stay quiet.** Compiler and linker response files, configuration-as-options files and catalogued argument-file channels ask in separate, attached and equals spellings. This includes clang configuration, curl configuration, wget configuration, file-list inputs and patch in every form. Literal `@` filenames to cat, ls, head, tail, stat, sort, diff, cmp and wc, Git revisions, and search patterns stay quiet; option values still follow their own tables. Why it stays: some tools expand options where others read data. In practice: users approve indirect options and bulk input even when they trust the file. Evidence: `positive-options.test.ts`, `read-only-programs.test.ts`, `program-identity.test.ts`.
- **Listed installers and trust changes ask.** Listed system package managers and version managers ask, including apt, apt-get, dnf, yum, brew, port, nix and its hyphenated entry points, scoop, winget, choco, nodenv, goenv, proto, pkgx and easy_install. This also includes anacron, fcrontab, hook and schedule installers, `prek`, `direnv`, `mise` and its `rtx` alias, `asdf`, `nvm`, `fnm`, `pyenv`, `rbenv`, `volta`, `rustup`, `sdk` and `corepack` installation, activation or trust forms. Unlisted programs can stay quiet. Only `sudo` (including `sudo-rs`, `gsudo`, versioned names, absolute paths, executable suffixes and capitalization) and `doas` admit a target quietly, and only for a positively admitted read-only form through the wrapper chain. `run0`, system-level `systemd-run`, `machinectl`, `pkexec`, `su`, `gosu`, `pfexec`, `dzdo`, `pbrun`, `please`, `pleaseedit`, `doasedit`, `sudoedit`, `super`, `really`, `runas` and `ksu` always ask, regardless of target or option order. Unprivileged `systemd-run --user` retains its treatment only without machine, UID or GID selectors; it cannot remove inherited privilege. System-mode properties are never admitted. Output options such as `sort -o`, output operands such as `uniq input output`, and file-creating wrappers such as `flock` ask under privilege. `env` and `sudo` assignments use the same literal-name allowlist as command prefixes: names must be unquoted and unescaped, and values must be plain literals after quote removal. Safe assignments preserve read-only Git forms. Builtin `test` and `[` retain their operand checks under privilege, including absolute-path spellings. List-only forms of sudo, doas and their identity aliases print rights quietly, including `-n` and user selection; bare sudo, validation and invalidation ask. Why it stays: these commands can install code or arrange for it to run later outside the project. In practice: approving an untrusted installer can affect later shells, commits or scheduled jobs. Evidence: `wrappers.test.ts`, `program-identity.test.ts`, `project-tools.test.ts`.
- **Input-driven operands need an operand-safe form.** `xargs` accepts appended or replacement input quietly only for a short table of direct programs without write or execution options, such as cat, ls and wc, or a recognised reader whose parsed `--` ends options before input words. Targets behind wrappers ask, including `xargs sudo cat`, `xargs env cat` and `xargs nohup cat`; `sudo xargs cat` remains a read form. An external printf with a literal format not starting with `-`, including after `--`, also stays quiet; direct builtin printf retains its arithmetic checks. The program word cannot contain the replacement string. Other targets ask regardless of pipe, heredoc or argument-file input. BSD `time -l`, `-h`, `-p` and their clusters preserve the target's verdict. Why it stays: input words can become options as well as filenames. In practice: bulk rg, file, less and diff invocations need approval unless options are bounded. Evidence: `program-identity.test.ts`, `wrappers.test.ts`.
- **Read-only entries do not inherit write options.** Every `sed` in-place spelling asks, including GNU and BSD suffix and cluster forms; other programs' write or execution modes must leave the read-only path and be checked separately or ask. Why it stays: a familiar program name does not make all its modes read-only. In practice: users approve in-place edits and uncatalogued options, including some routine read options; a quiet read entry is not permission to overwrite its operands. Evidence: `positive-options.test.ts`, `read-only-programs.test.ts`, `overasks.test.ts`.

#### Known gaps

The following content and environment limits remain open. Regression tests and static classification cover program identity, argument-file options, installers and the bounded safe forms; they do not prove runtime isolation.

- **Open — code stored in files or transformed before execution.** Earlier reviews recorded heredocs written to a file and then run by path, printf/echo-written scripts, transformed bodies and other-language scripts calling commands. A current direct heredoc containing `git push` followed by `bash run.sh` asks, but `bash run.sh` and `python3 script.py` alone are quiet. In practice: an agent can run unseen file contents, including destructive or publishing code, without a question. Evidence: `persisted-bodies.test.ts`, persisted-script and transformed-body review checks; current static file-launch checks.
- **Open — shell options before a script.** Current static checks leave `bash +x script.sh` and `bash +o errexit script.sh` quiet; the recorded `bash --rcfile /dev/null script.sh` form now asks. In practice: an agent can still run an unchecked script through the quiet option forms, with the account's file and network access. Evidence: `launchers.test.ts`, `write-targets.test.ts`, shell-option-before-script review checks.
- **Open — function, alias and execution indirection coverage.** Same-text shell functions and aliases, redefinition order and `exec -a` were recorded as classifier limits; current samples defining a function or alias that pushes, and `exec -a label git push`, all ask. Those samples do not establish complete coverage of shell name resolution. In practice: an unrecognised indirection could let a tricked agent execute a different program from the one the text appears to name. Evidence: `wrappers.test.ts`, `dynamic-code.test.ts`, function and alias review checks.
- **Open — inherited state and indirect write destinations.** The guard does not track environment variables or files supplied outside the command text, earlier-call symlinks, archives unpacked into configuration directories, `core.hooksPath` pointing at an ordinary directory, or commands inside a named configuration such as `ssh -F`. In practice: an agent's apparently ordinary command can load other code or reach files outside the project; users can lose data or have code run later through hooks or configuration. Evidence: `guardrails.ts` lexical write-target and command-option limits; inherited-state and indirect-write review checks.
- **Open — other platforms and shell languages.** Windows spellings and fish/csh/pwsh bodies had partial or no semantic coverage in the final review. Normalising executable suffixes does not prove those shells' syntax is understood. In practice: users on those platforms may encounter extra prompts or an unseen execution path that runs code without asking. Evidence: platform and alternate-shell review coverage checks, `program-identity.test.ts`.
- **Open — unrecognised wrappers and launchers.** Recognised opaque shell bodies ask, but a current `mystery-wrapper payload` sample is quiet because the classifier does not know that program's operand language. In practice: an agent can invoke a launcher that interprets an innocent-looking operand as code, potentially writing, deleting or publishing without a question. Evidence: `wrappers.test.ts`, `guardrails.ts` unlisted-wrapper limit, static unknown-wrapper check.
- **Open — code and file input behind a safe-looking form.** A safe-list command can honour inherited environment and project configuration, and the remote file-fed shell above still runs unseen input. Allowing the project's chosen code is deliberate; verifying what that code actually does remains outside this classifier. In practice: a malicious test or build, or a remote script, can use the agent's privileges without an additional guard prompt. Evidence: `project-tools.test.ts`, `write-targets.test.ts`, project-code and remote-file-input review checks. The regression suite also checks quiet numbered and executable-suffixed pytest entry points, Python module and tool-script launches, time-wrapped reads, and literal reader filenames. Script contents and platform shell semantics remain subject to the limits above.

## CLI

| Command                                          | Effect                                                                                                                              |
| ------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------- |
| `opencode-rig setup`                             | Install the role agents, merge the shipped rules, and write the global delivery-principles block.                                   |
| `opencode-rig setup --adopt-claude-instructions` | Also copy `~/.claude/CLAUDE.md` into the global instructions file.                                                                  |
| `opencode-rig setup --agents-only`               | Install or verify only the role agents; skip the rule merge and the instructions block.                                             |
| `opencode-rig init`                              | Alias for `setup`. Install or refresh the role agents and merge the shipped permission rules. Stops on an edited or unstamped file. |
| `opencode-rig check`                             | Verify installed agents against this package and report the permission rules, without writing.                                      |
| `opencode-rig init --upgrade`                    | Replace stale or locally edited stamped agents.                                                                                     |
| `opencode-rig remove`                            | Remove only files that carry the opencode-rig stamp, plus the global block setup wrote.                                             |
| `opencode-rig doctor`                            | Report install and environment health.                                                                                              |
| `opencode-rig doctor --assets`                   | Also curate shipped skill and role cards for roster drift and dead links.                                                           |
| `opencode-rig print-config`                      | Print the shipped permission rules for a hand-edited configuration.                                                                 |
| `opencode-rig version`                           | Print the package version.                                                                                                          |

Use `--project` to target `.opencode/agents` and the project's `opencode.jsonc` or `opencode.json` at the Git root instead of the global configuration directory. Outside Git, it uses the current directory. It redirects the rule merge as well as the agents.

The CLI doctor checks installation and environment basics. With `--assets`, it also checks the shipped file rosters, card structure, links, duplicate prose, and size. Use the in-session `rig_doctor` tool for plugin load, version-source agreement, Git grant, configured gates, stale runs, and exact frontmatter-description agreement with the routing manifest; a healthy CLI result does not establish those checks passed.

## Upgrading and removal

```bash
opencode plugin update opencode-rig
opencode-rig init --upgrade
```

Removal is the reverse:

```bash
opencode-rig remove
opencode plugin remove opencode-rig
```

`remove` keeps any agent file you wrote yourself; only stamped files are deleted. It does not undo the permission rules `init` merged into your configuration: those survive the removal and have to be removed by hand from `opencode.json` or `opencode.jsonc`. Re-running `init --upgrade` restores the agent files after a package upgrade. `init` rewrites the configuration file in place and keeps no backup of the previous content, so to roll the rules back delete the shipped entries from your configuration by hand — and keep your own copy before the first merge, because a file you already keep under version control already has one.

## Design boundaries

- No DAG or wave scheduler, approval allowlist parser, native question provider, or agent-shim lifecycle. Workflows are bounded by skill contracts instead.
- The permission rules are merged at install time only. `init` writes them once and every role agent carries the shared allowlist in its frontmatter; `src/guardrails.ts` never reads the ruleset, so no session-time rule evaluation happens.
- No remote mutation, ever. `rig-release` assesses readiness and stops there.
- No invented checks. If no gate is configured, a run needs an explicit exemption and the manual check that replaces it.
- Prompt rules are not a sandbox. The permission hook is host-enforced; workflow discipline is skill-enforced.
- A reviewer's report proves review happened, not that no defect exists. Unreviewed areas stay coverage gaps.

## Development

```bash
npm install
npm run check
```

`npm run check` runs the typecheck, the Vitest suite, the version-source check, and the tarball content check. Focused runs:

```bash
npx vitest run tests/unit/guardrails/guardrails.test.ts
node scripts/sync_version.mjs --check
node scripts/pack_check.mjs
```

## Documentation

- [Evidence rules](src/resources/references/evidence.md)
- [Commit handoff](src/resources/references/commit-handoff.md)
- [Convergence](src/resources/references/convergence.md)
- [Permission reference](src/resources/permissions-guide.md)
- [Changelog](CHANGELOG.md)

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
