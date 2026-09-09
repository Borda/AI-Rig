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

Result lifecycle:

1. `run_gates.py` writes `gates.json` and per-gate evidence.
2. Write `final-handoff.json`; `final_handoff.py render` validates it and writes digest-bound `final.md` plus `final-handoff.validation.json`.
3. `write-result.py` writes schema-v2 `result.candidate.json`, reconciles status with gate evidence, and requires the final-handoff binding in metadata.
4. Run configured skill-specific validation.
5. `validate-artifacts.py` validates the shared and skill-specific artifact contract, reruns `final_handoff.py check`, and reconciles the handoff with gates, confidence, and workflow evidence.
6. Rename only the validated candidate to `result.json`, then emit the validated `final.md` bytes verbatim.

For code-review, step 6 finishes through `find-review-report.py --complete-run <run-directory>`: both artifact validators rerun against the canonical promoted result; assessed PR lookup must select that exact result before any final bytes are emitted. A failed handoff reports blocked-first process status and retained evidence, not a normal assessed-final verdict. This command is read-only and does not repair or promote candidates.

Never hand-write `result.json`, promote an unvalidated candidate, manually reconstruct validated final text, or infer flags from stale examples. See `final-handoff-contract.md`.
