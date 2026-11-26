<!-- file: consolidator-prompt.md — consumers: plugins/cc_oss/skills/review/SKILL.md -->

<!-- policy-sibling: plugins/cc_oss/skills/review/modes/post-agent.md (Step 5 readiness rule: null-judgment sentence in the reviewer attribution list) -->

**Reviewer attribution (additive):** Preserve the aggregate prose summary, existing metadata fields, overall verdict, confidence and detailed sections.

- Add `Reviewers:` immediately after `Readiness:`, preserving template order `Agents:`, `Review contract:`, `Rating scale:`, `Readiness:`, `Reviewers:`, `CI:`. List actual readable role names and their scoped rating, for example `Software engineer [3], QA specialist [2], Documentation reviewer [1].`
- Use square brackets for each score: `Role [rating]`; a null rating renders as `Role [no source judgment]`.
- Ratings: 1 Approve, 2 Minor changes, 3 Major revision, 4 Insufficient, but a good initiative, 5 Block (source-5-v2); null means no source judgment, never rejection.
- Use each reviewer's explicit source-5-v2 rating and rationale. Completed undecidable output has null judgment/rating with blocked readiness and inspected coverage; missing output remains a distinct coverage obligation in review-validation.json.
- A reviewer that produced no output is not rated — record it as a missing-reviewer limitation instead.
- Display role names only in reviewer lists and finding authors; omit execution-origin labels and skipped roles.
- Never average ratings or replace the overall verdict.
- Retain all contributing roles when deduplicating.
- Add the template's findings overview with `ID | Author | Finding | Resolution proposal | Status`; Author lists all originating reviewer roles, not the consolidator or PR author.
- Preserve existing detailed sections and cross-reference stable IDs.
- Immediately after the closing metadata delimiter write exactly: `Legend: 1 = Approve · 2 = Minor changes · 3 = Major revision · 4 = Insufficient, but a good initiative · 5 = Block.`
- In terminal output the legend belongs immediately below the header table.
- Terminal gate rejection retains its existing behavior; use `Reviewers: Not assessed` when no reviewer assessed the source.

**Coverage reconciliation:** Read `<REPORT_DIR>/lineup.md` for a fresh publication that reached fan-out. The publication gate checks the report against the Step 2 record. Missing assessments remain in the sidecar even when the `Reviewers:` header omits them.

- For every `spawned` row, including promoted and merged units, reconcile each exact `roles:` name to exactly one `review-validation.json` assessment. Record `presence: completed` with the role's evidence when it assessed its scope. A timeout or missing assessment output requires `presence: missing`, null judgment and rating, and `readiness: incomplete`. Reconcile roles individually when only part of a merged spawn returned. Aggregate Readiness is `incomplete` whenever a required role is missing.
- Each `promoted-unslotted` unit is a missing required reviewer: add one assessment per exact name in its `roles:` list with `presence: missing`, null judgment and `readiness: incomplete`, so aggregate Readiness is `incomplete`.
- Each `dropped` unit (cut by `FANOUT_MAX` with no promotion trigger) is a cost decision. Its disclosure starts with `Unreviewed dimension: <unit> — dropped by FANOUT_MAX` and carries the generated full-review command. Readiness is unchanged and the dropped-unit confidence ceiling applies.
- Copy the `Unreviewed dimension:` lines from the parent's `RERUN_HINTS` into `Review Confidence` verbatim. The commands use report paths relative to the original checkout. Never write or repair `lineup.md`, `fill-gaps.json`, `head-sha.txt` or `$RUN_DIR/report-dir.txt`. The directory containing the plan's `prior_report` is read-only, including all files under it. If the parent evidence or its generated hints are missing or unreadable, return the missing input's path to the parent.

## Consolidation sequence

1. Read all finding files in `$RUN_DIR/`: `foundry--sw-engineer.md`, `foundry--qa-specialist.md`, `foundry--perf-optimizer.md`, `foundry--doc-scribe.md`, `foundry--linting-expert.md`, `foundry--solution-architect.md`, and `foundry--challenger.md`, `foundry--blind-solve.md`, `foundry--codex.md`, `codex-review-reply.json`, `review-env.json`, `claims.json`, `claims-result.json` if present, and `<REPORT_DIR>/lineup.md`. Skip missing files only as permitted by SKILL.md's coverage rules.

> Read the three `conversation-*.json` datasets and `conversation-coverage.txt` from `CONVERSATION_SNAPSHOT`. Apply `<_OSS_SHARED>/github-review-parsing.md` and reconcile the discussion with the frozen reviewed commit. Record each unavailable source under Review Confidence. An unavailable or missing binding, or an unreadable coverage record, is a conversation coverage limit to name there.

2. Load `<REVIEW_SKILL_DIR>/checklist.md` via `cat` (not Read tool — version-pinned cache path). Apply its signal-to-noise filter, annotation completeness and section caps.
3. Load `<_OSS_SHARED>/review-section-taxonomy.md` via `cat` for canonical headers and agent-to-section ownership. Load `<REVIEW_SKILL_DIR>/modes/verdict-states.md` via `cat` for the Gate × Outcome × Readiness truth table and the confidence ceilings; never publish a state it lists as impossible — write the state its `Write instead` column names.
4. Read the parent's validated candidates and assessments from `$RUN_DIR/cross-validation.md`. Include only parent-CONFIRMED candidates at every severity; critical/blocking findings additionally require independent verification. New or materially strengthened merged claims return to the parent for validation before publication.
5. A late source REJECT cannot be published with incoming PASS/BLOCK. Return only `terminal-ground=<GROUND> | evidence=<path>` pointing at the existing documented binding conflict; publish nothing. The parent validates the ground and routes through Stage 1's terminal publisher with original reviewed SHA and provenance, reusing applicable conduct/license confirmation. If no evidenced terminal ground exists, choose MINOR_EDITS or MAJOR_REVISION for fixable defects according to evidenced impact and repair scope; unfinished validation independently keeps readiness incomplete/blocked.
6. Apply the challenger, design-comparison, issue-alignment and filtering rules below. Write evidence/finding sidecars before the report, then return only the specified summary.
7. A `FILL_GAPS_PLAN=` binding makes this a fill-gaps run: merge per §Fill-gaps carry-over before writing any sidecar.

## Fill-gaps carry-over

Commit provenance follows the selected run's exact `head_sha`. A full SHA is shown unchanged. A legacy abbreviated identity is never expanded from the current PR head or invoking checkout: write `Commit SHA: unavailable (legacy abbreviated reviewed identity: <head_sha>)` and preserve the exact carry identity in the sidecars.

Only with a `FILL_GAPS_PLAN=<path>` binding. Read that JSON with the Read tool: `prior_report`, `prior_validation`, `prior_findings`, `head_sha`, re-run `roles`, re-validated `candidates`, `publish_rows` (carried CONFIRMED candidates whose findings row the prior run lost), and `carried` (assessment roles, candidate ids, open-concern candidate ids, finding ids kept from the prior run). This run's `$RUN_DIR` holds files only for the re-run roles; `cross-validation.md` holds only re-validated and new candidates.

- `review-validation.json`: copy every carried assessment, candidate and open concern from `prior_validation` **verbatim** — same keys, same values, never re-judged or re-worded. Add one assessment per re-run role from this run's files (no output → `presence: missing`), and the parent's records for re-validated and new candidates. Recompute aggregate `source_outcome`, `source_rating`, `readiness` and `readiness_reason` over the merged set per the protocol. A carried open candidate lacking its open concern gains one — additions are allowed, alterations are not.
- `findings.jsonl`: copy every carried row from `prior_findings` verbatim, then add rows for re-validated or new CONFIRMED candidates and one row per `publish_rows` id (fields from the carried candidate and its finding text in `prior_report`; `contract_version: 1`, `validation_status: CONFIRMED`, `candidate_id` = that id). Additions are allowed, alterations are not. A carried CONFIRMED candidate never lacks its row here: the plan refuses that carry until the id is named with `--candidates`. Run `mint_finding_ids.py` with `--carry-from "<prior_findings>"` (Findings JSONL step 2); never hand-write ids; a carried row keeps its id.
- Report body: carried findings keep their detailed text from `prior_report`. Report-only sections no agent re-ran (OSS Checks, Issue Root Cause Alignment, Design Divergence) are carried from `prior_report`. `Agents:` lists this run's agents, then `carried from <prior run dir name>`. Add one `Review Confidence` line: `Fill-gaps run: completes <prior_report>; re-ran <roles or none>; re-validated <candidates or none>.`
- The parent checks every carried record against the prior run after you return; drift sends the publication back.

## Challenger and design comparison

| Challenger section | Review severity |
| -- | -- |
| Blockers | `critical` / `high`, subject to parent validation and additional independent critical/blocking verification. |
| Concerns | `medium` |
| Nitpicks | `low` |

Never discard challenger candidates from validation; retain uncertainty separately from confirmed findings.

When `foundry--blind-solve.md` exists:

- Compare its blueprint against the diff and other agents' findings.
- Add `### Design Divergence`: taxonomy row exists, report-only, no resolve owner, exempt from section caps.
- List differences from the PR's approach, ranked by impact.
- For each divergence distinguish a genuine miss from context the blind-solve agent may have lacked (prior decision, incident, constraint); phrase the latter as a question for the author.
- Cross-reference divergences already covered by another finding; do not duplicate them.

No `foundry--blind-solve.md`: omit the section entirely, with no placeholder.

**Filtering rules:**

- Precision gate: only include findings with concrete, actionable location (function, line range, or variable name).
- Finding density: modules under 100 lines → aim ≤10 total findings.
- Root-cause merge: candidates sharing one root cause and one fix (e.g. a missing cleanup step that surfaces as both a wrong count and a capacity leak) → the report shows one finding that lists every symptom `file:line`; split only when the fixes differ. The surviving candidate is the highest-severity one (ties → first in report order). Each merged CONFIRMED candidate keeps its own `findings.jsonl` row, placed right after the survivor's row, carrying `merged_into: <surviving row's candidate_id>` with its own validated `severity` and `section`; it keeps its own `full_text` (where the row schema has one) as the symptom bullet the surviving finding lists. The surviving row has no `merged_into`. Resolve's surviving item takes the group's highest severity and becomes `[report][req]` when any member alone would be; an annotated GitHub item keeps its existing `type`. Report and envelope finding counts exclude `merged_into` rows — merging deduplicates the report, never discards a confirmed candidate. Consumers: `/oss:resolve` (requires `oss` plugin) folds `merged_into` rows into the survivor's action item; a Prior resolution ledger, when the prompt carries one, matches a `merged_into` row through its survivor.
- Ranking: place every `[blocking]`-tagged finding in `### [blocking] Critical`; within each section, order by severity (critical > high > medium > low).
- Codex deduplication: include `foundry--codex.md` unique findings under `### Codex Co-Review`; same file:line raised by both agent and Codex → keep agent version, mark 'also flagged by Codex'.

**Issue alignment (when `issue-*.md` files exist in `$RUN_DIR`):** Include `### Issue Root Cause Alignment` section placed immediately after `### [blocking] Critical`. Per linked issue: state root cause hypothesis, whether PR addresses it (yes / partially / no), whether PR description diverges from issue's stated problem, whether reproduction scenario tested. Any `root cause misalignment` or `scope divergence` finding at least HIGH severity. **PR description drift**: Before flagging `scope divergence`, cross-check PR thread and review comments to determine what was actually agreed; description diverging from *thread consensus* is signal worth flagging.

**Intended behavior (always):** open the aggregate summary with `Intended behavior: <verdict> — <basis>` copied from the first line of `foundry--sw-engineer.md`. Verdict `no` or `partly` → its `intent:` findings stay at least HIGH. Line missing (agent skipped or failed) → `undetermined` plus a `Review Confidence` entry naming the gap. Never infer `yes` from PR text.

## Report header

The file MUST begin with the `---`-delimited YAML metadata block from `<REVIEW_SKILL_DIR>/templates/review-report.md`: opening `---` on line 1, all template fields in order (including `Review contract:`, `Rating scale:`, `Readiness:`), closing `---`, then body. Do NOT encode the head as HTML comments (`<!-- ... -->`) or any other form; the orchestrator reads the block verbatim as the reply header. Missing `---` head breaks terminal output.

After the `Legend:` line, write exactly this line as its own paragraph, so a reader never takes `Gate: REVIEW_V1_PASS` beside a revision outcome for a contradiction: `Axes: Gate = CI merge gate at the reviewed head, or a premise rejection · Outcome = source judgment · Readiness = verification state; each axis is read on its own.`

The parent supplies resolved literal input bindings in its compact Agent prompt. Interpret placeholders using those values; never require the parent to read, substitute, cat, or inline this file body. `$RUN_DIR` is self-resolved by the prepended run-dir preamble.

| Field | Value or rule |
| -- | -- |
| `Title:` | `oss-review — [PR #N title]` |
| `PR:` | `#<PR_NUMBER>`; omit entirely when `<PR_NUMBER>` is empty (direct-path mode has no PR). |
| `Link:` | Literal `<PR_URL>` as one bare `https://` URL, so the delivered header table is clickable; when `<PR_URL>` is empty, `https://github.com/<owner>/<repo>/pull/<PR_NUMBER>` from the prompt's first line; omit entirely when `<PR_NUMBER>` is empty. |
| `Date:` | `<DATE>` |
| `Commit SHA:` | Literal `<REVIEWED_COMMIT_SHA>`: full frozen `pr-meta.json` `headRefOid` for an ordinary review, or the selected fill-gaps run's full `head_sha`. Never abbreviate or use the invoking checkout's HEAD. Missing identity and legacy abbreviated identity remain explicitly unavailable. |
| `PR Type:` | Classify diff INTENT, never title/file-count: `fix` / `feat` / `refactor` / `perf` / `docs` / `ci` / `chore` / `test` / `mixed`. |
| `Scope:` | Key changed files from `<CHANGED_FILES>`; skip test files if >3 source; cap ~5. |
| `Focus:` | `<SCOPE>` — one-line description from diff + PR body. |
| `Impact:` | Literal `<IMPACT>`: `FULL · <reason>` or `LIGHT · <reason>`; depth follows code path, not diff size. |
| `Agents:` | Short names of agents with output files in `$RUN_DIR/`. |
| `Reviewers:` | Actual scoped assessments per the additive attribution rules above. |
| `CI:` | Copy `<CI_STATUS>` verbatim from the final observation. Derive the source outcome and actionable findings independently from validated source evidence. |
| `Review contract:` / `Rating scale:` | `1` / `source-5-v2` |
| `Readiness:` | `complete`, `blocked`, or `incomplete`, matching the envelope. |
| `Gate:` | `REVIEW_V1_<GATE>` (`REVIEW_V1_PASS` or `REVIEW_V1_BLOCK` here; terminal reject-gate reports bypass this consolidator). The CI merge gate: PASS means no current CI failure was established for this gate. Keep pending and unavailable CI visible in its own header. Judge source Outcome from the validated findings, never from this field. |
| `Outcome:` | APPROVE / MINOR_EDITS / MAJOR_REVISION / INSUFFICIENT_BUT_PROMISING / REJECT / literal null; token only, optional leading `✓`/`⚠`/`✗`. Put severity counts/detail in Summary, never after the token. |
| `Summary:` | 1–2 sentences. |
| `Confidence:` | Aggregate score — key gaps. |
| `Next steps:` | Blockers first, max 5. |
| `Path:` | `→ <REPORT_DIR>/review-report.md` |

**Severity tiers:** Every finding must carry an explicit inline severity label: `[cosmetic]`, `[low]`, `[medium]`, `[high]`, or `[critical]`. Cosmetic findings go in the dedicated `### Cosmetic / Style` section — never interleaved with behavioural findings.

**Confidence parsing:** Parse each dimension agent's `confidence` from its JSON envelope. The Codex lane is a static, read-only co-review: take its confidence only from a numeric `confidence` between 0 and 1 on the bridge reply (`codex-review-reply.json`) whose `status` is `complete`; the bridge derives that number from the peer's own `confidence_gaps` lines and applies no ceiling, so this report's ceilings still apply on top. Carry each `confidence_gaps` line of that reply (`(-0.NN) <limit>`) into the `## Confidence` **Gaps** as a Codex lane gap, deduction first: `- (-0.NN) Codex: <limit>`. A reply whose `confidence` is `null` gets the `Review Confidence` row `— | <confidence_null_reason>` and stays out of min, median and the aggregate. A null, failed, timed-out, quota-blocked or blocked Codex reply never contributes a number: leave it out the same way and name it once in the `## Confidence` **Gaps** as `Codex co-review <status>: <confidence_null_reason>`, the bridge's closed-set code (a stored reply without the field reads `not-recorded`). No reply: `foundry--codex.md` reading `⊘ codex not run: peer-unavailable …` (bridge absent) → `Codex co-review not run: peer-unavailable`; `⊘ codex-blocked:quota …` → `Codex co-review not run: quota-exhausted`. A completed Codex reply whose `verdict` string in `codex-review-reply.json` ends with `source-5-v2 rating: <1-5> — <rationale>` (the Step 2 bridge request) supplies the Codex co-reviewer's scoped rating and judgment; read it from `verdict`, never from the transcript (the peer's schema-bound output never ends with that line), and a verdict without that final line is no rating. `DOCS_ONLY_MODE=true` → the report's `## Confidence` **Gaps** carry `- (-0.00) full test suite not run — documentation-only change` once; weight 0 — never lowers the aggregate, never a finding, `Next steps` item or low-confidence banner, and any reviewer gap asking for a test run folds into it.

**Missing-package gaps:** copy every agent's `### Ephemeral dependency evidence` line verbatim into `Review Confidence`. Such a line closes the same package gap raised by any other reviewer (Codex "not installed", perf "nothing measured") — cite it as the closure, never carry that gap into the aggregate score or `Confidence:` header. A gap naming only "<pkg> not installed / not importable", with neither an evidence line nor a failed-step reason, stays open: append `ephemeral-install procedure not attempted` and add the overlay run to `Next steps`.

**Reviewer environment:** read `$RUN_DIR/review-env.json` (`modes/provision-env.md`). Under `### Review Confidence` write one `**Environment**:` line: status, interpreter, installed/gated/failed counts, and host as `<os> <native_arch> <cpu_model>, <cpu_count> cores, <memory_bytes as GiB> GiB, accelerators <accelerators or none>`. A package in `installed[]` closes every "not installed" gap raised for it; one in `failed[]` keeps its gap with its `cause` and first `error_tail` line; one in `skipped_gated[]` is a contribution finding `<pkg>: gated (<gate_reason>)`. The host class is a fact of this run, never a gap or a confidence deduction. Only an OS or accelerator class that a claim or a PR code path requires and this host lacks is written, once, as `not reproducible on this host: requires <CLASS>; …` with its probe evidence; on a host of the required class nothing about the host is written. File absent on a code PR → `**Environment**: not provisioned — no review-env.json`, and each package gap follows the missing-package rule above.

**Author claims:** read `$RUN_DIR/claims.json` and `$RUN_DIR/claims-result.json` (`modes/claim-repro.md`). Write one `**Author claim**:` line per claim: `<claim_text> — <verdict> · Δ <relative_delta> · spread <spread> · <comparable_hardware.decision> · <rounds_completed>/<method.repeats> rounds, <method.execution> · <reason>` (`rounds_completed` is the top-level field of `claims-result.json`, `method.repeats` the requested rounds). `not-run` claims are execution gaps with their reason, never `not-reproduced`. `hardware-unavailable` names exactly the class this run's host lacks (`not reproducible on this host: …`) and nothing broader. Never restate the author's absolute numbers as reproduced. `claims.json` lists claims but `claims-result.json` is absent → every claim is `not-run` with reason `claim reproduction not run`, never omitted. No `claims.json`, or zero claims → no line.

**Confidence ceilings:** the aggregate score in `Confidence:` and on the `**Aggregate**` line is the median of the numeric reviewer confidences, never above the lowest ceiling in `verdict-states.md` §Confidence ceilings that applies (Readiness `incomplete` or `blocked`, PR code not executed on a code PR, a unit dropped by `FANOUT_MAX`). Name the binding ceiling first among the `Confidence:` key gaps. Ceilings only: no floor, no variance target, no stacking. The publication gate refuses a `Confidence:` above its Readiness ceiling.

**Gaps never carried into the score or `Confidence:` header:**

- A reviewer gap naming its own skipped Codex cross-check (`--no-codex`) while `foundry--codex.md` holds a completed Codex reply: drop it, the review's Codex lane covered that pass. Without a completed Codex lane, the one Codex gap above names it instead.
- The reviewer host's operating system (whatever host class it is, recorded per run): never a gap by itself. The host class is recorded once per run on the `**Environment**:` line; only a class that a claim or PR code path requires and this host lacks is written, as `not reproducible on this host: requires <CLASS>`.
- A missing Python package or another Python version: provisioned for the review, never a gap by itself; a bare one is flagged `ephemeral-install procedure not attempted` as above, and only a failed provisioning step with its concrete reason stays a gap.
- An absent hardware class (CUDA on a host without an NVIDIA GPU, another accelerator): a stated limit naming exactly that class and the paths it leaves unexecuted, nothing broader. It lowers confidence only through Readiness, when a candidate depends on it.
- A closure (a gap another reviewer's evidence closed) is cited under `Review Confidence`, never listed as a `(-0.00)` **Gaps** line; the documentation-only line above is the one fixed exception.

**Findings sidecar — write it BEFORE the report.** `oss:resolve` and later re-reviews read this file instead of re-parsing the report, so it carries every actionable finding the report will show, with nothing dropped and nothing extra beyond root-cause-merged `merged_into` rows:

1. Write `<REPORT_DIR>/findings.jsonl` with the Write tool, one compact JSON object per finding in report order. Fields:
   - `section`: the canonical section header from the taxonomy (e.g. `### Architecture & Quality`)
   - `severity`: `critical`, `high`, `medium`, `low` or `cosmetic`
   - `title`: one-line finding name
   - `full_text`: the finding bullet as the report prints it
   - `required_change`: the resolution proposal
   - `file`, `line`: location, `""` / `null` when none
   - `change`, `author`: the taxonomy's resolve `change` and Owner agent columns for that section; for `### [blocking] Critical`, use the originating agent and the `change` matching its domain
   - `verify_verdict`: `CONFIRMED` only when a Step 4 verifier confirmed exactly this finding, else `""`. A finding you merged from several agents' claims gets `""` unless the verifier confirmed every merged claim, because `/oss:resolve` skips re-proving a confirmed finding
   - `verify_file`: that verifier's `$RUN_DIR/verify-*.md` path, else `""`
   - `source_file`: the originating agent file in `$RUN_DIR`
   - `codex_eligible`: `true` when the finding meets the "good fit" criteria in `<_OSS_SHARED>/codex-delegation.md` (load via `cat`) and none of its "don't delegate" cases, else `false`. Criteria only — never invoke the bridge or edit code; `/oss:resolve` decides who implements.
   - Report-only sections (`Design Divergence`, `Issue Root Cause Alignment`, `OSS Checks`, `Recommended Next Steps`, `Review Confidence`) are not written here.
2. Run `python "<REVIEW_SKILL_DIR>/../../bin/mint_finding_ids.py" "<REPORT_DIR>/findings.jsonl"`. It adds a stable `id` to every record (section slug + hash of file and title, no line number, so the id survives line drift between runs). A non-zero exit means a record lacks a required field; fix the record and re-run, never hand-write ids. In a fill-gaps run append `--carry-from "<prior_findings>"` (the plan's `prior_findings` path): every carried row keeps its id verbatim, and a new row never takes a carried row's id.
3. Use those minted `id` values as the findings-overview `ID` column and prefix each detailed-section bullet with `[<id>]`.

**Prior resolution ledger** — only when the orchestrator's prompt carries a `Prior resolution ledger:` line: taking only the last record per `finding_id` (later lines are later resolve runs), for every finding whose last verdict is `rejected` and that this report does not list again, add one line under `Review Confidence` naming its title, file and the ledger's `why`. A rejected finding left out silently is indistinguishable from one nobody re-checked.

**Write to:** `<REPORT_DIR>/review-report.md` using Write tool.

**Source Files footnote**: after the `## Confidence` block, append `## Source Files` section. Use `Glob(pattern="*.md", path="$RUN_DIR")` to list every handover file present — lets reviewers locate raw subagent outputs without knowing the run timestamp. `$RUN_DIR` is consolidator's self-resolved run-dir (from run-dir preamble: `cat "${TMPDIR:-/tmp}/oss-review-run-dir-${CSID}"`) — not orchestrator-substituted.

**Gap ledger — after the report is written.** Load `<_OSS_SHARED>/gap-emission.md` via `cat` and Read `<REPORT_DIR>/gap-prepare.txt` (Step 2's prepare output: stage path, carried gaps, known limits). Write this run's gap records to `<REPORT_DIR>/gaps.jsonl.rec` with the Write tool (staged `.rec`, gap fields only). For a carried gap still open, stage nothing and show it in the report as `carried from <run id>`. When current evidence closes a carried gap, stage the same ID and text with `"status": "closed"`, zero or null deduction and the closing evidence path. Then run `python "<REVIEW_SKILL_DIR>/../../bin/gap_emit.py" append --dir "<REPORT_DIR>"` to fill the run identity and append through `gap_ledger.py`. Exit 1 → correct the named records and run it once more. Name a second refusal or a `⚠ GAP_LEDGER=unavailable` line once under `Review Confidence` and continue publication.

**Return ONLY** the one-line publication summary: `verdict=<APPROVE|MINOR_EDITS|MAJOR_REVISION|INSUFFICIENT_BUT_PROMISING|null> | findings=N | critical=N | high=N | file=<REPORT_DIR>/review-report.md`. A late terminal conflict instead returns the step 5 routing signal without publication.

## Publication validation

Preserve a supported source judgment when unavailable tooling, runtime evidence or independent verification blocks readiness. Such limits do not alone force source_outcome/source_rating to null; null requires source evidence insufficient to judge. Record missing independence as an open validation obligation, never a confirmed finding. Keep missing reviewer coverage, feasible undone checks, actual failed gates and required checks visible; complete feasible authorized work before delivery, and never claim unconditional approval or merge readiness while blocked/incomplete.

1. Follow the loaded candidate-validation protocol and the parent's evidence in `$RUN_DIR/cross-validation.md`; never infer confirmation from confidence or severity. New or materially strengthened claims require parent validation before publication.
2. Write `<REPORT_DIR>/review-validation.json`: contract_version=1, rating_scale=source-5-v2, aggregate source_outcome/source_rating/readiness/readiness_reason, returned/missing assessments (evidence and limits as string arrays), all candidates, durable open_concerns (one per UNVERIFIED/null candidate only; any such candidate forbids readiness=complete). Aggregate readiness=complete only when every assessment is complete and open_concerns is empty. A missing required role (presence=missing) has readiness=incomplete; it, like any null candidate, makes the aggregate incomplete, overriding blocked, also under a null aggregate outcome. Every findings row carries id, section, severity, title, change and author; the publication gate refuses a row without them. Header fields mirror this envelope.
3. `findings.jsonl` contains every confirmed actionable candidate exactly once; each row carries contract_version=1, validation_status=CONFIRMED and candidate_id referring to its matching CONFIRMED candidate. Preserve existing minted finding IDs separately. Never put unverified concerns in this collection.
4. Report Open Concerns names each obligation ID/owner/next_check/closure. Feasible undone checks mean incomplete; actual unavailable prerequisites after feasible checks mean blocked. Neither allows unconditional approval or a merge-readiness claim.
5. Null is absence of source judgment with returned assessment evidence, never rejection. Source REJECT requires documented binding conflict and an evidenced terminal ground; fixable defects mean MINOR_EDITS or MAJOR_REVISION according to evidenced impact and repair scope. INSUFFICIENT_BUT_PROMISING means a good initiative with substantially incomplete source implementation, never missing hardware/process evidence. Historical reports retain their original five-rating meanings.
6. Every UNVERIFIED candidate carries `missing_capability_kind`: `hardware` (an absent hardware class), `package` (a Python package, tool or Python version the review environment can provision), `approval` (a decision only the user or a maintainer can make, such as running fork-PR code or a GitHub write) or `external` (state outside the review host: a CI run, a hosted service, network access, private data or weights). A `package` candidate stays UNVERIFIED only with `provisioning_failure` naming the failed provisioning step and its concrete reason; otherwise provision and run the check, or record the candidate `null` (feasible check undone, readiness incomplete). Readiness `blocked` caused only by provisionable tooling is impossible (`verdict-states.md`). A package `$RUN_DIR/review-env.json` lists in `failed[]` or `skipped_gated[]` is kind `package` with `provisioning_failure` copied from its cause (`<cause>: <first error_tail line>`, or `gated: <gate_reason>`); a claim or check that cannot run because this host lacks a hardware class (`hardware-unavailable`) is kind `hardware`. A fill-gaps run copies carried candidates verbatim and never adds these keys to one; only re-validated and new candidates carry them. The publication gate (`--producer`) refuses every UNVERIFIED candidate without the key, a candidate carried verbatim by a fill-gaps plan excepted, so never omit it to avoid the `package` rule.
7. `APPROVE` never stands beside a CONFIRMED `critical` or `high` finding: that finding is a required source change, so the outcome is `MINOR_EDITS` or `MAJOR_REVISION` by evidenced impact. `APPROVE` beside `blocked` or `incomplete` readiness is valid: no required source change, verification unfinished; Summary and Next steps then state the open verification, never an unconditional approval or merge-readiness claim.
