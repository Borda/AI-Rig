# Codex agentic balanced-order sweep — 2026-09-08

`results/codex-agentic-20260907T212926Z` — one launch, three sequential child studies, 144 of 144 cells persisted. This is the set the [cross-provider agentic table](../README.md#agentic-results--every-model-on-one-cohort--2026-09-06) publishes:

- **`Luna`** — `gpt-5.6-luna`, started 2026-09-07T21:29:36Z, scope `7dd6da1d…`.
- **`Terra`** — `gpt-5.6-terra`, started 2026-09-07T23:29:54Z, scope `26318f29…`.
- **`Sol`** — `gpt-5.6-sol`, started 2026-09-08T00:51:46Z, scope `510ff102…`.

All three ran at high reasoning effort on Codex CLI 0.153.4, one repetition per cell, 600 s per cell, under manifest `5fd00458…`, in a shared private worktree holding the relocated locked index `9753c586…` — the same graph as `3c584089…` at a different path — against the same `pytorch-lightning` revision `be98784a1` every other table here uses.

**This is a new experiment revision rather than another repetition.** `experiment_revision` reads `codex-agentic-verified-launcher-balanced-order-2026-09-07` against `codex-agentic-skill-imports-guidance-2026-08-09` for every earlier agentic study, and the archived manifests differ in three material ways: `execution_order.strategy` is now `cyclic-arm-order-v1`, rotating the arms by locked task ordinal so BA-01 runs A/B/C, BA-02 runs B/C/A, and BA-03 runs C/A/B; the `C_strict` requirement now names a standalone complete compact query through the injected `CODEMAP_BIN` variable *or* the exact installed launcher, with a detector that credits a provenance-verified absolute path; and the frozen treatment is Codemap 0.34.0 rather than 0.33.1. None of these runs is poolable with any earlier agentic study.

| Run   | Pair                | Cells | Control score | Treatment score |   Gain | Cells correct | Tokens | Time |
| ----- | ------------------- | ----: | ------------: | --------------: | -----: | ------------: | -----: | ---: |
| Luna  | A_plain vs C_strict |    16 |         0.974 |       **0.990** | +0.016 |   13 → **14** |   −60% | −49% |
| Luna  | A_plain vs B_auto   |    16 |         0.974 |           0.881 | −0.093 |        13 → 8 |   −32% | −36% |
| Terra | A_plain vs C_strict |    16 |         0.967 |       **0.973** | +0.005 |   12 → **14** |   −34% | −25% |
| Terra | A_plain vs B_auto   |    16 |         0.967 |           0.842 | −0.125 |        12 → 6 |   +19% | −19% |
| Sol   | A_plain vs C_strict |    16 |         0.914 |       **0.994** | +0.080 |    9 → **14** |   −55% | −53% |
| Sol   | A_plain vs B_auto   |    16 |         0.914 |           0.830 | −0.083 |         9 → 5 |    +6% | −24% |

Score is the mean semantic quality over the scored cells; token and time are cohort totals against `A_plain`, the same estimator the three tables above use. The cross-provider table reports the same cells as per-task medians, which is why its percentages differ.

**The strict arm converges where the control does not.** It ends on exactly 14 of 16 correct in all three strata, while the controls read 13, 12, and 9. Sol is the widest gap the lane has produced — `9 → 14` at −57% input tokens and −57% wall-clock on the paired median — and it is wide because that stratum's control did badly, not because its strict arm did unusually well.

**Control performance is the volatile term, and the spread between strata is three cells rather than a capability ordering.** Sol's control differs from luna's on exactly three tasks — `BA-02` (0.574 against 1.000), `BA-09` (0.722), `BA-10` (0.846) — and matches it on the other thirteen, including the three every control fails identically in every execution (`BA-01` 0.889, `BA-12` 0.825, `BA-15` 0.877). The same sol stratum answered `BA-02`, `BA-09`, and `BA-10` correctly in `results/codex-agentic-20260907T141122Z`, for 12 of 16, and `BA-02` also flipped between terra's two executions, so these are unstable cells rather than a stratum ranking. The methodology manifest declares the three Codex strata as a list and assigns them no capability order, unlike the Claude tiers; nothing in this file supports reading `9 → 14` on sol as "the strongest model needed the most help". The frozen audit separates the causes: `BA-02` and `BA-10` use all-module second-order counts where the oracle counts non-test production modules, while `BA-09` is a genuine package-module naming error (`callbacks.__init__` instead of `callbacks`) with ranking correct. The task wording did not state the production-only boundary, so the first two are prompt/oracle ambiguity rather than evidence of general model weakness; none of the three should be summarized as one ranking/count failure.

**`B_auto` regresses on all three strata again, and converts almost nothing.** Over the 48 optional-arm cells it turns exactly one control failure into a success (sol `BA-09`) and loses sixteen control successes. `BA-03`, `BA-07`, and `BA-08` are lost by the optional arm in every stratum. Its token penalty does not reproduce: paired medians are −17%, +16%, and +1% against +45% on the superseded sol run, so the accuracy direction is the reproducible half and the token direction is not. Three things changed in one step here, so nothing in this sweep attributes the token shift to any single cause.

**The optional arm's loss is confined to two answer components, and it is the same two everywhere.** Scoring the 144 cells component by component, `A_plain`, `B_auto`, and `C_strict` are at parity on every enumeration field — `production_importers` reads 0.988 / 0.991 / 1.000, and `buckets`, `overlap_importers`, and every `*_count` field sit at 1.000 in all three arms. The whole regression lives in the two ordered/counted fields: `ranking` reads 0.911 / 0.625 / 0.944 and `rdep_counts` 0.758 / 0.395 / 0.994. Twenty-one `B_auto` cells miss `ranking` against seven controls and five strict cells. So the optional arm finds *who imports what* as well as anyone; it ranks and counts them worse than the arm with no tool at all.

**The mechanism is the counting definition, not tool access.** Both treatment arms reach for `query --compact central` in a similar number of cells (21 for `B_auto`, 23 for `C_strict`), so this is not ignorance of the subcommand. Tracing luna `BA-03` command by command: all three arms return an identical importer list, and only the ranking differs — control and strict answer `lightning.pytorch.trainer.call`, `B_auto` answers `lightning.pytorch.core.module`, the module with the largest raw reverse-dependency count once `examples.*` and tests are included. The task ranks within `production_importers`. The strict arm's first command reads the Skill's `Need → Query` routing table before it queries anything; the optional arm's first commands are `--help`, `doctor --json`, and `list --help`, after which it improvises a per-candidate `rdeps` loop and ranks by the raw counts that loop returns. Luna `BA-15` shows the softer version of the same failure — the right five modules in the wrong order, scored 0.14 — and luna `BA-07` the harder one, where the ranked set contains modules outside the candidate set entirely. Having the counts without the contract that defines which counts is worse than having no counts.

**Discovery cost still separates the two treatment arms.** Heuristic help/discovery calls run 59, 50, and 53 in `B_auto` against 7, 3, and 3 in `A_plain` and 2, 2, and 1 in `C_strict`. The Skill-bound arm does not shop for help; the bare-CLI arm does, in every stratum.

**Uptake fields still disagree in the optional arm.** `codemap_used` reads 14, 13, and 13 of 16 while the stricter `codemap_calls` reads 5, 8, and 11 — the same chained-command shell style seen before, not a difference in whether the tool ran. In `C_strict` both fields read 16 of 16 everywhere, and `A_plain` reads zero under both, so no control is contaminated.

Evidence recall and discovery efficiency, by stratum and arm, over all 16 cells — the same figures the cross-provider table carries as `control → treatment`:

| Stratum | Arm      |  EREC |  RREC | DEFF |
| ------- | -------- | ----: | ----: | ---: |
| Luna    | A_plain  | 0.990 | 0.990 | 2.45 |
| Luna    | B_auto   | 0.990 | 0.990 | 1.85 |
| Luna    | C_strict | 1.000 | 1.000 | 3.82 |
| Terra   | A_plain  | 0.990 | 0.990 | 3.57 |
| Terra   | B_auto   | 1.000 | 1.000 | 2.41 |
| Terra   | C_strict | 1.000 | 1.000 | 3.37 |
| Sol     | A_plain  | 0.970 | 0.970 | 2.47 |
| Sol     | B_auto   | 0.990 | 0.990 | 1.94 |
| Sol     | C_strict | 1.000 | 1.000 | 5.22 |

`C_strict` answers full recall in every cell of every stratum; no control does, and the optional arm only matches it on terra. DEFF leads under the strict arm on luna and sol, which reverses the earlier pattern where the grep-driven control always led; terra is the exception, and it is also the stratum whose control issued the fewest commands per cell (5.9 against luna's 10.2), so each of them touched proportionally more of the answer.

Cached input is 90%, 86%, and 89% of control gross, so the token columns are gross-input claims with the usual caveat; fresh-input paired medians are −21%, −7%, and −17% for `C_strict` against gross −45%, −29%, and −57%. The tail still dominates the cohort totals: luna's `BA-13` control alone spends 1,587,026 gross input tokens against the strict arm's 76,867, and sol's `BA-15` spends 561,558 against 106,016.

Arm order is balanced here, which the earlier tables' order caveat did not have; the provider prompt cache is still never reset, so rotation redistributes that exposure rather than removing it. Full per-task analysis: `.reports/benchmarks/codex-agentic-balanced-order-sweep-analysis.md`.
