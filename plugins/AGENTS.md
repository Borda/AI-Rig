# Plugin Authoring Rules

<!-- policy-sibling-sync: CLAUDE.md, AGENTS.md, plugins/AGENTS.md, plugins/CLAUDE.md -->

- A policy change in one listed file → relevance review of all other listed files before done.
- Sync applicable shared policy in both directions.
- Keep intentional agent-specific diffs and note when no counterpart change is needed.
- Apply these rules to every file under `plugins/`.
- `plugins/CLAUDE.md` remains the full authoring reference, and this file is the Codex-facing distillation.

Root `AGENTS.md` already applies here and is not restated: edit scope, core principles, multi-OS executables, benchmark isolation, focused delegation, Markdown structure and no-wrap, and the test/lint workflow. This file adds only what is specific to authoring plugins.

## Plugin Workflow

- Run `.venv/bin/python -m pytest -q plugins/<name>` for the touched plugin; use focused test paths while iterating, then the full plugin suite before completion.
- When `plugins/<name>/scripts/build_package.py` and `validate_package.py` exist, build into a temporary directory and validate the produced package before commit.
- There is no shared automatic release command. Update the owning manifests and CHANGELOG, validate the package, and leave remote publication to the human workflow.
- Completion requires relevant tests, lint/format where applicable, package validation where available, README synchronization, the SemVer gate, and `git diff --check`.

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
- Use `~/`, `$(git rev-parse --show-toplevel)`, or `$CLAUDE_PLUGIN_ROOT` as appropriate.
- Use a bare `plugins/` path only as the final fallback after resolving the installed cache path.
- A skill that starts background agents must provide the sentinel, five-minute polling, and fifteen-minute cutoff required by the shared agent-spawn protocol.

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
- Never read another plugin's `_shared` or `bin/` either; duplicate the content into each plugin and add a `propagate_shared.py` MANIFEST entry instead of borrowing.
- Audit Check 27 enforces both halves.

## README Synchronization

- Any edit to `agents/`, `skills/`, `rules/`, or `hooks/` requires the owning plugin README to be updated before completion.
- Synchronize tables, descriptions, triggers, scope, NOT-for boundaries, hook behavior, user-facing arguments, invocation syntax, cross-plugin references, model-tier lines, relationship tiering, and relevant curator antipatterns.
- Propagate changed interfaces to every cross-plugin README that mentions them.

## Python and `bin/` Policy

- Python is the default `bin/` language and requires Python 3.10+, type hints, a module docstring, an `if __name__ == "__main__"` guard, and ruff-format at 120 columns.
- Pure functions use doctests; code that performs I/O, subprocesses, environment reads, or argv parsing uses pytest coverage in the adjacent `tests/` directory.
- Use `bin/` only for deterministic transforms such as argument parsing, path resolution, or one-value computation.
- Keep decision flow, branching prompts, and agent dispatch in SKILL.md prose.
- Use Bash only for install-path resolution, safe `$ARGUMENTS` parsing, or simple `find | sort | head` pipelines without business logic.
- Inline SKILL.md Python is limited to cases requiring JSON parsing, multiline string manipulation, or numeric computation.

## Version Pre-Bump Gate

- Every commit touching a plugin's non-test file must apply exactly one SemVer bump to that plugin from its HEAD baseline, except a generated package manifest updated only to record test-file bytes.
- A commit changing only files under `plugins/<name>/tests/` needs no bump. When the plugin ships its tests, regenerate and validate the package manifest for those test bytes without bumping the version or updating the changelog.
- Pure test, CI, and documentation changes need no changelog entry unless they change shipped product or plugin behavior.
- Each touched plugin is evaluated independently, and one commit applies at most one bump per plugin.
- Before changing a version, run `git diff HEAD --name-only -- plugins/<name>/` and stop with no bump when every changed path is under `tests/`, or the only additional path is the generated package manifest updated for those tests.
- Otherwise read the baseline with `git show HEAD:plugins/<name>/.claude-plugin/plugin.json | grep version` and the on-disk version with `grep version plugins/<name>/.claude-plugin/plugin.json`; stop if disk already differs from HEAD.
- Classify patch (`Y`) for a fix, wording change, refactor, cleanup, or restoration of intended behavior.
- Classify minor (`X`) for a new capability, agent, skill, or designed behavior, resetting patch to zero.
- Test-only changes need no bump.
- Calculate from HEAD and write exactly that single bump; never increment from a previously bumped on-disk value.
- After calculating the bump, update every shipped runtime manifest, including `.codex-plugin/plugin.json` when present, so it shares the bumped version with `.claude-plugin/plugin.json`.
- Update CHANGELOG or release metadata only when a change affects shipped product or plugin behavior and the plugin convention requires it.

## Verification

- Before editing policy, inspect the document-level `policy-sibling-sync` contract and any section-specific `policy-sibling` marker.
- Review every listed instruction sibling even when no synchronized edit is ultimately needed, and update applicable shared policy in either direction.
- Before completion, verify the intended README and cross-references.
- Run `plugins/cc_foundry/bin/propagate_shared.py` with no flag when shared files are involved — check mode is the default.
- Run `check_orphaned_bin.py` when `bin/` files are involved.
- Run the relevant tests or lint checks.
- Run `git diff --check` on every owned file and confirm added prose paragraphs are not hard-wrapped.
