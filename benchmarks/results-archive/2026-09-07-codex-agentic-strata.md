# Codex agentic strata — 2026-09-07

The same 16 blast-radius tasks were then executed on the other two declared strata, at high reasoning effort on Codex CLI 0.153.4, 48/48 cells persisted in each, under the same repaired `agentic_contracts` prompt as `Luna⁺` and `Luna⁺⁺`:

- **`Terra`** — `results/codex-agentic-20260907T140422Z`, `gpt-5.6-terra` against the shared clone and the locked index `3c584089…`. Started 2026-09-07T14:04:22Z.
- **`Sol`** — `results/codex-agentic-20260907T141122Z`, `gpt-5.6-sol`, launched `--agentic --isolated` against a private run worktree holding a relocated copy of that index (`6f2d3cd7…`, same graph content at a different path). Started 2026-09-07T14:11:22Z.

These are the first agentic studies of either stratum, and they exist because of a defect rather than a plan: until 2026-09-07 the launcher's `--models` selection reached the structural lane only, and the agentic lane silently ran the manifest's default stratum. Every earlier "sol" or "terra" agentic invocation therefore produced a Luna study. A third artifact from the same afternoon, `results/codex-agentic-20260907T140408Z`, holds zero cells and a `KeyboardInterrupt` status: it is an interrupted launch, not a study, and nothing in this file reads from it.

| Run   | Pair                | Cells | Control score | Treatment score |   Gain | Cells correct | Tokens | Time |
| ----- | ------------------- | ----: | ------------: | --------------: | -----: | ------------: | -----: | ---: |
| Terra | A_plain vs C_strict |    16 |         0.932 |       **0.970** | +0.038 |   11 → **13** |    −3% | −31% |
| Terra | A_plain vs B_auto   |    16 |         0.932 |           0.826 | −0.106 |        11 → 5 |    −1% | −33% |
| Sol   | A_plain vs C_strict |    16 |         0.967 |       **0.991** | +0.023 |   12 → **15** |   −21% | −40% |
| Sol   | A_plain vs B_auto   |    16 |         0.967 |           0.855 | −0.112 |        12 → 6 |   +54% | −14% |

Score is the mean semantic quality over the scored cells; token and time are cohort totals against `A_plain`, the same estimator the two tables above use. Both runs scored all 16 cells in all three arms — no envelope loss, no incomplete cell, no contamination — so these are the cleanest Codex agentic artifacts in the record.

**Sol is the strongest strict-arm result this lane has produced, and it is also the cleanest.** Fifteen of sixteen cells correct against twelve, every `C_strict` cell adherent, 21% fewer input tokens, 43% less wall-clock at the paired median. It converts `BA-01`, `BA-12`, `BA-13`, and `BA-15` and loses only `BA-08`, where both treatment arms land on 0.850 against a perfect control.

**Terra's strict arm gains on the same tasks but pays nothing back in tokens.** Its cohort totals are almost flat across the three arms — 3.12M, 3.08M, and 3.04M gross input for `A_plain`, `B_auto`, and `C_strict` — so the saving that every other stratum shows is absent here, and the paired median over its ten admissible pairs is only −14%. Frozen native output records a query in every strict cell. The six historical nonadherence records (`BA-01`, `BA-04`, `BA-05`, `BA-12`, `BA-14`, `BA-16`) instead reflect an observation mismatch: those cells used per-cell absolute launchers while the original rule credited only the literal variable form. Absolute paths need reviewed per-coordinate provenance in replay and are never inferred from a basename. The flat token totals are descriptive, not evidence that the tool was skipped. `BA-12` is the extreme case at 574.0k input tokens against its control's 267.7k.

**The optional arm now fails on every Codex stratum, not just on Luna.** `B_auto` ends below its own control in all five Codex agentic executions up to this point — eight of eight once the balanced-order sweep is counted — and the two strata here are the worst of the five: 11 → 5 on terra and 12 → 6 on sol, both −37.5 points. Sol's optional arm also reads **more** input than its control — 3.90M against 2.53M gross, a +45% paired median — so on that stratum the available-but-optional integration costs both accuracy and tokens. Uptake does not explain it: `B_auto` reached for Codemap on 14 of 16 terra cells and 16 of 16 sol cells. Those counts are `codemap_used`, the observational signal, which credits a query issued inside a compound shell command; the stricter `codemap_calls`, which demands one standalone canonical query, reads 5 of 16 on terra and 14 of 16 on sol. Terra habitually chained its queries behind `&&` or `;`, so the gap between the two fields is a shell-style difference and not a difference in whether the tool ran. `A_plain` used it on none under either field, in both runs.

Expected-importer recall is 0.990 for `A_plain` and `B_auto` and 1.000 for `C_strict` in both runs; the single cell below full recall in each is `BA-15`, which the strict arm alone resolves completely. Discovery efficiency (DEFF) means run 3.56 / 2.71 / 2.47 on terra and 3.45 / 1.73 / 3.29 on sol — the one metric where the unaided control leads, because a grep-driven arm issues many cheap commands that each touch an expected importer.

Cached input is 86% of gross on terra's control and 84% on sol's, so the token columns here are gross-input claims with the same caveat the structural tables carry: sol's 21% gross reduction is a 21% fresh reduction (410.6k → 322.7k), which is the one place gross and fresh happen to agree.

These runs share the arm-order confound with every other table in this lane, and each is one execution at one repetition per cell. Two strata agreeing on the direction of `B_auto` is worth more than either alone; neither is a replication of the other's magnitude.

These two executions, together with `Luna⁺⁺` above, held the Codex rows of the cross-provider table until the balanced-order sweep replaced them. Their binary paired rows are kept here so nothing published is lost when the table moves:

| Run    | Arm      | Paired n | Control accuracy | Treatment accuracy |          Gain | Tokens | Time |
| ------ | -------- | -------: | ---------------: | -----------------: | ------------: | -----: | ---: |
| Luna⁺⁺ | C_strict |       16 |    68.8% (11/16) |      81.2% (13/16) | +12.5 pp (+2) |   −45% | −49% |
| Luna⁺⁺ | B_auto   |       16 |    68.8% (11/16) |       43.8% (7/16) | −25.0 pp (−4) |   −15% | −35% |
| Terra  | C_strict |       10 |     70.0% (7/10) |       80.0% (8/10) | +10.0 pp (+1) |   −14% | −27% |
| Terra  | B_auto   |       16 |    68.8% (11/16) |       31.2% (5/16) | −37.5 pp (−6) |    +7% | −32% |
| Sol    | C_strict |       16 |    75.0% (12/16) |      93.8% (15/16) | +18.8 pp (+3) |   −21% | −43% |
| Sol    | B_auto   |       16 |    75.0% (12/16) |       37.5% (6/16) | −37.5 pp (−6) |   +45% | −15% |
