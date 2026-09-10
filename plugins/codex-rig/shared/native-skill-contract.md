# Native Skill Contract

Codex-native skills are portable local workflows. Runnable contract: shared result schema plus selected package layout recorded in `package-manifest.json`.

## Required Sections

Every native `SKILL.md` keeps these sections or clear equivalents:

- YAML-style frontmatter: unindented `---`, `name:`, `description:` before first Markdown heading.
- `Input Schema`: required inputs, optional inputs, mode flags, and done condition.
- `Workflow`: linear steps with stable local commands where commands are useful.
- `Fail-Fast Rules`: conditions that stop or fail run.
- `Quality Gates`: check mapping and pass/fail decision rules.
- `Calibration Hooks`: expected calibration updates when behavior changes.
- `Output Contract`: shared JSON result fields from `quality-gates.md`.

> `agent-shims` is absent from calibration skill roster (`runtime/calibration/run.py` `SKILLS`); required-section, `result-template.json`, canonical result-artifact, and executable final-handoff checks do not run against it. This remains documented manager-lifecycle exception until it gains canonical run/result artifact.

Long workflows keep contract-level `## Workflow` with `### NN:` ordered subheaders. Do not make workflow steps `##` peers of contract sections.

## Portability Rules

- Start every user-facing message with short plain-English explanation before technical details: progress updates, questions, approval requests, errors, blockers, handoffs, and final answers. Say what happened or what action is needed, not just status label. Keep exact evidence below; machine-only payloads and explicitly requested exact caller formats remain unchanged. This communication rule takes precedence over compressed prose for opening explanation.
- Use `.reports/codex/<skill>/<canonical-safe-identity>/run-<NNN>/result.json` only when workflow defines and validates bounded non-sensitive identity; never serialize arbitrary arguments into artifact paths. Otherwise keep `.reports/codex/<skill>/<timestamp>/result.json`. Assessed PR reviews use safe identity `pr-<number>` after authoritative PR collection and run promotion; PR collection failure before authoritative identity remains timestamped unavailable diagnostic. Existing flat code-review artifacts stay discoverable without migration.
- New human-readable report artifacts use Caveman Ultra: each fact once, no filler or repeated context. Do not compress or omit machine-readable JSON, commands, paths, code, logs, patches, required tables, evidence, failures, risks, owner/action, or confidence limits. Use clear concise prose where Ultra would make security, irreversible, or ordered instructions ambiguous.
- Use `python PLUGIN_ROOT/shared/run_gates.py` and executable `write-result.py` when skill changes files or runs code checks.
- Use `PLUGIN_ROOT/shared/helper-cli-contract.md` for gate/write/validate lifecycle. Helper `--help` owns options; skills do not duplicate full local CLI invocations.
- Apply that lifecycle after every resume, user intervention, or repeated skill invocation. Recover first unmet checkpoint from retained evidence; never treat earlier failure, passing tests, or user's request to continue as permission to skip selected skill's normal closing gate or output structure.
- Use `PLUGIN_ROOT/shared/final-handoff-contract.md` and `final_handoff.py` for post-gate presentation checkpoint. Promote only schema-v2 result whose handoff, rendered final text, and digest record pass shared validation; emit `final.md` verbatim.
- Use `python PLUGIN_ROOT/shared/collect_diff.py` for scope-aware `working-tree`, `path`, `commit` diffs; do not duplicate git plumbing.
- `github_read.py` is plugin-wide GitHub data boundary. Use `python PLUGIN_ROOT/shared/github_read.py --out <file> -- gh <resource> view ...` only for audited built-in view groups: `gist`, `issue`, `pr`, `project`, `release`, `repo`, `ruleset`, `run`, and `workflow`; use explicit read-only `gh api graphql -f query=...` query for Discussions. Use `--fallback-url https://api.github.com/repos/...` only for final public unauthenticated REST GET fallback. It never runs `gh auth`, reads token/keychain state, or stores CLI stdout/stderr on failure. It permits only that allowlist, REST GET, and GraphQL query argv; mutations, file-backed API fields, unlisted view groups, and browser-opening flags fail before execution. Use `collect_pr.py --checkout` for complete PR evidence, diff, target/head refresh, and local checkout; it delegates its GitHub reads to `github_read.py` and continues with explicit partial-online-triage gap when supplemental review-thread evidence is unavailable. `gh pr checkout` is explicitly local-only and never remote mutation. Codex Git marketplace add/upgrade remains explicit non-`gh` lifecycle exception. Do not add collector merely to mirror GitHub resource: issues, releases, repositories, and Discussions use `github_read.py` directly. Add another collector only when named workflow needs composite validated evidence bundle or local-state operation, with its bundle contract and regression tests documented first.
- Use `PLUGIN_ROOT/shared/find-review-report.py` for path-free PR report lookup; no ad hoc JSON parsing in instructions.
- Delegation/in-main substitute passes use `PLUGIN_ROOT/shared/specialist-orchestration.md`, narrow context packs, explicit output contracts, parent consolidation.
- Put bulky skill result JSON examples in sibling `result-template.json`. In `SKILL.md`, reference it; do not embed long "Minimum artifact payload" blocks.
- Never automatically run `git`/`gh` `--force`. Stop and ask with concrete reason and overwrite risk if needed.
- Use `PLUGIN_ROOT/shared/validate-artifacts.py` for common shapes when durable notes, ledgers, JSONL exist.
- Prefer local files, `git`, `rg`, project commands, explicit citations.
- External services/browser optional; caveat when unavailable.
- Native operation requires no external-runner metadata, hidden cache, widget, slash syntax, non-Codex path variable.

## User Questions

Every user-facing question that requests a decision or missing input must show how to answer:

- Plain-text binary confirmation: append `(yes / no)`, for example `Authorize this local merge and commit? (yes / no)`. State the exact action and effects before asking. A `yes` authorizes only that presented scope; `no` leaves it unapproved.
- Closed selection: show all supported choices beside the question or immediately below it. Retain existing choice labels and accepted values; do not replace a multi-choice decision with yes/no. For indexes, ranges, or grouped selections, show the accepted syntax and a valid example.
- Free-text input: state the expected value or format, such as a report path, PR number/URL, or a short description of the missing constraint; include a neutral example when useful. Do not invent a finite menu when the input is genuinely free-form.
- Native choice or permission controls: use the actual supported options in that control. Do not add a conflicting yes/no suffix, duplicate its choices in another prompt, or imply that a chat answer bypasses runtime approval. Plain-text fallbacks must carry their own choices or input format.
- Preserve existing authorization, exact-digest binding, validation, denial, and retry rules. Do not re-ask a decision already supplied. Never treat silence, a preselected option, an example answer, or an unrelated reply as consent; clarify an ambiguous response before the dependent action while continuing unaffected authorized work.

This rule governs questions the workflow generates as well as literal prompt templates. Internal self-review questions and specialist evidence requests retain their own output contracts.

## Networked CLI Approval

Keep shell network access blocked by default. When workflow intentionally executes networked CLI, run complete owning command with runtime-approved external network access from its first attempt; wrappers and Python helpers own approval for every nested subprocess and HTTPS request. For Codex exec call, set `sandbox_permissions="require_escalated"` with narrow justification for intended remote read, download, paid run, or already-approved lifecycle action. The task authorizes requesting permission, never bypassing runtime prompt. Never enable persistent workspace network access, request broad interpreter prefix, or assume approval for standalone nested executable covers its parent command.

This contract covers every `gh` and `kaggle` invocation; collector-owned `git fetch` and public HTTPS fallback; Codex Git marketplace add/upgrade and any complete sync wrapper that owns them; and paid live calibration through `codex exec`. Local-only marketplace/plugin listing, `codex plugin add` from a configured marketplace snapshot, plugin removal, offline calibration, ordinary local `git`, and runtime web/browser/MCP/connector tools do not receive shell escalation. Missing external CLIs remain user-owned prerequisites: tell the user what must be installed and authenticated, but never install or authorize an installer from the workflow. Unknown project commands are not pre-authorized: if a selected gate or dependency command attempts network access, request approval for that exact owning command when observed.

## Approval Brief

Before every intentional approval request, give one short plugin-owned brief with these exact labels:

1. `Action and purpose`: complete owning command and why it is needed.
2. `External capability`: network, download, paid run, lifecycle, or local-checkout effect.
3. `Credential behavior`: whether existing CLI acts only as opaque credential broker.
4. `Filesystem and worktree effects`: expected local artifacts, checkout, or cache changes.
5. `Retry policy and safe denial outcome`: bounded retry rule and what safely stops or degrades.

For all intentional approval requests, keep the runtime `justification` or reason separate from the detailed pre-brief. It must be a short plain-English question about the requested outcome or material effect and must not repeat the command, argv, flags, paths, multiline content, or full approval brief. When reusable approval is justified, `prefix_rule` must be a short categorical safe prefix that grants only the intended command family, never the entire command. Omit `prefix_rule` for one-time or high-risk commands. The runtime command field remains the authoritative command display; the pre-brief remains the authoritative safety and effects explanation.

Denial aborts the active tool call and may end the assistant turn. Do not issue an equivalent approval request in the current turn. Do not switch to a broader command, enable persistent network access, or report completion. Ask the user to send a new message to resume; that new request starts a fresh decision under the documented command boundary.

## Actionable Pauses

Every workflow pause or blocker must be actionable rather than dead-end `blocked` label. The report's existing outcome, remaining, and next-action fields must explicitly provide:

- `Stopped action`: exact action and scope that stopped.
- `Cause`: concrete observed cause and evidence.
- `Rule`: governing rule or source, distinguishing actual rule from interpretation and never inventing rejection.
- `Continue`: safe work that remains allowed, including source inspection when source is accessible.
- `Next step`: each feasible recovery step and its responsible actor, such as using existing authorization first, obtaining specific approval, changing configuration/code, supplying source/evidence, or making decision.
- `Resume condition`: exact evidence or state that permits successful resumption.

Use existing authorization first and do not request redundant approval. Respect approval and retry rules after denial. Hard stops remain mandatory for unavailable source identity, unsafe execution, credential exposure or unauthorized credential access, and unauthorized destructive or remote mutations under applicable workflow rules; authorized credential-broker reads and authorized local changes continue. Discovered bugs, failing CI, or CLA/DCO gaps are merge issues and do not by themselves stop permitted source inspection. If no safe recovery exists, explain why and give human-owned alternative.

Routine preparation and safe diagnosis belong to agent. For PR, collect fresh GitHub CLI metadata, refresh required PR/target refs, use `gh pr checkout <number>` through collector, and verify resulting source before review or conflict resolution. If preparation fails, inspect retained classified errors and safe local state before asking user to act. Name actual failing operation, observed cause or explicit uncertainty, and responsible next action; never hand off only "repair the environment", "fix checkout", or "retry later". Ask only for specific missing authorization, credential action, prerequisite, protected-state decision, or unavailable evidence after permitted checks are exhausted. Do not invent reasons, expose secrets, silently weaken source identity, or retry deterministic failure unchanged.

Scope repeated-failure stop to failed operation and its dependencies, not entire user objective. Stop retrying rejected reviewer launcher, retain its ledger and unaccepted evidence, and continue accessible source inspection through permitted route. A genuinely different inspection route is not another attempt to repair failed protocol, does not reset its recurrence count, and must not reuse rejected output as accepted evidence. Use available instruction-bounded reviewers, or disclosed parent-serial review when allowed, without asking user to choose recovery that existing authorization already permits. If user explicitly requires independent coverage and no available route can supply it, complete remaining inspection and ask only for missing decision; identify exactly what remains incomplete. Never route around denied approval, unsafe execution, or missing source identity.

## Recurrence And Root-Cause Policy

Apply this fixed policy to every same or plausibly shared obstacle, including one that appears under different symptoms:

Attempts here mean attempts to resolve affected obstacle. Unaffected authorized work follows Actionable Pauses above; continuing that work neither resets count nor authorizes another attempt at stopped operation.

- Occurrence 1 is initial occurrence; capture symptom and evidence, then proceed with normal gates.
- Occurrence 2 (first recurrence) stops symptom patching. Run `investigate` or equivalent root-cause evidence before another fix attempt; record root-cause claim, supporting evidence, falsification check, and at least one rejected alternative.
- Occurrence 3 stops attempts on repeated obstacle and its dependencies, not unrelated authorized work. Ask human for specific missing decision; include attempted actions, current hypotheses/evidence, shared obstacle across differing symptoms, continuing safe work, and condition for resuming.
- Reset count only when evidence falsifies shared cause or material external-state change occurs. Record reset and its evidence.

Only recurrence-lifecycle owners link this policy directly: the `implement`, `code-remediate`, and `investigate` skills plus the `delegation-lead` role. Other skills use their own linear or bounded iteration contracts, and leaf specialists leave recurrence counting to their caller. They must not duplicate this link.

## Reasoning-Progress Escalation

Apply this separately to workstream whose decision process is stalled. It detects missing material progress, not agent's prose style, elapsed time, token count, or difficult task.

- A work cycle records one objective, chosen operation or hypothesis, observed output, and resulting next decision. Lifecycle owners persist this state as `<run-directory>/reasoning-progress.json` and run `python PLUGIN_ROOT/shared/escalation_ledger.py --ledger <run-directory>/reasoning-progress.json` before starting another cycle after either trigger. A failing validation stops workstream; it cannot be bypassed by another retry.
- Material progress is new falsifiable evidence, scope or root-cause narrowing that changes next decision, acceptance-check status change, or user-directed decision. Rephrasing, repeating semantically equivalent action, and unsupported confidence change are not progress.
- A closure condition is unchanged outcome that ends workstream: passing acceptance check, resolved decision, or user-approved scope. Each resolution attempt records its closure condition and falsifiable result.
- Either signal requires stall ledger: two consecutive cycles without material progress, or three sequential evidence-backed resolution attempts that leave same closure condition unmet. Record objective; closure condition; operations and hypotheses; observed outputs/evidence; why each attempt lacked progress or closure; current model and effort when observable; state changes; and active recurrence count.
- Pause stalled workstream and request exactly one higher-capability advisory pass when permitted route exists. Prefer one supported reasoning-effort step; otherwise use next applicable model tier under canonical routing boundaries. The advisory pass is valid only when its observed sandbox is `read-only`; it receives only ledger and necessary context, diagnoses stall, proposes one bounded recovery action plus stop condition, and does not make state changes or claim acceptance. Missing or unverified read-only routing makes advisory route unavailable.
- Parent may run one proposed bounded recovery action. If advisory route is unavailable, its recommendation is unsafe or unsupported, or recovery action has no material progress or leaves same closure condition unmet, stop and ask human. The handoff includes ledger, advisory output and route fidelity, current hypotheses, rejected alternatives, and one recommended next step with alternatives.
- An advisory pass, its recommendation, and recovery action never reset repeated-obstacle count or substitute for root-cause evidence. A closure-attempt count resets only when its condition is fulfilled or materially replaced by recorded user direction or external-state evidence. The recurrence policy can still require earlier human handoff.

## Evidence Rules

- Code claims: file/line refs. Current external: live primary source or stale/unverified caveat. Root cause: evidence, falsification, rejected alternative. Metric: baseline, guard, comparison. Release: SemVer plus changelog/migration evidence.
- Every skill/agent score uses bands: `<= 0.8` unacceptable; `0.8 < confidence < 0.85` very questionable; `0.85 <= confidence < 0.9` cautious-low; `>= 0.9` fair but not automatic.
- Skill JSON `metadata.confidence_recovery`: initial/final score, band, objective evidence, recovery, limits. Post-recovery `<= 0.8`: `confidence-not-acceptable`; `0.8 < confidence < 0.85`: `confidence-very-questionable`. Agent output has visible prose/table same fields.
- Close gaps with evidence or explicit unresolved/deferred record. Skill JSON uses `metadata.confidence_gap_closures`; agents show closure list/table.

## Calibration Hooks

Native-skill behavior changes update at least one:

- `PLUGIN_ROOT/runtime/calibration/benchmarks.json`
- `PLUGIN_ROOT/runtime/calibration/behavioral-cases.json`
- `PLUGIN_ROOT/runtime/calibration/behavioral-observations.jsonl`
- `PLUGIN_ROOT/runtime/calibration/run.py`

If intentionally no calibration update, review artifact explains why.
