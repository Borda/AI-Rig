# Claude multi-model — median change against `A_plain`

Per-task medians over the 16 blast-radius tasks, stated as change against the control: negative means the arm needed less. This is the Claude-only view; for the cross-provider view with accuracy, see [Agentic results — every model on one cohort](../README.md#agentic-results--every-model-on-one-cohort--2026-09-06).

| Tier   | Arm      | Elapsed | Cost | Input tokens | Tool calls |
| ------ | -------- | ------: | ---: | -----------: | ---------: |
| Haiku  | C_strict |    −46% | −45% |         −60% |       −62% |
| Haiku  | B_auto   |    −53% | −51% |         −70% |       −68% |
| Sonnet | C_strict |    −82% | −65% |         −77% |       −72% |
| Sonnet | B_auto   |    −76% | −56% |         −72% |       −62% |
| Opus   | C_strict |    −73% | −41% |         −50% |       −52% |
| Opus   | B_auto   |    −77% | −51% |         −43% |       −59% |

Evidence recall and discovery efficiency were measured on all 144 Claude agentic cells. The cross-provider table above carries them per row as `control → treatment`; this is the same data by tier and arm, over all 16 cells rather than the paired subset:

| Tier   | Arm      |  EREC |  RREC | DEFF |
| ------ | -------- | ----: | ----: | ---: |
| Haiku  | A_plain  | 0.972 | 0.972 | 0.54 |
| Haiku  | B_auto   | 1.000 | 1.000 | 1.85 |
| Haiku  | C_strict | 1.000 | 1.000 | 3.88 |
| Sonnet | A_plain  | 0.928 | 0.926 | 0.62 |
| Sonnet | B_auto   | 1.000 | 1.000 | 2.37 |
| Sonnet | C_strict | 1.000 | 1.000 | 3.74 |
| Opus   | A_plain  | 0.990 | 0.990 | 1.45 |
| Opus   | B_auto   | 1.000 | 1.000 | 4.29 |
| Opus   | C_strict | 1.000 | 1.000 | 4.36 |

Every treatment cell on every tier answers full recall; every sub-1.000 reading is an `A_plain` cell. Five control cells fall short in both fields — BA-03 (haiku 0.815), BA-12 (haiku 0.895), and BA-15 (0.846 on all three tiers) — and one falls short in the report alone: sonnet BA-07 exposes the module during the run at `erec` 1.000 and omits it from the final report at `rrec` 0.964. Sonnet's `A_plain` row reads 0.928 only because BA-12 hit the 600-second coordinate timeout and scores 0.0/0.0; over its 15 scored cells the row is 0.990, level with Opus. Sonnet's accuracy denominators above are 15 for the same reason — that cell is excluded, not scored as a loss. DEFF is an unbounded exposure-hit count per command, so it is comparable within a tier and not across them.

Tool *time* moves the other way — median +57% to +155%, more rather than less — because each index call costs more than a single grep. The win is in needing far fewer calls, not in each call being faster.
