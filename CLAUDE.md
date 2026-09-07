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

## Python Documentation Style

- A docstring's opening line must state the documented object's purpose in plain English. Move formulas, assignments, configuration literals, function-call notation, and other code-shaped details into the following description or a relevant section.

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

- `pathlib`; `Path(p).is_absolute()` not `startswith("/")`; `PurePath(p).as_posix()` before hash/serialize/compare — separators change digests
- POSIX-absolute literals unportable as fixtures: `/host/x` → `D:\host\x` on Windows
- Serialized telemetry/provenance paths = cross-host coordinates, not local paths: preserve exact string; recognize POSIX + Windows absolute form with `PurePosixPath` + `PureWindowsPath`; never host-`Path` before exact compare; regression both forms every host
- Byte-asserted or hashed writes: `newline="\n"` or bytes — text mode emits CRLF
- Sanitized subprocess `env=` keeps `SystemRoot`, `SYSTEMROOT`, `COMSPEC`, `PATHEXT`, `TEMP`, `TMP` on win32 — else child Python aborts: `_Py_HashRandomization_Init: failed to get random numbers`; temp dir via `os.environ.get("TMPDIR") or tempfile.gettempdir()`, never `/tmp`
- CI `run:` calling `.sh` needs explicit `shell: bash` — Windows pwsh dot-sources it, exits 0, runs nothing (false green)
- Symlink/mode/uid = capabilities: degrade in production code first
- Skip last resort: never blanket `skipif(sys.platform == "win32")` — probe capability, skip on `OSError`; document + re-audit each surviving skip
- Test skips are collection-time decorators only (`pytest.mark.skipif`, `pytest.mark.skip`, parametrized marks); never call `pytest.skip()` from a test or fixture body
- Green macOS ≠ Windows support: prove with `PureWindowsPath`/`ntpath`; monkeypatching `os.name` does not change `pathlib`
- **Recurrent defect guard**: cross-OS simulations supply every host-only API/constant they exercise. Missing surfaces such as `os.killpg`/`signal.SIGKILL` use `monkeypatch.setattr(..., raising=False)`; the regression first uses `monkeypatch.delattr(..., raising=False)` to prove absence. Run the simulated branch on every host — no OS skip.

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
