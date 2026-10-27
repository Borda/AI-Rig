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

Every numeric cell is `old + new`. Old counts a signature seen in an earlier validated round; new counts a signature that first appears in this round. Keep every row.

## Stop reasons

`converged`, `plateau`, `non-converging`, `round-limit`, `unavailable-reviewer`, `authority-required`, `drift`.

Security or critical residue blocks completion. `round-limit` is a safety ceiling, not a success state.
