# Codex agentic — 20-task suite — 2026-09-09

Source: `results/codex-agentic-20260909T001312Z/{gpt-5.6-luna,gpt-5.6-terra,gpt-5.6-sol}/telemetry.jsonl`. Three separate 60-cell studies, one repetition, high reasoning effort, Codex CLI 0.153.4, manifest `e88606fb…`. Different provider manifests and treatment delivery prevent a pooled cross-provider comparison: Claude optional exposes a Skill, while Codex optional exposes the CLI.

**The demonstrated Codex benefit is faster, shorter correct answers—not higher semantic accuracy among scored controls.** All scored plain answers receive full semantic credit. Required Codemap reduces median output and elapsed time in every stratum's both-exact cohort. Terra and Sol also deliver more scored answers than plain, but that availability difference must not be described as better semantic reasoning.

Same semantic-credit definition as above, using `quality.graded_score`; unavailable answers contribute zero across all 20 assigned tasks. Unlike the previous mixed table, these are not legacy component means.

| Model | Plain semantic credit | Optional semantic credit | Required semantic credit | Scored answers A / B / C | Admitted exact A / B / C |
| ----- | --------------------: | -----------------------: | -----------------------: | ------------------------ | ------------------------ |
| Luna  |                100.0% |                    99.9% |                    94.9% | 20 / 20 / 19             | 20 / 18 / 18             |
| Terra |                 95.0% |                   100.0% |                    99.9% | 19 / 20 / 20             | 19 / 20 / 19             |
| Sol   |                 85.0% |                   100.0% |                    99.9% | 17 / 20 / 20             | 17 / 20 / 18             |

Counts are out of 20. All Codex cells meet their assigned treatment rule; semantic exact and admitted exact counts coincide here. Missing usage on interrupted cells means observed token totals would understate full consumption; no missing usage is imputed as zero.

Canonical both-exact paired medians:

| Model | Treatment | Both-exact pairs | Gross input | Fresh input | Output tokens | Elapsed time |
| ----- | --------- | ---------------: | ----------: | ----------: | ------------: | -----------: |
| Luna  | Optional  |               18 |      +17.6% |       −1.0% |        −16.9% |       −12.2% |
| Luna  | Required  |               18 |       +3.8% |       +0.3% |        −15.2% |        −9.1% |
| Terra | Optional  |               19 |      −19.0% |       −5.3% |        −16.1% |       −17.9% |
| Terra | Required  |               18 |      +26.0% |       +1.3% |        −41.2% |       −33.5% |
| Sol   | Optional  |               17 |      +41.6% |      +19.7% |         −4.8% |        −0.4% |
| Sol   | Required  |               15 |       +4.5% |      −17.8% |        −19.8% |       −15.7% |

Required gross input increases in all three strata; output and latency improve. Optional uptake remains uneven: Luna 14/20, Terra 3/20, Sol 10/20. Optional-arm performance therefore measures availability plus voluntary use, not the effect of a Codemap call on every task. The large earlier optional-arm collapse is not reproduced, but “no regressions” would be false: Luna loses two exactly correct tasks relative to plain.
