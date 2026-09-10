# Repository Agent Instructions

<!-- policy-sibling-sync: CLAUDE.md, AGENTS.md, plugins/AGENTS.md, plugins/CLAUDE.md -->

- Any policy change in one listed instruction file must trigger a relevance review of every other listed file before completion.
- Synchronize applicable shared policy in either direction; preserve intentional agent-specific differences and record when no counterpart change is needed.

## Instruction Layering

- Repository-wide policy belongs in this top-level file.
- Lower-scope instruction files inherit it and must add only narrower rules or explicit exceptions, never repeat the same policy; when a top-level policy changes, review lower layers for conflicts or obsolete duplication rather than copying the new text into them.

## Edit Scope

- All edits stay inside this project directory. Never edit `$CODEX_HOME` or `~/.claude/` directly — both are install targets populated from this checkout, and a hand-edit there is overwritten on the next sync.
- Permitted roots: `.codex/` (Codex config, skills, session policy), `.claude/settings.json` and `.claude/settings.local.json`, `plugins/*/{agents,skills,rules,hooks,bin}/`.
- The Makefile installs from the pushed GitHub remote, not the local working tree: commit and push first, then `make sync-claude` or `make sync-codex`. Running it against uncommitted work silently installs the previous state.
- Never initiate propagation mid-task; it is a deliberate human-triggered step.

## Core Principles

Start every user-facing message with a short plain-English explanation of the outcome, situation, or requested action before technical details. Apply this to progress updates, questions, approval requests, errors, blockers, handoffs, and final answers. Keep later evidence precise; do not prepend prose to machine-only payloads or violate an explicitly requested exact output format.

Simplicity and reliability come first. Understand the affected flow and root cause, then prefer the smallest clear, reversible solution that satisfies the verified contract. Prefer established project patterns, standard tools, and deletion over new abstractions, dependencies, configuration, or layers; add complexity only when current evidence proves it necessary.

Verification is part of implementation. Work is not complete until relevant checks pass and failures, residual risks, and deliberately deferred scope are reported accurately.

## Python Record Types

- Prefer dataclasses for reused, fixed-shape internal records to clarify contracts and reduce field-name mistakes.
- Keep dictionaries for dynamic keys, external JSON, and simple mappings. Shared types modules must reduce real complexity.
- Preserve runtime validation, behavior, and serialized schemas; annotations alone do not enforce types.

## Python Documentation Style

- A docstring's opening line must state the documented object's purpose in plain English. Move formulas, assignments, configuration literals, function-call notation, and other code-shaped details into the following description or a relevant section.

## Multi-OS Executables

Scripts, hooks, `bin/` entry points, and CI steps all run on Linux, macOS, and native Windows. A POSIX-only assumption is a defect to fix at the source, never a reason to skip the platform.

- `pathlib`; `Path(p).is_absolute()` not a leading-slash check; `PurePath(p).as_posix()` before hashing, serializing, or comparing a path — native separators change the digest.
- POSIX-absolute literals are not portable fixtures: `/host/x` resolves to `D:\host\x` on Windows.
- Serialized telemetry or provenance paths are cross-host coordinates, not local paths: preserve their exact string; recognize declared POSIX and Windows absolute forms with `PurePosixPath` and `PureWindowsPath`; never convert them through host `Path` before exact comparison. Regressions must exercise both forms on every host.
- Byte-asserted or hashed writes use `newline="\n"` or bytes; text mode emits CRLF on Windows.
- Sanitized subprocess `env=` keeps `SystemRoot`, `SYSTEMROOT`, `COMSPEC`, `PATHEXT`, `TEMP`, `TMP` on win32, else the child Python aborts before running; temp dirs via `os.environ.get("TMPDIR") or tempfile.gettempdir()`, never `/tmp`.
- A workflow `run:` step invoking `.sh` needs explicit `shell: bash` — the Windows default shell dot-sources it and exits zero, a false green.
- Symlinks, file modes, and uid checks are capabilities: degrade in production code first.
- Skips are the last resort: never a blanket `skipif(sys.platform == "win32")`, always a capability probe skipping on `OSError`, with each surviving skip documented and re-audited.
- Test skips must be collection-time decorators (`pytest.mark.skipif`, `pytest.mark.skip`, or parametrized marks); never call `pytest.skip()` from a test or fixture body.
- Green macOS is absence of regression, not Windows support: prove Windows semantics with `PureWindowsPath` or `ntpath`, since monkeypatching `os.name` does not change `pathlib`.
- Recurrent defect guard: a test simulating another OS must explicitly supply every host-only API and constant it exercises instead of assuming the runner exports them. For absent surfaces such as `os.killpg` or `signal.SIGKILL`, install test doubles with `monkeypatch.setattr(..., raising=False)` and use `monkeypatch.delattr(..., raising=False)` in the regression to prove the missing-attribute case; keep the simulated branch running on every host rather than adding an OS skip.

## Benchmark Isolation

- Benchmark task IDs, target repositories, prompt wording, expected answers, and task-specific source or symbol examples are test evidence, not production content.
- Never copy them into shipped plugins, Skills, templates, or user-facing docs; use neutral generic examples and encode the generalized contract in a regression test instead.

## Plan Isolation

- Plans, reports, scratch artifacts, and private implementation notes are evidence, not production content.
- Never copy plan-only notation, section references, task IDs, private source or code examples, plan-only placeholder names, or private shorthand into shipped code, plugins, Skills, templates, schemas, or user-facing docs, and never make a shipped artifact depend on access to its originating `.plans/` or `.reports/` context.
- Re-express every adopted requirement as a self-contained contract with complete or sufficiently descriptive names, neutral examples, and all context needed to understand and verify it without the originating plan.

## Focused Delegation

- Use the lowest-cost capable subagent for small, well-defined support work when the task splits into independent bounded workstreams and the expected time or cost saving exceeds coordination overhead.
- Give each subagent narrow file or evidence ownership, only the context it needs, and explicit acceptance gates; parallelize disjoint work and never assign duplicate investigation or overlapping edits.
- Keep indivisible or very small work in the main agent.
- The main agent owns integration, reviews every handoff against its gates, resolves conflicts, and retains final acceptance for behavior-changing or executable results.

## Markdown Policy

- Never hard-wrap prose in any Markdown file.
- Keep each prose paragraph on one physical line; preserve intentional structural breaks in headings, lists, tables, blockquotes, links, HTML `<details>` blocks, and fenced code.
- Do not blindly unwrap or reflow a whole file; edit only the intended prose and retain its surrounding structure.

Structure Markdown for scanning and correct execution, not from line length alone. When one paragraph combines multiple actions, conditions, actors, statuses, exceptions, or decision branches, use the smallest fitting structure:

- Parallel obligations or independently checkable facts → bullets.
- Ordered actions, recovery paths, or state transitions → numbered lists.
- Compact closed mappings or comparisons with repeated fields → tables; keep long causal explanations out of table cells.
- Genuine notes, warnings, interpretation limits, or safety boundaries → blockquotes.
- Optional depth that would interrupt the main path → an existing or justified `<details>` block.
- Keep causal reasoning and cohesive rationale as prose.
- Do not convert paragraphs wholesale, add headings for every rule, or duplicate an existing navigation system.
- Keep headings concise and move detailed contracts below them.
- When reformatting behavior-sensitive agent, skill, setup, approval, or recovery instructions, preserve modal language, exact literals, ordering, and stop conditions; run the affected contract and calibration gates because formatting can change instruction salience even when the words remain similar.

## Lossless Instruction Compression Handover Gate

Compression or structural reformatting of any `AGENTS.md` or `CLAUDE.md` is behavior-sensitive and must pass every gate before handoff:

1. Save a verified byte-exact pre-change backup under `.codex/caveman-compress/backups/`, outside active instruction-discovery paths; never overwrite an existing backup.
2. Compare the backup and result for complete semantic preservation: scope, actors, obligations, modal strength, exceptions, ordering, approval and stop conditions, thresholds, examples, and cross-file relationships must remain unambiguous.
3. Preserve headings, list hierarchy, fenced and inline code, commands, paths, URLs, identifiers, versions, numbers, environment variables, and other behavior-bearing literals exactly unless the task explicitly changes them.
4. Run the affected Markdown, instruction-contract, and calibration gates. Broad instruction-set changes also require an independent agent followability review against the pre-change backup.
5. Reject the compression and restore the pre-change file when any instruction is lost, weakened, broadened, made ambiguous, harder to navigate, or less reliably followed. An unresolved comparison difference blocks completion.

Plugin-specific authoring, installability, cross-reference, versioning, and verification rules live in [plugins/AGENTS.md](plugins/AGENTS.md).

## Test Parametrization

- Keep single, simple `str`, `bool`, `int`, and `float` cases bare with pytest's default IDs; descriptive IDs alone do not justify wrappers. Use concise semantic IDs for generated oversized strings whose default IDs would be unreadable.
- Wrap every tuple case, including multi-argument rows, as `pytest.param(..., id="meaningful-case")`: pass row arguments separately; preserve a tuple-valued single argument as `pytest.param((...), id=...)`.
- Use `pytest.param` with semantic IDs for functions, containers, and other opaque/unstable objects; retain case-specific `marks=`. IDs describe behavior or intent, never memory addresses or object hashes.
- Never pass separate `ids=` to `pytest.mark.parametrize`—list, tuple, callable, or otherwise. Generated cases and mapping rows follow the same rules; attach each required ID to its case.
- Remove optional trailing commas that force short lists or calls onto multiple lines, then run the pinned Ruff hooks. Retain required tuple commas, comments, and Ruff-restored wrapping; skip ambiguous edits. Preserve values, argument unpacking, order, duplicates, marks, and assertions.

## Test Selection

- Use semantic markers: `integration` for real component interactions; `installed_plugin` for tests runnable without repository context; `packaging` for build/install/payload contracts; `live` for real external services or credentials.
- Apply markers directly to tests or homogeneous classes; never assign `pytestmark`, including lists of markers. Do not infer membership from paths, names, platforms, or mocked subprocesses. Ordinary tests may remain unmarked.
- Reuse repeated collection-time `pytest.mark.skipif(...)` conditions as named decorators such as `_skip_node_unavailable`; preserve predicates and reasons. This does not authorize new skips or weaken the capability-probe policy.
- `installed_plugin` does not imply `packaging`. A `live` test also carries `integration` and retains explicit opt-in guards; selection never authorizes network or paid execution. Capability probes remain separate.
- Register selectors in repository and shipped test configuration; validate with `--strict-markers`. Classify tests by their behavioral contract or execution requirements, never duration; use CI duration reports to investigate underperforming tests. Full CI remains unfiltered.
- From the repository root, use `.venv/bin/python -m pytest -m installed_plugin` or `.venv/bin/python -m pytest -m "packaging and not live"`; omit paths for project-wide discovery. On Windows use `.venv\Scripts\python.exe`.

## Project Workflow

- Python minimum: 3.10. The repository root is an environment anchor, not an installable package.
- Bootstrap test tooling with `uv sync --only-group test`; benchmark-only dependencies use `uv sync --only-group bench`.
- Run focused tests with `.venv/bin/python -m pytest <paths>` and broaden to the affected suite before completion.
- Lint/format edits via the pinned pre-commit hooks, never the bare tool: `pre-commit run ruff-check --files <changed-python-paths>`, `pre-commit run ruff-format --files <changed-python-paths>`, and `pre-commit run mdformat --files <changed-markdown-paths>`; direct `ruff` or `mdformat` invocation drifts from the version/config pinned in `.pre-commit-config.yaml`.
- Use `pre-commit run --all-files` only when the task requires the repository-wide gate; preserve unrelated working-tree changes.
- Release and build entry points are plugin-specific; follow `plugins/AGENTS.md` and the owning plugin's scripts and README. Remote publication remains human-owned.
