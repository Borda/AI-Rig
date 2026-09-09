# Codex results — `gpt-5.6-terra` — 2026-09-07

`results/codex-combined-20260907T055156Z/structural` — `gpt-5.6-terra` at high reasoning effort, Codex CLI 0.153.4, 219 cells over 73 tasks in five stages, one repetition per cell, a 600 s wall-clock cap per cell, against the same `pytorch-lightning` 2.6.5 revision `be98784a1` and the same frozen index `3c584089…` every other table here uses. The structural stage started at 2026-09-07T05:55:12Z; every stage is marked `completed` and all 219 cells persisted. Arm contracts are byte-identical to the `gpt-5.6-sol` run's on all three arms (`936a684f…`, `ae5f9a51…`, `83db65ba…`), so this is the second Codex study executed under the optional-use `B_auto` contract. As on Sol, `execution.codex_cli` records `observed_version: codex-cli 0.153.4` against `reviewed_version: codex-cli 0.146.1` and `auth_source_recorded` is false. Full analysis, together with the two Luna agentic re-executions from the same day: `.reports/benchmarks/codex-terra-agentic-paid-analysis.md`.

This is the stratum whose first launch, on 2026-09-06, was refused on a paid-approval token before any model call. The refusal cost nothing and left no telemetry; this section is the relaunch, and it is a separate study from the `gpt-5.6-sol` one it was originally launched beside.

The binary paired rows on the shared 45-task headline cohort are in [Structural results — every model on one cohort](../README.md#structural-results--every-model-on-one-cohort--2026-09-06). On the run's own mean-semantic-quality metric with the runner's totals estimator, over the headline cohort:

| Model | Pair                | Paired n | Control accuracy | Treatment accuracy |    Gain | Cells correct | Tokens | Output | Time |
| ----- | ------------------- | -------: | ---------------: | -----------------: | ------: | ------------: | -----: | -----: | ---: |
| Terra | A_plain vs C_strict |       40 |            91.6% |          **97.2%** | +5.6 pp |   35 → **37** |   −55% |   −55% | −59% |
| Terra | A_plain vs B_auto   |       41 |            91.8% |          **98.4%** | +6.6 pp |   36 → **39** |   −16% |   −40% | −36% |

Over all 55 structural tasks including the diagnostic cohort, `C_strict` reads 92.0% → 96.6% across 42 pairs at −50% tokens and `B_auto` 92.2% → 98.5% across 43 pairs at −12%. Per-arm mean quality over every headline cell that arm scored, without pairing: `A_plain` 0.918 (36/41 correct), `B_auto` 0.986 (43/45), `C_strict` 0.975 (42/45) — different denominators by arm, so not a like-for-like comparison.

**Both treatment arms changed tasks in one direction only.** Neither converts a correct control cell into an incorrect one: `C_strict` gains DI-01 and DI-02, `B_auto` gains DI-01, DI-02, and DI-06, and the sole quality regression anywhere in the cohort is BR-07, which loses 0.056 in both arms. Per-task paired quality deltas are +0.056 mean and 0.000 median for `C_strict` at 10 wins / 29 ties / 1 loss, and +0.066 / 0.000 for `B_auto` at 10 / 30 / 1. As on Sol, the median is 0.000 because the control is already near ceiling; the movement lives in the minority of tasks where it is not.

**The control lost four cells that the treatment arms did not, and the admission rule then credits the control for it.** `A_plain` `CQ-01` ended with a command item still open (`pending_item`, 459,672 tokens), `A_plain` `FT-05` and `A_plain` `DI-05` exited non-zero with zero tokens recorded, and `A_plain` `FT-03` failed answer extraction. One treatment cell is also dropped, `C_strict` `FT-01` for non-adherence. That is why this stratum pairs 40 and 41 tasks where Sol pairs 45 and 44 — and because every one of the dropped control cells is a cell the unaided arm failed to complete, dropping them raises the control's measured accuracy. The gains above are therefore conservative rather than flattered.

Optional access again bought nothing on tokens, and this time uptake cannot be the explanation. Thirty-four of the 41 admitted `B_auto` cells issued a successful query — against 26 of 44 on Sol — and the split is still flat: 0.942× median when Codemap was queried against 0.947× when it was not. `B_auto` is nevertheless the better arm on accuracy here (+7.3 pp against `C_strict`'s +5.0 pp on the shared binary cohort) while spending roughly what its control spent, so on this stratum availability moved answers without moving cost, and requirement moved cost.

Tokens are gross input including cached reads, and terra is cached at about 85%: whole-run gross totals are 7,081,329 / 5,357,713 / 3,161,083 for `A_plain` / `B_auto` / `C_strict` against fresh totals of 1,062,001 / 808,849 / 746,235, so the 55% whole-run gross reduction from `A_plain` to `C_strict` is a 30% fresh reduction. On the headline cohort the median per-task ratio is −42% gross but −26% fresh, and the median paired delta is −29,376 gross tokens against −3,642 fresh. Any dollar claim must be stated on fresh tokens or must say that it is not.

The four executable and extraction stages are separate, nonpoolable strata:

| Model | Stage      | Cells | A_plain correct | B_auto correct | C_strict correct | B tokens | C tokens | B time | C time |
| ----- | ---------- | ----: | --------------- | -------------- | ---------------- | -------: | -------: | -----: | -----: |
| Terra | ReadCrop   |    18 | 5/6             | 6/6            | 6/6              |    +101% |     +10% |   +93% |   +26% |
| Terra | Fix-Single |    12 | 4/4             | 4/4            | 4/4              |     −12% |      +3% |   −19% |    +9% |
| Terra | Fix-Multi  |     9 | 2/3             | 3/3            | 3/3              |      −5% |      −2% |   −13% |    −7% |
| Terra | Patch      |    15 | 3/5             | 3/5            | 4/5              |     −20% |      −9% |   −22% |     0% |

Token and time columns are cohort totals restated as change against `A_plain`, the same estimator the Luna and Sol stage tables use. **ReadCrop inverts here**: on Luna and Sol the strict arm read 37% and 34% less on this stage, while on terra it reads 10% more and the optional arm reads 101% more — `B_auto` spent 142,476 tokens on RC-04 against the control's 47,213 without issuing a single Codemap call. The stage's correctness moves the other way: RC-03 is wrong in the control and right in both treatment arms, where on Sol the same task failed identically in all three. Editing stages are close to parity in both directions and support no claim at 3 to 5 tasks each.

`B_auto` again abandons the tool wherever code must be modified: voluntary uptake is 4 of 6 cells in ReadCrop and 48 of 55 in the structural stage, but **0 of 12 across Fix-Single, Fix-Multi, and Patch** — the identical count Sol produced. Two independent strata now show that an optional rollout will not reproduce the `C_strict` behaviour on editing work.

The Patch stage is the one place the strict arm looks better rather than cheaper: PT-05 is solved only by `C_strict`, where `A_plain` and `B_auto` both fail, and PT-04 fails in all three arms. Sol's Patch loss ran the other way — there `C_strict` was the only arm to fail PT-04. Two strata, one repetition each, disagreeing on which arm fails which patch task, is the shape of noise and not of an effect.

Contract fidelity, unchanged in character from the earlier strata: 40 of the 110 treatment cells did not issue the exact locked query (31 `B_auto`, 9 `C_strict`); the failing component was the option set in 26, the target in 17, and the endpoint in 14. Nonconformance did not cost quality — nonconforming cells mean 0.995 against 0.959 for conforming ones. `skill_delivery_observed` is false in all 165 structural cells, including every `C_strict` cell whose `codemap_delivery` reads `installed_skill`, so as on Sol the supported claim is that required Codemap *querying* produced the effect, not that the Skill file was read. Delivery is also confounded with arm — `B_auto` used `direct_cli` in all 48 of its Codemap cells and `C_strict` used `installed_skill` in all 54 of its own.

Data-quality notes: one repetition per cell throughout, so no cell has a variance estimate and no significance testing was performed; the four agentic-family stages carry 3 to 6 tasks each, where a single task flip moves a pass rate by 17 to 33 points. The three incomplete `A_plain` cells and one `A_plain` extraction failure are described above and are all control-side. This run has no agentic blast-radius stage of its own; the agentic cells executed in the same launch ran `gpt-5.6-luna` and are reported in the agentic lane.

<details>
<summary>Superseded and partial structural runs</summary>

Earlier structural tables were removed rather than carried forward: they were measured under retired harnesses, retired arm names, or incomplete runs, and blending them with the current numbers is not defensible. On the Codex side one run aborted at 33 of 219 cells when a second study was started against the clone the Claude suite still held. The launcher now takes an exclusive lock on the target clone for the duration of a study, so a second run against the same clone is refused before it spends anything rather than failing partway through on the other run's staged edits.

</details>
