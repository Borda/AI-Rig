<!-- file: consolidator-prompt.md — consumers: review/SKILL.md §Step 5 -->

**Reviewer attribution (additive):** Preserve the aggregate prose summary, existing metadata fields, overall verdict, confidence and detailed sections.

- Add `Reviewers:` immediately after `Agents:`, listing actual readable role names and their scoped rating, for example `Software engineer [3], QA specialist [2], Documentation reviewer [1].`
- Use square brackets for each score: `Role [rating]`; a null rating renders as `Role [no source judgment]`.
- Ratings: 1 Approve, 2 Minor changes, 3 Major revision, 4 Insufficient, but a good initiative, 5 Block (source-5-v1); null means no source judgment, never rejection.
- Use each reviewer's explicit source-5-v1 rating and rationale. Completed undecidable output has null judgment/rating with blocked readiness and inspected coverage; missing output remains a distinct coverage obligation in review-validation.json.
- A reviewer that produced no output is not rated — record it as a missing-reviewer limitation instead.
- Display role names only in reviewer lists and finding authors; omit execution-origin labels and skipped roles.
- Never average ratings or replace the overall verdict.
- Retain all contributing roles when deduplicating.
- Add the template's findings overview with `ID | Author | Finding | Resolution proposal | Status`; Author lists all originating reviewer roles, not the consolidator or PR author.
- Preserve existing detailed sections and cross-reference stable IDs.
- Immediately after the closing metadata delimiter write exactly: `Legend: 1 = Approve · 2 = Minor changes · 3 = Major revision · 4 = Insufficient, but a good initiative · 5 = Block.`
- In terminal output the legend belongs immediately below the header table.
- Terminal gate rejection retains its existing behavior; use `Reviewers: Not assessed` when no reviewer assessed the source.

# Consolidator Agent Prompt — develop:review

The parent supplies these resolved literal input bindings in the compact Agent prompt. Interpret placeholders using those values; never require the parent to read, substitute, cat, or inline this file body:

- $REPORT_DIR_LITERAL — report directory path (resolved in Step 2)
- $DATE — from `date +%Y-%m-%d` output
- $REVIEW_CHECKLIST — resolved checklist path or empty string
- $REVIEW_TEMPLATE — resolved review-report.md template path (Step 5); consolidator reads it itself — never embed the template body in this prompt (double-bills its tokens)

`$RUN_DIR` is the exception — do NOT substitute it. Run-dir preamble (placed after the task line by SKILL.md §Step 5) makes consolidator self-resolve `$RUN_DIR` via `IFS= read -r RUN_DIR < "${TMPDIR:-/tmp}/dev-review-run-dir-${CSID}"`; leave `$RUN_DIR` literal in prompt text.

## Consolidation sequence

1. Read all finding files in `$RUN_DIR/`: `sw-engineer.md`, `qa-specialist.md`, `perf-optimizer.md`, `doc-scribe.md`, `linting-expert.md`, `solution-architect.md`, and `challenger.md`, `blind-solve.md`, `codex.md` if present. Skip missing files only as permitted by SKILL.md's coverage rules. Merged spawns write one file per dimension; the filename list stays unchanged.
2. Read the report template at `<REVIEW_TEMPLATE>` with the Read tool; it defines output structure. Read `<REVIEW_CHECKLIST>` with the Read tool and apply its consolidation rules: signal-to-noise filter, annotation completeness, section caps.
3. Read `$RUN_DIR/cross-validation.md`, including the parent's validated candidates and assessments. Apply the validation and coverage rules below before merging claims.
4. Apply the challenger, blind-solve and Codex rules below, then write the versioned evidence and confirmed-finding sidecars before the report.
5. Write `<REPORT_DIR_LITERAL>/review-report.md`, append its source-file inventory, and return only the specified compact JSON.

## Evidence and coverage

- `<REVIEW_CHECKLIST>` empty/unset: insert the top-level note `Review checklist not applied (review-checklist.md missing from this develop install) — severity anchors may be inconsistent.`
- `$RUN_DIR/cross-validation.md` contains `Cross-Validation: SKIPPED`: prepend `Cross-validation: skipped` to the YAML header and executive summary. Required validation remains incomplete; the note never authorizes unconditional approval or unvalidated findings.
- Precision gate: include only findings with a concrete actionable location (function, line range, or variable name).
- Finding density: modules under 100 lines → aim ≤10 total findings. Rank by impact: blocking > critical > high > medium > low.
- Every finding carries an explicit inline severity: `[cosmetic]`, `[low]`, `[medium]`, `[high]`, `[critical]`. Put cosmetic findings in `### Cosmetic / Style`.

## Challenger and design comparison

Map `challenger.md` sections before merging:

| Challenger section | Review severity |
| -- | -- |
| Blockers (`[CRITICAL]`) | `critical` or `high`; promote to `critical` only with independent corroboration, e.g. Step 4 CONFIRMED or matching evidence from another agent. Without required verification retain a separate open concern, never downgrade uncertainty into an actionable high finding. |
| Concerns (`[HIGH]`) | `medium` |
| Nitpicks (`[LOW]`) | `low` |

Never drop challenger candidates from validation. Publish only parent-CONFIRMED claims; retain unresolved concerns separately.

When `blind-solve.md` exists:

- Compare its blueprint against the diff and other agents' findings.
- Add `### Design Divergence`, listing differences from the PR's approach ranked by impact.
- For each divergence distinguish a genuine miss from context the blind-solve agent may have lacked: prior decision, incident, constraint.
- Cross-reference divergences already covered by another finding; do not duplicate them.

No `blind-solve.md` (skipped or FIX/REFACTOR/CHORE): omit the section entirely, with no placeholder note.

## Report header

The file MUST begin with the template's `---`-delimited YAML metadata block: opening `---` on line 1, all template fields in order (including `Review contract:`, `Rating scale:`, `Readiness:` and `Gate:`), closing `---`, then body. No HTML comments (`<!-- ... -->`) or prose before the block; the orchestrator reads it verbatim as the reply header. Missing `---` head breaks terminal output.

| Field | Value or rule |
| -- | -- |
| `develop-review:` | Target: file / dir / working-tree diff. |
| `Title:` | `develop-review — <target>`; required by the report-delivery hook. |
| `Date:` | `<DATE>` |
| `Change Type:` | Classify change INTENT from the diff; never file-count heuristics or commit-message prefix. Fix and feat can touch the same files. |
| `Scope:` | Comma-separated key changed py files; cap ~5. |
| `Focus:` | Scope-label em-dash one-line description of what the change does, from the diff, not commit message. |
| `Agents:` | Comma-separated agent names with output in `$RUN_DIR/`. |
| `Reviewers:` | Actual scoped assessments per the additive attribution rules above. |
| `CI:` | Fixed `N/A (develop:review is read-only — runs no tests)`; never fabricate pass/fail. |
| `Review contract:` / `Rating scale:` | `1` / `source-5-v1` |
| `Readiness:` | `complete`, `blocked`, or `incomplete`, matching the envelope. |
| `Gate:` | `REVIEW_V1_PASS`, `REVIEW_V1_BLOCK`, or an evidenced `REVIEW_V1_REJECT_<GROUND> @<sha>`. |
| `Outcome:` | APPROVE / MINOR_EDITS / NEEDS_WORK / INSUFFICIENT_BUT_PROMISING / REJECT / literal null. |
| `Summary:` | 1–2 sentence overview. |
| `Confidence:` | Aggregate score — key gaps. |
| `Next steps:` | Comma-separated blockers + high-priority actions, max 5. |
| `Path:` | `→ <REPORT_DIR_LITERAL>/review-report.md` |

`Change Type` means what the change DOES: `fix` corrects wrong behavior; `feat` adds capability/API; `refactor` restructures the same behavior; `perf` makes the same behavior faster; `docs` changes docs/comments only; `ci` changes the pipeline only; `chore` is deps/tooling; `test` changes tests only; `mixed` combines intents.

## Codex and missing packages

- For `codex.md`, put unique findings under `### Codex Co-Review`. Same file:line → keep the agent version and mark `also flagged by Codex`. Codex has fixed confidence 0.75.
- develop:review executes no code. A claim blocked by a Python package absent or installed outside the declared spec stays a Confidence gap — state `not run: develop:review is read-only`, the package, and the declaring spec `file:line`; never close it by installing anything, and name the targeted test run that would close it under `Next steps`.
- Exhaust allowed static checks. A child inspection-only restriction does not prove the parent lacks an authorized runtime check; record the actual authorization/capability boundary. Feasible undone parent work means incomplete.

## Publication validation

1. Follow the loaded candidate-validation protocol. Do not infer confirmation from confidence or severity. A claim you add or materially strengthen beyond the parent's `cross-validation.md` is not published as confirmed: record it in `review-validation.json` as a candidate with null disposition and one `open_concerns` entry (owner: parent, next_check: validate the claim), keep it out of `findings.jsonl`, and list its candidate IDs in the envelope's `new_claims` array (empty when none) so the parent validates them before delivery.
2. Write `<REPORT_DIR_LITERAL>/review-validation.json`: contract_version=1, rating_scale=source-5-v1, aggregate source_outcome/source_rating/readiness/readiness_reason, returned/missing assessments, all candidates, and durable open_concerns. Aggregate readiness=complete only when every assessment is complete and open_concerns is empty. A missing required role (presence=missing) has readiness=incomplete; it, like any null candidate, makes the aggregate incomplete, overriding blocked, also under a null aggregate outcome. Header fields mirror the envelope.
3. Write `findings.jsonl` containing every confirmed actionable candidate exactly once. Every row carries contract_version=1, validation_status=CONFIRMED and candidate_id referring to the matching CONFIRMED candidate. Preserve stable finding IDs separately. Never put unverified concerns in this collection.
4. The report's Open Concerns names every obligation ID/owner/next_check/closure. Feasible undone checks mean incomplete; genuinely unavailable prerequisites after feasible checks mean blocked. Neither permits unconditional approval or a merge-readiness claim.
5. Null judgment means absence of source judgment supported by returned assessment evidence, never rejection. REJECT requires documented binding conflict and an evidenced terminal ground; fixable defects mean NEEDS_WORK. INSUFFICIENT_BUT_PROMISING means substantially incomplete source implementation with a good initiative, never missing hardware/process evidence. Historical reports retain their original five-rating meanings.

## File output and return

Write the consolidated report to `<REPORT_DIR_LITERAL>/review-report.md`. After `## Confidence`, append `## Source Files` using `Glob(pattern='*.md', path='$RUN_DIR')` to inventory source handovers.

Return ONLY compact JSON, preserving this envelope:

```text
{"status":"done","findings":N,"severity":{"critical":N,"high":N,"medium":N,"low":N},"file":"<REPORT_DIR_LITERAL>/review-report.md","confidence":0.N,"new_claims":["<candidate_id>"],"summary":"<one-line verdict>"}
```
