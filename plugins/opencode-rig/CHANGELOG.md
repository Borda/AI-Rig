# Changelog

All notable changes to this package are documented here. Versions follow [Semantic Versioning](https://semver.org/).

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
