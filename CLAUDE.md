<!-- policy-sibling-sync: CLAUDE.md, AGENTS.md, plugins/AGENTS.md, plugins/CLAUDE.md -->

- Any policy change in one listed instruction file must trigger a relevance review of every other listed file before completion.
- Synchronize applicable shared policy in either direction.
- Preserve intentional agent-specific differences.
- Record when no counterpart change is needed.

## Instruction Layering

Repository-wide policy belongs in this top-level file. Lower-scope instruction files inherit it and must add only narrower rules or explicit exceptions, never repeat the same policy.

When a top-level policy changes, review lower layers for conflicts or obsolete duplication rather than copying the new text into them.

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

Repo pins lint/format tools via `.pre-commit-config.yaml` (ruff, eslint, mdformat, prettier, codespell, etc). **Never invoke these tools directly** (`ruff check`, `ruff format`, `eslint`, `mdformat`, ...) — version/config drift vs CI.

Invoke the specific hook instead:

```bash
pre-commit run <hook-id> --files <path>   # single hook, targeted files
pre-commit run --all-files                # full sweep
pre-commit run <hook-id> --all-files      # single hook, repo-wide
```

Hook ids (from `.pre-commit-config.yaml`): `ruff-check`, `ruff-format`, `eslint`, `mdformat`, `codespell`, `pyproject-fmt`, `validate-pyproject`, `end-of-file-fixer`, `trailing-whitespace`.

- Applies to ad-hoc checks during edits — not just the commit-time run
- If a hook is missing/needed and not yet in config, add it to `.pre-commit-config.yaml` rather than shelling out around it

## Test Workflow

- Python minimum 3.10. Repository root is an environment anchor, not an installable package.
- Bootstrap test tooling with `uv sync --only-group test`; benchmark-only dependencies use `uv sync --only-group bench`.
- Run tests with `.venv/bin/python -m pytest <paths>` — **not** `uv run pytest` or a bare `pytest`; the project venv is the pinned environment. Start focused, broaden to the affected suite before completion.

## Test Parametrization

Generic `pytest.param` and ID discipline lives in `foundry:rules/python-testing.md` §Test Structure (delivered as `~/.claude/rules/foundry-python-testing.md`); the equivalent text for Codex stays in `AGENTS.md` §Test Parametrization. Repo-specific addition:

- Remove optional trailing commas that force short lists or calls onto multiple lines, then run the pinned Ruff hooks. Retain required tuple commas, comments, and Ruff-restored wrapping; skip ambiguous edits. Preserve values, argument unpacking, order, duplicates, marks, and assertions.

## Test Selection

Generic marker discipline — semantic markers, never `pytestmark`, `--strict-markers`, classify by contract not duration, named `skipif` decorators — lives in `foundry:rules/python-testing.md` §Test Selection — Markers; `AGENTS.md` §Test Selection carries it for Codex. This repository's vocabulary and commands:

- `integration` for real component interactions; `installed_plugin` for tests runnable without repository context; `packaging` for build/install/payload contracts; `live` for real external services or credentials.
- `installed_plugin` does not imply `packaging`. A `live` test also carries `integration` and retains explicit opt-in guards; selection never authorizes network or paid execution. Capability probes remain separate.
- Register selectors in repository **and shipped test** configuration.
- From the repository root, use `.venv/bin/python -m pytest -m installed_plugin` or `.venv/bin/python -m pytest -m "packaging and not live"`; omit paths for project-wide discovery. On Windows use `.venv\Scripts\python.exe`.

## Python Documentation Style

Docstring conventions live in `foundry:rules/python-code.md` §Docstring Style — including the rule that a docstring's opening line states purpose in plain English, with code-shaped detail moved below. `AGENTS.md` §Python Documentation Style carries it for Codex. No repo-specific addition.

## Markdown Policy

Never hard-wrap prose in any Markdown file. Keep each prose paragraph on one physical line; preserve intentional structural breaks in headings, lists, tables, blockquotes, links, HTML `<details>` blocks, fenced code. Do not blindly unwrap or reflow a whole file; edit only the intended prose and retain its surrounding structure.

Structure Markdown for scanning and correct execution, not from line length alone. When one paragraph combines multiple actions, conditions, actors, statuses, exceptions, or decision branches, use the smallest fitting structure:

- Parallel obligations or independently checkable facts → bullets.
- Ordered actions, recovery paths, or state transitions → numbered lists.
- Compact closed mappings or comparisons with repeated fields → tables; keep long causal explanations out of table cells.
- Genuine notes, warnings, interpretation limits, or safety boundaries → blockquotes.
- Optional depth that would interrupt the main path → existing or justified `<details>` block.

Keep causal reasoning and cohesive rationale as prose. Do not convert paragraphs wholesale, add headings for every rule, or duplicate an existing navigation system. Keep headings concise and move detailed contracts below them.

When reformatting behavior-sensitive agent, skill, setup, approval, or recovery instructions, preserve modal language, exact literals, ordering, and stop conditions; run the affected contract and calibration gates because formatting can change instruction salience even when the words remain similar.

## Lossless Instruction Compression Handover Gate

Compression/structural reformatting of any `AGENTS.md` or `CLAUDE.md` = behavior-sensitive. Before handoff:

1. Save verified byte-exact pre-change backup under `.codex/caveman-compress/backups/`, outside active instruction-discovery paths; never overwrite existing backup.
2. Compare backup vs result for complete semantic preservation: scope, actors, obligations, modal strength, exceptions, ordering, approval/stop conditions, thresholds, examples, and cross-file relationships stay unambiguous.
3. Preserve headings, list hierarchy, fenced/inline code, commands, paths, URLs, identifiers, versions, numbers, environment variables, and other behavior-bearing literals exactly unless the task explicitly changes them.
4. Run affected Markdown, instruction-contract, and calibration gates. Broad instruction-set changes also require independent agent followability review against the pre-change backup.
5. If any instruction is lost, weakened, broadened, ambiguous, harder to navigate, or less reliably followed, reject the compression and restore the pre-change file. An unresolved comparison difference blocks completion.

## Multi-OS Executables — POSIX Assumption = Defect

Scripts, hooks, `bin/`, and CI steps all run on Linux, macOS, and native Windows. Fix at source; skip never.

The full rule now lives in the foundry plugin, delivered to Claude as `~/.claude/rules/foundry-python-code.md`: production portability in `foundry:rules/python-code.md` §Multi-OS Executables (`pathlib`, `as_posix()` before hash/compare, cross-host serialized paths via `PurePosixPath`/`PureWindowsPath`, `newline="\n"`, win32 `env=` keeping `SystemRoot`/`COMSPEC`/`PATHEXT`/`TEMP`/`TMP`, `shell: bash` for `.sh` CI steps, capability degradation) and test-side portability in `foundry:rules/python-testing.md` §Cross-OS Tests (capability-probe skips over blanket `skipif(sys.platform == "win32")`, collection-time skip decorators only, and the recurrent-defect guard for simulated-OS tests). `AGENTS.md` §Multi-OS Executables carries the same rule verbatim for Codex, which does not receive foundry rules.

No repo-specific addition beyond the two `.sh` files named as legacy debt in `plugins/CLAUDE.md` §Installability.

## Benchmark Isolation

Benchmark task IDs, target repositories, prompt wording, expected answers, and task-specific source or symbol examples are test evidence, not production content.

Never copy them into shipped plugins, skills, templates, or user-facing docs; use neutral generic examples and encode the generalized contract in a regression test instead.

## Plan Isolation

Plans, reports, scratch artifacts, and private implementation notes are evidence, not production content.

Never copy plan-only notation, section references, task IDs, private source or code examples, plan-only placeholder names, or private shorthand into shipped code, plugins, skills, templates, schemas, or user-facing docs, and never make a shipped artifact depend on access to its originating `.plans/` or `.reports/` context.

Re-express every adopted requirement as a self-contained contract with complete or sufficiently descriptive names, neutral examples, and all context needed to understand and verify it without the originating plan.

## Memory Policy

Nothing to auto-memory (`~/.claude/projects/.../memory/`). Learnings → skills, agents, rules, plugin files (versioned, distributed with plugin).

- New rule/guideline → edit `plugins/*/skills/*/SKILL.md`, `plugins/*/agents/*.md`, or `plugins/*/rules/*.md`
- Lesson/correction → update governing skill/agent/rule
- Never write to MEMORY.md or create memory files
