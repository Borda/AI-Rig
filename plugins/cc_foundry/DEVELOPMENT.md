# 🛠️ foundry — Development Notes

Technical detail for contributors, maintainers and agents. User-facing material lives in [README.md](README.md). Content here was moved out of the README unchanged in substance; long paragraphs are split into bullets at sentence and semicolon boundaries.

<!-- consumers: README.md (pointer) -->

## Contents

- [Approval and guard internals](#approval-and-guard-internals)
- [Review loop and workflow internals](#review-loop-and-workflow-internals)
- [Rule loading internals](#rule-loading-internals)
- [Profile and context-composition internals](#profile-and-context-composition-internals)
- [Hook inventory](#hook-inventory)
- [Bin helper inventory](#bin-helper-inventory)
- [Test layout](#test-layout)
- [Rule table (full descriptions)](#rule-table-full-descriptions)
- [Contribution and review expectations (full text)](#contribution-and-review-expectations-full-text)
- [Setup notes](#setup-notes)

## Approval and guard internals

### Local Git grant (full text)

- Local Git operations that need authorization (commit, staging owned changes, a local merge and its merge commit, cherry-pick, branch/worktree prep) ask one question: **Approve** (this operation only), **Approve always** (this operation, plus a standing grant for this project) or **Deny** (skip it and leave changes as they are).
- The `approval-guard.js` hook records an Approve always answer in `claude-git-approval.json` inside git's own common directory (`git rev-parse --git-common-dir`) — once per project
- it applies to every worktree, and git never tracks or checks out anything there.
- While a grant passes the rule's check (`node <foundry>/hooks/commit-guard.js --check-grant`: a regular, non-linked file with the expected version, scope and answer, in the session project's own common directory — resolved with Git's discovery environment removed and bare repositories refused, so a tracked bare-repository fixture, a planted `.git` file or a steered `GIT_DIR` supplies nothing), later sessions skip the question for task-scoped non-destructive local operations, and committing the agent's own completed task is the default on completion
- the reply that commits states `grant .git/claude-git-approval.json@<created_at>` or `no grant`.
- Only the hook writes the grant, from your actual answer to a question whose Approve always option tells you it persists, what it covers and how to revoke it.
- The hook blocks an agent tool call that names the grant directly
- it sees names, not contents, so a patch, script or archive that writes the file is not blocked and stays forbidden by the rule alone.
- `rm "$(git rev-parse --git-common-dir)/claude-git-approval.json"` revokes it.
- A grant never covers push (asked every time), force operations, history rewriting or destructive recovery
- a checkout under review gains nothing from it
- your narrower instructions (no commit, leave unstaged, read-only, wait) still win, and spawned subagents, teammates and bridge children never use it.
- Without a grant or an active skill workflow, the question is asked.

### Push token (full text)

- Every push asks its own question, `git-push`: **Approve** (this push, once) or **Deny**.
- The question shows the branch, the target and the command the agent will run.
- Your Approve makes the `approval-guard.js` hook record a single-use token, `claude-push-approval.json` in the same common directory.
- The token holds the current branch, HEAD and the id of the question you answered, and expires after 15 minutes
- a later Approve replaces it and a later Deny removes it.
- `commit-guard.js` lets one push of that branch at that HEAD to a configured remote run — `git push`, `git push origin`, `-u`, `origin <branch>`, `origin HEAD:<branch>`, `"$(git branch --show-current)"`, `git lfs push origin <branch>` — after checking that your Approve to that very question is in the session transcript, and consumes the token as it does
- of parallel push calls only one runs, and a spawned agent never spends it.
- The push must run from the checkout itself, be the only push in its command, and have no commit, reset or checkout beside it.
- A second push, a new commit or another branch needs a new Approve.
- Anything else — deleting a remote branch, `--prune`, `--all`, `--tags`, another branch or ref, a URL, path or variable destination, `cd`, `send-pack`, `http-push`, `subtree push` — no Approve can cover: the agent gives you the command to run yourself.
- A dry run (`--dry-run`) needs no approval.
- Force pushes stay forbidden even with a token.
- A new session or `/clear` deletes a pending token — per project, so a second session in the same project clears the first one's.
- The former `! touch /tmp/claude-push-auth-…` sentinel no longer authorizes anything.

### `gh` write token (full text)

- Foundry alone ships the git/gh guards, approval records and one-time tokens.
- GitHub reads through `gh` run without a question.
- Every GitHub write through `gh`, on any repository, asks its own question, `gh-write`: **Approve** (this exact command, once) or **Deny**.
- The question names the command once in an inline code span
- your Approve makes the `approval-guard.js` hook record a single-use token, `claude-gh-write-approval.json` in the same common directory, holding that command for 15 minutes.
- `gh-write-guard.js` then lets exactly that text run once as the whole Bash command and spends the token
- other text, a second run or a spawned agent's call stays blocked.
- A later Approve replaces the token, a later Deny removes it, and a new session or `/clear` deletes it.
- Outside a git repository nothing is recorded, so you run the command yourself.
- Claude Code still applies a settings deny rule after a hook allows a call, so the plugins no longer ship gh write deny entries
- re-run `/foundry:setup` once to remove the ones an earlier setup merged.

### Token reservation lock

- Single-use push and GitHub-write approvals use an exclusive reservation in the git common directory while validating, spending, replacing or revoking a token.
- Concurrent calls wait at most one second, then deny if it remains busy
- copies of the same tool call share its proven claim.
- The reservation is removed on normal completion.
- If a hook crashes, stop active hooks before manually deleting `<token-file>.spent-lock` from the git common directory
- Foundry also removes these reservations at SessionStart and `/clear`.

## Review loop and workflow internals

### Challenge-loop presentation

- Review loops fix feasible authorized findings, investigate repeated signatures, and continue whenever the weighted score decreases
- structural labels alone do not stop remediation.
- Completion prompts ask only for a missing decision or authorization, regardless of report length, and already-authorized next steps continue.
- Concrete gates for fresh scope, paid execution, sensitive or destructive actions, and remote changes remain in force.
- Independent challenge loops show the cumulative old/new table once after each newly validated challenge, before fixes or another review.
- Pending reviews and unrelated status updates do not repeat it
- final handoffs retain their canonical Results table.
- Each open finding needs a recorded fix, escalation, or justified deferral
- chat table timing has no machine-readable delivery receipt.

### Report templates

- The audit summary, configuration-health, investigation, and session report templates use short descriptive fields
- writing requirements sit in separate notes and are omitted from completed reports.
- Other audit modes and calibrate/brainstorm/distill templates keep their earlier placeholder style.

### Workflow audits

- Workflow audits and adversarial reviews trace producer guarantees through downstream consumers and the next ordinary user action.
- They challenge identity, ownership, destination, lifetime, and resume assumptions with concrete counterexamples and report untested handoffs.
- The shared review/fix loop inventories first-pass coverage, checks each fix against its invariant and a sibling route, and distinguishes carried defects from later discoveries.
- Out-of-scope declines preserve mandated deliverable files and caller reply formats
- `unsupported` envelopes surface a routing miss with `reason` and `owner`, never findings or a timeout.

### Loop round contract

- Each round challenges and collects findings, reports the cumulative old/new table, resolves feasible authorized findings, escalates unfixable `security`/`critical`/`high` findings, then repeats until clean or plateau/non-convergence, subject to authority, evidence, recurrence and stricter caller budgets.
- The table is `Iteration | Security | Critical | High | Medium | Low | Nits | Weighted score`
- open and fixed-pending-verification signatures split into old and new, while verified-fixed and rejected findings stay excluded.
- Security and critical have separate columns
- score weights remain `20/10/6/4/2/1`.
- Empty rounds print no progress table.
- A 1,500-output-token reviewer target is hard only when the runtime supports it
- findings are never dropped to meet it.
- Before the baseline review of a diff the parent or its resolver wrote, the author records an attestation (invariant, sibling routes, consumer, failing-before/passing-after check) and fixes the gaps it exposes
- the reviewer never sees it and it never counts toward scoring.
- Each round's cost lands in an advisory `loop-metrics.jsonl`.

### Codemap prompt reuse

- Optional Codemap guidance is shipped locally with foundry
- loading it does not read another plugin's private shared directory.
- Codemap queries still require the `codemap-py` plugin, with file-read fallback when the CLI or local contract is unavailable.
- Specialist Codemap prompts reuse only matching, successful, fresh answers, inspecting each batch child's metadata and preserving valid empty results.
- Supplied answers avoid duplicate pre-flight calls
- source and independent-review reads remain allowed.
- QA distinguishes static test/mock relationships from measured line coverage and treats missing measurements as unknown.

### Shared quality stack

- `skills/_shared/quality-stack.md` is the canonical copy of the quality-gate stack `develop`'s feature/fix/refactor skills run at close, propagated byte-identical into `develop`'s own `_shared/` via `propagate_shared.py`.
- Its flaky-test retry now re-runs only the failing node-ids instead of the whole directory twice, doctests fold into the main pytest run when a collection probe confirms it's safe (report status `not-merged`/`pass`/`fail`, replacing the old binary pass/fail line), and a `pytest-xdist` capability probe runs the wide pass in parallel when available — a failure that reproduces only under that parallel pass is reported `PARALLEL_FAIL_SERIAL_PASS` (likely a test-isolation bug) rather than marked flaky.
- `! BREAKING`: the doctest report line's format changed to the 3-state form above.

### Planning and delegation

- Small implementation uses a concise plan, brief parent self-challenge (not independent), acceptance within existing authority, useful bounded execution ownership or indivisible parent execution, and consolidation with independent result challenge and relevant verification.
- Medium/large work retains independent plan challenge before acceptance.
- Approval prompts only for actual missing authority or protected decisions
- reuse accepted parent plans and establish core acceptance before optional expansion.
- Validate a coherent bounded outcome or split it into smaller outcomes, then complete the active branch depth-first using a compact task map and context limited to the active piece, dependencies, and interfaces.
- Complete required leaf challenge and verification before dependent work
- dependency-ready siblings with agreed stable interfaces and disjoint owners may proceed.
- Unknown causes or design choices first become bounded investigation or decision prerequisites
- parent consolidation retains required gates.
- This replaces broadly open workstreams and far-ahead detail to reduce context, coordination, and rework
- that benefit is expected, not measured.

### Skill internals moved from the README

- Codemap integration metadata uses the `codemap-py.integration.v2` managed-block body while retaining the `v1` sentinel format
- use `codemap-py integrate audit` for read-only integration evidence.
- Full-sweep quality audit of the project's `.claude/` config by default, or of all `plugins/*/` agent and skill files with `--local` (plugin source checkout).
- Installed plugins in `~/.claude/plugins/cache/` are not audited
- a default run that finds no `.claude/` agents or skills reports them as `⚠ UNSCANNED` instead of a clean sweep.
- Catches broken cross-references, inventory drift, model-tier mismatches, description overlap, doc staleness.
- Reports findings by severity; fix level chosen from always-fire follow-up gate after report.
- Adversarial mode challenges every claim via `foundry:challenger` + Codex.
- `/foundry:audit`'s fix dispatch (`audit/modes/fix.md`, `audit/templates/audit-fix-prompt.md`) overrides per-call to `haiku` for `PARALLEL_SAFE_CATEGORIES` findings (pure transcription of an already-known replacement) regardless of the spawned agent's own frontmatter tier — judgment-bearing and cross-file fixes keep the agent's default.
- Confidence describes the correctness of the delivered result and how it was produced and checked within scope.
- Report completion separately; a high score does not pass an unmet gate.

### Calibration, audit and helper notes

- `propagate_shared.py` and `check_orphaned_bin.py` use ASCII status labels so success, no-op propagation, and orphan warnings work with legacy console encodings such as Windows cp1252.
- > Plugin manifests accept numeric `X.Y.Z` versions: unchanged or one patch, minor, or major bump from committed HEAD.
- Python dictionary literals bind to an enclosing class/function and a direct named assignment when present, then compare by version field name.
- A newly inserted named assignment does not shift another producer's baseline.
- Changed duplicate inline fields without stable assignment names fail closed because the checker cannot identify which producer changed.
- Computed keys, dynamic values, and reassignment through a dictionary variable are outside this static check.
- > The shared-file checker also keeps the short Codex question guide and conditional details identical across Codex Rig, Codemap and Bridge.
- Copies ship independently; installed plugins do not depend on one another.

## Rule loading internals

- `~/.claude/rules/` is one flat directory shared by every installed plugin, and four of this marketplace's plugins ship a `rules/quality-gates.md`, so source basenames would silently overwrite one another.
- Each plugin therefore namespaces its own rules with its plugin name: `quality-gates.md` installs as `foundry-quality-gates.md`, alongside `develop-quality-gates.md`, `oss-quality-gates.md`, and `research-quality-gates.md` from the sibling plugins' own setup skills.
- The prefix is inert — verified against Claude Code 2.1.220 that it changes neither unconditional rule loading nor `paths:` frontmatter matching.
- > **Stub + on-demand split (token diet):** `debugging.md`, `external-data.md`, `artifact-lifecycle.md`, `quality-gates.md`, `communication.md`, `claude-config.md`, `public-github.md`, `task-lifecycle.md`, `notebooks.md` (full body: `notebook-style.md`) = thin always-loaded stubs with hard constraints only
- full procedural bodies live in `rules/_full/` (resolved from plugin cache, NOT symlinked or injected), Read on demand at the trigger point named in each stub — drafting a commit, multi-file fix, multi-page fetch, defining new output dirs, writing a report, building an `AskUserQuestion` call, setting a Bash timeout, a GitHub write, a subagent spawn, writing a notebook cell.
- Sections that matter only while touching one kind of file sit behind a narrower `paths` glob instead (`markdown.md` for `*.md`, including the full link-verification wording and the instruction-file compression gate, `python-code.md` for pinned lint/format hooks and complexity limits), each leaving a one-line eager stub where the obligation must hold outside the trigger.
- Rules that matter only during one activity are injected rather than loaded: `hooks/rule-inject.js` adds `agent-spawn.md` (spawn slots, end-turn-after-spawn, parallel spawn ceilings) on the first `Agent()` call and `git-commit.md` on the first `git add`/`commit`/`push` of each session and subagent, again after compaction or `/clear`
- the always-loaded files keep compact stubs carrying the hard limits (`task-lifecycle.md` spawn stubs, `claude-config.md` spawn ceilings and git hard bans).
- `tests/rules/test_rule_context_budget.py` pins the always-loaded byte total.
- > Zero constraint loss: every named rule survives in the stub or its `_full/` body, verified by heading-set diff.
- > **Measured (2026-08-07)**: the always-injected preamble is ~20.5K tokens per agent and is re-sent every turn — a 56-turn agent re-sends ~1.15M tokens of it.
- The splits above cut ~2.5K of that, plus ~1.3K from `plugins/CLAUDE.md` → `plugins/AUTHORING.md`.
- **Rule savings are realized only after `make sync-claude` propagates to the plugin cache** — `~/.claude/rules/*.md` are symlinks into a version-pinned cache directory, so edits to this repo's `rules/` do not affect running agents until installed.

## Profile and context-composition internals

- `cost_analyzer.py --composition` writes a `## Context composition` Markdown section with per-window usage, injection bytes (including instructions), estimated tool-result tokens and duplicate bytes, and likely large-input triggers.
- It accepts `--projects-root`, `--session-id` (full id or prefix), `--since`, and `--output`
- main and subagent contexts are reported independently.
- Explicit compaction records define windows when present
- otherwise a post-call context drop below 70% starts a window.
- `--runtime claude` is the default and also reports repeated Read content flags, hook counts and sizes per prompt, large cache-write triggers, and the session's 5-minute/1-hour cache-write split.
- Claude sessions also show a timestamp-ordered Logged compaction events table from `~/.claude/logs/compactions.jsonl` (override with `--compactions-log`), including hook metadata and gate estimates.
- Each gate is paired with the next logged precompact event and compared with the following main-transcript window: estimated versus realised calls, and the current gate's 120,000-token floor versus realised first-call context (prompt + output).
- Missing timing or a matching window is reported as unavailable
- gates without a following precompact event say no compaction followed.
- Missing logs add a no logged compaction events note
- malformed rows warn and are skipped, and unknown events are ignored.
- `--runtime codex` reports tokens only, without prices, from `$CODEX_HOME/sessions` (default `~/.codex/sessions`)
- `--projects-root` overrides discovery.
- Codex deduplicates native `response_id` usage records or legacy sequential cumulative token-count events, reports subagent shares across linked rollout threads, flags attempted code edits, and estimates each thread's AGENTS.md bytes × calls.
- Native response records take precedence for an entire thread
- historical event-only usage in mixed-format threads is unavailable.
- Injection kinds include AGENTS.md, skills, model switches, multi-agent instructions and tagged hook developer messages.
- This analyzer ships in the Claude foundry plugin.
- Reads JSONL logs foundry `task-log.js` hook already writes for the clock side (`bin/timing_analyzer.py`) and Claude Code transcripts for the cost side (`bin/cost_analyzer.py`) — answers "where did wall clock go in `/oss:resolve`?" and "what did this session cost?" alike.
- Agents run in the background, so a spawn-only row carries a false-zero `duration_ms`
- the real span is recovered by joining the matching `started→completed` pair in `invocations.jsonl`.
- The cost side is best-effort: if no transcripts fall in the window, the `## Tokens & cost` section is omitted and the clock report still ships.

## Hook inventory

Every hook under `hooks/`, as originally documented. `hooks.json` registers them with `${CLAUDE_PLUGIN_ROOT}` paths.

### `hooks.json`

- hook registrations (${CLAUDE_PLUGIN_ROOT} paths), including PreCompact and PostCompact
- the two SessionEnd entries (task-log.js, audit-close.js) carry an explicit 15 s timeout — the cap every plugin's SessionEnd audit-close.js registration shares

### `task-log.js`

- SubagentStart/Stop tracking to /tmp/claude-state-<session>/
- PreCompact/PostCompact append metadata to ~/.claude/logs/compactions.jsonl (no instruction or summary text)
- PreCompact writes the session's edited-file list from `timings.jsonl` (Write/Edit/MultiEdit/NotebookEdit, whole session) into session-context.md and saves skill-contract.md verbatim with verified/unverified status from the writer's session + raw-byte SHA-256 owner stamp, skipping matching foreign-session stamps
- SessionEnd sweeps idle, clean, registered agent-*/oss-* worktrees (2 h) and keeps dev-\*, dirty or output-holding trees

### `statusline.js`

- status bar agent counts

### `teammate-quality.js`

- TaskCompleted/TeammateIdle teammate output quality gate

### `lint-on-save.js`

- runs pre-commit after every Write/Edit
- async + cross-session lock; 15s timeout
- skips .temp/

### `artifact-guard.js`

- PostToolUse Write/Edit on .reports/**/\*.md, .temp/**/\*.md
- soft 10K-token cap + .temp/ ultra-caveman article-density heuristic
- feedback only, never blocks

### `rtk-rewrite.js`

- transparently rewrites CLI calls for token compression

### `agent-router.js`

- PreToolUse Agent hook; 3-tier routing fallback (worktree → cache → local)

### `commit-guard.js`

- PreToolUse Bash guard; git commit is prompt-discipline only, not hook-gated
- `--check-grant` (the rule's local Git grant check, reads no stdin) prints `grant .git/claude-git-approval.json@<created_at>` for a record `lib/approval-grants.js` accepts, else `no grant: <reason>` — Foundry only, along with its grant writer
- force push (any `--force*`, `-f` cluster, `+refspec`, `--mirror`, any abbreviation git accepts such as `--mir`, and an unquoted glob argument that a planted file could turn into one) blocked unconditionally on any branch for `push`, `send-pack`, `http-push`, `subtree … push` and `lfs … push` (program name matched lower-cased)
- every `remote-<transport>` helper and every subcommand under an enabling inline `help.autocorrect` count as force
- a dry run (`--dry-run`, `-n`) passes without a token
- regular push runs only by spending the single-use push token `claude-push-approval.json` (scope `push-once`, 15 min) through `lib/approval-grants.js` `spendToken` — an exclusive reservation and a rename claim per `tool_use_id`, so of parallel calls exactly one runs, after proving the approving `git-push` question and the user's Approve in the session transcript
- a spawned agent's call or one without `tool_use_id` never spends it
- by the user's decision it covers one push of the approved branch at the approved HEAD to a configured remote name (`git push [-u] [<remote>] [HEAD|<branch>][:<branch>]`, a current-branch substitution, `lfs push`), while a delete (`:ref`, `--delete`, `-d`), `--prune`, `--all`/`--branches`, `--tags`, `--follow-tags`, any other option or abbreviation, another ref, a URL, path, unknown or variable destination, `cd`/`pushd`/`popd`, a `-C`/`--git-dir`/`--work-tree`/`GIT_DIR` switch, inline config redirecting the push, `send-pack`, `http-push` and `subtree push` are never covered (blocked, token kept, the user runs them by hand)
- a second push, a loop, function or repeating runner, HEAD-moving git beside the push (commit, reset, checkout, update-ref, a config or remote write), a new commit or another branch is blocked (no auto-arm)
- any exception on the push path exits 2
- SessionStart and `/clear` delete both tokens with their claims (per project, not per session), SessionStart also this repository's leftover `claude-push-auth-*` sentinels, which no longer authorize anything
- invocations found through grouping, prefixes, quoting, brace expansion, any-case git names, inline `-c`/`--config-env` aliases, here-strings and heredocs fed to a shell, nested shell source and `python -c`/`perl -e`/`ruby -e`/`node -e` code via `lib/shell-git.js`
- fails closed when the command cannot be parsed or the library is missing
- accepted limits (third-party `git-<name>` programs, GitHub API force updates left to `gh-write-guard.js`) listed in its header and the lib header

### `gh-write-guard.js`

- PreToolUse Bash; blocks (exit 2) every GitHub write made through `gh` the user did not approve, before any allow rule applies: known write subcommands (`pr create/merge/comment`, `issue close`, `release`/`gist` writes, `repo edit/delete/sync/rename`, `run rerun/cancel`, `workflow run/enable/disable`, `label`/`secret`/`variable` writes and the rest of gh 2.102's write set, every documented group and verb alias included) wherever flags sit or however quoted (`$'…'` escapes bash and zsh read alike decoded, quoting they read differently blocked, as is any `$` form, `{name}>` descriptor prefix or shell option switched at run time that the lexer does not model; a variable or substitution where a write verb could stand blocks), and `gh api` failing closed on any `-X`/`--method` but a literal GET (run-time values included), `--input`, a method-override or run-time header name, REST `-f`/`-F` fields without an explicit GET, a GraphQL `mutation`, a query file or a query the command text does not show, and unknown options, flags placed before the `api` word included
- reads and unknown subcommands pass to normal permission handling
- invocations found via `lib/shell-git.js`, which drops redirections while quoting is known
- fails closed without the library or an exact argv (quoting the lexer cannot follow, gh named in interpreter code)
- inactive without Node.js (launcher exits 0, SessionStart diagnostic warns)
- an exact-argv write whose whole command text, trimmed, equals a valid `gh-write-once` token's `command` runs once: `lib/approval-grants.js` `spendToken` reserves the token before renaming it to a claim keyed by `tool_use_id`, so every loaded Foundry copy allow that call and no other, and the hook prints a PreToolUse `allow`
- other text, an invalid, expired or foreign token, a spawned agent, no `tool_use_id`, no `lib/approval-grants.js` or a write found without an exact argv stays blocked and keeps the token
- ships in Foundry only, along with both libs

### `allow-dispatch.js`

- PreToolUse Bash; the only registered auto-allow hook — calls the two required decision modules plus Foundry's optional gh-read module below as libraries in rank order (provenance, shape, gh read), writes the first allow's payload, and appends one audit record naming the effective verdict plus every module's own verdict
- a module that throws contributes "none" and never suppresses the others
- exits 0 on every path, because an allow-only hook that crashes grants nothing and falls back to normal permission handling
- propagated to every plugin except codemap-py

### `sentinel-read-allow.js`

- decision module (shape lane), no longer registered as a hook of its own
- auto-allows blueprint idioms ($(cat "${TMPDIR:-/tmp}/…") sentinel reads, $(date -u +FMT) stamps, substitution-free `IFS= read -r VAR < sentinel` form) when every segment is read-only — kills "Contains expansion" prompts for pre-canned skill code
- whole-line `#` comments are skipped in segment validation (a separator after one still validates as live, and a quoted `#` stays data), and `..` rejects only as a real path component so `...`/`v1.2..v1.3` are not read as traversal
- an argument may expand only `$NAME`, `${NAME}` or `${NAME}` with a `:-`/`%`/`#` read of literal text or a nested name — a zsh sigil (`${~X}`, `$=X`), `$[…]`, an assigning operator (`:=`, `::=`), a subscript, or a `{NAME}` brace group against a redirection (which assigns NAME) falls through
- verdicts are invariant under runtime value substitution, which is what this module covers that `blueprint-allow.js` structurally cannot
- keeps its standalone entry point, which `bin/audit_hook_coverage.py` still subprocesses
- everything else falls through to normal permission checks
- propagated to every plugin except codemap-py

### `blueprint-allow.js`

- decision module (provenance lane, rank 1), no longer registered as a hook of its own
- normalizes the command, hashes it, and exact-matches it against the plugin's committed `blueprint-manifest.json` (generated by `bin/build_blueprint_manifest.py` from every verbatim bash block/command shipped in skills/agents/rules)
- a hit plus an independent danger re-check allows without a prompt, covering `$(python …)`/`$(git …)` capture-substitutions the shape-based module cannot reach
- any deviation from the blueprint text misses and falls through to a normal prompt
- propagated to every plugin except codemap-py

### `github-read-allow.js`

- decision module (gh-read lane, rank 3), not registered as a hook of its own
- auto-allows a Bash command made only of allowlisted gh reads (read verbs, help and local config reads, `gh api` GETs incl. `?query`, explicit `-X GET` fields and double-quoted endpoint variables, GraphQL queries the text shows), each optionally piped into a closed filter set (`jq`, `head`, `tail`, `wc`, `sort`, `uniq`, `grep`, `cut`, `tr`, `column`) with no file, program or environment access, in every session and spawned agent, with no grant or record
- never allows a gh write; ships in Foundry only
- the shared dispatcher skips this optional lane where its module is absent

### `audit-close.js`

- PostToolUse/PostToolUseFailure (Bash) + SessionStart/SessionEnd
- appends one record observing that a Bash call completed, and one per session boundary
- writes nothing to stdout on any event (SessionStart stdout is injected into the model's context)
- every installed plugin writes its own row with nothing coordinating them, so four rows for one completion are corroboration rather than duplicates
- propagated to every plugin except codemap-py

### `lib/audit-log.js`

- shared record writer for the two hooks above: canonical serialization (a cross-language contract with `bin/verify_blueprint_audit.py`), self-hash, and a single append into `~/.claude/logs/audit/s-<key>.jsonl`
- append-only by design — it holds no lock, marker or state directory and has no code path that deletes or rewrites a file
- never throws, so a logging failure cannot change a permission decision
- `RIG_AUDIT=0` disables writing only
- propagated to every plugin except codemap-py

### `stale-plugin-check.js`

- SessionStart (every source); when the plugin root this session loaded carries `.orphaned_at` or is not the installed path, prints one JSON object: a `systemMessage` the user sees (plugin, loaded vs installed version, `exit, then claude --resume <session_id>`) and `additionalContext` telling the model not to run that plugin's workflows on the stale copy until the user chooses, through AskUserQuestion, to restart or continue — asked once per session and plugin version: a compact/clear warning after that ask keeps the user notice but drops the ask, while a first detection at compact/clear (plugin replaced mid-session) still asks
- each copy checks only its own plugin, because plugins update independently
- silent when current or outside the plugin cache
- never blocks; propagated to every plugin except codemap-py

### `rules-check.js`

- SessionStart (every source); when setup's `foundry-<name>.md` rule links are missing (setup never ran or failed), names each rule file to read and injects the commit rule so commits keep their format and co-author trailers

### `rule-inject.js`

- PostToolUse + PostToolUseFailure `Agent|Bash`, SessionStart `compact|clear`, PreCompact
- injects `rules/agent-spawn.md` as `additionalContext` after the first `Agent()` call and `rules/git-commit.md` after the first Bash call running a commit-creating git command (via `lib/shell-git.js`), once per session and per subagent (`agent_id`)
- a call blocked by a PreToolUse hook never spends the slot
- the context arrives beside the call's result, so commit-format bullets stay always-loaded in `claude-config.md`
- reads the plugin's own `rules/` at run time
- SessionStart `compact` restores the skill contract as verified or advisory via the writer's owner stamp (matching session + raw-byte SHA-256), skips matching foreign-session stamps, and uses a Read path above the injection cap
- `clear` only resets sentinels

### `approval-guard.js`

- PostToolUse AskUserQuestion + PreToolUse Bash/Write/Edit/MultiEdit/NotebookEdit
- runs the writer and the self-grant guard of `lib/approval-grants.js`.
- Writer: the user's `Approve always` answer to the exact `git-approve` question (options `Approve` / `Approve always` / `Deny`) writes `claude-git-approval.json` (version, scope, created_at, question, answer) in the git common dir — one grant per project, shared by every worktree
- `git-push` answered `Approve` writes the single-use push token `claude-push-approval.json` (scope `push-once`, adding branch, head, expires_at) for one push of the current branch at the current HEAD (a detached HEAD records nothing), and `Deny` removes a pending token
- `gh-write` answered `Approve` writes the single-use token `claude-gh-write-approval.json` (scope `gh-write-once`, adding command and expires_at) for the one gh command the question names in an inline code span (none, two, a fenced block or a span across lines records nothing), and `Deny` removes it
- an `Approve always` description not naming that file, an answer the model pre-filled in its own call (read from the session transcript's `tool_use` block named by `tool_use_id`, tail-first, bounded to 16 MiB — never from PostToolUse `tool_input`, which the harness may fill with the user's pick), a spawned-agent question, a missing transcript, id or call, a rejected location, or a record path that is a symbolic link or not a regular file records nothing and says why
- written through an exclusive temp file and a rename, and an identical record (times aside) is left unchanged by a silent copy, so the loaded Foundry copies leave one record and report it once.
- Guard: exit 2 on file-tool writes to any approval-record name or to a path named `.git` (other paths inside `.git` pass), and on Bash commands naming a record as a write target (a redirection or output option onto it, or an operand of anything but an exact read, delete, print or mention-only git subcommand; a glob counts only in the common dir; scope ids never count), the common dir as a whole where a command writes it (copy, move, sync, extract or clone destination, any `ln` operand, a redirect onto it, an operand of a command not known to only read), a brace or glob word that could expand to a record or the git dir, a claim or temp record name through a file tool, or that could run the writer — names matched as the shell and filesystem resolve them (quotes, escapes and `$'…'` removed, NFKC + case folded, Windows trailing dot/space/`::$DATA`/8.3 names, globs and braces, `.git//` and `.git/./` collapsed)
- exclusion operands (`--exclude`, `--ignore`, find's `-path`/`-name` without a writing action, `tree -I`, `zip -x`) are not targets
- it sees names, never contents, so a patch, `git apply`, archive, script or forged payload stays prompt discipline
- ships in Foundry only

### `lib/approval-grants.js`

- approval-record scope table (`local-git-non-destructive`, `push-once` and `gh-write-once`, each written), record store, branch and HEAD reader (`gitHead`, shared by the writer and the spend), single-use spend shared by every loaded Foundry copy (`spendToken`, `removeRecord`) and guard logic behind `approval-guard.js`, read by `commit-guard.js` (`--check-grant`, push token spend, token wipe) and `gh-write-guard.js` (gh write token spend)
- one `approval-record` family at version 1, one file per scope in the git common dir, resolved with `GIT_DIR`/`GIT_COMMON_DIR`/`GIT_WORK_TREE`/`GIT_CEILING_DIRECTORIES`/`GIT_DISCOVERY_ACROSS_FILESYSTEM`/`GIT_INDEX_FILE` removed and `-c safe.bareRepository=explicit`, refusing a bare repository, a missing work tree, a common dir not named `.git` or not linked back from its work tree, and one other than `CLAUDE_PROJECT_DIR`'s
- a reader rejects an unknown record version or scope, so copies of different releases fail closed
- mirrored as prose in codex-rig's `native-skill-contract.md`
- ships in Foundry only

### `lib/shell-git.js`

- program-agnostic quote-aware shell lexer shared by `commit-guard.js`, `rule-inject.js` (git readings, `gitSubcommandArgs`) and `gh-write-guard.js` (`programInvocations`), shipped only in Foundry: finds every invocation in a Bash command through `{ }`/`if`/`time`/`sudo`/`xargs`, quoted args, brace expansion, substitutions, heredocs, here-strings and `bash -c`/`eval`/`env -S` source
- unquoted redirections are dropped with their target and fd number while quoting is known (a quoted `>` stays an argument)
- the program name ignores case and `.exe`
- inline `-c`/`--config-env` aliases expanded (run-time values read as a force push)
- an enabling inline `help.autocorrect` reads every subcommand as a force push
- an unquoted glob argument adds a reading as `--force`
- `python -c`/`perl -e`/`ruby -e`/`node -e` code naming git and push reads as a push, and code naming any other program makes `programInvocations` answer null (no exact argv, the caller fails closed)
- out-of-scope spellings listed as accepted limits in its header
- for git a coarse operator split is unioned so detection only grows
- fails closed on unparsable quoting or oversized brace expansion

### `session-restore.js`

- SessionStart matcher `clear`; reads `<cwd>/.claude/state/session/LATEST` from the hook payload's own `cwd`, and injects that handover doc as raw stdout (the documented SessionStart context channel) when it is unconsumed and under 30 min old — then marks it consumed and unlinks the pointer so a second `/clear` never re-injects
- over ~8000 chars only `## Goal` + files table + `## Next step` + a `/foundry:session recall` pointer go in
- every other path is a silent exit 0, so it can never block a session start

### `write-guard.js`

- PreToolUse Edit/Write/NotebookEdit
- guard-only inverse of the two allow hooks above — grants nothing, only forces a confirmation (`permissionDecision: "ask"`) on writes to a small repository-independent protected set: `CLAUDE.md`/`AGENTS.md`, `.claude/settings*.json`, `.pre-commit-config.yaml`, `pyproject.toml`
- a protected file git already tracks never asks, because every write to it is diffable and revertible
- untracked, new and gitignored protected files ask, and a failed git probe counts as untracked
- a protected file that does not exist yet asks once and is recorded in a per-session ledger (`write-guard-created-<session id>` in the temp dir), after which writes to it by that session pass, while a file that already existed untracked keeps asking
- source, tests, `.github/**`, `CHANGELOG.md` and dependency lockfiles are deliberately unprotected, so pairing with `acceptEdits` removes routine friction while untracked protected files stay gated
- patterns are anchored on a path-separator boundary (`docs/pyproject_notes.md` passes through), matched after POSIX normalization so Windows backslashes cannot disable it, and case-insensitive because macOS/Windows filesystems case-fold (`claude.md` writes the real `CLAUDE.md`)
- everything else is silent passthrough
- propagated to every plugin except codemap-py

### `md-compress.js`

- normalizes markdown whitespace/table-padding in place on Edit

### `batch-nudge.js`

- PostToolBatch/SubagentStart/SubagentStop/UserPromptSubmit
- counts, per agent (`agent_id`, so subagents sharing a session never sum), consecutive single-call batches of a read-only tool (Read/Grep/Glob; Bash only for single read-only commands: `ls`/`grep`/`cat`/`head`/`tail`/`wc`, read-only `git` subcommands, `find` without write actions, no redirects or chaining, only plain characters, no quotes, escapes, braces, parentheses, `$` or globs; strict by design)
- a parallel or non-batchable batch resets the streak
- SubagentStart/SubagentStop delete that agent's streak file
- at streak 4, injects a reminder as `additionalContext` (exit 0, never blocks)

### `enforce-audit-header.js`

- Blocks only audit's fix follow-up until aggregate.md and valid summary.jsonl exist and the follow-up call's first question text is a short plain summary (at most 1,500 characters) naming the `summary.jsonl` path and the Audit Report headline
- option descriptions and previews never supply it, and a reply-text copy before the call does not count.
- A valid empty summary permits a zero-finding audit.
- Recovery questions remain available.
- On Stop: keeps a turn going once per aggregate when its final reply (the text after the turn's last tool call) lacks the full Audit Report
- a shown follow-up does not satisfy it.

### `enforce-profile-header.js`

- Blocks only the owning workflow follow-up until its report exists and the follow-up call's first question text is a short plain summary (report path, `Title`, `Outcome`/`Verdict`; at most 1,500 characters, no table)
- option descriptions and previews never supply it, and a reply-text table before the call does not count.
- Diagnostic/recovery questions remain available
- unreadable delivery evidence is not success.
- On Stop: keeps a turn going once per report when its final reply (the text after the turn's last tool call) lacks the full header table
- a shown follow-up does not satisfy it.

### `report-header-table.js`

- Shared local module: matches workflow-specific questions and current report headers
- at question time checks only the call's first question text for the report path and the `Title` and `Outcome`/`Verdict` values within 1,500 characters
- at Stop only the final reply after the turn's last tool call counts for the full header table.

## Bin helper inventory

These Python helpers are installed workflow support and maintainer surfaces, not additional slash-command skills. They resolve paths, validate plugin documents, prepare reports, or perform bounded deterministic transforms; invoke them through the owning skill or with the installed plugin root.

`propagate_shared.py` and `check_orphaned_bin.py` use ASCII status labels so success, no-op propagation, and orphan warnings work with legacy console encodings such as Windows cp1252.

#### Audit, consistency, and document checks

| Helper | Purpose |
| -- | -- |
| `audit_churn.py` | Emit git-history recurrence signals for `/foundry:audit`. |
| `audit_preflight.py` | Parse `/foundry:audit` arguments and prepare its run state. |
| `audit_static.py` | Run and aggregate the deterministic Layer-1 audit checks. |
| `check_agent_waits.py` | Flag skill/agent text that polls for agents or sends task bookkeeping as its own turn. |
| `check_bash_persistence.py` | Detect shell variables referenced across separate Bash tool calls. |
| `check_bin_test_coverage.py` | Require every bin/ script to have a real test file (audit Check R4). |
| `check_cli_flag_drift.py` | Detect drift between documented flags and `argparse` options. |
| `check_codemap_guard.py` | Detect unmanaged codemap index-guard copies. |
| `check_agent_plugin_root.py` | Flag agent Markdown that writes `${CLAUDE_PLUGIN_ROOT:-…}` (audit Layer-1 `agent-plugin-root`; exit 1 on a finding, `plugin-root-check: allow` exempts a line). |
| `check_bridge.py` | Detect whether the bridge plugin is installed and enabled (marketplace allowlist). |
| `check_fence_symmetry.py` | Validate Markdown code-fence pairing and nesting. |
| `check_gitignored_refs.py` | Reject tracked-file references to this repo's gitignored documents. |
| `check_install_state.py` | Validate post-install state under `~/.claude` for `/foundry:audit`, including native Windows symlink targets. |
| `check_mode_dispatch.py` | Detect dangling mode-dispatch references in skill files. |
| `check_orphaned_bin.py` | Find bin scripts not referenced by plugin Markdown. |
| `check_plugin_layout.py` | Validate a plugin's on-disk layout for `/foundry:audit` Check 8. |
| `check_plugin_module_docs.py` | Require a module docstring on every shipped plugin module. |
| `check_plugin_version_sync.py` | Require matching dual-host manifests, retain tracked host manifests in shipped plugins, and check changed shipped JSON version, schema, and protocol fields plus matching Python dictionary literals and module-level `*_VERSION`, `_SCHEMA`, and `PROTOCOL` integers against committed HEAD. |
| `check_readme_drift.py` | Detect README version and bin-reference drift from disk. |
| `check_routing_links.py` | Validate computed paths in skills and agent files. |
| `check_rtk_alignment.py` | Compare the RTK rewrite hook's prefix list against the installed RTK. |
| `check_skill_contracts.py` | Run structural contract checks over skill Markdown for `/foundry:audit`. |
| `check_spawn_prompt_vars.py` | Detect unresolved variables in Markdown spawn prompts. |
| `check_tag_symmetry.py` | Check XML-tag symmetry in agent and skill Markdown. |
| `classify_resolver_sites.py` | Classify resolver call sites as inline-required or cat-only. |
| `consolidate_inventory.py` | For `/foundry:audit --consolidate`: inventory instruction sections with load group and merge-blocking dependencies, select the groups to examine, vet proposed merge clusters, and settle judge decisions and reviews. |
| `scan_efficiency_signals.py` | Emit deterministic cost signals for `/foundry:audit --efficiency`. |
| `efficiency_measured.py` | Measure model-switch rewrites, cold start, pre-gate cost, subagent growth and idle-gap cache losses from transcripts; keeps a baseline ledger for `/foundry:audit --efficiency`. |
| `size_importance.py` | Judge instruction blocks spine or complement and flag only complement text that outweighs its load class, for `/foundry:audit --efficiency` (E11). |

> Plugin manifests accept numeric `X.Y.Z` versions: unchanged or one patch, minor, or major bump from committed HEAD.
>
> Python dictionary literals bind to an enclosing class/function and a direct named assignment when present, then compare by version field name.
>
> A newly inserted named assignment does not shift another producer's baseline.
>
> Changed duplicate inline fields without stable assignment names fail closed because the checker cannot identify which producer changed.
>
> Computed keys, dynamic values, and reassignment through a dictionary variable are outside this static check.

#### Session, install, and state helpers

| Helper | Purpose |
| -- | -- |
| `find-polluter.py` | Binary-search a test suite for test-isolation contamination. |
| `agent_watch.py` | Report each spawned agent's deliverable and deadline in one call — no polling. |
| `append_ledger.py` | Append staged `.rec` records (or `--from` files) to a growing ledger without rewriting it. |
| `adversarial_loop.py` | Record and validate review → fix loop rounds: append a round (and, with `--append-metrics`, its advisory cost record), print the progress table, require root cause on a repeated finding and stop at its third occurrence. |
| `compact_gate.py` | Decide if boundary compaction pays: `continue`, non-blocking `advise`, or opt-in `stop`. |
| `compaction_boundary.py` | Print a skill's compaction Boundary step, then run the optional foundry `compact_gate.py`; `continue` when the provider is missing. Propagated to oss and develop. |
| `get_plugin_install_path.py` | Resolve the active plugin path from Claude's registry. |
| `heal_git_artifacts.py` | Reclaim stale skill locks and orphaned git worktrees; `locks --pattern` must be a bare file-name glob, and a worktree whose status cannot be read is never removable; unregistered dirs are removed only with `--include-orphans` after a fresh re-check, symlinks are never followed, and `--managed-prefix` rejects `*`. |
| `jq_write.py` | Apply an atomic `jq` JSON edit (`--arg`, `--slurpfile`); cwd, TMPDIR, `~/.claude/*.json`; an unlisted flag exits 3. |
| `load_mode.py` | Resolve a skill mode/template directory and emit one file. |
| `load_shared_doc.py` | Resolve the plugin shared-doc directory and emit one file; `--optional` turns a missing plugin, directory or file into one `⚠ WARNING` line with exit 0 (warn-and-skip, used by the Codex pre-pass and audit cross-validation loads). |
| `make_run_dir.py` | Create a UTC-timestamped run directory. |
| `merge_settings_permissions.py` | Merge shipped `permissions-{allow,deny}.json` into `~/.claude/settings.json` atomically. |
| `settings_json.py` | Python-only setup JSON probes and atomic writes for hooks, statusLine, enabledPlugins, env defaults and advisorModel. |
| `plugin_layout.py` | Print each plugin's manifest-declared skills and agents directories (`PLUGIN_LAYOUT:` lines) so `--local` audit globs honour renamed directories. |
| `purge_plugin_cache.py` | Remove orphaned versions from the plugin cache. |
| `remove_hook_from_registry.py` | Remove matching hook commands from a JSON registry. |
| `resolve_agent_file.py` | Locate the source file for a calibration target. |
| `resolve_memory_dir.py` | Resolve the project-scoped Claude memory directory. |
| `resolve_plugin_root.py` | Resolve and validate the current installed plugin root. |
| `resolve_shared_path.py` | Resolve a plugin's shared directory portably (own, or the active `codemap-py` install); the cwd source-tree fallback is used only inside that plugin's own source repository, elsewhere it exits 1. |
| `resolve_skill_subdir.py` | Resolve a skill `modes/` or `templates/` subdirectory; the project-local `.claude/skills` tier applies only with `--local`. |
| `state.py` | Persist small shell values across Bash tool-call boundaries. |
| `symlink_with_guard.py` | Scan and safely manage foundry-init symlink conflicts. |
| `write_skill_contract.py` | Write contract + owner stamp; leading `--keep-sentinel <name>` (flat session sentinel) or `--keep-file <path>` (file in a per-session state dir) appends user-keep; optional `-- calls_ahead=N drop=... interactive=false` tail. |

#### Reporting, synchronization, and transforms

> The shared-file checker also keeps the short Codex question guide and conditional details identical across Codex Rig, Codemap and Bridge. Copies ship independently; installed plugins do not depend on one another.

| Helper | Purpose |
| -- | -- |
| `audit_hook_coverage.py` | Measure how often the auto-allow hooks fire across session transcripts. |
| `build_blueprint_manifest.py` | Hash shipped bash blueprints into the per-plugin allow manifest. |
| `codemap_changed_modules.py` | List the codemap module names behind the Python files changed since the branch point (`--base REF`, `--cap N`); feeds the agents' per-module query loops. |
| `cost_analyzer.py` | Bucket Claude session token usage and estimated cost. |
| `extract-keep-flag.py` | Parse `--keep "<items>"` and clear a stale compaction contract (`--inlined`; sentinel writes atomic, a contract owned by another session is left alone). |
| `extract_code_blocks.py` | Extract and classify fenced Markdown code blocks. |
| `list_audit_files.py` | Inventory the config files `/foundry:audit` covers, with block counts; `--entrypoints --paths-only -0` emits NUL-terminated agent and `SKILL.md` paths for `xargs -0` in either tree. |
| `measure_config_size.py` | Size the config Claude loads, for `/foundry:audit`. |
| `parse-skill-flags.py` | Parse boolean and value skill flags into shell assignments; `--keep-state <prefix>` also writes per-session sentinels and clears a stale contract, replacing `extract-keep-flag.py` + `eval`. |
| `propagate_shared.py` | Keep byte-identical cross-plugin shared files synchronized. |
| `resolve_centrality.py` | Convert codemap centrality output into a resolver map. |
| `timing_analyzer.py` | Bucket Claude session clock time into a Markdown report. |
| `verify_blueprint_audit.py` | Verify and prune the auto-allow audit log in `~/.claude/logs/audit/`. |
| `verify_perm.py` | Verify permission presence across settings and the permissions guide. |

## Test layout

One runner (pytest) covers both Python `bin/` scripts and JS hooks. Current test count: `grep -rc 'def test_' plugins/cc_foundry/tests/*.py | awk -F: '{s+=$2} END{print s}'`.

Apply pytest markers and conditional skips directly to test functions or existing test classes, as described in the [bin authoring guide](skills/_shared/bin-authoring-guide.md#bash-script-testing).

**Run locally** (requires Python ≥ 3.10 and Node ≥ 18 — run from repo root):

```bash
pip install pytest pytest-cov
pytest plugins/cc_foundry/tests/ -v
```

JS hook tests only:

```bash
pytest plugins/cc_foundry/tests/ -v -k "js"
```

### JS hook tests (subprocess, black-box)

Each test spawns `node <hook>.js` with JSON payload on stdin, asserts filesystem side-effects under `/tmp/claude-state-<session_id>/` + exit codes.

| File | Hook | Tests | Covered |
| -- | -- | -- | -- |
| `test_task_log_js.py` | `task-log.js` | 35 | Agent lifecycle (Pre/PostToolUse, SubagentStart/Stop), tool_use_id→agent_id re-key on SubagentStart (direct + agent_type-fallback match, including named agents matched by name) so SubagentStop's unlink actually lands, background agents kept past dispatch-time PostToolUse, pending/ race condition, worktree `last_active` refresh on tool activity, codex skill tracking, per-tool counters (`last` stamp per call, `inflight` opened at PreToolUse and released on PostToolUse/Failure, floored at 0 on duplicate fire), SessionEnd stale-worktree sweep keeps `dev-*`, uncommitted and fresh trees |
| `test_stale_plugin_check_js.py` | `stale-plugin-check.js` | 77 | Runs every shipped copy (foundry, oss, develop, research) under its own plugin name: silent for the installed root, a source-tree root, a missing registry, or another event; for an orphaned, replaced or uninstalled root prints exactly one JSON object whose `systemMessage` names the plugin, both versions and the `claude --resume <session_id>` step (a plain restart when the payload has no session id) and whose `additionalContext` requires an AskUserQuestion before that plugin's workflows run — dropped on a compact/clear after this session already got it, kept on first detection at compact/clear, on resume, and without a session id; each plugin registers its own copy once on SessionStart, and every copy is in the propagation manifest |
| `test_rules_check_js.py` | `rules-check.js` | 4 | Blank config names every rule and injects the commit rule without frontmatter; silent when all rules are linked or on another event; a partial install names only the missing rule |
| `test_rule_inject_js.py` | `rule-inject.js` | 80 | Agent injects the rule body once (exact PostToolUse/PostToolUseFailure `additionalContext` shape, no frontmatter), second Agent silent, each `agent_id` and each session injects once; git add/commit/push in any segment or spelling inject (`x && git commit`, `git -C dir push`, `env VAR=1 git push`, heredoc message), `git status`/`echo git commit-ish`/`git commit-tree`/`git stash push` silent; compact/clear reset sentinels; compact restores contracts, clear silent; startup leaves them; a rule over 9,500 chars injects summary + rule and `_full` paths; malformed/empty/foreign payloads, missing rule file and unwritable temp dir exit 0 silently; both shipped rules inject in full under the cap; grouped/wrapped/nested, `GIT`, inline-alias, `bash <<<`, inline `help.autocorrect` and redirection-before-subcommand (`git 2>/dev/null commit`) forms and merge/revert/cherry-pick/am/rebase/annotated tag inject; PreToolUse claims nothing; a commit-guard-blocked push leaves the slot; subagent PreCompact re-arms only that subagent; missing lib fails open; registration matchers |
| `test_statusline_js.py` | `statusline.js` | 23 | `🤖` segment: empty/active/stale agents, non-worktree agent stays visible past 10 min (30-min backstop) and drops past 30 min, long-running agent kept visible by fresh `last_active`, codex agent type label (via `agents/` and the `codex/` dir), missing-`agents/`-dir tolerance. `🛠️` segment: freshness from `last` (a call 5 s into a 40 s-old window stays shown), in-flight call visible past the 30 s window, finished call and leaked in-flight slot dropped, legacy `since`-only record still renders |
| `test_commit_guard_js.py` | `commit-guard.js` | 403 | Push guard: every remote write needs a single-use `push-once` token covering one push of the approved branch at the approved HEAD to a configured remote, remote and branch written out (delete, prune, tags, other ref, URL, variable target, `cd`, inline push redirects blocked); spent once by rename-claim with transcript proof, never by a spawned agent; a dry run spends nothing. Force and mirror pushes blocked even with a token. Shell readings: heredoc data vs code, substitutions, zsh-only syntax failing closed with the syntax named. Local Git (staging, merge, cherry-pick, branch/tag delete, reset, stash, worktree) passes. `--check-grant`, SessionStart and `/clear` token wipe, fail closed on internal errors. |
| `test_approval_guard_js.py` | `approval-guard.js` | 411 | Writer: records only from the user's own answer to the exact question (`git-approve` `Approve always` with the record file disclosed; `git-push` and `gh-write` `Approve` with question id, branch and HEAD or the exact command and file digests); refuses spawned agents, prefilled answers, drifted option sets, hidden characters and compound gh spans; `Deny` revokes pending tokens; only the writing copy reports. Two and four Foundry installs in temporary roots race on the same answer and tool-call id, leaving one record and spending one token. Self-grant guard: blocks Bash and file-tool writes of record, claim and temp names (brace, glob and symlink spellings), writes onto the git dir as a whole, running the writer, and named writes to a session transcript; reads, revoke, memory files and local Git reads of `.git` pass. |
| `test_gh_write_guard_js.py` | `gh-write-guard.js` | 447 | Blocks every gh write without a `gh-write-once` token for the exact command; the token runs once across all loaded Foundry copies (rename-claim), only with its transcript proof, unchanged file operands and a single gh invocation. Force updates (`repo sync --force`, `pr update-branch --rebase`, force fields including nested GraphQL keys, `--input` to refs or GraphQL) blocked even with a token. Reads pass; wrappers, substitutions, heredocs and zsh-only syntax (named) are covered. |
| `test_agent_router_js.py` | `agent-router.js` | 12 | Builtin/plugin-agent passthrough (tier 1), unknown-agent reroute to `general-purpose` (tier 3), SessionStart index build, tier-2B LLM request (resolved model ID, thinking disabled) and response parse (typed text block; error/non-2xx/no-text raise, never a silent "none"), runtime Haiku resolution (env override, 24h cache, Models API pick by `line`, logged fallback, session-scoped cache path) |
| `test_batch_nudge_js.py` | `batch-nudge.js` | 66 | never blocks (exit 0, no `decision`/`continue` key) at any streak; no nudge below threshold; nudge (`additionalContext`, cites §Turn Batching) fires exactly on the batch crossing threshold and the streak restarts after it; parallel or mixed batch resets streak; non-batchable tool (Edit) resets streak; UserPromptSubmit resets the main agent's streak, not a subagent's; interleaved agents keep separate streaks and one agent's parallel batch never resets another's; read-only Bash (`git log`/`status`/`show`, `find -name`, `ls`/`grep`/`tail`/`wc`, the plain characters `=:@%,+`) counted, mutating git subcommands, `--output`, redirects, newline/substitution/compound commands and `find -delete`/`-exec` are not; Pre/PostToolUse events ignored; malformed input exits 0 silently; SubagentStart/SubagentStop reset and delete only that agent's file, never main's; commands with quotes, escapes, braces, parentheses, `$IFS`, globs or zsh `=(...)` not counted; subagent events with no, empty or `main` agent_id keep main's streak; failed state write leaves no temp file; hooks.json registers the hook on PostToolBatch, SubagentStart, SubagentStop and UserPromptSubmit only; cited rule heading exists |
| `test_sentinel_read_allow_js.py` | `sentinel-read-allow.js` | 146 | Blueprint sentinel reads + date stamps + substitution-free `IFS= read -r` form allowed (observed prompt commands, unquoted variant, defaults, pipes into whitelisted tokens); passthrough on non-whitelisted tokens, non-TMPDIR paths, nested/second substitutions, backticks, process subst, heredocs, write redirects, quote smuggling, Codex bypass PoCs, any `$` form outside the admit set (zsh sigils, `$[…]`, assigning operators, subscripts) and `{NAME}` redirection prefixes; whole-line comments skipped without letting a separator hide a live command, and a quoted `#` stays data; `..` matched as a path component so `...`/`v1.2..v1.3`/`file..txt` allow while leading/mid/trailing/bare/`${V:-..}` traversal rejects; verdict invariant across runtime value substitution (sentinel basename, var name, CSID form, target filename, subdir path) but flips on injection through the varied slot; no `updatedInput` emitted; non-Bash + malformed JSON hygiene; requiring the module attaches no stdin listener and does not exit, and `evaluate` labels allow / passthrough(`shape-mismatch`) / none(`not-applicable`) without changing the payload `decide` produces |
| `test_blueprint_allow_js.py` | `blueprint-allow.js` | 119 | Verbatim block/command/trailing-comment/trailing-whitespace/CRLF variants allowed with `src` provenance, no `updatedInput`; one-char deviations and recombined halves of two manifest entries passthrough; missing manifest, malformed JSON, wrong-typed entries, non-Bash tool, malformed stdin fail open; independent danger re-check refuses even a tampered danger-token entry the generator would never emit; normalization vectors shared with the generator via `blueprint_normalization_vectors.json` (digests agree across JS/Python); `evaluate` labels each decline by the check that made it (`danger`, `manifest-unavailable`, `bailout`, `no-match`) and carries a digest exactly when the command was normalized |
| `test_write_guard_js.py` | `write-guard.js` | 47 | Every protected class asks with its labelled reason (agent instructions, permission config, lint gate, package metadata); in a git repository a tracked protected file (relative or absolute path), permission config and the commit gate included, passes through while untracked, new and gitignored ones ask; a missing protected file asks on creation and passes afterwards in the same session (a denied creation, a pre-existing untracked file, another session and a dangling symlink still ask); `.github/**` scripts and workflows, `CHANGELOG.md` and lockfiles never ask, tracked or not; absolute paths, Windows backslash separators and case variants (`Claude.md`, `PYPROJECT.TOML` — case-folding filesystems) match; `Write` and `NotebookEdit` (via `notebook_path`) guarded alongside `Edit`; routine source/test edits and anchored near-misses (`src/my_changelog_helper.py`, `src/github/client.py`) pass through; unguarded tools, missing path and malformed stdin fail open; the only `permissionDecision` in the source is `ask`, and no `updatedInput` is emitted |
| `test_enforce_audit_header_js.py` | `enforce-audit-header.js` | — | Current aggregate summary in the first question's text (summary path and Audit Report headline; option fields and reply text alone denied), 1,500-character cap, a shown follow-up never settling final-reply delivery, zero findings, missing/unreadable evidence, recovery questions, and sentinel lifecycle. |
| `test_enforce_profile_header_js.py` | `enforce-profile-header.js` | — | Current report summary in the first question's text (report path, Title, Outcome; option fields and reply text alone denied), 1,500-character cap, a shown follow-up never settling final-reply delivery, recovery boundary, state-file parsing, and sentinel lifecycle. |
| `test_report_header_table_js.py` | `report-header-table.js` | — | Question-time summary (report path, Title, Outcome or Verdict in the first question's text; later questions, option descriptions, previews, tables and HTML denied; 1,500-character cap; optional previews held to the host preview cap), Stop judged on the final reply only and never settled by a shown follow-up, once-per-report continuation, inline-markdown value cells and workflow recognition. |

`run_hook` and `state_dir` = pytest fixtures in `conftest.py`; test methods receive as parameters — no `from conftest import` needed.

**Why Python tests for JS hooks?** Black-box contract tests: each spawns real `node` process, sends JSON payload on stdin, asserts filesystem side-effects + exit codes. Python = orchestrator only — JS runs for real. One runner (pytest) covers all plugin tests (Python `bin/` scripts + JS hooks), no `package.json` or `node_modules` required. Tradeoff: covers hook *interface* contract, not individual JS function internals. Jest = right addition if internal logic ever warrants unit testing.

### Python `bin/` script tests

| File | Script | Tests | Covered |
| -- | -- | -- | -- |
| `test_check_routing_links.py` | `check_routing_links.py` | 42 | Computed path resolution, orphan-risk detection (R2), bin-ref integrity (R3), security path guard |
| `test_check_codemap_guard.py` | `check_codemap_guard.py` | 44 | MANIFEST-managed (read with `ast.literal_eval`) vs registry-declared vs provider-CLI classification, registry rot detection, git-root anchoring and raw-project-name invariants |
| `test_build_blueprint_manifest.py` | `build_blueprint_manifest.py` | 37 | Normalization (CRLF, quote-aware comment strip, blank-run collapse, spacing variants never collide), logical-command splitting (never on `;`/`&&`, continuation-safe, escaped-backslash-aware), heredoc/quote-span bail-out to whole-block-only, danger-token detection (deferred + block-level, dropped count reported), block+command dual hashing with `src` at the opening fence, cross-OS path posix-ification (no backslash ever in emitted JSON), `--update`/`--check` drift + determinism, byte-identical two-run reproducibility |
| `test_symlink_with_guard.py` | `symlink_with_guard.py` | 66 | Create/update/remove symlinks, guard against stale links, unconditional purge of `~/.claude/skills/` links |
| `test_extract_code_blocks.py` | `extract_code_blocks.py` | 30 | Fence parsing, lang normalisation, heuristic code/prose classification, token filtering |
| `test_check_agent_waits.py` | `check_agent_waits.py` | 42 | Waiting-tool, fixed-poll and bookkeeping-turn detection vs prohibitions; repo-wide zero-finding gate over every plugin; foundry spawn sites use the shared deadline protocol; quality-stack nested `index` freshness; task-only blocks flagged when they set `in_progress` or never say they ride with real work (reflow-safe); task-driving skills must state the ride rule |
| `test_check_bash_persistence.py` | `check_bash_persistence.py` | 75 | Cross-block variable reference detection, env-var filtering, multi-block files, `modes/` scan, explicit `--scan-dir` with no match |
| `test_find_polluter.py` | `find_polluter.py` | 64 | Safe/unsafe node-id validation, isolation test runner, bisect loop (timeout exits 1), path-traversal + report guidance |
| `test_check_cli_flag_drift.py` | `check_cli_flag_drift.py` | 35 | AST flag extraction, invocation-scoped matching, docstring `Usage:`-block scan vs own argparse surface, REMAINDER passthrough, no-exec guarantee |
| `test_audit_log_js.py` | `hooks/lib/audit-log.js` | 62 | Canonical serialization and record hash matched against committed cross-language vectors (key order, explicit null versus absence, non-ASCII and above-BMP characters, integers only, ill-formed strings rejected); filesystem keys hashed so `a/b` and `a?b` cannot merge and lone-surrogate ids are rejected before hashing; directory 0700 and file 0600; append never rewrites earlier bytes; disabled, unwritable, symlinked and unserializable cases return `_unwritten` rather than throwing; `_no-session` soft cutoff refuses and leaves the file byte-identical; identity read from `plugin.json`, not the version-named directory; the export surface contains no file-removing API |
| `test_shell_git_js.py` | `hooks/lib/shell-git.js` | 259 | Shell lexer shared by the guards: simple commands across quoting, substitutions (`$(…)`, backticks, process substitution), heredocs (quoted bodies as data to data commands, bodies fed to a shell as code, a heredoc inside `$(` that bash 3.2 could close early failing closed), wrappers (`env`, `xargs`, `sudo`, `find -exec`), zsh-only and bash/zsh-divergent syntax marked unreadable with a named reason, and the coarse fallback pass. |
| `test_hook_stdout_captures.py` | both decision modules + `allow-dispatch.js` | 37 | Replays `fixtures/hook_stdout_captures.json`, the raw stdout bytes both modules wrote before the refactor, across the whole command corpus and six manifest scenarios: each module's standalone driver is byte-unchanged, and the dispatcher writes \`blueprint |
| `test_allow_dispatch_js.py` | `allow-dispatch.js` | 30 | One row per call carrying the effective verdict plus every lane's verdict; blueprint outranks shape when both allow; a gh read is allowed at rank 3 with no record, and blueprint outranks it; real dispatcher rows verify with three lanes, or two when the optional gh-read module is absent; whitespace splits the lanes (passthrough versus none); both-none writes no row; a module that throws or fails to load leaves the other deciding and is recorded `module-error`; a stalled shape lane does not delay an already-written blueprint allow; `RIG_AUDIT=0` never loads the library, and a broken library leaves stdout and exit code untouched; raw command text never reaches the log and nothing outside the audit directory is created; `integration`-marked concurrent writers all verify their own hashes |
| `test_github_read_allow_js.py` | `github-read-allow.js` | 429 | Allowlisted gh reads (read verbs with any option, help and local config reads, `gh api` GETs incl. `?query`, explicit `-X GET` fields and double-quoted endpoint variables after a literal `/`, GraphQL queries the text shows), each optionally piped into the closed filter set, are allowed in a repository, outside one and in a spawned agent with no record, and gh-write-guard passes every one; filters reading or writing a file, running a program or reading the environment, run-time text outside the endpoint, GET fields that may reach GraphQL, `config get oauth_token`, extension help, gh writes and unreadable quoting are not; without the lexer nothing is allowed |
| `test_heredoc_in_substitution_js.py` | `lib/shell-git.js` | 9 | A heredoc inside `$(…)` whose body closes the substitution early in bash 3.2 is unreadable syntax (fail closed); the `git commit -m "$(cat <<'EOF' … EOF )"` idiom with balanced parentheses or an apostrophe stays readable. |
| `test_audit_close_js.py` | `audit-close.js` | 32 | Success and failure rows with the same status words `timings.jsonl` uses; both events write both rows; authority is never inferred from execution; non-Bash and unhandled events write nothing; lifecycle rows are skipped without a usable session id while completion rows still land in the shared stream; four simulated plugins produce four retained rows naming their own versions; stdout empty on every event; nothing created outside the log |
| `test_verify_blueprint_audit.py` | `bin/verify_blueprint_audit.py` | 107 | Table-driven against `fixtures/audit/` — a corpus and its expectations built before the verifier existed. Exit 1 only on a failed record hash; torn framing and every classification stay warnings. Covers the classifications that were wrong in earlier designs: a healthy four-plugin file is clean, four closers are corroboration while a missing one is `incomplete-closure`, a four-plugin shape allow is clean apart from `multi-allow`, abstention requires a real passthrough, and `none`/`module-error` are counted apart from it. Plus the reader's own robustness: a stray directory or dead symlink in the log dir must not raise, since an uncaught raise exits 1 and that code means `corrupt-record`. Plus acceptance of two or three distinct known verdict lanes and rejection of missing, extra, duplicate or unknown lanes and values this system never writes, both limits, the JSON output shape and totals, and prune's dry run, age cutoff, `--keep-session` and refusal to prune `_no-session.jsonl` |
| `test_audit_registration.py` | hooks.json + propagation + docs | 38 | Every plugin registers the dispatcher, registers neither decision module directly, and registers the closer on all four events with the right matcher; the four new files are in the propagation manifest with all three copies and the check-only gate passes; the verifier is named in all four READMEs (a gap `check_orphaned_bin.py` does not cover) and no document still claims a registration order; the shape module still runs as a subprocess for `audit_hook_coverage.py`; a `claude-state-<session>` teardown cannot touch the audit log |
| `test_audit_hook_coverage.py` | `audit_hook_coverage.py` | 22 | Manifest union across plugins (single-plugin probe under-reports), newest-version selection by numeric key at any nesting depth, mechanism attribution with blueprint winning over shape, memoization, shape-hook argv/payload dispatch, transcript parsing (Bash-only, malformed-line and vanished-file tolerance), `--since`/`--project`/`--skills-only` filters |
| `test_verify_perm.py` | `verify_perm.py` | 21 | Settings allow-entry detection, missing/malformed JSON, CLI exit codes |
| `test_check_orphaned_bin.py` | `check_orphaned_bin.py` | 21 | Orphaned bin/ script detection, consumer-reference parsing, multi-plugin scan |
| `test_cost_analyzer.py` | `cost_analyzer.py` | 30 | Message-id dedupe (3x-inflation guard), per-model pricing (future id of a known family at that family's newest price with a stderr warning, no-family id and retired id without its own row at per-field max, cache write/read ratio per model, 1h vs 5m write split, Haiku 5.5 prompt-size band), main/sidechain bucketing with per-call cost, subagent-transcript merge, session discovery no-double-count, CLI session-id + window modes |
| `test_efficiency_measured.py` | `efficiency_measured.py` | 113 | Content-block row merge (tool names survive, Bash reduced to first real word), per-skill command spans, model-switch and idle-gap thresholds (growth and compaction excluded), literal-CLI prefix, exact pre-gate cost, pinned baseline and delta, label normalisation, project scoping, unreadable-transcript skipping, CLI exit codes |
| `test_jq_write.py` | `jq_write.py` | 50 | Arg parsing (unlisted flag exits 3), JSON path writes, merge semantics, a private temp file per call (a foreign `<target>.tmp` untouched), file mode kept, symlinked `settings.json` written through, `$HOME` before `Path.home()` |
| `test_resolve_shared_path.py` | `resolve_shared_path.py` | 37 | Plugin/subdir validation, path traversal rejection, tier-1/2/3 resolution cascade, registry record beating a newer cache dir, un-prefixed provider source fallback, tier 3 refused outside the plugin's own source repository |
| `test_heal_git_artifacts.py` | `heal_git_artifacts.py` | 83 | PID liveness on both platform branches (Windows simulated on every host, POSIX probe patched so signal 0 is asserted rather than sent), lock verdicts, worktree tiering, the `--include-orphans` opt-in and pre-removal orphan re-check, symlink reporting, the rejected `*` prefix, CLI exit codes, and a guard that no doctest calls the POSIX probe — signal 0 is `CTRL_C_EVENT` on Windows, so one did abort every Windows CI leg with a `KeyboardInterrupt` |
| `test_check_fence_symmetry.py` | `check_fence_symmetry.py` | 17 | Unclosed fences, nested/interleaved fences, multi-file scan |
| `test_check_gitignored_refs.py` | `check_gitignored_refs.py` | 24 | Token extraction, existence+check-ignore classification in a real tmp checkout, waiver marker, placeholder templates, CLI exit codes |
| `test_check_bridge.py` | `check_bridge.py` | 17 | nested registry parsing, `installPath` requirement, stale-cache rejection, settings opt-out, marketplace allowlist |
| `test_make_run_dir.py` | `make_run_dir.py` | 17 | Portability invariants (no `/tmp` literals, no `utcnow`), timestamp format, links into instruction sources |
| `test_check_spawn_prompt_vars.py` | `check_spawn_prompt_vars.py` | 16 | `$VAR` in markdown code blocks, caller-substituted-var whitelist, multi-file scan |
| `test_check_tag_symmetry.py` | `check_tag_symmetry.py` | 14 | Empty/whitespace XML blocks, unbalanced open/close tags, multi-file scan |
| `test_resolve_skill_subdir.py` | `resolve_skill_subdir.py` | 16 | Tier-1/2/3 subdir resolution cascade, `--local`-only project tier (`.claude/skills`), fallback ordering |
| `test_purge_plugin_cache.py` | `purge_plugin_cache.py` | 15 | Report-vs-apply contract, seven deletion guards, `--expect-count` abort, argument rejection |
| `test_audit_static.py` | `audit_static.py` | 14 | Layer-1 checker aggregation: seeded-defect detection, clean-scope pass, option-shaped file names passed after `--`, JSONL output |
| `test_state.py` | `state.py` | 28 | Set/load round trip, unsafe and reserved key rejection, line-break value rejection, session-scoped namespaces, clear |
| `test_resolve_memory_dir.py` | `resolve_memory_dir.py` | 30 | Claude Code directory-name slugs (case kept, one hyphen per code unit, Windows/Unicode, 200-char hash cut), git fallback, missing-git fallback |
| `test_resolve_plugin_root.py` | `resolve_plugin_root.py` | 9 | Registry lookup, cache-scan fallback, orphaned-version skip, security gates (cache-dir + manifest-name) |
| `test_get_plugin_install_path.py` | `get_plugin_install_path.py` | 18 | Registry lookup, multiple-entry tie-breaking, missing plugin exit code, stale loaded-root warning (orphaned or not the installed path) |
| `test_classify_resolver_sites.py` | `classify_resolver_sites.py` | 5 | Directory scan for extractable/non-extractable resolver sites, CLI report + `--list-extractable` — maintainer-only, ad hoc invocation, not wired into any skill |
| `test_c33_dir_resolution.py` | _(C33 dir-resolution logic)_ | 4 | Latest-version selection, older-version exclusion, no-cache fallback |
| `test_consolidate_inventory.py` | `consolidate_inventory.py` | 671 | Section splitting (nested fences, frontmatter), `paths:` parsing, load classes incl. match-everything globs, stub/`_full` pairing, install-alternative and `CLAUDE.src.md` groups, shared-file, policy-sibling and frontmatter blocking, inbound citations, merge-scope groups (cross-plugin, per-skill lazy, agents), cluster guard (single-file, degenerate, duplicate, overlapping, unsafe id), group selection, settle rules and digest-bound review verdicts, cited deep headings, shared-file digest, strict `paths:` reading, one CommonMark block reader (containers, lazy lines, agreement with markdown-it) and edit simulation, inline-markup titles, flow-list comments, stale inventory and re-derived reports, malformed inventory and report files, read-only guarantee, real-tree controls |
| `test_audit_preflight.py` | `audit_preflight.py` | 52 | Bare-scope/challenge-alias parsing, `--keep` flag extraction, tool-presence marker caching (fresh/expired/corrupt), session-scoped state files, mutually-exclusive-flag and missing-dir exits, template resolution, links at state children |
| `test_check_bin_test_coverage.py` | `check_bin_test_coverage.py` | 17 | Real-function counting vs stub bodies (pass/ellipsis), async and nested-class tests counted, non-test functions ignored, syntax-error files treated as covered, missing/empty/stub-only test files flagged, private scripts skipped, `--local` clean/dirty tree gating |
| `test_check_install_state.py` | `check_install_state.py` | 20 | Nested/flat registry shape resolution, missing registry/foundry/cache-dir severity, settings merge completeness, statusline and allow-list checks, bridge enablement, stale-hook and rule-link staleness, native Windows and POSIX symlink-target checks, `--check` dispatch |
| `test_check_plugin_layout.py` | `check_plugin_layout.py` | 26 | Manifest presence/validity/required-fields/name checks, real dir/file existence severities, reference resolution, hooks.json presence and validity, hooks-registry sync across plugin/settings, setup-skill keyword coverage, absent-CLI and `claude plugin validate` timeout degrading to a skip so 8f/8g still run, absent-plugin-dir and broken-plugin runs both reporting on stdout at exit 0, plugin-root reference extraction |
| `test_check_rtk_alignment.py` | `check_rtk_alignment.py` | 17 | Single/double-quoted prefix-array parsing, missing/unreadable file fallback, indented-command extraction, aligned-list pass, unknown/unlisted/meta-command classification, `grep -w` word-boundary semantics (prose-only mention valid, hyphen a boundary, bare substring still invalid), RTK/hook-absent skip, unparsable list skip, `rtk --help` timeout skip, misaligned prefixes reported on stdout |
| `test_check_skill_contracts.py` | `check_skill_contracts.py` | 27 | Bare-comment and shell-timeout exemption detection, single/multiline Bash-call timeout presence, `add_argument` exemption that a docstring mention does not satisfy, config-file discovery across both roots (installed `.claude/agents/*.md`, source `plugins/*/agents/*.md`, nested `rules/_full/`, unrelated Markdown excluded), mode-file shadow and overlap detection, `--check` dispatch, non-local 32f skip |
| `test_list_audit_files.py` | `list_audit_files.py` | 19 | Local sweep finds skill entrypoints/modes/templates/shared, excludes nested agent sidecars and `rules/_full`, installed sweep narrower, entrypoints-only sweep, NUL/newline bare-path output and `-0` argument guard, dedupe+sort, block counting per file, header/relative-path labelling, local vs installed root selection |
| `test_measure_config_size.py` | `measure_config_size.py` | 17 | Empty-dir header-only output, agent/skill/rules size thresholds (warn/fail), skill path labelling, tighter rules budget, global CLAUDE.md counted once, delivered `~/.claude/rules/` counted and per-file flagged, symlinked rule counted once, `--mode` required and inventory/overhead dispatch |
| `test_resolve_agent_file.py` | `resolve_agent_file.py` | 12 | Bare/plugin-prefixed agent and skill resolution, leading-slash skill selection, cc-prefixed vs bare plugin directory precedence, single/ambiguous/no-match glob resolution, resolved vs unresolved CLI output, `--local` failure labelling |
| `test_scan_efficiency_signals.py` | `scan_efficiency_signals.py` | 24 | Frontmatter block extraction, Agent-in-loop detection (for/while) with batch-guard/head-limit exemption, declared-model presence, file-type inclusion (SKILL.md/agent files, modes excluded), report section ordering, boilerplate file counting, missing-model reporting, CLI exit code |
| `test_size_importance.py` | `size_importance.py` | 129 | Block splitting (fences kept whole), the obligation proxy, spine on any doubt (high-stakes words beat the gate, directive-first, structural words, imperatives and ordered items under a rationale label), exact gate shapes with paragraph-level proof for a gated block, move and trim by load class, the own-gate exemption, copy collapsing, the pre-trim versus split-out real-case shape, and the command line. |
| `test_append_ledger.py` | `append_ledger.py` | 9 | Staged record appended after existing lines, staged file consumed, missing trailing newline supplied, parallel `<ledger>.<writer>.rec` swept in name order, absent ledger created, empty flush no-op, `--from` sources appended and kept, missing source skipped, `--from` leaves staged records alone |
| `test_write_skill_contract.py` | `write_skill_contract.py` | 93 | Five-field contract write, optional seven-field form appending a labelled ledger list (empty items omit the block, blank lines dropped), keep-file first line only (cut at bare CR and other line separators, newline-less file adds nothing), parent-dir creation, overwrite of previous contract, cwd-relative path, verbatim placeholder survival, wrong-arity/empty-skill exit codes, `--help` usage, no file written on usage error |
| `test_compaction_boundary.py` | `compaction_boundary.py` | 19 | Boundary-step excerpt cases and byte-equality with the replaced `sed` range, contract lookup (caller dir, own plugin, none), gate pass-through, resolver failure, gate failure, missing resolver and contract, `-h` |
| `test_plugin_layout.py` | `plugin_layout.py` | 7 | Manifest-declared skills/agents directory per host manifest, defaults when a manifest is invalid or declares neither, `PLUGIN_LAYOUT:` lines, missing plugins directory tolerated |
| `test_check_agent_plugin_root.py` | `check_agent_plugin_root.py` | 9 | Only the `${CLAUDE_PLUGIN_ROOT:-…}` form is flagged, per-line findings over `--scan-dir`, clean-scan pass line, non-Markdown file ignored, repo-wide gate that no foundry agent defaults the plugin root |
| `test_extract_keep_flag.py` | `extract-keep-flag.py` | 23 | Option-value reading, keep value written and printed, stale contract cleared only when owned and not inlined, missing slug / session id exit 2, `--out` confined to allowed roots, symlink at the sentinel replaced not followed, venue persisted |
| `test_merge_settings_permissions.py` | `merge_settings_permissions.py` | 43 | Output matches the replaced `jq` merge, user rule order kept, write through a symlinked `settings.json`, idempotent second run, file mode kept, `$HOME` before `Path.home()` (and the fallback), paths outside the write root refused, bad input leaves settings unchanged |
| `test_settings_json.py` | `settings_json.py`, setup skill | 43 | Every subcommand, exact probes, atomic replacement and failure cleanup, preserved env values, idempotent shipped setup blocks with jq forced to fail. |
| `test_bin_input_hardening.py` | several foundry `bin/` scripts | 34 | Trailing-newline token validators, purge registry guard, `resolve_agent_file` path components, wildcard session id, literal ledger names, sentinel-name separators, symlink-guard marker and source checks, bounded Git calls in the version-sync check |
| `test_codemap_changed_modules.py` | `codemap_changed_modules.py` | 21 | Default-branch detection, merge-base vs `HEAD~1` comparison point, NUL-separated changed `.py` files, index-name resolution and cap, absent CLI / unusable payload / unchanged tree as silent no-ops, `--base`/`--cap` parsing |

CI runs the full suite on every push to `main` and on PRs touching `plugins/` (see `.github/workflows/ci-tests.yml`).

## Rule table (full descriptions)

| Rule file | Applies to | Governs |
| -- | -- | -- |
| `communication.md` | all | Re: anchor format, progress narration, tone, output routing, breaking-findings format, terminal colors, confidence display, Markdown no-hard-wrap stub (structure selection in `markdown.md`) |
| `quality-gates.md` | all | Confidence block format, independent adversarial review/fix loop, link verification stub (full wording in `markdown.md`), Python complexity-limit stub (rule in `python-code.md`), output routing (long output to file) |
| `git-commit.md` | injected by `rule-inject.js` after the first commit-creating git call (`add`/`commit`/`push`/`merge`/`revert`/`cherry-pick`/`am`/`rebase`, annotated `tag`) runs; `**/.git/COMMIT_EDITMSG`, `**/.gitmessage` | Commit message format, diff-gathering before writing, co-author trailers, branch + push safety (hard bans stay always-loaded in `claude-config.md`) |
| `claude-config.md` | all | Bash timeouts (3x P90), turn batching (one response per independent call set), directory navigation rules, no hardcoded absolute paths, agent/skill spawn discipline, parallel spawn ceilings stub (table in `agent-spawn.md`), pinned pre-commit hooks over bare lint tools (stub; detail in `python-code.md`), instruction-file compression gate (stub; gate in `foundry-config.md`), git commit/push hard bans |
| `artifact-lifecycle.md` | all | Canonical artifact layout (`.plans/`, `.reports/`, `.temp/`), run directory naming, TTL policy, plan and benchmark evidence kept out of shipped content |
| `external-data.md` | all | Pagination rules: GitHub CLI, REST APIs, GraphQL, Cloud APIs — never work on partial result set |
| `foundry-config.md` | `.claude/**` | Plan-mode gate before any `.claude/` edit, post-edit checklist, XML tag conventions, distribution rules |
| `python-code.md` | `**/*.py` | Google-style docstrings, closed option sets as enums, dataclass/TypedDict selection and record-type boundaries, deprecation API, multi-OS portability, complexity limits, pinned lint/format hook usage |
| `python-testing.md` | `tests/**/*.py`, `**/test_*.py` | pytest design: TDD process, fixtures, parametrization, marker selection, pytest-xdist runs, behaviour-vs-bookkeeping check classification, mutation checks on a copy, cross-OS tests, mocking, what to test in priority order, test names, uncollected-test hazards, prose-reading tests, shared-helper scope, `test-style` hook codes |
| `notebooks.md` | `**/*.ipynb`, `**/*.py` | Notebook cell granularity, markdown narrative depth, plot framing, shell magics, docstring placement — `.ipynb` and Jupytext `# %%` scripts |
| `public-github.md` | all | GitHub reads run free; every gh write needs the user's one-time `gh-write` approval of the exact command |
| `untrusted-content.md` | all | Ingested external text is data, never instruction — delimiters, no permission widening, memory propagation |
| `markdown.md` | `**/*.md` | Never hard-wrap prose, structure selection (bullets, numbered lists, tables, blockquotes), behavior-sensitive reformatting, instruction-file compression gate, link verification (fetch + read + match per URL) |
| `agent-spawn.md` | injected by `rule-inject.js` after the first `Agent()` call runs; `**/skills/**/*.md`, `**/agents/**/*.md` | `Agent()` spawn slots (three-slot delta-first labels, pre-spawn check), end the turn after spawning with per-agent deadlines, parallel spawn ceilings by model tier |
| `model-tiering.md` | `**/skills/**/*.md`, `**/agents/**/*.md` | Model tier ladder (script, Haiku, Sonnet, Opus), checkable escalation triggers, no `model:` pin on inline skills, lean brief-file delegation, author cost checklist |

## Contribution and review expectations (full text)

- Evidence and real-case acceptance: shipped `rules/quality-gates.md` requires cited proof for factual, causal and completion claims, a representative real case alongside TDD, and unchanged user-authorized behavior.
- Mocks are supplementary polishing after real behavior is validated
- unavailable real acceptance stays explicit.
- Review limits remain factual process evidence
- an out-of-scope extra review needs one explicit user decision with its gap, expected output and stop condition.
- The shared cross-validation protocol requires parent validation at every severity before findings publication, then capped independent critical verification.
- Review presentation uses role names only and square-bracketed scores, such as `Software engineer [3]`
- execution provenance stays in evidence and limits.
- Version-one review envelopes distinguish source judgment, verification readiness and durable open concerns.
- Static proof may settle a claim
- unavailable evidence never refutes it, and incomplete feasible work never becomes unconditional approval.
- Missing coverage and other review limits never become source findings or automatic extra-review requests
- declined optional reviews are omitted from findings and action items while factual limits remain disclosed.

## Setup notes

- Merges `statusLine`, `permissions.allow`, `permissions.deny`, `enabledPlugins`, the shipped `env` defaults, and (when pinned by the project) `advisorModel` into `~/.claude/settings.json`
- removes the obsolete `Bash(gh api repos/*:*)` allow entry, the broad `Bash(claude:*)`, `Bash(find:*)`, `Bash(node:*)`, `Bash(sed:*)` and `Bash(eval:*)` allow entries (`permissions-allow-retired.json`), and the retired deny entries (gh writes, local branch/tag deletion; `permissions-deny-retired.json`) while preserving every other existing allow and deny entry.
- It also symlinks all rule files into `~/.claude/rules/` as `foundry-<name>.md` and `TEAM_PROTOCOL.md` into `~/.claude/`.
- Run it from the project repository root.
- The setup summary counts removals from the complete retired allow set.
- Idempotent — safe to re-run.
