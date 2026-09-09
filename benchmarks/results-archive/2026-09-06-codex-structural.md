# Codex results — 2026-09-06

`results/codex-combined-20260906T085207Z/structural` — `gpt-5.6-luna` at high reasoning effort, Codex CLI 0.153.4, all five stages completed, 219/219 cells persisted. Previous Codex tables were removed rather than carried forward: they were produced under earlier manifests, earlier arm-reporting code, and in one case a run that aborted at 33 of 219 cells when a second study was started against the clone the Claude suite still held. The launcher now takes an exclusive lock on the target clone for the duration of a study, so a second run against the same clone is refused before it spends anything rather than failing partway through on the other run's staged edits.

This is the Codex-only view, scored on mean semantic quality with the runner's own totals estimator. For the same run restated on the metric and estimator the Claude tables use, see [Structural results — every model on one cohort](../README.md#structural-results--every-model-on-one-cohort--2026-09-06).

This frozen run's `B_auto` arm was executed under a prompt that required a Codemap query; the contract has since been changed to optional-use to match Claude's `B_auto`, so a future Codex `B_auto` run answers a different question and its numbers must not be blended with the `B_auto` figures below.

Structural stage, headline cohort (45 tasks; the ten self-consistency and symbol-extraction tasks marked diagnostic in the locked policy are excluded):

| Model | Pair                | Paired n | Control accuracy | Treatment accuracy |    Gain | Cells correct | Tokens | Output | Time |
| ----- | ------------------- | -------: | ---------------: | -----------------: | ------: | ------------: | -----: | -----: | ---: |
| Luna  | A_plain vs C_strict |       43 |            93.0% |          **98.9%** | +6.0 pp |   38 → **42** |   −53% |   −59% | −51% |
| Luna  | A_plain vs B_auto   |       44 |            91.3% |          **97.4%** | +6.1 pp |   38 → **41** |   −42% |   −50% | −42% |

Luna was the only Codex stratum measured when this section was written. `gpt-5.6-sol` and `gpt-5.6-terra` have since been executed and have their own sections below; all three declared Codex strata now carry a completed 219-cell study.

Codex accuracy is the mean semantic quality score over the paired cells, not a pass count, so the percentage and the correct-cell count move independently: `C_strict` gains 6.0 points of mean quality while converting four more cells outright. Both arms also convert 33 perfect headline cells into 37, over 43 pairs for `C_strict` and 44 for `B_auto`. The `C_strict` row is paired over 43 tasks rather than 44 because the adherence clause described above removes `RV-01`, whose `C_strict` cell never called Codemap; that cell is incorrect in both arms, so dropping it raises both means and narrows the gain from the +7.0 pp this row read before the clause was applied. Token, output, and time here are totals across the cohort — treatment sum ÷ control sum — restated as change against the control, a different estimator from the Claude tables above, which use per-task medians. Totals let a few expensive cells move the figure; medians do not. Both readings are reported rather than reconciled, because each answers a different question: what the whole study cost, and what a typical task cost.

The gap between them is large and almost always in the same direction. Measured on identical cells, the totals estimator reports a bigger saving than the paired per-task median on 32 of the 36 provider/arm/metric combinations, ties on one, and is smaller on three, with the gap reaching 26 percentage points: Luna `C_strict` input tokens read −53% as totals against −29% paired on the same 43 cells, Haiku `B_auto` −75% against −49%, Opus `C_strict` −37% against −19%, Terra `B_auto` elapsed −36% against −20%.

The mechanism is concentration, not distortion. On Luna `C_strict`, five tasks hold 36.5% of all control input tokens — `GR-03`, `RV-03`, `GR-04`, `GR-01`, `CQ-01` — and four of those five are also among the cheapest ratios in the stratum (0.033× to 0.119×). A sum therefore weights the study toward exactly the cases where Codemap helps most, while a median counts each of them once. It can cut the other way: Luna `B_auto`'s largest control cell is `BR-08` at 1,244,119 tokens, 14.2% of the control total at a losing 1.096×, which is why that row's gap is only 9 pp. The paired geometric mean sits between the two and is the honest bridge — on Haiku `B_auto` the three readings are −75% totals, −71% geometric mean, and −49% median, which shows the median is low because the log-space distribution is strongly right-skewed rather than because the totals are wrong. The cause is the tail named above — a handful of tasks where the unaided arm spends millions of tokens dominate a sum and count once in a median. Where a figure will be read as "what you should expect on your next task", the paired median or geometric mean is the honest one; the totals figure answers "what this study cost in aggregate" and nothing narrower.

Over all 55 structural tasks including the diagnostic cohort, `C_strict` reads 88.4% → 97.6% across 53 pairs at −61% tokens, and `B_auto` reads 88.6% → 97.0% across 54 pairs at −52% tokens.

Per-arm mean quality over every headline cell that arm scored, without pairing: `A_plain` 0.915 (39/45 correct), `B_auto` 0.974 (41/44), `C_strict` 0.983 (42/44). As in the Claude tables, these denominators differ by arm and are not a like-for-like comparison.

The four executable and extraction stages are separate, nonpoolable strata:

| Model | Stage      | Cells | A_plain correct | B_auto correct | C_strict correct | C tokens | C time |
| ----- | ---------- | ----: | --------------- | -------------- | ---------------- | -------: | -----: |
| Luna  | ReadCrop   |    18 | 6/6             | 6/6            | 6/6              |     −37% |   −31% |
| Luna  | Fix-Single |    12 | 4/4             | 4/4            | 4/4              |     +45% |   +25% |
| Luna  | Fix-Multi  |     9 | 3/3             | 3/3            | 3/3              |     +29% |   +24% |
| Luna  | Patch      |    15 | 5/5             | 5/5            | 4/5              |     −51% |   −18% |

ReadCrop reproduces the Claude finding: equal correctness, and the strict arm reads less. The two localized-edit stages are the honest counterexample — Fix-Single and Fix-Multi cost 45% and 29% more input than the control for identical correctness, which is why production guidance already says to skip Codemap for a fully localized edit with no unresolved structural fact. The single Patch loss is PT-04/C_strict: the patch applied to the correct file and the regression suite stayed green, but the target test still failed on a different assertion, so the fix was wrong rather than the tooling. One task at n=1 supports no efficiency or quality claim either way.

Four structural cells need naming rather than averaging:

- **BR-08 / C_strict — unscoreable.** The terminal event arrived while a command item was still open (`pending_item`), so the cell is recorded incomplete and excluded from both sides of the pair. Its answer text is present but was never admitted.
- **RV-01 / C_strict — treatment not credited.** The model resolved `CODEMAP_BIN` itself and ran `codemap-py query --compact undocumented lightning.pytorch.core.module` through the launcher's absolute path, exit 0. The locked contract credits only the unexpanded `$CODEMAP_BIN` form and explicitly declines to infer delivery from a path, so the call counted as zero and the cell is marked non-adherent. That is the contract behaving as specified, not a bug; whether it should stay that way is a live question — see the defects section below.
- **RV-04 / B_auto and RV-05 / C_strict — correct answers scored as extraction failures.** Both gave the ground-truth number (24 and 11) in a phrasing the count patterns do not match. Both are excluded from every paired figure rather than counted as zeros.

Thirty-six of the 110 treatment cells did not issue the exact locked query (28 `B_auto`, 8 `C_strict`); the failing component was the option set in 26, the target in 16, and the endpoint in 14. Nonconformance did not cost quality — those cells mean 0.971 against 0.954 for the conforming ones — so this is a contract-fidelity measure, not a performance one. Recall that this frozen run's `B_auto` arm required a Codemap query, unlike the optional-use `B_auto` contract now in force.
