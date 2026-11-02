# develop — Development Guide

Technical detail for contributors and agents working on the `develop` plugin. User-facing documentation lives in [README.md](README.md).

## Contents

- [Plugin structure](#plugin-structure)
- [Permission manifests](#permission-manifests)
- [Hook internals](#hook-internals)
- [Bin helper inventory](#bin-helper-inventory)
- [Auto-allow audit log](#auto-allow-audit-log)

## Plugin structure

```text
plugins/cc_develop/
├── .claude-plugin/
│   ├── plugin.json              -- manifest (name, version, author)
│   ├── permissions-allow.json   -- tool calls the skills expect to be pre-approved
│   ├── permissions-allow-retired.json -- obsolete allow rules /develop:setup removes
│   └── permissions-deny.json    -- operations that must stay denied (see below)
├── bin/
│   └── sync_rules.py            -- installs rules/*.md into ~/.claude/rules/
├── rules/
│   ├── quality-gates.md         -- delivered as ~/.claude/rules/develop-quality-gates.md
│   └── _full/adversarial-loop.md -- procedure for explicitly requested iterative review/fix loops
└── skills/
    ├── plan/
    │   └── SKILL.md
    ├── feature/
    │   └── SKILL.md
    ├── fix/
    │   └── SKILL.md
    ├── refactor/
    │   └── SKILL.md
    ├── debug/
    │   └── SKILL.md
    ├── review/
    │   └── SKILL.md
    └── setup/
        └── SKILL.md
```

## Permission manifests

- `.claude-plugin/permissions-allow.json` lists the tool calls the skills expect to be pre-approved.
- `.claude-plugin/permissions-allow-retired.json` lists allow rules earlier versions shipped; `/develop:setup` removes exactly those from `settings.json` through `--drop-allow-file`, keeping the rule text out of the command line. The setup summary counts removals from this complete retirement set.
- `.claude-plugin/permissions-deny.json` is its counterpart — the operations that must stay denied no matter how broad the allow list becomes: destructive shell and git commands (`rm -rf`, `sudo`, `ssh`, `chmod 777`, force-push, `claude --dangerously-skip-permissions`). It holds no gh write or local branch/tag deletion entry.

Develop ships no git/gh guard; foundry supplies that guard (requires `foundry` plugin). Develop's allow list omits `Bash(gh api repos/*)`, `Bash(gh api graphql:*)`, `Bash(git branch:*)`, and `Bash(git tag:*)`. Without foundry or other matching user rules, these command families reach normal permission handling on new installs, including prompts for routine reads such as `git branch --show-current`.

Upgrades preserve these four allow entries in existing settings if an older setup merged them. `/develop:setup` does not remove them because foundry ships the same strings; without foundry, remove them manually from `~/.claude/settings.json` to restore prompts. Stale gh deny entries from older versions are cleaned only by `/foundry:setup` (requires `foundry` plugin).

Both files are merged into `~/.claude/settings.json` by `/develop:setup` (Step 5). Setup removes the obsolete `Bash(gh api repos/*:*)` allow entry and the broad `Bash(claude:*)`, `Bash(find:*)`, `Bash(node:*)`, `Bash(sed:*)` and `Bash(eval:*)` entries while preserving every other existing allow and deny entry; the merge is otherwise additive and idempotent. Deny entries are prefix matches, so they stop the documented command forms rather than every possible flag ordering.

## Hook internals

Hooks register automatically from `hooks/hooks.json` when the plugin is enabled — no `settings.json` edits needed.

- Each hook runs `node` from PATH, then `/opt/homebrew/bin/node` or `/usr/local/bin/node`; when none exists, the hooks skip without blocking and one `SessionStart` check reports once per session that they are inactive.
- The plugin's `bin/python` and native Windows `bin/python.cmd` preserve the shipped `python` command and probe candidate runtimes for Python 3.10+ before executing the workload once.
- Plugin `bin/` follows system directories on PATH, so an eligible system `python` takes precedence; missing eligible runtimes produce a clear diagnostic.

| Hook | Event | Behaviour |
| -- | -- | -- |
| `agent-router.js` | `PreToolUse` (`Agent`) | Reroutes `Agent()` calls when the requested agent is not installed: exact match → semantic match → `general-purpose`. |
| `sentinel-read-allow.js` | (decision module, no longer registered as a hook of its own; called by `allow-dispatch.js` in rank order) | Auto-allows the pre-canned TMPDIR sentinel-read and `$(date +FMT)` idioms inside read-only commands, so skill bash blocks stop raising "Contains expansion" prompts. Everything else falls through to normal checks. Whole-line `# …` comments inside a block are skipped rather than blocking it, and `..` is matched as a path component so ellipsis and version ranges are not mistaken for traversal. |
| `blueprint-allow.js` | (decision module, no longer registered as a hook of its own; called by `allow-dispatch.js` in rank order) | Byte-identical copy of the cc_foundry canonical (propagated via `propagate_shared.py`); normalizes the command, hashes it, and exact-matches it against this plugin's committed `blueprint-manifest.json`, so verbatim bash shipped in its own skills runs without a prompt. Any deviation from the blueprint text misses and falls through to a normal prompt. |
| `allow-dispatch.js` | `PreToolUse` (`Bash`) | Byte-identical copy of the cc_foundry canonical (propagated via `propagate_shared.py`); the only registered Bash auto-allow hook. Calls `blueprint-allow.js` then `sentinel-read-allow.js` as libraries, emits the first allow, and appends one audit record naming the effective verdict and both modules' own verdicts. Exits 0 on every path: an allow-only hook that crashes grants nothing and the call falls back to normal permission handling. |
| `audit-close.js` | `PostToolUse`, `PostToolUseFailure` (`Bash`), `SessionStart`, `SessionEnd` | Byte-identical copy of the cc_foundry canonical (propagated via `propagate_shared.py`); appends one record observing that a Bash call completed, and one per session boundary. Writes nothing to stdout on any event. Every installed plugin writes its own rows with nothing coordinating them. |
| `lib/audit-log.js` | (shared module, not a hook) | Byte-identical copy of the cc_foundry canonical (propagated via `propagate_shared.py`); the shared record writer behind the two hooks above. Append-only — no lock, marker, state directory or deletion path — and never throws, so a logging failure cannot change a permission decision. `RIG_AUDIT=0` disables writing only. |
| `write-guard.js` | `PreToolUse` (`Edit`, `Write`, `NotebookEdit`) | Byte-identical copy of the cc_foundry canonical (propagated via `propagate_shared.py`); grants nothing — forces a confirmation on writes to untracked files in the protected set (`CLAUDE.md`/`AGENTS.md`, `.claude/settings*.json`, `.pre-commit-config.yaml`, `pyproject.toml`); a tracked protected file never asks, since every write to it is diffable and revertible, and a protected file the session created asks once on creation, then passes for that session. Source, tests, `.github/**`, `CHANGELOG.md` and lockfiles are deliberately unprotected; everything else is silent passthrough. |
| `stale-plugin-check.js` | `SessionStart` | Byte-identical copy of the cc_foundry canonical (propagated via `propagate_shared.py`); when this session still runs a replaced develop version (the loaded root carries `.orphaned_at` or is no longer the installed path), shows the user a warning naming develop with its loaded and installed versions and the restart step (`exit, then claude --resume <session_id>`), and tells the model not to run develop workflows on the stale copy until the user chooses, through `AskUserQuestion`, to restart or continue — once per session and version: after that ask, a compact/clear warning drops it (a first detection at compact/clear still asks). Silent when current or outside the plugin cache; never blocks. |
| `enforce-review-header.js` | `PreToolUse` (`AskUserQuestion`), `PostToolUse` (`AskUserQuestion`), `Stop` | Blocks only the owning workflow follow-up until its report exists and the follow-up call's first question text is a short plain summary (report path, `Title`, `Outcome`/`Verdict`; at most 1,500 characters, no table); option descriptions and previews never supply it, and a reply-text table before the call does not count. Diagnostic/recovery questions remain available; unreadable delivery evidence is not success. On Stop: keeps a turn going once per report when its final reply (the text after the turn's last tool call) lacks the full header table; a shown follow-up does not satisfy it. |
| `report-header-table.js` | Shared local module | Shared local module: matches workflow-specific questions and current report headers; at question time checks only the call's first question text for the report path and the `Title` and `Outcome`/`Verdict` values within 1,500 characters; at Stop only the final reply after the turn's last tool call counts for the full header table. |

## Bin helper inventory

These helpers are installed workflow support and maintainer surfaces, not additional slash-command skills. The skills own the development workflow; the helpers handle bounded flag parsing, Codemap context, test execution, worktree setup, path resolution, and state extraction.

| Helper | Purpose |
| -- | -- |
| `agent_watch.py` | Report each spawned agent's deliverable and deadline in one call. |
| `append_ledger.py` | Append staged `.rec` records (or `--from` files) to a growing ledger without rewriting it. |
| `adversarial_loop.py` | Record and validate review → fix loop rounds: append a round (and, with `--append-metrics`, its advisory cost record), print the progress table, require root cause on a repeated finding and stop at its third occurrence. |
| `check_bridge.py` | Report whether the Claude-Codex bridge plugin is installed and enabled (`true`/`false`, or `--status` for `available`, `disabled`, `absent`). |
| `build_codemap_batch.py` | Build one Codemap pre-flight batch for changed modules. |
| `codemap_resolve.py` | Resolve Codemap auto, strict, or disabled mode. |
| `codemap_scan.py` | Derive affected modules and emit structural Codemap queries. |
| `compaction_boundary.py` | Print the compaction Boundary step from the plugin's contract, then run the optional foundry compaction gate. |
| `derive_codemap_target.py` | Derive TARGET_MODULE/TARGET_FN from a skill goal; `--keep-state <prefix>` writes each value to a per-session sentinel the skill reads with `IFS= read -r`, so no `eval`. |
| `dev_batch_gate.py` | Decide whether feature/refactor run in batch mode (`--no-batch` opts out); when on, record the calling skill in `dev-batch-skill`. |
| `dev_codemap_gate.py` | Normalize and persist Codemap mode for all six workflows. |
| `dev_issue_fetch_wrap.py` | Fetch and persist upstream issue context for development skills. |
| `dev_load_shared.py` | Print one `skills/_shared` document (warm `dev-shared` sentinel, else a fresh resolve); `--optional` turns a missing document into a skip warning. A cold resolve outside the develop source repository with no install exits 1. |
| `dev_parse_args.py` | Parse development-skill arguments into shell-safe assignments. |
| `dev_run_dir.py` | Create a timestamped `.developments/` run directory and optional sentinel; `--persist <skill>` also writes the skill's `dev-<skill>-dev-dir` sentinel. |
| `dev_run_repro.py` | Reproduce a bug with the allow-listed pytest runner (`--tb=long`, last 60 lines); only pytest exit 1 is reproduced, other non-zero is `! GATE ERROR`. |
| `dev_run_tests.py` | Run the project's test command from its session sentinel without a shell: `pytest` mode appends literal arguments to an allow-listed pytest runner, `suite` mode also accepts `tox`, `tox -q`, `make test`, `npm test`, `cargo test`, `go test ./...`; `--tail N`, child exit status passed through, exit 64 when no test command ran (unresolved, disallowed or failed-to-launch runner); writes the `dev-tests-verdict` session file (`failed <nonce>` only for a pytest failure with a summary, echoing the gate's `DEV_TESTS_VERDICT_NONCE`), which red gates require beside exit 1. |
| `dev_setup_worktree_wrap.py` | Set up team-mode worktree run directories and state. |
| `dev_shared_resolve.py` | Resolve develop's own shared directory portably; `--init` persists the `dev-shared` and a fresh `dev-agent-watch-dir` sentinel for the run. Without an install, the cwd `plugins/cc_develop` tree is used only inside the develop source repository; elsewhere it exits 1. |
| `dev_test_targets.py` | Select the tests a change touches; `--run` runs them in one call; `--skill <skill>` reads the pytest command and run directory from sentinels. |
| `diagnosis_parse.py` | Parse and validate a `--diagnosis` path from arguments. |
| `extract-keep-flag.py` | Parse `--keep "<items>"` and clear a stale compaction contract. |
| `extract_json_field.py` | Recover a JSON object from text and print a selected field. |
| `find-polluter.py` | Binary-search test isolation contamination. |
| `get_plugin_install_path.py` | Resolve the active plugin path from Claude's registry. |
| `heal_git_artifacts.py` | Reclaim stale skill locks and leftover git worktrees; unregistered dirs only with `--include-orphans`, symlinks never followed, `*` prefix rejected. |
| `issue_fetch.py` | Validate an issue argument and fetch it through `gh`. |
| `merge_settings_permissions.py` | Merge the plugin's shipped allow and deny rules into `~/.claude/settings.json` in one atomic write (`/develop:setup`). |
| `parse-skill-flags.py` | Parse boolean and value skill flags into shell assignments; `--keep-state <prefix>` also writes per-session sentinels, so skills read values without `eval`. |
| `plan_hash.py` | Record a `--plan` file's hash and flag edits made during the build. |
| `parse_target_qname.py` | Split a `module::function` suspect out of a skill's arguments. |
| `pytest_gate.py` | Run an allow-listed pytest command with full output. |
| `resolve_review_target.py` | Resolve a review target and its changed Python files; `--committed` diffs `HEAD~1..HEAD`. Exits 1 on a missing path or a target with no Python file. |
| `resolve_shared_path.py` | Resolve the active `codemap-py` install for its shared contracts; the cwd source-tree fallback is used only inside that plugin's own source repository. |
| `review_impact_tier.py` | Read `codemap-py query diff-impact` JSON from stdin and print `FULL` or `LIGHT` with a reason; unknown impact always yields `FULL`. |
| `run_pytest_short.py` | Run an allow-listed pytest command on one or more targets; show final lines. |
| `setup_worktree.py` | Create a team-mode `.temp/develop/` run directory; prints its timestamp and path. |
| `sync_rules.py` | Install namespaced rule symlinks into `~/.claude/rules/`. |
| `verify_blueprint_audit.py` | Verify and prune the auto-allow audit log. |
| `write_skill_contract.py` | Write the compaction-boundary contract the PreCompact hook appends verbatim. |

- `merge_settings_permissions.py` (a byte-identical copy propagated from foundry) merges the plugin's shipped `permissions-allow.json` / `permissions-deny.json` into `~/.claude/settings.json` in one atomic write for `/develop:setup`.
- `write_skill_contract.py --keep-sentinel dev-<skill>-keep-items` reads the user's `--keep` text from the session sentinel and appends it to the contract's `preserve:` line.
- `compaction_boundary.py` (a byte-identical copy propagated from foundry) replaces the inline Boundary-step print and gate lines in the fix and review boundary blocks.

## Auto-allow audit log

`allow-dispatch.js` and `audit-close.js` append one JSON line each per Bash call to `~/.claude/logs/audit/`: one file per session (`s-<key>.jsonl`, the key being a hash of the session id) plus a shared `_no-session.jsonl` for calls that arrive without one.

- A record names the deciding plugin and hook, the session and tool-call ids, the working directory, the effective decision with its lane and provenance, and both decision modules' own verdicts.
- Command text is never written — only a digest, and for a provenance allow the manifest `src` it matched.
- Note the limit honestly: `task-log.js` already writes the first 200 characters of every Bash command into `timings.jsonl` under the same `tool_use_id`, so the guarantee is about this file rather than about the log directory.
- That text is masked first (env assignments, credential-named values and flags, URL credentials, Bearer values, well-known token prefixes), and `task-log.js` tightens the log directory to `0700` and log files to `0600`.

With several plugins installed each writes its own rows and nothing coordinates them, so one completed call yields one row per plugin at each end. That is corroboration, not duplication.

```bash
python "${CLAUDE_PLUGIN_ROOT}/bin/verify_blueprint_audit.py" verify
python "${CLAUDE_PLUGIN_ROOT}/bin/verify_blueprint_audit.py" prune --older-than 30 --dry-run
```

`verify` exits 1 only when a record's own hash fails to verify; torn framing from concurrent appends and every classification are warnings. `record_hash` detects a record that changed after it was written — it is not tamper-evidence, because the same user owns the log, the hooks and the checker.

Read the classifications as observations rather than proof:

- A call with a decision row and no completion row is reported as `completion-unobserved` when the session later ended and `in-flight-or-hard-kill` when it did not, and neither distinguishes a refusal at the prompt from a deny rule elsewhere or a closer that died.
- Equally, that a tool ran never establishes that a human approved it.

`prune` is the only component that deletes a log file, is never run by a hook, and never prunes `_no-session.jsonl` by age — a growing one means the host stopped sending a session id. Nothing schedules `prune`. `RIG_AUDIT=0` disables audit writing without changing any permission decision.

Records follow the twelve mandatory fields of ["Agent Audit Trail: A Standard Logging Format for Autonomous AI Systems"](https://datatracker.ietf.org/doc/draft-sharif-agent-audit-trail/), an active individual-submission Internet-Draft that is not endorsed by the IETF and has no standing in its standards process.

Deliberate deviations:

- `outcome` adds `pending`, because a decision that has not executed yet has no outcome in the draft's vocabulary.
- `trust_level` is `plugin`/`unknown` rather than the draft's `L0`–`L4`, because what is observed is which component proposed an allow, not an authentication level.
- `agent_id` is `<plugin>/<hook>` rather than a URI.
- `session_id` is the host's id verbatim rather than a UUID.
- `parent_record_id` and `prev_hash` are always null, because this log is not chained — lineage is derived on read instead.
