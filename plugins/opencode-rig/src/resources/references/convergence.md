# Convergence rules

## Scoring

| Severity | Weight |
| -- | -- |
| security | 20 |
| critical | 10 |
| high | 6 |
| medium | 4 |
| low | 2 |
| nit | 1 |

The open weighted total counts only findings whose latest state is `open`.

## Decision

After the baseline `W0`:

- Any decrease in the total is convergence.
- Equal totals are a plateau and stop the loop.
- An increase is non-convergence and stops the loop.

A new finding never resets prior signatures, history, or the baseline. A signature is the changed file plus a normalized invariant, not a line number.

## Progress table

| Iteration | Security | Critical | High | Medium | Low | Nits | Weighted score |
| -- | -- | -- | -- | -- | -- | -- | -- |
| 1 | 0 + 1 | 0 + 0 | 0 + 2 | 0 + 1 | 0 + 0 | 0 + 0 | 24 |

Every numeric cell is `old + new`. Old counts a signature seen in an earlier validated round; new counts a signature that first appears in this round. Keep every row. Print the table at the end of every validated round — before fixes, the next dispatch, or a stop — never only in the final handoff.

## Stop reasons

`converged`, `plateau`, `non-converging`, `round-limit`, `unavailable-reviewer`, `authority-required`, `drift`.

Security or critical residue blocks completion. `round-limit` is a safety ceiling, not a success state.

## Finding classes before fixes

A validated finding is triaged before it is assigned a fix:

| Class | Meaning | Action |
| -- | -- | -- |
| `contract` | A documented guarantee of this workflow is false in a reachable state. | Fix, and add a test that fails on the previous bytes. |
| `ambiguity` | The document does not say which behavior is required. | Decide, write the decision into the contract, then fix if the decision requires it. |
| `limit` | The behavior is outside what the workflow can enforce (an agent that runs arbitrary code, changes ambient state between observations, or declines to record it). | Record it in the limits, with the boundary that makes it a limit. Do not fix, and do not reopen it in a later round. |

## Review question and stop rule

Give every fresh reviewer the contract, the accepted-limit register, and one bounded question: *can a completed run misrepresent its declared work within this contract?* A fresh session is still required; a resumed reviewer carries the previous round's reasoning and is not independent.

Stop the loop when one completed round reports no `contract`-class finding of high severity or above. Limits do not reopen the loop. A round with only limit-class reports is a converged round for this purpose, even when the reviewer labels them high under the generic severity scale; the register decides the class, not the label.
