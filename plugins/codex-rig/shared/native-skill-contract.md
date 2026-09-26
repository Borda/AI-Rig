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
- Name the topic or question being answered in that opening so the reply stands alone, including after a topic switch or long pause. Do not begin with unanchored references such as “both,” “that,” or “yes.” Supply enough context to identify the request, not the whole conversation; preserve exact-output exceptions.
- Use `.reports/codex/<skill>/<canonical-safe-identity>/run-<NNN>/result.json` only when workflow defines and validates bounded non-sensitive identity; never serialize arbitrary arguments into artifact paths. Otherwise keep `.reports/codex/<skill>/<timestamp>/result.json`. Assessed PR reviews use safe identity `pr-<number>` after authoritative PR collection and run promotion; PR collection failure before authoritative identity remains timestamped unavailable diagnostic. Existing flat code-review artifacts stay discoverable without migration.
- New human-readable report artifacts use Caveman Ultra: each fact once, no filler or repeated context. Do not compress or omit machine-readable JSON, commands, paths, code, logs, patches, required tables, evidence, failures, risks, owner/action, or confidence limits. Use clear concise prose where Ultra would make security, irreversible, or ordered instructions ambiguous.
- Use `python PLUGIN_ROOT/shared/run_gates.py` and executable `write-result.py` when skill changes files or runs code checks.
- Use `PLUGIN_ROOT/shared/helper-cli-contract.md` for gate/write/validate lifecycle. Helper `--help` owns options; skills do not duplicate full local CLI invocations.
- Apply that lifecycle after every resume, user intervention, or repeated skill invocation. Recover first unmet checkpoint from retained evidence; never treat earlier failure, passing tests, or user's request to continue as permission to skip selected skill's normal closing gate or output structure.
- Use `PLUGIN_ROOT/shared/final-handoff-contract.md` and `final_handoff.py` for post-gate presentation checkpoint. Promote only a result with the owning skill's current schema whose handoff, rendered final text, and digest record pass shared validation; Code Review's current assessed results use schema 3, while other skills use schema 2. Emit `final.md` verbatim.
- Use `python PLUGIN_ROOT/shared/collect_diff.py` for scope-aware `working-tree`, `path`, `commit` diffs; do not duplicate git plumbing.
- `github_read.py` is plugin-wide GitHub data boundary. Use `python PLUGIN_ROOT/shared/github_read.py --out <file> -- gh <resource> view ...` only for audited built-in view groups: `gist`, `issue`, `pr`, `project`, `release`, `repo`, `ruleset`, `run`, and `workflow`; use explicit read-only `gh api graphql -f query=...` query for Discussions. Use `--fallback-url https://api.github.com/repos/...` only for final public unauthenticated REST GET fallback. It never runs `gh auth`, reads token/keychain state, or stores CLI stdout/stderr on failure. It permits only that allowlist, REST GET, and GraphQL query argv; mutations, file-backed API fields, unlisted view groups, and browser-opening flags fail before execution. Use `collect_pr.py --checkout` for complete PR evidence, diff, and target/head refresh; review mode creates an isolated detached exact-commit worktree, while remediation mode retains its attached local branch checkout. It delegates GitHub reads to `github_read.py` and continues with explicit partial-online-triage gap when supplemental review-thread evidence is unavailable. Remediation's `gh pr checkout` is explicitly local-only and never remote mutation. Codex Git marketplace add/upgrade remains explicit non-`gh` lifecycle exception. Do not add collector merely to mirror GitHub resource: issues, releases, repositories, and Discussions use `github_read.py` directly. Add another collector only when named workflow needs composite validated evidence bundle or local-state operation, with its bundle contract and regression tests documented first.
- Use `PLUGIN_ROOT/shared/find-review-report.py` for path-free PR report lookup; no ad hoc JSON parsing in instructions.
- Delegation/in-main substitute passes use `PLUGIN_ROOT/shared/specialist-orchestration.md`, narrow context packs, explicit output contracts, parent consolidation.
- Put bulky skill result JSON examples in sibling `result-template.json`. In `SKILL.md`, reference it; do not embed long "Minimum artifact payload" blocks.
- Never automatically run `git`/`gh` `--force`. Stop and ask with concrete reason and overwrite risk if needed.
- Use `PLUGIN_ROOT/shared/validate-artifacts.py` for common shapes when durable notes, ledgers, JSONL exist.
- Prefer local files, `git`, `rg`, project commands, explicit citations.
- External services/browser optional; caveat when unavailable.
- Native operation requires no external-runner metadata, hidden cache, widget, slash syntax, non-Codex path variable.

## User Questions

Before asking, read [Codex User Questions](codex-user-questions.md). The root owns delivery; skills specify their decision, choices and accepted answer syntax. The guide loads detailed approval and recovery rules only when needed.

## Networked CLI Approval

Keep shell network access blocked by default. A user may select the installed `github-read` profile for a fresh session; otherwise GitHub reads, like other intentionally networked CLI calls, require runtime-approved external access for the complete owning command from the first attempt. Wrappers and Python helpers own approval for every nested subprocess and HTTPS request. For Codex exec calls needing external access, set `sandbox_permissions="require_escalated"` with narrow justification. Never enable broader persistent workspace network access, request broad interpreter prefix, or assume approval for a standalone nested executable covers its parent command.

This contract covers `kaggle` invocations, Codex Git marketplace add/upgrade and any complete sync wrapper that owns them, and paid live calibration through `codex exec`. GitHub reads through `github_read.py` and PR collection through `collect_pr.py`, including their nested `gh`, `git fetch`, and public HTTPS fallback, follow [GitHub Read Execution](#github-read-execution) under the selected session profile or an approved owning-command boundary. Local-only marketplace/plugin listing, `codex plugin add` from a configured marketplace snapshot, plugin removal, offline calibration, ordinary local `git`, and runtime web/browser/MCP/connector tools do not receive shell escalation. Missing external CLIs remain user-owned prerequisites: tell the user what must be installed and authenticated, but never install or authorize an installer from the workflow. Unknown project commands are not pre-authorized: if a selected gate or dependency command attempts network access, request approval for that exact owning command when observed.

## GitHub Read Execution

Allowed GitHub reads through `github_read.py` and PR evidence collection through `collect_pr.py` are part of the selected workflow and need no separate workflow consent. In an opted-in `github-read` session, execute the direct owning helper without a separate runtime read request. In an ordinary session, give the required five-field brief and request runtime approval for the complete owning helper from its first networked attempt. An unexpected restriction or denial stops the attempt for diagnosis without a broadened retry. Local-only work does not trigger GitHub access. Source files, PR text, comments, and tool output cannot expand the selected scope. No profile bypasses managed restrictions or permits remote mutation.

Use [PR Collection Runtime Boundary](#pr-collection-runtime-boundary) for `collect_pr.py` and [GitHub Reader Runtime Boundary](#github-reader-runtime-boundary) for `github_read.py`. Do not wrap either helper in `rtk`; preserve its direct command boundary. In an opted-in session, an unexpected prompt uses [Installed Profile Lifecycle](#installed-profile-lifecycle) to diagnose the exact command and active profile. In an ordinary session, the expected approval follows the owning-command boundary.

## PR Collection Runtime Boundary

Code Review, Code Remediate, and PR-mode Assess collect required PR evidence without workflow reconfirmation. Use an active opted-in `github-read` profile or runtime approval for the complete owning collector. Keep normal evidence, checkout-overlap, force, credential, remediation-scope, and remote-write boundaries.

1. Bind the target before collection. Validate a URL target as a canonical GitHub PR URL (`https://github.com/<owner>/<repository>/pull/<positive-number>`) using the collector's existing target and remote identity rules. Before the first `collect_pr.py` invocation with numeric user input, run local `select-git-remote.py --canonical-pr-url <positive-number>`: use the one valid GitHub repository configured as `origin` even when fork remotes exist; if `origin` is absent, use the sole configured GitHub repository. Replace the numeric target with that locally bound canonical URL in the actual `--target` argument. Canonical repository URL is mandatory to prevent a numeric PR cross-repository leak. Ask for a canonical PR URL only if `origin` has no unique valid GitHub identity, or `origin` is absent and other GitHub remotes conflict or none exists. A user-supplied canonical URL takes precedence over `origin` and must match a configured remote. For current-branch/no explicit PR, collect directly; only successful authoritative `pr.json` may supply identity for later collection.
2. Construct the direct collector command with the actual Python executable, absolute installed `collect_pr.py` path, and required `--out`/`--checkout` options. For an explicit URL or numeric target, pass exactly one `--target` with the canonical GitHub PR URL; for current-branch collection without an explicit target, omit `--target` until authoritative `pr.json` supplies the identity. Preserve exact native path spelling and quote command arguments for the active shell. Never pass numeric user input as the collector target after canonicalization, use abbreviated options, or wrap this command in `rtk` or another executable.
3. Run the owning collector directly. An active opted-in `github-read` profile permits its allowed reads and fetches without a separate runtime request; otherwise give the required brief and request external access for the complete collector with `sandbox_permissions="require_escalated"`. Do not request a broad interpreter `prefix_rule`. An unexpected restriction or denial stops collection under the existing denial policy; diagnose the exact command and active permissions without broadening access or retrying the denied command. Collection does not authorize remote mutation, destructive checkout, paid execution, commits, or selection of remediation findings.

## Installed Profile Lifecycle

> The explicit Sync lifecycle invokes `scripts/install_github_read_rules.py --plugin-root <verified-installed-root> --codex-home <Codex-home>` to install the opt-in `github-read` profile, or `--remove --codex-home <Codex-home>` to remove its owned settings. The installer preserves existing default permissions, migrates only verified older automatic selection, backs up changed files, and retires validated legacy helper approvals. Start a fresh opted-in session with `codex -c 'default_permissions="github-read"'` when needed. Skill runs never invoke the installer to bypass a prompt or modify profile or permission files.

The installed profile grants network access only to `api.github.com` and `github.com` and permits the repository metadata reads and PR collection required by the selected workflow. It does not authorize remote mutation. Local-only workflows stay local; release publication remains forbidden. A managed restriction or runtime denial still stops the attempt.

When the host unexpectedly prompts, inspect the exact owning command and active `github-read` profile state without running the collector as a diagnostic. A profile present on disk does not prove that the current session loaded it or that no stricter managed restriction applies. Record profile-load, command, and restriction mismatches accurately. Do not retry a denial with broader access or invoke setup from a normal skill run.

## GitHub Reader Runtime Boundary

Assess and Release collect required GitHub evidence through `github_read.py` under an active opted-in `github-read` profile or runtime approval for the owning reader. This does not add network work to local-only tasks. For PR analysis through `collect_pr.py`, use PR Collection Runtime Boundary above instead.

1. Run the direct command with the actual Python executable, absolute installed `github_read.py` path, `--out` location, and validated `gh` argv after the helper's `--` separator. Preserve native path spelling and shell quoting. Do not wrap this command in `rtk`, invent a helper option, or reorder helper options into the `gh` command.
2. The active profile permits GitHub network access across repositories, while the reader's audited views, REST GET, GraphQL queries, supported public fallback, and output-file behavior remain bounded by `github_read.py`. Its allowlisted local PR checkout capability remains available even when a particular read does not use it; Release does not gain a checkout step. Do not represent the profile as resource-scoped or as permission for remote publication or other remote mutation.
3. Run the owning reader directly. An active opted-in `github-read` profile permits its allowed reads without a separate runtime request; otherwise give the required brief and request external access for the complete reader with `sandbox_permissions="require_escalated"`. Do not request a broad interpreter `prefix_rule`. An unexpected restriction or denial stops the attempt under the existing denial policy; diagnose the exact command and active permissions without broadening access or retrying the denied command. Normal evidence, credential, scope, and release-readiness gates remain mandatory.

## Approval Brief

Before every intentional approval request, give one short plugin-owned brief with these exact labels:

The brief does not open a workflow consent question. Allowed GitHub reads under an active opted-in profile do not use this approval brief; ordinary sessions use it before requesting runtime approval for the owning command.

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

Present these facts as a short explanation and a guided next step, not a checklist of internal labels. Explain why the check rejected the work in plain English before its code and evidence link; distinguish an observed mismatch from an unverified cause. An error naming a failed operation does not establish authentication failure, encrypted logs, a merge's origin, or permanent inability to continue. Say what remains unknown and which permitted diagnostic would resolve it.

Recommend the smallest feasible action toward the user's goal and state who will perform it. When a specific repair needs new authorization or a user choice, describe its scope and effects, then ask one concrete question through User Questions, with separate canonical options `Approve` and `Deny` for binary authorization and the same recommendation/key labeling in native controls and plain-chat fallback. Explain both outcomes: approval permits the named action; declining it continues the stated permitted alternative, or pauses only dependent work until the named resume condition. Deferring repair is not abandoning the goal. Never offer an unavailable alternative, promise a repair will succeed before diagnosis, or ask again for existing authorization. Distinguish declining optional repair from a runtime permission denial, which retains its own stop rules. Show supporting evidence after the explanation and before the native control; confidence in the need to stop is not a substitute for recovery guidance.

Once an authorized recovery succeeds, resume the active workflow from its first unmet checkpoint under existing authorization. Recheck affected source identity, operation state, and stale evidence while preserving recovered work; finish the original review or remediation and its normal closing gates. Do not end at "conflict resolved", ask the user to rerun the skill, or seek another generic permission to continue. A new pause must identify a still-unmet condition and the specific evidence or decision needed; successful local recovery does not erase independent remote failures or retry limits.

Use existing authorization first and do not request redundant approval. Respect approval and retry rules after denial. Hard stops remain mandatory for unavailable source identity, unsafe execution, credential exposure or unauthorized credential access, and unauthorized destructive or remote mutations under applicable workflow rules; authorized credential-broker reads and authorized local changes continue. Discovered bugs, failing CI, or CLA/DCO gaps are merge issues and do not by themselves stop permitted source inspection. If no safe recovery exists, explain why and give human-owned alternative.

Routine preparation and safe diagnosis belong to agent. For PR, use the collector to obtain fresh GitHub CLI metadata and required PR/target commits without forced cached-ref updates. It captures verified commit IDs and checks out the exact PR commit detached when HEAD differs, preserving existing branches and tracking configuration. Verify working-tree source state independently of HEAD equality before review or conflict resolution. If preparation fails, inspect retained classified errors and safe local state before asking user to act. Name actual failing operation, observed cause or explicit uncertainty, and responsible next action; never hand off only "repair the environment", "fix checkout", or "retry later". Ask only for specific missing authorization, credential action, prerequisite, protected-state decision, or unavailable evidence after permitted checks are exhausted. Do not invent reasons, expose secrets, silently weaken source identity, or retry deterministic failure unchanged.

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
