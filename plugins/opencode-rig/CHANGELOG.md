# Changelog

All notable changes to this package are documented here. Versions follow [Semantic Versioning](https://semver.org/).

## 0.3.0

- Ship the approval rules inside the package as `src/resources/permissions.json`, with `src/resources/permissions-guide.md` as the review reference. The rules that keep push, hard reset, clean, and rebase behind an explicit answer can be read before the plugin changes a session.
- `opencode-rig init` merges those rules into the configuration file you already keep: `~/.config/opencode/opencode.jsonc`, or an existing `opencode.json` beside it, or — with `--project` — the `opencode.jsonc` or `opencode.json` in the current directory. Your own rules, comments, and ordering survive the merge, and a rule of yours in the `git rebase` family is left to govern that whole family. Routine sessions stop re-asking without losing your configuration.
- `opencode-rig print-config` prints the same rules for a configuration you would rather edit by hand, and `opencode-rig check` reports whether the rules are present without failing on the report.
- Every role agent carries the shipped ruleset after its own `shell: *` catch-all, so inspection commands such as `git status`, `grep`, and `cat` stop asking inside a role agent. A command the list does not name still asks.
- The read-only list no longer allows `rg`, whose `--pre` option runs an arbitrary program, or a `date` with arguments, whose GNU `-s` option sets the system clock. A command that accepts `--output` now asks instead of writing a file silently.
- The guide states the two channels the allowlist does not close, so the widened surface stays reviewable: a redirection attached to an allowed command still matches, which gives the six read-only role agents a write path their own `edit: deny` does not cover, and an agent rule is appended after the global ones, so an agent allow outranks a global deny for the same command.
- The permission hook now classifies the v2 `execute` action as well as `shell`, so a destructive command that arrives through Code Mode is no longer skipped. A destructive or history-rewriting command that would discard working-tree changes now names the paths that belong to someone else's work, so a shared checkout cannot lose them silently.
- `rig-plan` now requires a scope verdict table (every surface `in-scope`, `deferred` with an unblock condition, or `out-of-scope` with a reason), a revision counter, and a verdict-changes section; owned paths must reconcile with the table both ways and the delegation brief is derived from it. A surface silently moving out of scope was the defect this prevents.
- `rig-implement` refuses to start without a frozen plan, reports one plan row per changed path, refuses to revert a path that is not in scope, requires explicit user approval recorded through `rig_git_grant` before a commit, and states split-and-conquer delegation plus RTK guidance for read-heavy commands.
- `opencode-rig setup` (with `init` as its alias) now also writes the shipped delivery principles into `~/.config/opencode/AGENTS.md` as a digest-stamped managed block. Setup refuses to shadow an existing `~/.claude/CLAUDE.md`; `--adopt-claude-instructions` copies it into the block, the file is created owner-only, symlinked targets and parent directories are never written through, and `remove` strips the block while restoring the user's bytes. `--agents-only` installs only the role agents so the Makefile sync stages keep their own refusals.
- The guardrail working-tree lookup no longer executes repository-configured Git config such as `core.fsmonitor`, treats filenames as untrusted text in the approval prompt, and is bounded to one second so it cannot stall the permission hook.

## 0.2.0

- Enforce `correctness` as a mandatory review axis for `rig-review` runs; `rig_findings` now normalizes `axes`/`aspects` (case/duplicates), validates allowed dimensions, and requires coverage when correctness is included. Update `rig-review`, `rig-reviewer`, and `rig-remediate`/`rig-challenge` guidance accordingly.
- Add `rig-research` with `docs`, `sota`, `paper`, and `code-fidelity` modes. Every claim is recorded as `sourced`, `inferred`, or `unknown`, contradictions are reported rather than averaged, and a research run requires a `rig-scientist` review before completion.
- Add `rig-distill`, which mines this project's recorded runs or a supplied external inventory for recurring signatures, applies an explicit evidence threshold, rejects proposals that duplicate an existing asset, and proposes without installing or pruning anything.
- Add `rig-optimize`, a guarded benchmark campaign. The benchmark must already be a configured gate, a win must clear the recorded noise band and the declared minimum improvement, every correctness gate still has to pass, and a kept round must be independently recomputed by `rig-optimizer`.
- Extend `rig-release` with read-only release-note, migration-note, and contributor-reply drafts. Every draft line traces to a commit, finding, or changelog entry; nothing is posted.
- Add `rig-scientist` and `rig-optimizer` role cards. Both are read-only, and `rig-scientist` is the only role with `webfetch` allowed.
- Add `rig_doctor` action `assets` and `opencode-rig doctor --assets`: roster drift and dead relative links fail; duplicated prose and oversized cards warn.
- `pack_check` now derives the required skill and role files from `rig.json` instead of a hard-coded list, so the tarball contract follows the roster.
- Refuse to finalize new skill runs without their evidence: research and optimize require review, optimize additionally requires gates, and distill stays advisory.

## 0.1.1

- A first `init` now exits successfully after installing the five role agents. It previously counted every freshly written file as a problem, so a scripted install such as `make sync-opencode` stopped on a clean machine.
- `init --check` still reports missing agents as problems, and repeating `init` remains a no-op.

## 0.1.0

- Initial release of the OpenCode evidence-first workflow plugin: eight skills (`rig-plan`, `rig-investigate`, `rig-implement`, `rig-review`, `rig-remediate`, `rig-challenge`, `rig-release`, `rig-doctor`) with matching slash commands.
- Seven tools (`rig_evidence`, `rig_gate`, `rig_findings`, `rig_loop`, `rig_git_grant`, `rig_dispatch`, `rig_doctor`) record scope digests, gate receipts, validated findings, convergence rounds, and install health in a project artifact directory.
- Runs cannot complete without their workflow evidence: source drift marks them `stale`, failed gates block implementation and release, review requires a named independent reviewer, remediation requires every finding closed, and challenge requires convergence with no security or critical residue.
- A permission hook escalates remote mutations, destructive operations, history rewrites, and unreviewed local commits to `ask` without ever downgrading a configured answer; an explicit local grant covers plain commits only.
- `opencode-rig init` installs or upgrades five read-only `rig-*` role agents with hash stamps, refuses to overwrite edited or unstamped files, and `remove` deletes only stamped files.
- Doctor reports plugin load, version agreement, agent installation, Git grant state, artifact ignore status, detected gates, and stale runs, with an explicit opt-in cleanup for stale runs.
