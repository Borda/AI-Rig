# Handoff table

> Every workflow ends with this table, placed before its evidence sections. The table is the decision surface: a reader must be able to act on it without reading the body.

## Fields

| Field | Values | Required content |
| -- | -- | -- |
| Verdict | `APPROVE`, `MINOR_EDITS`, `MAJOR_REVISION`, `INSUFFICIENT_BUT_PROMISING`, `REJECT`, `null`, `investigated`, `blocked` | One sentence stating what the verdict covers and the single reason for it. |
| Alignment | `high`, `partial`, `off` | Evidence that the subject follows or breaks the project's actual codebase and direction: module layout, naming, documentation, tests, dependency policy. Name what was checked and what was not. |
| Findings | counts by severity, blocking subset, fixed vs open | `N open (a high, b medium, c low, d nit), m blocking, k fixed`. Use `none` and the reason when the workflow produces no findings. |
| Next action | exact skill or command, or `none — hand off` | The one primary action and what it closes. Secondary follow-ups may follow it; a human-owned step is named as such. |
| Confidence | number between 0 and 1 | The value and each named gap and what closes it. Lead with the gap most likely to change the verdict. |

## Verdict scale

> A review, a remediation's re-review, and a final challenge that judges a contribution report one source judgment on this scale. `investigated` and `blocked` are run states, not source judgments.

| Level | Token | Label | Meaning |
| -- | -- | -- | -- |
| 1 | `APPROVE` | Approve | No required source change remains; non-blocking notes may accompany it. |
| 2 | `MINOR_EDITS` | Minor changes | Confirmed required corrections with bounded impact and a localized remedy. |
| 3 | `MAJOR_REVISION` | Major revision | A valuable, aligned contribution whose remaining confirmed defects have material impact or substantial repair scope. |
| 4 | `INSUFFICIENT_BUT_PROMISING` | Insufficient, but a good initiative | A promising, project-aligned contribution whose implementation is substantially incomplete. |
| 5 | `REJECT` | Block | Evidenced incompatibility with the documented project direction or a binding constraint; preference alone never rejects. |
| none | `null` | No source judgment | The review ran but no defensible source judgment exists; never a rejection. |

## Rules

- Lead with the table, then the findings, gates, limits, and artifacts as evidence. Never bury the verdict or the next action in prose.
- Every value must be traceable to the body. The table adds no claim the body does not support.
- `MAJOR_REVISION` and `REJECT` require at least one blocking finding or a stated blocking reason. `MINOR_EDITS` names the confirmed required corrections. `APPROVE` requires the verification behind it, including when it carries non-blocking notes. `null` names the undecidable scope, its missing prerequisite and the next check: the review ran but cannot judge the source. `blocked` names the unavailable prerequisite or the decision that is the user's: the run could not proceed.
- Alignment is a judgment about fit, not a restatement of the findings. A clean diff can be `partial` when it contradicts the documented direction; a risky change can be `high` when it follows the established pattern and the risk is stated.
- A convention that was not checked is listed under Alignment, not omitted. When nothing was assessed, write `off — no codebase assessment was possible` and the reason.
- Confidence reflects the weakest accepted evidence, not the average. Adjust only for named gaps and state what each is and what closes it, including an unavailable independent reviewer, a selected axis that did not run, an unexecuted check required by the applicable skill, or a required class the host lacks. Honor the skill's documented check exemptions. Count an unexecuted manual substitute when the exemption requires it. Exclude null, blocked or failed contributions from the judgment and name each gap once. Never substitute a constant for a reviewer or check with no score of its own.
- Record the host operating system and architecture and packages that can be provisioned as facts of the run, never confidence gaps. Name a required hardware or OS class the host lacks as `not reproducible on this host: requires <CLASS>`.
- A skill that hands work to another names it exactly, for example `code-remediate` on `F-001`–`F-003`, `rig-plan` for the approved finding, or `rig-implement` with the recorded plan digest.
- A diagnostic workflow maps its outcomes onto the same values: pass is `APPROVE`, warnings are `APPROVE` with each warning listed as a non-blocking note, and a failing prerequisite is `blocked`; a read-only report is `investigated`. Any other workflow that completes with nothing required reports `APPROVE`, and one that leaves required work reports `MINOR_EDITS` or `MAJOR_REVISION` by the evidence rule above.
- A run with a frozen plan adds, below the table, the plan outcome and rounds (`converged`, `round-limit`, `authority-required`), the task-tree leaves and their owners, the final-challenge reviewer and verdict, and the challenge-loop rounds and stop reason when the loop ran. The five table fields stay the decision surface.
- The table is part of every run's report; there is no separate summary that can disagree with it.
