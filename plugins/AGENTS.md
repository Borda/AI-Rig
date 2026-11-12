# Plugin Authoring Rules

<!-- policy-sibling-sync: CLAUDE.md, AGENTS.md, plugins/AGENTS.md, plugins/CLAUDE.md -->

- A policy change in one listed file → relevance review of all other listed files before done.
- Sync applicable shared policy in both directions.
- Keep intentional agent-specific diffs and note when no counterpart change is needed.
- Apply these rules to every file under `plugins/`.
- `plugins/CLAUDE.md` remains the full authoring reference, and this file is the Codex-facing distillation.

Root `AGENTS.md` and the shipped Codex Rig global template (`plugins/codex-rig/assets/AGENTS.md`) already apply here and are not restated: edit scope (root), core principles, multi-OS executables, benchmark isolation, focused delegation, Markdown structure and no-wrap (template), and the test/lint workflow (both). This file adds only what is specific to authoring plugins.

## Plugin Workflow

- Run `.venv/bin/python -m pytest -q plugins/<name>` for the touched plugin; use focused test paths while iterating, then the full plugin suite before completion.
- OpenCode Rig runs on Node instead: use `npm --prefix plugins/opencode-rig run check` (or `make rig-check`) for the full gate and `npx vitest run tests/<path>` for a focused suite. It has no pytest module.
- When `plugins/<name>/scripts/build_package.py` and `validate_package.py` exist, build into a temporary directory and validate the produced package before commit.
- There is no shared automatic release command. Update the owning manifests and CHANGELOG, validate the package, and leave remote publication to the human workflow.
- Completion requires relevant tests, lint/format where applicable, package validation where available, README synchronization, the SemVer gate, and `git diff --check`.

## Plugin Tests

- Ruff (the pinned `ruff-check` pre-commit hook) enforces docstrings on every test module, class, method and function (`D100`–`D103`, tests only), the 120-character docstring line (`W505`), and the `pytest.skip()` ban (`TID251`). The remaining test-style rules live in foundry `rules/python-testing.md` §Mechanical Enforcement and Codex Rig `shared/global-baseline-details.md` §"Testing details" and are checked by review; fix the test, never add a custom checker script.
- Classify a check before writing it: if the test did not exist, could anything actually break undetected? If not, it is linting and belongs in a pre-commit hook or Ruff rule, not pytest.
- Shared test helpers and fixtures stay inside the same plugin's `tests/` directory, in `conftest.py` or an `_`-prefixed module. Never import across plugins: `installed_plugin` tests run from the installed plugin without the repository.
- Past 15 test files, a plugin's `tests/` groups into topic subfolders of 4 or more files; helpers and the tree-wide `conftest.py` stay at the plugin's `tests/` root. Full rule: foundry `rules/python-testing.md` §File Layout and Codex Rig `shared/global-baseline-details.md` §"Testing details".
- Shipped executables (`bin/`, hooks) stay independent, self-contained scripts and never import a test helper. Byte-identical propagated copies stay identical; a test's convenience never justifies diverging one.
- Real-tool tests — `integration`, `packaging`, `installed_plugin`, `live`, and end-to-end hook runs — stay real. Mock only boundaries the test does not cover, never for speed.
- Contract tests over shipped Markdown (skills, rules, agents) parse it line by line, per foundry `rules/python-testing.md` §Tests Reading Markdown or Prose. Never reflow or edit instruction Markdown to make a test pass.

## Change Consistency

- Before any plugin addition, edit, or deletion, ask which other uses share the same behavior or contract, then scan the owning plugin and other plugins for similar implementations, invocation patterns, consumers, documentation, tests, and calibration cases. Do not limit the scan to exact names or already-touched files.
- Record relevant counterparts before implementation. Apply the change to every counterpart that shares the contract, including removal of obsolete references after deletions; extend file ownership and verification scope explicitly when needed.
- Preserve intentional differences in behavior, permissions, platform support, and plugin independence. Similar wording alone does not justify identical changes, a shared abstraction, or a new cross-plugin dependency.
- Ask the user when evidence cannot establish whether a difference is intentional, the correct common behavior is ambiguous, or a counterpart requires authority outside the requested scope. Present the concrete counterparts and recommended treatment; continue unaffected authorized work.
- Before completion, repeat the scan for missed or stale uses, run affected contract/calibration/package checks, and summarize synchronized counterparts plus intentional exclusions with reasons. A confirmed applicable counterpart left inconsistent keeps the task incomplete unless the user explicitly defers it.

## Markdown Annotation Convention

- In plugin Markdown, write prose annotations, notes, and load directives as `>` blockquotes.
- Use `#` for real Markdown headings and, inside fenced `bash` or `python` blocks, code comments.
- Never use `#` as a fake prose comment or load directive because plain-text `#` breaks heading hierarchy.
- Comments in procedural code explain only WHY: non-obvious constraints, workarounds, incidents, or safety rationale.
- Comments in example or pattern code may also explain expected output, motivation, or when to apply the pattern.

## Installability

- Every file must work after `claude plugin install <name>@borda-ai-rig`.
- Assume only the installed plugin path, never the source tree.
- Do not hardcode sibling-plugin paths, `plugins/<name>/` directories, or absolute user or temporary paths.
- Each executable Claude block begins `PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name <plugin>) || exit $?; export PLUGIN_ROOT`, then calls helpers as `python "$PLUGIN_ROOT/bin/<name>.py"`. The host substitutes the plain token in skills and agents; loaders render it in loaded fragments. Never use defaulted tokens or cwd-relative bootstrap paths.
- The shared resolver selects the install record before validated cache versions; source fallback is last and restricted to its own AI-Rig checkout with cwd inside that checkout. Use a plugin loader or `render_plugin_root.py` for fragments containing executable bootstrap tokens; `cat` is only for token-free documents. Codex recipes retain their explicit portable-launcher policy.
- Codex agent waits follow [Agent Waits](codex-rig/shared/native-skill-contract.md#agent-waits), including blocking waits, status reporting and the owning workflow's deadline. Claude `Agent()` background notifications and health monitoring follow [Claude installability](CLAUDE.md#installability); they do not govern Codex waits.

## Independent Plugins and Cross-References

- Treat each plugin as independently installable: never use local or relative paths to another plugin.
- Use plugin-prefixed agent or skill names, such as `foundry:sw-engineer`.
- An agent `subagent_type` must match its filename.
- Cross-plugin calls and prose references must check whether the dependency plugin is available and degrade gracefully when it is absent.
- Annotate prose command references with (requires `<plugin>` plugin).
- Never propose a prerequisite-plugin or global-registration dependency as the resilience mechanism.

## Shared Files and Orphan References

- Each file added under a skill's `modes/`, `templates/`, or `_shared/` must either have its basename as a literal string in a consumer Markdown file or carry a `<!-- file: <basename> — consumers: ... -->` header.
- Add the consumer reference before creating the shared file.
- Manifested shared files are byte-identical copies: edit the canonical file, run `plugins/cc_foundry/bin/propagate_shared.py --apply`, and verify by running the script with no flag at all. Check mode is the default and has no flag; passing `--check` exits 2 with `unrecognized arguments`.
- Keep resilience code in the plugin whose users need the fallback, not in the plugin that may be absent.
- Every plugin resolves its own `skills/_shared` through its own resolver and reads only files it ships itself.
- Never use `$HOME/.claude/skills/_shared/...` or a bare `.claude/skills/_shared/...` path. `/foundry:setup` symlinks only `rules/*.md` and `TEAM_PROTOCOL.md`, and purges any leftover `~/.claude/skills/` link; a directory carrying `SKILL.md` there registers as a user-level skill and shadows Claude Code's bundled skill of the same name.
- Never read another plugin's `_shared` or `bin/` either (except the optional-provider instances and the loop-checker resolver below); duplicate the content into each plugin and add a `propagate_shared.py` MANIFEST entry instead of borrowing.
- One exception, the optional-provider contract: a consumer may read or execute a provider plugin's shipped file at runtime when the consumer's feature is a no-op without that provider, resolution goes through the active install record (`resolve_shared_path.py <provider> <subdir>`, registry tier first; the resolver's newest-cache tier is a degraded fallback reached only without a usable install record or when the recorded install lacks the subdir, and a wrapper never globs the cache itself), and absence degrades to the consumer's own fallback line. Current instances: `resolve_shared_path.py codemap-py claude-skills/_shared` reads the provider contract; `resolve_shared_path.py foundry bin` executes the provider's `compact_gate.py` rather than reading a file, with a same-line `[ -f "$_GD/compact_gate.py" ]` presence check and the consumer's own fallback echo `compact-gate: continue (foundry gate unavailable)`. Add each new instance to this enumerated list and audit Check 27. All three conditions apply to both. Content needed to survive a plugin's absence never qualifies.
- A second exception, the loop-checker resolver: `rules/_full/adversarial-loop.md` ships byte-identical in every Claude plugin (`propagate_shared.py` MANIFEST, canonical `plugins/codex-rig/shared/adversarial-loop.md`), so it cannot name its own plugin. Its Claude resolver globs the cache for any plugin's byte-identical `bin/adversarial_loop.py`, takes the newest by modification time whose version directory carries no `.orphaned_at`, and falls back to the source tree. Under mixed releases the pick may come from another release; newer checkers still read older ledger and actions schemas. No other rule, skill or wrapper may glob the cache for a sibling's `bin/`.
- Audit Check 27 enforces both halves.

## README Synchronization

- Any edit to `agents/`, `skills/`, `rules/`, or `hooks/` requires the owning plugin README to be updated before completion.
- Synchronize tables, descriptions, triggers, scope, NOT-for boundaries, hook behavior, user-facing arguments, invocation syntax, cross-plugin references, model-tier lines, relationship tiering, and relevant curator antipatterns.
- Propagate changed interfaces to every cross-plugin README that mentions them.

## Diagram Sync — animated SVG architecture diagrams

<!-- policy-sibling: plugins/CLAUDE.md §Skill Diagram Sync -->

A plugin may ship animated SVG diagrams of its runs, blocking gates, agent fan-out and any work that overlaps: one per multi-agent or multi-gate skill at `<plugin>/assets/images/<skill>-arch.svg`, and one plugin-level overview of how the plugin's parts connect at `<plugin>/assets/images/<plugin-topic>-architecture.svg`. Each is embedded in the matching README section. A diagram is **documentation, never a contract**: no skill loads it, and it carries no authority.

- `SKILL.md`, `modes/*.md`, `shared/` contracts, and source win on any disagreement. A diagram that contradicts them is a defect in the diagram, never a licence to change behavior to match the picture.
- Update the diagram in the same change as any edit to step order, gate placement, agent fan-out, spawn width, what runs beside what, a route or mode topology, or a process boundary, permission or transport a plugin-level diagram shows. Review every diagram that names the touched skill or component; record "no diagram change needed" when none applies. A stale diagram is worse than none.
- Edit the SVG text directly, keep every fact in `<text>` so it stays greppable, and render it once to confirm nothing overlaps. No script, external font or link inside the SVG: it must render inside an `<img>` on GitHub.
- Every diagram closes with a footer stating its non-normative status, the update obligation and its normative source.
- Each `SKILL.md` with a skill diagram carries one HTML comment after the frontmatter and before its first heading naming the SVG, so an editor meets the obligation at the point of edit.
- Claim an overlap only where the skill genuinely dispatches work in one wave. Never document aspirational parallelism.
- Format is a block schema, not a step list: named blocks flow top to bottom, `FAN n` marks concurrent lanes dispatched together and `JOIN` where they are collected; a split into mutually exclusive paths is a branch, never a `FAN`. Every arrow beyond plain "block finished, next block starts" carries a short pill naming its condition, bound or reason. A stage drawn more than once is a dashed `↺` reuse ghost of one solid home card. Keep fonts at 10.5px or larger.
- Cards stay short and structured: at most 4 fragment lines of about 45 characters (`label: value`, `A · B · C`), never sentences; longer detail goes into a compact table row or stays in the normative source. Mark the key noun, gate or artifact with a bold `<tspan>` (at most 2 per line) and qualifiers, conditions and bounds with an italic `<tspan>`; never bold a whole line. Emphasis stays inside the same `<text>` so every fact remains greppable.
- Optional per skill: add one where runs, gates and fan-out are hard to hold in the head, and skip it for a linear single-agent skill. A plugin without multi-agent skills ships the single plugin-level overview instead.
- Shipped Codex Rig files are digest-listed: after editing a diagram or the `SKILL.md` comment, regenerate its manifest with `build_package.py --update` and run `scripts/check_codex_private_refs.py`; write SVG path commands as `M 56,518`, never `M56,518`, since its work-item pattern `M\d{2}` flags the latter.
- Claude counterpart: `plugins/CLAUDE.md` §Skill Diagram Sync (same obligations; `SKILL.md` and `modes/*.md` are its normative sources, and there is no manifest regeneration).

## Python and `bin/` Policy

- Python is the default `bin/` language and requires Python 3.10+, type hints, a module docstring, an `if __name__ == "__main__"` guard, and ruff-format at 120 columns.
- OpenCode Rig is the TypeScript exception: its `bin/` entrypoint and release scripts are ESM `.mjs` with a Node engine floor declared in `package.json`, formatted by Prettier, and gated by `npm run check`. Do not add a Python launcher or use these files as a pattern for Python plugins.
- Interpreter bootstrap exception: the shared `bin/python` POSIX shell launcher and native Windows `bin/python.cmd`, plus Codemap runtime launchers, may select and validate Python before any Python code can run. Keep application logic in Python; bootstrap preserves arguments, streams, and exit status without workload retries.
- Pure functions use doctests; code that performs I/O, subprocesses, environment reads, or argv parsing uses pytest coverage in the adjacent `tests/` directory.
- Use `bin/` only for deterministic transforms such as argument parsing, path resolution, or one-value computation.
- Keep decision flow, branching prompts, and agent dispatch in SKILL.md prose.
- Use Bash only for install-path resolution, safe `$ARGUMENTS` parsing, or simple `find | sort | head` pipelines without business logic.
- Inline SKILL.md Python is limited to cases requiring JSON parsing, multiline string manipulation, or numeric computation.

## Growing Ledgers

- A file that only grows — `.jsonl` record logs, `.tsv` rows, `.log` files, diaries, journals, results ledgers — is appended to, never re-written; a read-modify-write round trip can silently drop or reword earlier records.
- Every instruction that adds to a ledger names its mechanism; "append", or "append with the Write/Edit tool", is not one.
- A model-valued record is staged as one record in `<ledger>.rec`, then appended by the plugin's own `bin/append_ledger.py "$LEDGER"`, which supplies a missing trailing newline and deletes the staged file. Do not use `cat … >> … && rm -f …`: `rm` is outside Claude's plugin allow list and blueprint manifest, so that form prompts on every append.
- Parallel writers stage separate `<ledger>.<writer>.rec` files, appended by one `append_ledger.py` call after all writers return. Values already in the shell are appended directly with `printf` or `jq -c` redirected with `>>`; files already on disk with `append_ledger.py "$LEDGER" --from <files>`.
- Corrections are appended as new event records and readers take the last record per key; values derived later go to a sidecar file, not back into the ledger.
- Exempt, where editing or rewriting is correct: single-value state, mtime-clock `agent-watch-*.tsv` batch files, compaction contracts, digest- or validator-bound rendered artifacts, dedup and merge reconciliation, the artifact being refined, and frozen plans.
- `tests/instructions/test_append_only_ledgers.py` enforces the instruction wording; `plugins/CLAUDE.md` §Growing Ledgers holds the full rationale.
- Codex Rig keeps the same `.rec` staging but has no `append_ledger.py`: the helper that owns a ledger's schema appends and validates the staged record (`codex-rig/shared/native-skill-contract.md` §Append-Only Ledgers). Intentional difference — Codex has no permission matcher or blueprint manifest to satisfy, and per-schema validation rejects a bad record before it lands.

## Version Pre-Bump Gate

- Apply the repository Version Continuity rule to serialized artifact schemas separately from this plugin SemVer gate; compare each schema family with its own committed `HEAD` baseline before handoff.
- Every commit touching a plugin's non-test file must apply exactly one SemVer bump to that plugin from its HEAD baseline (from the commit's parent when amending or rewriting a commit that already bumps — root `AGENTS.md` §Version Continuity), except a generated package manifest updated only to record test-file bytes.
- A commit changing only files under `plugins/<name>/tests/` needs no bump. When the plugin ships its tests, regenerate and validate the package manifest for those test bytes without bumping the version or updating the changelog.
- Pure test, CI, and documentation changes need no changelog entry unless they change shipped product or plugin behavior.
- Every changelog bullet states what changed and its user-visible impact. A bare file or mechanism name ("Sync report-header-table.js", "update helper", "refactor") says nothing; name the behaviour, and trace each claim to the diff.
- Changelog shape is a plain list: every line of a section is a `- ` entry or a sub-entry of one, with no free paragraph before, between or after entries and no blank lines between items. One entry per topic: a few related sentences saying what changed and its user-visible impact, compact but complete. Sub-bullets only enumerate parallel items. No rationale, implementation mechanics or long example lists; compress, never drop a change. Match the released sections below.
- Changelog target section: new entries go only into the section of the pending commit's own version — the `## <version>` heading equal to the bumped value, or a declared `Unreleased` section when the file has one — at the top. Pick it from `HEAD`, never from the nearest surrounding text: a section whose heading already exists in `HEAD`'s changelog (the top one included) is released history, so open the new heading above it instead of appending; when amending a commit that already carries the bump, its baseline is that commit's parent and its own top section is the pending one. Never add or reword a bullet in an older section; a change that needs one is a new bullet in the pending section.
- Each touched plugin is evaluated independently, and one commit applies at most one bump per plugin.
- Before changing a version, run `git diff HEAD --name-only -- plugins/<name>/` and stop with no bump when every changed path is under `tests/`, or the only additional path is the generated package manifest updated for those tests.
- Otherwise read the baseline with `git show HEAD:plugins/<name>/.claude-plugin/plugin.json | grep version` and the on-disk version with `grep version plugins/<name>/.claude-plugin/plugin.json`; stop if disk already differs from HEAD.
- Classify patch (`Y`) for a fix, wording change, refactor, cleanup, or restoration of intended behavior.
- Classify minor (`X`) for a new capability, agent, skill, or designed behavior, resetting patch to zero.
- Test-only changes need no bump.
- Calculate from HEAD and write exactly that single bump; never increment from a previously bumped on-disk value.
- After calculating the bump, update every shipped runtime manifest, including `.codex-plugin/plugin.json` when present, so it shares the bumped version with `.claude-plugin/plugin.json`.
- OpenCode Rig is the npm exception: it ships no host manifest, so `package.json` carries the version, `rig.json`, `src/version.ts`, and the README marker mirror it, and `node plugins/opencode-rig/scripts/sync_version.mjs` writes all four from the same HEAD-derived bump.
- Update CHANGELOG or release metadata only when a change affects shipped product or plugin behavior and the plugin convention requires it.

## Permission Gates

- A feature that adds a permission gate, approval question or stop first diffs its gate list against `HEAD` behaviour; an operation that ran without a stop at `HEAD` keeps running without one unless the user asked for the new stop.
- Default to relaxed; tighten only with a concrete, user-accepted justification. Push stays always-ask and force push forbidden; no other git class gains an always-ask stop.
- `plugins/CLAUDE.md` §Permission Gates — Diff Against HEAD holds the Claude counterpart.

## Shell-Text Guards — What Not To Do

Repository-maintainer rules for the opencode-rig shell guard; not applicable to shipped user content.

- Never enumerate bypass strings to grow the guard: repeated reviews grew it from about 328 to about 2800, then about 3400 lines and kept finding holes in new spellings.
- Add quiet classes only with a positive model: a short explicit allowed-program-and-option table, never a known-bad deny-list, because quiet needs understood behaviour.
- Say in user docs: text classifier = speed bump, not security boundary; further hardening needs sandboxing or isolation because string rules cannot enforce it.
- Fix a review budget first; when spent, document limits and open gaps instead of widening, because spelling enumeration does not converge.
- Read-only entries never gain write options; programs that write or run code leave the list, because names do not establish safety.
- Resolve program identity in one function for every lookup; no second raw-spelling match, because divergent identity creates bypasses.
- No round, finding, probe or task numbers in test names, titles, labels, markers or comments; name by scope and behaviour and merge into scope-named files, because evidence must stand alone.

## Verification

- Before editing policy, inspect the document-level `policy-sibling-sync` contract and any section-specific `policy-sibling` marker.
- Review every listed instruction sibling even when no synchronized edit is ultimately needed, and update applicable shared policy in either direction.
- Before completion, verify the intended README and cross-references.
- Run `plugins/cc_foundry/bin/propagate_shared.py` with no flag when shared files are involved — check mode is the default.
- Run `check_orphaned_bin.py` when `bin/` files are involved.
- Before a mass edit of shipped files, search the repo for the sha256 of each target's `HEAD` bytes (`git show HEAD:<path> | shasum -a 256`), not its git blob OID, excluding regenerated manifests; leave digest-pinned files untouched (`plugins/CLAUDE.md` §Edit Quality Gate).
- Run the relevant tests or lint checks.
- Run `git diff --check` on every owned file and confirm added prose paragraphs are not hard-wrapped.
