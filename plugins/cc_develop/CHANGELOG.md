# Changelog

## 0.44.0

- Blueprint manifest re-checks reject unsupported shell forms and command overrides. Stale or tampered entries cannot regain auto-approval through the following forms:
  - Parameter expansions beyond supported reads.
  - Arithmetic beyond read-only `$((…))` expressions.
  - Malformed or unterminated heredocs.
  - zsh glob qualifiers and misplaced array assignments.
  - Shell options outside `pipefail`, `errexit`, `nounset` and `nomatch`, including wrapped or constructed flags.
  - Wrapper options requiring operands.
  - Lookup-variable writes, including disguised names, invisible characters and arithmetic operands.
  - Unsupported `$` expansions and named-descriptor redirections.
  - Unmodeled brace compounds, including inside nested `case` arms.
  - Command-shadowing functions and `source`/`.`.
- Blueprint checks keep ordinary text operands and quoted-delimiter heredocs as data, while checking command substitutions across lines in unquoted heredocs. Quoted assignment text avoids false rejections, and dangerous commands cannot hide in expanded heredoc text. Shell script paths held in variables require `--` before the path to remain auto-allowed.
- Blueprint checks and sentinel reads accept literal string-only `printf`/`print` formats with fixed widths, literal percent signs and newline, tab or backslash escapes. Supported formatted output remains auto-allowed under the stricter checks. Nonliteral formats, non-string conversions and unsupported escapes are rejected. Every format word is checked against that closed list whatever its first character, so a dash-leading `print -f` or `printf` format can no longer write a lookup variable, while `print -rf`, `printf -- '-%s'`, `env printf` and inert quoted text stay allowed.
- Sentinel reads require plain target names and reject lookup variables, prompt strings, hook arrays and command tables. Only empty `IFS=` prefixes on `read` are allowed, preventing other field-separator writes and shell-environment changes. Arguments accept supported variable reads, literal or variable defaults and trims; unsupported expansions and named-descriptor redirections are rejected.
- Review → fix loops now keep a round ledger and progress table, making review history visible. Repeated invariants are tracked across all earlier rounds, including renamed findings, requiring root-cause evidence on repetition and stopping at the third occurrence. Action invariants must match their ledger findings after whitespace and case normalization, preventing inconsistent recurrence tracking.
- Filter reviews require grammar coverage across every reader, exposing unmodeled constructs as findings and preventing clean verdicts with incomplete coverage. Repeated filter failures require defaults that reject unmodeled input, preventing another narrow bypass fix.
- Debug compaction labels its test target `test_path=`. The block remains auto-allowed under lookup-variable write checks.

## 0.43.5

- The adversarial-loop rule and the challenger dispatch rule list a reviewer reply truncated before its required closing block (envelope or `## Confidence`) as an agent failure, so a cut-off reply is no longer scored as a complete round with fewer findings. Where the host can continue that reviewer, the parent asks it for only the remainder, in replies short enough to pass the transport, until the closing block arrives; a reply that adds nothing new records the failure.

## 0.43.4

- The adversarial-loop progress table shows security findings in their own `Security` column ahead of `Critical`, instead of folding them into `Critical`, so a reader sees how many open findings are security bypasses. Score weights and the `Weighted score` column are unchanged.
- The adversarial-loop rule treats any reviewer round that returns no deliverable, a refused findings write, an unparsable envelope, a safety-classifier stop or a `blocked` envelope as an agent failure, records its cause in `loop-report.md` (never as a round, where it would read as a clean review), re-dispatches that reviewer once with a brief that removes the cause, switches to another permitted reviewer route if the same one fails again or cannot be re-dispatched (no attempt left, no approval, or a cause no brief can remove), and stops as `independence-unavailable` only when no route remains, instead of letting the round drop out as `not-run`.

## 0.43.3

- Adversarial review loops treat a `foundry:challenger` round that returns no deliverable or a `blocked` envelope as an agent failure, never a silent `not-run`: the cause is recorded and the round re-dispatched once. Security-filter reviews keep Codex enabled, so the challenger runs the bypass search through its Codex cross-check and needs no separate Codex row.
- The file-handoff protocol lists the report names Claude Code refuses for a subagent Write (`findings.md`, `report.md`, `summary.md`, `analysis.md` and suffixed `findings-*.md`), so an agent names its file after itself and the report is no longer lost.

## 0.43.2

- The runtime `blueprint-allow.js` hook refuses, even on a manifest hit, every command that can point a command name at another program: `hash`, `builtin`, `alias`, `enable`, `functions`, `eval` and their kin; any mention of the zsh `commands`/`functions`/`aliases` or bash `BASH_CMDS`/`BASH_ALIASES` tables, attached option prefixes included (`set -Acommands`); `set -A`, `read -a`, `printf -v`, name references and indirect expansions. Spellings that slipped past the earlier per-spelling checks, such as `set -Acommands ruff /bin/echo` and `builtin -- hash -p …`, are now refused.
- The same hook reads each command as the shell's own words before judging it: quotes, backslash escapes, `$'…'` strings and line continuations are resolved and joined, and redirections are set aside with their targets. `\rm`, `r""m`, `h$'a'sh`, `\-v` and `co""mmands` therefore count as `rm`, `hash`, `-v` and `commands`, and a redirection such as `printf 2>/dev/null -v …` or `2>"a b" set -A …` no longer hides the option or the command. A command word written as a pattern that can expand to a refused name (`/bin/r?`, `{ha,}sh`) is refused. The hook reads past the zsh precommand modifiers `noglob`, `nocorrect` and `-` and past a wrapper's own arguments (`timeout 5 set -A …`), and it judges the command text carried by a shell's `-c` (after options such as `-o shwordsplit`), a `trap` action, `watch` and `env -S`, so `noglob rm -rf x` and `zsh -o shwordsplit -c 'set -A …'` are refused. An option written after another option's argument (`set -o noglob -A …`, `read -t 5 -a …`, `print -f '%s %s' -v …`) still counts. Text the reader cannot settle is refused rather than guessed: a command that ends inside an unclosed quote; a `$'…'` escape bash and zsh decode differently, wherever it stands, assignment and redirection target included (a NUL byte, `\u…`, `\c…`, an unknown letter such as `\m`, an octal value above `\377`); an expansion inside a command word (`h${x}sh`); and a pattern among a wrapper's words or a POSIX bracket class in a command pattern (`command h[a]sh …`, `/bin/r[[:lower:]]`). A shell's `-c` script written after `--` is judged like any other. Short options are read as clusters, so `zsh -co shwordsplit '…'`, `zsh -xo name -c '…'` and `env -iS '…'` still reach the carried script, and every argument after a `-c` cluster is judged as script text. A `/` inside an expansion (`h${x:-/}sh`) no longer counts as a path separator. `unset -f`, `-F` or `-m`, which remove a function that shadows a command, is refused like `unalias`. A `-c` string counts as a bare variable (`bash -c "$METRIC_CMD"`) only when it is exactly that variable, so invisible Unicode whitespace around it no longer makes the hook and the generator disagree. An expansion where `printf`, `print`, `set`, `read`, `unset` or a declaring builtin still reads options (`printf "$o" …`) is refused, since its value may be `-v` or `-A`. A quoted or escaped prefix (`'h'${x}sh`) no longer hides an expansion inside a command word, and a bracket class holding a quote or backslash (`h[\!a]sh`) is matched as any one character. The command after a zsh short or brace form (`for x (a) cmd`, `select x (a) cmd`, `if true { cmd }`, `while … { cmd }`, `repeat 1 cmd`) or after bash `coproc` is judged like any other command. The `&` of `2>&1` or `&>` no longer splits a command in two, an escaped quote in `$'\''` no longer ends the string early (also when a line continuation splits `$` from `'`), a parenthesis inside a quoted string no longer ends a `$(…)`, and a `{fd}>` redirection is set aside like `2>`.
- The blueprint hook refuses a command whose raw text has a backslash-escaped blank at a line end or before `#`. The shell ends the command there, or keeps the rest of the line as an argument, while normalization would read a continuation or a comment, so a shipped digest could approve a different command. No shipped block contains the shape.
- The sentinel-read shape lane judges every word a `read` receives: a sensitive variable such as `PATH` named anywhere, attached to an option (`-aPATH`) or after an option's argument, or a computed name (`read "$V"`) refuses the command. Only the first name was checked before, so `read -r IGNORE PATH < file` could rebind command lookup without a prompt.
- The plugin's blueprint manifest no longer auto-allows a line that is not a complete command on its own, a block that writes heredoc data, a block that sources a state file, or one that runs a variable's value as a command; those run after a prompt.

## 0.43.1

- Follow the active Codemap provider’s automatic missing/stale-index preparation contract, preserving explicit confirm-first and frozen preferences across later tool calls and nested query helpers.
- Disabling Codemap now disables only structural lookup: feature, fix, refactor and batch loops still select heuristic tests, execute them and propagate failures.

## 0.43.0

- Helper scripts that write or read a session sentinel (`dev_run_dir.py`, `dev_parse_args.py`, `dev_codemap_gate.py`, `codemap_resolve.py` and the other `bin/` sentinel helpers) fall back to `/tmp` on POSIX when `TMPDIR` is unset, the directory a skill's `${TMPDIR:-/tmp}` names, instead of `tempfile.gettempdir()`, which picks `TEMP` or `TMP` first. With either set, `dev_run_tests.py` reported an unresolved runner (exit 64) and the red gates could not see its verdict file. Native Windows keeps `gettempdir()`.
- Keep supported review source judgments separate from blocked verification and include the reviewed full commit SHA in report headers; retain explicit unavailable identity for unversioned or historical abbreviated sources.
- Keep the working-tree-base qualification for explicit review targets even with `--worktree`, so dirty existing worktrees and external paths are not labelled committed source.
- `/develop:fix`, `/develop:feature` and `/develop:refactor` run tests through the new `dev_run_tests.py` instead of `eval "$PYTEST_CMD …"` and `eval "$TEST_CMD"`: `pytest` mode appends literal arguments to the allow-listed pytest runner, `suite` mode also accepts `tox`, `tox -q`, `make test`, `npm test`, `cargo test` and `go test ./...`. A planted or foreign-owned `dev-pytest-cmd`/`dev-test-cmd` session file, or a project test command outside that list, or an unresolved sentinel, is refused with exit 64 and nothing runs, so a gate never mistakes a refused command for a failing test (1), an interrupted run (2) or a missing runner; a crash inside the wrapper and invalid wrapper arguments also exit 64, and a reader that closes the output pipe early does not change the status; a test command that itself exits 64 is reported as 2, never 1, so it cannot pass a red-demo gate. The gates in `fix`, `feature` and `refactor` name exit 64 as a runner that refused to start. Gates that cut output with `--tail N` keep the test command's own exit status.
- `dev_run_tests.py pytest` and `dev_run_repro.py` (`/develop:debug`) read a pytest exit of 1 as a reproduced failure only when the output carries pytest's failure summary (a whole `N failed … in Xs` stats line, or a `FAILED path::test` line naming a test node id; colour codes from `--color=yes` are ignored, and the same text inside an interpreter error message does not count): a missing pytest, a plugin that cannot load or a startup hook that exits 1 now returns 64 from the wrapper and a `GATE ERROR` from the debug runner instead of unlocking the fix step (with `-qqq --no-summary` a real failure prints no summary, so that setup is reported as no test run). The project's own `conftest.py` and pytest plugins are trusted: they run inside pytest and could print a counterfeit summary, so the check catches a runner that never started, not a project faking its own results.
- The `/develop:fix` reproduction and Outcome B gates and the `/develop:feature` doctest demo gate accept exit 1 only together with a `failed <nonce>` verdict that `dev_run_tests.py` writes to the `dev-tests-verdict` session file, where the gate exports a fresh nonce for every run and trusts the file only when it is a regular file the user owns: a Python that dies during start-up (for example a broken `PYTHONHOME`) also exits 1, and used to print `GATE OK` without any test running. The gate never writes the file itself, and the wrapper replaces it without following a link planted at its name. A session id outside `[A-Za-z0-9_-]` records no verdict, so the gate stops instead of reading a file another session shares. The example-script demo gate first checks that Python can run a script. The demo gate block no longer carries an inline comment that kept it out of the blueprint manifest, so it runs without a permission prompt again.
- `dev_run_repro.py` (`/develop:debug`) and `dev_run_tests.py --tail` enforce their 600 s cap on the whole run: output is read on a helper thread, so at the cap the test command is killed and the gate returns even when a test worker it started keeps the output open. Such a worker is not killed and runs until it exits by itself.
- The `/develop:feature` example-script demo gate reads the demo's own status through `pipefail` (no log file under `$TMPDIR`), accepts only exit 0 (feature may already exist) or 1 (red demo), and stops with `GATE ERROR` for any other status; before, every non-zero status counted as `GATE OK`, because the pipe reported the status of `tail`.
- The runtime `blueprint-allow.js` hook treats `uv run` and `poetry run` as carrying their wrapped command and flags `hash name=/path`, `hash -p /path name` (also the attached `-p/path` and bundled `-rp` spellings), `builtin hash …` and any write to the zsh `commands` / bash 4 `BASH_CMDS` tables (`commands[x]=`, `commands=(…)`, `+=`, `${…:=…}`, `printf -v`, `read -A`, or a write nested in a lookup subscript such as `${commands[$(…)]}`; a plain `${commands[x]}` or `${commands[$name]}` lookup stays allowed), so a destructive command behind one of those, or a rebound command name, is refused even on a manifest hit.
- `/develop:plan` and `/develop:feature` read the derived module and function from per-session sentinels written by `derive_codemap_target.py --keep-state` instead of running `eval` on its output; option abbreviations are not accepted, so a goal starting with `--keep` stays goal text.
- The propagated quality stack builds its runner array from a closed `case` instead of `eval`, so a missing, stale or planted `quality-stack-runner` session file stops with `! BLOCKED` rather than running or silently falling back to `python -m`. The runner blocks of the quality stack are no longer pre-approved by the blueprint hook (the runner array is an expansion in command position, which the hook declines), so they prompt where the `eval` form ran silently.
- `/develop:feature` counts only a failing run (exit 1) as the red demo: an interrupted, internal-error, usage-error, no-tests or refused run now stops with `! GATE ERROR` instead of `GATE OK`.
- `Bash(eval:*)` is no longer in the shipped allow list and `/develop:setup` removes it from `settings.json`, including an identical entry added by hand: ad-hoc `eval` commands now prompt, while shipped blocks no longer use `eval`.

## 0.42.1

- `/develop:review` test gaps default to `[low]` and reach `[medium]` only for a changed (or, for file and directory targets, reviewed) public entry point with no tests; the checklist anchors documented-contract violations at MEDIUM, and the blocking section sets placement, never severity.
- The `/develop:review` consolidator keeps each root-cause-merged row with its own validated severity and section, so `/oss:resolve` (requires `oss` plugin) can fold it into the survivor's item with the group's strongest routing.
- The shared file-handoff protocol adds an `unsupported` decline status that orchestrators surface as a routing miss instead of a timeout.

## 0.42.0

- Narrow broad `find`, `node`, `claude`, and `sed` allows while retaining scoped read-only rules and broad `eval`/`python` for existing recipes; ad-hoc commands outside shipped blocks now prompt unless a narrower rule covers them. Setup removes the old broad entries from `settings.json`, including an identical entry added by hand.
- Shared keep-state and codemap helpers reject unsafe sentinel slugs or session IDs before changing state, while codemap keeps its documented `shared` session fallback.
- `/develop:feature` keeps its TDD cycle count and 30-minute wall clock across Step 4 → Step 3 returns, so the 5-cycle cap now trips across review returns instead of silently resetting; each new run still starts from zero. `/develop:refactor` keeps one wall clock per run the same way (its cycle count still resets per Step 4 pass, as documented), and its Step 4 loop states a single increment site, so a change-test pair no longer counts twice.
- `/develop:refactor` writes its characterization-gate exit to its own `dev-refactor-gate-exit` sentinel instead of the `dev-gate-exit` sentinel `/develop:debug` also writes, declares `<module_name>`, `<target_module>` and `<test_file>` as substitution tokens (unsubstituted, `eval` parsed them as shell redirects), and no longer trips the mixed-goal split question on "add type annotations/hints" goals.
- `/develop:feature` runs the pre-change baseline suite once, at the pre-loop gate, instead of a second identical run in TDD step 1; no longer claims solo Step 4 spawns `foundry:qa-specialist` (edge-case tests come from team mode Wave 2 or the quality stack's Progressive Review); and the "no test suite" Branch Safety Guard branch now keys on the full-suite run exiting 5, not on a missing `$TEST_CMD` that the gate already blocks.
- `/develop:setup` validates the merged `~/.claude/settings.json` with `python -m json.tool` instead of `jq`: on a machine without `jq` (native Windows) the validation used to exit 127 and restore the backup over a good merge. Setup and the README now say to re-run `/develop:setup` after foundry's rules are delivered or removed, so the `develop-quality-gates.md` link switches between the delta and full form.
- `parse-skill-flags.py` no longer drops a `--keep` value whose quotes the shell consumed when the host substituted `$ARGUMENTS` (`--keep "a b"` used to arrive as `--keep a b` and persist an empty keep list); it now also accepts `--keep 'a b'` and `--keep="a b"`, strips a dangling `--keep` from `CLEAN_ARGS`, and warns on stderr when the value was unquoted or empty, so a lost compaction keep list is visible instead of silent.
- `/develop:debug`, `/develop:feature`, `/develop:plan` and the shared `--plan` pre-flight read parsed flags from per-session `parse-skill-flags.py --keep-state` sentinels instead of running `eval` on script output, leaving the compaction contract alone (`--inlined`). `parse-skill-flags.py` gains `--keep-state`, `--out-file` and `--inlined` (synced from oss).
- `dev_load_shared.py` applies the same source-repository gate on its cold path: with no usable `dev-shared` sentinel and no installed develop plugin, a working directory without develop's own manifest gets one `! BREAKING` line and exit 1, `--optional` included, instead of the planted document's text.
- `resolve_shared_path.py` without a registry or cache hit uses a cwd-relative `plugins/<dir>/...` tree only inside that plugin's own source repository (its `.claude-plugin/plugin.json` names it); elsewhere it exits 1 with a `!` message, so a repository under analysis can no longer supply the codemap-py contract or the foundry compaction gate path.
- `dev_shared_resolve.py` without an installed develop plugin prints the cwd-relative `plugins/cc_develop/skills/_shared` only when the working directory is the develop source repository (its `plugins/cc_develop/.claude-plugin/plugin.json` names `develop`). In any other repository it exits 1 with a `!` message and `--init` writes no sentinel, so a repository under analysis can no longer supply the protocol docs a develop skill loads.
- `heal_git_artifacts.py worktrees` keeps unregistered directories unless `--include-orphans` is passed, re-checks one right before deleting it, reports symlinked children as skipped and rejects a `*` managed prefix. The worktree-isolation leftover report and its approved removal both pass `--include-orphans`, so the list you approve and what gets removed stay the same.
- `/develop:plan`, `review`, `fix`, `refactor`, `debug` and `feature` load their shared protocol docs through the new `bin/dev_load_shared.py`: one command per load instead of a sentinel read, a fallback and a cat. An empty or stale shared-dir sentinel now re-resolves the install instead of failing on a dead path.
- `/develop:review` challenger brief now uses the consolidator's severity map (Blockers → critical/high, Concerns → medium, Nitpicks → low), so challenger findings rank the same as the other reviewers'.
- `/develop:feature`, `/develop:fix`, `/develop:debug` and `/develop:refactor` keep a quoted `--keep` spec intact, read the cleaned arguments for slug, issue and Codemap-target detection, and write the negated-flag sentinels under a variable-named file too (for example `challenge-enabled`), keeping the flag-named file as an alias.
- Sentinel writes in the bin helpers are atomic (temp file, then replace), so a symlink planted at a predictable sentinel name in a shared temp dir is replaced, never followed; the plan namespace stays confined to `dev-plan-*` under the temp dir.
- Sentinel writers in the bin helpers refuse a sentinel another user already owns (the write fails with an error instead of reusing a planted value; the codemap currency status is skipped), every directory they create is mode 0700, and the codemap gate rejects a plan namespace that is a symlink, a regular file or another user's directory. `extract-keep-flag.py` gains the same owner refusal and 0700 state directory. Sentinel names and paths are unchanged, so existing skill reads keep working.
- `/develop:fix` and `/develop:debug` gate on the real pytest exit code: only exit 1 prints `GATE OK`, exit 4, 5, 127 or any other code prints `GATE ERROR`, and an empty runner command is refused instead of being misread as a reproduced bug. `bin/dev_run_repro.py` now runs the reproduction.
- `/develop:fix` runs the final gate once when the project's test command is not plain pytest, since the quality stack only runs pytest; a NOT-for or non-Python abort now points to direct editing or the native toolchain, not `/develop:feature`.
- `/develop:feature` and `/develop:refactor` `--team` runs now execute the Step 5 quality stack and the final gate before the Final Report, and never report the suite green earlier; the feature challenger decision is two-state and the impossible pre-implementation QA challenge is gone.
- `--team` modes drop the liveness probe and the `*-team-check` sentinel; teammates are watched through armed `agent_watch.py` deadlines (1800 s), and spawn prompts carry `name` and `description` slots with a delta-first first line. `bin/setup_worktree.py` no longer accepts or creates a health sentinel.
- `/develop:feature` team `sw-engineer` may create new `tests/` files for its own red-green tests; existing test files stay QA-owned.
- `/develop:review --worktree` now reviews the last commit: the resolver gains `--committed` (`HEAD~1..HEAD`), because a fresh worktree starts clean at `HEAD`. `resolve_review_target.py` exits 1 for a missing path or a target with no Python file, caps its Python scan at 200k entries, and the review skill stops on that exit.
- `/develop:review` consolidator records new or strengthened claims as null-disposition candidates with an open concern and lists them in the new envelope field `new_claims`; `Edit` is dropped from the review tools.
- `/develop:plan` keeps `CLEAN_ARGS` in the plan namespace for the slug, keys the depth checkpoint on the goal, lets Revise edit the plan in place, and routes blockers through `AskUserQuestion`; the breaking-change gate revises once and then re-gates.
- `ci-log-extract` fetches, strips and falls back in one self-contained fence and reloads the digits-only run ID; batch mode ships its run fence with an owner stamp so a stale `BATCH_ID` from an aborted run is discarded.
- Issue bodies are fenced as untrusted data, an invalid `dev-upstream` repo override exits 1, `issue_fetch --repo` is validated as `[HOST/]OWNER/REPO`, option-shaped pytest targets are rejected, `--log`, `--record-dir` and the codemap batch output are confined to the working directory or temp dir, plan-mode `--pytest-cmd` is checked against the runner allowlist, and `extract_json_field` caps input at 1M characters.
- `dev_shared_resolve.py` announces its working-directory fallback on stderr; `sync_rules.py` rejects a plugin name outside `[a-z0-9][a-z0-9-]*`; the codemap currency sentinel is written atomically.
- The codemap gate, issue fetch and worktree setup wrappers time out (60, 120 and 30 s) and fail closed; new shared-sentinel and wrapper tests cover them.
- `/develop:setup` probes the bare `python` command only, with no `python3` or `py -3` fallback, since every later step runs bare `python`.
- The shared agent-resolution fallback tiers for `qa-specialist` and `linting-expert` are `sonnet`, matching their frontmatter.
- The quality stack uses `uv run` only when the project has a `uv.lock` and `poetry run` only with a `poetry.lock`, so a project without them no longer fails on a missing runner. It runs ruff through the project's pinned pre-commit hooks when the config pins it, avoiding version drift against CI, and saves the loading plugin's shared directory to a sentinel so later steps stop with `BLOCKED` instead of reading an empty path.
- `resolve_shared_path.py` rejects an absolute (`/etc`) or empty-segment (`skills//_shared`) subdirectory and a plugin name with a trailing newline, which could resolve to a path outside the plugin install. It also reads its own plugin name from `plugin.json`: in the installed cache the directory name is the version number, so the own-plugin lookup never matched there.
- `extract-keep-flag.py` no longer clears a compaction contract that another session owns or that an inlined `--keep` already wrote, so a parallel session's resume state survives. It also validates the slug and session ID and writes its sentinels atomically.
- `verify_blueprint_audit.py verify` reports an audit-log line nested deeper than Python's recursion limit as a damaged line, like invalid JSON, instead of crashing the whole check.
- The report-delivery hook recognises a `/research:topic` follow-up question that offers `/research:topic plan` as that workflow's own gate, so its report path, title and header-table checks apply instead of being skipped.
- `/develop:review` moves each dimension brief and the Security augmentation into its own `templates/brief-*.md` file; the parent passes only the directory and each child reads only its own brief, and `batch-mode.md` moves its bisect section to `_shared/batch-bisect.md`, loaded on the failure path only.
- Skills load `worktree-isolation.md`, `codemap-context.md` and `ci-log-extract.md` only when worktree mode, codemap or a CI run ID applies, and `fix` and `refactor` no longer load `preflight-helpers.md` twice.
- New `bin/dev_batch_gate.py` prints the batch-mode verdict for `feature` and `refactor`; `dev_run_dir.py --persist <skill>` writes the run-dir sentinel atomically; `dev_shared_resolve.py --init` writes the shared-dir and agent-watch sentinels in one call; `dev_test_targets.py --skill {feature,fix,refactor}` reads the runner and run-dir sentinels, and an empty runner exits 1.
- Compaction boundary blocks in `fix` and `review` call `bin/compaction_boundary.py`, and six contract writers pass `--keep-sentinel dev-<skill>-keep-items` instead of reading the keep sentinel inline.
- `/develop:setup` merges the shipped allow and deny rules through `merge_settings_permissions.py` in one atomic write, so allow can no longer land without deny and the deny-rollback branch is gone.
- `/develop:debug` aborts with `! BLOCKED` when the symptom-mode test path is left unsubstituted or empty, and its timeout handling relies on armed `agent_watch.py` deadlines instead of a sentinel and `find -newer` probe.
- Test-run policy, substitution-token and issue-ID parsing rules are stated once per skill and cross-referenced; `plan` no longer mentions Codex corrections or a Codex design review it never runs.
- `/develop:debug` derives its file-name slug once after mode selection, so the Step 1-3 analysis, blocker and hypothesis files and the Step 4 diagnosis share one name; issue mode stops with `! BLOCKED` instead of fetching an issue from raw, flag-carrying arguments; the compaction contract keeps the test path; team mode persists its run-dir timestamp; the lead cross-checks competing hypotheses inline, and the Step 1 `sw-engineer` writes its analysis file as a watched deliverable.
- `/develop:fix --team` prompts read `~/.claude/TEAM_PROTOCOL.md` (the `${HOME}` form never expanded inside a prompt; same fix in the feature team prompts and the shared spawn template), no longer send teammates to the lead-facing spawn template, and the health check stops instead of inventing a fresh run-dir timestamp when its sentinel is lost.
- `/develop:feature` and `/develop:refactor` stage checkpoint lines and accepted-risk records in `checkpoint.md.rec` and append them with `append_ledger.py`, like `/develop:fix`, so the resume offer has step lines to read.
- `/develop:plan` saves a prose-wrapped feasibility reply to `.temp/develop/` and recovers the role verdicts from that file, replacing the unrunnable `echo "$RESPONSE"` pipeline.
- `/develop:plan` now writes a compaction contract before each gate that waits on you (debug-cycle cap, breaking change, challenger blockers, final Proceed/Revise) and ends those questions with a `/compact` hint naming where the scope findings or plan are saved, so compacting during a long wait resumes without losing the goal or plan path; a crashed run's stale contract is cleared at start and the contract is removed when the plan ends. The effort-sizing and challenger blocks now stop with `! BLOCKED` on a lost namespace sentinel instead of reading paths at the filesystem root, and the plan namespace directory is created private (mode 700), so other users on a shared temp dir cannot read or plant plan state in it.
- `--team` in `/develop:feature`, `/develop:fix`, `/develop:refactor` and `/develop:debug` checks for `~/.claude/TEAM_PROTOCOL.md` before creating a run directory and stops with one shared `! BLOCKED` line naming `/foundry:setup` when it is missing — feature no longer offers to silently continue solo, and the other three no longer spawn teammates told to read a missing file. Team spawn blocks also stop when the run timestamp or run-dir sentinel is lost, instead of building empty output paths.
- `/develop:debug` drops the partial-install check for `/develop:fix`, which ships in the same plugin and so could never be missing.
- `/develop:review` loads `codex-prepass.md` with `cat` like every other shared doc, and prints the report date and the Codex-output path from shipped blocks instead of assigning them in prose.
- `dev_parse_args.py` spec mode rejects a variable name that is not a shell identifier, since its `VAR=value` output is `eval`'d; `read_sentinel` ignores a sentinel owned by another user, so a value planted in a shared temp dir never reaches a command.
- `/develop:review` detects a PR number after flag parsing: `dev_parse_args.py` classifies the flag-stripped target into the new `dev-review-pr-number` sentinel, so `42 --full` now offers `/oss:review 42`, a quote or newline in the arguments no longer breaks the gate block, `/oss:review` receives only the bare number, and a missing clean-args sentinel reviews the diff instead of the raw argument text.
- `/develop:review` checks for the oss plugin through Claude Code's install record instead of globbing the plugin cache, so an orphaned oss version left in the cache no longer offers a `/oss:review` that cannot run.
- `/develop:review` ships its own severity checklist (`templates/review-checklist.md`) instead of reading the oss plugin's, so review agents and the consolidator apply severity anchors without oss installed; PR/issue-linkage patterns a local diff cannot satisfy are left out.
- `/develop:fix` Part A reports a candidate test that could not run (pytest exit other than 0 or 1, such as a rejected runner) as `! GATE ERROR` instead of letting it pass for "Outcome A: fails already".
- `review_impact_tier.py` reads at most 10 MiB of diff-impact JSON; larger input is not parsed and `/develop:review` falls back to the `FULL` lineup instead of holding the whole payload in memory.
- The quality stack (`foundry--quality-stack.md`) halts on a failing doctest in the separate doctest pass (`GENUINE FAILURE`) and on any other non-zero pytest exit except 5 (`CONFIG ERROR`), instead of recording the exit code and reporting `not-merged` while the run continued.
- `resolve_shared_path.py` no longer executes a `plugins/<dir>/bin/get_plugin_install_path.py` found in the working directory; it runs the helper shipped beside itself, and ignores a registry `installPath` outside `~/.claude/plugins/cache/borda-ai-rig`.
- `blueprint-manifest.json` is regenerated with the allowlist gate: 906 of the 936 entries the previous gate admitted stay auto-allowed, including the quality-stack `eval "$RUNNER …"` fences and the `eval "$PYTEST_CMD …"` and `eval "$TEST_CMD"` gates. Only single lines lifted out of those blocks drop out, because their runner variable or `case` context lives elsewhere in the block; the whole blocks still match.
- The bundled `blueprint-allow.js` refuses a manifest hit that hides a destructive command behind a `case` arm label or inside a function definition (`case x in a) rm -rf /;; esac`, `f() { rm -rf x; }`), which it let through before.

## 0.41.1

- Sync `write-guard.js`: a tracked file never asks, and an untracked or gitignored file asks only for `CLAUDE.md`, `AGENTS.md`, `.claude/settings*.json`, `.pre-commit-config.yaml` and `pyproject.toml`. `.github/**`, `CHANGELOG.md` and lockfiles are no longer protected, so editing a newly created file there no longer prompts on every edit. A protected file that does not exist yet asks once on creation; later writes to it in the same session pass.

## 0.41.0

- `/develop:plan` step tables gain a `Verification` column for simple and staged plans (fix plans already had one), and each saved `Suggested approach` step ends with `— verify: <check>`, so `/develop:feature` and `/develop:fix` receive it.
- `/develop:feature`, `/develop:fix` and `/develop:refactor` with `--plan` (or an inline-generated plan) record the plan file's hash through the new `bin/plan_hash.py` and re-check it first in the Final Report; a change leads the report with `! BLOCKED — plan changed during build`. Detection only: `.plans/` is gitignored, so edits leave no diff, and nothing is reverted.
- Sync the file-handoff protocol: a tool or permission denial that blocked part of a task returns `done_with_concerns`, never `done`.

## 0.40.6

- `/develop:review` and `/develop:fix` follow foundry's non-blocking compaction boundary gate through the shared compaction contract: an `advise` decision continues with the next phase and shows a short one-line `/compact` hint at the next natural pause; only `FOUNDRY_COMPACT_GATE=stop` holds the run until `continue`.

## 0.40.5

- Sync `write-guard.js`: a protected file git already tracks no longer asks before a write, since the write is diffable and revertible; untracked, new and gitignored protected files still ask, and `.claude/settings*.json` and `.pre-commit-config.yaml` always ask.
- Sync the adversarial-loop rule: reviewer briefs that run adversarial shell cases require case files written with a file-editing tool and fake `gh`/`git` binaries.

## 0.40.4

- Keep review coverage and confidence gaps as factual limitations; require a specific user decision for additional review and omit declined requests from findings and final actions.
- Sync the cross-validation protocol, and state in the review consolidator prompt that aggregate `readiness: complete` requires every assessment complete and no open concerns, and that a missing required role or a `null` candidate makes the aggregate `incomplete`, overriding `blocked`, also under a null aggregate outcome.

## 0.40.3

- Sync the quality-gates rule: after each validated adversarial-loop round, print the progress table exactly, with all rows kept and each cell as `old + new`.

## 0.40.2

- Sync the cross-validation protocol: assessment evidence and limits are string arrays, open concerns cover only UNVERIFIED or pending candidates, and an open candidate forbids complete readiness.

## 0.40.1

- Render reviewer attribution with readable role names and square-bracketed ratings, omit execution-origin labels, and display null judgments as “no source judgment.”

## 0.40.0

- Gate existing compaction boundaries with persisted metadata and optional Foundry `compact_gate.py`; append the fix checkpoint before writing its contract. Follow-up questions carry the report path and key outcome, while the final reply preserves the full report header.
- Route skill questions through saved-content and plain-summary delivery without previews, using persisted session/digest ownership metadata and atomic contract writes.

## 0.39.2

- Validate each review candidate against allowed source evidence before publishing findings; retain revision, checks, counterevidence, disposition and durable open concerns through remediation.
- Define source ratings as 1 approve, 2 minor edits, 3 major revision, 4 insufficient-but-promising, and 5 reject only for an evidenced conflict with binding constraints. A completed undecidable assessment (`null`) differs from a missing reviewer; neither implies rejection or approval, and ratings never replace the aggregate verdict.
- Breaking report-intake change: add versioned `review-validation.json` with `source-5-v1` ratings and `REVIEW_V1_PASS` / `REVIEW_V1_BLOCK` / `REVIEW_V1_REJECT_<GROUND>` gate headers. These prefixes deliberately block legacy resolver intake: upgrade the resolver and rerun its report gate rather than stripping the prefix.

## 0.39.1

- Remove shipped repository-API, GraphQL, branch and tag allow grants; new installs use normal permission handling, including routine-read prompts when no other grant matches. Remove shipped GitHub-write and local branch/tag-deletion denies while retaining force-push and mirror-push denies.
- Develop setup preserves existing repository-API, GraphQL, branch and tag allow entries because Foundry may also have installed them; standalone users remove them manually. Foundry setup owns stale GitHub-deny cleanup.
- Add an optional `gh-read` decision lane after blueprint and shape; a missing optional module falls through.

## 0.39.0

- Require the report header in the first follow-up question text or in every eligible option preview before `develop:review` proceeds; record delivery only after the question is shown. When the full header exceeds preview limits, use a compact summary and saved-report path.
- Warn about stale session state and offer a choice; debug/fix hypothesis teammates are read-only, while team edits share the orchestrator worktree and need no separate teammate merges.

## 0.38.7

- Add `--force-if-includes` and mirror pushes to Develop’s explicit force-push deny list. The agent router resolves the newest Haiku model dynamically from environment/cache/API and falls back with diagnostics.

## 0.38.6

- Require evidence-backed findings and completion claims in review and fix; retain representative real-case acceptance, use mocks only as supplementary checks, and do not weaken assertions or requested behavior.

## 0.38.5

- Retry transcript reads for up to two seconds before denying review-header delivery; report the visible-reply character count and distinguish thinking/reasoning and tool output from user-visible delivery.

## 0.38.4

- Keep the report-print exception available and enforce Stop when `develop:review` follow-up is skipped; return a short terminal summary while retaining full findings in the report.
- Account for confidence with two-decimal gap deductions summing to `1 - score`; use `(-0.00)` for nonreducing limits and one residual entry for unexplained shortfall.

## 0.38.3

- Add a native Windows Python launcher and runtime probes, preserving workload arguments and exit status; use portable launcher commands in the existing adversarial-log instructions.

## 0.38.2

- Add a portable `python` fallback for hosts that only provide `python3`; Develop hooks also probe for Node and report once when hooks cannot run.

## 0.38.1

- Require strict score descent for unchanged scope; plateau, increased score, stale evidence or a stricter caller budget stops the loop. Completion questions are conditional on missing decisions or authority, and authorized next actions continue without an unconditional skill menu.
- Remove the arbitrary three-review cap: strict integer score descent bounds reviews while stricter caller budgets still apply.

## 0.38.0

- Record append-only adversarial round logs with a split header and staged-append protocol while retaining historical-reader compatibility.
- Make debug’s hypothesis ledger append-only and select review depth from the Codemap code path rather than diff size. Run the challenger at every diff size; `--challenge` becomes a compatibility no-op and `--no-challenge` disables it.
- List Codex-eligible mechanical findings under Recommended Next Steps; review names candidates for the fix workflow or user and never invokes `bridge:implement` or edits code itself.

## 0.37.1

- Correct Develop’s GitHub repository API allow rule and migrate only the exact obsolete entry in setup.

## 0.37.0

- Add `dev_test_targets.py` to select focused tests from fresh, complete Codemap impact data or fall back to name/import heuristics; record selections in `test-targets.jsonl`. Feature, fix and refactor loops run targeted tests, then require a fresh full-suite gate after any later fix. Agent-watch notifications/deadlines have an explicit timeout fallback.

## 0.36.1

- Correct a stale step reference in `develop:debug` and split long Markdown prose without changing its instructions.

## 0.36.0

- Create worktrees before asking about existing trees, and report a leaked worktree only when creation retries fail with a matching path or branch.
- Warn when loaded plugin paths are stale and clean up report-template attribution.

## 0.35.0

- Deliver Develop’s quality-gate deltas only when Foundry ownership is verified; tests compare the full rule with its reduced delta.

## 0.34.0

- Batch non-overlapping edits and tests into fewer test runs in `develop:feature` and `develop:refactor`; failures bisect the batch, and `--no-batch` opts out. Probe parallel runners, merge doctest collection, retry focused failing nodes and classify isolation failures.
- Breaking report change: use the Final Report Doctests values `pass` / `fail` / `not-merged`, replacing the earlier pass/fail contract; failed merged collection uses a separate doctest pass and reports `not-merged`.
- Repair zsh multi-token runner expansion and pipeline-exit capture across workflows.

## 0.33.4

- Split long helper functions in Develop’s `bin/` modules and add `main()` docstrings; refresh its blueprint manifest.

## 0.33.3

- Move six Codemap test files into `tests/codemap/` and update affected repository-root anchors.
- Expand adversarial review coverage to first-pass inventories, invariant/original/consumer/sibling evidence, finding-origin accounting and the loop-actions schema 2.

## 0.33.2

- Reject CLI-option-shaped Codemap module names before subprocess calls, contain Git-artifact cleanup within the selected repository, atomically replace propagated rules, and handle oversized audit lines without crashing the verifier.
- Make `develop:review` fail closed when its run directory is missing, correct challenger-severity and checklist handling, and surface breaking changes explicitly for background callers without a foreground question path.

## 0.33.1

- Record challenge progress cumulatively and require a later independent review after fixes; keep completion bound to validated rounds and the caller’s source, role and evidence gates.

## 0.33.0

- Add scoped reviewer ratings and a finding-author column to `develop:review`, preserving the aggregate verdict without averaging ratings. Ratings distinguish approval, minor changes, changes required, insufficient evidence and block/reject; missing reviewers remain unrated, and the legend, summary and findings overview travel with the header.

## 0.32.3

- Show cumulative adversarial-loop progress after each completed review while keeping unresolved and unavailable reviews distinct from clean results.

## 0.32.2

- Preserve Codemap package-initializer queries across mixed diffs and Windows/source-layout paths; keep incomplete preflight data partial and legitimate source reads available.

## 0.32.1

- Block the review follow-up until the report exists and its complete header is visibly delivered.

## 0.32.0

- Add an isolated blind-solve reviewer to `develop:review` for feature or mixed changes only when an active plan names an affected module; compare its blueprint with the implementation and report explainable divergences as questions.
- Refresh the review compaction contract and hint before follow-up.

## 0.31.0

- Read Codemap’s shared context live from its active install, remove `--semble`, and retain the standalone path fallback. Add bounded shutdown timeouts to Develop session hooks.

## 0.30.6

- Compress prose and nonfunctional code comments across Develop skills, templates, shared files and rules while preserving frontmatter, headings, code, load paths and functional comments.

## 0.30.5

- Preload deferred task-tool schemas before task-hygiene calls. Clarify OSS PR-scoped versus Develop timestamp-scoped report paths and repair README rendering and anchors.

## 0.30.4

- Include unchanged consumers and the next ordinary user action in workflow reviews; require outcome probes for identity, destination, lifetime, retry and resume assumptions.

## 0.30.3

- Adopt the bounded adversarial convergence procedure for review-and-fix cycles, with validated independent evidence, fresh source snapshots and explicit stop or recovery decisions.

## 0.30.2

- Restore four quality-gate obligations in the Develop rule copy: route files by copy intent, avoid option questions whose actions cannot be executed, merge instruction blocks without losing rules, and record deferred work in the delivered artifact.

## 0.30.1

- Parse skill flags in shared helpers, avoiding zsh’s empty `BASH_REMATCH`; derive feature and plan Codemap targets in `derive_codemap_target.py`.
- Use TSV only for supported query shapes and feature-probe it, falling back to JSON with older Codemap CLI installs.

## 0.30.0

- Add a weighted loop capped at three independent review/fix iterations; stop on plateau or non-convergence, and leave structural findings unapplied until explicit approval for separately scoped work.
- Dispatch reviewers in the background, end the turn and resume on completion notifications; bound inactivity handling to about 15 minutes and retain partial/error outputs instead of silently omitting failed reviewers.

## 0.29.2

- Extract argument-target parsing, review-target resolution and skill-compaction contract writing into tested Python helpers; persist the tried-candidate ledger so resumed debugging uses state that was actually written.

## 0.29.1

- Ship Develop-local Codemap context and gate contracts so standalone installs do not depend on another plugin’s private shared directory; prefer the active install and fall back to file reads when Codemap is unavailable.

## 0.29.0

- Register the shared Bash allow-audit hook in Develop, preserving the existing command decisions while adding an audit trail.
- Record ranked allow-hook decisions and completion/session events, storing command digests rather than raw commands; audit can be disabled, and verification/pruning does not make the log tamper-evident.

## 0.28.1

- Clarify bridge detection, diagnosis containment and JSON recovery contracts, and strengthen helper tests and test-marker metadata.

## 0.28.0

- Add a shipped deny list and permission-merge setup step for Develop, with backup, validation and rollback if settings are malformed.

## 0.27.1

- Replace routine self-review and score-triggered adversarial passes with named-gap triggers; retain findings before presentation filtering and raise feature/refactor effort to xhigh.

## 0.27.0

- Merge paired review dimensions into one spawn, enforce fan-out limits and bound feature iteration by cycle and wall-time caps.

## 0.26.0

- Replace the legacy Codex integration with explicit bridge calls for implementation, advice and review; preserve unavailable behavior and local review fallbacks.

## 0.25.1

- Close sentinel-read hook allow bypasses found by adversarial review while retaining the reviewed read-only command cases.

## 0.25.0

- Add a protected-file write guard to Develop and document the propagated blueprint allow hook in its README.

## 0.24.0

- Add shared stale-lock and worktree healing with repository-root containment; reduce review fan-out to keep total agents bounded.

## 0.23.0

- Add the blueprint-manifest auto-allow hook to Develop while preserving the existing read-only command policy.

## 0.22.2

- Replace runtime test skips with collection-time capability markers and synchronize Develop documentation with current commands and skill roster.

## 0.22.1

- Anchor Codemap index and logs at the Git root, and resolve module names from the index so Develop queries work from subdirectories and do not guess from paths.

## 0.22.0

- Choose a task-fit Codemap route before fix retrieval; use no query for a fully localized edit unless a structural fact or explicit tool request remains unresolved.

## 0.21.1

- Trim overlong comments in Develop workflow scripts without changing commands or functional rationale.

## 0.21.0

- Rank only review dimensions selected by scope and evidence, cap default fan-out at four, and let `--full` run every eligible dimension without widening the scope.

## 0.20.0

- Extract Codemap gating, issue fetching and worktree setup into tested Python helpers, and split team-mode prompts into loadable files. Add a language preflight before pytest runs in projects without Python markers.
- Check review-header delivery in the transcript as a nudge before follow-up; the hard delivery gate arrives later.

## 0.19.1

- Add shared-path fallback guards at every resolver call so zsh and broken installs cannot silently use an empty path.

## 0.19.0

- Add `/develop:setup` to install and clean up only Develop’s namespaced rules without editing settings or another plugin’s rules.

## 0.18.5

- Use closed option sets for Develop’s scan-source and spec-type values, deriving command choices from the accepted values.

## 0.18.4

- Resolve shared files from Develop’s own installed plugin rather than Foundry’s cache or user-level skill links, allowing standalone use.
- Ship Develop-local quality-stack, Codex and validation/handoff instructions so standalone workflows retain those protocols.

## 0.18.3

- Repair shared-shell state and sentinel handling so Develop workflows retain paths and flags between calls.
- Add explicit `--issue N` mode and improve report/header and sentinel recovery paths.

## 0.18.2

- Add a fail-open, expiring hook that blocks an active review follow-up until its report file exists; printing and delivering the header remains a skill obligation.

## 0.18.1

- Keep report header table cells on one physical line so long values cannot break the rendered table.

## 0.18.0

- Add `--worktree` to feature, fix, debug, refactor and review; edits run in a HEAD-based worktree and report its branch/path for manual integration. Plan remains read-only and does not accept the flag.
- Worktrees start from committed `HEAD`; debug diagnoses and review reports return to the main tree for manual integration.

## 0.17.4

- Require Develop review to print its report header before asking the follow-up question.

## 0.17.3

- Label injected structural context as `codemap-py` throughout Develop and keep debug-to-fix result matching in sync.

## 0.17.2

- Migrate Develop’s structural context and test-impact calls to the `codemap-py` CLI while preserving existing preflight behavior.

## 0.17.1

- Point Develop’s Codemap wrappers and fallback paths at the dual-runtime `codemap-py` package.

## 0.17.0

- Propagate the sentinel-read allow hook to Develop so pre-canned read-only state reads no longer trigger avoidable permission prompts.

## 0.16.2

- Use Codemap `diff-impact` for function-level review context; remove bare-module calls to `fn-rdeps`/`fn-blast` while retaining module-level `rdeps` queries.

## 0.16.1

- Render YAML report metadata as a two-column Markdown table while retaining machine-readable YAML in the saved report.

## 0.16.0

- Add the session ID to temporary sentinels so parallel sessions and projects do not share state.

## 0.15.8

- Move the source directory to `plugins/cc_develop/` while preserving the installed `develop:` namespace.

## 0.15.7

- Review public API changes against neighboring symbols and established naming, placement, parameter and return-shape patterns.

## 0.15.6

- Make Codemap helper loads version-proof and correct build/rebuild wording across Develop’s shared gates.

## 0.15.5

- Use `cat` for shared-document loads so documented shell permissions survive plugin updates, remove a superseded team-prompt copy and ship a Develop permission-allow manifest.

## 0.15.4

- Compress Develop’s Markdown prose while preserving commands, code, frontmatter and functional annotations.

## 0.15.3

- Align Develop’s issue-ID routing and remove a review flag step that the parser did not support.
- Recover the final top-level JSON object from helper output and repair router timeout handling.

## 0.15.2

- Add explicit `--help` and argument validation to Develop’s bin scripts.

## 0.15.1

- Move shared Codemap context and currency gates into the provider contract while keeping Develop’s flags and graceful fallback in its own wrapper.
- Change Develop’s plugin license to Apache-2.0.

## 0.15.0

- Use indexed symbol spans for targeted edits in larger files; fall back to full reads when spans drift or replacement text is ambiguous.

## 0.14.0

- Reuse test-impact results from debug in fix while the index remains current; size plans from structural dependency and coupling data.

## 0.13.0

- Batch Codemap preflight queries for Develop review in one process and extract the query builder into a tested Python helper.

## 0.12.2

- Prioritize Codemap results in reviewer prompts; recognize direction-scoped `query_complete` with the legacy `exhaustive` alias and invoke the Semble companion when available for incomplete context or a missing index.

## 0.12.1

- Document that Develop never auto-invokes OSS skills and the shared quality loop escalates to `develop:review` only when a concern signal warrants it.

## 0.12.0

- Add per-skill compaction contracts to Develop review, feature, fix, refactor and debug so the workflow can resume from persisted state after context compaction.
- Add `--keep` context preservation and harden argument parsing for tokenized and quoted values.

## 0.11.7

- Skip the default challenger on small single-file changes while allowing `--challenge` to force it; `--no-challenge` still disables it.

## 0.11.6

- Fix cross-platform runner handling, test ordering and flag spelling across feature, fix, refactor and debug.
- Gate characterization tests on unmodified source before refactoring, then rerun the same tests afterward. Failed or no-test prechecks require recovery, explicit acceptance of proceeding without a safety net, or abort.

## 0.11.5

- Persist shell state between calls so Develop review retains its selected target and shared path, while feature keeps its Codemap module target.

## 0.11.4

- Synchronize Develop’s agent router with the canonical implementation, enforce source-drift checks and add SessionStart agent indexing with optional OpenAI-embedding/Anthropic matching and fallback.

## 0.11.3

- Correct Foundry detection in Develop’s agent fallback so installed specialist agents are no longer silently replaced by general-purpose agents.

## 0.11.2

- Verify the user’s premise before debugging, and use a synthesis-only general agent to consolidate local review findings.

## 0.11.1

- Remove self-evident shell comments from Develop skill bodies while preserving comments that explain constraints.

## 0.11.0

- Restore missing/stale Codemap index gates in debug, feature, fix, plan and refactor, and document automatic build/rebuild prompts.

## 0.10.9

- Require resolved literal paths in reviewer prompts and preserve agent descriptions when the router rewrites dispatches.

## 0.10.8

- Clean feature/refactor team sentinels on exit, move trigger/skip guidance into skill descriptions and surface strict Codemap failures.

## 0.10.7

- Add two-tier Codemap currency checks and wire missing/stale-index gates into all six Develop workflows.

## 0.10.6

- Query direct reverse dependencies before blast-radius results in Develop review, and warn when Codemap context is unavailable.

## 0.10.5

- Query direct function callers before transitive impact and move feature team/report plus review consolidator prompts into loadable templates.

## 0.10.4

- Consume Codemap `test-impact` to select affected tests in feature, fix and refactor, falling back to the full suite; record query counts, completeness, coverage caveats and gaps.

## 0.10.3

- Add Codemap preflight checks to Develop review so index materialization and per-agent queries are explicit before findings are written.
- Refresh changed Codemap files incrementally and query richer function, test and documentation context before review findings are written.

## 0.10.2

- Remove comments that merely restate Develop workflow commands while preserving rationale and behavior.
- Add a `Title` field to report metadata.

## 0.10.1

- Classify changes by intent, including chore work, and align report metadata with explicit cosmetic-through-critical severity sections.

## 0.10.0

- Add premise verification before Develop workflows form a diagnosis or design; review findings must cite file and line evidence and meet the source threshold.

## 0.9.4

- Honor `CODEMAP_INDEX_DIR` in Develop’s Codemap consumers, allowing indexes outside the default project cache.

## 0.9.3

- Migrate debug, feature, fix and refactor to the shared skill-driven argument parser; persist parsed flags in per-skill and compatible legacy temp files instead of caller-side eval blocks.

## 0.9.2

- Add trigger and skip metadata to Develop skills, and clarify Python-project eligibility and CI-only failure handoffs.

## 0.9.1

- Persist run-directory and parsed-argument values across fresh shell calls, and derive Codemap targets from the active traceback or symptom.

## 0.9.0

- Add the shared argument parser, automatic Codemap context with strict/off overrides, and safer issue/test helper argument handling.

## 0.8.1

- Replace the pinned Haiku model ID with the `haiku` alias in Develop’s agent router.

## 0.8.0

- Add `--repo` to feature, fix and debug issue fetching and parse it in refactor for consistency; add two-wave feature teams and per-agent debug health sentinels. Persist plan flags across calls, bound plan/debug dispatch depth and distinguish public breaking changes from internal signature-change risks.

## 0.7.5

- Pass the resolved diagnosis path directly from debug’s follow-up question into `/develop:fix`.

## 0.7.4

- Update Develop’s runtime and documentation references to the consolidated Foundry setup entrypoint.

## 0.7.3

- Update Develop’s Codemap references to the unambiguous `scan-codebase` and `query-code` skill names.

## 0.7.2

- Remove a trivial Codemap flag helper in favor of direct skill prose and extend the plugin’s hook and workflow regression coverage.
- Use inline doctests for simple feature contracts and example scripts for complex ones before converting them to formal pytest tests.

## 0.7.1

- Persist and reload the Codex output path across shell calls, and distinguish failed reviewer outputs from roles that were legitimately skipped.

## 0.7.0

- Replace Develop shell utilities with cross-platform Python CLIs and update skill callers; keep only permitted shell wrappers.

## 0.6.2

- Narrow debug tool permissions, remove debug/feature effort overrides and update test/configuration support.

## 0.6.1

- Restrict Codemap scans to the project tree and cap them at 2,000 Python files; sanitize project and sentinel names, validate issue IDs and pytest commands, parse diagnosis paths without shell evaluation, and reject missing diagnosis files.
- Add a non-Python preflight to feature; feature's lead or software engineer owns primary red-green tests, while QA adds edge-case and regression tests after implementation. In fix, retain the challenger on diagnosis handoff, stop for a decision without a reproduction path, and preserve pre-existing regression tests.
- Gate mixed feature/refactor goals and broad refactors spanning 3+ modules, 5+ files or any public-API rename; require a scope decision before proceeding.

## 0.6.0

- Extract structural scan logic into `codemap_scan.py`; add pytest-gate, short-run and worktree shell helpers and connect their skill callers.

## 0.5.3

- Add plugin-owned helpers for issue fetching, shared-path resolution, run directories and Codemap flags; reduce repeated shell parsing in Develop skills.

## 0.5.2

- When review receives a PR-like argument, detect whether OSS is installed before routing and ask explicitly when its PR workflow is unavailable. Treat pytest exit 5 as a no-tests/collection condition, cap refactor cycles, and author new QA reproduction tests only when no relevant existing test is found; strengthen weak existing tests instead.

## 0.5.1

- Fix Develop’s shared-path guard so SemVer rules resolve correctly before a fix workflow uses them.

## 0.5.0

- Standardize report YAML headers, add the report path, list challenger in review fallback roles and adjust shared-path fallback handling.

## 0.4.0

- Accept full GitHub Actions URLs in `--ci-run`, extract CI log handling and inline plan generation into shared helpers, and repair feature team-mode dispatch and fix-plan initialization.

## 0.3.15

- Remove the explicit `opusplan` model override from Develop review so runtime model selection applies.

## 0.3.14

- Limit deprecation checks in Develop review to public API surfaces.

## 0.3.13

- Separate temporary reviewer handoffs from final reports under `.reports/review/`; the consolidated Develop review names the source handoff files.

## 0.3.12

- Add the breaking-change gate to fix and plan work, and preserve the confirmed diagnosis in debug/fix handoffs.

## 0.3.11

- Add `--team` modes with explicit reviewer/teammate dispatch and keep the shared quality-stack and Codemap contracts aligned with their callers.

## 0.3.10

- Keep issue-ID normalization and fetching in one shell call, repair Foundry fallback paths, recognize team flags, and distinguish workflow routing metadata.

## 0.3.9

- Extract reusable preflight handling; detect unsupported flags and ask whether to abort or continue ignoring them.

## 0.3.8

- Make Codemap and Semble opt-in with explicit flags; strict preflight stops with a clear status when a requested tool is unavailable.

## 0.3.7

- Extract task-hygiene instructions for reuse across Develop workflows and bound agent-router matching with a five-second timeout.
- Parse `--plan` and `--diagnosis` handoffs in fix/refactor and fail fast when the referenced file is missing. Offer Codemap’s optional Semble companion query only when structural coverage is incomplete, then combine reverse-dependency results.

## 0.3.6

- Add a three-tier agent-router fallback: exact installed agent, optional LLM-selected local role, then `general-purpose`. Require a selected follow-up skill to run in the same turn.

## 0.3.5

- Require analysis agents to self-check claims and coverage, and route low-confidence work for adversarial review when available. Debug issue mode fetches the issue first and runs a referenced test before falling back to the full suite when the path is absent or invalid.

## 0.3.4

- Centralize Codemap context and task hygiene in shared Develop files; add challenger coverage and reliable `--plan`/`--diagnosis` parsing across applicable modes.
- Protect existing plans with a counter suffix instead of silently overwriting them.

## 0.3.3

- Reconcile existing tasks before creating new ones and correct the plugin-prefixed workflow dispatch and scope boundaries.

## 0.3.2

- Run a read-only adversarial challenger across Develop’s six workflows by default; `--no-challenge` skips the extra review.

## 0.3.1

- Accept issue identifiers with or without a leading `#` in feature, fix and debug commands.

## 0.3.0

- Keep local review in `develop:review` and reserve `oss:review` for pull requests; integer input to local review now redirects to the PR workflow.

## 0.2.1

- Extract agent fallback and test-runner detection into shared sidecars, add a bounded refactor cycle cap, and reuse Codemap blast-radius context for multi-file work. Missing Codemap indexes now return a failing status so callers can gate correctly.

## 0.2.0

- Separate `PYTEST_CMD` from the project test runner and detect the repository’s runner before testing. Add `--plan` and `--diagnosis` handoffs, early exit-code gates, and persisted checkpoints to feature, fix and debug flows. Review loops now stop when findings stabilize.
- Persist structured plans with feasibility review/resolution and Codex validation before implementation.

## 0.1.0

- Establish the standalone Develop plugin with feature, fix, refactor, plan and debug workflows under its own namespace.
- Extract shared branch-safety, quality-gate, review-loop and Codex-delegation instructions for Develop workflows to reuse.
- Remove the former Foundry Develop skill copy after moving its workflows into the plugin.
- Add `develop:review` for local Python files, directories and diffs, with six scoped specialist roles consolidated into a report.
- Add external-API detection/fetch/citation checks, surfaced assumptions and five-axis feature/fix quality scans.
- Inject optional Codemap hotspots and dependency/reverse-dependency context into reviews, with risk tiers.
- Remove Foundry as a required dependency; absent named agents fall back to `general-purpose` with role descriptions.
