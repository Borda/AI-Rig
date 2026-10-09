# AI-Rig — Project-Only Instructions

<!-- Scope: maintaining this repository only; never shipped. Policy meant for every project or install goes into the plugin that ships it, never here. -->

<!-- policy-sibling-sync: CLAUDE.md, AGENTS.md, plugins/AGENTS.md, plugins/CLAUDE.md -->

- Any policy change in one listed instruction file must trigger a relevance review of every other listed file before completion.
- Synchronize applicable shared policy in either direction.
- Preserve intentional agent-specific differences.
- Record when no counterpart change is needed.

## Instruction Layering

Repository-wide policy belongs in this top-level file. Lower-scope instruction files inherit it and must add only narrower rules or explicit exceptions, never repeat the same policy.

When a top-level policy changes, review lower layers for conflicts or obsolete duplication rather than copying the new text into them.

**This file is for maintaining this repository only and is never shipped** — plugin users never see it. Anything general, meant for every project or every install, must be distributed in the plugin that ships it (`plugins/cc_foundry/rules/` or `plugins/cc_foundry/CLAUDE.src.md` for Claude, `plugins/codex-rig/assets/AGENTS.md` or `plugins/codex-rig/shared/` for Codex), never added here. This file holds only this repository's own conventions and pointers to those shipped rules.

## Version Continuity

This repository's own convention, not shipped policy: other projects commonly decide SemVer at release time rather than per commit.

- Before changing any shipped version or serialized artifact schema, identify its version family and read that family's value from the last committed `HEAD`; do not use an earlier uncommitted edit or an unrelated nested version as the baseline.
- A changed integer schema advances exactly one step from its committed current value. A new version family starts at 1. Keep older versions only as explicitly labeled historical readers; several revisions before the next commit still produce one bump from `HEAD`.
- Check the proposed value against `HEAD` before handoff and record the comparison with the affected verification. Reject skipped or downgraded versions. Plugin release versions also follow the separate SemVer pre-bump gate in `plugins/CLAUDE.md`.

## Edit Scope — Hard Constraint

**ALL edits must stay within this project directory.** Never directly edit `~/.claude/` or `$CODEX_HOME` (cache, settings, hooks, or any file under the user home).

**Permitted edit roots** (project-local):

- `.claude/settings.json`, `.claude/settings.local.json` — project Claude config
- `.codex/` — project Codex config, skills, session policy (mirrored to `$CODEX_HOME` by `Makefile`, never edited there)
- `plugins/*/{agents,skills,rules,hooks,bin}/` — plugin source

**Propagation to live cache** at `~/.claude/plugins/cache/`:

- `Makefile` installs from the pushed GitHub remote, not local working tree — commit and push first, then `make sync-claude`
- Never run `make sync-*`/`make clear-*` against uncommitted/unpushed changes — cache will not reflect them
- Never suggest or initiate propagation mid-workflow
- Applies to all skills — no skill auto-syncs

## Lint/Format — Use pre-commit Hooks, Not Direct Tools

Generic rule: `foundry:rules/claude-config.md` §Lint/Format (command forms: `foundry:rules/python-code.md` §Lint/Format). Hook ids (from `.pre-commit-config.yaml`): `ruff-check`, `ruff-format`, `eslint`, `mdformat`, `codespell`, `pyproject-fmt`, `validate-pyproject`, `end-of-file-fixer`, `trailing-whitespace`.

## Test Workflow

- Python minimum 3.10. Repository root is an environment anchor, not an installable package.
- Bootstrap test tooling with `uv sync --only-group test`; benchmark-only dependencies use `uv sync --only-group bench`.
- Run tests with `.venv/bin/python -m pytest <paths>` — **not** `uv run pytest` or a bare `pytest`; the project venv is the pinned environment. Start focused, broaden to the affected suite before completion.
- Broad local runs go parallel: `.venv/bin/python -m pytest -n 4 <paths>` (pytest-xdist, already in the test group). Measured on the full suite: 11556 tests in ~5 min at `-n 4`, several times faster than the same run serial. Use `-n 4` for any run wide enough to be worth waiting on; keep focused single-file runs serial, where worker startup costs more than it saves.
- Coverage tracing is opt-in: CI passes the `--cov=<plugin>/bin` list (see `.github/workflows/ci-tests.yml`), and a local run measures only when you add `--cov=<path>` with `--cov-report=term-missing`. Keep sources out of `[tool.coverage]`: it also configures tests that start their own coverage run. Untraced is the default because tracing slows every test, most on Windows.
- Reading a failure serially, and a pass-serial/fail-under-`-n` test being a real isolation defect: `foundry:rules/python-testing.md` §Parallel Runs (pytest-xdist).

## Test Selection

Generic marker discipline: `foundry:rules/python-testing.md` §Test Selection — Markers. This repository's vocabulary and commands:

- `integration` for real component interactions; `installed_plugin` for tests runnable without repository context; `packaging` for build/install/payload contracts; `live` for real external services or credentials.
- `installed_plugin` does not imply `packaging`. A `live` test also carries `integration` and retains explicit opt-in guards; selection never authorizes network or paid execution. Capability probes remain separate.
- Register selectors in repository **and shipped test** configuration.
- From the repository root, use `.venv/bin/python -m pytest -m installed_plugin` or `.venv/bin/python -m pytest -m "packaging and not live"`; omit paths for project-wide discovery. On Windows use `.venv\Scripts\python.exe`.

## Shipped Generic Policy — Pointers

Policy below applies to any project; the full text ships in foundry (`plugins/cc_foundry/rules/`, delivered to Claude as `~/.claude/rules/foundry-<name>.md`). Cited as `foundry:rules/<file>.md`. In this repository read `plugins/cc_foundry/rules/<file>.md` directly: the installed copy lags the working tree until it is published and synced with `make sync-claude`.

| Topic                                                      | Foundry home                                                                      |
| ---------------------------------------------------------- | --------------------------------------------------------------------------------- |
| Test parametrization (`pytest.param` IDs, trailing commas) | `python-testing.md` §Test Structure                                               |
| Python record types (dataclass default, dict boundaries)   | `python-code.md` §Structured Data                                                 |
| Docstring conventions (plain-English opening line)         | `python-code.md` §Docstring Style                                                 |
| Multi-OS executables                                       | `python-code.md` §Multi-OS Executables; tests `python-testing.md` §Cross-OS Tests |
| Adversarial convergence loop                               | `quality-gates.md` §Adversarial Convergence Loop; `_full/adversarial-loop.md`     |
| Notebook authoring (`.ipynb` and Jupytext `# %%` scripts)  | `notebooks.md`; `_full/notebook-style.md`                                         |
| Markdown authoring (no hard-wrap, structure selection)     | `markdown.md` §Markdown Authoring                                                 |
| Instruction-file compression gate                          | `markdown.md` §Instruction-File Compression Gate                                  |
| Plan and benchmark isolation from shipped content          | `artifact-lifecycle.md` §Evidence Isolation                                       |

Repository-specific additions:

- Adversarial convergence loop: `AGENTS.md` links the Codex source-tree entrypoint; Foundry ships a local copy for Claude.
- Multi-OS executables: this repository supports Linux, macOS, and native Windows, so the conditional rule always applies here, without exception.
- Instruction-file compression gate: save the byte-exact backup under `.codex/caveman-compress/backups/`; this repository's calibration gate (`plugins/codex-rig/runtime/calibration/run.py --layout plugin`) is mandatory.

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

## Memory Policy

Nothing to auto-memory (`~/.claude/projects/.../memory/`). Learnings → skills, agents, rules, plugin files (versioned, distributed with plugin).

- New rule/guideline → edit `plugins/*/skills/*/SKILL.md`, `plugins/*/agents/*.md`, or `plugins/*/rules/*.md`
- Lesson/correction → update governing skill/agent/rule
- Never write to MEMORY.md or create memory files
