# Codex Quality Gates

Detailed codex-native workflows emit the same gate fields. Documented lightweight local paths run applicable project checks directly and report their evidence without creating this bundle.

Apply [Acceptance and recovery](native-skill-contract.md#acceptance-and-recovery): reviewer/report formatting and wording hints do not become failed acceptance checks. Preserve semantic structure and factual evidence; automatically handle equivalent presentation at the owning reader and retain raw evidence. Calibration benchmark wording misses are advisory entries in `checks.txt`; structural contract and behavioral failures retain their normal failed-check accounting. Configured project checks still run, with authorized automatic formatting/correction and relevant reruns; a tool's diagnostic alone does not establish its impact on delivery.

## Review limitations and additional review

Before classifying a material gap as unresolved or deferred, apply [Agent-owned confidence recovery](#agent-owned-confidence-recovery). A limitation describes the evidence left after feasible authorized recovery; it is not a substitute for performing that recovery.

Deliver the full review supported by the evidence actually obtained. Missing independent coverage, unavailable reviewer tools, unproven context isolation, incomplete provenance, unexecuted checks and confidence limitations describe the review's limits; they are not source defects, severity findings, operational-blocker rows or automatic remediation tasks. Preserve actual failed checks and qualify readiness and conclusions honestly; never fabricate passing evidence or unconditional approval. A concrete source defect supported by evidence remains a finding even when coverage is limited.

The one bounded independent implementation challenge required by [Proportional Execution](native-skill-contract.md#proportional-execution) is part of the authorized implementation workflow, subject to actual host, paid-execution and protected-state boundaries; it is not an optional extra review or a multi-round loop. Preserve any unmet independent coverage as a factual limitation, not a fabricated clean result.

If an additional review beyond the already authorized scope would resolve a material uncertainty, ask once for a decision before dispatch. State the exact question or gap, bounded scope, expected evidence/output and stopping condition, and offer to finish with the stated limitation. Existing explicit authorization for that same pass remains valid. A declined or dismissed optional review is skipped: do not re-ask, dispatch an alternative reviewer, or carry the request into final findings, blockers, recommendations, next steps or remediation selection. Retain only the factual limitation describing what was not verified. An unanswered optional question never authorizes dispatch; finish the available review with its limitation. An explicitly pending required decision keeps only its dependent work paused.

One accepted additional pass does not authorize another pass or a review/fix loop. An explicitly requested convergence workflow retains its bounded mechanics; its coverage gaps still remain limitations rather than synthetic source findings. Reviewers assess the assigned source and report real defects, not requests to review their own review. A report-intake consumer preserves this distinction, including for historical reports that incorrectly promoted a process limitation into a finding.

An explicit user decision may waive additional reviewers, review rounds or broader review coverage, including independence the user previously requested. Treat this as a change to the review scope, not a gate override. Record the user's decision, exact target/revision, waived coverage, retained requirements and resulting limitation in the review notes. Preserve frozen plans and rejected evidence; use the owning workflow's separate fallback plan for the revised scope. A declined optional repair alone does not revoke an earlier explicit independence requirement. Reuse an already explicit waiver for the same target and gap; never ask again or dispatch waived work.

Source identity/freshness, evidence integrity and provenance for claimed work, truthful check results, artifact validation, promotion and completion checks are never waivable. Keep required project checks and their actual failures; a coverage waiver never authorizes merge approval, fabricates independent evidence, turns an unexecuted check into a pass or admits an invalid artifact. A review can complete within its revised scope only after the normal validators and completion gates succeed; otherwise deliver the supported findings as a clearly incomplete report. State review completion separately from merge readiness.

## Required checks

1. `lint`: `ruff check` (or project lint command)
2. `format`: formatter check or formatter run result
3. `types`: `mypy` (or project type check command)
4. `tests`: impacted tests at minimum, full suite for broad changes
5. `review`: self-review diff/risks; check request conformance, constraints, disclosed deviations

## Required gate output

Each skill run must write:

- `status`: `pass`, `fail`, or `timeout`
- `checks_run`: list of check ids that were executed
- `checks_failed`: list of check ids that failed
- `findings`: severity counts `{critical, high, medium, low}`
- `confidence`: numeric `0.0` to `1.0`
- `artifact_path`: absolute or repo-relative path to result artifact

Name the delivered result or claim assessed by each confidence score. Assess confidence in the correctness of the delivered result within its stated scope, including whether it was produced and checked correctly and the limitations that could make it wrong. When the user requests process confidence, score that delivered result and its production and verification; do not substitute progress toward completion, certainty that a status report is accurate, or speculation about future success. A score used by an acceptance gate must assess the process or substantive judgment that gate protects; confidence in an ancillary status fact may be reported separately but cannot satisfy that gate. Report completion and gate status separately. An unfinished step alone is not a confidence deduction; an evidence gap reduces confidence only when it materially limits the named claim. A high-confidence blocked or incomplete outcome remains blocked or incomplete, and a high score never changes a failed check or completes pending work.

Confidence needs objective evidence. Before user output, apply bands: `<= 0.8` unacceptable; skill artifact returns `status=fail` or `status=timeout` with `confidence-not-acceptable` in `checks_failed`. `0.8 < confidence < 0.85` very questionable; return `status=fail` or `status=timeout` with `confidence-very-questionable`. `0.85 <= confidence < 0.9` cautious-low; proceed only with objective evidence, recovery actions, remaining limits. `>= 0.9` fair but not automatic; name evidence-backed material limits. Result JSON records `metadata.confidence_recovery`: initial score, evidence, recovery actions, recomputed score, band status, remaining limits. Chat reports score and material limits; artifact holds detailed recovery/closure evidence. Every scored skill/agent output lists confidence gaps or degradation reasons. Close each with evidence or explicit unresolved/deferred record. Preserve result fields `status`, `checks_failed`, and `confidence_gaps`. Write `metadata.confidence_gaps` and `metadata.confidence_gap_closures`; when `confidence < 1.0`, include one concrete gap/residual limit and matching `closed`, `unresolved`, or `deferred` closure.

Each `run_gates.py` invocation writes `gates.json` with exactly five IDs. Entries contain `id`, `status`, `exit_code`, `duration_seconds`, `command_path`, `stdout`, `stderr`; `missing-command`, `not-applicable`, `timeout` also need reason. The three log fields record path relative to run's output directory in POSIX form (`checks/<id>.<kind>.txt`); absolute path is accepted only inside that directory. Readers accept any relative entry that resolves inside output directory, trying `--out` first, then its ancestors for runs written before this convention — never their own working directory, which is what once made same artifact valid in one directory, invalid in another. Every accepted log must resolve inside output directory. `not-applicable` passes only with explicit reason; `missing-command`/`timeout` fail. Result status/check lists reconcile with `gates.json`.

For `--pytest-python` source binding, keep the project's pytest selection and parallel options. The test gate records selected test paths and declared module origins from each executing pytest process, including xdist workers. A missing or crashed worker receipt, an observed origin outside the verified source, or an untracked origin without valid local snapshot membership cannot certify reviewed source; retain the failing or inconclusive gate evidence. A module absent in one worker remains neutral when another worker proves its tracked origin or admitted local snapshot membership. Clean PR mode still requires its expected head and tracked origins. A local review mirror may omit `--expected-head` only when `--out` already contains the valid collector `local-source/review-worktree.json` and `local-source/source-snapshot.json` for that exact mirror, with no competing PR receipt; the runner freezes a current runtime snapshot before gates. Local test/module origins must match regular-file snapshot members and current bytes, so a collector-admitted added file may keep `tracked=false` only with that proof. This local admission does not relax clean PR or release gate contracts, and old unbound logs cannot be promoted retroactively.

Optional but recommended:

- `recommendations`: list of concrete next improvements
- `follow_up`: list of prioritized next actions
- `metadata`: required with confidence for machine-readable gaps/closures; otherwise skill-specific evidence not hidden in prose notes

## Agent-owned confidence recovery

The agent owns recovery of material evidence gaps within the requested scope. Before final handoff, identify the next evidence-producing action for each gap and execute it when feasible and authorized. Merely recording a deduction, naming a maintainer, or meeting a confidence threshold does not complete an available check. Seek the strongest supported result, then recompute confidence from the evidence actually obtained.

- Missing reviewer provenance: inspect authentic dispatch, context delivery, child terminal output and join receipts; complete outstanding authorized passes and collect existing receipts through the owning workflow. Apply its supported bounded recovery when eligible. Never invent a join, rewrite rejected history, count parent inspection as independent coverage, or start additional review beyond scope without authorization.
- Unstarted required reviews: finish producer-owned capacity preparation and dispatch the complete required roster through its supported route. A valid oversized-input batching route, queued specialist or generated schedule is executable continuation, not proof that independent review is unavailable. Preliminary output records a genuine pause only after feasible authorized recovery; it never substitutes for required source judgment or closes an unfinished review.
- Unexecuted project checks: inspect the declared command, existing environment and actual permissions, then run the applicable pinned checks. A hook's ability to edit files alone is not a reason to defer: for read-only work, use the owning workflow's disposable-copy procedure and retain original-source identity, command, exit status and before/after diff. A pass after autofix verifies changed scratch bytes, not the original source. Actual dependency, execution and permission boundaries remain controlling.
- Missing declared project dependencies: every workflow executing project code applies [Project Dependency Preparation](native-skill-contract.md#project-dependency-preparation) before treating package absence as blocked verification. This includes implementation, investigation, optimization and their delegated test/check work. The parent owns shared-environment changes and required canonical reruns; a read-only reviewer requests preparation from the parent and retains its existing overlay and execution boundaries.
- Degraded structural tooling: preserve the provider's original status, then identify the specific source or test fact needed for the requested decision. Inspect added or unmapped files directly and trace relevant dynamic references through source, configuration and affected tests. Record which required facts that evidence closes; a provider limitation alone is not an unresolved review obligation or an automatic confidence deduction. Keep a material gap only when a required fact remains unproved, with the exact missing check and owner. Never claim exhaustive dynamic-call coverage from a bounded search or relabel the provider result as complete.
- Unavailable CI detail: inspect retained status and diagnostics, use permitted evidence retrieval and run relevant local checks when feasible. A failed result-page read does not excuse skipping local verification. Keep local results separate from remote status; local success never changes or proves a remote required-check result.
- Recovery stays within existing retry, recurrence, budget and stop rules. Diagnose before retrying; a different route needs evidence that it avoids the observed cause and remains authorized. Do not rotate reviewers, repeat unchanged failures, expand scope, or bypass admission merely to improve confidence.
- Defer only when evidence establishes an actual missing capability, prerequisite, decision, explicit scope exclusion or exhausted permitted recovery. Record attempts and results, the concrete remaining blocker, the responsible owner and the exact resume condition in existing recovery/closure fields. Continue unaffected authorized work; ask the user only for a genuinely missing decision. Residual uncertainty that has no feasible in-scope check remains an honest limitation.

This is an execution obligation, not a new artifact schema or permission grant. Preserve failed evidence and all existing completion gates. Narrow unsupported claims without dropping requested acceptance criteria; never inflate scores, waive required independence, or mark an unperformed check passing to reach a confidence band.

## Confidence Deduction Accounting

For every reported confidence gap or limitation, put its contribution first, before the description: `(-0.NN) Description` using an ASCII minus and two decimals. Unique deductions must sum to exactly `1.00 - score` at the displayed precision; choose the displayed score and deductions consistently. Keep existing confidence bands and evidence requirements.

- A limitation that did not reduce the score carries `(-0.00)`; retain its evidence or unresolved/deferred rationale.
- Name any cap, floor, or band that determined the score and show its contribution. Attribute overlapping causes without double-counting; when a limit repeats a gap, reference the same label and count its deduction once.
- Put shortfall no named gap explains in one explicit `residual` entry with its deduction and reason; never hide it or silently distribute it across named gaps. A score below `1.00` requires at least one gap or residual entry.
- This is transparent judgment accounting, not an empirically calibrated probability or measured causal decomposition. Deductions remain evidence-backed estimates; arithmetic precision does not establish statistical precision.

A disclaimer that a confidence score lacks empirical calibration is not itself a failed check or unfinished remediation task. Preserve it as an interpretation limit; do not invent a calibration requirement from it or from selecting all actionable items. If it arrives as a report obligation, retain its provenance and record an evidence-backed `not-applicable` disposition citing this contract. Explicitly requested calibration experiments, project-required calibration checks, actual coverage gaps, failed confidence thresholds and unsupported confidence claims still require their normal evidence and closure.

Example: confidence `0.96` in the correctness of a delivered workflow result and how it was checked; `(-0.02)` unavailable target-host execution and `(-0.02)` unexercised live workflow each limit that claim, while `(-0.00)` installation is excluded from the requested scope. The two reducing limitations account for the `0.04` shortfall; the scope disclosure contributes zero. If the reported claim is instead the verified fact that execution is blocked, score that distinct claim on its own evidence rather than carrying the workflow deductions automatically; the blocked execution remains incomplete.

## Fail rules

- An execution failure never makes an applicable check `not-applicable`. Missing tools, launcher panics, or sandbox restrictions require diagnosis and preserved failed evidence. Choose the required check entrypoint before execution. Do not rerun passing checks solely to wrap them in a different report or obtain a second approval: lightweight paths retain direct-check evidence, and detailed paths reuse an existing valid canonical receipt for unchanged source, selection, environment, and command. A direct check cannot silently replace a required source-bound canonical gate; if that proof was not collected, run the required gate or disclose it as incomplete. Same-directory reruns archive runner-owned receipts under `gate-attempts/<NNN>` before executing and reject failed-to-skipped reclassification. Incomplete prior state blocks overwrite; diagnose it before starting a separately scoped run.
- Any `critical` finding => `status=fail`
- Any failed check in `checks_failed` => `status=fail`
- Missing command/tool for required gate => `status=fail`
- Explicitly unsupported gate with reason => that gate's per-check `gates.json` entry `status=not-applicable`; non-failing, excluded from `checks_failed` (top-level run `status` stays `pass`/`fail`/`timeout` only — see "Required gate output")
- All five `gates.json` entries `not-applicable` => top-level run `status=pass` (zero failures to report)
- Missing artifact => `status=fail`
- Gate timeout => `status=timeout`
- For every skill/agent: `confidence <= 0.8` cannot pass; `0.8 < confidence < 0.85` cannot be complete; `0.85 <= confidence < 0.9` needs recorded recovery/limits; `confidence >= 0.9` still needs evidence.

## Artifact path contract

- Path: `.reports/codex/<skill>/<YYYY-MM-DDTHH-MM-SSZ>/result.json`
- Optional: `notes.md` in same directory

## Final Chat Contract

Every final chat is compact, outcome-coupled handoff in this order:

1. `Outcome`: name the topic or question being answered and state decision or completed result first, including gate status when it changes whether work is complete. The opening must stand alone; avoid unanchored “both,” “that,” or “yes” without repeating the conversation.
2. `Results`: show every decision unit needed to understand outcome. Use the skill's required table for every outcome, including a single result, zero findings, all checks passing, partial completion and blocked work; do not collapse skipped, rejected, deferred, or unresolved rows.
3. `Verification`: name checks run and their exact results, including skips, failures, and unavailable checks.
4. `Remaining`: list every unresolved, deferred, skipped, externally owned, or environment-blocked obligation with its owner and next action. Write `None` only when evidence closes all obligations.
5. `Confidence`: report score, material limits, and any unresolved confidence gap under shared confidence-band policy and [Confidence Deduction Accounting](#confidence-deduction-accounting).
6. `Artifact`: link the canonical result artifact and any essential supporting report. An artifact is supplemental, never a substitute for the outcome, result rows, verification, remaining-work disclosure, or next steps.
7. `Recommendations / next steps`: end with prioritized owner/action entries that move unresolved work or accepted recommendation forward. Reference result rows instead of repeating them; omit this section when no follow-up is justified or a terminal-close branch forbids it.

Keep headings and tables proportional to result. A single decision still uses one short result row; branch-heavy results use separate bullets or tables instead of one long paragraph. Tables and stable labels are human reading anchors across all skills and workflows, not optional decoration. Keep ordered actions in numbered steps and parallel facts in bullets. Terminal-close and unavailable outcomes use a compact status table without inventing assessed findings or recommendations. Exact caller output and immediate yields for accepted pending questions retain their existing contracts.

Skill contracts retain local outcomes, exact results, domain content, and terminal exceptions; other fields inherit.

## Native contract

Skill files should follow `native-skill-contract.md`. Configured skills require:

- `Input Schema`
- `Workflow`
- `Fail-Fast Rules`
- `Quality Gates`
- `Calibration Hooks`
- `Output Contract`

Configured agents require:

- `Boundaries`
- `Evidence Standard`
- `Output Contract`
- clear `TRIGGER`, `SKIP`, and `NOT for` routing clauses

## Execution helpers

- Use `PLUGIN_ROOT/bin/python PLUGIN_ROOT/shared/run_gates.py` to execute five checks consistently.
- Use executable `PLUGIN_ROOT/shared/write-result.py` to write canonical JSON result payloads.
- Use `PLUGIN_ROOT/shared/final_handoff.py` and `final-handoff-contract.md` after gates to validate and render exact final-response structure. This checkpoint is not sixth gate; its failure blocks result promotion.
- Use `PLUGIN_ROOT/bin/python PLUGIN_ROOT/shared/collect_diff.py` to collect scope-aware git diff artifacts consistently.
- `github_read.py` is plugin-wide GitHub data boundary. Use it for all issue/release/repository reads and explicit GraphQL query for Discussions; use `collect_pr.py --checkout` for complete PR evidence. Never invoke `gh` directly. The broker prefers authenticated `gh` but never inspects credentials; it permits only audited built-in view groups (`gist`, `issue`, `pr`, `project`, `release`, `repo`, `ruleset`, `run`, `workflow`), REST GET, GraphQL query argv, persists no CLI failure output. Its public `api.github.com` fallback is unauthenticated, cannot replace private-only evidence, so complete PR collection fails closed if its review-thread query cannot run. `gh pr checkout` changes only local checkout. Codex Git marketplace add/upgrade remains separately authorized lifecycle operation.
- Apply `native-skill-contract.md` Networked CLI Approval to each intentional shell-network path: keep persistent workspace networking disabled and approve complete owning command, not only nested executable.
- PR checkout/update artifacts never record `git`/`gh` `--force`; force needs stop-and-ask confirmation with overwrite-risk rationale first.
- Use `PLUGIN_ROOT/shared/validate-artifacts.py` to validate common report, ledger, gate-log, and result JSON artifacts.
- Schema-v2 results must bind `final-handoff.json`, `final.md`, and `final-handoff.validation.json`; after validation and promotion, emit `final.md` verbatim. Historical schema-v1 results remain readable without this binding. Approved nested review uses [Nested review delivery](final-handoff-contract.md#nested-review-delivery); all artifact and completion gates remain mandatory.
- Use `PLUGIN_ROOT/shared/severity-map.md` to map findings to severity levels.

## Behavior-Change Guardrails

Codex behavior changes must also update registrations, docs, calibration in same patch. Prefer existing agent/skill unless new role/workflow has distinct triggers, acceptance criteria, measurable gates.

A passing assessed `audit` result additionally requires an executed passing `review` gate. The generic runner still permits all `not-applicable` diagnostics; skill applicability checks decide whether those records can certify workflow completion.

New terminal handoffs use presentation version 12. Keep full evidence in machine records; display file paths outside every table as `- Item: file-path` lists. Presentation normalization is automatic; factual bindings, source coverage and authority checks retain their existing meaning.
