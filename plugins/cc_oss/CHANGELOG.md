# Changelog

## 0.52.3

- Adversarial review loops treat a `foundry:challenger` round that returns no deliverable or a `blocked` envelope as an agent failure, never a silent `not-run`: the cause is recorded and the round re-dispatched once. Security-filter reviews keep Codex enabled, so the challenger runs the bypass search through its Codex cross-check and needs no separate Codex row.
- The file-handoff protocol lists the report names Claude Code refuses for a subagent Write (`findings.md`, `report.md`, `summary.md`, `analysis.md` and suffixed `findings-*.md`), so an agent names its file after itself and the report is no longer lost.

## 0.52.2

- The runtime `blueprint-allow.js` hook refuses, even on a manifest hit, every command that can point a command name at another program: `hash`, `builtin`, `alias`, `enable`, `functions`, `eval` and their kin; any mention of the zsh `commands`/`functions`/`aliases` or bash `BASH_CMDS`/`BASH_ALIASES` tables, attached option prefixes included (`set -Acommands`); `set -A`, `read -a`, `printf -v`, name references and indirect expansions. Spellings that slipped past the earlier per-spelling checks, such as `set -Acommands ruff /bin/echo` and `builtin -- hash -p …`, are now refused.
- The same hook reads each command as the shell's own words before judging it: quotes, backslash escapes, `$'…'` strings and line continuations are resolved and joined, and redirections are set aside with their targets. `\rm`, `r""m`, `h$'a'sh`, `\-v` and `co""mmands` therefore count as `rm`, `hash`, `-v` and `commands`, and a redirection such as `printf 2>/dev/null -v …` or `2>"a b" set -A …` no longer hides the option or the command. A command word written as a pattern that can expand to a refused name (`/bin/r?`, `{ha,}sh`) is refused. The hook reads past the zsh precommand modifiers `noglob`, `nocorrect` and `-` and past a wrapper's own arguments (`timeout 5 set -A …`), and it judges the command text carried by a shell's `-c` (after options such as `-o shwordsplit`), a `trap` action, `watch` and `env -S`, so `noglob rm -rf x` and `zsh -o shwordsplit -c 'set -A …'` are refused. An option written after another option's argument (`set -o noglob -A …`, `read -t 5 -a …`, `print -f '%s %s' -v …`) still counts. Text the reader cannot settle is refused rather than guessed: a command that ends inside an unclosed quote; a `$'…'` escape bash and zsh decode differently, wherever it stands, assignment and redirection target included (a NUL byte, `\u…`, `\c…`, an unknown letter such as `\m`, an octal value above `\377`); an expansion inside a command word (`h${x}sh`); and a pattern among a wrapper's words or a POSIX bracket class in a command pattern (`command h[a]sh …`, `/bin/r[[:lower:]]`). A shell's `-c` script written after `--` is judged like any other. Short options are read as clusters, so `zsh -co shwordsplit '…'`, `zsh -xo name -c '…'` and `env -iS '…'` still reach the carried script, and every argument after a `-c` cluster is judged as script text. A `/` inside an expansion (`h${x:-/}sh`) no longer counts as a path separator. `unset -f`, `-F` or `-m`, which remove a function that shadows a command, is refused like `unalias`. A `-c` string counts as a bare variable (`bash -c "$METRIC_CMD"`) only when it is exactly that variable, so invisible Unicode whitespace around it no longer makes the hook and the generator disagree. An expansion where `printf`, `print`, `set`, `read`, `unset` or a declaring builtin still reads options (`printf "$o" …`) is refused, since its value may be `-v` or `-A`. A quoted or escaped prefix (`'h'${x}sh`) no longer hides an expansion inside a command word, and a bracket class holding a quote or backslash (`h[\!a]sh`) is matched as any one character. The command after a zsh short or brace form (`for x (a) cmd`, `select x (a) cmd`, `if true { cmd }`, `while … { cmd }`, `repeat 1 cmd`) or after bash `coproc` is judged like any other command. The `&` of `2>&1` or `&>` no longer splits a command in two, an escaped quote in `$'\''` no longer ends the string early (also when a line continuation splits `$` from `'`), a parenthesis inside a quoted string no longer ends a `$(…)`, and a `{fd}>` redirection is set aside like `2>`.
- The blueprint hook refuses a command whose raw text has a backslash-escaped blank at a line end or before `#`. The shell ends the command there, or keeps the rest of the line as an argument, while normalization would read a continuation or a comment, so a shipped digest could approve a different command. No shipped block contains the shape.
- The sentinel-read shape lane judges every word a `read` receives: a sensitive variable such as `PATH` named anywhere, attached to an option (`-aPATH`) or after an option's argument, or a computed name (`read "$V"`) refuses the command. Only the first name was checked before, so `read -r IGNORE PATH < file` could rebind command lookup without a prompt.
- The plugin's blueprint manifest no longer auto-allows a line that is not a complete command on its own, a block that writes heredoc data, a block that sources a state file, or one that runs a variable's value as a command; those run after a prompt.
- Review and resolve read their `KEY=value` state files (flags, gate, mode flags, `pr-vars.sh`) as data instead of sourcing them, so a value carrying a PR's CI check names or an agent-written line can no longer run as a command inside an auto-allowed block. The resolve reader also keeps a final line written without a trailing newline.

## 0.52.1

- Follow the active Codemap provider’s automatic missing/stale-index preparation contract, preserving explicit confirm-first and frozen preferences across later tool calls and nested query helpers.
- Review and resolve test selection honor the owning invocation’s disabled Codemap choice while retaining heuristic test plans and required verification.

## 0.52.0

- Helper scripts that write or read a session sentinel (`parse-skill-flags.py`, `resolve_preflight.py`, `release_setup.py`, `extract_vitality_vars.py`, `detect_codemap.py` and the other `bin/` sentinel helpers) fall back to `/tmp` on POSIX when `TMPDIR` is unset, the directory a skill's `${TMPDIR:-/tmp}` names, instead of `tempfile.gettempdir()`, which picks `TEMP` or `TMP` first; with either set, script and skill read different files. Native Windows keeps `gettempdir()`.
- Keep supported review source judgments separate from blocked verification and include the reviewed full commit SHA in report headers; retain explicit unavailable identity for unversioned or historical abbreviated sources.
- The runtime `blueprint-allow.js` hook treats `uv run` and `poetry run` as carrying their wrapped command and flags `hash name=/path`, `hash -p /path name` (also the attached `-p/path` and bundled `-rp` spellings), `builtin hash …` and any write to the zsh `commands` / bash 4 `BASH_CMDS` tables (`commands[x]=`, `commands=(…)`, `+=`, `${…:=…}`, `printf -v`, `read -A`, or a write nested in a lookup subscript such as `${commands[$(…)]}`; a plain `${commands[x]}` or `${commands[$name]}` lookup stays allowed), so a destructive command behind one of those, or a rebound command name, is refused even on a manifest hit.
- `extract_vitality_vars.py --keep-state` stores a multi-line axis signal on one line, because the sentinel reader keeps only the first line; the whole signal reaches the report.
- `/oss:analyse` vitality mode reads the extracted scores, confidences and weights from per-session sentinel files written by `extract_vitality_vars.py --keep-state` instead of running `eval` on the script's output; the script's default shell-assignment output is unchanged.
- `Bash(eval:*)` is no longer in the shipped allow list and `/oss:setup` removes it from `settings.json`, including an identical entry added by hand: ad-hoc `eval` commands now prompt, while shipped blocks no longer use `eval`.

## 0.51.1

- `/oss:resolve` treats every finding in a review's `### [blocking] Critical` section as required work at any severity, and `/oss:review` places `[blocking]`-tagged findings (such as Block-tier test gaps) there at their own severity, so they no longer drop to optional `[suggest]` items.
- `/oss:resolve` folds root-cause-merged review rows (`merged_into`) into the survivor's action item: the item lists `merged_finding_ids` and every symptom, takes the group's highest severity and `[req]` routing, matches a GitHub comment on any symptom's line, and gets one `resolution.jsonl` record per folded finding; a `merged_into` naming no single survivor keeps the row as its own item with a `⚠` warning, so no confirmed finding is dropped or dispatched twice.
- The `/oss:review` publication gate refuses a non-text `merged_into`, so a malformed report fails at review time instead of hard-blocking `/oss:resolve`; the review consolidator keeps each merged row's own severity and section.
- `oss:cicd-steward` rates findings on a consequence-based severity table and reports `ubuntu-latest` drift only when the prompt names runner reproducibility; `oss:gh-scraper` and `oss:repo-warden` stop on off-shape input with a single error or `declined` envelope, and `/oss:analyse` vitality surfaces the decline reason instead of a timeout.
- `oss:shepherd` applies absent-from-input checks only to partial input and rates a feature shipped in a patch as its own medium finding; `/oss:release` lowers classification confidence when the Truth check is skipped with any Removed or Breaking Changes item.
- The review checklist anchors documented-contract violations at MEDIUM, and the shared file-handoff protocol adds the `unsupported` decline status.

## 0.51.0

- `/oss:resolve` report gate (`find_review_report.py`) and findings merge (`merge_action_items.py`) write stdout and stderr as UTF-8, so on native Windows a `! BLOCKED —` message or a non-ASCII report path no longer reaches the calling shell block as undecodable console bytes.
- Narrow broad `find`, `node`, `claude`, and `sed` allows while retaining scoped read-only rules and broad `eval`/`python` for existing recipes; ad-hoc commands outside shipped blocks now prompt unless a narrower rule covers them. Setup removes the old broad entries from `settings.json`, including an identical entry added by hand.
- Keep-state parsing rejects unsafe sentinel identities before changing state, and bulk review commits sanitize PR references and bullet contents so embedded control characters cannot forge resolve markers or co-author trailers.
- `/oss:resolve` stops with `! BLOCKED — not a pull-request URL` when given a GitHub issue or repository URL, instead of continuing with an empty PR number that every later PR step silently skips.
- `/oss:setup` validates settings with Python instead of jq, so machines without jq keep a valid permissions merge rather than restoring its backup; malformed JSON still triggers restoration.
- `/oss:release audit` and `/oss:resolve` run version comparisons, commit-SHA extraction and deferred-file overlap checks through plugin-owned Python helpers, preserving their results and failure guards while avoiding multi-line shell quoting.
- `parse-skill-flags.py` no longer drops a `--keep` value whose quotes the shell consumed when the host substituted `$ARGUMENTS` (`--keep "a b"` used to arrive as `--keep a b` and persist an empty keep list); it now also accepts `--keep 'a b'` and `--keep="a b"`, strips a dangling `--keep` from `CLEAN_ARGS`, and warns on stderr when the value was unquoted or empty, so a lost compaction keep list is visible instead of silent.
- `parse-skill-flags.py --keep-state <prefix>` writes one per-session sentinel per parsed flag, value, `CLEAN_ARGS` and `--keep` value, and clears a crashed run's stale compaction contract only when no other session owns it (`--inlined` skips the clear); output without the option is unchanged. `/oss:analyse`, `/oss:resolve`, `/oss:review` and `/oss:release` read their flags back with `IFS= read -r` instead of running `eval` on script output, the contract clear in analyse, resolve and review no longer deletes a contract another session in the same directory owns, and the release audit checks reuse Mode Detection's parse instead of running the parser a second time.
- `resolve_shared_path.py` without a registry or cache hit uses the cwd-relative `plugins/cc_oss/...` (or a provider's `plugins/<name>/...`) tree only inside that plugin's own source repository, where its `.claude-plugin/plugin.json` names it. Elsewhere it exits 1 with a `!` message instead of printing the path, so a repository under review can no longer supply oss protocol docs or the codemap-py contract.
- `heal_git_artifacts.py worktrees` keeps unregistered directories unless `--include-orphans` is passed, re-checks one right before deleting it, reports symlinked children as skipped and rejects a `*` managed prefix. The worktree-isolation leftover report and its approved removal both pass `--include-orphans`, so the list you approve and what gets removed stay the same.
- `check_oss_pr_signals.py` prints secret matches as `path:line: kind` and never the credential value; line numbers are hunk-aware and removed lines are marked. The removed-export check matches whole identifiers instead of substrings and caps at 500 identifiers. A `--latest-tag` starting with `-` exits 1 and an `--output-file` symlink is refused.
- `fetch_gh_data_group1.py` requests secret-scanning alerts with `hide_secret=true` and drops the `secret` field; repository parts `.` and `..` and malformed cutoff dates are rejected, and a timed-out fetch is non-fatal.
- New `bin/gh_batch_query.py`: `commit-prs` maps a Git range to its merged PRs and `pr-files` lists the changed files of several PRs, 25 per GraphQL request, resumable with `--offset-file`. `/oss:release` gather and the codemap signals use it instead of one `gh` call per commit or PR; the release gather handles up to 500 commits per run and resumes from `NEXT_OFFSET`.
- New `bin/resolve_changelog_file.py` resolves the changelog path (root, `docs/`, first one-level `CHANGELOG*`, else `CHANGELOG.md`) for release notes, prepare and audit, replacing the `find | head -1` lookups.
- `resolve_pr_refs.py` rejects head and base branch names git would read as an option and gains `--local-base`, the no-PR path for report mode and comment dispatch that publishes only the default branch. `derive_fork_remote.py` validates remote and ref names and gains `--fork-repo` for a fork whose repository name differs from origin's.
- `commit_action_item.py`, `commit_all_items.py` and `commit_lint_fixes.py` use `--literal-pathspecs` and bounded git timeouts, so a changed file named like a glob no longer sweeps unrelated edits into a bot commit; the lint-fix commit is limited to dirty files inside the PR range and refuses without the base sentinel.
- `resolve_test_plan.py` accepts only a plain runner command from project docs (no shell metacharacters, no `python -c`) and falls through to the Makefile or fallback otherwise. `build_triage_batch.py`, `build_vitality_paths.py`, `detect_thread_type.py`, `extract_changed_symbols.py` and `extract_contributors.py` escape or reject option-shaped and malformed input.
- `release_setup.py` creates its sentinel directory `0700` and exits 1 when it is a symlink or owned by another user; `release_append_marker.py`, `setup_release_dir.py` and `release_append_publish.py` write atomically and publish only from their own stage directory. `run_audit_checks.py` skips symlinks and paths outside the root and fences matched lines as untrusted data.
- `resolve_centrality.py`, `review_impact_tier.py`, `classify_breaking.py`, `search_downstream_consumers.py`, `mint_finding_ids.py`, `vitality_extract.py`, `fill_gaps_scope.py` and `resolve_preflight.py` reject malformed or unsafe input instead of crashing or acting on it.
- `agent_watch.py` exits 2 when the batch file has no rows, reports an unreadable batch file as `timed_out`, and does not count a deliverable older than the batch file as done.
- `/oss:release`: `audit` rejects a `..HEAD` range, reloads its range and target across blocks, picks the newest upstream review naming this PR, and numbers the report instead of overwriting it; `--changelog` derives the target version from the notes range, and the collapse guard on `SUMMARY.md` and `MIGRATION.md` fails closed when unarmed; `prepare` rejects an empty, `v`-only, option-shaped or slash-bearing version before persisting it; `demo` takes its range from the first range-shaped token.
- `/oss:review`: a rejected PR still allocates its report directory; the issue agent is `foundry:sw-engineer` and skipped for docs-only and non-code scopes; test paths are anchored to path segments; CI configuration other than `.github/workflows/` and `.github/actions/` prints a not-reviewed warning instead of spawning or skipping silently.
- `/oss:resolve`: unknown flags are reported before parsing and ask Abort or Continue; a push is verified against the PR head commit; checkout uses `--end-of-options` and `gh pr checkout --branch=`; the conflict-resolution merge refuses an unrelated `MERGE_HEAD` and a refused merge exits 1; comment dispatch passes its task through a file in an untrusted-data fence; PR title and body are fenced and review and comment lists are paginated; the change scope is persisted for the lint and QA gate.
- `/oss:analyse` persists its thread type, item and comments through sentinels, uses `-F` for GraphQL placeholders, reloads the codemap flag in every signal block, and runs the vitality codex review from one instruction block; a missing `.done` marker or unknown verdict counts as timed out.
- `shepherd` gains the `Skill` tool for the `humanizer` skill and uses `gh-write` approval wording; `cicd-steward` fixes the `gh-action-pypi-publish` name, risk-tier order and Python matrix and routes CHANGELOG policy to `oss:shepherd`; `gh-scraper` no longer uses BSD-only `date -r`; `repo-warden` records its partial-file path in a sentinel. The agents use `${CLAUDE_PLUGIN_ROOT}` without a source-tree fallback.
- Skill-owned halts print `! BLOCKED` instead of the `⛔` form, and agent spawns carry a `name` and a delta-first prompt line.
- `/oss:setup` probes bare `python`, writes `settings.json` through a redirect that keeps a symlink intact, and drops `model` from its frontmatter.
- `resolve_shared_path.py` rejects an absolute (`/etc`) or empty-segment (`skills//_shared`) subdirectory and a plugin name with a trailing newline, which could resolve to a path outside the plugin install. It also reads its own plugin name from `plugin.json`: in the installed cache the directory name is the version number, so the own-plugin lookup never matched there.
- `verify_blueprint_audit.py verify` reports an audit-log line nested deeper than Python's recursion limit as a damaged line, like invalid JSON, instead of crashing the whole check.
- The report-delivery hook recognises a `/research:topic` follow-up question that offers `/research:topic plan` as that workflow's own gate, so its report path, title and header-table checks apply instead of being skipped.
- `/oss:resolve` action-item dispatch is split into `dispatch-prelude`, `dispatch-phase1`, `dispatch-phase2` and `dispatch-phase3`, and the report-intake block moves to `modes/report-intake.md`, loaded only for `report` and `pr + report` runs; `/oss:release` loads `modes/delegation.md` and `modes/append-cross-cycle.md` on demand, and `shepherd` loads each sidecar guide per task type instead of all at once.
- New `bin/symbol_exists_at_ref.py` replaces the AST heredoc copied into the truth-check and gather prompts; `bin/codemap_batch.py` runs the review QA and docs codemap supplements as one `codemap-py` batch call instead of two per module; `bin/filter_action_items.py` validates and filters numeric item-id files for both resolve sites.
- `build_merge_plan.py` gains `--resume-from merge-result.json`, `merge_action_items.py` resolves and mints the findings file itself (`--report-file`, `--impl-dir`, `--persist-findings`, `--intake-accepted`), and `build_analyse_paths.py` gains `--mode report-file`, so a thread report is written to the path the fast path probes and is reused.
- Compaction boundary blocks in `resolve`, `review` and the dispatch phase call `bin/compaction_boundary.py`, and seven contract writers pass `--keep-sentinel <name>` instead of reading the keep sentinel inline.
- `/oss:setup` merges the shipped allow and deny rules through the new `bin/merge_settings_permissions.py` in one call instead of two `jq` blocks, keeping first-seen order without re-sorting.
- Variables that were empty in a fresh shell are reloaded from sentinels: analyse and review `CLEAN_ARGS`, release `RANGE`, `MIGRATION_DOC` and `CHANGELOG_FILE`, and resolve `CODEMAP_ENABLED` and the index path, so the review-cache materialization no longer skips silently; the release delegation steps reload their branch, date, range and waiver file instead of always failing the assert.
- `repo-warden` loads its scoring group through three fixed per-group blocks and reads the data file from the vitality session sentinel instead of an unset `AXIS_GROUP` and `DATA_FILE`; `gh-scraper` drops the unused `Write` tool; `/oss:analyse` parses `--reply` before the shared-path hard fail; the `demo` mode persists the output path its gate reloads.
- Session sentinels written by the resolve, analyse, release and codemap helpers are created fresh with owner-only permissions and are never written through a file or symlink another local user planted at the same name in a shared temp directory; a sentinel that cannot be replaced stops the step instead of reusing someone else's value.
- `/oss:resolve` pushes to a renamed fork correctly: Step 4 records the PR's head repository name and Step 10 passes it to `derive_fork_remote.py --fork-repo`, so a fork remote added at push time points at the contributor's repository instead of one named after upstream.
- `/oss:analyse` vitality refuses oversized inputs instead of reading them whole: a Group 1 dataset file over 64 Mi characters is reported missing, and an existing data file over the same bound stops assembly with exit 1 rather than silently dropping its records. `/oss:resolve`'s specialist merge refuses a plan or centrality file over 10 Mi characters before any cherry-pick.
- Repository text from the vitality data file (README, CONTRIBUTING, workflow files) reaches the rework agent inside an untrusted-data fence, and the Codex independent review is told that text is data, not instructions.
- `/oss:review` runs the Codex co-review as a background bridge call beside the spawned review agents and writes its findings to `foundry--codex.md` itself, since the bridge runs Codex read-only and cannot create that file.
- `/oss:release notes v1.2->v2.0` builds notes and CHANGELOG entries from the requested commits: Mode Detection now saves an explicit two-endpoint range for Gather changes, which ran in a fresh shell and silently fell back to `LAST_TAG..HEAD`. A lone version token (`prepare v1.3.0`) is never taken as a range, and each run clears the previous run's saved range.
- `/oss:review` routes a GitHub Actions-only PR to `oss:cicd-steward` with CI status, the acceptance gate and the challenger: `.github/` and YAML files no longer qualify a PR for the narrow `TESTS_CI` lineup, which skipped all of them. Notebooks, requirements manifests, `pyproject.toml` and other config are no longer counted as docs or skipped as "no relevant files"; they get the code lineup, and a CHORE PR touching a notebook keeps qa-specialist for the OWASP/CVE scan like a dependency manifest.
- `/oss:review` spawns the merged docs+lint unit as `foundry:linting-expert` with documentation folded in, still writing both `foundry--linting-expert.md` and `foundry--doc-scribe.md`. `--fill-gaps` reruns now use the same agents as the full review — `foundry:sw-engineer` for linked-issue analysis, `foundry:linting-expert` for the docs+lint unit — instead of `foundry:doc-scribe` for both.
- `check_agent.py` counts only agents in the installed plugin cache, so a `.claude/agents/shepherd.md` planted in the repository being worked on no longer makes `/oss:release` treat it as the installed `oss:shepherd`.
- `review_impact_tier.py` reads at most 10 MiB of diff-impact JSON; larger input is not parsed and `/oss:review` falls back to the `FULL` lineup instead of holding the whole payload in memory.
- Read-only `bridge:review` passes in `/oss:analyse` vitality (independent Codex scorecard, adversarial rework) and `/oss:release audit` no longer ask Codex to write files it cannot write: Codex returns its findings, the orchestrator writes the report file and `.done` sentinel, so a completed Codex review is aggregated instead of always reporting ⏱ timed_out. A non-`complete` bridge status still writes no `.done`.
- `resolve_shared_path.py` no longer executes a `plugins/<dir>/bin/get_plugin_install_path.py` found in the working directory, so a contributor checkout under review cannot plant code that runs while `/oss:*` skills resolve their shared files; it runs the helper shipped beside itself and ignores a registry `installPath` outside `~/.claude/plugins/cache/borda-ai-rig`.
- `blueprint-manifest.json` is regenerated with the allowlist gate: 1909 of the 1949 entries the previous gate admitted stay auto-allowed, including the `/oss:review` blocks that source their flag and gate files, the `/oss:resolve` dispatch and full-suite blocks, the `/oss:analyse` cache write-back `mv "$CACHE_FILE.tmp" "$CACHE_FILE"`, and the `/oss:review` step that parks a refused report with `mv -f`. Two blocks are left out and prompt: the `/oss:resolve` lock cleanup that runs `rm -f`, and the `uv add` example in `cicd-steward`.
- The bundled `blueprint-allow.js` refuses a manifest hit that hides a destructive command behind a `case` arm label or inside a function definition (`case x in a) rm -rf /;; esac`, `f() { rm -rf x; }`), which it let through before.

## 0.50.5

- Sync `write-guard.js`: a tracked file never asks, and an untracked or gitignored file asks only for `CLAUDE.md`, `AGENTS.md`, `.claude/settings*.json`, `.pre-commit-config.yaml` and `pyproject.toml`. `.github/**`, `CHANGELOG.md` and lockfiles are no longer protected, so editing a newly created file there no longer prompts on every edit. A protected file that does not exist yet asks once on creation; later writes to it in the same session pass.

## 0.50.4

- Sync the file-handoff protocol: a tool or permission denial that blocked part of a task returns `done_with_concerns`, never `done`.
- `/oss:analyse` runs inline instead of as a forked background agent. The `context: fork` frontmatter made the harness hide the run as `@oss-analyse`: its phase tasks never reached the caller's `TaskList` and its progress was not shown.

## 0.50.3

- `/oss:resolve`, `/oss:review` and the shared compaction contract follow foundry's non-blocking boundary gate: an `advise` decision starts the next phase (the resolve fan included) at once and shows a short one-line `/compact` hint at the next natural pause; only `FOUNDRY_COMPACT_GATE=stop` holds the run until `continue`.

## 0.50.2

- `/oss:release audit` splits dependency CVEs by release scope. `run_audit_checks.py` diffs the range's dependency manifests and lock files and passes the touched package names to `parse_audit_json.py --introduced`, which reports `new` and `pre-existing` vulnerable packages. A CVE new in the range is a critical finding (red flag, verdict `BLOCKED`); a pre-existing one stays a `medium` warning and never blocks alone.
- `/oss:release audit` counts dependency CVEs only for packages the built distribution ships: `[project].dependencies`, non-dev optional groups and their transitive runtime requirements. Test, docs and other dev-only packages are listed as `not shipped` and never become findings; an unreadable or dynamic dependency set falls back to scanning everything and says so.

## 0.50.1

- Sync `write-guard.js`: a protected file git already tracks no longer asks before a write, since the write is diffable and revertible; untracked, new and gitignored protected files still ask, and `.claude/settings*.json` and `.pre-commit-config.yaml` always ask.
- Sync the adversarial-loop rule: reviewer briefs that run adversarial shell cases require case files written with a file-editing tool and fake `gh`/`git` binaries.

## 0.50.0

- Keep review coverage and confidence gaps as factual limitations; require a specific user decision for additional review and omit declined requests from findings and final actions.
- Write report-path test sentinels with LF on every platform so Git Bash reads valid paths on Windows.
- `/oss:resolve` no longer hard-blocks a review report that fails intake. It runs `find_review_report.py --assess`, prints every gap graded `critical` (a lost, unbacked, duplicated or stale finding, a confirmed row resolve's merge would refuse, an ambiguous verdict, an unreadable sidecar, or a blocked/missing QA review under a claimed-complete report) or `degraded` (a wrong readiness or aggregate-outcome label, another reviewer's coverage gap, open-concern bookkeeping, a legacy report), and asks: proceed with the valid parts (offered only with no critical gap and no `REJECT_` gate), rerun review for selected gaps, rerun in full, or abort. The review's report, sidecars and findings are never edited. Proceeding passes `merge_action_items.py --accept-degraded` for that report only; the verdict re-check stays strict, and the final report records the degraded intake and its gaps.
- New `/oss:review <N> --fill-gaps <report> --roles "…" --candidates "…"`: re-runs only the named reviewer roles and candidate checks against an unchanged PR head, carries every other record of the prior run into a new run, and publishes a confirmed candidate's missing finding row without re-validating it. `fill_gaps_scope.py` validates the selection, plans the run and verifies the carried records.
- `find_review_report.py --assess` prints the graded gaps as one JSON line; exit 2 on any critical gap, exit 1 when the report path cannot be published, else 0 with the report path published. The line carries `head_moved` (`true`/`false`, or `null` when the run recorded no commit id or the PR head cannot be fetched); a moved head drops the targeted rerun from resolve's triage and recommends a full rerun.
- `--assess` never under-grades a report the strict gate refuses: a completed reviewer without coverage, rationale or evidence is `assessment-unbacked`, a non-string candidate id `candidate-invalid`, any record rule no label correction satisfies `ungraded-rule` (all critical), and a refused report always shows at least one gap. A malformed sidecar grades `sidecar-malformed` instead of crashing, a legacy gate line other than PASS, BLOCK or `REJECT_<GROUND>` grades `legacy-gate-ambiguous` (critical), and the QA reviewer is recognised by one shared pattern (`qa`, `security`, `test`) in both grading and the fill-gaps unit map.
- `--fill-gaps` refuses before any spawn unless the run it would publish can pass the publication gate. It checks the carried projection, which is the prior run minus the re-run roles and re-validated candidates, plus the rows to publish, with every other record verbatim. The check uses the gate's own record rules and varies only what the new consolidator may write. A refusal names the carried records to add to `--roles`/`--candidates`, or says that no addition can help. A sidecar, contract or open-concern gap that no rerun can repair is refused outright. The prior `head-sha.txt` must hold a 7–40 hex commit id. A named CONFIRMED candidate only gains its missing row when its record passes every rule and has no open concern; otherwise it is re-validated. Carried findings rows must also pass the producer row rules (merge-ready fields, unique ids): a failing row names its candidate as an addition, and a row naming no recorded candidate needs a full review.
- New `fill_gaps_scope.py triage --report=<report> | --assessment=<--assess JSON>` prints whether resolve may offer a targeted rerun, plus the required and optional subjects, using the planner's own checks. It covers the recorded commit id, a moved head, a terminal REJECT, and whether the carried records can pass. Resolve's triage offers **Rerun review for selected gaps** only when the verdict is admissible and always includes the required subjects, so it never suggests a request the planner refuses. The one exception is a PR head that could not be checked: the rerun is still offered but not marked recommended, and the planner re-checks the head before any spawn. Triage also offers every rerun the planner would accept, adding any carried record the run needs to the required subjects; when the gaps lie outside every record (a header-only or aggregate-label defect), every recorded subject that plans alone is offered as optional. A QA or security role named by a gap is always required, and the rerun is not offered when that role cannot be named in `--roles`. Every refusal ends with a pointer to a full review. The verdict carries the resolved `pr`. When one optional subject sits beside required ones, or when more than 16 optional subjects are all of one kind, the user is asked instead of having it taken for them.
- The fill-gaps projection lets the aggregate outcome follow its projected assessments, so a rerun can correct a mislabelled aggregate without re-running a healthy reviewer. Allocation refuses a `--fill-gaps` run that has no plan instead of falling back to a plain run. A null aggregate outcome beside a missing reviewer is now valid with `Readiness: incomplete`, because a missing reviewer outranks `blocked`; the strict gate and `--assess` apply the same rule.
- `--assess` adds `recorded_rejection`, the report's `Gate: REJECT_…` line, even when critical gaps blank `gate`, and resolve's triage shows it. Deeply nested or malformed sidecars grade as gaps instead of raising `RecursionError`. `fill_gaps_scope.py verify` reports malformed plans and sidecars as problem lines, never a traceback.
- `mint_finding_ids.py --carry-from <prior findings.jsonl>` keeps every carried row's id verbatim in a fill-gaps run, and a new row never takes a carried row's id. The consolidator passes it with the plan's `prior_findings`.
- The strict gate requires an assessment `role` to be a non-empty string. `merge_action_items.py` blocks a findings row or link whose fields have the wrong type, instead of crashing.
- The carry check and the publication gate share one consolidator re-spawn per run. Before that re-spawn the refused report is parked as `review-report.refused.md`, and a final refusal empties the report-dir sentinel, so the delivery hook never demands a refused report. A consolidator that publishes a `REJECT_` gate itself fails the gate: only Stage 1's terminal publisher may publish a rejection.
- Resolve's report-mode lookup no longer aborts under zsh when a report lineage holds no file; the printed fill-gaps command quotes the report path, and the subject follow-up question stays within 4 questions of 2–4 options each.
- `/oss:review` now checks its own publication before printing it as finished: after the consolidator returns, it runs `find_review_report.py --report --producer`, which applies the same intake check `/oss:resolve` uses plus producer checks: the header must carry `Review contract: 1`, and every `findings.jsonl` row must carry the fields resolve's merge requires (`id`, `section`, `severity`, `title`, `change`, `author`), with a known severity word and no id shared by two rows. A consolidator-published `REJECT_` keeps its own reject route and skips these row checks. A failing report gets one consolidator re-spawn with the failure reason; a second failure stops with `! BLOCKED` instead of delivering a report that resolve would reject (for example `Readiness: complete` with open UNVERIFIED concerns, a blocked reviewer, or a CONFIRMED candidate without a `findings.jsonl` row). The gate and resolve's merge share one row rule, and `merge_action_items.py --recheck-verdicts` now blocks on a publication row it cannot use instead of crashing.
- `find_review_report.py` names the broken rule on rejection: `incomplete-review-report` is followed by a `[gate] reason:` line (for example `open candidate 'V13' (UNVERIFIED) under readiness=complete`). `gate_line()` is unchanged; the new `gate_verdict()` returns the gate line and the reason.
- Sync the cross-validation protocol and consolidator prompt: aggregate `readiness: complete` requires every assessment complete and no open concerns.

## 0.49.0

- When `/oss:resolve` sends a review item to Codex to fix, hand it the CodeQL alert the item links: `fetch_alert_context.py` reads it in the calling session (the child has no network) and appends it to the brief as delimited untrusted data, only for alerts in the PR's own repository and the item's own file. The helper now also writes the item line, with the reviewer's comment fenced as untrusted, and always ends with one line, outside and after every block, naming the caller-fetched block tokens (`none` when nothing was fetched), so a comment cannot pass a forged section off as fetched data. An item whose path holds a line break, a control character or `<`/`>`, or whose line is not a line number, is refused and takes the ordinary path; a path with spaces is written as a quoted JSON string. A failed fetch, an unresolved repository, an alert elsewhere, a foreign-repository link, a link past the cap or a code-scanning link on another host, in another form or with its number in other digits each append a notice so the child returns `UNCERTAIN` instead of guessing. Links with `http://`, `www.` or a different host case are fetched too. Inside a fence, marker-shaped spellings get a word joiner inserted, never consumed, so identifiers and paths that merely mention them stay verbatim. The spellings, and alert links, are found on a folded view (format characters and invisible marks dropped, dashes unified, NFKC), so in-word zero-width, fullwidth and dash look-alike spellings cannot slip through; the comment itself reaches the child unchanged apart from the inserted joiners.
- Require `full_comment_text` to keep the comment body verbatim, links included.
- Drop the `Suggestion` column from the resolve report's Challenge Log; `Resolution` is the outcome, and `suggestion_why` stays in `challenge-log.txt`.

## 0.48.3

- Sync the quality-gates rule: after each validated adversarial-loop round, print the progress table exactly, with all rows kept and each cell as `old + new`.

## 0.48.2

- Stop the resolve picker from restating items the printed table and item options already show: the bulk question carries counts, notices and the saved table path, and the group-preview question carries the group count and path.
- Make `enforce-resolve-table.js` require the saved table file (every pending and closed id as its own cell) instead of item lines in the question text.
- Fix review validation publication: the reviewer instructions now require string-array assessment evidence and limits, open concerns only for UNVERIFIED or pending candidates, and no complete readiness beside an open candidate; the report intake gate accepts a single string for evidence and limits.

## 0.48.1

- Show reviewer names and finding authors as roles only, using square-bracketed scores (`Software engineer [3]`); keep execution provenance in evidence and limits, omit skipped roles, and render a null score as `[no source judgment]`.

## 0.48.0

- Add owner-stamped atomic compaction contracts and boundary metadata; call Foundry's compaction gate when present and continue with an explicit fallback when absent.
- Route medium- and high-effort single-file code, test, documentation, style, configuration and CI items to `bridge:implement`; keep architecture, performance, refactor and extra-high-effort work with specialists.
- Ask follow-up questions with plain report path, title and outcome text within 1,500 characters; print the complete header in the final reply and remove duplicated option previews.
- List pending resolve items in question text, using top items, exact type counts and the saved complete inventory when the budget is exceeded; print the full table once before selection.
- Read pinned PR-head source in early challenges even after a clean merge, keeping freshness verification and source reads on the same revision.

## 0.47.1

- Validate every review candidate at every severity before publication; write a version-one validation envelope separating source judgment, readiness, reviewer coverage and open concerns.
- Keep missing reviewers and undecidable judgments distinct from rejection, retain all confirmed findings, and require compatible validation intake before resolve merges them.
- Preserve VALID, affirmative REJECT, UNVERIFIED and ALREADY_FIXED evidence states through resolution; unverified items remain pending with an owner and next check, while already-fixed history needs revision-bound proof.
- Recheck carried verifier and parent evidence against the source revision actually consumed by each resolve wave and preserve independent provenance across Git Bash paths on Windows.

## 0.47.0

- Remove broad GitHub API allow and write-deny entries that conflict with Foundry's guarded read and approval lanes; use explicit GET for ecosystem search requests.
- Include the optional GitHub-read lane in Bash audit verification.
- Ask separately about target drift and then obtain a single-use push approval for the concrete scope; retain explicit skip and manual-push recovery outcomes.

## 0.46.1

- Keep finding collection and selection in one stream with no conflict agent running beside the picker.
- Start conflict resolution and read-only early challenge together after selection; early challenge reads pinned PR-head source, while implementation waits for conflict completion.
- Preserve pending items and release the mutex on an unanswered group gate instead of choosing a silent default.

## 0.46.0

- Put report and action inventories in the picker's own delivery surface and enforce a final report header when a follow-up is skipped.
- Check stale plugin sessions at session start and preserve isolation through the selected worktree and pinned specialist base.
- Cap yellow and red vitality trajectory scores at their band maximum instead of letting an arithmetic mean exceed the rubric.

## 0.45.3

- Assemble vitality datasets and compute rubric metrics in Python so large JSONL lines cannot disappear through truncated source reads.
- Distinguish failed fetches, valid empty datasets, permission-limited alerts and pending contributor statistics; preserve missing-data confidence caps and compare unrounded metrics.
- Fetch repository community and workflow evidence through bounded helper calls and carry extractor-applied bands, scores and confidence deductions into scorer output.
- Keep every low finding as its own action item and table row, while bounding resolve challenge and implementation work by group rather than total selection size.

## 0.45.2

- Deny force-if-includes and mirror pushes in shipped permissions.
- Resolve the fallback Haiku model at runtime with a bounded cache and logged fallback; use Sonnet for shepherd communication.

## 0.45.1

- Require representative real-case evidence alongside regression checks in the quality rules.

## 0.45.0

- Process every selected resolve item in the same pass instead of imposing an item cap; bound challenge chunks to twelve items and implementation groups to five.
- Persist predictable selection, grouping, dispatch and push decisions before unattended implementation and retain exact selected IDs in report mode.

## 0.44.7

- Recheck target-branch freshness before QA and again before push, remerge and revalidate when the target advances, and report unverifiable freshness explicitly.
- Keep non-fast-forward push recovery manual instead of repeating merge or push operations unattended.

## 0.44.6

- Build grouped resolve subjects as valid Conventional Commits, preserving a single item's typed subject and deriving shared type/scope for multiple items.
- Show the action inventory once immediately before selection and account for transcript flush delay before denying a report-table gate.

## 0.44.5

- Enforce report-header delivery at Stop as well as follow-up questions so skipping the picker does not omit the report.
- Keep reviewer ratings, legend, aggregate summary and findings overview in the delivered review, and report explicit confidence deductions in resolve handoffs.

## 0.44.4

- Bootstrap Python without requiring Python to start the launcher: provide POSIX and native Windows entry points, probe supported runtimes, execute the workload once and preserve its exit status.
- Run adversarial ledger checks through the packaged portable launcher.

## 0.44.3

- Add Python fallback bootstrap support and probe common Node install locations for hooks on hosts whose PATH lacks the runtime command.
- Use `Codex <codex@openai.com>` consistently in generated resolve commit trailers.

## 0.44.2

- Continue authorized feasible remediation while the weighted finding score decreases, removing the fixed review-round ceiling.
- Ask completion questions only when a decision or authorization is missing, retaining concrete scope, paid-execution and protected-action gates.

## 0.44.1

- Keep item origin/type separate from resolution status and allow only pending GitHub items to absorb report findings.
- Respect explicit closed-item selections in report mode and retain push intent for a merge-only run with no pending items.

## 0.44.0

- Publish stable finding IDs and structured review handoffs, merging them into GitHub action items without losing unmatched findings or contributor attribution.
- Persist resolution and release provenance as append-only ledgers with separate mutable headers.
- Gate resolve selection on visible action-table delivery and choose lighter review only when changed symbols and transitive callers stay in tests or leaf modules.

## 0.43.1

- Replace the obsolete repository API allow rule with valid wildcard syntax during setup, preserving unrelated user permissions.

## 0.43.0

- Use completion notifications and per-agent deadlines throughout OSS workflows, surfacing timed-out deliverables without polling.
- Select affected resolve tests and run the full suite once at the final gate; limit reproductions to their actual examples or named tests.
- Gather Git state in one structured snapshot and refuse a push scope that cannot be described by target, commits and diff evidence.

## 0.42.2

- Tolerate case and separators in a leading review verdict token during resolve intake while rejecting glued suffixes and placeholders.
- Keep new report Outcome fields as standalone verdict tokens, with counts in Summary.

## 0.42.1

- Ground release truth checks in exact baseline and HEAD source coordinates, distinguishing a candidate binding from public-export proof and keeping uncertain absence qualified.
- Reconcile typed breaking-change counts with individual evidence ledger entries before classification approval.
- Capture the caller branch before resolve checkout and keep release append merges staged at stable user-visible artifact paths.

## 0.42.0

- Warn when a running session uses an older plugin root than the active install.
- Refold appended release summaries into a short hook, real user wins and an upgrade call; revalidate the final merged draft against current source and migration documentation.
- Recognize conventional revert subjects as well as Git's default format and require evidence links for added changelog entries.
- Retry a colliding worktree name once with a disambiguator and inspect stale worktrees only when the remaining failure actually names a candidate.

## 0.41.1

- Overlap PR intelligence with checkout and trial merge, then overlap conflict resolution with item selection when their inputs are independent.
- Replace Fine dispatch with Per specialist and Custom choices; per-specialist dispatch removes the five-item split while retaining ownership and coupling rules. Document each skill's actual fan-out and gate structure.

## 0.41.0

- Let resolve users choose automatic, sequential, finer or previewed specialist dispatch, changing wave width while preserving file ownership and routing.
- Enforce model-tier wave ceilings and show real formed groups only on the requested preview path.
- Deliver one quality-rule variant per plugin, using OSS deltas when the Foundry shared rule is present.

## 0.40.4

- Ship shared notebook authoring guidance for generated release demos, requiring explanatory narrative, focused cells and plot interpretation.

## 0.40.3

- Refresh resolve compaction state after challenge and before specialist implementation, preserving skip logs and task IDs across both long phases.
- Detect a same-session active resolve run before offering a duplicate review follow-up.

## 0.40.2

- Split large helper implementations into focused functions.

## 0.40.1

- Accept approved review outcome tokens with their optional leading verdict symbol during resolve intake, preserving rejection and incomplete-report checks.

## 0.40.0

- Stage and promote release artifacts with a recoverable publication journal, branch-scoped state and patch-ID provenance for cross-cycle reverts.
- Preserve exact prior artifact lines for append reconciliation and separate waived or reclassified changes from gathered release evidence.
- Expand adversarial discovery to unchanged producers, consumers and related write paths before accepting a local fix.

## 0.39.2

- Reject argument-injection and path-escape inputs at PR, project, worktree and release-envelope boundaries; bound PR diff reads.
- Keep shepherd's breaking-change findings as consolidated report blocks for the parent instead of asking an unanswered question inside a subagent.
- Persist raw Bridge batch replies and challenge verdicts before commit and log consumers read them, avoiding in-memory reconstruction after compaction.
- Route annotation-only PR review to the documentation reviewer, retaining separate test/CI lint routing.

## 0.39.1

- Freeze staged, unstaged and relevant untracked source for independent review, binding findings to the exact diff and declared coverage.
- Carry stable prior finding signatures into bounded review cycles and require a later independent check after feasible fixes.

## 0.39.0

- Add per-reviewer scoped ratings and rationales without replacing the aggregate verdict, confidence or detailed findings.
- Retain every contributing reviewer in deduplicated findings and show an author column in the findings overview.
- Show `not-run` convergence state when independent coverage or validated round evidence is missing.

## 0.38.7

- Correct installed release-resource discovery for versioned plugin cache paths.
- Show validated cumulative convergence progress before further fixes and preserve LF-only structural IDs and paths on native Windows.

## 0.38.6

- Reject missing or malformed structural identity metadata instead of converting it into plausible strings.
- Require valid reverse-dependency completeness metadata before reusing a cached answer.

## 0.38.5

- Bind structural cache reuse to canonical index path, file size and modification time, plus project, target, query and flags.
- Query each canonical module once per resolve pre-loop and require successful direction-complete results before skipping source reads.

## 0.38.4

- Fence resolve integration against the recorded base SHA and require a clean index before specialist commits are merged.
- Defer direct Bridge item staging in grouped, all and stage-only modes until integration can account for the complete change set.
- Distinguish an empty item-specific staging area from a successful commit and build merge plans from explicit per-item evidence.

## 0.38.1

- Require a complete current report-header table before analyse or review follow-up, preserving diagnostic questions when delivery fails.
- Block resolve intake on incomplete reports or failed report-path publication and keep implementation tasks open until every child reaches an explained state.

## 0.38.0

- Add an independent blind-solve lane for feature and mixed PRs, then compare its design with the submitted implementation during consolidation.
- Accept `report` before or after the PR number or URL and persist challenge logs, item tasks and implementation paths for compaction-safe resume.

## 0.37.0

- Load structural gates from the active registry-selected `codemap-py` installation, skipping orphaned cache versions and removing the local contract copy.
- Remove the semantic companion `--semble` path and keep structural context or source reads as the supported review routes.
- Resolve and gate a saved review report once before remediation, distinguish rejected or incompatible lineage, and reuse it instead of starting another review.
- Group resolve commits by domain, file or specialist according to the selected strategy.

## 0.36.8

- Condense agent, skill and rule guidance for source snapshots, independent review, contributor communication and workflow handoffs.

## 0.36.7

- Store PR reviews under `.reports/review/pr-<N>/run-<NNN>/` and allocate increasing run directories; resolve finds the newest report for the requested PR.
- Fetch review bodies and inline comments independently, including collapsed findings, and refresh them even when the primary PR thread cache is reused.

## 0.36.6

- Build release contributors from Git authors, coauthors and verified in-range PR authors, including bots when requested; use paginated commit-to-PR associations instead of merge dates as range proof.
- Preserve historical changelog entries and remove only exact confirmed stale Unreleased claims for unshipped reverts or pivots.

## 0.36.5

- Include state producers, consumers and the next ordinary user action in adversarial review boundaries.
- Make reviewer labels visibly distinct across the batch and preserve stable item ordering during grouped resolve challenge dispatch.

## 0.36.4

- Ship the full adversarial-loop contract with a frozen source snapshot, evidence ledger, weighted findings and independent verification after fixes.
- Diagnose failed receipts before retrying instead of assuming a transport failure.

## 0.36.3

- Give review spawns distinct readable slots and keep target identity in the prompt; preserve original item order inside grouped resolve challenges.

## 0.36.2

- Restore confidence-gap reporting and complete per-URL fetch, content and match checks in the shipped quality rules.

## 0.36.1

- Parse whole flag tokens and value flags in helpers, fixing substring matches and positional arguments contaminated by release flags.
- Apply resolve structural flags before mode parsing and bound oversized dispatch selections explicitly.

## 0.36.0

- Require independent review and targeted fixes in a bounded weighted convergence loop; never present a final local fix as independently verified before a later review.
- Resume background agents from completion notifications instead of polling, and preserve partial or timed-out results explicitly.
- Choose host-valid private temporary directories for commit authorization and preflight, avoiding POSIX paths interpreted as native Windows locations.

## 0.35.2

- Extract analyse parsing and paths, vitality report paths, PR refs, fork remotes, review-report lookup and release envelopes into packaged helpers.
- Centralize compaction contract writing and validate helper handoff outputs before later workflow stages consume them.

## 0.35.1

- Ship a local propagated structural gate contract and keep file-read-only operation available when the optional structural plugin is absent.

## 0.35.0

- Record Bash auto-allow decisions in an append-only audit trail, including blueprint or command-shape provenance and post-execution success or failure.
- Add audit verification for recorded decisions and session cleanup boundaries.

## 0.34.2

- Remove location duplication from resolve inventory columns and label discussion rows `pending · thread (no GH resolve)` so they are not mistaken for resolvable inline threads.

## 0.34.1

- Document Bridge availability fallback and structural helper behavior with executable examples.

## 0.34.0

- Treat PR diffs and GitHub thread text as untrusted data, keeping embedded instructions out of the review execution contract.
- Merge shipped allow and deny permissions during setup and add explicit dangerous-command denials.

## 0.33.2

- Enforce strict scope exits for unrelated content and use concrete severity criteria in thread analysis.
- Run a second quality pass only for a named actionable evidence or coverage gap; preserve caller-visible fixes that should not disappear into same-release feature deduplication.

## 0.33.1

- Run a humanizer pass on contributor and release drafts after shepherd voice guidance, including when shepherd itself is unavailable.

## 0.33.0

- Fetch one PR snapshot and reuse its metadata, checks and diff throughout review, including helper signal analysis.
- Batch disjoint-file Bridge candidates while retaining per-item commits, and combine item and commit choices into one question call when the inventory fits.
- Cap independent verifier agents and group compatible critical findings for verification.

## 0.32.1

- Replace legacy Codex plugin calls with `bridge:review` and `bridge:implement`, checking installed, disabled and absent states before use.
- Keep complex resolve work explicitly skipped when Bridge is unavailable, allow an explicit implementation-agent override, and retain independent vitality and release auditing through Bridge.
- Standardize shared contract and Markdown formatting, including skill compaction state and review output instructions.

## 0.31.2

- Read PR check status from structured JSON and persist CI state for the acceptance gate instead of parsing terminal tables with non-portable grep.
- Reload thread identifiers in each fetch block so issue and discussion requests survive fresh shells.

## 0.31.1

- Harden sentinel-read auto-allow against traversal and shell-shape bypasses, distinguishing a real `..` path component from harmless dotted names.

## 0.31.0

- Add a write guard for protected file edits, writes and notebook edits.
- Remove the POSIX process-liveness doctest that sends Ctrl+C on Windows.

## 0.30.0

- Detect abandoned resolve locks and stale agent worktrees before creating new ones, checking process liveness on each platform.
- Reclaim proven-dead locks with visible output; report old clean worktree candidates and ask before removal, preserving dirty and user-named trees.

## 0.29.0

- Auto-allow bounded plugin helper commands through a blueprint-manifest hook.
- Limit contributor summaries to the most significant contributions and omit unresolved GitHub handles.

## 0.28.2

- Clarify helper contracts and executable examples for sentinel paths, release markers, output-directory containment and GitHub fetch validation.

## 0.28.1

- Anchor structural index paths at the Git root and preserve unsanitized project names, fixing subdirectory, space and non-ASCII lookups.
- Bind cached answers to stronger index identity and follow the provider's gated build action without consumer overrides.
- Normalize Windows extended symlink prefixes during rule synchronization.

## 0.28.0

- Add a cheap PR acceptance gate before full review: terminal rejection needs affirmative evidence for a defined ground, while blocking concerns continue to source review.
- Record the acceptance decision and reviewed SHA in report metadata so resolve can detect a changed PR before acting on a rejection.

## 0.27.2

- Stop treating cherry-pick or stable-branch release history as unexplained scope drift; inspect merge ancestry and distinguish raw branch merges from reviewed PR merges.
- Verify release classification against actual changes instead of trusting `fix:` or `feat:` labels.

## 0.27.1

- Batch related resolve and review preparation steps to reduce interaction turns while preserving the existing decision gates.

## 0.27.0

- Rank relevant review dimensions and cap default fan-out; add `--full` to request all scope-relevant dimensions without restoring irrelevant reviewers.
- Reduce multi-comment dispatch batches to three agents and retain domain-grouped resolve work.

## 0.26.0

- Extract shared flag parsing and changed-symbol extraction to reduce repeated shell and prompt work.
- Require reviewers to consult existing structural answers first, limit extra queries and stop repeated lookup when the provider reports a complete answer.
- Preserve all resolve commit choices and enforce actual report-table delivery before marking header printing complete.

## 0.25.1

- Correct empty dataset and PR counts so commands do not produce a duplicate zero.

## 0.25.0

- Add `/oss:setup` to deliver plugin-owned rules as namespaced symlinks, validating sources before mutation and refusing to overwrite unowned destinations.

## 0.24.5

- Represent closed helper choices as typed options for PR scope, thread type, resolve mode and commit mode.
- Print report metadata as Markdown tables with single-line bounded values while preserving the raw metadata format on disk.

## 0.24.4

- Ship the shared handoff, cross-validation, delegation and summary documents with OSS instead of depending on another plugin's skill links.
- Resolve companion installations through a plugin-owned registry helper with validated names and newest-version selection.

## 0.24.3

- Validate commit IDs and structural module paths before they reach Git commands or cache paths; make report-path handling portable across Windows and POSIX.
- Preserve low and cosmetic report findings during resolve extraction rather than silently dropping items already present in the source report.
- Keep CI reruns, tag pushes and release creation human-owned and remove subagent-local task tracking that the parent cannot observe.

## 0.24.2

- Enforce analyse and review report existence through follow-up question hooks so missing or empty reports cannot be skipped on the way to the picker.

## 0.24.1

- Include unresolved upstream review blockers and an available independent Codex pass in release readiness auditing.
- Find resolve inputs in both OSS and Codex review report locations.

## 0.24.0

- Add release `--append`: rerun the pipeline for newly landed commits and merge evidence-backed additions into existing artifacts.
- Track processed commits, invalidate markers that are no longer reachable or have been superseded by a release tag, and reconcile earlier entries when later changes revert or replace them.
- Guard whole-file summary and migration merges against accidental content collapse and reuse resolved contributor identities.

## 0.23.2

- Use `No.N` for local item ordinals and full issue or PR URLs for cross-repository references, avoiding accidental GitHub links and reviewer pings in attribution.

## 0.23.1

- Verify factual claims about names, versions, identifiers and counts before a resolve challenger marks them valid; reserve a source lookup when the referenced code cannot settle the claim.

## 0.23.0

- Add opt-in `--worktree` isolation for PR review and resolution, keeping checkout and implementation off the invoking worktree and reporting the isolated path on exit.
- Normalize pass, warning and failure symbols across analysis, release and review reports.

## 0.22.4

- Track report-header printing as a required review phase after consolidation and before follow-up choices.
- Make every resolve challenge-log row self-contained with the finding, evidence verdict, fix verdict and their reasons.

## 0.22.3

- Name `codemap-py` consistently in structural context, report headings and provider guidance.

## 0.22.2

- Migrate structural queries and index setup to the `codemap-py` command and skill namespace across triage, vitality, release, review and resolve.
- Ship matching CLI permission coverage and valid file-permission rule syntax.

## 0.22.1

- Locate the structural gate contract in the dual-runtime `codemap-py` package's Claude skill directory.

## 0.22.0

- Auto-allow bounded reads of plugin-owned workflow sentinels through a Bash hook.
- Reload state through shell builtins and preserve explicit defaults when a state file is absent.

## 0.21.1

- Add change-set diff-impact context and fix module iteration under zsh; stop issuing function queries with module-only names and use module reverse dependencies for importer context.

## 0.21.0

- Scope temporary workflow state by session ID across analyse, release, review and resolve so concurrent sessions do not overwrite each other's flags, timestamps or paths.
- Move reusable preflight state under `.temp/state/preflight/`.

## 0.20.3

- Move the plugin source directory from `plugins/oss` to `plugins/cc_oss` and update helper, shared-document and skill fallback paths while retaining the `oss` plugin identity.

## 0.20.2

- Audit public API consistency whenever signatures, flags, names or return shapes change, including overlapping discriminators and sibling conventions.
- Print the complete final resolve report and action-item resolution table inline.

## 0.20.1

- Follow the structural provider's current gate actions through its contract instead of hard-coding a potentially stale skill invocation.

## 0.20.0

- Dispatch independent resolve changes to specialist worktrees, retaining sequential Codex-first work and deterministic merge ordering by structural centrality.
- Guard a resolve run with a branch mutex and HEAD fingerprint so concurrent runs or external branch changes cannot silently corrupt integration.
- Map report sections to implementation categories and gate selections larger than twenty items before dispatch.

## 0.19.6

- Load shared and mode documents through shell `cat` calls with matching permission coverage, avoiding unsupported Read-tool paths.
- Persist the shared provider paths needed by subsequent analyse modes.

## 0.19.5

- Condense OSS guidance for draft-only communication, release readiness, structural context and review evidence.
- Keep vitality full-mode adversarial review mandatory and report silence rate ahead of response times from the answered-only sample.

## 0.19.4

- Bind reproduction dispatch to the selected role: QA for test examples, software engineering for other or ambiguous examples, and general-purpose only when Foundry is absent.

## 0.19.3

- Let user-controlled pre-commit hooks finish instead of killing bulk or lint commits with a short timeout.
- Match the intended resolve stash precisely and add a timeout to the agent router's remote request.

## 0.19.2

- Add consistent `--help` support to packaged Python helpers while preserving their existing argument and exit-code contracts.

## 0.19.1

- Consume the structural provider's shared gate contract instead of maintaining a separate OSS copy of missing/stale-index behavior.
- Change the plugin license to Apache-2.0.

## 0.19.0

- Add optional structural signals to thread triage and vitality, including candidate-symbol lookup, overlapping PR files, coupled modules and repository constraints.
- Cache review structural queries by index state and classify release breaking changes with caller-package evidence.

## 0.18.2

- Extract independent vitality assessment and adversarial rework into separate modes; quick mode skips loading both.
- Run independent adversarial reviewers together when Codex is available and keep completeness-aware semantic search fallback.

## 0.18.1

- Load only the assigned vitality scoring-group rubric instead of sending every scorer the complete thirteen-axis reference; label unassigned axes explicitly.
- Batch compatible resolve work and clarify CI, documentation and release-agent scope to reduce unnecessary delegation.

## 0.18.0

- Preserve per-skill compaction contracts and allow analyse state retention with `--keep`.
- Validate vitality weight parsing for malformed or repeated axes and clean commit authorization state on helper exit paths.

## 0.17.8

- Add `--quick` vitality analysis, skipping independent and adversarial passes with a visible unreviewed-result limitation and lower confidence cap.

## 0.17.7

- Use the packaged shared-path resolver as the executable source of truth instead of duplicating resolution shell code.
- Skip adversarial challenge for non-logic PRs and reuse the shared shepherd reply protocol.

## 0.17.6

- Return resolve's item loop to the orchestrator instead of the detached dispatch runner, keeping task and commit sequencing in one owner.
- Skip PR content classification when review is drafting a reply from a supplied report.

## 0.17.5

- Persist and reload analyse reply flags, release branch/date values and PR review mode classification across independent shell calls.

## 0.17.4

- Build a session-scoped agent index and route missing roles through local semantic matches, using OpenAI embeddings when configured or an Anthropic selection fallback before general-purpose dispatch.

## 0.17.3

- Detect Foundry from its actual installed or checkout agent file rather than assuming a cache-directory name proves availability.

## 0.17.2

- Deduplicate matching report and GitHub findings while retaining the additional report reviewer attribution.
- Split review launch, collection and verification into independently tracked tasks, deleting unused verification tasks explicitly.

## 0.17.1

- Print selection-driving resolve inventories inline and define authors as the people or roles responsible for fixing each item.

## 0.17.0

- Preclassify documentation and test PRs before selecting reviewers, avoiding a full source-review team for non-logic changes.
- Keep resolve item scope independent from commit granularity and extract release changelog-audit and draft-writing prompts.

## 0.16.10

- Remove redundant shell commentary while retaining notes explaining persistent state, source evidence, fallback paths and non-obvious workflow constraints.

## 0.16.9

- Offer structural-index build and stale-index gates before review or resolution.
- Print merged GitHub and report action inventories immediately with change category, severity, author and location.
- Keep human contributors using GitHub privacy-email addresses instead of classifying them as bots.

## 0.16.8

- Extract contributor collection and canonical per-item commit messages into helpers; deduplicate contributors by email and filter bot identities.
- Move structural review-context generation into a dedicated mode and require vitality partial-score files through the Write tool.

## 0.16.7

- Account for an already-running background index refresh before rebuilding stale structural context.
- Preserve PR identity in the review metadata and make bulk-selection outcomes explicit, including stop and unanswered-question behavior.

## 0.16.6

- Detect structural-index currency and offer explicit missing-index or stale-index choices before review and resolve consume it.

## 0.16.5

- Keep all four resolve commit modes distinct from bulk item selection and defer branch restoration for stage-only work.
- Include reverse function-dependency context in the review pipeline.

## 0.16.4

- Require shepherd's inline-suggestion table whenever findings identify file and line locations; verify the reply file and retry missing required content.
- Block consolidation when the Agent tool is unavailable, preserve all readiness rows in release preparation, and use short PR references in draft notes.

## 0.16.3

- Name review reports through a `Title` metadata field and fence the printed header so Markdown preserves its delimiters.

## 0.16.2

- Keep resolve task completion pending until the relevant per-item, group or bulk commit succeeds; persist item-to-task IDs for batch execution.
- Refresh both merge inputs and verify no conflict markers remain before accepting conflict resolution.
- Print the complete review report header as the first reply content.

## 0.16.1

- Pass structural context to every applicable review dimension, including function blast radius and affected tests, rather than only the software reviewer.
- Label discussion action items as lacking a GitHub resolve button in the selection UI.

## 0.16.0

- Correct release mode detection, handle unknown flags explicitly and ask before using the initial commit when no stable tag exists.
- Extract release truth checking and review scope, reviewer and consolidator prompts into dedicated files; run independent changelog audit and contributor extraction together after their prerequisites.
- Separate resolve bulk item selection from commit-mode selection and use compact selection for large inventories.

## 0.15.0

- Split PR and report intelligence and batch execution into resolve modes, requiring a result for every selected item and keeping user questions and task ownership with the parent.
- Repeat lint and QA after fixes and block publication when blocking findings remain after three iterations.
- Route documentation PRs to the documentation reviewer and preserve dedicated CI routing; override small-diff classification when refactor or performance intent demands deeper review.
- Enforce sequential release phases, net-state classification, contributing-PR accumulation and explicit failure handling before downstream artifacts are produced.

## 0.14.0

- Enable resolve structural context when an index is available, with `--no-codemap` to disable it and strict `--codemap` preflight when explicitly requested.
- Compare documentation coverage of a significant release feature with similar existing features, including headings, explanation and examples.
- Require concrete diff locations for review findings and primary evidence or independently corroborated experiments for external-standard claims.

## 0.13.0

- Audit scheduled deprecation removals against the target release and flag overdue symbols still present at HEAD; keep an unknown target informational.
- Preserve the original branch name for same-repository PR remediation instead of creating PR-number aliases.

## 0.12.1

- Keep resolve commit mode in a separate question with all four choices: per item, by topic, one commit, or stage only.
- Require the GitHub CLI for PR checkout instead of silently taking an alternate checkout path.

## 0.12.0

- Make release notes reflect the final state at HEAD; remove claims for symbols that no longer exist and notify the user of excluded claims.
- Challenge release drafts against actual definitions, and display the full readiness table during release preparation.

## 0.11.10

- Share review section names, agent ownership and severity-to-action mapping between review consolidation and resolve parsing.
- Persist release setup and resolve preflight outputs as individual files so later shell calls reload the same values safely.

## 0.11.9

- Extract thread-type detection, PR scope classification and dependency/secret signals into read-only Python helpers.
- Fetch second-stage repository documents and workflow contents through a packaged helper after repository metadata is known.

## 0.11.8

- Keep all available resolve follow-up routes in one review question, including PR, report and combined resolution.
- Execute shared initialization before analyse modes and clarify documentation-only and CI-only review support.

## 0.11.7

- Mark capped GitHub responses partial, distinguish null fields from missing fields, and keep real CI results separate from workflow or badge presence in vitality scoring.
- Resolve review resources from the installed plugin before falling back to the checkout.

## 0.11.6

- Order resolve selections by severity and reserve the final question for bulk actions; group item choices by proximity within the question budget.
- Include change categories in resolve reports and check contributor guidance for a code-of-conduct reference.

## 0.11.5

- Use the GitHub `claude[bot]` noreply identity in resolve helper and workflow commit trailers.

## 0.11.4

- Add topic-grouped resolve commits, allowing implemented items to be assigned labels before committing each group.
- Create ordered PR-review phase tasks before scope detection and update them before long output blocks.

## 0.11.3

- Check complete release drafts for duplicate concepts, contradictions, impossible fixes, broken causation and internal-only entries; fix the collected issues before writing.
- Classify release outputs as communication artifacts rather than the published product.
- Fold fixes to never-released features into their Added entry and describe changes to accidentally supported behavior explicitly.

## 0.11.2

- Skip deprecation findings for removed APIs that never appeared in a released tag.
- Treat PR discussion consensus as authoritative when the description has drifted from agreed implementation intent.

## 0.11.1

- Preserve permission-denied and pending GitHub datasets as explicit partial evidence; guard zero-sample responsiveness and use one analysis timestamp for age calculations.
- Fail immediately when shared initialization is invalid, derive the release trunk from repository metadata, and retain human ownership of public GitHub changes.

## 0.11.0

- Expand vitality scoring to thirteen axes and recalibrate weights and response thresholds; detect stale-bot inflation instead of treating every closure as maintainer responsiveness.

## 0.10.6

- Distinguish resolvable inline review threads from discussion comments and report findings; mark discussion items done only with evidence of a subsequent answer or fix.
- Persist analyse drift and scraper time-window values across shell calls.
- Check for existing helpers before accepting new abstractions, flag oversized review scope, and retain praise and learning notes after defect findings in contributor replies.

## 0.10.5

- Update requested structural-index setup guidance to `/codemap:scan-codebase` and resolve analyse modes from the OSS plugin root.
- Keep reviewer and reply timeouts explicit, returning timed-out or partial output instead of silently dropping agents.

## 0.10.4

- Fetch the target repository before merge analysis even when the invoking branch has no upstream, and stop if the target-branch fetch fails.
- Parse review flags directly and keep shepherd's role limited to contributor feedback rather than code analysis.

## 0.10.3

- Persist authoritative action-item details in the implementation handoff and scan caller blast radius once before dispatch.
- Preserve PR motivation and labels in resolve context, default ambiguous comment work to software engineering, and recompute authorization paths before cleanup.

## 0.10.2

- Preserve resolve's selected commit mode across the full item loop without re-asking; make bulk and lint commits manage their scoped authorization cleanup.

## 0.10.1

- Route evidence and fix challenges to the domain reviewer: software engineering for logic, QA for tests, and challenger for architecture or coupling.
- Warn with setup guidance and print a plain summary when Foundry's terminal template is unavailable; log scraper completion to stderr.

## 0.10.0

- Port packaged shell helpers to Python for native Windows compatibility, including preflight, GitHub data fetching, release setup, staging and commits.
- Preserve argument quoting, status output and commit authorization across the new helper entry points.

## 0.9.3

- Add change-category and severity fields to resolve action items for implementation routing.
- Run PR checkout and conflict resolution even when no action items are selected; route available fallback implementation by change category while explicitly skipping unsupported complex work.

## 0.9.2

- Resolve shared paths through one helper and let packaged shell operations run on macOS when GNU `timeout` is absent.
- Remove subagent-local task tools from shepherd drafting.

## 0.9.1

- Prefer active registry installs and skip orphaned shared-provider cache versions; validate helper inputs and use NUL-delimited file paths.
- Bound vitality JSON reads and restore analyse follow-up questions while rejecting invalid report paths.

## 0.9.0

- Extract vitality score assembly and variable loading into Python helpers, renormalizing unavailable axes and loading all report variables in one JSON pass.
- Extract release setup, audit, GitHub fetching and resolve commit/staging operations into packaged helpers.
- Keep partial GitHub datasets explicit and validate Action SHA lookups before using them.
- Harden resolve parser output before sourcing it, refuse default-branch PR checkouts, and require a target-specific push confirmation.
- Restore pre-item stashes on exit paths and centralize staging in a dedicated helper.
- Route mixed documentation and CI PRs to the relevant two reviewers.

## 0.8.8

- Resolve shared paths and shepherd availability through packaged helpers, with explicit degraded behavior when an optional agent is absent.
- Reduce scraper tools to fetching and writing, and use lighter analysis effort for data-only vitality scoring.

## 0.8.7

- Parse resolve findings by contained section names, including numbered review headings.
- Refresh the review health checkpoint when output advances so active reviewers are not marked stalled; expose GitHub rate-limit evidence to the scraper.

## 0.8.6

- Port resolve and review argument parsers to Python, preserving shell-quoted outputs for flags, PR numbers, URLs and report paths.
- Use the `python` command consistently for plugin helpers and vitality extraction.

## 0.8.5

- Recognize security-intent commit bodies during release classification and link public advisories without exposing vulnerability identifiers in public notes.
- Explicitly support documentation and CI configuration PRs alongside Python review, while rejecting unsupported source-only languages.

## 0.8.4

- Add structured report headers and explicit skill scope exits; save release audit reports under `.reports/release/` with a terminal verdict.
- Keep follow-up gate choices tied to the skill's supported operations.

## 0.8.3

- Add CI-only review routing, keep software-engineer and challenger coverage for applicable reviews, and include performance review for refactors.
- Continue source review when CI is red, recording failing required checks in the report instead of aborting the review.

## 0.8.2

- Bound critical-finding verification to three agents, grouping findings when necessary.
- Add a confirmation gate when resolve selections exceed eight Codex dispatches, with batch and reduced-scope choices.

## 0.8.1

- Limit standard-library deprecation findings to public APIs, excluding private functions, classes, constants and modules.

## 0.8.0

- Add `--agent` selection to resolve implementation and use a three-tier review layout with literal report and run paths in agent prompts.
- Reuse a supplied review report directly in reply mode; keep small release notes and demos inline with a size guard before larger delegated work.
- Pass repository identity and a shared analysis timestamp to vitality scorers.

## 0.7.5

- Require explicit confirmation for a detected unannounced breaking change, stating the former behavior, new break and reason; prior deprecation followed by removal remains a separate classification.
- Remove the separate Comment column from resolve inventories.

## 0.7.4

- Launch linked-issue agents and Codex review in one batch; keep vitality report formatting and adversarial review with the orchestrator.
- Expand literal values in delegated prompts, verify shepherd output exists, and print only the corrected release classification table after self-review.
- Restore stashed changes before resolve commits so conflict markers cannot enter a committed result.

## 0.7.3

- Use all CI runs as the pass-rate denominator, classify test and CI dependency alerts separately from runtime exposure, and compute active CODEOWNERS from commit evidence.
- Require silence rate in responsiveness scoring and keep independent vitality review grounded in raw data.
- Calibrate resolve effort per item and skip QA only when every selected change is limited to lint, typing, documentation or formatting.

## 0.7.2

- Read vitality weights from the scoring rubric for both the main report and independent assessment instead of duplicating fixed weights.
- Offer grouped resolve-item selection and bulk actions, including separate required and suggested item batches for larger inventories.

## 0.7.1

- Condense agent and skill guidance, including quality checks, output envelopes and shared-provider fallback instructions.
- Separate vitality fetching from scoring and keep downstream notifications and tag publication as human actions.

## 0.7.0

- Split vitality into a GitHub data scraper and three parallel axis scorers, with raw JSONL, partial scores, a shared rubric and a structured report template.
- Keep shepherd communication draft-only and separate public reply drafting from direct GitHub publication.
- Scope analyse caches by repository and extract review parsing and release templates into reusable files.

## 0.6.1

- Record vitality data sources, actual record counts, plugin version and source revision for reproducible reports.
- Calibrate release-note classification separately from unregistered prepare, audit and demo modes; clarify unsupported resolve-flag handling.

## 0.6.0

- Replace repository health mode with vitality analysis, using timestamped GitHub evidence and weighted axes for maintenance, community, quality and security.
- Reject unsupported review and release flags with the supported syntax; make skill triggers and scope redirects explicit.

## 0.5.4

- Classify removal after a prior deprecation as Removed; reserve Breaking Changes for an immediate unannounced public break, and include migration examples for deprecations and removals.
- Seed reviewer prompts with a bounded Codex finding list and expand output-path variables before dispatch.

## 0.5.3

- Exclude an original change and its in-range revert from ordinary release sections, collecting their net-zero result under Reverted.
- Add adversarial release review and a final truth check against source evidence.
- Stop full PR review when required CI checks fail; run linked-issue analysis alongside Codex review.

## 0.5.2

- Monitor reviewer output for stalled agents and run independent review checks alongside the main reviewers.
- Hand resolve item details through files, retain source-comment links, and distinguish GitHub and report-derived action items.
- Standardize generated release notes on `DRAFT.md` and initialize shepherd's shared guidance before use.

## 0.5.1

- Make structural and semantic review context opt-in through `--codemap` and `--semble`; fail preflight with setup guidance when an explicitly requested provider is unavailable.

## 0.5.0

- Run release preparation as named sequential phases, producing a combined draft, optional changelog, summary, migration guide and runnable demo.
- Keep simple single-file resolve items available without Codex and explicitly skip complex items that need the missing provider.
- Add optional semantic companion searches for non-exhaustive or absent structural review context.

## 0.4.3

- Detect stable release branches from their first-parent tag and annotate cherry-picked changes in release classification.
- Read resolved PR review-thread status when gathering action items, and allow simple resolution items to use a software-engineer fallback when Codex is unavailable.
- Make skill boundaries explicit: analyse gathers evidence, resolve implements PR feedback, and review rejects unsupported source-language PRs.

## 0.4.2

- Add a three-tier agent router: use an installed role, select an available substitute, or fall back to a general-purpose agent.
- Execute a selected follow-up skill in the same turn instead of stopping after announcing it.

## 0.4.1

- Extract resolve argument parsing and review report templates; use compact per-mode summaries and report release-audit verdicts for orchestration.
- Resolve annotated Action tags to commit SHAs, and report documented stability-contract violations separately from SemVer findings.

## 0.4.0

- Consolidate release generation around notes plus `--changelog`, `--summary` and `--migration`; share triage, SemVer, release and contributor-voice guidance.
- Rename the CI agent to `cicd-steward` and distinguish workflow ownership from shepherd's release coordination.
- Reuse a fresh analyse reply report only after a type-aware activity check confirms that the thread has not changed.

## 0.3.3

- Run the normal lint and QA gate after single-comment resolution and commit automatic lint fixes through the resolve workflow.
- Suppress low-priority annotation and docstring nits when a review contains high or critical findings; clarify CI, lint and release agent ownership.

## 0.3.2

- Add adversarial PR review with an optional `--no-challenge` flag, task tracking, and a fallback challenger role when Foundry is absent.
- Ask for confirmation when a report path is supplied to analyse without `--reply`; render resolve action inventories as Markdown tables with explicit status and notes.
- Check documented behavior changes against deprecation requirements and keep narrowly scoped reviews focused on the requested surface.

## 0.3.1

- Accept `#123` as well as `123` for issue and PR input across analyse, resolve and review.

## 0.3.0

- Share one shepherd reply protocol between analyse and review; analyse replies now use the same summary and inline-suggestion format as PR review replies.

## 0.2.0

- Centralize Foundry agent fallbacks in the OSS shared resolver so analyse, resolve and review use consistent substitute roles.
- Initialize the review run directory before linked-issue agents launch, and warn when the shared file-handoff protocol is unavailable.

## 0.1.0

- Introduce OSS thread, repository-health and ecosystem analysis; GitHub PR review; review-action resolution and conflict handling; and release notes, changelog, migration and readiness workflows.
- Ship shepherd and CI specialist agents, with plugin-prefixed cross-plugin dispatch and general-purpose fallback roles when companion plugins are unavailable.
- Restrict `/oss:review` to GitHub PRs; route local file and diff reviews to `/develop:review`, and check issue/discussion reproductions before reporting.
- Track resolve action items and merge conflicts as tasks; add optional structural context for reviewers and keep public releases behind a human handoff.
