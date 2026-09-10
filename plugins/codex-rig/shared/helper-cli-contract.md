# Helper CLI Contract

Helper option schemas live in `--help`, not skills. In a plugin, derive `PLUGIN_ROOT` from the selected `SKILL.md` path and require every helper in `package-manifest.json`; never choose a cache with glob/latest/mtime logic or use a source-tree fallback. The list below is the full release closure: run an entry only when the current package manifest contains it. Before creating or changing an invocation, run the relevant packaged command:

- `python PLUGIN_ROOT/shared/create_run.py --help`
- `python PLUGIN_ROOT/shared/run_gates.py --help`
- `python PLUGIN_ROOT/shared/collect_diff.py --help`
- `python PLUGIN_ROOT/shared/github_read.py --help`
- `python PLUGIN_ROOT/shared/collect_pr.py --help`
- `python PLUGIN_ROOT/shared/escalation_ledger.py --help`
- `python PLUGIN_ROOT/shared/codemap_adapter.py --help` — optional structural-context probe; `context` accepts the closed `--query-kind` vocabulary (`skip`, `central`, `callers`, `blast`, `dependencies`, `test-impact`, `coupling`, `standard`); see `codemap-contract.md`
- `python PLUGIN_ROOT/runtime/calibration/run.py --help`
- `python PLUGIN_ROOT/runtime/calibration/run_live_ab.py --help`
- `python PLUGIN_ROOT/runtime/calibration/score_behavioral.py --help`
- `python PLUGIN_ROOT/shared/find-review-report.py --help`
- `python PLUGIN_ROOT/shared/app_server_review.py --help` — explicit paid review route; `--check-host` verifies setup without a model turn, not review completion
- `python PLUGIN_ROOT/shared/select-git-remote.py --help`
- `python PLUGIN_ROOT/shared/write-result.py --help`
- `python PLUGIN_ROOT/shared/final_handoff.py --help`
- `python PLUGIN_ROOT/shared/validate-artifacts.py --help`
- `python PLUGIN_ROOT/skills/code-review/validate_artifacts.py --help`

Create every skill run with `python PLUGIN_ROOT/shared/create_run.py --skill <skill-id>`. Retain its single printed path and pass that literal path explicitly to every later helper and artifact operation. Never persist the path in a shell variable or assume state survives between command/tool calls.

Artifact namespaces use `.reports/codex/<skill>/<canonical-safe-identity>/run-<NNN>/` only when the skill defines and validates a bounded canonical safe identity; otherwise they use the generated timestamp. Never serialize raw prompts, paths, URLs, refs, credentials, or arbitrary arguments into directory names. PR review is the first identity-indexed workflow, with the normalized authoritative identity `pr-<number>`.

Local reviews and every non-PR workflow keep the initial `.reports/codex/<skill>/<timestamp>/` path. A PR review initially uses the timestamped code-review path because current-branch input may not reveal a PR number before collection. After successful authoritative `pr.json` collection, invoke `python PLUGIN_ROOT/shared/create_run.py --skill code-review --promote-pr-run <run-directory>`, capture the single printed final path, and use that literal `.reports/codex/code-review/pr-<number>/run-<NNN>/` path for every remaining operation. Promotion derives the PR number from the collected artifact and allocates the next numeric run index; callers never construct either value. A pre-identity collection failure stays in its timestamped unavailable-diagnostic path and is never an assessed PR review. Existing flat code-review artifacts remain valid lookup inputs and require no migration.

Also run each skill-specific local CLI's `--help`. Do not copy full flags/templates into `SKILL.md`; state only:

- gate commands or skip reasons
- metadata variable and confidence gaps
- shared validator skill name
- prior skill-specific validator
- extra artifacts and pass/fail rules

Helper JSON and stable stderr codes are machine evidence, not user-facing answers. On any helper failure, the owning workflow first explains the failed operation in plain English, inspects its retained diagnostics, and reports the observed cause or explicitly unknown detail. Then retain the exact code and evidence, state what safe work continues, and name the next action, owner, and resume condition. This includes collection, report lookup, manifest/preflight validation, App Server waves, live calibration, shim diagnostics, and escalation-ledger validation. Diagnose within existing authority before asking for action; never invent a cause, expose raw sensitive output, rerun paid or deterministically failed operations unchanged, or turn a helper's rejected result into acceptance.

Result lifecycle:

1. `run_gates.py` writes `gates.json` and per-gate evidence.
2. Write new `final-handoff.json` with `presentation_version=2`; `final_handoff.py render` validates it and writes digest-bound `final.md` plus `final-handoff.validation.json`. Preserve the recorded presentation of historical handoffs.
3. `write-result.py` writes schema-v2 `result.candidate.json`, reconciles status with gate evidence, and requires the final-handoff binding in metadata.
4. Run configured skill-specific validation.
5. `validate-artifacts.py` validates the shared and skill-specific artifact contract, reruns `final_handoff.py check`, and reconciles the handoff with gates, confidence, and workflow evidence.
6. Rename only the validated candidate to `result.json`, then emit the validated `final.md` bytes verbatim.

For code-review, step 6 finishes through `find-review-report.py --complete-run <run-directory>`: both artifact validators rerun against the canonical promoted result; assessed PR lookup must select that exact result before any final bytes are emitted. A failed handoff starts with a plain-English explanation of the incomplete operation, then its exact process status, retained evidence, and specific next action—not a normal assessed-final verdict or bare blocker code. This command is read-only and does not repair or promote candidates; the caller owns permitted diagnosis and evidence-backed recovery.

Never hand-write `result.json`, promote an unvalidated candidate, manually reconstruct validated final text, or infer flags from stale examples. See `final-handoff-contract.md`.

## Resume And Re-entry

This applies to every skill, including continuation after user intervention, approval, a corrected failure, context compaction, or a repeated skill invocation. Re-entry changes where work resumes, not what completion requires. A request to continue or retry is not an exact-output override and never waives the normal closing gate.

1. Re-read the selected skill's current output contract and recover the run path, objective, source identity, prior actions, and gate status from retained evidence. Do not infer completion from a prior chat answer, passing tests alone, or notes containing a recommendation.
2. Identify the first unmet checkpoint. Reuse evidence only while its source, scope, and inputs remain valid; repeat invalidated checks, never replay completed work merely because the user intervened. Preserve old attempts; missing trustworthy state requires a fresh documented run, not invented provenance. Re-entry does not reset recurrence counts, authorize another failed paid wave, or broaden permissions.
3. Complete the remaining workflow and its normal closing gate. Artifact workflows still require gates, rendered handoff, candidate, validation, promotion, and exact final output in the order above. Code Review additionally requires its completion/discovery check. Preserve required summary labels and tables after the plain-English opening; do not replace them with an informal bullet recap.
4. If closure cannot pass, explain the incomplete checkpoint, actual cause or unknown detail, governing rule, continuing work, and specific next action/owner/resume condition. Label evidence preliminary; do not claim a standard result passed or present a merge verdict. Diagnose and perform authorized recovery before handing work back.

`agent-shims` retains its documented non-artifact exception, not a closing-gate exception: re-entry still completes the selected action's verification and diagnostic interpretation before its normal final answer. Do not fabricate result artifacts or introduce a new lifecycle solely to enforce presentation.
