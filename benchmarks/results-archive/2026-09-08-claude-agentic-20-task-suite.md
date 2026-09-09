# Claude agentic — 20-task suite — 2026-09-08

Sources: `results/code-2026-09-08-6.json` (Opus), `results/code-2026-09-09.json` (Sonnet), `results/code-2026-09-09-2.json` (Haiku). Three separate 60-cell studies: 20 tasks × three arms × one repetition, 180 cells persisted. Manifest `90398b99…`; target revision `be98784a1`. The four added tasks extend graph enumeration and ranking depth, not behavioral change-impact validation. Do not pool these studies with the earlier 16-task suite or across models.

**Codemap improves semantic accuracy where this run shows headroom, most clearly on Haiku.** Optional Codemap raises Haiku's semantic credit by **19.38 percentage points**, from 77.9% to 97.3%, and exactly correct answers from **6/20 to 18/20**. Required Skill raises its semantic credit to 94.0% and exactly correct answers to 14/20, although compliance failures prevent that semantic benefit from becoming reliable strict-arm delivery. Opus gains 3.61 points with either treatment. Sonnet is a counterexample: its near-perfect plain arm retains a small semantic advantage.

The semantic columns below use the same measure and denominator: sum of raw `answer_graded_score` divided by all 20 assigned tasks, with unavailable answers contributing zero. They do **not** erase correct-answer credit for a treatment violation. Semantic exact counts use `answer_correct`; admitted exact additionally requires valid, complete, uncontaminated, adherent execution. This diagnostic view complements, rather than replaces, the saved admission-gated `quality` metric.

| Model  | Plain semantic credit | Optional semantic credit | Required semantic credit | Semantic exact A / B / C | Admitted exact A / B / C |
| ------ | --------------------: | -----------------------: | -----------------------: | ------------------------ | ------------------------ |
| Opus   |                 96.3% |                    99.9% |                    99.9% | 18 / 19 / 19             | 18 / 19 / 19             |
| Sonnet |                 99.2% |                    97.9% |                    97.6% | 18 / 18 / 17             | 18 / 18 / 14             |
| Haiku  |                 77.9% |                    97.3% |                    94.0% | 6 / 18 / 14              | 6 / 18 / 6               |

All exact counts are out of 20. Haiku plain has 19 scored answers; every other Claude arm has 20. Required-arm adherence is 20/20, 16/20, and 6/20 respectively. Thus Haiku's saved strict-arm `quality=30.0%` does not mean only 30% of its answer content was correct.

**Better answers can also cost less.** Across all assigned Haiku tasks, optional Codemap used **53.6% less gross input** and **46.4% less money** ($4.77 → $2.56). This is an observed whole-arm result, not a paired median. Opus shows the tradeoff: optional total cost increases from $10.59 to $12.48, required to $12.00, despite improved answers and shorter runtimes. These observations support adaptive use, not a universal savings claim or a causal model-tier explanation.

Canonical efficiency below uses only pairs where **both tasks pass exactly, including admission checks**. Each entry is the median per-task percentage change relative to plain, not the ratio of arm totals. Quality improvements and regressions remain visible in the preceding table; excluded pairs are not silently treated as savings.

| Model  | Treatment | Both-exact pairs | Gross input | Output tokens | Elapsed time |
| ------ | --------- | ---------------: | ----------: | ------------: | -----------: |
| Opus   | Optional  |               17 |      +23.5% |        −66.2% |       −56.5% |
| Opus   | Required  |               17 |       +4.5% |        −63.0% |       −55.2% |
| Sonnet | Optional  |               17 |      −60.3% |        −84.5% |       −77.9% |
| Sonnet | Required  |               13 |      −65.9% |        −85.4% |       −78.8% |
| Haiku  | Optional  |                6 |      −42.1% |        −57.9% |       −50.0% |
| Haiku  | Required  |                2 |      −30.4% |        −56.6% |       −36.9% |

Claude “fresh input” is omitted: this runner subtracts cache creation as well as cache reads, leaving under 0.1% of gross in every cell. That residue is not comparable with Codex gross-minus-cache-read input. Haiku's two eligible strict pairs cannot support a general efficiency conclusion.
