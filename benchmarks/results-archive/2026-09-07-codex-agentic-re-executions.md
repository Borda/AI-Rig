# Codex agentic re-executions — 2026-09-07

The same 16 blast-radius tasks were executed twice more on `gpt-5.6-luna`, at high reasoning effort on Codex CLI 0.153.4, 48/48 cells persisted in each:

- **`Luna⁺`** — `results/codex-agentic-20260907T065010Z`, launched `--agentic --isolated`, which builds a private run worktree and relocates the locked index into it (`e0ce11d9…`, the same index content at a different path). Started 2026-09-07T06:50:13Z.
- **`Luna⁺⁺`** — `results/codex-combined-20260907T055156Z/agentic`, the agentic stage of the combined launch whose structural half ran terra, against the shared clone and the locked index `3c584089…`. Started 2026-09-07T09:51:11Z.

Both ran the repaired `agentic_contracts` prompt; the 2026-09-06 run did not, so these two are executions of one contract and the older run is not a third repetition of it. The two 09-07 manifests differ only in the structural-manifest and launcher hashes they carry, not in any agentic material.

`Luna⁺⁺` held the `Luna` row of the [cross-provider agentic table](../README.md#agentic-results--every-model-on-one-cohort--2026-09-06) until the [balanced-order sweep](2026-09-08-codex-agentic-balanced-order-sweep.md) replaced it; the row it held read 16 pairs, `11 → 13` correct at −45% tokens and −49% time. All three Luna executions are now reported here and nowhere else: one model with several studies published as adjacent rows reads as a repetition design, which it is not.

| Run    | Pair                | Cells | Control score | Treatment score |   Gain | Cells correct | Tokens | Time |
| ------ | ------------------- | ----: | ------------: | --------------: | -----: | ------------: | -----: | ---: |
| Luna⁺  | A_plain vs C_strict |    15 |         0.959 |       **0.971** | +0.012 |       11 → 11 |   −65% | −62% |
| Luna⁺  | A_plain vs B_auto   |    15 |         0.959 |           0.844 | −0.115 |        11 → 5 |   −32% | −43% |
| Luna⁺⁺ | A_plain vs C_strict |    16 |         0.959 |       **0.985** | +0.026 |   11 → **13** |   −51% | −46% |
| Luna⁺⁺ | A_plain vs B_auto   |    16 |         0.959 |           0.851 | −0.108 |        11 → 7 |    −6% | −28% |

Score is the mean semantic quality over the scored cells; token and time are cohort totals against `A_plain`, the same estimator the 09-06 table uses. `Luna⁺` reports 15 cells rather than 16 because its `A_plain` `BA-06` cell exited without an answer (`incomplete`) — an absent answer, not an envelope loss.

`Luna⁺`'s `C_strict` row is the one place the two views disagree in sign, and the reason is the admission rule rather than the model: over its 15 scored cells the strict arm converts `BA-13` and `BA-15` and loses `BA-07` and `BA-11`, for 11 correct against 11. The binary cross-provider row drops `BA-13` and `BA-14` for non-adherence — `BA-13` being one of the two cells the arm converted — which is what turns 11 → 11 into 10 → 9. Read the −7.7 pp there as the cost of removing a converted cell, not as a regression the arm produced.

**The direction reproduces; the size does not.** Across the three executions `C_strict` scores +0.031, +0.012, and +0.026 against its control while reading 48%, 65%, and 51% fewer input tokens, and `B_auto` scores −0.069, −0.115, and −0.108 while reading 21%, 32%, and 6% fewer. Every run has the strict arm up and the optional arm down; no run has them within noise of each other. Correct-cell counts move the same way — `B_auto` ends below its own control in all three (9 → 6, 11 → 5, 11 → 7) — and the same task, `BA-07`, is lost by `B_auto` in all three and by `C_strict` in all three, which makes it the clearest single-task counterexample in this lane.

**The envelope repair holds.** The 09-06 run lost 12 of 48 cells to the missing wrap instruction; `Luna⁺` and `Luna⁺⁺` lose zero, and `Luna⁺⁺` is the first *Codex* agentic run in this record with no non-poolable cell and no non-adherent cell — 48 admissible cells out of 48. (Claude's Haiku and Opus tiers already pair 16 of 16 in both arms on their own 2026-09-06 artifact.) Voluntary Codemap use in `B_auto` was 16/16, 14/16, and 16/16, and `A_plain` used it in none, so no control is contaminated in any run.

Expected-importer recall is 0.99 for `A_plain` and 1.00 for both treatment arms in each 09-07 run, with exposure hits per command (DEFF) 1.84 / 1.76 / 2.60 on `Luna⁺` and 2.56 / 1.75 / 2.53 on `Luna⁺⁺`. Both absolutes carry the recall and floor caveats below.

These runs share the arm-order confound with every other agentic table: arms ran `A_plain` → `B_auto` → `C_strict` per task with no provider cache reset, so the elapsed reductions overstate the treatment effect.
