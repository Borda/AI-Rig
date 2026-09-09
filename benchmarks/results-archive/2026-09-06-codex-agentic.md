# Codex agentic results — 2026-09-06

`results/codex-combined-20260906T085207Z/agentic` — `gpt-5.6-luna` at high reasoning effort, Codex CLI 0.153.4, 48/48 cells persisted, none incomplete, none contaminated.

| Model | Pair                | Cells | Control score | Treatment score |   Gain | Cells correct | Tokens | Time |
| ----- | ------------------- | ----: | ------------: | --------------: | -----: | ------------: | -----: | ---: |
| Luna  | A_plain vs C_strict |    16 |         0.929 |       **0.960** | +0.031 |    9 → **13** |   −48% | −47% |
| Luna  | A_plain vs B_auto   |    16 |         0.929 |           0.860 | −0.069 |         9 → 6 |   −21% | −33% |

Score is mean semantic quality across the 16 cells; the correct-cell count is how many of those cells were right outright. For this run beside the Claude models on one binary-scored cohort, see [Agentic results — every model on one cohort](../README.md#agentic-results--every-model-on-one-cohort--2026-09-06). `B_auto` is worse on both — it loses three cells the unaided control answered — so its cheaper tokens buy a real quality regression rather than a trade.

Restricted to pairs where both cells returned the strict answer envelope — the only poolable comparison — `C_strict` reads 0.935 → **0.996** over 9 pairs at −44% tokens and −45% time, and `B_auto` reads 0.937 → 0.857 over 11 pairs at −15% tokens and −32% time. The strict arm is better on both readings and the optional-use canary is worse on both; the direction does not depend on which subset is used.

Expected-importer recall was 0.990 for `A_plain` and `B_auto` and 1.000 for `C_strict`, in both the full agent text and the final report. Exposure hits per command (DEFF) were 2.00 / 1.76 / 2.42.

Twelve of the 48 cells lost the strict envelope and fall back to diagnostic bare-JSON recovery, which is not poolable: seven `C_strict`, three `B_auto`, two `A_plain`. This is a prompt-contract defect rather than a provider result — see [Task defects found, fixed, and what remains](../README.md#task-defects-found-fixed-and-what-remains). Three `C_strict` cells (BA-01, BA-13, BA-14) issued no Codemap call at all and are recorded as treatment not followed; BA-01's transcript narrates running the query it never ran.
