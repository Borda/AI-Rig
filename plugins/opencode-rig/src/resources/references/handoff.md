# Handoff table

> Every workflow ends with this table, placed before its evidence sections. The table is the decision surface: a reader must be able to act on it without reading the body.

## Fields

| Field | Values | Required content |
| -- | -- | -- |
| Verdict | `approved`, `approved with notes`, `changes requested`, `investigated`, `blocked` | One sentence stating what the verdict covers and the single reason for it. |
| Alignment | `high`, `partial`, `off` | Evidence that the subject follows or breaks the project's actual codebase and direction: module layout, naming, documentation, tests, dependency policy. Name what was checked and what was not. |
| Findings | counts by severity, blocking subset, fixed vs open | `N open (a high, b medium, c low, d nit), m blocking, k fixed`. Use `none` and the reason when the workflow produces no findings. |
| Next action | exact skill or command, or `none — hand off` | The one primary action and what it closes. Secondary follow-ups may follow it; a human-owned step is named as such. |
| Confidence | number between 0 and 1 | The value and the single largest limit behind it. |

## Rules

- Lead with the table, then the findings, gates, limits, and artifacts as evidence. Never bury the verdict or the next action in prose.
- Every value must be traceable to the body. The table adds no claim the body does not support.
- `changes requested` requires at least one blocking finding or a stated blocking reason. `approved` and `approved with notes` require the verification behind them. `blocked` names the unavailable prerequisite or the decision that is the user's.
- Alignment is a judgment about fit, not a restatement of the findings. A clean diff can be `partial` when it contradicts the documented direction; a risky change can be `high` when it follows the established pattern and the risk is stated.
- A convention that was not checked is listed under Alignment, not omitted. When nothing was assessed, write `off — no codebase assessment was possible` and the reason.
- Confidence reflects the weakest accepted evidence, not the average. The limit is the one most likely to change the verdict.
- A skill that hands work to another names it exactly, for example `code-remediate` on `F-001`–`F-003`, `rig-plan` for the approved finding, or `rig-implement` with the recorded plan digest.
- A diagnostic workflow maps its outcomes onto the same values: pass is `approved`, warnings are `approved with notes`, and a failing prerequisite is `blocked`; a read-only report is `investigated`.
- A run with a frozen plan adds, below the table, the plan outcome and rounds (`converged`, `round-limit`, `authority-required`), the task-tree leaves and their owners, and the final-challenge reviewer and verdict. The five table fields stay the decision surface.
- The table is part of every run's report; there is no separate summary that can disagree with it.
