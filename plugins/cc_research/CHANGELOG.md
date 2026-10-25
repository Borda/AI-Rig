# Changelog

## 0.32.0

- Review loops add a pre-baseline attestation: before the first review of a diff the parent or its resolver wrote, the author lists each changed behavior's invariant, sibling routes, consumer and failing-before/passing-after check, and fixes the gaps within scope. Fewer avoidable defects reach a paid independent review. The reviewer never sees the attestation, it never counts toward scoring or clean acceptance, and it is skipped for review-only requests, unauthored code and continuations of a prior ledger.
- `adversarial_loop.py --append-metrics` records each round's cost (route, brief bytes, duration, tokens, attested gaps; unknown values stay `null`) in an advisory `loop-metrics.jsonl` beside the ledger, so later investigations can price review rounds. It never affects scores, recurrence or clean acceptance.
- The loop rule names where a first-reported finding's origin class is stored (an `origin:` evidence entry), so missed versus fix-introduced defects can be counted from the ledger.
- Review loops report why a stalled round stalled: `adversarial_loop.py --plateau-kind` prints `stuck` (the fix did not land), `whack-a-mole` (the fix exposed new siblings) or `mixed` on stderr, and the loop rule recommends the matching next step. It is informational only and never changes the stop decision, exit status or JSON summary.
- The cost log's `route` follows a shared vocabulary (`parent`, `agent:…`, `bridge:…`, `codex:…`, `other:…`) so routes can be joined to outcomes, and `--append` warns, never refuses, when a finding first reported after round 1 has no `origin:` evidence entry.
- Review loops can pilot a leaner reviewer brief: `adversarial_loop.py --brief-arm` assigns `lean` or `full` per run directory, only for loops that are not filter or gate reviews and have no open security or critical finding. The cost log records the wording sent (`brief`), so about 10 runs can be compared by first-reported weight, rounds and brief size. A high or higher severity finding first found after a lean baseline pauses the lean arm.
- The review-loop cost log and pilot flags are hardened: a rejected cost record now only warns (`warning: metrics-rejected:…`) and no longer hides the committed round's summary or progress table; records bind only to a valid current ledger, take fractional `duration_s`, keep one stripped route per line even after a partial write, and a `brief-arm-pause` file carries the lean-arm pause. `--brief-arm` refuses to combine with another action, `--plateau-kind` speaks only at a plateau or non-converging stop, the `origin:` warning also fires for an unknown class, and the attestation goes to a randomly named temp file outside the workspace so a reviewer cannot derive its path, and `attested_gaps` is `null` whenever no attestation was written.

## 0.31.0

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

## 0.30.5

- The adversarial-loop rule and the challenger dispatch rule list a reviewer reply truncated before its required closing block (envelope or `## Confidence`) as an agent failure, so a cut-off reply is no longer scored as a complete round with fewer findings. Where the host can continue that reviewer, the parent asks it for only the remainder, in replies short enough to pass the transport, until the closing block arrives; a reply that adds nothing new records the failure.

## 0.30.4

- The adversarial-loop progress table shows security findings in their own `Security` column ahead of `Critical`, instead of folding them into `Critical`, so a reader sees how many open findings are security bypasses. Score weights and the `Weighted score` column are unchanged.
- The adversarial-loop rule treats any reviewer round that returns no deliverable, a refused findings write, an unparsable envelope, a safety-classifier stop or a `blocked` envelope as an agent failure, records its cause in `loop-report.md` (never as a round, where it would read as a clean review), re-dispatches that reviewer once with a brief that removes the cause, switches to another permitted reviewer route if the same one fails again or cannot be re-dispatched (no attempt left, no approval, or a cause no brief can remove), and stops as `independence-unavailable` only when no route remains, instead of letting the round drop out as `not-run`.

## 0.30.3

- Adversarial review loops treat a `foundry:challenger` round that returns no deliverable or a `blocked` envelope as an agent failure, never a silent `not-run`: the cause is recorded and the round re-dispatched once. Security-filter reviews keep Codex enabled, so the challenger runs the bypass search through its Codex cross-check and needs no separate Codex row.
- The file-handoff protocol lists the report names Claude Code refuses for a subagent Write (`findings.md`, `report.md`, `summary.md`, `analysis.md` and suffixed `findings-*.md`), so an agent names its file after itself and the report is no longer lost.

## 0.30.2

- The runtime `blueprint-allow.js` hook refuses, even on a manifest hit, every command that can point a command name at another program: `hash`, `builtin`, `alias`, `enable`, `functions`, `eval` and their kin; any mention of the zsh `commands`/`functions`/`aliases` or bash `BASH_CMDS`/`BASH_ALIASES` tables, attached option prefixes included (`set -Acommands`); `set -A`, `read -a`, `printf -v`, name references and indirect expansions. Spellings that slipped past the earlier per-spelling checks, such as `set -Acommands ruff /bin/echo` and `builtin -- hash -p …`, are now refused.
- The same hook reads each command as the shell's own words before judging it: quotes, backslash escapes, `$'…'` strings and line continuations are resolved and joined, and redirections are set aside with their targets. `\rm`, `r""m`, `h$'a'sh`, `\-v` and `co""mmands` therefore count as `rm`, `hash`, `-v` and `commands`, and a redirection such as `printf 2>/dev/null -v …` or `2>"a b" set -A …` no longer hides the option or the command. A command word written as a pattern that can expand to a refused name (`/bin/r?`, `{ha,}sh`) is refused. The hook reads past the zsh precommand modifiers `noglob`, `nocorrect` and `-` and past a wrapper's own arguments (`timeout 5 set -A …`), and it judges the command text carried by a shell's `-c` (after options such as `-o shwordsplit`), a `trap` action, `watch` and `env -S`, so `noglob rm -rf x` and `zsh -o shwordsplit -c 'set -A …'` are refused. An option written after another option's argument (`set -o noglob -A …`, `read -t 5 -a …`, `print -f '%s %s' -v …`) still counts. Text the reader cannot settle is refused rather than guessed: a command that ends inside an unclosed quote; a `$'…'` escape bash and zsh decode differently, wherever it stands, assignment and redirection target included (a NUL byte, `\u…`, `\c…`, an unknown letter such as `\m`, an octal value above `\377`); an expansion inside a command word (`h${x}sh`); and a pattern among a wrapper's words or a POSIX bracket class in a command pattern (`command h[a]sh …`, `/bin/r[[:lower:]]`). A shell's `-c` script written after `--` is judged like any other. Short options are read as clusters, so `zsh -co shwordsplit '…'`, `zsh -xo name -c '…'` and `env -iS '…'` still reach the carried script, and every argument after a `-c` cluster is judged as script text. A `/` inside an expansion (`h${x:-/}sh`) no longer counts as a path separator. `unset -f`, `-F` or `-m`, which remove a function that shadows a command, is refused like `unalias`. A `-c` string counts as a bare variable (`bash -c "$METRIC_CMD"`) only when it is exactly that variable, so invisible Unicode whitespace around it no longer makes the hook and the generator disagree. An expansion where `printf`, `print`, `set`, `read`, `unset` or a declaring builtin still reads options (`printf "$o" …`) is refused, since its value may be `-v` or `-A`. A quoted or escaped prefix (`'h'${x}sh`) no longer hides an expansion inside a command word, and a bracket class holding a quote or backslash (`h[\!a]sh`) is matched as any one character. The command after a zsh short or brace form (`for x (a) cmd`, `select x (a) cmd`, `if true { cmd }`, `while … { cmd }`, `repeat 1 cmd`) or after bash `coproc` is judged like any other command. The `&` of `2>&1` or `&>` no longer splits a command in two, an escaped quote in `$'\''` no longer ends the string early (also when a line continuation splits `$` from `'`), a parenthesis inside a quoted string no longer ends a `$(…)`, and a `{fd}>` redirection is set aside like `2>`.
- The blueprint hook refuses a command whose raw text has a backslash-escaped blank at a line end or before `#`. The shell ends the command there, or keeps the rest of the line as an argument, while normalization would read a continuation or a comment, so a shipped digest could approve a different command. No shipped block contains the shape.
- The sentinel-read shape lane judges every word a `read` receives: a sensitive variable such as `PATH` named anywhere, attached to an option (`-aPATH`) or after an option's argument, or a computed name (`read "$V"`) refuses the command. Only the first name was checked before, so `read -r IGNORE PATH < file` could rebind command lookup without a prompt.
- The plugin's blueprint manifest no longer auto-allows a line that is not a complete command on its own, a block that writes heredoc data, a block that sources a state file, or one that runs a variable's value as a command; those run after a prompt.
- fortify's `bash -c "$METRIC_CMD"` and `"$GUARD_CMD"` blocks and sweep's heredoc-data block now prompt each run instead of being auto-allowed.

## 0.30.1

- Follow the active Codemap provider’s automatic missing/stale-index preparation contract, preserving explicit confirm-first and frozen preferences across later tool calls and nested query helpers.

## 0.30.0

- Helper scripts that write or read a session sentinel (`parse-skill-flags.py`, `codemap_resolve.py`, `find_judge_verdict.py`, `resolve_shared.py` and the other `bin/` sentinel helpers) fall back to `/tmp` on POSIX when `TMPDIR` is unset, the directory a skill's `${TMPDIR:-/tmp}` names, instead of `tempfile.gettempdir()`, which picks `TEMP` or `TMP` first; with either set, script and skill read different files. Native Windows keeps `gettempdir()`. `codex_state.py --state` accepts both directories.
- The runtime `blueprint-allow.js` hook treats `uv run` and `poetry run` as carrying their wrapped command and flags `hash name=/path`, `hash -p /path name` (also the attached `-p/path` and bundled `-rp` spellings), `builtin hash …` and any write to the zsh `commands` / bash 4 `BASH_CMDS` tables (`commands[x]=`, `commands=(…)`, `+=`, `${…:=…}`, `printf -v`, `read -A`, or a write nested in a lookup subscript such as `${commands[$(…)]}`; a plain `${commands[x]}` or `${commands[$name]}` lookup stays allowed), so a destructive command behind one of those, or a rebound command name, is refused even on a manifest hit.
- `Bash(eval:*)` is no longer in the shipped allow list and `/research:setup` removes it from `settings.json`, including an identical entry added by hand: ad-hoc `eval` commands now prompt, while no shipped research block uses `eval`.

## 0.29.1

- `/research:verify` rates a changed training budget or schedule duration as a HIGH protocol mismatch and judges setting differences by their effect rather than numeric ratio; MATCH rows leave Severity blank.
- `research:data-steward` reports engineering hygiene at Info while silent record loss or duplication stays High, and an out-of-domain reply keeps its required Confidence block; `research:data-steward`, `research:scientist` and `/research:verify` examples now use neutral generic wording.
- `/research:fortify` rejects unrelated positional input before source-run lookup, and ablation variant ids are ASCII slugs so worktree selection and revert lookup agree.
- `/research:retro` renames the `timeout-as-revert` anomaly to `unreliable-timeout`, reports an automatically adjusted threshold without suggesting a re-run, and rates every dead window MEDIUM; `/research:judge` shows unverified checks in the terminal summary as well as the report.
- `/research:run` accepts a dry-run or walkthrough of a campaign and requires a text goal to name a measurable target; `/research:sweep` stops terminally on a non-optimization goal; `/research:plan` routes hard goal constraints to `guard_cmd`.
- The shared file-handoff protocol adds an `unsupported` decline status that orchestrators surface as a routing miss instead of a timeout.

## 0.29.0

- Narrow broad `find`, `node`, `claude`, and `sed` allows while retaining scoped read-only rules and broad `eval`/`python` for existing recipes; ad-hoc commands outside shipped blocks now prompt unless a narrower rule covers them. Setup removes the old broad entries from `settings.json`, including an identical entry added by hand.
- Shared keep-state and codemap helpers reject unsafe sentinel slugs or session IDs before changing state, while codemap keeps its documented `shared` session fallback.
- `/research:setup` validates the merged `settings.json` with `python -m json.tool` instead of `jq`, so setup no longer stops (or restores the backup over a good merge) on machines without `jq`, such as native Windows.
- The Docker sandbox binds the project with `--mount` instead of `-v`, so a project path containing `:` or `,` mounts the right directory; it runs as the invoking uid:gid on POSIX, so files written to `.experiments` are no longer root-owned on Linux, and verify mode creates `.experiments` when missing. Upgrading on Linux: if earlier root-run sandboxes left `.experiments` root-owned, verify mode prints a warning naming the fix, `sudo chown -R "$(id -u):$(id -g)" .experiments`.
- Sentinel-writing scripts (`check_output_within_root.py`, `codemap-flag.py`, `resolve_shared.py --persist`, `find_judge_verdict.py`, `fortify_next_variant.py`, `parse_kaggle_args.py`) exit 2 on a sentinel slug or session id outside `[A-Za-z0-9._-]` or containing `..`, so a crafted value can no longer write outside the temp directory; `resolve_shared.py --persist` also replaces a symlink planted at its sentinel path instead of writing through it.
- `parse-skill-flags.py` no longer drops a `--keep` value whose quotes the shell consumed when the host substituted `$ARGUMENTS` (`--keep "a b"` used to arrive as `--keep a b` and persist an empty keep list); it now also accepts `--keep 'a b'` and `--keep="a b"`, strips a dangling `--keep` from `CLEAN_ARGS`, and warns on stderr when the value was unquoted or empty, so a lost compaction keep list is visible instead of silent.
- Every research skill that spawns agents now arms a per-agent deadline file and runs the new `bin/agent_watch.py` once per wake-up (`_shared/agent-resolution.md` §Agent waits); `/research:verify` loads that doc too. A stalled agent is reported ⏱ `timed_out` at the next wake-up instead of relying on a prose 15-minute deadline.
- `/research:run --team --hypothesis <path>` now feeds the user's hypothesis queue into team Phase B queue assembly beside the axis hypotheses; before, team mode never read that file, so the supplied queue was silently ignored.
- Research sentinel writers (`check_output_within_root.py`, `codemap-flag.py`, `find_judge_verdict.py`, `fortify_next_variant.py`, `parse_kaggle_args.py`) create each `${TMPDIR}` sentinel fresh with mode 0600 through the new `bin/_sentinel_io.py`, so a symlink another local user planted at the predictable name is replaced instead of written through.
- `/research:topic` team consolidator prompt now tells the agent not to call TaskCreate/TaskUpdate, matching the teammate prompts.
- `/research:judge` and `/research:topic` parse flags and `--keep` with `parse-skill-flags.py --keep-state` instead of `extract-keep-flag.py` plus `eval`: one process fewer per skill start, `--inlined` still protects sweep's contract when judge runs inside sweep. `/research:plan`, `/research:retro`, `/research:verify` and `/research:fortify` read parsed flags back from per-session sentinels instead of running `eval` on script output; fortify's parse uses its own `fortify-args` prefix, so the validated `--venue` sentinel is never overwritten. `parse-skill-flags.py` gains `--keep-state`, `--out-file` and `--inlined` (synced from oss), which also clears the `retro` documented-`--keep` flag-drift finding.
- `/research:plan` prints `→ profiling <file> under cProfile (≤600 s) — runs the script` before profiling a file, so it is visible that the user's script is about to execute; without the foundry plugin, plan now runs the architecture and solo perf reviews as `general-purpose` per its loaded `agent-resolution.md` instead of skipping them, so the advisory block is no longer empty on a research-only install.
- `resolve_shared_path.py` without a registry or cache hit uses a cwd-relative `plugins/<dir>/...` tree only inside that plugin's own source repository (its `.claude-plugin/plugin.json` names it); elsewhere it exits 1 with a `!` message, so a repository under analysis can no longer supply the codemap-py contract or the foundry compaction gate path.
- `resolve_shared.py` and `load-agent-reference.py` without an installed research plugin read the cwd-relative `plugins/cc_research` tree only when the working directory is the research source repository (its `plugins/cc_research/.claude-plugin/plugin.json` names `research`). Elsewhere `resolve_shared.py` exits 1 with a `!` message, flags or not, and `load-agent-reference.py` skips that tree for the installed cache and blocks when there is none, so a repository under analysis can no longer supply research protocol docs or agent reference fragments.
- `heal_git_artifacts.py worktrees` keeps unregistered directories unless `--include-orphans` is passed, re-checks one right before deleting it, reports symlinked children as skipped and rejects a `*` managed prefix. The `fortify` post-loop sweep now passes `--managed-prefix 'variant-' --include-orphans`, so it still removes leftover variant dirs and nothing else under its variant root.
- `/research:kaggle` Step 1 lists the competition files and leaderboard and downloads the sample submission in one command, one turn fewer. A download failure now surfaces together with the listing result.
- `fortify` measures every variant `--runs N` times (default 3, range 1–10, so cost scales with N) and classifies each ablation against `max(noise floor, 2σ of the difference)` using the new `bin/fortify_metric_stats.py`. An effect inside that threshold is `BORDERLINE` instead of `MARGINAL`, a removal that improves the metric by up to 5% is `NEUTRAL`, and the full variant must reproduce the source run's best metric within 2% (widened to 2σ) or fortify aborts as `diverged`. `SANITY_DIVERGENCE_PCT` and `NOISE_FLOOR_PCT` override the tolerances.
- `fortify` runs the full variant first, anchors worktree paths and verifies the worktree before reverting, orders reverts topologically, propagates metric and guard exit codes, rejects unsafe variant names, retries a stale worktree once, and is `BLOCKED` on a text-goal run with no program file.
- `judge` runs 16 checks: new C13 flags a `scope_files` overlap with the metric or guard scripts, new C9b validates `metric_repeats` and `min_delta_pct` and adds a low advisory (never a Required Change) when a `perf` or `ml` program measures one sample per iteration, `{field}` tokens in `## Config` are flagged high and their commands skipped instead of substituted, a Colab-compute run skips the dry run with a medium finding, and a methodology review that timed out no longer forces `NEEDS-REVISION` when the review completed.
- `run` Phase 3 enforces iteration scope with the new `bin/check_iteration_scope.py`: an iteration that edits files outside `scope_files` or the metric/guard scripts is restored and recorded as `scope-violation`, and an agent self-commit is folded back so exactly one commit is measured. New statuses `commit-failed`, `scope-violation`, `sandbox-failed` and `codex-no-op`.
- `run --hypothesis <path>` consumes a JSONL queue with no oracle spawn: one entry is popped per iteration, progress is checkpointed, and normal ideation resumes when the queue is exhausted.
- `run --resume` takes only runs with status `running`, restores the saved flags, sandbox, Colab and clarification settings, re-runs `--keep` and codemap parsing, reuses the existing hypothesis file, and skips tried hypotheses by id. A branch that no longer contains `best_commit` is refused; team runs dispatch to the team mode.
- `run` reworks a failed guard by amending the one iteration commit, persists `sandbox_network` and the verify timeout, applies escalation proposals as their own iterations, shows the Colab code-identity check before the baseline, and states the real docker quotas (`--cpus 2 --memory 2g --pids-limit 512`).
- `verify` fences the paper text with the new `bin/fence_untrusted.py` and tells the scientist to treat it as data only; a paper with no checkable claims reports `NO VERIFIABLE CLAIMS` with a null fidelity.
- `retro --compare` applies the same truncation rule to the second run and stops with `Cannot compare runs with different metrics` on a metric mismatch between runs that record `config.metric_cmd` (runs started before `run` persisted its `config` only warn); `--keep` is no longer parsed.
- `topic` runs team mode as background spawns with file handoff, keeps the `--keep`-stripped topic for search and headings, and offers `/research:topic plan` as the follow-up.
- `kaggle` checks file-listing exit codes, warns at 200 listed rows, extracts downloads with `python -m zipfile`, and keeps the `--offline-setup` choice across resume.
- `setup` probes `jq` before touching `settings.json`, leaves it unchanged when the permissions merge fails, restores the backup only when the written file no longer parses, and checks only the bare `python` command.
- `find_run_id.py --match-program` compares real paths; five new test files cover `detect-complexity`, `gate-on-sentinel`, `load-agent-reference`, `require-vars` and `resolve-anti-overwrite-path`.
- New `bin/compaction_boundary.py`: the `topic`, `kaggle`, `judge`, `fortify`, `run` and `sweep` compaction boundaries now make one call that prints the contract's Boundary step and the foundry compaction-gate decision, and continue with a plain notice when foundry is not installed instead of failing.
- `resolve_shared_path.py` rejects an absolute (`/etc`) or empty-segment (`skills//_shared`) subdirectory and a plugin name with a trailing newline, which could resolve to a path outside the plugin install. It also reads its own plugin name from `plugin.json`: in the installed cache the directory name is the version number, so the own-plugin lookup never matched there.
- `extract-keep-flag.py` no longer clears a compaction contract that another session owns or that an inlined `--keep` already wrote (for example a running sweep), so a parallel session's resume state survives. It also validates the slug and session ID and writes its sentinels atomically.
- `extract-keep-flag.py` refuses a keep sentinel another user already owns (exit 1 with an error naming the file) and creates a missing `--out-file` state directory with mode 0700; `codemap_resolve.py` leaves another user's codemap currency sentinel untouched instead of replacing it. Sentinel names are unchanged.
- `verify_blueprint_audit.py verify` reports an audit-log line nested deeper than Python's recursion limit as a damaged line, like invalid JSON, instead of crashing the whole check.
- The report-delivery hook recognises a `/research:topic` follow-up question that offers `/research:topic plan` as that workflow's own gate, so its report path, title and header-table checks apply instead of being skipped.
- `bin/resolve_shared.py` gains `--persist` and `--emit`: the eight Agent Resolution blocks resolve the shared directory in one call that verifies each named document and exits 1 with `! Plugin path resolution failed` or `! <doc> not found`, where the old empty-value guard could never fire.
- New `bin/revert_iteration_commit.py` replaces the two inline revert-guard blocks in `run` Phase 5 and Phase 7, and new `bin/codex_state.py` (`read`, `bump --outcome noop|productive`) replaces the Codex counter forks; a malformed counter file now warns on stderr instead of reading as 0 silently.
- `run` and `plan` resolve their own skill directory from `${CLAUDE_PLUGIN_ROOT}` without the cache glob, and `judge`, `verify`, `retro`, `fortify` and `run` build report paths with `resolve-anti-overwrite-path.py` in a fenced block instead of an improvised loop.
- Nine `--keep` contract writers across six skills (`kaggle`, `fortify`, `topic`, `sweep`, `judge` and `run`) pass `--keep-sentinel <name>`, and `/research:setup` merges allow and deny rules through `merge_settings_permissions.py` in one call that reports `dropped` for the obsolete rule.
- `retro` prints `retro_result` and `effect_r` (the effect-size step was invisible), and its three setup blocks are one, which also fixes an empty `RUN_ID_ARG`; `judge` persists `SKIP_VALIDATION` so it no longer reads false in a fresh shell; `verify` prints `CODEMAP_RAW`; `run` prints `CHANGE_LINES` and the sandbox network and verify-timeout values.
- `fortify` setup runs in four blocks instead of eight and still stops on an invalid venue; `sweep` loads only the plan sections it runs.
- `data-steward` probes foundry once in the acquisition sidecar load and echoes the result; `fortify`, `judge`, `kaggle` and `retro` drop tools they never use.
- `run` writes the parsed program into `state.json` `config` (metric and guard commands, `metric.direction`, scope, compute, strategy, iteration ceiling) instead of an empty `{}`, so `/research:fortify` finds the campaign's metric and guard without `METRIC_CMD`/`GUARD_CMD` overrides and `/research:retro` tests a lower-is-better metric in the right direction. A text goal records its auto-detected commands and prints the inferred direction; `retro` warns when an older run records no direction instead of silently assuming `higher`.
- `run` accepts `metric_repeats` (1–10, default 1) and `min_delta_pct` (default 0.1) in `## Config`: above 1, the baseline and every iteration measure the metric that many times, each run under its own verify timeout, and a change is kept only when its mean gain beats `max(min_delta_pct, 2σ of the difference)` — the noise model `fortify` uses. New `bin/run_metric_stats.py` tracks the runs and makes the keep decision; metric cost scales with `metric_repeats`, and the default keeps the single-sample behaviour. `plan` writes both keys into new programs.
- `run`'s revert guard stops with `! BLOCKED` instead of silently skipping when `HEAD` is another iteration's experiment commit, so a mistyped iteration number in the Phase 4 subject no longer keeps a change the loop decided to revert.
- `run` and `fortify` stop with `! MISSING — jq not on PATH` before reading `state.json`, instead of misreporting a missing `jq` as an unreadable `state.json` or a `.config` without metric commands.
- `verify` prints the plugin's untrusted-content protocol (`skills/_shared/untrusted-content.md`: fresh-token fence, neutralised end markers, data never instruction) with its other startup docs, so the lead applies the rule its scientist prompt already states.
- `data-steward` loads its web-explorer handoff protocol with one `resolve_shared.py --emit` call; the old two-step form read an unset variable and failed with `cat: /file-handoff-protocol.md`.
- `sweep`'s mid-loop contract refresh also aborts on an unsubstituted `<MAX_REFINE>`, which could otherwise land verbatim in the compaction contract.
- `plan --team` no longer appends a `## Team Mode Notes` section it had no data to fill, and no longer prints the whole run team-mode file; it still says `--team` applies at run time and warns when the team mode is missing.
- `run --colab` no longer claims the ideation agent can prototype through Colab MCP — no ideation agent has that tool; Phase 5 measures through it.
- Helper hardening: `fix_jupytext_blank_md.py` skips a file outside the working directory, `retro_analyze.py` streams its JSONL and rejects one over 64 MiB, `compute_effect_size.py` rejects stdin over 1 MiB, `read_state_field.py` refuses a state file over 16 MiB, `verify_patient_split.py` reads only the identifier column, `fortify_next_variant.py` streams its ledgers, and `load-agent-reference.py` picks the newest non-orphaned cached version instead of walking the whole plugin cache.
- `resolve_shared_path.py` no longer executes a `plugins/<dir>/bin/get_plugin_install_path.py` found in the working directory; it runs the helper shipped beside itself and ignores a registry `installPath` outside `~/.claude/plugins/cache/borda-ai-rig`.
- `blueprint-manifest.json` is regenerated with the allowlist gate: 685 of the 707 entries the previous gate admitted stay auto-allowed, including the `fortify` `bash -c "$METRIC_CMD"` and `"$GUARD_CMD"` blocks, the `kaggle competitions list` check and the setup blocks that `cp`. Three blocks are left out and prompt: the `kaggle` block that runs `kaggle competitions download`, which writes data files, and the two `judge` blocks whose command word is the `${metric_cmd}`/`${guard_cmd}` placeholder the model fills in.
- The bundled `blueprint-allow.js` refuses a manifest hit that hides a destructive command behind a `case` arm label or inside a function definition (`case x in a) rm -rf /;; esac`, `f() { rm -rf x; }`), which it let through before.

## 0.28.7

- Sync `write-guard.js`: a tracked file never asks, and an untracked or gitignored file asks only for `CLAUDE.md`, `AGENTS.md`, `.claude/settings*.json`, `.pre-commit-config.yaml` and `pyproject.toml`. `.github/**`, `CHANGELOG.md` and lockfiles are no longer protected, so editing a newly created file there no longer prompts on every edit. A protected file that does not exist yet asks once on creation; later writes to it in the same session pass.

## 0.28.6

- Sync the file-handoff protocol: a tool or permission denial that blocked part of a task returns `done_with_concerns`, never `done`.

## 0.28.5

- Shared compaction contract synced with foundry's non-blocking boundary gate: an `advise` decision continues with the next phase and shows a short one-line `/compact` hint at the next natural pause; only `FOUNDRY_COMPACT_GATE=stop` holds the run.

## 0.28.4

- Sync `write-guard.js`: a protected file git already tracks no longer asks before a write, since the write is diffable and revertible; untracked, new and gitignored protected files still ask, and `.claude/settings*.json` and `.pre-commit-config.yaml` always ask.
- Sync the adversarial-loop rule: reviewer briefs that run adversarial shell cases require case files written with a file-editing tool and fake `gh`/`git` binaries.

## 0.28.3

- Keep review coverage and confidence gaps as factual limitations; require a specific user decision for additional review and omit declined requests from findings and final actions.

## 0.28.2

- Sync the quality-gates rule: after each validated adversarial-loop round, print the progress table exactly, with all rows kept and each cell as `old + new`.

## 0.28.0

- Write Research skill contracts atomically with validated optional gate metadata and a session/digest owner stamp, allowing the continuation path to verify which session produced the contract.
- At main-thread phase boundaries, consult Foundry’s optional compaction gate and continue safely when it is unavailable. A compact decision emits a `/compact` keep payload and waits for continuation; restore saved report and task inputs without repeating the phase, require confirmation for unverified contracts, and skip foreign-session stamps.
- Require topic follow-up questions to name the report path, title and outcome within the 1,500-character question budget. Save longer workflow context to a file and summarize it in question text; retain the full report header in the final response after the answer.

## 0.27.2

- Remove broad GitHub API, branch and tag commands from Research’s shipped allow list. Remove shipped GitHub-write and local branch/tag deletion denies while retaining destructive-operation protections.
- Keep existing user allow and deny entries during Research setup; stale-deny cleanup remains with Foundry setup, so already-installed entries persist until that migration is performed.

## 0.27.1

- Remove the obsolete `resolve-quality-gates.sh` helper and read the scientist’s link-integrity requirements from the loaded quality-gates rule.

## 0.27.0

- Accept the report header in the first follow-up question’s text or eligible option previews; when the delivery budget is exceeded, require a compact summary and saved-report path instead of silently dropping the context.
- Add a SessionStart stale-plugin warning naming loaded and installed versions and restart/resume steps. Ask once per session and loaded version whether to restart or knowingly continue; compaction can repeat the warning without re-asking.

## 0.26.13

- Move shared output-routing policy to Foundry while retaining Research’s follow-up examples in its quality-gate delta; remove the obsolete Git-slug helper and run commit-sentinel steps.
- Resolve the router’s API model through an override, a 24-hour cache, the Models API or a logged fallback. Disable thinking for bounded routing replies and report API or timeout failures before falling back to a general-purpose agent.

## 0.26.12

- Require quality-gate completion claims to cite concrete evidence and stay within the scope of checks actually performed.
- Require a representative real case alongside TDD and recheck it before completion; mocks supplement real validation and unavailable real acceptance remains explicit.

## 0.26.11

- Re-read missing visible report-header delivery for up to two seconds to allow transcript output to flush, and include the observed visible-character count in diagnostics. Reasoning and tool output do not count as user-visible delivery.

## 0.26.10

- Check report delivery again at Stop, track delivery by report version and read the Stop payload’s last assistant message when available.
- Require two-decimal confidence deductions that sum to the reported shortfall, including tier and fetch penalties; separate retrospective finding confidence from statistical-test availability.

## 0.26.9

- Add native Windows Python fallback for Research helper recipes, preserving arguments, exit status and one-time workload execution after runtime validation.

## 0.26.8

- Add a Python fallback for unchanged skill and agent recipes; try known Node locations for hooks and report inactive hooks at SessionStart when Node is unavailable.

## 0.26.7

- Remove the fixed three-review cap from Research’s adversarial loop: continue while the unchanged-scope score strictly decreases, stop on a plateau or caller budget, and resolve feasible authorized findings before requesting a new decision.

## 0.26.6

- Convert research run, team, and fortify checkpoints to append-only JSONL ledgers so later updates do not rewrite earlier records.
- Keep isolation-motivated agent spawns even when their work is small.

## 0.26.5

- Correct the GitHub repository API permission pattern in Research setup and document the exact migration from the obsolete rule.

## 0.26.4

- Select targeted tests during fix loops, then run the full suite once at the final gate; wait for agents through notifications and report timed-out agents.

## 0.26.3

- Restructure long Research prose into readable lists and paragraphs.

## 0.26.2

- Add stale-root detection from installation-registry paths and orphan markers; callers can distinguish a loaded stale root from the active installation.
- Use concise descriptive fields in Research report templates and keep authoring instructions out of completed reports.

## 0.26.1

- Add a block-and-flow architecture map for the topic workflow, including fan/join stages, gates, mode branches, and degenerate cases.

## 0.26.0

- Install Research’s quality-gate delta only when Foundry rule ownership is proven; otherwise deliver the complete Research rule so standalone installations retain every obligation.
- Discover deferred task tools before calling them, combine bookkeeping with substantive work, and batch related cleanup, contract and context-loading calls.

## 0.25.5

- Adopt the shared notebook-authoring contract in Kaggle workflows while keeping Kaggle-specific guidance as a small extension; update the Jupytext blank-cell helper to use the named Markdown rule.

## 0.25.4

- Split long Research Python helpers into smaller functions and add `main()` docstrings; regenerate the blueprint manifest.

## 0.25.3

- Inventory review coverage and finding origins before repair. Require each fix to name its invariant, original counterexample, downstream consumer and sibling route, with failing-before and passing-after evidence.

## 0.25.2

- Dispatch data-steward modes from caller prompt intent, prioritizing review intent over acquisition keywords; label the judge's merged scientist/architect review accurately.
- Record lower-priority data-integrity and hygiene observations instead of omitting them when more severe findings exist, cap confidence for acknowledged missed findings, and execute compound fortify metric commands through the shell.

## 0.25.1

- Record each finding’s fix, escalation or justified deferral, and require later independent verification before closing a repair.
- Keep every finding when a reviewer exceeds the advisory 1,500-token output target; repeat the convergence table only when a new round has completed.

## 0.25.0

- Show completed convergence rows in status, approval/recovery and pause messages; use not-run/N/A when no round is validated and retain the final Results table.

## 0.24.4

- Show cumulative old/new finding counts and weighted scores after each completed challenge, before the next fix, review or stop. Keep pending reviews distinct from completed clean rounds.

## 0.24.3

- Reject failed Codemap query output and distinguish static test/mock relationships from measured coverage while keeping formula and source verification explicit.

## 0.24.2

- Block topic follow-up until the saved report and matching visible header exist; retain diagnostic questions and add frontmatter to topic plan reports.

## 0.24.1

- Ship the common skill compaction-contract format and boundary lifecycle, recording the active skill, source paths, progress and next steps for continuation after compaction.

## 0.24.0

- Resolve the Codemap gate contract from the active `codemap-py` installation, remove Research’s frozen gate copy and preserve the fallback when the provider is absent. Follow the loaded provider contract’s documented stale-index policy.

## 0.23.5

- Condense Research agent and skill guidance covering test contracts, commands, flags, paths and workflow order.

## 0.23.4

- Extend adversarial review through unchanged downstream consumers and the next ordinary user action; record handoffs that were not verified.
- Use a dedicated `%%bash` cell for consecutive notebook shell commands and `# !` for a single inline command.

## 0.23.3

- Add the shared adversarial review loop to Research quality gates, with evidence-backed convergence scoring and an explicit stop when findings remain open.

## 0.23.2

- Restore quality-gate requirements lost from the Research copies and verify their parity with the canonical rule.

## 0.23.1

- Parse skill flags with Python helpers instead of shell regexes, fixing captures that were empty under zsh. Match `--team` as a complete token and keep flag-shaped words inside `--keep` values from becoming workflow options.

## 0.23.0

- Add the quality-gate convergence loop: track scores across review/fix rounds and ask for a decision when the loop stops with findings open.

## 0.22.2

- Replace zsh-incompatible `BASH_REMATCH` parsing in Kaggle, fortify and sweep with argument parsers.

## 0.22.1

- Ship Research’s propagated Codemap gate contract and load it from the active Research root, retaining the fallback when the optional provider is absent.

## 0.22.0

- Add a Bash auto-allow audit trail so permitted blueprint commands are recorded and verifiable.

## 0.21.1

- Document Research helper behavior and verify their executable examples against the implementation.

## 0.21.0

- Add permission-deny rules to Research and merge its own allow/deny policy during setup with a backup and validation step.
- Add CPU, memory, and process quotas to research sandbox containers, with positive-value overrides and safe defaults.

## 0.20.1

- Validate `program.md` section headers before judging a research proposal, so malformed input stops at the schema gate.
- Trigger additional review for a specific actionable gap or unproven claim instead of routine score thresholds; preserve all discoveries before filtering the presented summary.

## 0.20.0

- Reduce research workflow spawns and repeated work: combine related plan and judge advisors, fetch paper content once, avoid redundant timing runs, and report actual launch batches.
- Persist Codex usage counters in experiment state; make topic SOTA search single-owner and let hypothesis oracles annotate feasibility.
- Reduce fortify’s per-variant shell calls while ensuring skip paths still advance the variant cursor.

## 0.19.0

- Replace the legacy Codex integration with the self-contained Bridge `implement`, `advise`, and `review` calls, including explicit unavailable and disabled behavior.
- Format plugin Markdown through the repository formatter and condense Research ML reference guidance.

## 0.18.1

- Close sentinel-read auto-allow bypasses so command forms outside the read-only contract continue through normal permission checks.

## 0.18.0

- Guard writes to protected files with a shared hook, propagated to Research and registered in its hook configuration.

## 0.17.0

- Add stale Git lock and worktree reporting; require explicit confirmation before removing worktree directories.
- Have fortify sweep its variant root after Git prunes worktrees, including worktrees whose accumulator entry was never saved.

## 0.16.0

- Auto-allow only validated blueprint-manifest commands, with the hook registered after the sentinel-read gate.

## 0.15.4

- Add a tested Jupytext helper that removes heading-spacer lines inside Markdown cells only; Kaggle generation now applies the cell-scoped repair instead of a file-wide regex.

## 0.15.3

- Move test capability skips to collection time and update Research docs to match the shipped agent and skill roster.

## 0.15.2

- Anchor Codemap paths at the Git root so subdirectory invocations select the same index. Preserve UTF-8 paths and raw basenames through Research’s resolver and flag handling.
- Read stale-index currency from the returned JSON even when the provider exits nonzero, so the gate does not lose its stale verdict.
- Normalize Windows extended-length symlink targets so setup recognizes and updates its own rule links.

## 0.15.1

- Ground Kaggle notebooks in the authenticated Kaggle CLI’s competition files, leaderboard, and sample submission instead of guessing the submission schema.
- Keep Kaggle credentials out of prompts, notebooks, and artifacts; gate full archive downloads behind a size-aware user choice.

## 0.14.3

- Remove self-evident shell comments from Research workflows while retaining comments that explain constraints or edge cases.

## 0.14.2

- Resolve the shared Research helper once per skill and reuse a session sentinel in fortify, plan, retro, and verify to cut repeated subprocess calls.

## 0.14.1

- Route Research Python scripts through the interpreter so skill commands work on native Windows, where extensionless shell-style calls are not portable.
- Record an agent-spawn budget and avoid parallel specialist calls when inline work is cheaper.

## 0.14.0

- Replace repeated inline logic with Python helpers and cache resolved shared paths in session-scoped sentinels for workflow reuse.
- Add the Research-specific scientist Codemap preamble and a shared transcript helper that nudges when the visible report-header table is missing.

## 0.13.1

- Move agent sidecar references out of the scanned `agents/` tree and update loaders to use the new `references/` location, preventing sidecars from being registered as agents.
- Fix zero-match `grep -c` handling where the fallback could append a second zero and corrupt numeric output.

## 0.13.0

- Add `/research:setup` to deliver only Research’s own rules into Claude’s user-level rule namespace without modifying settings.
- Add a tested `sync_rules.py` helper that validates source rules before creating, refreshing, pruning, or preserving destination links.

## 0.12.8

- Validate retrospective metric direction with a closed enum, and render topic-team report metadata as a two-column table.

## 0.12.7

- Make Research workflows read shared contracts and helpers from their own plugin installation, so the plugin remains usable without Foundry.

## 0.12.6

- Rehydrate shell variables and sentinel state between separate tool calls, and fix path handling that lost values or a trailing newline.

## 0.12.5

- Require research topic reports to print their report path before follow-up questions and carry that path through single-agent, team, and plan flows.

## 0.12.4

- Replace ordinal `#N` references that looked like GitHub issue numbers with plain step/rank labels in Research instructions.

## 0.12.3

- Enforce printing the research report header before the topic workflow asks a follow-up question.

## 0.12.2

- Label Codemap references as `codemap-py` throughout Research skills so headings and handoff parsing identify the correct provider.

## 0.12.1

- Migrate Research’s Codemap commands, resolver and gate calls to the `codemap-py` CLI and namespace.
- Correct file-permission allow-rule forms so the shipped rules use syntax the host accepts.

## 0.12.0

- Add a hardened sentinel-read allow hook across Research skills so only read-only sentinel substitutions bypass permission prompts; writes, traversal, and environment-changing forms remain denied.
- Replace command-substitution sentinel reads with `IFS= read -r` to avoid triggering broad expansion permissions.

## 0.11.0

- Scope temporary sentinels to the Claude session ID so concurrent sessions cannot overwrite each other’s state.
- Move skill contracts from `.claude/state` into the project’s `.temp/state` directory and audit temporary sentinels for missing session suffixes.

## 0.10.9

- Move the plugin directory to `plugins/cc_research` while retaining the `research:` command namespace.

## 0.10.8

- Require Kaggle notebooks to both optimize the competition metric and teach the workflow top-to-bottom; add an executable check that repairs bare Markdown heading spacers.

## 0.10.7

- Run Codemap index builds through the foreground CLI instead of invoking the slash-only scan skill, which could not be dispatched through the Skill tool.

## 0.10.6

- Load shared documents through shell reads rather than the host Read tool where the latter could not resolve the plugin’s shared-file path.

## 0.10.5

- Compose the existing Kaggle workflow from selected foundation, EDA, training, inference and submission contracts; validate the sections required by full, EDA-only and inference-only modes.
- Require notebook steps to fail promptly on errors and reject mutually exclusive EDA-only and inference-only selections before generation.

## 0.10.4

- Harden Research helpers and workflows: constrain Docker verification commands, make run discovery resilient to concurrent deletion, support detached Git states, and align quality-gate confidence thresholds.

## 0.10.3

- Add consistent `--help` and argument parsing to Research command-line helpers through the shared argparse migration.

## 0.10.2

- Change the declared plugin license from MIT to Apache-2.0 and synchronize the README’s displayed version.

## 0.10.1

- Move judge J3 prompt templates into a shared file, gate the Codex adversarial pass by program complexity, and mark the data-steward agent manual-only.

## 0.10.0

- Add per-skill compaction contracts to Research’s phased workflows so progress, decisions, and next steps survive automatic context compaction.
- Save contract state at workflow boundaries and clear it when the task completes; the first boundary remains the earliest point at which skill-specific context can be preserved.

## 0.9.5

- Wire the Docker sandbox runner into research metric-computation modes; it had been tested but was not used by the workflow.
- Replace unreachable synchronous-agent polling with post-return artifact checks; missing or empty expected artifacts produce timeout markers and partial-result handling.

## 0.9.4

- Let research runs honor `program.md` iteration limits up to 50 and source fortify metric/guard commands from experiment state.
- Normalize program paths and fail closed when Git metric state cannot be verified.

## 0.9.3

- Recompute variables lost between shell blocks across fortify, Kaggle, judge, and retro, restoring correct run paths, selected modes, output filenames, and specialist dispatch.

## 0.9.2

- Re-derive topic arguments before unknown-flag and `plan` mode routing, and rebuild the commit-sentinel path before cleanup.
- Cache local-role descriptions at SessionStart; reroute unavailable roles through semantic matching, using embeddings when configured and API selection otherwise before general-purpose fallback.

## 0.9.1

- Detect Foundry in marketplace-independent cache paths and the source tree instead of searching for plugin names among marketplace directories.

## 0.9.0

- Share Kaggle style rules across notebook modes, keep configuration next to the cells that consume it, and support frozen packages for offline kernels.
- Require competitor-notebook context scans and make source-package extraction an explicit post-validation offer.
- Skip the architect review spawn for narrow judge scopes while marking the methodology rating directly.

## 0.8.0

- Add Research Codemap integration with automatic, strict, and disabled modes, plus shared context and gate contracts for run and paper verification.
- Make Codemap index availability determine whether automatic mode injects blast-radius context.

## 0.7.4

- Extract repeated Git slug generation into a Research helper and fix agent-router handoffs to preserve agent descriptions.

## 0.7.3

- Fix Research agent XML-style instruction delimiters so scientist and data-steward sections are interpreted as intended.

## 0.7.2

- Remove comments that merely restated code across Research agent and skill files.

## 0.7.1

- Remove invalid agent frontmatter from reference sidecars and replace a personal Kaggle example with user-neutral wording.

## 0.7.0

- Add `/research:kaggle` to generate competition notebooks as Jupytext scripts, with full, EDA-only, and inference-only modes and modality-aware data exploration.
- Add an offline-setup path for inference notebooks and shared unsupported-flag handling across research workflows.
- Route broad topic searches to web search and reserve the scientist agent for deep analysis of one paper.

## 0.6.9

- Reuse Research’s run-directory and health-monitor helpers, and fail explicitly when the shared plugin path cannot be resolved.

## 0.6.8

- Move Colab setup and GPU-backed metric verification into a dedicated run mode.
- Move the plan overwrite guard ahead of advisor results and combine related decisions into one question.
- Add agent resolution and calibration guidance to fortify.

## 0.6.7

- Repair Research skill parsing and references during the shared executable-output routing update.

## 0.6.6

- Add fail-fast checks, JSONL sanitization, and stronger Codex detection across Research workflows.
- Document reproducibility and concurrent-storage risks in the research agent patterns.

## 0.6.5

- Persist fortify, judge and topic paths across separate shell calls; distinguish verification sentinels by branch and date so concurrent work does not reuse the wrong state.
- Extract run metric-computation and resume modes into loadable files, and tighten the scientist’s plugin-prefixed agent references.

## 0.6.4

- Update Research references to the current Foundry setup section after the setup workflow was consolidated.

## 0.6.3

- Validate sweep output containment with a shared helper, and correct Docker absolute-path checks and mount spelling for Windows paths.

## 0.6.2

- Fix the judge’s specialist prompt to use resolvable program and run paths.

## 0.6.1

- Add `find_run_id.py` and `read_state_field.py` to replace inline JSON extraction, preventing run-ID injection and correcting fortify state lookup.

## 0.6.0

- Replace Research shell helpers with cross-platform Python equivalents and extract patient-split verification from inline skill code.

## 0.5.2

- Extract effect-size computation into a tested Python helper and move topic planning and team flows into mode files.

## 0.5.1

- Promote judge findings about optimizing a proxy at the expense of the real objective to blocking severity; skipping validation cannot produce an APPROVED outcome.
- Validate sweep output paths and load explicitly supplied hypothesis files. Harden run-directory, health-monitor and Docker helpers so invalid inputs fail before work starts.

## 0.5.0

- Move Research shell business logic into typed, tested Python helpers; keep skill instructions focused on workflow decisions.
- Add a scientist ML-concepts reference specialization for machine-learning research tasks.

## 0.4.3

- Add helpers for run-directory creation, shared-path resolution, and health-monitor startup; restore the documented flat run-directory shape.

## 0.4.2

- Make Research run, retro, and fortify report paths anti-overwrite by selecting the next unused suffix; remove the obsolete retro health-monitor sentinel.

## 0.4.1

- Strengthen fortify’s metric sign and coupled-component checks, judge’s proxy-metric and no-op detection, and retro’s threshold and timeout analysis.
- Use a consistent `python` command for Research Python helpers.

## 0.4.0

- Store research reports under `.reports/research/` and add YAML metadata to report templates.
- Normalize report frontmatter and shared output conventions across Research workflows.

## 0.3.8

- Fix research audit findings: remove an agent-name collision, repair fallback paths and allowed tools, and mark optional cross-plugin references explicitly.
- Condense Research instruction files.

## 0.3.7

- Move plan's unsupported-flag check before input processing and use an interactive goal choice after file profiling; resolve sweep shared paths before its first step.

## 0.3.6

- Fix shared-path resolution, duplicated judge execution, and stale shell-variable references across plan, judge, sweep, run, and verify.
- Remove the redundant polling step from the synchronous verify agent call.
- Ask before silently ignoring unsupported skill flags.

## 0.3.5

- Add paper-verification cutoff constants and refinement-pass reporting, plus run health-monitor checkpoints.

## 0.3.4

- Add agent routing through an exact installed role, optional API-assisted local-role selection, and a general-purpose fallback when the preferred agent is unavailable.
- Require a follow-up skill selected through a quality-gate question to execute in the same turn.

## 0.3.3

- Remove the judge committee mode and create all review checkpoints before dispatching agents.
- Make fortify handle zero-valued baseline metrics and read the judge verdict from its artifact instead of inferring success from a rating.
- Use file-based review handoffs for plan specialists.

## 0.3.2

- Decline nonresearch scientist inputs without returning partial findings, and forward the sweep’s `--architect` option into the run workflow.
- Remove redundant Notes headings while retaining workflow notes.

## 0.3.1

- Add the topic workflow’s standalone agent-resolution fallback and explicit follow-up question; make the planning workflow’s `--team` path a named task-tracked phase.

## 0.3.0

- Share agent-resolution fallback guidance across Research skills instead of duplicating it.
- Add command timeouts and repair paths, directory cleanup, and metric-check behavior across run, plan, judge, fortify, retro, and topic workflows.

## 0.2.0

- Add `research:fortify` for ablation studies, `research:retro` for post-run statistical review, and `research:verify` for paper-to-code fidelity checks.
- Split data-steward and run guidance into mode-loaded sidecars, reducing always-loaded instruction size while retaining specialized details.
- Add the research MkDocs site and GitHub Pages workflow.

## 0.1.0

- Create the standalone Research plugin with scientist and data-steward agents and the `topic`, `plan`, `judge`, `run`, and `sweep` skills.
- Add skill metadata, team/advisory modes, and resume handling; migrate research run constants and shared quality guidance into the new plugin structure.
- Make research commands use their namespaced plugin invocations and document standalone installation with graceful general-purpose agent fallbacks.
- Move the run protocol into the plugin and standardize research report paths and plugin-prefixed agent references.
