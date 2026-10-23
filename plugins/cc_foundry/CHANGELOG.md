# Changelog

## 0.70.1

- The runtime `blueprint-allow.js` hook refuses, even on a manifest hit, every command that can point a command name at another program: `hash`, `builtin`, `alias`, `enable`, `functions`, `eval` and their kin; any mention of the zsh `commands`/`functions`/`aliases` or bash `BASH_CMDS`/`BASH_ALIASES` tables, attached option prefixes included (`set -Acommands`); `set -A`, `read -a`, `printf -v`, name references and indirect expansions. Spellings that slipped past the earlier per-spelling checks, such as `set -Acommands ruff /bin/echo` and `builtin -- hash -p …`, are now refused.
- The same hook reads each command as the shell's own words before judging it: quotes, backslash escapes, `$'…'` strings and line continuations are resolved and joined, and redirections are set aside with their targets. `\rm`, `r""m`, `h$'a'sh`, `\-v` and `co""mmands` therefore count as `rm`, `hash`, `-v` and `commands`, and a redirection such as `printf 2>/dev/null -v …` or `2>"a b" set -A …` no longer hides the option or the command. A command word written as a pattern that can expand to a refused name (`/bin/r?`, `{ha,}sh`) is refused. The hook reads past the zsh precommand modifiers `noglob`, `nocorrect` and `-` and past a wrapper's own arguments (`timeout 5 set -A …`), and it judges the command text carried by a shell's `-c` (after options such as `-o shwordsplit`), a `trap` action, `watch` and `env -S`, so `noglob rm -rf x` and `zsh -o shwordsplit -c 'set -A …'` are refused. An option written after another option's argument (`set -o noglob -A …`, `read -t 5 -a …`, `print -f '%s %s' -v …`) still counts. Text the reader cannot settle is refused rather than guessed: a command that ends inside an unclosed quote; a `$'…'` escape bash and zsh decode differently, wherever it stands, assignment and redirection target included (a NUL byte, `\u…`, `\c…`, an unknown letter such as `\m`, an octal value above `\377`); an expansion inside a command word (`h${x}sh`); and a pattern among a wrapper's words or a POSIX bracket class in a command pattern (`command h[a]sh …`, `/bin/r[[:lower:]]`). A shell's `-c` script written after `--` is judged like any other. Short options are read as clusters, so `zsh -co shwordsplit '…'`, `zsh -xo name -c '…'` and `env -iS '…'` still reach the carried script, and every argument after a `-c` cluster is judged as script text. A `/` inside an expansion (`h${x:-/}sh`) no longer counts as a path separator. `unset -f`, `-F` or `-m`, which remove a function that shadows a command, is refused like `unalias`. A `-c` string counts as a bare variable (`bash -c "$METRIC_CMD"`) only when it is exactly that variable, so invisible Unicode whitespace around it no longer makes the hook and the generator disagree. An expansion where `printf`, `print`, `set`, `read`, `unset` or a declaring builtin still reads options (`printf "$o" …`) is refused, since its value may be `-v` or `-A`. A quoted or escaped prefix (`'h'${x}sh`) no longer hides an expansion inside a command word, and a bracket class holding a quote or backslash (`h[\!a]sh`) is matched as any one character. The command after a zsh short or brace form (`for x (a) cmd`, `select x (a) cmd`, `if true { cmd }`, `while … { cmd }`, `repeat 1 cmd`) or after bash `coproc` is judged like any other command. The `&` of `2>&1` or `&>` no longer splits a command in two, an escaped quote in `$'\''` no longer ends the string early (also when a line continuation splits `$` from `'`), a parenthesis inside a quoted string no longer ends a `$(…)`, and a `{fd}>` redirection is set aside like `2>`.
- The blueprint manifest generator applies the same rebind denial as the hook, by name instead of by spelling. It reads commands with the same rules as the hook, the refusals for text it cannot settle included. `command … -v` counts as a name lookup only when `-v` or `-V` stands among its leading options, so a trailing `-v` no longer exempts the command `command` runs. No shipped block uses any of those commands or tables, so no manifest loses an entry and every drop count is unchanged.
- The manifest allowlist no longer admits `eval "$RUNNER …"` or `eval "$(…)"`: a block that runs `eval` gets no manifest entry and prompts.
- Audit Sub-check 23e reports a command-name rebind in any shipped shell fence, including the option forms (`set -A`, `read -a`, `printf -v`, `declare -n`) and a rebind behind `command`, an assignment, a backslash, a wrapper or a redirection, so a new block that would lose its manifest entry this way is named with the reason. A `#` inside quotes, an escaped `\"` included, no longer hides the rest of the line from the check. A line ending in `\` is joined with the next, a command word with `$` after its first character (`h$'\x61'sh`) is flagged, and `print -v` after an option value such as `-f '%s %s'` is caught. Whitespace inside quotes stays part of its word, so `2>"a b" set -A …` keeps `set` in command position, and a `read` operand built by a backtick substitution is flagged. Redirections are set aside with their targets before matching, so `read 2>/dev/null "$n"`, `hash>/dev/null -p …` and `{fd}>/dev/null set -A …` are flagged. A rebind behind a wrapper's options (`command -- hash …`, `sudo -u root hash …`) is reported too, while a `command -v` lookup is not.
- Audit Check 45 prints its findings with `printf '%b'`, so a `%` in a reported path is no longer read as a format directive.
- The blueprint hook refuses a command whose raw text has a backslash-escaped blank at a line end or before `#`. The shell ends the command there, or keeps the rest of the line as an argument, while normalization would read a continuation or a comment, so a shipped digest could approve a different command. No shipped block contains the shape.
- The sentinel-read shape lane judges every word a `read` receives: a sensitive variable such as `PATH` named anywhere, attached to an option (`-aPATH`) or after an option's argument, or a computed name (`read "$V"`) refuses the command. Only the first name was checked before, so `read -r IGNORE PATH < file` could rebind command lookup without a prompt.
- The manifest generator gives no entry to a block that sources a state file, runs a variable's value as a command (`bash -c "$VAR"`, `$RUNNER …`) or writes heredoc data (`python - <<'PY'` code keeps its entry), emits per-line entries only for complete commands, and admits a mutating `bin/` helper only when it is listed with its guard (`heal_git_artifacts.py` and `merge_specialist_batch.py` added). Entries fell from 1298/2023/949/698 to 1090/1817/845/655 across foundry, oss, develop and research.
- A census test rebuilds every admitted manifest entry and fails when one sources a file, runs a variable as shell code, writes heredoc data, is a line bash cannot parse alone, or calls a helper whose source deletes trees or writes git history without review.
- `permissions-guide.md` no longer lists `bash -c "$CMD"` or sourcing a state file as allowed shapes, and describes the escaped-blank refusal.

## 0.70.0

- Helper scripts that write or read a session sentinel (`parse-skill-flags.py`, `extract-keep-flag.py`, `write_skill_contract.py`, `audit_preflight.py`, `state.py`) fall back to `/tmp` on POSIX when `TMPDIR` is unset, the directory a skill's `${TMPDIR:-/tmp}` names, instead of `tempfile.gettempdir()`, which picks `TEMP` or `TMP` first; with either set, script and skill read different files. Native Windows keeps `gettempdir()`. Path checks that confine a file to the temp dir (`settings_json.py`, `jq_write.py --slurpfile`, `--out-file` of the flag parsers) accept both directories. `claude-config.md`, `python-code.md`, `bin-authoring-guide.md` and the `/foundry:distill` executable brief prescribe the same fallback.
- The shared quality stack (used by `/develop:fix`, `/develop:feature` and `/develop:refactor`) runs `ruff`, `mypy` and `pytest` through a runner array that a closed `case` builds from the three detected literals (`uv run`, `poetry run`, `python -m`) instead of `eval "$RUNNER …"`: a missing, stale or planted `quality-stack-runner` session file now stops with `! BLOCKED — runner state missing or not one of …` and executes nothing (a missing file used to fall back to `python -m` even in a `uv` or `poetry` project), and failing pytest node-ids are retried as separate array elements.
- The runner fences of the shared quality stack are no longer pre-approved by the blueprint hook: `"${R[@]}" ruff` is an expansion in command position, which both gates decline, so the six runner blocks (and their propagated copies in `cc_develop`) prompt instead of running silently as the `eval "$RUNNER …"` form did (quality-stack manifest entries 107 → 101 in foundry, 112 → 106 in develop). An earlier design that taught the gates to trust fixed-text arrays was dropped after repeated review rounds kept finding new ways to overwrite a trusted array.
- The runtime danger check (`blueprint-allow.js`) and the generator's `is_dangerous` treat `uv run` and `poetry run` as carrying their wrapped command, flag `hash name=/path`, `hash -p /path name` (also the attached `-p/path` and bundled `-rp` spellings) and `builtin hash …`, which rebind a command word to another binary, and refuse blocks that may write the zsh `commands` or bash 4 `BASH_CMDS` tables in any spelling (`commands[x]=`, `commands=(…)`, `+=`, `${…:=…}`, `printf -v`, `read -A`, or a write nested in a lookup subscript such as `${commands[$(…)]}`); a plain `${commands[x]}` or `${commands[$name]}` lookup stays allowed. The generator's `uv run` check declines any option it does not know (`uv run -f ruff rm …`, where `-f` swallows the next word), so a value-taking option can no longer hide the command that really runs; before, that block was admitted.
- `Bash(eval:*)` is no longer in the shipped allow list and `/foundry:setup` removes it from `settings.json`, including an identical entry added by hand: ad-hoc `eval` commands now prompt, while shipped blocks no longer use `eval`.
- `/foundry:audit` Check 23c flags any `eval "$…"`, `eval $(…)` or `eval -- …` in a shipped skill, mode, template or `_shared` file (previously `_shared` files were never scanned) as high severity (was medium, `SKILL.md` only) with no exemption for `parse-skill-flags.py` or `derive_codemap_target.py`, and only lines inside code fences (backtick or `~~~`, blockquoted ones included, each closed only by a marker at least as long as its opener) are scanned, a comment inside a blockquoted fence stays a comment, so prose that quotes `` `eval "$(…)"` `` is never a finding while a comment mentioning `eval`, a quoted `#` or a backtick command substitution no longer hides a live `eval` in code; Check 30b's fix pattern uses a closed-`case` runner array. `permissions-guide.md`, `bin-authoring-guide.md` and the curator agent name `--keep-state` sentinel files as the replacement for `eval` of script output.
- The quality stack's failed-test retry reads a node id with one nested bracket level (`test_x[[a] b]`) whole instead of cutting it at the first `]` followed by a space, so the isolated retry targets the right test; deeper nesting still truncates, which selects nothing and fails loudly.
- `check_cli_flag_drift.py` stops reading a documented command's flags at a standalone `--` that is not inside a quoted value (a `$(…)` or backtick inside double quotes still ends the scope, quotes and backslashes are read the way the shell reads them, so a backslash inside single quotes such as `'C:\'` is a plain character while an ANSI-C `$'it\'s'` value honours its escapes, and a quoted value continued onto the next line with `\` is read as one value), and treats argparse's implicit `-h`/`--help` as defined unless every parser in the script sets `add_help=False` (a help-less parent parser shared through `parents=` does not count), so flags forwarded to a wrapped tool (`dev_run_tests.py pytest -- --tb=short`) are no longer reported as drift of the wrapper script.

## 0.69.1

- `/foundry:calibrate` A/B verdicts replace `none` with `baseline-ahead` (general agent ahead), `inconclusive` (both arms at the recall ceiling) and `no-lift`, evaluated first match wins; a mixed-sign result reports its negative delta as a trade-off, and older `none` ledger entries are documented as covering all three.
- `foundry:challenger`, `foundry:creator`, `foundry:doc-scribe`, `foundry:linting-expert` and `/foundry:manage` decline out-of-scope requests with one `! UNSUPPORTED — <reason>; owner: …` line and still write a mandated deliverable file or envelope; the shared file-handoff protocol adds an `unsupported` status that orchestrators surface as a routing miss, never as findings or a timeout.
- `/foundry:manage` matches `add perm` and `remove perm` as command words, so permission commands no longer trip the out-of-scope redirect.
- Severity output is anchored by consequence: `foundry:solution-architect` gains a severity rubric (compatibility breaks and startup cycles `[HIGH]`, governance gaps at most `[MEDIUM]`) and returns only findings for review-only asks; `foundry:perf-optimizer` caps constant-factor waste at `high`, keeps advisories out of numbered findings and marks static reviews `[Measured] none`; `foundry:linting-expert` defaults plain mypy errors and B006 to `medium`; `foundry:challenger` rates meta-findings and contagion by each finding's own failure mode.
- `foundry:qa-specialist` limits Coverage Gaps to documented or code-explicit contracts and moves unspecified behaviour to `Contract questions:`; `foundry:doc-scribe` treats a documentation standard as a hard filter only when the prompt makes it the acceptance criteria; `foundry:creator` lets the outline's `## Format` override house format defaults.
- `foundry:sw-engineer` reviews separate correctness bugs from suggestions and direct from latent bugs, with minimal fix diffs; read-only third-party release impact questions route to general-purpose, and `foundry:web-explorer` no longer sends local code analysis to `foundry:sw-engineer` without a fix request.
- `/foundry:humanizer` check mode rates findings by checklist group (Syntax high, Vocabulary medium, Formatting low) and flags triads only when vague; `/foundry:audit` accepts `permissions-guide.md`, `MEMORY.md` and configuration README targets instead of declining them.

## 0.69.0

- Narrow broad `find`, `node`, `claude`, and `sed` allows while retaining scoped read-only rules and broad `eval`/`python` for existing recipes; ad-hoc commands outside shipped blocks now prompt unless a narrower rule covers them. Setup removes the old broad entries from `settings.json`, including an identical entry added by hand.
- Shared keep-state helpers reject sentinel identities containing `..` before changing state; audit Check R4 also recognizes real tests that import or reference a script under another test filename, reducing false missing-test findings.
- `/foundry:setup` uses Python for every JSON read, probe and atomic settings write, so setup completes without jq on native Windows and other hosts while preserving existing env values and safe re-runs.
- The Windows Python launcher disables implicit current-directory executable lookup, preventing a checkout's planted interpreter from running during runtime selection.
- `/foundry:calibrate --local` help now states what the flag changes (rules target, mode files, cross-plugin availability check) and that agent/skill targets always come from the source tree, matching `resolve_agent_file.py`.
- `/foundry:setup` validates settings with Python instead of jq, so machines without jq keep a valid settings merge rather than restoring its backup; malformed JSON still triggers restoration.
- `/foundry:brainstorm` writes its question-delivery copies (`<slug>-branches.md`, `-tree.md`, `-spec-draft.md`, `-plan.md`) under a per-invocation `.reports/brainstorm/<ts>/` run dir printed at Step 0, so a later brainstorm or breakdown run on the same slug no longer overwrites an earlier run's files; rounds within one run still replace their own copy.
- `foundry:perf-optimizer` and `foundry:sw-engineer` replace basic tool-usage examples (py-spy/scalene/timeit/strace commands, a sample `pyproject.toml`, a Protocol class, an exception-hierarchy listing) with one-line principles, shrinking each agent's always-loaded prompt; the macOS SIP tracing caveat and the `pstats` read-back one-liner stay.
- `parse-skill-flags.py` no longer drops a `--keep` value whose quotes the shell consumed when the host substituted `$ARGUMENTS` (`--keep "a b"` used to arrive as `--keep a b` and persist an empty keep list); it now also accepts `--keep 'a b'` and `--keep="a b"`, strips a dangling `--keep` from `CLEAN_ARGS`, and warns on stderr when the value was unquoted or empty, so a lost compaction keep list is visible instead of silent.
- `/foundry:audit` Check 45 resolves a policy-sibling path against the marker file's own plugin root when it is not found from the repository root, so plugin-relative sibling paths in shipped files (codex-rig, research) no longer raise false `45-BROKEN` findings (20 on the current tree, now 0).
- `/foundry:brainstorm`, `/foundry:investigate`, `/foundry:distill` and `/foundry:calibrate` start with one `parse-skill-flags.py --keep-state` call instead of `extract-keep-flag.py` followed by an `eval` of `parse-skill-flags.py`: one process fewer per skill start and no `eval` of script output, with the same owner-safe stale-contract clear and keep sentinel. `/foundry:manage` and `/foundry:profile` drop `eval` too and read their flags from per-session sentinels. `parse-skill-flags.py` gains `--keep-state <prefix>`, `--out-file` and `--inlined` (synced from oss).
- `resolve_shared_path.py`'s last-resort source-tree tier now uses a cwd-relative `plugins/<dir>/<subdir>` only when the working directory is that plugin's own source repository (`plugins/<dir>/.claude-plugin/plugin.json` names the requested plugin). Otherwise the tier is skipped and the CLI exits 1 with a `!` message naming the refused tree, so a repository under analysis can no longer supply the shared docs that foundry loads via `load_shared_doc.py` or a sibling plugin loads via its own copy.
- `heal_git_artifacts.py worktrees` no longer deletes unregistered directories (orphans) by default: they are reported with a hint and removed only with the new `--include-orphans`, after a fresh re-check right before deletion (still unregistered, `git worktree list` answering, untouched since the sweep, no uncommitted work in its own `.git`). Symlinked children are reported as skipped instead of silently dropped from the report, and `--managed-prefix` rejects `*` (exit 2), so one sweep can no longer claim every child of a directory.
- `/foundry:audit` Check 23 and the curator agent accept a shared-doc load by either `cat` or a plugin `bin/` loader and no longer propose converting one to the other.
- `/foundry:audit` Check 28 and the Check 27 `_shared` path greps now also scan `plugins/*/claude-skills/`, so bridge and codemap-py skills get the cross-plugin and subagent_type checks.
- `/foundry:audit` Layer 1 gains the `agent-plugin-root` check (`bin/check_agent_plugin_root.py`): agent Markdown that writes `${CLAUDE_PLUGIN_ROOT:-plugins/<plugin>}` is flagged, because the host substitutes only the exact `${CLAUDE_PLUGIN_ROOT}` token in an agent prompt and the default form resolves to a repo-relative path an installed plugin does not have. `audit_static.py` exits 1 on a finding; a line carrying `plugin-root-check: allow` is exempt. The foundry agents now use the exact token. `audit_static.py --scan-dir <one plugin>` passes that plugin to `readme-drift`, `mode-dispatch` and `tag-symmetry` directly instead of reporting a findingless FAIL or skipping, and a checker that exits non-zero without findings is reported as an error with its stderr line.
- `/foundry:audit --efficiency` now flags a `model:` field on any inline skill (one without `context: fork`) in every plugin, not only foundry; on this tree that covers 9 skills outside foundry: `cc_oss` `resolve`, `analyse`, `review` and `release`, and `codemap-py` `query-code`, `integration`, `debrief-coding`, `rename-refs` and `test-impact`.
- `bin/plugin_layout.py` prints each plugin's manifest-declared skills and agents directories, and `--local` audits glob those instead of a fixed `*/skills/*/SKILL.md`, so a plugin that renames its skills directory (for example `claude-skills/`) is no longer silently skipped.
- Skills that run inline in the main session (`humanizer`, `investigate`, `profile`, `session`, `setup`) no longer set `model:`, which only applies with `context: fork`. The efficiency audit flags an inline skill that sets one, and `humanizer` leaves the haiku ceiling row and the isolation-spawn examples in the rules.
- `/foundry:audit`: the `/distill executables` follow-up option is gone from the gate, `--adversarial` Phase A spawns `foundry:challenger` (`--no-codex`) and Phase D reads `.py` files only, `--upgrade` accepts a change only on a capability A/B run with `--local` and a 0.05 noise floor, and `audit_preflight.py` stops with `BLOCKED` on an unsafe session id or a state directory owned by someone else. `/foundry:manage` prints the final `/foundry:audit` and `/foundry:calibrate` commands for the user to run, since both are `disable-model-invocation`.
- `check_bash_persistence.py` no longer misses cross-block variable references (Checks 41 and 43), also scans `modes/` files, and an explicit `--scan-dir` with no match no longer falls back to the current directory.
- `/foundry:calibrate` accepts `claude-skills` targets, passes `model=` for `--local` targets, records `severity_assigned[]`, adds a pace gate and ledger, and persists `TIMESTAMP` in apply mode; interactive skills (`create`, `brainstorm`, `manage create`) are excluded from the skill domain table. `/foundry:create` splits its first question. `/foundry:brainstorm` reviews its Step 5 output through `foundry:challenger`, caps D2 at 4, and keeps `/develop:*` and `/foundry:manage` out of its B3 invocations. `/foundry:distill` external mode reviews before writing, and `--project` defaults to the current project only.
- `quality-stack.md` reads its shared directory from the `qs-shared-dir-${CSID}` sentinel and stops with `BLOCKED` when it is missing; it also recognises `uv.lock`, `poetry` and pre-commit setups, treats MYPY as advisory, and spawns the Cycle 1 agents directly. `perf-optimizer` checkpoints with `git stash create`; `sw-engineer` routes pre-commit work and `doc-scribe` carries a NOT-for clause; `solution-architect` and `creator` fall back to a `needs_decision` envelope; `challenger` gets the `Skill` tool and a security scope.
- ! BREAKING: `resolve_skill_subdir.py` resolves the project-local tier only with `--local`, so `/foundry:manage create` no longer sees `.claude/skills` overrides.
- Bin hardening: sentinel and state writes are atomic and link-safe (`extract-keep-flag.py` also takes `--inlined` and skips a contract owned by another session), `jq_write.py` exits 3 on an unlisted flag, `write_skill_contract.py` rejects a multi-line value, `find-polluter.py` exits 1 on a timeout or an option-shaped test id, `check_bridge.py` accepts only allow-listed marketplaces, `heal_git_artifacts.py` rejects a `--pattern` that is not a bare file-name glob and keeps a worktree with an unreadable status out of the removable tier, and `resolve_shared_path.py` rejects `SUBDIR` traversal and reads its own name from `plugin.json`. `purge_plugin_cache.py` deletes nothing when the registry is unusable, `resolve_agent_file.py` rejects path components, `compact_gate.py` rejects a wildcard session id, `symlink_with_guard.py` rejects a degenerate marker or a source link leaving the plugin root, `check_codemap_guard.py` reads its `MANIFEST` with `ast.literal_eval` instead of importing the file, and `verify_blueprint_audit.py` survives a deeply nested record.
- `extract-keep-flag.py` refuses a keep sentinel another user already owns (exit 1 with an error naming the file, instead of reusing a planted value) and creates a missing `--out-file` state directory with mode 0700, so other users can no longer list it. Sentinel names are unchanged; native Windows skips the owner check.
- `merge_settings_permissions.py` merges the shipped permissions in the order they were written (no re-sort) and writes through a symlinked `settings.json`. The blueprint allow manifest drops 18 mutating command idioms, so those blocks prompt again instead of auto-allowing.
- The `/foundry:audit` skills checks moved from one `checks-skills.md` into `checks-skills-{gaps,bash,contract,crossplugin,deadfiles,duplication,sweep}.md`, which `checks-skills.md` now indexes. A `skills` scope run no longer loads the sweep file (Checks 35, 42, 43, 44; the `plugins` and `full` scopes still do), and the Check 27 and 30b self-exclusion greps cover every `checks-skills*.md` file.
- `write_skill_contract.py` takes a leading `--keep-sentinel <name>` or `--keep-file <path>` (not both) and appends the first line of that sentinel or file as `user-keep:` itself; a missing or empty source adds nothing. `audit`, `calibrate`, `distill`, `brainstorm` and `investigate` use it instead of an inline `read -r _KEEP` plus string append.
- `load_shared_doc.py --optional` prints a one-line warning and exits 0 when the plugin, subdir or file is missing, instead of `! BREAKING` and exit 1; the audit codex pre-pass and the Step 6 cross-validation load use it in place of an inline `[ -f ] || warn` block.
- New `compaction_boundary.py` runs the compaction contract resolve and gate as one call, finds the contract relative to its own plugin so an installed plugin works from any directory, and times out its child processes; it is propagated to `develop` and `oss`.
- New `codemap_changed_modules.py` lists the codemap modules touched by the current diff from `git diff -z`, so a path git quotes or one with a comma now resolves; `challenger`, `doc-scribe`, `perf-optimizer`, `qa-specialist`, `solution-architect` and `sw-engineer` call it instead of an inline pipeline.
- `extract_code_blocks.py` now finds fences inside blockquotes: it strips the opener's quote depth from content and close lines and skips a block whose quote ends before the close, so blockquoted fences that were missed before are now found.
- `jq_write.py` accepts `--slurpfile <name> <file>` (source confined to the working directory, `TMPDIR` or `~/.claude`) and writes `settings*.json` directly under `~/.claude`, so `/foundry:setup` no longer needs an unchecked `jq … > tmp && mv` write-back. `list_audit_files.py --paths-only -0` prints NUL-separated paths for `xargs -0`.
- `/foundry:manage` Step 4 (`create`, `rename`, `content-edit`, `delete`, `perm`) and `/foundry:calibrate` Step 6 (apply) moved into `modes/` files loaded on demand, so a run reads only the mode it uses. Duplicated rules in `manage`, `calibrate`, `distill`, `profile`, `investigate` and `brainstorm` are stated once.
- `/foundry:audit --efficiency` run alone now writes the `aggregate.md` and `summary.jsonl` the audit gate reads, and `check_codemap_guard.py` tracks the split `oss` dispatch files. Agent `tools:` grants match what each file uses: `challenger` drops `WebFetch`/`WebSearch` and `web-explorer` drops `Glob`.
- `quality-stack.md`: when doctests run as a separate pass (their collection failed, so they could not join the main run), a failing doctest now halts the quality stack with `GENUINE FAILURE`, and any other non-zero pytest exit except 5 halts with `CONFIG ERROR`. Before, that exit code was recorded and never read, so failing doctests were reported as `not-merged` and the run continued. Exit 5 (no doctests in the module) still passes.
- `resolve_shared_path.py` no longer executes a `plugins/<dir>/bin/get_plugin_install_path.py` found in the working directory: with `CLAUDE_PLUGIN_ROOT` unset and no cached copy it runs the helper shipped beside itself, so a repository checked out for review cannot plant code that runs during path resolution. The registry lookup reads `<home>/.claude/plugins/installed_plugins.json` explicitly and ignores a recorded `installPath` outside `~/.claude/plugins/cache/borda-ai-rig`, falling back to the cache scan.
- `/foundry:audit` without `--local` no longer claims to audit the installed plugin cache, which it never read. When the project's `.claude/` holds no agents or skills, the run records `⚠ UNSCANNED` for them and the report carries an `Unscanned Plugins` section instead of a clean result.
- `build_blueprint_manifest.py` admits an entry to `blueprint-manifest.json` only when every command it runs passes a fail-closed allowlist. An unknown command word (`ln`, `perl`, `strace`, an unlisted CLI) keeps the block out, so it prompts, while the reviewed shapes shipped skills rely on stay auto-allowed: read-only utilities, plugin `python` scripts, `python -c` with fixed code, `eval "$RUNNER …"` whose runner the same block reads from the skill's own sentinel or sets to a literal, sourcing or unlinking a session state file, `cp`, `tee`, `mv "$F" "$F.done"` consume-marker renames, `mv` (plain or `-f`) between two session state paths that name no variable except `TMPDIR`, `CSID` or one the block sets from its own state (so `.temp/$DEST` with an outside value still prompts), and wrappers whose wrapped command passes. On the same tree cc_foundry keeps 1292 of the 1336 entries the previous gate admitted. The blocks left out are the `perf-optimizer` `git restore` checkpoint revert, the `/foundry:setup` loop that `unlink`s and re-links files in `~/.claude/rules`, and documentation examples (`uv tool install`, `strace`, `uv pip`, placeholder `<script>.sh` blocks).
- The blueprint hook's danger re-check (`blueprint-allow.js`) and the generator's `is_dangerous` now see a command hidden behind a `case` arm label or inside a function definition, so `case x in a) rm -rf /;; esac` and `f() { rm -rf x; }` are refused even on a manifest hit. Before, the label or the `f()` word was read as the command and the `rm` passed. New shared cross-language vectors pin both languages to the same verdicts.

## 0.68.3

- `batch-nudge.js` runs on `PostToolBatch` instead of guessing parallelism from a 1.5 s gap between `PreToolUse` calls. The batch size is read directly, so a slow tool or a fast model turn no longer counts as a missed batch. The reminder is injected as `additionalContext` instead of `PostToolUse` exit 2 stderr, node starts once per batch instead of twice per tool call, and the reminder cites `foundry-claude-config.md §Turn Batching`, which exists, instead of a missing CLAUDE.md section. Verified on Claude Code 2.1.304; the docs give no minimum version, and a build without the event leaves the nudge inactive.
- `batch-nudge.js` keeps one streak per agent (`agent_id`). Subagents share the session id, so their single calls used to add up and nudge the wrong agent. A user turn resets only the main agent's streak. A subagent's file, `agent-<agent_id>.json` (so an id of `main` never touches the main agent's streak), is deleted on `SubagentStart` (spawn, resume, a teammate handling a new message) and on `SubagentStop`, so no streak carries into the agent's next turn and finished agents leave no file. A failed state write removes its temp file.
- `batch-nudge.js` counts a Bash call only when it is provably read-only, so `git add` → `commit` → `push` no longer draws a "batch these" reminder. Every character must be a letter, digit, space, tab or one of `_./=:@%,+-`. Quotes, escapes, braces, parentheses, `$` and globs let the shell rewrite words or run code after the check (`find . "-delete"`, `find . {-delete,-print}`, `git log -1 ${IFS}--output=x`, zsh `cat =(touch x)`), so such commands are not counted. Within that, `git` counts only for `log`/`status`/`diff`/`show`/`rev-parse`/`ls-files`/`blame` without `--output`, and `find` only without `-delete`/`-exec`/write actions. Strict on purpose: `2>/dev/null`, `git -C <path>`, pipes and `find -name '*.py'` are not counted.

## 0.68.2

- `write-guard.js` stops asking on every edit to a new file. A tracked file never asks now, `.claude/settings*.json` and `.pre-commit-config.yaml` included, and an untracked or gitignored file asks only for `CLAUDE.md`, `AGENTS.md`, `.claude/settings.json`, `.claude/settings.local.json`, `.pre-commit-config.yaml` and `pyproject.toml`. `.github/**` (scripts such as `.github/scripts/action_runner.py` as well as workflows), `CHANGELOG.md` and dependency lockfiles left the protected set: a new file there is untracked by nature, so each edit of work the user had just requested prompted again and protected nothing. A protected file that does not exist yet asks once on creation and is recorded per session; later writes to it in that session pass, while a file that already existed untracked keeps asking.

## 0.68.1

- `task-log.js` masks credentials before writing to `~/.claude/logs`: `NAME=value` env assignments (leading, after `export`/`env`/`&&`), credential-named assignments, `--token`/`--password` flags, JSON credential fields, URL credentials, `Bearer`/`Basic` values and well-known token prefixes (`ghp_`, `github_pat_`, `glpat-`, `npm_`, `sk-`, `xox*-`, `AKIA`, `eyJ` and others), in Bash commands, agent prompts, last assistant messages, Skill arguments and the generic tool summary. Control, zero-width and directional characters are stripped. Paths and `desc=`, `skill=`, `type=` values stay intact because `sessionEditedFiles` and `timing_analyzer.py` parse them.
- `task-log.js` creates the log directory `0700` and log files `0600`, and tightens a pre-existing one (owned by the user, not a symlink) the same way.
- File-handoff protocol: a tool or permission denial that blocked part of a task returns `done_with_concerns` with the denied tool named in `summary`, never `done`; the denied call or a bypass of it is not re-run (restructuring to an already-allowed form is fine), and `error` is used when nothing completed.

## 0.68.0

- The compaction boundary gate no longer holds a skill run. When compacting would pay off, `compact_gate.py` now prints `advise`: the skill starts its next phase right away and shows the `/compact` hint at the next point where its turn ends anyway (after a spawn, or in the next question). The old hold-and-wait behaviour is opt-in through `FOUNDRY_COMPACT_GATE=stop`, which prints `stop`. A timed wait is not offered because a Claude Code turn cannot resume on a timer.
- The `/compact` hint is one fixed short line, `/compact keep: skill contract .temp/state/skill-contract.md`, in its own code block. The old line copied the contract's `next:` text and ran to about 1,400 characters; a terminal copy wrapped it, and Claude Code turned the paste into a pasted-text block that never ran as a command. The PreCompact and SessionStart hooks already carry the full contract, so the gate no longer echoes it after the decision line.

## 0.67.5

- Rules now require that a baseline claim ("pre-existing on HEAD") names its tree and runs with explicit paths; a new check is classified as behaviour (pytest) or bookkeeping (pre-commit hook); mutation checks run on a copy outside the repository; an option that widens a grant is read before it is marked recommended; commit messages name behaviour in plain words, never a skill's internal step label; reviewer briefs that run adversarial shell cases require case files written with a file-editing tool and fake `gh`/`git` binaries.
- `write-guard.js` no longer asks before writing a protected file that git already tracks (`CLAUDE.md`, `AGENTS.md`, `CHANGELOG.md`, `.github/**`, `pyproject.toml` and the rest): every such write is diffable and revertible, and the hook's ask could not be remembered, so each edit prompted again. Untracked, new and gitignored protected files, such as `.claude/settings.local.json`, still ask, and `.claude/settings*.json` and `.pre-commit-config.yaml` always ask because they take effect before any diff review.
- The global self-improvement loop folds a correction into the project-owned rule, skill or agent file that governs it instead of a lessons file; `.notes/lessons.md` stays a legacy input of `/foundry:distill`.

## 0.67.4

- Select fixed context-composition fixture sessions explicitly so rendering tests remain valid after the checkout ages beyond the default 24-hour usage window.
- Recognize literal dictionary-key version assignments in continuity checks, preserving existing schema baselines while rejecting skipped versions.
- Keep review coverage and confidence gaps as factual limitations; require a specific user decision for additional review and omit declined requests from findings and final actions.
- Make the `git-approve`, `git-push` and `gh-write` questions readable: the first line states the ask (commit, push or write target), one blank line follows, then short fact lines; what each answer does moves into the one-line option descriptions instead of repeating in the question body. The `Approve always` description names what the grant never covers (push, force, history rewrite, discarding work, deleting unique history, auth changes). The `git-push` and `gh-write` `Approve` descriptions keep the token terms (15-minute validity; `gh-write` also re-hashes the files the command reads). Branch, command span and grant-file requirements for the approval hook are unchanged.
- Cross-validation protocol: state that aggregate `readiness: complete` requires every assessment `readiness: complete` and an empty `open_concerns`; one blocked or incomplete assessment makes the aggregate blocked or incomplete, and a missing required role or a `null` candidate makes it `incomplete`, overriding `blocked`, also under a null aggregate outcome. Resolver intake already enforced this; the protocol now says it.

## 0.67.3

- State the adversarial-loop progress table in the always-loaded quality-gates rule: after each validated round, print exactly the `Iteration | Critical | High | Medium | Low | Nits | Weighted score` table with all rows kept and each cell as `old + new`. The format lived only in the lazily read full rule, and a live loop report replaced it with a reduced table.

## 0.67.2

- Add a no-restating rule to the question-text guidance: a question never repeats rows, items or findings that the last printed text already shows or that its own options carry.
- Clarify the version-one review publication in the cross-validation protocol: string-array evidence and limits, open concerns only for UNVERIFIED or pending candidates, no complete readiness beside an open candidate.

## 0.67.1

- Show reviewer roles without execution provenance and render ratings in square brackets, keeping provenance in evidence and limits. Update the shared protocol and Foundry README to match.
- Register Develop’s historical index-directory configuration note as documentation so the Codemap guard check accepts the changelog while retaining executable guard checks.

## 0.67.0

- Add a boundary gate that checks calls-ahead and current context against estimated rebuild cost, verifies the owned skill-contract digest, attempts to log each decision, and prints the contract only when compaction is warranted.
- Add Claude and Codex context-composition reports, including usage deduplication and compaction detection; harden transcript parsing and token bounds in the cost analyzer.
- Extend skill-contract metadata with validated gate parameters and a session-scoped ownership stamp; save verified contracts at PreCompact and restore them after compaction.
- Adopt explicit compaction boundaries in phased workflows, with a safe continuation fallback when Foundry is unavailable and phase-specific preservation of reports and state.
- Build edited-file context from session timings, including MultiEdit and NotebookEdit, instead of relying on a truncated transcript tail.
- Require follow-up questions to name the report path and key outcome fields within the text budget; move concise plans and decision context into question text while retaining full report headers for final replies.
- Clarify compaction authority, question formatting, and approval scope in Foundry rules and CLAUDE.src.md.

## 0.66.1

- Require parent validation of review candidates at every severity before publishing actionable findings, retaining the source revision, contract, trigger, checks, counterevidence, environment, limits, and disposition.
- Separate source judgment from verification readiness; distinguish confirmed, refuted, already-fixed, optional, unfinished, and unavailable concerns, and preserve unresolved obligations with an owner and closure condition.
- Version the review ratings and finding records while preserving historical meanings and archive readers; reject incompatible current consumers and partial contract downgrades.
- Carry validation identity and retained evidence through report merging, dispatch, remediation, and late-wave checks; reuse proof only while it still applies, and keep a rejected repair separate from refuting the underlying defect.
- Align Foundry shared protocols and README guidance with the cross-plugin review contract.

## 0.66.0

- Guard `gh` writes with `gh-write-guard.js`: exact writes require a user-approved single-use grant, and force updates remain denied even with a grant.
- Add approval provenance through `approval-guard.js` and grant records: standing approval applies only to plain local commits; pushes and GitHub writes consume one-use grants, with self-grant attempts rejected.
- Generalize the shell Git lexer for Bash and Zsh invocation differences, and use it when checking commit-guard push commands.
- Add an optional GitHub-read lane to allow dispatch and allow audit verification to accept two- or three-lane verdicts.

## 0.65.0

- Show report headers, plans, findings, trees, and queues once as option previews, moving oversized output to a named file and unknown-flag or blocker prompts into the question text.
- Make `foundry:sw-engineer` spawns choose worktree isolation at the call site; update distill execution clusters to transplant each worktree patch and keep management scaffolds in the caller's tree.

## 0.64.9

- Add `rule-inject.js` to load spawn and commit rules lazily after the first matching tool call, including failed calls and compacted sessions.
- Deny `git push --force-if-includes` and `git push --mirror` as explicit backstops, and update Foundry's commit permission guidance.
- Resolve the current Haiku model at runtime with a 24-hour cache and fallback; disable thinking, raise its token allowance, and report resolver failures instead of treating them as an empty result.
- Put compact and repeated-failure guidance in the actual follow-up question, and update the model-window and cache-write guidance.

## 0.64.8

- Ground completion claims in cited source evidence and a representative real case; retain the real-case check through investigation, implementation, and verification.

## 0.64.7

- Wait briefly for transcript flush before rejecting a just-printed report header; explain visible-text failures and permit one identical follow-up retry without printing the table again.

## 0.64.6

- Make audit and profile delivery hooks check the current report at both the follow-up question and turn end; keep diagnostic and recovery questions available when delivery evidence is missing.
- Standardize confidence gaps as per-gap deductions that sum to the reported shortfall, including in calibration prompts and report templates.

## 0.64.5

- Add POSIX and native-Windows `python` launchers that select an eligible Python 3.10+ runtime, preserve workload arguments and exit status, and leave a real `python` earlier on PATH in control.

## 0.64.4

- Add a SessionStart rule check that detects missing setup-created `foundry-<name>.md` links, names the rule files to load, and injects the commit rule so an incomplete setup does not silently omit commit policy.
- Route registered hooks through Node.js from PATH or supported fixed locations, then exit cleanly when no Node runtime is available.
- Require co-author trailers to replace host-default attribution, with Codex attribution when Codex shaped the work and Claude attribution when Claude shaped the diff.

## 0.64.3

- Continue independent review-and-fix cycles while the same-scope nonnegative integer score strictly decreases; stop on a plateau, increase, lost authority, or exhausted caller budget, with no arbitrary three-review cap.
- Clarify evidence and contract handling in Foundry quality gates and the audit workflow, and make the strict-decrease rule visible in the quality-gate example.

## 0.64.2

- Make audit and calibration histories append-only through `append_ledger.py`: stage audit findings in `.rec` files and append calibration's per-target result files with `--from`, preserving prior history and avoiding duplicate legacy migration.
- Run isolation-motivated agent spawns regardless of task size, while retaining the ordinary budget rules for other spawns.

## 0.64.1

- Correct the GitHub repository API permission pattern and migrate only the exact retired setup entry, so unrelated permission entries remain untouched.

## 0.64.0

- Replace agent-wait polling with per-agent deadlines armed at spawn and checked on each wake; mark a missed deadline or notification without output timed out immediately.

## 0.63.2

- Register propagation of the shared native-question MCP implementation from Codex Rig to Codemap and Bridge.

## 0.63.1

- Record approved setup conflicts using the `rules/foundry-<name>.md` filename that the next phase checks, so approvals are recognized during linking.
- Split long Foundry prose into Markdown lists and shorter paragraphs.

## 0.63.0

- Warn at SessionStart when the loaded Foundry plugin root is marked orphaned or differs from the installed path, and tell the user to restart without blocking the session.
- At SessionEnd, sweep only registered idle `agent-*` and `oss-*` worktrees; retain dirty trees, ignored run output, other prefixes, and orphan directories.
- Shorten report template fields while keeping authoring guidance outside the completed report.

## 0.62.1

- Add audit and distill architecture notes describing phase overlap and ownership, and align the skills with those documented boundaries.

## 0.62.0

- Add audit efficiency checks for removable setup, loader, contract-write, and bookkeeping work while preserving model-decision boundaries.
- Load shared task-hygiene guidance in workflows and gate parallel spawn width using model-specific ceilings.

## 0.61.0

- Add a Foundry notebook-authoring rule covering notebook narrative, cell boundaries, plotting, and shell usage; propagate the canonical shared style to the plugin.

## 0.60.0

- Persist shared quality-stack variables across Bash blocks so batch mode and shell-variable checks use the intended values.
- Probe xdist for wide runs while keeping diagnostic retries serial; check collection before folding doctests into pytest, classify doctest assertion failures as genuine, quote failed node IDs through eval, and explicitly report the degraded unmerged doctest path.

## 0.59.2

- Split oversized Foundry bin helpers into smaller functions with maintainer-facing `main()` docstrings, improving navigation without changing their command contracts.

## 0.59.1

- Cap batched agent spawns at the configured stall bound so a single batch cannot exceed the workflow's wait budget.

## 0.59.0

- Strengthen producer-to-consumer evidence checks and validate host-manifest agreement and changed shipped version or schema continuity against committed HEAD; reject skipped or downgraded values, removed tracked manifests, and ambiguous unsupported selections.

## 0.58.2

- Set model-specific spawn ceilings and use them to batch Foundry audit and adversarial waves.
- Harden Foundry path utilities against destination overwrite, command injection, and traversal; bound file sizes and make hook-registry matching literal rather than regex-based.

## 0.58.1

- Bound independent review to three rounds including baseline, show the cumulative table once after each validated round, and require a parent action record for every unresolved finding.

## 0.58.0

- Add reviewer attribution and current-tool activity to the status line and task log, and clarify scope and handoff rules for adversarial review cycles.

## 0.57.5

- Show the full cumulative old/new findings table after each validated review round, preserving previous rows and excluding verified-fixed and rejected findings.

## 0.57.4

- Tighten Foundry's Codemap consumer contract: reuse only matching fresh answers, check query completeness, cap supplementary queries, and keep direct source reads available when Codemap is absent or incomplete.

## 0.57.3

- Add the short and detailed native-question guides to Foundry's propagation manifest so independently installed Codex-facing plugins can ship synchronized copies.

## 0.57.2

- Gate audit and profile follow-ups on saved report evidence and current-turn delivery of the required summary or matching header table; keep diagnostic and recovery questions available.

## 0.57.1

- Add an idle-compaction contract explaining how long-open questions can expire prompt-cache benefits and what context to preserve before compaction.

## 0.57.0

- Resolve the active Codemap provider's shared gates and context at runtime instead of shipping stale copies; degrade to Foundry's fallback when the optional provider is absent.
- Set a 15-second timeout on SessionEnd task-log and audit-close hooks.

## 0.56.2

- Shorten Foundry agent, skill, rule, and reference prose.

## 0.56.1

- Update SessionEnd retention for PR-scoped numbered OSS review runs: expire individual runs after thirty days and remove empty PR parents while retaining legacy flat-run handling.

## 0.56.0

- Preload the task tools before use and clarify that task-list state is session-scoped, so an empty list does not prove no work is running.

## 0.55.5

- Extend Foundry audit and adversarial guidance to check producer-consumer handoffs and ordinary user actions, keeping evidence and authority gates active across the whole route.

## 0.55.4

- Add a canonical shared-procedure copy and propagation check so Foundry installs the same procedure content that its sibling consumers receive.

## 0.55.3

- Require each agent task to begin with a distinct delta-first name, description, and task-leading line; check duplicate slots and follow the live tool fields rather than stale examples.

## 0.55.2

- Make independent Fetch, Read, and Match verification explicit for every URL, including when another URL on the same domain was verified.

## 0.55.1

- Replace Foundry skill flag parsing based on Bash-only regex capture and ad hoc text matching with shared argument parsers that work under Zsh as well as Bash.

## 0.55.0

- Add severity-weighted independent review and remediation loops with an original default cap of three iterations. Use score ratios to continue or stop; stop on structural findings or repeated failure signatures, and block completion or commits while security or critical findings remain open.
- Replace synchronous or polling assumptions with background-spawn, end-turn, and completion-notification handling; preserve missing-output timeout reporting, update audit checks for the background contract.

## 0.54.2

- Extract repeated Foundry skill shell blocks into Python helpers while leaving judgment steps, agent prompts, and output templates in the skills.
- Fix install-state detection for Claude's nested installed-plugin registry shape, and let investigation contracts record tried and probed candidates for safe post-compaction resume.

## 0.54.1

- Align Foundry Codemap guidance with the provider-owned consumer contract, preserving the active plugin root and the missing-dependency fallback.

## 0.54.0

- Unify auto-allow decisions under ranked provenance and shape dispatch; append decision and completion audit records, and add canonical record hashing with verify/prune commands. Logs are not tamper-evident. Verification exits 1 for record-hash mismatch, while framing and schema findings remain warnings; unusable inputs exit 2.
- Use ASCII status labels for success, no-op propagation, and orphan warnings so Foundry CLI output works on legacy Windows consoles.

## 0.53.0

- Expand the Foundry Python rules for cross-platform executables and semantic test selection, while trimming duplicated repository-level guidance.

## 0.52.1

- Expand maintainer-facing contracts and executable examples for Foundry Python helpers, including bridge checks, resolver-site classification, cost analysis, and timing analysis.

## 0.52.0

- Add explicit denials for dangerous Git operations and Claude's permission-bypass flag to Foundry's plugin permission policy.
- Add an untrusted-content rule: fetched pages, thread bodies, and pull-request text remain data, even when they contain instruction-like text.

## 0.51.3

- Add severity/fix requirements and precision discipline across foundry agents (challenger, creator, curator, doc-scribe, solution-architect, sw-engineer, web-explorer) so every finding states severity, cites a named criterion, and speculative items are labelled rather than dropped
- Reconcile self-recheck-ban premise (CLAUDE.src.md, quality-gates.md, rules/debugging.md, rules/\_full/debugging.md) with named-trigger re-scans already required elsewhere (shepherd.md, challenger.md)
- Consolidate model tiering to the 5-family: foundry:linting-expert haiku to sonnet, synced across curator.md's tiering table and README

## 0.51.2

- debugging.md (+\_full): forbid dispatching foundry:challenger as subagent_type fork; challenger gets diff+symptom+spec only, no implementer reasoning trail
- Stop after the same failure signature twice rather than exhausting the iteration cap; keep approved plans read-only during implementation unless the user explicitly authorizes revision.

## 0.51.1

- Define spawn discipline in Foundry configuration: use work-displacement spawns only when expected work exceeds their overhead, while preserving isolation-motivated review and role spawns.

## 0.51.0

- Propagate Foundry-owned quality-stack and cross-validation guidance into consumer plugins as namespaced files, with shared-file checks preventing drift.

## 0.50.1

- Add a pre-commit guard against references to tracked, repository-private ignored documents, including path traversal and separator variants.
- Tighten Foundry's specialist references while preserving routing rules, commands, thresholds, and execution contracts.

## 0.50.0

- Replace Foundry's legacy Codex rescue integration with explicit bridge implement, advise, and review calls, including unavailable and disabled behavior.
- Resolve the active plugin registry's nested structure, distribute Foundry's availability detector to each independent consumer, and add an audit utility that compares local Bash transcripts with installed blueprint manifests.

## 0.49.2

- Document Foundry's consumer-owned `skills/_shared/codemap-context.md` as the host for the v2 integration body, retaining v1 sentinels and read-only integration audit. It ships without a managed block until integration applies it.

## 0.49.1

- Close sentinel-read auto-allow bypasses by rejecting unsafe shell functions, traversal, write redirection, and loader or lookup assignments; retain only validated read forms.

## 0.49.0

- Guard writes to protected repository files and provide a recovery tool for Git metadata artifacts damaged by hooks.

## 0.48.0

- Add `heal_git_artifacts.py` to safely recover stale skill locks and orphaned Git worktrees, and propagate its canonical Foundry implementation through the shared-file manifest.

## 0.47.0

- Add a blueprint manifest builder and auto-allow hook so exact, predeclared skill commands can run without repeated approval prompts.

## 0.46.2

- Restrict Foundry test skips to collection-time capability probes, and consolidate polluter-finder coverage under one canonical test module.

## 0.46.1

- Anchor Foundry Codemap context and query paths at the repository root, and provide canonical consumer guidance plus audit checks for declared provider routes and CLI flags.

## 0.46.0

- `foundry:session` becomes one inline skill with six modes — `dump`, `recall`, `list`, `park`, `sweep`, `drop`; `park`/`sweep`/`drop` are new, replacing the behavioural parking rule that only ever loaded on invocation and so never fired
- `hooks/carryover-restore.js` renamed `session-restore.js`; store path, injection banner and the oversized-doc pointer now read `/foundry:session recall <slug>`; `hooks.json` SessionStart command updated
- `rules/compaction.md` manual-sibling note, `rules/foundry-config.md` log table, and distill's slug-divergence guard no longer name removed skills or logs; `calibrate/modes/skills.md` had the same skill listed twice, bare and plugin-prefixed — the bare row is gone
- README rows, mode listings, hook description, skill-directory count and test table synced in both `plugins/cc_foundry/README.md` and `.claude/README.md`; version 0.45.0 to 0.46.0

## 0.45.0

- Add `/foundry:carryover` and restoration hooks to preserve selected work context across session handoffs.

## 0.44.0

- Extend `/foundry:profile` with cost analysis and classify hook and resolver activity alongside session timing.

## 0.43.0

- Add a batch-nudge hook and route mechanical, single-file audit fixes to a lower-cost implementation path under explicit gates.

## 0.42.2

- Compress duplicated bin-authoring gate wording across Foundry manage spawn prompts.
- Replace the estimated compaction break-even with measured session observations.
- Convert two Foundry audit/calibrate loader sites to the appropriate resolver calls; require Python variable documentation in `#:` comments where a docstring cannot bind.

## 0.42.1

- Add an agent-budget note to Foundry spawn sites: account for fixed spawn overhead, avoid work-displacement spawns below break-even, and require partial results when an agent exhausts its call budget.

## 0.42.0

- Extract Foundry's shared-document and mode loaders into Python bin helpers, replacing repeated resolve-then-cat blocks in skill bodies.
- Bound supplementary Codemap queries to three and stop enumeration after a complete result while preserving required source reads; budget spawns and require honest partial-output envelopes.

## 0.41.0

- Move subagent-type telemetry evidence and a worked failure example into `rules/_full/CLAUDE-full.md`, leaving the mandatory rule in the always-loaded stub.
- Require evidence before factual claims and explicit disclosure when verification is unavailable; move worked rule detail to on-demand `_full` files while retaining mandatory stubs.

## 0.40.1

- Move agent supplemental references into `references/`; re-key agent records so SubagentStop removes them, use separate worktree and non-worktree age limits, and strengthen audit Bash-correctness and efficiency checks.

## 0.40.0

- Install Foundry rules under namespaced `foundry-<name>.md` links and migrate legacy links during setup.
- Adopt a destination only when its target resolves within this Foundry install or its matching marketplace lineage; preserve links into other plugins, checkouts, or dotfiles as conflicts.

## 0.39.1

- Use named string enums for closed option sets and split compound pre-commit checks into individually selectable subchecks.

## 0.39.0

- Give each plugin its own shared-file resolver and remove Foundry-path reach-ins from consumer workflows, so installed siblings can operate independently.
- Add an orphaned-plugin-cache cleanup helper that reports by default and supports guarded explicit apply. Setup passes the displayed candidate count and aborts cleanup if that count changes.

## 0.38.4

- Preserve Foundry shell variables and sentinel newlines across separate Bash tool calls, preventing fresh-shell workflows from losing state.

## 0.38.3

- foundry:audit — enforce-audit-header.js; also adds the report.md Write step Step 11 was missing entirely (Path: field pointed at a file nothing ever created); gates on summary.jsonl instead of report.md since report.md is written after the gate fires
- foundry:profile — enforce-profile-header.js; parses REPORT_DIR out of the Step 1 KEY=VALUE state file

## 0.38.2

- Add audit Check 41d to detect legacy numeric phase headers and require the current letter-suffixed phase convention.

## 0.38.1

- Use a fresh, narrowly scoped agent for new independent follow-up tasks; resume only when accumulated reasoning is needed to continue the same task.

## 0.38.0

- Add audit Check 45 for dangling and asymmetric policy-sibling references.
- Add policy-reference graph tracing to curator evaluation criteria and the fix workflow.

## 0.37.4

- Give Foundry's review agents WebFetch and WebSearch so they can verify factual reviewer claims against authoritative sources.
- Require each YAML report-header table cell to stay on one physical line so wrapped values do not break the table.

## 0.37.3

- Require review workflows to print the report header before presenting a follow-up question; standardize outcome symbols and report formatting.

## 0.37.2

- Move Foundry's Codemap consumers to the provider's command surface and register Foundry as a host of the shared structural-context contract.

## 0.37.1

- Update Codemap references to the `codemap-py` name and stop propagating the Bash sentinel-read hook into its Python-only hook tree.

## 0.37.0

- Add a Bash pre-tool hook that auto-allows only validated read-only sentinel and timestamp command substitutions; unsafe shell forms continue through normal permission checks.
- Document the safe sentinel-read form and scope it to the active session and temporary directory.

## 0.36.1

- Render report `---` YAML headers as two-column Markdown tables in terminal (never raw YAML — wraps badly); add table template to foundry quality-gates; reword communication.md exemption
- Add foundry task-lifecycle "Fleet-view description: unique-first" note

## 0.36.0

- Scope temporary sentinels to the current Claude session to prevent collisions across sessions and projects; move skill contracts and logs to their intended writable locations.

## 0.35.6

- Move Foundry under `plugins/cc_foundry` and update repository references while preserving the `foundry:` command namespace.

## 0.35.5

- Extract plugin-table trimming into a tested `trim_plugin_tables.py` utility and document it in the Foundry README.

## 0.35.4

- Replace emoji confidence bars with distinct plain `fit` and `conf` scores, add runtime-state write/delete permissions, and put unique task labels before spawn boilerplate.
- Update the Foundry README rule table to match the confidence display.

## 0.35.3

- fixes headless block: sync.sh runs `claude --print /foundry:setup --approve`, where a Write-tool permission prompt on ~/.claude/settings.json cannot be approved and hangs; in-bash writeback needs no tool prompt

## 0.35.2

- Require code-touching agents to check declared dependency APIs before adding utilities and verify public symbols from the installed library rather than relying on memory.

## 0.35.1

- Add post-write artifact-compression feedback, task-log error breadcrumbs, option-local confidence bars, and soft output caps.
- Replace implicit skill loads with explicit Bash loads.

## 0.35.0

- Add a five-step confidence display before multiple-choice questions, with a marked leading option; omit the display when choices are matters of taste.

## 0.34.6

- Shorten Foundry agent, skill, rule, and shared Markdown prose.

## 0.34.5

- Require each commit-message claim to match the final diff before commit, so reverted or never-landed work is not described as part of the change.

## 0.34.4

- Limit Markdown normalization to Edit operations and write atomically; Read no longer rewrites files or redirects the user to normalized copies.

## 0.34.3

- Restrict the hook to push approval: force-push remains blocked, regular push needs a user-armed one-use token, and commits use the prompt authorization rule.

## 0.34.2

- CLAUDE.src.md: add Proportionality and Legible deviation to Core Principles; add diff-against-ask bullet to Verification Before Done

## 0.34.1

- Add a five-second timeout to the Foundry agent-router embedding request so a stalled connection cannot hang PreToolUse or SessionStart.

## 0.34.0

- Add `--help` handling to Foundry bin scripts and audit Check 42 for drift between documented skill flags and argparse definitions.

## 0.33.0

- Add /foundry:humanizer skill — strips AI-writing tells (clichés, triads, weasel-word attribution, formatting tics) from human-facing prose, grounded in Wikipedia's AI-writing-signs checklist
- Sync foundry README and root README skill table plus hook test-coverage counts
- Refresh worktree-agent activity on tool events and measure staleness from last activity so long-running active agents stay visible.

## 0.32.4

- Add a decision gate for when a design needs an ADR, require design branches to be resolved one at a time, and document a write-delegation checklist.

## 0.32.3

- Make the statusline tolerate a missing agents directory and merge Codex subagents into the agent count.

## 0.32.2

- foundry: calibrate now confirms via AskUserQuestion before large fan-outs — mode-category scopes (all/agents/skills/plugins) always confirm since exact spawn count isn't knowable until Step 2; narrow scopes confirm past a 50-spawn threshold; --apply alone no longer bypasses this gate, only --skip-gate does
- foundry: extract audit SKILL.md Steps 4/5/5b/7 into modes/steps-4-5-7.md (52KB to 37.8KB force-loaded on every invocation)
- foundry: extract sw-engineer.md packaging/API-stability guidance into packaging-patterns.md, mirroring the existing hook-authoring.md pattern

## 0.32.1

- Add context-cost discipline, cap low-confidence audit reruns through a consolidated batch, and cache manage schemas for 24 hours.

## 0.32.0

- Add explicit compaction checkpoints and preserve contracts to Foundry's phased workflows, with per-skill checkpoint data, optional keep-items, and cleanup on completion.

## 0.31.6

- Layer 2 (judgment by domain): audit Step 3 now groups all of a plugin's files into one holistic foundry:curator for plugin scope, so cross-file breaks (tool-grant mismatch, contract splits, dead dispatch) that per-file batching misses get caught.
- manage: /foundry:calibrate mandatory only on routing-surface (description/trigger) changes, not every content edit

## 0.31.5

- Remove Foundry's redundant preflight-helper load, prune repeated deterministic curator checks and unused color metadata, and correct README and hook paths.
- Add `state.py` set, load, and clear namespaces to persist small shell values across independent Bash calls.

## 0.31.4

- At SessionEnd, prune completed run directories and temporary artifacts older than 30 days while preserving incomplete runs with no result file.

## 0.31.3

- develop/review: REVIEW_ARGS empty -> always working-tree-diff; \_FOUNDRY_SHARED empty -> cross-validation always SKIPPED; develop/feature: TARGET_MODULE empty -> codemap rdeps skipped
- foundry/profile: SINCE/SESSION_ID/TOP_N + REPORT_DIR (timestamped, not re-derivable) now persisted/reloaded; foundry/session: MEMORY_DIR re-derived; foundry/setup: PLUGIN_ROOT reloaded in 2 blocks

## 0.31.2

- Wire blocking pre-commit checkers for README drift, mode dispatch, shared-file synchronization, and shell-variable persistence; fix false positives and restore the lessons-to-memory dispatch.

## 0.31.1

- Replace RTK command-prefix allowlisting with positive read-only checks for guarded commands; compound shell commands and unproven commands now pass to the normal permission matcher.
- Add GitHub API mutation deny entries as defense in depth.

## 0.31.0

- Remove distill's review mode and point roster-boundary analysis to the Foundry audit workflow.
- Add audit Check 34 for roster-boundary overlap and gaps, and prohibit subagents from mutating task state.

## 0.30.0

- Use the most common case as the default shape for test-data factories, prefer plain functions for pure factory data, and flag newly duplicated code blocks across files.
- Rename distill lessons mode to memory; process all projects by default, support an interactive project picker, and parallelize multi-project analysis and curator writes.

## 0.29.1

- foundry: solution-architect opusplan→opus (removes plan-mode overhead); audit batching capped at 10 batches

## 0.29.0

- `foundry:distill` prune mode: multi-project `--project` flag with interactive picker across all `~/.claude/projects/` slugs

## 0.28.0

- Centralize validated installed-root resolution, retain background agent tracking after dispatch, and introduce health-sentinel boilerplate plus an audit fix hierarchy.
- Extract brainstorm breakdown into a mode file and include required descriptions when the agent router rewrites rerouted tool input.

## 0.27.8

- Remove unused `when_to_use` frontmatter from Foundry skills and move trigger and skip behavior into their active descriptions.
- Add explicit blocked-stop directives to Foundry skills that disable model invocation.

## 0.27.7

- Replace manual path-containment checks in the Foundry helper with `Path.is_relative_to()` for explicit root membership.

## 0.27.6

- foundry curator + audit: curator.md step 7 in §Code Block Authoring adds bash compression as P3 flag — multi-line if/fi with single body → &&/|| one-liner, \_rc capture only when format string contains %d, ;-joined sequential single-assignment lines, WHY-only comment rule; audit/SKILL.md adds verbose-bash-block to PARALLEL_SAFE_CATEGORIES so compression fixes can run in parallel without cross-file reads

## 0.27.5

- Add source-grounding rules to Foundry agent cards: verify behavioral claims against source or tests instead of memory or inference.
- Register Codemap blast-radius calibration and identify it as the calibratable subtask of development workflows.
- Print report YAML metadata first and exempt it from decorative boxes; offer a challenger review after substantive manage changes, with explicit trivial-edit and mode exemptions.

## 0.27.4

- Update Foundry audit checks and agent guidance to use Codemap's expanded index and query surface.

## 0.27.3

- Register OSS analysis vitality as a target that Foundry calibration can evaluate.
- Keep session reasoning and file contents out of spawn prompts; omit human box headers and footers from machine-parsed replies.

## 0.27.2

- Remove redundant explanatory comments from Foundry shell and Python blocks while preserving executable behavior.

## 0.27.1

- foundry/curator.md: added when_to_use to valid skill frontmatter schema (resolves curator↔audit Check 31a contradiction)
- foundry/checks-skills.md: Check 31a changed from presence check to duplication check
- foundry/skill-scaffold.md: when_to_use: documented as optional field

## 0.27.0

- Add /foundry:profile: session clock-time analyzer; timing_analyzer.py (691 lines) + SKILL.md + 9 pytest tests; buckets local-tool/agent-spawn/skill/idle/main-loop-reasoning from ~/.claude/logs/\*.jsonl

## 0.26.0

- foundry: add C901 + PLR rules to linting-expert ruff config template; [tool.ruff.lint.mccabe] max-complexity=12 and [tool.ruff.lint.pylint] max-args=12/branches=12/statements=50/returns=6; PLR2004 in ignore (magic-value too noisy)
- foundry: add Complexity Thresholds section to python-code.md — table with CC≤12, required-args≤7 (style), total-args≤12 (PLR0913), branches≤12, stmts≤50, returns≤6; refactor-first noqa policy
- foundry: add Python Code Complexity section to quality-gates.md — plain-language enforcement rules, required-vs-kwargs arg distinction

## 0.25.3

- Skip `.temp/` linting before lock acquisition, place the recommended option second, and batch challenger/verification for grouped changes.

## 0.25.2

- Extract hook-registry removal into a guarded Foundry Python helper, and add source-evidence requirements to Foundry quality gates.

## 0.25.1

- foundry/skills/brainstorm/SKILL.md — AQQ ≤4 cap compliance: branch selection via free-text reply, tree ops as category-picker with follow-up AQQ per category
- plugins/CLAUDE.md — markdown annotation rule: > for prose comments in plugin .md files; # only inside code fences

## 0.25.0

- Define safe routing for multi-value script output through temporary files, and prohibit `eval` as a data transport mechanism.

## 0.24.3

- foundry statusline: fix 0% bar after /clear vs genuinely full context

## 0.24.2

- Add an audit check-numbering convention using letter suffixes beginning at `a`; add checks 29a/29b and update the check index and related references.

## 0.24.1

- Set Foundry commit guidance to use the `claude[bot]` GitHub noreply address for Claude co-author attribution.

## 0.24.0

- foundry-config.md: add abstraction-first stop rule — when change touches 3+ files with identical content, find shared abstraction before writing
- plugins/CLAUDE.md: add compression tier table + token-first length unit convention (tokens primary, lines secondary)

## 0.23.3

- Keep tables and fenced code outside blockquotes so terminal rendering and copy/paste preserve their structure.

## 0.23.2

- Require both an architectural and a visual-domain signal before applying the CV/ML documentation checklist, and make architecture discovery language aware.
- Persist brainstorm viewer state across Bash calls and prevent create outlines from overwriting existing files.

## 0.23.1

- Add architecture antipattern guidance for oversized or misused interfaces and expand QA checks for shell-injection risks in subprocess usage.

## 0.23.0

- Update Foundry rule references after the shared agent instructions were renumbered, keeping cross-references on the current section.

## 0.22.3

- Harden native-Windows path serialization and containment, reject POSIX absolute paths during run-directory validation, handle home-directory fallback, and prevent duplicate feature dispatch for the same brainstorm specification.

## 0.22.1

- Replace hard-coded `/tmp` paths in Foundry setup and initialization workflows with the platform temporary-directory fallback.

## 0.22.0

- foundry-config.md: curator vs sw-engineer dispatch rule for .claude/ config edits
- comment-dispatch.md: sentinel via compute_commit_sentinel.py; lint+QA gate simplified; fallback → foundry:sw-engineer

## 0.21.2

- Show top-level effort level and active skills in the status line, tracking both Skill calls and user slash commands and clearing completed activity.

## 0.21.1

- Show Claude and Codex agents in one statusline group, include the model effort when available, and simplify agent and tool count labels.

## 0.21.0

- Rename `/foundry:init` to `/foundry:setup` and update Foundry skill references.
- Collect non-auto-fixable audit decisions before changes begin, add high-churn and recency guidance to commit review, and introduce a JSON writer plus health-sentinel updates.

## 0.20.0

- Replace Foundry shell helpers with cross-platform Python executables and add Python helpers for Bash-persistence and spawn-prompt validation.
- Verify that `CLAUDE_PLUGIN_ROOT` resolves to the requested plugin before using a shared-path fallback.

## 0.19.2

- Automatically arm the commit sentinel after an explicit user commit request; retain workflow and confirmation authorization, the separate default-branch question, and an independent authorization signal for push.

## 0.19.1

- extract token-bloat blocks to foundry/skills/\_shared/: design-artifacts.md, pytest-config.md, pytorch-tracking.md

## 0.19.0

- Add a registry-aware helper that resolves the active Foundry install path, plus root-cause debugging guidance and audit checks for executable boundaries and security risks.

## 0.18.0

- Convert Foundry business-logic shell scripts to typed Python helpers, leaving only policy-permitted shell wrappers.

## 0.17.0

- Add `extract_code_blocks.py` and a bin-authoring guide for classifying fenced code and deciding when to extract scripts; use token estimates and explicit gate/score/verdict columns in audit Check 33.

## 0.15.2

- Refactor Foundry skills to delegate repeated mechanics to existing `bin/` helpers while keeping workflow decisions in Markdown.
- Lead execution failures, including partial failures, with a visible bold Markdown failure block.

## 0.15.1

- adversarial.md: Phase A-prime — unconstrained foundry:curator pass, parallel with challenger; catches structural/operational gaps (confirmed failure with no exit 1, undocumented implicit deps, non-happy-path silent failures); Phase C deduplicates against standard audit

## 0.15.0

- Make audit Check 17 and efficiency Check 33 group code blocks by functional purpose, using syntactic similarity as a secondary signal; keep efficiency tables out of the base audit path.

## 0.14.2

- Route Foundry creator's release-note work to the OSS release role and clarify cross-plugin feature references in Foundry management guidance.
- Remediate per-file audit confidence below 0.80 through reasoning, documentation, and available Codex cross-check passes; consolidate findings, recompute totals, and explicitly report unavailable Codex.

## 0.14.1

- Refresh stale skill links during setup and unlink managed destinations before relinking them.

## 0.14.0

- Add shared-path and audit-mode shell loaders to remove repeated skill preflights, and port the polluter finder to Python.
- Add audit Check 33 to identify similar code blocks and assess extraction opportunities by within-file purpose and syntax.

## 0.13.2

- Strengthen Foundry challenger findings with numbered, evidence-backed required changes; add calibration-only model-tier guidance and scaffold templates for skills and agents.

## 0.13.1

- Normalize YAML frontmatter quoting in Foundry distill and manage skills for consistent metadata parsing.
- Run calibration categories sequentially and pipeline spawns in batches of five, with task state reflecting actual execution.

## 0.13.0

- Define YAML report headers and terminal rendering in the shared quality-gates rule; simplify audit follow-up choices to fit the host limit.
- Add `Skill` to the permitted tools where audit and review workflows dispatch follow-up skills.

## 0.12.2

- Track Agent tool launches at PreToolUse and remove them at completion, so the statusline displays agents reliably even when SubagentStart is missed.

## 0.12.1

- Rank commit subject types by product significance.
- Ask whether to mark-and-continue, fix, or abort when retries establish a flaky test; do not continue silently.

## 0.12.0

- Use the least-certain subfinding when aggregating confidence, and narrow image and tensor documentation guidance to CV/ML projects.

## 0.11.2

- Fix 32 audit findings: XML tag escaping (creator, curator), ls-td→find (audit, brainstorm, calibrate, distill), investigate run dir → .temp/investigate/, health-monitor loop compressed, calibrate general-purpose→foundry:curator for .md edits, manage mode headers clarified, brainstorm/calibrate plugin dependency caveats, init Write-tool for settings bootstrap, doc-scribe comment relocation, challenger/solution-architect XML cleanup, qa-specialist emoji system made display-only aliases, manage jq note corrected

## 0.11.1

- Standardize commit subjects as `type(scope): detail` and limit deprecation checks to public APIs.

## 0.11.0

- Separate final reports from intermediate `.temp/` handovers and require final reports to include Source Files provenance.

## 0.10.5

- Add explicit trigger and skip boundaries to Foundry specialist agents and relevant skills.
- Define the breaking-change gate in the versioning rules and require explicit user acknowledgment before audit continues past each breaking finding.

## 0.10.4

- foundry:manage — plugin-prefixed invocations (/foundry:manage); jq replaces python3 for settings check; DIRECTIVE variable made explicit in content-edit mode
- foundry:investigate — symptom/signals/hypothesis captured in bash vars before Codex spawn; inline Confidence block example; shell var refs replace literal placeholders in prompts
- foundry:calibrate — graceful-exit protocol (safety-net result.jsonl on any crash/exit); CODEX_MODES variable fix; phase timeout hint injected into subagent prompts; Skill added to allowed-tools

## 0.10.3

- Shorten Foundry agent, skill, rule, and shared Markdown prose.

## 0.10.2

- Resolve the default branch dynamically and require its additional five-minute authorization sentinel alongside the ordinary fifteen-minute commit sentinel.

## 0.10.1

- Add Foundry audit checks for orphaned empty structural blocks and align curator formatting and completeness guidance.
- Give a delegated audit fixer ownership of its follow-up gate and skip the parent gate on that route to prevent double-gating.

## 0.10.0

- Ask for clarification when Foundry skills receive unsupported flags instead of silently ignoring them.

## 0.9.6

- Add audit checks for identical Bash blocks repeated across three or more files and for inline Python permission problems.

## 0.9.5

- Require challenger and curator approval of each audit fix before spawning its implementer; retain blocked findings and reasons in the fix summary.
- Add Check 31 to find tool calls in skill bodies that are missing from `allowed-tools` frontmatter.

## 0.9.4

- Reset statusline agent badges immediately on `/clear`; remove per-turn lock files and clean stale session directories without deleting live concurrent sessions.

## 0.9.3

- Clear per-branch commit authorization sentinels at session start and when `/clear` is submitted, so approval cannot leak across sessions.

## 0.9.2

- Correct agent frontmatter names to unprefixed local names, quote skill argument hints for valid YAML, and register OSS analysis and release calibration targets.

## 0.9.1

- Add explicit fallbacks for optional OSS and Research roles, allowing Foundry workflows to skip cleanly when a sibling plugin is unavailable.

## 0.9.0

- Require QA agents to inventory public APIs and engineers to plan before implementation.

## 0.8.2

- Require Foundry quality-gate workflows to invoke a selected follow-up skill in the same turn rather than merely narrating the intended action.

## 0.8.1

- Add a resilience rule that prevents a plugin's recovery mechanism from depending on the plugin it is meant to recover, and raise challenger and creator effort to xhigh.

## 0.8.0

- Move Foundry executable helpers into the plugin's `bin/` directory and consolidate audit/calibration argument syntax.
- Make memory pruning read-only by default and require an explicit memory-write decision.

## 0.7.1

- Add a Codex cross-check to challenger with a `--no-codex` opt-out, convergence reporting, and surfaced failures; register Research verification cases for calibration.

## 0.7.0

- audit: `adversarial` mode (alias: `challenge`) — foundry:challenger Phase A + Codex Phase B + curator consolidation

## 0.6.1

- Classify manage content edits as trivial or substantive before dispatch, and rename the CI guardian role to CI/CD steward throughout routing references.

## 0.6.0

- Rename `foundry:self-mentor` to `foundry:curator`, and add a creator agent plus `/create` workflow for scaffolding agent definitions.
- Add a short pre-seeding exchange to brainstorm before it creates formal branches.

## 0.5.0

- Add a read-only, red-team challenger that tests plans and code across assumptions, missing cases, security, architecture, and complexity, then must attempt to refute each finding.

## 0.4.0

- Add an external-resource mode to `/distill` that analyzes a plugin or skill from a URL or local path, compares it with the current setup, scores adoption candidates, and presents adopt, adapt, discuss, or skip choices.
- Check for lesson conflicts before writing and show the resulting diff for review; start a fast calibration benchmark when the target has no prior run.

## 0.3.0

- Deny sudo, SSH, world-writable permissions, and recursive force deletion in Foundry policy; add plugin audit checks for Bash correctness and dependency cycles.

## 0.2.1

- Add `foundry:linting-expert` to agent-resolution fallback tables and provide an unconstrained deep-plugin audit prompt.

## 0.2.0

- Add a live browser tree viewer with JSON sidecar updates to brainstorm; require an approved design before implementation and distinguish rejected from resolved branches.
- Add a screenshot of the brainstorm tree to the Foundry README.

## 0.1.4

- Add a PreToolUse commit guard with a short-lived authorization sentinel and wire its lifecycle into `/resolve`; clarify report confidence templates and shared-file failure handling.

## 0.1.3

- Make `/foundry:audit` converge through up to five fix and revalidation passes, report remaining findings when the cap is reached, and write per-pass cross-file validation results.

## 0.1.2

- Clarify Foundry audit orchestration, batching, and delegation thresholds, and repair namespaced references among its agents and skills.
- Make sibling plugins able to operate without Foundry by providing role-description fallbacks when optional agents are unavailable.

## 0.1.1

- Add GitHub read-only guidance and a deny policy; merge denials during setup and support `init --approve` for noninteractive recommended choices.
- Remove the misplaced standalone `stats-reader.js` script from the hooks directory.

## 0.1.0

- Establish `plugins/foundry/` as the canonical distribution for agents, skills, hooks, and rules, with `/foundry:init` merging settings and linking or copying configuration into the host environment.
- Move the team protocol, permissions guide, configuration, and shared rules into the plugin; replace former `.claude/` files with links and remove duplicate permission and status-line configuration.
- Namespace Foundry skill and agent dispatch references and repair broken setup links so the split plugin can resolve its own workflows and cross-plugin roles.
