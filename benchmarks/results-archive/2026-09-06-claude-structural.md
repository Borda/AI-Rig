# Claude multi-model — paired accuracy and efficiency

This is the Claude-only view over all 55 tasks, with cost. For the cross-provider view on the shared 45-task headline cohort, see [Structural results — every model on one cohort](../README.md#structural-results--every-model-on-one-cohort--2026-09-06); the numbers there are smaller denominators and differ accordingly.

Paired accuracy uses only tasks where both arms of that pair produced a scored, parsed answer, so both percentages share one denominator — the count in parentheses is that denominator's numerator, the cells answered correctly. Gain is the treatment minus the control in percentage points, with the cell delta beside it. Token, cost, and time columns are per-task medians of treatment ÷ control restated as change against the control: negative means the Codemap arm needed less.

| Tier   | Pair                | Paired n | Control accuracy | Treatment accuracy |          Gain | Tokens | Cost | Time |
| ------ | ------------------- | -------: | ---------------: | -----------------: | ------------: | -----: | ---: | ---: |
| Haiku  | A_plain vs C_strict |       47 |    74.5% (35/47) |  **87.2%** (41/47) | +12.7 pp (+6) |   −65% | −71% | −54% |
| Haiku  | A_plain vs B_auto   |       48 |    75.0% (36/48) |  **85.4%** (41/48) | +10.4 pp (+5) |   −48% | −52% | −40% |
| Sonnet | A_plain vs C_strict |       50 |    84.0% (42/50) |  **92.0%** (46/50) |  +8.0 pp (+4) |   −52% | −40% | −46% |
| Sonnet | A_plain vs B_auto   |       49 |    83.7% (41/49) |  **95.9%** (47/49) | +12.2 pp (+6) |   −52% | −43% | −48% |
| Opus   | A_plain vs C_strict |       50 |    88.0% (44/50) |  **96.0%** (48/50) |  +8.0 pp (+4) |   −21% | −11% | −24% |
| Opus   | A_plain vs B_auto   |       51 |    88.2% (45/51) |  **94.1%** (48/51) |  +5.9 pp (+3) |   −23% | −23% | −25% |

Bold marks the better arm of each pair. Cost is each run's captured `total_cost_usd`, not a local price table.

Per-arm accuracy over every cell that arm scored, without pairing:

| Tier   |       A_plain |        B_auto |      C_strict |
| ------ | ------------: | ------------: | ------------: |
| Haiku  | 75.0% (36/48) | 80.8% (42/52) | 80.4% (41/51) |
| Sonnet | 82.4% (42/51) | 96.0% (48/50) | 92.2% (47/51) |
| Opus   | 88.2% (45/51) | 92.3% (48/52) | 96.1% (49/51) |

These denominators differ by arm, because each arm drops its own extraction failures and contaminated cells, so the columns are not a like-for-like comparison and the differences between them are not the gain. Haiku shows why: `C_strict` reads below `B_auto` here (80.4% against 80.8%) while beating it on every shared denominator in the paired table above. Use this table for what each arm scored on its own, and the paired table for which arm is better.

Safety-grade (FN + BR tasks answered with recall ≥ 0.90) separates the arms where accuracy alone saturates: Haiku `A_plain` 10/14 against `B_auto` 13/14 and `C_strict` **14/14**; Sonnet and Opus reach 14/14 in all three arms. The pattern matches the ratio columns — the Codemap arms help most where the unaided model is weakest, and on Opus they converge toward parity on quality while still cutting roughly a quarter of the tokens and time.

Median wall-clock per structural task: Haiku 1m18s control against 19s `C_strict`; Sonnet 44s against 15s; Opus 32s against 16s.
