# Changelog

All notable changes to this package are documented here. Versions follow [Semantic Versioning](https://semver.org/).

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
