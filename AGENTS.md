# AI-Rig — Project-Only Agent Instructions

<!-- Scope: maintaining this repository only; never shipped. Policy meant for every project or install goes into the plugin that ships it, never here. -->

<!-- policy-sibling-sync: CLAUDE.md, AGENTS.md, plugins/AGENTS.md, plugins/CLAUDE.md -->

- Any policy change in one listed instruction file must trigger a relevance review of every other listed file before completion.
- Synchronize applicable shared policy in either direction; preserve intentional agent-specific differences and record when no counterpart change is needed.

## Instruction Layering

- Repository-wide policy belongs in this top-level file.
- Lower-scope instruction files inherit it and must add only narrower rules or explicit exceptions, never repeat the same policy; when a top-level policy changes, review lower layers for conflicts or obsolete duplication rather than copying the new text into them.
- **This file is for maintaining this repository only and is never shipped** — plugin users never see it. Anything general, meant for every project or every install, must be distributed in the plugin that ships it (the Codex Rig global template [plugins/codex-rig/assets/AGENTS.md](plugins/codex-rig/assets/AGENTS.md) or `plugins/codex-rig/shared/`; foundry rules for Claude), never added here. This file holds only this repository's own conventions and restates none of the shipped baseline.

## Shipped Global Baseline

These generic rules ship in the global template (section names as in that file) and are not restated here: core principles and focused delegation (§Execution Discipline, §Subagent Spawn Rules, §Code Quality), internal record types (§Code Quality item 19), docstring opening line (§Docstring Style Resolution), Multi-OS Executables, Markdown Authoring, the lossless instruction compression gate (§Scope And Layering), pytest parametrization, marker discipline and xdist isolation (§Testing), pre-commit hooks over bare lint tools (§Code Quality item 16), benchmark isolation (§Testing), plan isolation (§Work Handover) and Notebook Authoring. Sections below add only this repository's specifics. This repository supports Linux, macOS, and native Windows, so the conditional Multi-OS Executables rule always applies here.

## Edit Scope

- All edits stay inside this project directory. Never edit `$CODEX_HOME` or `~/.claude/` directly — both are install targets populated from this checkout, and a hand-edit there is overwritten on the next sync.
- Permitted roots: `.codex/` (Codex config, skills, session policy), `.claude/settings.json` and `.claude/settings.local.json`, `plugins/*/{agents,skills,rules,hooks,bin}/`.
- The Makefile installs from the pushed GitHub remote, not the local working tree: commit and push first, then `make sync-claude` or `make sync-codex`. Running it against uncommitted work silently installs the previous state.
- Never initiate propagation mid-task; it is a deliberate human-triggered step.

## Version Continuity

This repository's own convention, not shipped policy: other projects commonly decide SemVer at release time rather than per commit.

- Before changing any shipped version or serialized artifact schema, identify its version family and read that family's value from the last committed `HEAD`; do not use an earlier uncommitted edit or an unrelated nested version as the baseline.
- A changed integer schema advances exactly one step from its committed current value. A new version family starts at 1. Keep older versions only as explicitly labeled historical readers; several revisions before the next commit still produce one bump from `HEAD`.
- Check the proposed value against `HEAD` before handoff and record the comparison with the affected verification. Reject skipped or downgraded versions. Plugin release versions also follow the separate SemVer pre-bump gate in [plugins/AGENTS.md](plugins/AGENTS.md).

## Interpreter Commands — Fix the Launcher, Not the Call Site

Claude recipe commands retain `python` identities used by allow rules and blueprint digests. Codex does not add plugin `bin/` to shell PATH: its packaged helper recipes explicitly use the plugin's portable launcher, a narrow exception to the fixed-command rule.

- Preserve explicit project interpreters, managed-environment commands, and recorded command identities. Never add per-call interpreter selection or repeat interpreter-selection instructions across skills.
- Portable launchers validate the runtime before executing the workload, preserve arguments and exit status, and never retry a failed workload with another interpreter.
- Fix each launch layer at its owning boundary:

| Launch layer                   | Mechanism                                                                                                                         |
| ------------------------------ | --------------------------------------------------------------------------------------------------------------------------------- |
| Claude Bash tool               | Byte-identical plugin `bin/python` fallback, propagated by `propagate_shared.py`; real system `python` keeps its PATH precedence. |
| Claude hooks                   | Plugin-owned hook launchers; JavaScript hooks retain their existing Node probe and SessionStart diagnostic.                       |
| Codex helper recipes and hooks | Explicit plugin `bin/python` on POSIX or `bin/python.cmd` on native Windows; quote paths for the host shell.                      |
| Codex MCP servers              | Fixed `python` executable prerequisite, checked during setup; no global shim installation or automatic PATH modification.         |

- Guards: `tests/test_hook_interpreter_fallback.py` and `plugins/codex-rig/tests/packaging/test_sync_codex.py`. Extend them when adding a launch layer.

## Adversarial Convergence Loop

The loop ships in the global template. In this repository read the source-tree copy [plugins/codex-rig/shared/adversarial-loop.md](plugins/codex-rig/shared/adversarial-loop.md) directly before every independent review → authorized-fix cycle: the installed copy lags the working tree until it is published and synced.

## Lossless Instruction Compression Handover Gate

The gate ships in the global template (§Scope And Layering). Repository specifics: this repository's calibration gate (`plugins/codex-rig/runtime/calibration/run.py --layout plugin`) is mandatory; save the byte-exact backup under `.codex/caveman-compress/backups/`, and plugin-specific authoring, installability, cross-reference, versioning, and verification rules live in [plugins/AGENTS.md](plugins/AGENTS.md).

## Test Selection

Generic marker discipline ships in the global template (§Testing). This repository's vocabulary and commands:

- Use semantic markers: `integration` for real component interactions; `installed_plugin` for tests runnable without repository context; `packaging` for build/install/payload contracts; `live` for real external services or credentials.
- `installed_plugin` does not imply `packaging`. A `live` test also carries `integration` and retains explicit opt-in guards; selection never authorizes network or paid execution. Capability probes remain separate.
- From the repository root, use `.venv/bin/python -m pytest -m installed_plugin` or `.venv/bin/python -m pytest -m "packaging and not live"`; omit paths for project-wide discovery. On Windows use `.venv\Scripts\python.exe`.

## Project Workflow

- Python minimum: 3.10. The repository root is an environment anchor, not an installable package.
- Bootstrap test tooling with `uv sync --only-group test`; benchmark-only dependencies use `uv sync --only-group bench`.
- Run focused tests with `.venv/bin/python -m pytest <paths>` and broaden to the affected suite before completion.
- Broad local runs go parallel: `.venv/bin/python -m pytest -n 4 <paths>` (pytest-xdist, already in the test group). Measured on the full suite: 11556 tests in ~5 min at `-n 4`, several times faster than the same run serial. Use `-n 4` for any run wide enough to be worth waiting on; keep focused single-file runs serial, where worker startup costs more than it saves.
- Coverage tracing is opt-in: CI passes the `--cov=<plugin>/bin` list (see `.github/workflows/ci-tests.yml`), and a local run measures only when you add `--cov=<path>` with `--cov-report=term-missing`. Keep sources out of `[tool.coverage]`: it also configures tests that start their own coverage run. Untraced is the default because tracing slows every test, most on Windows.
- The generic xdist isolation rule ships in the global template (§Testing); in this repository the usual shared machine-global state is a `${TMPDIR}` sentinel without its `-${CSID}` suffix.
- Lint/format edits via the pinned pre-commit hooks (generic rule: global template §Code Quality item 16): `pre-commit run ruff-check --files <changed-python-paths>`, `pre-commit run ruff-format --files <changed-python-paths>`, and `pre-commit run mdformat --files <changed-markdown-paths>`; direct `ruff` or `mdformat` invocation drifts from the version/config pinned in `.pre-commit-config.yaml`. Use `pre-commit run --all-files` only when the task requires the repository-wide gate.
- Release and build entry points are plugin-specific; follow `plugins/AGENTS.md` and the owning plugin's scripts and README. Remote publication remains human-owned.
