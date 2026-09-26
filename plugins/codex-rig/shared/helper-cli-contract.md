# Helper CLI Contract

Helper option schemas live in `--help`, not skills. In plugin, derive `PLUGIN_ROOT` from selected `SKILL.md` path and require every helper in `package-manifest.json`; never choose cache with glob/latest/mtime logic or use source-tree fallback. The list below is full release closure: run entry only when current package manifest contains it. Before creating or changing invocation, run relevant packaged command:

- `python PLUGIN_ROOT/shared/create_run.py --help`
- `python PLUGIN_ROOT/shared/run_gates.py --help`
- `python PLUGIN_ROOT/shared/release_evidence.py --help` — explicit `record-demo` executes an authorized local demo and retains script/output receipts; ordinary release validation never executes it
- `python PLUGIN_ROOT/shared/collect_diff.py --help`
- `python PLUGIN_ROOT/shared/github_read.py --help`
- `python PLUGIN_ROOT/shared/collect_pr.py --help`
- `python PLUGIN_ROOT/shared/escalation_ledger.py --help`
- `python PLUGIN_ROOT/shared/adversarial_loop.py --help` — validates convergence ledger consistency, not reviewer authenticity
- `python PLUGIN_ROOT/shared/codemap_adapter.py --help` — optional structural-context probe; `context` accepts closed `--query-kind` vocabulary (`skip`, `central`, `callers`, `blast`, `dependencies`, `test-impact`, `coupling`, `standard`); see `codemap-contract.md`
- `python PLUGIN_ROOT/runtime/calibration/run.py --help`
- `python PLUGIN_ROOT/runtime/calibration/run_live_ab.py --help`
- `python PLUGIN_ROOT/runtime/calibration/score_behavioral.py --help`
- `python PLUGIN_ROOT/shared/find-review-report.py --help`
- `python PLUGIN_ROOT/shared/app_server_review.py --help` — explicit paid review route; `--check-host` verifies setup without model turn, not review completion
- `python PLUGIN_ROOT/shared/select-git-remote.py --help`
- `python PLUGIN_ROOT/shared/write-result.py --help`
- `python PLUGIN_ROOT/shared/final_handoff.py --help`
- `python PLUGIN_ROOT/shared/validate-artifacts.py --help`
- `python PLUGIN_ROOT/skills/code-review/validate_artifacts.py --help`

For the selected workflow path, identify helpers whose invocation is already known, including its required start and closing gates. Run their independent `--help` commands in one tool batch when the host supports it; keep each command's exit status and complete output available. Return a compact labeled index of each helper's usage and relevant option descriptions, targeting at most 16 KiB of aggregate text; retain full help in the tool session or run artifacts so it can be inspected without rerunning the command. If the index exceeds that bound, return status and usage for every helper and inspect needed option details from retained output. If an option's meaning is absent or ambiguous in the index, inspect that helper's retained full help before invoking it. When top-level help lists only subcommands, inspect the selected subcommand's `--help` before using its options. Never silently truncate a failed help check or infer an option from a partial response. Do not run the full release roster on every skill invocation. Inspect a conditional helper when its branch is selected, and batch any helpers newly known at that point. Reuse help already observed for the same packaged helper bytes and invocation within the run; a changed package or unknown option requires fresh help. A failed help check blocks that helper's invocation, not unrelated work. Batching never combines approval boundaries or authorizes a networked or paid operation.

Create every skill run with `python PLUGIN_ROOT/shared/create_run.py --skill <skill-id>`. Retain its single printed path, pass that literal path explicitly to every later helper and artifact operation. Never persist path in shell variable or assume state survives between command/tool calls.

Artifact namespaces use `.reports/codex/<skill>/<canonical-safe-identity>/run-<NNN>/` only when the skill defines and validates a bounded canonical safe identity; otherwise they use the generated timestamp. Never serialize raw prompts, paths, URLs, refs, credentials, or arbitrary arguments into directory names. PR review is the first identity-indexed workflow, with the normalized authoritative identity `pr-<number>`.

Local reviews and non-PR workflows normally keep the initial `.reports/codex/<skill>/<timestamp>/` path. Bounded challenge-resolve chunks are the exception: each child uses `create_run.py --skill challenge-resolve --root <coordinator-run>/chunks/runs`, keeping its ordinary skill/timestamp suffix under the coordinator. A PR review initially uses timestamped code-review path because current-branch input may not reveal PR number before collection. After successful authoritative `pr.json` collection, invoke `python PLUGIN_ROOT/shared/create_run.py --skill code-review --promote-pr-run <run-directory>`, capture single printed final path, use that literal `.reports/codex/code-review/pr-<number>/run-<NNN>/` path for every remaining operation. Promotion derives PR number from collected artifact, allocates next numeric run index; callers never construct either value. A pre-identity collection failure stays in its timestamped unavailable-diagnostic path, never assessed PR review. Existing flat code-review artifacts remain valid lookup inputs, require no migration.

Also run each skill-specific local CLI's `--help`. Never copy full flags/templates into `SKILL.md`; state only:

- gate commands or skip reasons
- metadata variable and confidence gaps
- shared validator skill name
- prior skill-specific validator
- extra artifacts and pass/fail rules

Helper JSON and stable stderr codes are machine evidence, not user-facing answers. On any helper failure, owning workflow first explains failed operation in plain English, inspects its retained diagnostics, reports observed cause or explicitly unknown detail. Then retain exact code and evidence, state what safe work continues, name next action, owner, resume condition. Includes collection, report lookup, manifest/preflight validation, App Server waves, live calibration, shim diagnostics, escalation-ledger validation. Diagnose within existing authority before asking for action; never invent cause, expose raw sensitive output, rerun paid or deterministically failed operations unchanged, or turn helper's rejected result into acceptance.

Result lifecycle:

1. `run_gates.py` writes `gates.json` and per-gate evidence. Optional `--expected-head <full-lowercase-sha>` guards each executable command with clean Git head/status observations before and after execution; mismatches, dirtiness, inspection errors fail the gate. PR review additionally passes `--worktree <absolute verified checkout root>` to execute checks there and bind that path in source receipts; this option requires `--expected-head`. Python PR test gates use `--pytest-python <absolute interpreter>`, one or more `--pytest-import <module>`, and `--pytest-args-json <JSON string array>` instead of free-form `--tests`: the selected interpreter runs pytest in process and records post-test imported module origins; missing, untracked, or outside-worktree origins cannot pass. The mode cannot prove imports performed only in test subprocesses or xdist workers, which remain inconclusive. New release-contract runs require the head flag and matching `metadata.release_head`; unrelated unflagged callers retain their existing behavior. Keep output outside tested source or Git-ignored to avoid self-induced dirtiness. Git source receipts alone do not prove imported package identity.
2. Write new `final-handoff.json` with `presentation_version=3`; `final_handoff.py render` validates it, writes digest-bound `final.md` plus `final-handoff.validation.json`. Current assessed Code Review schema-3 candidates and canonical results require this presentation version; preserve recorded presentation of historical handoffs.
3. `write-result.py` writes schema-v2 `result.candidate.json` for other skills and schema-v3 candidates for every current Code Review disposition, reconciles status with gate evidence, and requires final-handoff binding in metadata.
4. Run configured skill-specific validation.
5. `validate-artifacts.py` validates shared and skill-specific artifact contract, reruns `final_handoff.py check`, reconciles handoff with gates, confidence, workflow evidence.
6. Rename only validated candidate to `result.json`. For code-review, complete the lookup below before emitting any final bytes; other artifact workflows emit validated `final.md` bytes verbatim.

For code-review, step 6 finishes through `find-review-report.py --complete-run <run-directory>`: both artifact validators rerun against canonical promoted result; assessed PR lookup must select that exact result before any final bytes are emitted. Explicit assessed PR intake applies the same newest-run check: the current report root supersedes legacy for the same PR because each root allocates run numbers independently, then each root orders its own PR runs numerically. Current schema-3 assessed candidates and canonical results require the assessed handoff branch and presentation version 3; historical schema-2 caller-contract handoffs remain readable, and schema-2 or schema-3 reports without current proof are available only through explicitly unverified `--archive-result` output. A failed handoff starts with plain-English explanation of incomplete operation, then its exact process status, retained evidence, specific next action—not normal assessed-final verdict or bare blocker code. This command is read-only, does not repair or promote candidates; caller owns permitted diagnosis and evidence-backed recovery.

Review intake through `--target` or `--result` also reruns both artifact validators before returning the canonical artifact path. PR intake uses its recorded producer thread by default; an explicit producer override remains validated. Discovery metadata alone never makes a report actionable, including historical reports whose supported schema still must pass its validators.

Never hand-write `result.json`, promote unvalidated candidate, manually reconstruct validated final text, or infer flags from stale examples. See `final-handoff-contract.md`.

## Resume And Re-entry

Applies to every skill, including continuation after user intervention, approval, a corrected failure, context compaction, or a repeated skill invocation. Re-entry changes where work resumes, not what completion requires. A request to continue or retry is not an exact-output override, never waives the normal closing gate.

1. Re-read selected skill's current output contract, recover run path, objective, source identity, prior actions, gate status from retained evidence. Never infer completion from prior chat answer, passing tests alone, or notes containing recommendation.
2. Identify first unmet checkpoint. Reuse evidence only while its source, scope, inputs remain valid; repeat invalidated checks, never replay completed work merely because user intervened. Preserve old attempts; missing trustworthy state requires fresh documented run, not invented provenance. Re-entry doesn't reset recurrence counts, authorize another failed paid wave, or broaden permissions.
3. Complete remaining workflow and its normal closing gate. Artifact workflows still require gates, rendered handoff, candidate, validation, promotion, exact final output in order above. Code Review additionally requires its completion/discovery check. Preserve required summary labels and tables after plain-English opening; never replace them with informal bullet recap.
4. If closure can't pass, explain incomplete checkpoint, actual cause or unknown detail, governing rule, continuing work, specific next action/owner/resume condition. Label evidence preliminary; never claim standard result passed or present merge verdict. Diagnose and perform authorized recovery before handing work back.

`agent-shims` retains its documented non-artifact exception, not closing-gate exception: re-entry still completes selected action's verification and diagnostic interpretation before its normal final answer. Never fabricate result artifacts or introduce new lifecycle solely to enforce presentation.
