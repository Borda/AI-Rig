# opencode-rig

Current version: `0.2.0`

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

- OpenCode `2.1.0` or newer (tested with `2.1.4`).
- Node.js `20` or newer.
- Git for scope evidence and the optional local commit grant.

## Install

Published package, when available:

```bash
opencode plugin add opencode-rig@0.2.0
npx opencode-rig@0.2.0 init
```

Before the first npm release, install straight from this repository:

```bash
opencode plugin add 'github:Borda/AI-Rig#main::path:plugins/opencode-rig'
node plugins/opencode-rig/bin/opencode-rig.mjs init   # or the path inside your clone
```

A branch specifier follows `main`; replace it with a full commit hash when the install must stay pinned. `opencode plugin add` is idempotent and writes the package specifier into `~/.config/opencode/opencode.json`. It caches the package for loading but does not link this package's CLI on `PATH`, so run `init` through `npx` for a published version or the checked-out script otherwise.

`init` writes the seven `rig-*` role agents that OpenCode cannot register from a plugin package; they land in `~/.config/opencode/agents/`, or in `.opencode/agents/` with `--project`. Add `--upgrade` to replace stamped agents after a package upgrade; without it, `init` and `check` stop on a locally edited file.

The recommended registration is already checked in at the repository root as [`opencode.jsonc`](../../opencode.jsonc), and `make sync-opencode` performs the global registration plus `init` from pushed state.

Add the plugin options to `opencode.jsonc` if you want anything other than the defaults:

```jsonc
{
  "plugins": [{ "package": "opencode-rig", "options": { "guardrails": "standard" } }],
}
```

Add the recommended permission rules with `opencode-rig print-config`. They keep push, hard reset, clean, and rebase behind an explicit answer. Plain commits are governed by the guardrail hook instead of a blanket rule, so a recorded grant can take effect.

Ignore the artifact directory in projects that use the plugin:

```bash
printf '\n.opencode-rig/\n' >> .gitignore
```

## Workflows

Run a workflow through its slash command, or load the matching skill by name.

| Command            | Skill             | Use it for                                                     | Completion evidence                                                                 |
| ------------------ | ----------------- | -------------------------------------------------------------- | ----------------------------------------------------------------------------------- |
| `/rig-plan`        | `rig-plan`        | Multi-file or ambiguous changes that need a frozen plan        | Bound plan digest and one independent challenge                                     |
| `/rig-investigate` | `rig-investigate` | Bugs, CI failures, and unexplained behavior before a fix       | Reproduction, root cause, eliminated hypotheses                                     |
| `/rig-research`    | `rig-research`    | Source-backed answers before a design or dependency decision   | Labeled claims, contradictions, independent source check                            |
| `/rig-implement`   | `rig-implement`   | Executing a frozen plan or a clearly scoped change             | Passing gates, changed-path digests, commit handoff                                 |
| `/rig-review`      | `rig-review`      | Reviewing a working tree, commit, or path                      | Validated findings artifact with mandatory `correctness` axis and reviewer identity |
| `/rig-remediate`   | `rig-remediate`   | Closing findings from a review                                 | Every finding in a terminal bucket plus re-review                                   |
| `/rig-challenge`   | `rig-challenge`   | Risky or security-sensitive changes                            | Converged loop with no security or critical residue                                 |
| `/rig-optimize`    | `rig-optimize`    | A concrete performance target with a configured benchmark gate | Baseline spread, kept rounds, correctness gates                                     |
| `/rig-distill`     | `rig-distill`     | Repeated friction across several recorded runs                 | Counted patterns and accepted asset proposals                                       |
| `/rig-release`     | `rig-release`     | Release readiness before a human publishes                     | HEAD baseline, gates, package list, read-only drafts                                |
| `/rig-doctor`      | `rig-doctor`      | A workflow or tool is missing or failing                       | Pass/warn/fail report with remediation commands                                     |

Every workflow follows the same envelope: classify the request, collect authoritative source, act in small batches, verify with configured checks, finalize the run, then report limits honestly.

`rig-research` covers docs, state-of-the-art comparisons, paper analysis, and paper-versus-code fidelity. `rig-distill` reads recorded runs; it proposes assets but never installs, edits, or prunes them. `rig-optimize` only uses a benchmark that is already a configured gate, so a performance claim always has a reproducible command behind it.

## Tools

| Tool            | Purpose                                                                                                                                                                    |
| --------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `rig_evidence`  | Start, inspect, record, refresh, and finalize an evidence run. Binds scope bytes, paths, plan digests, and reviewer evidence.                                              |
| `rig_gate`      | Detect and run configured gates, store receipts and logs, and report failures by gate id.                                                                                  |
| `rig_findings`  | Write or read a validated findings inventory with severity, coverage, and bucket accounting.                                                                               |
| `rig_loop`      | Drive the append-only adversarial loop: baseline, assessed rounds, cumulative progress, and a validated stop reason.                                                       |
| `rig_git_grant` | Read, record, or revoke the local Git approval grant after an explicit user decision.                                                                                      |
| `rig_dispatch`  | Run one bounded role in a child session when native `rig-*` subagents are unavailable.                                                                                     |
| `rig_doctor`    | Check plugin load, version agreement, agent installation, Git grant, artifact ignore, gates, and stale runs; `action: "assets"` also curates shipped skill and role cards. |

All seven tools are registered under their full names; there is no namespace to remember. Calls are only accepted for an active or explicitly named run, and mutating calls validate their input before writing anything.

### Example: rig-review findings

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

Without installed agents, `rig_dispatch` provides the same role cards through a child session. The child never runs gates, commits, or declares completion.

## Configuration

Options live in the plugin entry of `opencode.jsonc`.

| Option               | Default           | Meaning                                                                                                                                         |
| -------------------- | ----------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| `guardrails`         | `"standard"`      | `standard` guards remote, destructive, and history-rewriting commands; `strict` also asks for merge and cherry-pick; `off` disables escalation. |
| `artifacts`          | `".opencode-rig"` | Artifact root inside the project. It may not escape the project directory.                                                                      |
| `gates`              | `[]`              | Explicit gate commands. When set, detection is skipped.                                                                                         |
| `gateTimeoutMs`      | `600000`          | Per-gate timeout, between 1 second and 1 hour.                                                                                                  |
| `maxParallel`        | `4`               | Ceiling for concurrent inline children, between 1 and 16.                                                                                       |
| `maxLoopRounds`      | `3`               | Round limit used to validate the `round-limit` stop reason, between 1 and 10.                                                                   |
| `ttlDays`            | `30`              | Age at which doctor reports a run as stale, between 1 and 3650.                                                                                 |
| `journal`            | `true`            | Append redacted tool-call records to the active run or artifact root.                                                                           |
| `codemap`            | `"auto"`          | Codemap integration mode. `auto` is inert until a codemap provider is present.                                                                  |
| `compactionPreserve` | `true`            | Inject active run identity into compaction context.                                                                                             |

Unknown option keys fail plugin setup instead of being ignored.

## Evidence and artifacts

A run lives under `.opencode-rig/runs/<skill>/<run-id>/`:

- `run.json` is the authoritative record: scope digest, inputs, outputs, gates, findings summary, review evidence, loop summary, limits, and status.
- `gates/` holds one JSON receipt and one log per gate, plus a summary.
- `findings.json` holds the validated inventory and its coverage map.
- `loop.jsonl` is append-only; a terminal record supersedes the scored record of its round without rewriting history.
- `report.md` is the human-readable result, written only when a run completes.

Finalization fails closed. Source drift marks the run `stale`; a failed gate, missing review, open remediation finding, or non-converged loop leaves it `in_progress` or `blocked`. To adopt edited source you must call `rig_evidence` with `action: "refresh"` and a reason, then rerun the affected gates.

Findings must be validated against source. Every finding ends in exactly one bucket: `fixed`, `rejected`, `deferred`, or `accepted-risk`. A terminal bucket requires a disposition, and the review counts must reconcile with the run summary.

## Git guardrails

The permission hook runs after configured rules and can only escalate. It never turns a configured `deny` or `ask` into `allow`.

- Push, remote configuration, `gh` publication, package publishing, and container pushes always ask.
- Hard reset, forced clean, forced checkout, restore, stash drop, forced worktree removal, and recursive file deletion always ask.
- Rebase, amend, filter-repo, and replace always ask.
- A plain local commit asks unless a grant exists.

A grant is a single JSON file in the repository's common Git directory, outside version control and shared by every worktree. Record it only after the user explicitly accepts that it covers local commits and excludes push, force, history rewriting, and destructive operations:

```text
rig_git_grant action=approve confirm="<the user's own approval>" scope_acknowledged=true
```

Revoke it with `rig_git_grant action=revoke`. `rig_git_grant action=status` reports `granted`, `absent`, `invalid`, or `no-git`. A symlink, an unknown schema, or a path outside the common Git directory counts as no grant.

Nothing in this plugin pushes, tags, or publishes. Release and publication remain human-owned.

## CLI

| Command                        | Effect                                                                    |
| ------------------------------ | ------------------------------------------------------------------------- |
| `opencode-rig init`            | Install or refresh the role agents. Stops on an edited or unstamped file. |
| `opencode-rig check`           | Verify installed agents against this package without writing.             |
| `opencode-rig init --upgrade`  | Replace stale or locally edited stamped agents.                           |
| `opencode-rig remove`          | Remove only files that carry the opencode-rig stamp.                      |
| `opencode-rig doctor`          | Report install and environment health.                                    |
| `opencode-rig doctor --assets` | Also curate shipped skill and role cards for roster drift and dead links. |
| `opencode-rig print-config`    | Print the recommended permission rules.                                   |
| `opencode-rig version`         | Print the package version.                                                |

Use `--project` to target `.opencode/agents` in the current directory instead of the global configuration directory.

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

`remove` keeps any agent file you wrote yourself; only stamped files are deleted.

## Design boundaries

- No DAG or wave scheduler, approval allowlist parser, native question provider, or agent-shim lifecycle. Workflows are bounded by skill contracts instead.
- No remote mutation, ever. `rig_release` assesses readiness and stops there.
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
npx vitest run tests/unit/guardrails.test.ts
node scripts/sync_version.mjs --check
node scripts/pack_check.mjs
```

## Documentation

- [Evidence rules](src/resources/references/evidence.md)
- [Commit handoff](src/resources/references/commit-handoff.md)
- [Convergence](src/resources/references/convergence.md)
- [Changelog](CHANGELOG.md)

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
