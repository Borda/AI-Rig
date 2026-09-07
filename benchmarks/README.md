# Codemap Benchmarks

Empirical validation for the `codemap` plugin. Provider ownership is explicit in every LLM runner name: `claude` and `codex` identify provider-exclusive transport, while `cli`, `generate`, and `provider_parity_contracts` are provider-neutral. The structural benchmark is **repo-agnostic**: swap `tasks-bench.json` (which ships a `repo` header with name, namespace, and default clone path) to run against any Python codebase. Reference results use `pytorch-lightning` pinned at tag `2.6.5` (auto-cloned to `.sandbox/pytorch-lightning`).

## Provider-parity expansion

### Current cross-provider acceptance status

| Workload   | Codex evidence    | Claude evidence                         | Current judgment                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| ---------- | ----------------- | --------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| ReadCrop   | 2026-09-06 · luna | 2026-08-11 · haiku                      | Both full suites preserve source-answer quality; the strict installed integration lowers aggregate input and command use, with disclosed per-task variance.                                                                                                                                                                                                                                                                                                                        |
| Fix-Single | 2026-09-06 · luna | 2026-08-11 · haiku                      | Both full suites preserve executable quality. Efficiency is heterogeneous, so production guidance skips Codemap for a fully localized edit with no unresolved structural fact.                                                                                                                                                                                                                                                                                                     |
| Fix-Multi  | 2026-09-06 · luna | 2026-08-12 · haiku, sonnet (bounded W3) | Revised FM-01/FM-03 scopes are checksum-valid on Codex Luna and Claude Haiku/Sonnet. P1 closes as a harness and heterogeneous-evidence milestone: FM-02 and the semantically accepted FM-03 A/C pairs support bounded conclusions, while incomplete FM-01 A/C and model-specific failures remain reported and no universal multi-file efficiency claim is admitted.                                                                                                                |
| Patch      | 2026-09-06 · luna | 2026-08-12 · haiku (bounded W4)         | Current checksum-valid Claude `claude-patch-post-lifecycle-9e7bbb02bc3a` and Codex `codex-patch-post-lifecycle-4119d30180f3/patch` scopes both complete 15/15 cells with strict-query delivery, patch transport, containment, oracle, regression, and cleanup evidence. Valid A/C quality ties remain provider/model-stratified: Claude has two lower-context/time C pairs plus strict-only successes; Codex has three lower-context/time C pairs plus two strict oracle failures. |

Each cell dates the evidence the judgment rests on and names the model stratum that produced it, rather than reporting a completed/pending state: a state word says a run happened, a date says which run, so a figure can be traced to an artifact instead of to a status. Every Codex cell cites the 2026-09-06 combined study `codex-combined-20260906T085207Z`. On the Claude side, Fix-Multi and Patch cite the canonical artifacts named later in this file, while the ReadCrop and Fix-Single cells date the newest completed full-suite artifact on disk for that stage (`claude-readcrop-8c605ce4f83e`, `claude-fix-single-5647f59aeeba`), because no canonical artifact is declared for those two stages. Run metadata records a creation timestamp and the model but no codemap-py version, so a plugin build cannot yet be recovered from a result artifact.

Both providers have now completed a full suite against the current runner and the corrected task suite, so no cell rests on pre-correction evidence. The Codex figures in this table come from one 219-cell study covering the structural stage plus all four workloads; the Claude figures remain per-workload runs taken between 2026-08-11 and 2026-08-12, which is why the two columns carry different dates.

Two further Codex strata — `gpt-5.6-sol` and `gpt-5.6-terra`, both 2026-09-07 — have since run the same four workloads and are reported in [Results](#results) rather than folded into the judgments above, because each stage carries only 3 to 6 tasks per stratum. Their stage figures do not all reproduce: the ReadCrop token direction reverses on terra, and the single Patch failure lands in a different arm on each stratum. The judgments above therefore stand on the cited luna evidence and must not be restated as multi-stratum Codex conclusions. Both strata have since run the 16-task agentic cohort as well — first on 2026-09-07 and again in the three-stratum balanced-order sweep completed 2026-09-08, both reported in [Results](#results). Those rows agree with luna on the direction of both arms but not on their size. The terra strict arm's six skipped-query cells on 2026-09-07 proved to be an observation limit as much as a compliance one: the cells did query, through an absolute launcher path the variable-only detector did not credit, and terra is adherent on all sixteen cells under the revised rule.

These are separate, nonpoolable strata. `A_plain` versus `C_strict` is decision-grade; `B_auto` is an optional-use canary. The complete task suites and unfavorable cells remain in the reported artifacts rather than being filtered to favor Codemap. Exact artifacts, scorer replays, limitations, and the P1 closure decision are documented below and in the active provider-parity expansion plan.

The final Fix-Multi gate is complete as a bounded W3 stratum. FM-02 remains accepted from the existing evidence. The checksum-valid `benchmarks/results/claude-fix-multi-f16f4b86418d` artifact remains immutable diagnostic provenance: FM-01 omitted the explicit `should_stop` dry-run field and the original FM-03 required an invalid `Strategy.setup`/`super()` contract. The canonical FM-03 task now uses cooperative `Strategy.setup_environment` propagation. Final paid artifacts are `benchmarks/results/claude-fix-multi-f2719755cb23` (Haiku), `benchmarks/results/claude-fix-multi-243a7e2174ea` (Sonnet), and `benchmarks/results/codex-unified-91752e388e4e/fix-multi` (Luna); all transport, patch, path, lifecycle, and integrity checks pass. Artifact glyphs and stored quality labels are immutable. A prospective scorer replay classifies all Haiku FM-01 A/B/C rows as failures under the final reason/verbose gate, Sonnet FM-01 A/B as passes and C as a verbose-gated failure, Haiku/Sonnet FM-03 A/B/C as semantic passes (including harmless method-docstring changes), and all Codex FM-01/FM-03 cells as passes. Valid A/C efficiency comparisons therefore exclude incomplete pairs. That validity gate was defined after the outcomes were observed and is not pre-registered in the locked policy, so every Fix-Multi efficiency ratio in this file is exploratory-only; the correctness verdicts are unaffected. This closes P1.3/P1 as a harness and heterogeneous-evidence milestone, not as a universal multi-file efficiency claim.

```bash
# Regenerate and inspect the fresh Claude scope before any paid run.
# First run `bash benchmarks/run-all.sh claude --dry-run`, then export the
# target and index paths it prints for the current machine.
REPO_PATH="${REPO_PATH:?set to the target clone printed by run-all.sh}"
INDEX_PATH="${INDEX_PATH:?set to the locked index printed by run-all.sh}"

python3 benchmarks/run-claude-agentic.py \
    --study fix-multi \
    --repo-path "$REPO_PATH" \
    --index "$INDEX_PATH" \
    --model haiku \
    --tasks FM-01,FM-03 \
    --dry-run
```

The corresponding Codex dry run used the same revised `FM-01,FM-03` selector. The paid executions and their immutable output directories are recorded in the W3 acceptance section below; if locked sources change in a future rerun, discard the old approval and repeat the dry run. Never reuse the rejected `f16f4b86418d` directory or its approval.

The tracked methodology policy seed, benchmark suites, and manifest builders are the canonical inputs. `run-all.sh` regenerates the ignored [methodology manifest](manifests/provider-parity-methodology.json), [Codex integration manifest](manifests/codex-integration.json), its [human companion](manifests/codex-integration.md), and the agentic machine/human manifests before live admission; tests do the same before collection. A paid launcher then freezes those exact generated bytes and their hashes in its run-owned source snapshot, so later workspace edits cannot change an admitted run. Runtime logs and telemetry remain under ignored `results/` paths.

The superseded historical 55-task/165-cell structural run remains immutable provenance under the approved 0.28.2 machine manifest SHA-256 `568caefa6cdd1e876e2f35a5e2476d5e661d9672894191c930017f14a29305e4`; it is not current P1 acceptance evidence. Its methodology SHA-256 was `3320c2d35e3189d43e3c2336603189083cc7ef8e76ac10dfb2f99ef47ee07afa`. The later 0.28.3 prospective lock is retained as superseded provenance rather than active evidence: methodology SHA-256 `5f613da7ff7c431ff30be9e44a3d9444d1246766a8505e38fc2c6e2908a18112`, machine-manifest SHA-256 `3a69c31a82db95526d8b3e7ab3edf3c9b3a49dd917683413dc43154ddd6f42f8`, and human-manifest SHA-256 `be884757b2e738f3bfe9efba2cf75522b82220fe13fc36dd48d059ba3b7e5086`. Current completed agentic and structural evidence is reported below; no historical result is rewritten or pooled with a later contract.

Two paid post-fix diagnostic attempts stopped before any model cell: the first on macOS `/var` alias handling during snapshot creation, the second because the intentional `DI-01` stage conflicted with global clean-worktree admission. Both are infrastructure diagnostics, not treatment evidence. The repaired `DiffImpactStageAdmission` records exact Git status, repository commit, and intended-file SHA-256, restores the target in `finally`, and fails closed on unapproved changes or index/commit drift. The subsequent diagnostic completed 54/54 cells across `DI-01`, `DI-05`, `DI-06`, `GR-01`, `GR-03`, and `GR-04`: mean quality was equal at `0.8945` for A/B/C; paired geometric ratios were B/A `0.4465` input, `0.2732` output, `0.3335` elapsed and C/A `0.2849` input, `0.2623` output, `0.2932` elapsed. All 36 B/C cells used Codemap transport, but only 11/36 matched the exact locked query shape. This exposed a measurement bug: exact query-shape mismatch had been incorrectly folded into treatment adherence. The prospective runner now preserves transport adherence and reports `locked_query_conformance` separately, with continuous `locked_query_fitness`, `locked_query_endpoint_fitness`, `locked_query_target_fitness`, and `locked_query_option_fitness`; exact mismatches remain diagnostic counts and do not alone exclude a cell from pooling. Direct and Skill guidance now route production direct callers through `fn-rdeps … --exclude-tests`, transitive callers through `fn-blast`, and production centrality through `central --top N --exclude-tests`.

The provider-neutral library lives in `_bench_common/provider_parity_contracts.py`; it locks task/prompt identity, arm semantics, evaluator dispatch, continuous fitness components, capability strata, headline exclusions, and effort-aware paired construction. It does not generate tasks, run benchmarks, invoke models, or implement provider transport.

The agentic benchmark uses the same provider-neutral contract. `_bench_common/agentic_contracts.py`, `suites/tasks-agentic.json`, `run-claude-agentic.py`, and `run-codex-agentic.py` share the 16 committed BA-01–BA-16 task objects, materialized prompts, answer envelopes, ground-truth oracle, continuous scorer, and paired metrics. The canonical arms are `A_plain`, `B_auto`, and `C_strict`; provider adapters own only transport, isolation, and native event normalization. The default repeat count is one: Claude's default launcher batch is 16 tasks × 3 arms × 3 model tiers = 144 cells, while Codex's default launcher batch is 16 tasks × 3 arms × 2 models = 96 cells. A single Codex model still runs 48 cells. Either provider's run with more than one repetition is explicitly non-poolable and requires its exact derived scope SHA-256; Codex selected-task scopes use the same admission rule.

The interrupted `benchmarks/results/codex-agentic-20260804T212004Z` run persisted 31/48 cells and is invalid, non-poolable diagnostic evidence. Its shared prompt named answer labels without declaring the value shapes enforced by the scorer, so models often returned semantically reasonable rich structures that silently scored zero; BA-04 also permitted an estimated affected count while the oracle required exact equality. The stopped `benchmarks/results/codex-agentic-20260805T122121Z` run persisted 14/48 successful transports across BA-01–BA-05 before plugin-version admission failed while preparing the next cell. That run is also invalid and non-poolable: each C cell re-resolved plugins from mutable marketplace state, and the old response path discarded valid bare JSON plus raw EREC/RREC/DEFF evidence when the strict labelled envelope was absent. Its displayed zero scores therefore do not establish a Codex or Codemap quality failure.

The repaired `codex-agentic-protocol-evidence-separation-2026-08-05` revision applies to both providers. It gives the same typed field instructions and synthetic JSON example, validates shapes before scoring, represents high-centrality modules as `module → rdep count`, defines BA-03 prefix buckets, defines BA-04's exact deduplicated second-wave set, and distinguishes BA-05 public initializers from internal examples. A strict labelled answer is pooling-eligible; one complete bare JSON object may be scored only as a diagnostic, while malformed or ambiguous responses remain semantically unscored. Raw EREC/RREC and unbounded DEFF are computed independently from response formatting. Codex snapshots the exact run-owned Codemap and Codex Rig source trees once, installs directly from those immutable paths, validates their bytes before later cells, and records expected/observed identities in private `runtime-isolation.jsonl`. The repaired revision completed its first full 48-cell Codex run on 2026-08-05; its bounded exploratory results are reported in [Codex agentic parity study](#codex-agentic-parity-study).

The stopped partial artifacts `benchmarks/results/codex-integration-20260802T095824Z` and `benchmarks/results/codex-integration-20260803T191236Z` are audit-only and non-poolable; no treatment effect is inferred from either. The latter persisted 86/165 rows, first failed authentication at `execution_index=50`, and then recorded identical zero-token `401 Unauthorized` failures. Root fixes in the relock include zero-argument `coupled` canonical detection, CQ-03 ranking by internal import count with complete ordered five `name + dep_count` rows, and RV-02 acceptance of natural `N modules directly import/depend` forms.

The completed run is frozen at `results/codex-agentic-20260805T170347Z` under methodology SHA-256 `e1717b806ebad49111aac8b8b7703d0cdf241440d3ef0140f7e96cc3eff7804e`, agentic machine/human manifest SHA-256 values `9ee83804df5fa43b7a4d64ae9ea316005fffff1d974429cae452f0c90ad54185` / `52216b8e513538367ae1ce21c9e384a8ce440e96016ad308d147e280a447567a`, and default scope SHA-256 `a7dbcd13a33460db2f577c960f69f640d73000c217281ff3940a5c5c44f2457a`. All 48 coordinates persisted with treatment adherence, stable token accounting, and no contamination or infrastructure failure. The checksum ledger verifies every listed entry but omits the permitted empty `runtime-isolation.jsonl` sidecar, so the artifact supports an exploratory performance summary but not a claim of complete checksum coverage or independent runtime-identity attestation.

After the run, the terminal legends were clarified with metric directionality without changing task, prompt, oracle, scorer, transport, or treatment behavior. The prospective generated locks are methodology `597a7928a6e096d453e277d923a4eecc1bff7ca01ecb3e018a3ce10019518946`, structural machine/human `62d27a3a155e8c105f373bbc66c42e7d885c81ae4fb213aaf1f754e47ec20f34` / `1cff24fa6f415534d056edf0f3a0adcf5b891816d7a30a9ca68cdb0db3ecba3c`, agentic machine/human `7a4ce9bcbba1b729a2af766e9b0f3160eff5a2940ee8cec7aa3c5677b39e19f4` / `f96c5d1898a37d047b230448a73c8b02e4c64d39eca8328cdd3fd825f8c737cc`, and default agentic scope `2f1f17c8ba968bf7cc51225da584434ee7a6480886b72a298c82fdbef603a94f`. The archived paid runner differs from the prospective runner only in those legend strings, so this presentation-only relock does not require another paid run and does not rewrite the frozen result hashes.

The historical paid run used shared RV evaluator v6 and remains immutable/non-poolable. Its raw answers contain the correct RV-02 count `64 modules directly import` in all A/B/C rows; v6 failed to extract these natural forms. The relocked evaluator accepts optional `directly` and natural `[unique] [public] symbols [are] uncovered` phrasing; immutable tests preserve RV-05's real `2/5` symbol loss and aggregate score `0.7` with `correct=false`. It does not retroactively rescore historical telemetry.

Claude is the mature, repeatedly debugged reference adapter, but it is not an unquestionable oracle. Provider parity is bidirectional: every unexplained Claude/Codex divergence is investigated as a possible Codex defect, Claude defect, provider/backend limitation, or shared-methodology bug. Only transport, isolation, and provider-native event normalization may legitimately diverge.

**B2 Claude adapter migration.** Both Claude runners now route explicit canonical arms through the shared contracts:

- **`A_plain`** — Codemap is absent and inaccessible.
- **`B_auto`** — Codemap is available; the model may use it, and no-call is valid.
- **`C_strict`** — Codemap is available and must be used at least once; no-call is recorded as a separate compliance failure while task scoring remains independent.

The contract text above is the shared use policy; how Codemap is *delivered* stays provider-native, and the two providers differ in a way that matters when reading a `B_auto` row:

- **Codex** — `B_auto` receives only the locked direct CLI (`"$CODEMAP_BIN" query --compact …`), the plugin is not installed, and `CODEMAP_SKILL_FILE` is removed from the arm environment, so the arm has the tool and no Skill guidance. `C_strict` installs the packages and binds the query Skill immutably.
- **Claude** — `B_auto` and `C_strict` receive the *same* tool section and the same allow-lists (`Bash(scan-query:*)`, `Skill` permitted in both); the only difference is the one required-use sentence appended for `C_strict`. A Claude `B_auto` cell therefore has the Skill route available and simply is not told to use it.

So "optional use" means the same thing on both providers, but "no Skill guidance" is true only of Codex. A Codex `B` row measures CLI-without-Skill against no-Codemap; a Claude `B` row measures same-access-without-the-instruction.

Canonical runs load the locked task/prompt/evaluator policy and fail closed unless the target commit/tree, clean worktree, and index bytes/metadata match the manifest; result records carry task, suite, evaluator, envelope, arm-contract, repository, and index provenance. Legacy labels (`plain`, `codemap`, `semble`, `combined`) retain their historical behavior and remain `legacy-unversioned`; they are not retroactively mapped to A/B/C. `--dry-run` prints the selected plan without invoking Claude or writing model results; the real-code runner's default `--arm all` plan is A/B/C and validates the locked inputs, while the agentic runner validates them when a canonical arm is selected.

**Codex structural adapter and active controls.** `run-codex-structural.py` is one task-driven Codex CLI. With no `--tasks`, it plans or runs all 73 supported tasks (55 structural, 6 ReadCrop, 4 Fix-Single, 3 Fix-Multi, and 5 Patch), producing 219 A/B/C cells. `--tasks` accepts family tokens, exact IDs, or a mixed list; task IDs route to the appropriate stage-specific scorer. ReadCrop, Fix-Single, Fix-Multi, and Patch remain separate, nonpoolable result stages even though they share the transport and launcher. All stages normalize gross/cached/fresh input, output/reasoning output, command count, tool elapsed time, Codemap usage, error, and compliance fields. ReadCrop has a source-extraction oracle; executable stages give the agent one benchmark-owned writable checkout, capture its canonical Git diff outside the agent sandbox, then apply that diff in a second clean worktree for the ordinary apply, exact changed-path, independent behavior-oracle, and cleanup gates. Patch additionally stages each task's frozen historical target-test fixture and task-local index before the agent runs; paid admission still requires a fresh scope lock. Git metadata and project dependencies are intentionally unavailable inside the agent sandbox, and the equal-arm prompt directs the agent to use bounded syntax/static validation instead of wasting turns on inaccessible Git or project pytest. The checkout receives a derived frozen index whose only content change is `scan_root`; source and derived hashes plus a hash excluding that root are recorded, and index mutation makes the cell ineligible. `git apply --recount` and the behavior result after recovery are diagnostic-only fields for malformed hunk headers. A has no Codemap access. B receives only a locked direct CLI and may use it when useful; B is an optional-use canary, so a no-query B cell is compliant. C installs the locked Codemap and Codex Rig packages, binds the installed query Skill immutably, and must complete the task-specific canonical compact query. An explicit Skill-file read remains diagnostic-only. Prompts state Codemap's static-graph boundary: it is for compact symbol/dependency/importer/caller facts, not runtime validation, tests, or edits. Additional repository reads and shell commands are allowed in B/C but do not replace C's required treatment evidence. A_plain-versus-C_strict is the decision-grade contrast; a B regression means users should prefer the installed integration and does not undermine the strict-arm result. The Claude and Codex adapters share task loading, prompt materialization, hashes, validators, ground truth, scoring, and pairing; provider-specific code is limited to transport, isolation, and native event normalization. All canonical stage rows use the shared Rich renderer on terminals and plain redirected output, preserving compact `k`/`M` token and `m`/`s` time units without changing telemetry.

Each cell receives an isolated `CODEX_HOME`. Permission profiles deny the copied credential, host agent roots, network, and source-tree writes; treatment arms may write only to the index-local coordination directory. The runner records answer, quality, extraction, treatment adherence, Codemap use, locked-query component fitness, and transport failures per cell and continues after the admission smoke so these outcomes remain measurable. Terminal output uses `treatment:✓|✗` for the assigned transport contract and `codemap-used:✓|✗` for observed Codemap use: clean `A_plain` is `treatment:✓ codemap-used:✗`; a contaminated A row can be `treatment:✗ codemap-used:✓`; B requires the assigned direct-CLI availability and isolation but may make zero queries, while C requires the assigned Skill delivery plus its successful compact query. Exact locked-query agreement is independently recorded as `locked_query_conformance`; continuous Jaccard components are `locked_query_fitness`, `locked_query_endpoint_fitness`, `locked_query_target_fitness`, and `locked_query_option_fitness`, so a useful but non-exact query no longer masquerades as failure to receive the treatment.

Existing Claude and exploratory Codex results remain historical evidence and are never pooled with the current study. The confirmatory population has 45 independently scored tasks; ten static-reference or approximate/self-consistency tasks run as diagnostics. Target-dependent ground truth is valid only for PyTorch Lightning tag `2.6.5` at commit `be98784a1a03581b7051a355ae1084fd352d7cea`.

### Entrypoint ownership

| Ownership        | Entrypoint                                   | Role                                                                                                          |
| ---------------- | -------------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| Claude only      | `run-claude-structural.py`                   | Structural and real-code A/B/C plus legacy Claude arms                                                        |
| Claude only      | `run-claude-agentic.py`                      | Agentic Claude/semble comparison plus canonical Claude arms                                                   |
| Codex only       | `run-codex-structural.py`                    | One task-driven 73-task CLI: structural A/B/C plus separate ReadCrop, Fix-Single, Fix-Multi, and Patch stages |
| Codex only       | `run-codex-agentic.py`                       | Approval-gated 16-task agentic A/B/C execution and artifact persistence                                       |
| Provider-neutral | `run-all.sh`                                 | Safe dispatcher for smoke, Claude, or Codex batch workflows                                                   |
| Provider-neutral | `run-codemap-cli.py`                         | Deterministic scan/query correctness and performance; no model                                                |
| Provider-neutral | `_bench_common/provider_parity_contracts.py` | Shared task, arm, scoring, provenance, and pairing library; not a runner                                      |
| Provider-neutral | `generate-tasks-bench.py`                    | Validates or refreshes shared structural oracle fields                                                        |
| Provider-neutral | `generate-tasks-real-issues.py`              | Refreshes shared real-issue task evidence                                                                     |

Archived manifests retain historical consumer labels from before this rename. Active execution uses only the concise names above; no compatibility launchers remain.

## Benchmark overview

| Benchmark                                                      | Provider         | Script                     | LLM | Arms                                   | Tasks                                                                                                      | Primary question                                                                                      |
| -------------------------------------------------------------- | ---------------- | -------------------------- | --- | -------------------------------------- | ---------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| [Agentic](#agentic-benchmark-shared-claudecodex-contract)      | Claude           | `run-claude-agentic.py`    | Yes | Canonical 3 (A/B/C); legacy 4 explicit | 16 import-graph tasks                                                                                      | Does Codemap change agentic exploration recall, efficiency, or adoption under the shared contract?    |
| [Structural](#real-codebase-benchmark-run-claude-structuralpy) | Claude           | `run-claude-structural.py` | Yes | Legacy 2; parity 3 (A/B/C)             | 60 tasks — 11 series (SE / FN / RV / CQ / BR / DG / FT / RI / DI / GR / MB)                                | Does scan-query reduce token cost and improve structural recall on pre-implementation research tasks? |
| Provider parity                                                | Codex            | `run-codex-structural.py`  | Yes | Parity 3 (A/B/C)                       | 73 task IDs: 55 structural + 6 ReadCrop + 4 Fix-Single + 3 Fix-Multi + 5 Patch; stages reported separately | Does Codemap provide an objective within-Codex advantage under the same shared contracts?             |
| Codex agentic parity                                           | Codex            | `run-codex-agentic.py`     | Yes | Canonical 3 (A/B/C)                    | 16 tasks × one repetition                                                                                  | Does Codemap change agentic exploration recall, efficiency, or adoption under the shared contract?    |
| [Query](#query-benchmark-run-codemap-clipy)                    | Provider-neutral | `run-codemap-cli.py`       | No  | —                                      | Deterministic query/correctness suites                                                                     | Is scan-query correct, complete, and fast enough?                                                     |

Run **Query** first — validates the index before spending LLM tokens on agentic runs. Every measurement any of these produce is collected under [Results](#results).

### Files

<details>
<summary><strong>Per-file ownership and purpose</strong></summary>

| File                                            | Ownership        | Purpose                                                                                                                                                           |
| ----------------------------------------------- | ---------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `policy/provider-parity-methodology.json`       | Provider-neutral | Tracked canonical methodology policy seed consumed by the deterministic builders                                                                                  |
| `manifests/provider-parity-methodology.json`    | Provider-neutral | Ignored, on-demand generated shared task, evaluator, target, index, and analysis identities; frozen into each admitted run                                        |
| `manifests/codex-integration.json`              | Codex            | Ignored, on-demand generated machine-enforced plain/direct-CLI/Skill execution contract                                                                           |
| `manifests/codex-integration.md`                | Codex            | Ignored, on-demand generated human-readable manifest review and paid-run instructions                                                                             |
| `manifests/codex-agentic.json`                  | Codex            | Ignored, on-demand generated machine-readable 16-task agentic A/B/C execution lock, exact-SHA admission, runtime limits, and artifact contract                    |
| `manifests/codex-agentic.md`                    | Codex            | Ignored, on-demand generated human-readable 16-task agentic scope, treatment, scorer, approval, and launch review                                                 |
| `build-provider-parity-methodology-manifest.py` | Provider-neutral | Deterministically regenerates or verifies the shared methodology manifest                                                                                         |
| `build-codex-integration-manifest.py`           | Codex            | Deterministically regenerates or verifies the structural machine and human manifests                                                                              |
| `build-codex-agentic-manifest.py`               | Codex            | Deterministically regenerates or verifies the shared 16-task agentic machine and human manifests                                                                  |
| `_bench_common/agentic_contracts.py`            | Provider-neutral | Shared agentic arms, prompt materialization, answer contracts, oracle, parsing, scoring, and paired metrics                                                       |
| `_bench_common/provider_parity_contracts.py`    | Provider-neutral | Canonical task identity, A/B/C semantics, evaluator dispatch, headline eligibility, and paired effects; not a runner or generator                                 |
| `run-claude-agentic.py`                         | Claude           | Agentic benchmark measuring how Codemap/semble structural context changes Claude exploration                                                                      |
| `run-claude-structural.py`                      | Claude           | Repo-agnostic structural benchmark driven by the `tasks-bench.json` repository header                                                                             |
| `run-all.sh`                                    | Provider-neutral | Sole batch dispatcher: no-model cross-provider smoke, paid Claude batches, or approval-gated Codex structural and agentic studies                                 |
| `run-codemap-cli.py`                            | Provider-neutral | Query-level correctness, coverage, and latency against a real repository                                                                                          |
| `run-codex-structural.py`                       | Codex            | Codex structural provider-parity transport for canonical A/B/C cells with isolated plugin homes, native telemetry normalization, and shared structural evaluators |
| `run-codex-agentic.py`                          | Codex            | 16-task Codex agentic runner with shared Claude-parity scoring, treatment isolation, paid admission, telemetry, and partial-artifact preservation                 |
| `generate-tasks-bench.py`                       | Provider-neutral | Validates or refreshes shared structural oracle fields; it does not author prompts                                                                                |
| `generate-tasks-real-issues.py`                 | Provider-neutral | Refreshes shared real-issue evidence                                                                                                                              |
| `suites/tasks-agentic.json`                     | Provider-neutral | Shared 16 blast-radius navigation tasks (BA-01–BA-16), answer contracts, and difficulty tiers used by both agentic adapters                                       |
| `suites/tasks-bench.json`                       | Provider-neutral | 60 tasks across 11 series plus the target repository header                                                                                                       |
| `suites/tasks-code.json`                        | Provider-neutral | 15 code-level tasks used by the scan-query benchmark                                                                                                              |
| `suites/tasks-patch.json`                       | Provider-neutral | 5 end-to-end patch tasks requiring patch application and tests                                                                                                    |
| `suites/tasks-readcrop.json`                    | Provider-neutral | 6 symbol-contract extraction tasks scored by keyword recall                                                                                                       |
| `suites/tasks-fix-single.json`                  | Provider-neutral | 4 single-file fix tasks scored by diff keyword recall                                                                                                             |
| `suites/tasks-fix-multi.json`                   | Provider-neutral | 3 multicaller fix tasks scored by clean patch application, exact changed-path boundaries, and complete-caller AST behavior oracles                                |
| `results/`                                      | Provider-neutral | JSON snapshots and Markdown reports from past runs                                                                                                                |

</details>

### Selecting model strata

`provider-parity-methodology.json` declares the strata each provider runs: `haiku`, `sonnet`, `opus` for Claude and `gpt-5.6-luna`, `gpt-5.6-terra`, `gpt-5.6-sol` for Codex. The Codex list is declared three times and every copy must agree: the methodology manifest is what `--models=` resolves a name against, and the integration and agentic manifests' `model.additional_strata` are what each runner admits at its own preflight. A test asserts the three lists are identical, because a stratum present in one and missing from another is a name the launcher accepts and the runner then refuses. `--models=` restricts and orders that declared list for one invocation — it never introduces a model, so an undeclared name, a Codex name passed to Claude, or a repeated name fails before any study starts.

An explicit selection reaches every selected lane. Multiple models run sequentially in the requested order, with separate model results; omitting a workload selector runs structural first, then agentic. No selected model is silently replaced by a default. Without `--models`, the launcher runs all three Claude tiers or Codex `luna,terra`; an explicit selection replaces those defaults.

| Provider | Workload selector | No `--models`                        | One model                    | Multiple models               |
| -------- | ----------------- | ------------------------------------ | ---------------------------- | ----------------------------- |
| Claude   | `--struct`        | All three tiers: `haiku,sonnet,opus` | Selected tier                | Selected tiers, in order      |
| Claude   | `--agentic`       | All three tiers: `haiku,sonnet,opus` | Selected tier                | Selected tiers, in order      |
| Claude   | None              | Both lanes, all three tiers          | Selected tier in both lanes  | Selected tiers in both lanes  |
| Codex    | `--struct`        | `luna,terra`, in order               | Selected model               | Selected models, in order     |
| Codex    | `--agentic`       | `luna,terra`, in order               | Selected model               | Selected models, in order     |
| Codex    | None              | Both lanes, `luna,terra`             | Selected model in both lanes | Selected models in both lanes |

`--struct` and `--agentic` are mutually exclusive. `--dry-run` is supported for every row and invokes no model. `--repetitions=N` requires `--agentic`; the default is one repetition. The full Codex launcher defaults produce 438 structural cells, 96 agentic cells, or 534 combined cells. Explicitly selecting `luna,terra,sol` produces 144 agentic cells: 16 tasks × 3 arms × 3 models. Models remain separate, nonpoolable studies; one repetition is a screening run, not proof of repeatable savings. Direct single-model runners retain their own defaults; this table describes `run-all.sh`.

A stratum answers to its full declared name or to its nickname — the segment after the last dash — whenever that nickname belongs to exactly one declared stratum, so `gpt-5.6-terra` is also `terra` and the Claude tiers already are their own nicknames. Nicknames are canonicalized to the declared full name before anything is run or hashed, so the two spellings name the same run and mint the same approval. An ambiguous nickname is refused rather than resolved for the operator.

```bash
bash benchmarks/run-all.sh claude --struct --models=opus,haiku       # two tiers, in that order
bash benchmarks/run-all.sh codex --struct --models=luna,terra --dry-run
bash benchmarks/run-all.sh codex --struct --models=terra --dry-run   # second stratum alone
bash benchmarks/run-all.sh codex --agentic --models=luna,terra,sol --repetitions=1 --isolated --dry-run
bash benchmarks/run-all.sh claude --agentic --models=haiku,sonnet --dry-run
bash benchmarks/run-all.sh codex --models=luna,terra --isolated --dry-run  # both lanes, both models
```

Claude uses its existing tier-specific runner dispatch. Codex runs each selected model as its own child study in its own run directory under one aggregate approval. A multi-model dry run discloses the summed design and binds the ordered selection and applicable child scopes. For example, two complete structural studies have this design:

```text
== CODEX MULTI-STRATUM AUTHORIZATION ==
MODELS             gpt-5.6-luna gpt-5.6-terra
DESIGN             438 cells = 219 per stratum × 2 strata (separate, nonpoolable studies)
```

The token commits to the list as given, so reordering or extending it changes the approval. A combined Codex run binds both the structural and agentic scopes and prints only the combined paid command. Omitting `--models` is equivalent to explicitly selecting `luna,terra`; use `--models=luna` for the original single-model scope. Copy the command from a fresh dry run instead of reusing a token from another selection.

Codex sweeps stop on the first child execution failure and preserve completed artifacts. Task-level outcomes remain recorded according to each runner's existing policy; a low score is not itself a launcher failure. `--isolated` gives the invocation a private target worktree and frozen-index relocation shared by its sequential children.

### Codex agentic parity study

The current Codex agentic adapter uses all 16 committed BA tasks across `A_plain`, `B_auto`, and `C_strict`, with one repetition by default. It consumes the shared prompt materializer, response assessor, AST oracle, semantic component scorer, and raw EREC/RREC/DEFF evidence scorer used by Claude. DEFF is an unbounded exposure-hit count per command, not a normalized quality score. Validate one exact 48-cell model plan without credentials or a model:

```bash
bash benchmarks/run-all.sh codex --agentic --models=luna --dry-run
```

The deterministic review lock is `benchmarks/manifests/codex-agentic.json`; regenerate or verify it with `uv run python benchmarks/build-codex-agentic-manifest.py [--check]`. The dedicated human companion records the current manifest SHA, task order, treatment contract, exact approval variable, and retry-inclusive per-cell timeout in seconds. New manifests use their recorded cyclic A/B/C order; three repetitions place every arm in every position once per task as variance screening, not statistical-significance proof or a universal-savings claim. Metadata binds the compared model/effort, manifest-locked repository/index, frozen plugin bytes, isolated-home mode, and observed provider-cache policy; it does not claim cache reset. No-model dry runs require no credentials and no paid approval. A paid run requires the exact active machine-manifest SHA and private auth source; the launcher creates a fresh timestamped run directory automatically, with an optional `CODEX_RUN_DIR` override for a new path. Final run checksums attest the result artifacts, invocation launcher, and `source.sha256`; verify the archived source bytes separately with `(cd "$RUN_DIR/.launcher/source" && shasum -a 256 -c ../source.sha256)`. Codex CLI version is recorded as observed provenance only and is not a pinned or admission requirement. Each cell has only the retry-inclusive per-cell timeout; no total-run ceiling or wall-clock environment/CLI control applies. A non-default repetition or selected scope must additionally present the resolver's scope SHA-256.

Each completed Codex agentic run writes `summary.json` and prints matching compact per-arm rows for both `all_assigned` and `matched_adherent` cohorts. They report gross, cached, fresh, and output tokens; semantic score; runtime; observed use; and adherence. They also expose native command count, heuristic help/discovery count, captured tool-output characters, and supported start/complete-event concurrency and serial-wave diagnostics. Captured characters are not model-visible tokens, and command waves are not model request counts; absent or partial native events leave the affected diagnostic unavailable. The A_plain/C_strict task summary reports quality and fresh-input win/loss separately; it never turns a missing value into a win. `B_auto` remains an optional-use diagnostic canary, not decision-grade evidence. A diagnostic replay never changes source telemetry, answers, token fields, or historical adherence. It writes a new JSON document containing each original row, corrected observed-use fields, and hashes of the source telemetry, run metadata/checksum ledger when present, and detector bytes:

```bash
python3 benchmarks/run-codex-agentic.py \
    --replay-telemetry-path "$RUN_DIR/telemetry.jsonl" \
    --replay-output-path "$REPLAY_DIR/observed-use-replay.json" \
    --replay-launcher-map-path "$RUN_DIR/reviewed-launchers.json"
```

The output path must be outside the historical telemetry directory. The optional map has exactly one `task_id`/`repetition`/`arm` entry per reviewed legacy coordinate, an exact absolute `launcher_path`, and nonempty object `source_evidence`; `launcher_path: null` is valid for A or variable-only rows. New rows retain their per-cell verified launcher path, so they need no map. An absolute command without recorded or mapped provenance is reported as unavailable rather than inferred from its basename. Replay is offline: it reads captured native events and does not invoke a model, query an index, or modify input artifacts.

For approval UX, the matching no-model dry run prints a lowercase 16-character SHA-256 scope prefix for copyable `--paid-approval` (or its equivalent approval variable). The complete 64-character scope SHA-256 remains recorded in run metadata and provenance, and the CLI accepts that full value as well. Never mix a prefix or full scope from another dry run with the selected command; regenerate approval after any locked-source change.

Measured output from this study is reported with every other agentic run under [Agentic results](#agentic-results).

## Unified batch entrypoint

`run-all.sh` is the only batch orchestrator. It requires one provider mode; both providers accept the mutually exclusive `--struct` and `--agentic` workload selectors plus `--dry-run`. `--repetitions=N` is agentic-only. Omitting a workload selector runs structural then agentic for both providers. A combined paid Codex invocation takes one approval token that binds both child scopes together — the unified dry run prints it — freezes one outer source, and preserves isolated `structural/` and `agentic/` child artifacts. The structural child re-derives its scope from its own no-model plan and verifies the combined token there, before any model call; either scope drifting invalidates it. `CODEX_AGENTIC_PAID_APPROVAL` remains the single-study token for `--agentic` runs. Missing or unknown arguments do nothing:

```bash
bash benchmarks/run-all.sh smoke
bash benchmarks/run-all.sh claude
bash benchmarks/run-all.sh claude --struct --dry-run
bash benchmarks/run-all.sh claude --agentic --dry-run
bash benchmarks/run-all.sh codex --struct --dry-run
bash benchmarks/run-all.sh codex --struct --tasks=DI,GR --dry-run
bash benchmarks/run-all.sh codex --agentic --dry-run
CODEX_PAID_APPROVAL=<combined-approval-token-printed-by-the-unified-dry-run> \
    CODEX_AUTH_SOURCE="$HOME/.codex/auth.json" \
    bash benchmarks/run-all.sh codex
CODEX_PAID_APPROVAL="$(shasum -a 256 benchmarks/manifests/codex-integration.json | awk '{print $1}')" \
    CODEX_AUTH_SOURCE="$HOME/.codex/auth.json" \
    bash benchmarks/run-all.sh codex --struct
```

Modes:

- `smoke` — validate the frozen active index, run the deterministic query check, and execute Claude and Codex dry-run/preflight paths. It invokes no model.
- `claude` — validate the shared methodology lock and frozen index, then run the same locked 55 structural tasks as Codex across every canonical coordinate (`A_plain`, `B_auto`, `C_strict`) for each Claude model tier before the agentic batch. The shared revision-bound policy assigns a deterministic hash-randomized arm order per task; it is not counterbalanced, because nothing distributes the six A/B/C permutations evenly across the task set. Only `run-codex-structural.py` counterbalances, by rotating the locked task ordinal through the six permutations. Runner infrastructure failures stop the batch; individual cell outcomes remain recorded by their runner.
- `claude --struct [--dry-run]` — run or plan only the shared 55-task canonical Claude structural matrix with the same deterministic per-task arm-order policy; the agentic preflight and batch are excluded. Direct `run-claude-structural.py` calls retain their legacy arm and 60-task defaults unless `--provider-parity` is explicit.
- `claude --agentic --dry-run` — validate the shared 16-task methodology, resolve the exact one-repeat 144-cell scope across three Claude model tiers, and print the complete no-model plan. `--repetitions=N` derives and passes a distinct scope SHA-256; the same flag without `--agentic` is rejected.
- `claude --agentic` — run only the shared 16-task canonical A/B/C Claude study. The default is one repetition; a higher explicit repetition is admitted only with the launcher's exact derived scope SHA-256.
- `codex [--dry-run]` — plan or execute structural then agentic studies for each selected model: by default Luna and Terra, totaling 438 structural plus 96 agentic cells. Paid mode requires the aggregate manifest-bound approval and one private auth source before either suite starts. `CODEX_RUN_DIR`, when supplied without a selector, is the new combined parent whose child runs are `structural/` and `agentic/`, with separate model subdirectories for sweeps; the sequence stops on the first suite failure and preserves completed artifacts.
- `codex --struct --dry-run` — validate the frozen index, run the deterministic query check and FN-02 Codex smoke, then print the 219-coordinate per-model unified plans (438 total by default). It needs no paid approval, authentication source, or result directory and invokes no model.
- `codex --struct --tasks=DI,GR --dry-run` — resolve the requested structural families, validate the selected scope, exercise the selected no-model preflight, and print the 90-coordinate per-model plan (180 total by default). Selected scopes are targeted and non-poolable; they need no paid approval or authentication source for dry-run.
- `codex --struct` — validate the frozen index, run the fail-fast FN-02 A/B/C smoke and exact no-model plan, then execute all 73 tasks × one repetition × three arms per selected model. Structural, ReadCrop, Fix-Single, Fix-Multi, and Patch outcomes use native scorers and separate child artifacts; cell outcomes are recorded without fail-fast after admission. Paid execution requires the aggregate scope approval and private auth source; the launcher creates a fresh run directory, with an optional `CODEX_RUN_DIR` override for a new path. Each cell uses the retry-inclusive per-cell timeout in seconds; no total-run ceiling is configured.
- `codex --struct --tasks=DI,GR` — execute only the selected, non-poolable task scope after the same smoke and admission gates. Family, exact-ID, and mixed selectors are accepted; the resolved aggregate scope SHA-256 is printed and must authorize that scope.
- `codex --agentic --dry-run` — validate the shared 16-task agentic lock, target, index, and A/B/C capability probes, then print 48 planned cells per selected model (96 total by default) without credentials or a model.
- `codex --agentic` — execute BA-01–BA-16 across A/B/C for one repetition per selected model, using the exact approval printed by the matching dry run and a private auth source. Per-model requirements are documented in `manifests/codex-agentic.md`; a sweep adds aggregate scope approval. Runtime/admission integrity failures stop and preserve partial artifacts; ordinary model/task/treatment outcomes remain measurable and do not fail fast. The launcher creates a fresh run directory, with an optional `CODEX_RUN_DIR` override for a new path. Each cell uses the retry-inclusive per-cell timeout in seconds; no total-run ceiling is configured. Add `--repetitions=N` only with the resolver's matching scope SHA-256.

#### Running two studies at once

Every study mutates its target checkout — staged diff-impact edits, patch checkouts, index rebuilds — and each cell verifies a clean tree before trusting its result, so one shared clone admits one study at a time. A machine-wide lock keyed on the target path enforces that, naming the live run rather than letting two studies corrupt each other's worktree.

`--isolated` gives a run its own `git worktree` off the managed clone, so a second study can run beside the first:

```bash
bash benchmarks/run-all.sh codex --struct --isolated --dry-run
```

- The worktree is created before the lock is taken, so each isolated run locks its own path and no longer contends with the shared clone.
- It is removed when the run succeeds and kept when it fails, with its path printed — a failed run's tree holds the staged edit, half-applied patch, or rebuilt index that explains the failure. The next isolated run prunes what a killed run left behind.
- It never scans a second graph: the locked index is copied into the worktree with only its `scan_root` moved, and the relocation provenance travels to every lane the run launches — Claude and Codex, structural and agentic — because that provenance is what admission checks in place of the byte hash. Relocation needs the managed clone to hold the locked index already, so a first run without `--isolated` has to build it.
- It costs one checkout and one index copy, which is why it is opt-in rather than the default.
- It cannot be combined with `REPO=`; both name the tree to run in, and honouring one silently would put the study somewhere the operator did not ask for.

Any Codex mode whose scope includes a `PT-*` task provisions the Patch stage's test runtime itself, so no separate preparation step is required. The Patch behavior oracle runs Lightning's own tests against the disposable checkout's `src`, which needs an interpreter carrying Lightning's runtime and test dependencies; `run-all.sh` builds one from the target clone's `requirements/{pytorch,fabric}/{base,test}.txt` beside the managed clone at `<temp-root>/codemap-bench-patch-venv` and exports `CODEMAP_BENCH_PATCH_PYTEST` for the run. The first build downloads torch and takes several minutes; later runs reuse it after verifying `import torch`. Setting `CODEMAP_BENCH_PATCH_PYTEST` before the launcher overrides that build with your own executable pytest, and the same value must be exported for both the token-minting dry run and its paid execution because scope admission fingerprints the interpreter and its pytest module. The environment lives outside `$ROOT` so a paid launcher's frozen source snapshot and checksum ledger are unaffected.

Claude preparation validates the shared methodology lock without requiring the Codex integration manifest; Codex and `smoke` retain the full methodology-plus-Codex-manifest cross-check. The Codex smoke preflight now uses the unified `--tasks FN-02` selector and no longer passes removed legacy flags. Paid/model validation remains pending human authorization, a fresh aggregate scope, credentials, and a new output directory.

#### Codex structural runner

`run-codex-structural.py` is task-driven: stage and execution mode are inferred from task selection and dry-run state. Omit `--tasks` to plan or run all 73 supported tasks (55 structural + 6 ReadCrop + 4 Fix-Single + 3 Fix-Multi + 5 Patch), for 219 A/B/C cells at the default one repetition. Use family tokens for a stage or exact IDs for a targeted scope; mixed selectors are allowed, duplicates are removed, and locked manifest order is retained:

```bash
# All supported tasks: 73 tasks × 3 arms = 219 cells
python3 benchmarks/run-codex-structural.py --repo-path /path/to/pytorch-lightning --model gpt-5.6-luna --dry-run

# Selected families: ReadCrop + Fix-Single + Fix-Multi + Patch
python3 benchmarks/run-codex-structural.py --repo-path /path/to/pytorch-lightning --model gpt-5.6-luna --tasks RC,FS,FM,PT --dry-run

# Selected exact IDs, mixed across stage families
python3 benchmarks/run-codex-structural.py --repo-path /path/to/pytorch-lightning --model gpt-5.6-luna --tasks RC-01,FS-03,FM-02 --dry-run
```

Run the no-model `--dry-run` first; omit it only for the approved execution command. ReadCrop, Fix-Single, Fix-Multi, and Patch retain stage-specific scorers and separate, nonpoolable result artifacts. Empty selectors, unknown IDs/families, and invalid mixtures fail before target setup or model admission. `CODEX_PAID_APPROVAL` remains the manifest-bound authorization and stale-manifest lock; no separate boolean paid flag is needed. The dry-run prints a lowercase 16-character SHA-256 prefix accepted by `--paid-approval`; the full 64-character scope remains recorded for provenance and is accepted too.

Credential handling is explicit, not discover-and-search. The security-approved paid-run contract opens only `CODEX_AUTH_SOURCE`, requires a user-owned nonsymlink regular file with mode `0600`, snapshots it into private run-scoped sequential auth state, and atomically propagates the current state into each disposable mode-`0700` Codex home. The source is immutable and drift-checked before each cell; a valid refresh from one cell seeds the next. Cleanup is verified for the run state and every home. Known authentication failures stop immediately; an unknown equivalent zero-token infrastructure failure stops after three matching occurrences, while semantic/model failures remain recorded and continue. Credential bytes, the source path, and standard auth/token/cookie fields are redacted from telemetry and run metadata. Do not run another Codex session concurrently: server-side refresh rotation can invalidate the benchmark state, and the source may require reauthentication after the run. Approval, auth-source, and run-directory controls are removed from measured Codex arm environments.

At paid launch, the runner freezes a run-scoped source bundle containing the benchmark runner, manifests, suites, and plugin sources; later workspace edits cannot affect that run. Only the sample repository and its frozen index remain external inputs. The target is pinned to PyTorch Lightning tag `2.6.5`; the hardcoded ground truth and active manifest reject every other tree. The managed temporary clone is reset to that tag before each mode. `REPO=/path/to/clone` may select an external clone, but the script never resets an override. Preflight still requires the locked clean commit, and index identity is checked in two tiers: at the managed clone the index must reproduce the locked SHA-256 byte for byte, while off that path — an overridden `REPO`, or the private worktree `--isolated` creates — the graph is verified by its path-independent semantic digest and the skipped byte check is announced rather than dropped. The byte hash covers bytes that embed the managed clone's own absolute path, so it can only reproduce there; the Patch stage has verified its own indexes this way since it was written. A missing index is rebuilt and admitted only when normalization of declared environment-specific metadata reproduces the complete locked SHA-256. Every Codex result row records provider, model, effort, task, repetition, arm, telemetry, adherence, Codemap-use, provenance, timing, gross input tokens, cached input tokens, fresh input tokens, output tokens, and limits; `run-metadata.json` is updated after each durable cell. Native Codex input usage is cumulative within a turn, so cached input is a subset of gross input. Gross input is retained for reporting; when `cached_input_tokens <= gross_input_tokens`, fresh input is `gross - cached`; only an inconsistent `cached_input_tokens > gross_input_tokens` row is reported as `?` and token-ineligible.

### Codex result artifacts and ordering

The append-only `telemetry.jsonl` is the execution record. Rows retain `execution_index` and the actual randomized arm order so interrupted runs can be audited without rewriting history. The runner rejects existing raw/metadata artifacts for a new run; partial runs are audit-only and are never resumed, pooled, or re-scored as confirmatory evidence. Before setup, paid `run-all.sh` execution copies itself to a mode-`0500` private launcher under the new run directory and re-executes that snapshot. The runner archives the exact launcher bytes, validates the manifest-bound SHA-256 before and after every cell and at completion, and fails the run if those bytes drift. A successful run also emits `telemetry-canonical.jsonl`, an atomically written derived view sorted by locked task position, repetition, and fixed treatment order. Human labels, machine telemetry, and manifest IDs all use the same canonical arm names: `A_plain`, `B_auto`, and `C_strict`. Terminal summaries and later paired analysis use the canonical view; raw and canonical files are never pooled or silently substituted. `run-metadata.json` records the canonical artifact status and SHA-256 alongside the raw telemetry hash.

The human result line uses fixed columns and compact units (`k` = 1,000; `M` = 1,000,000). Each top-level smoke, Codex paid, or diagnostic paid section emits exactly one shared terminal legend; nested preflight/study sections do not repeat it. Legends use `A_plain`, `B_auto`, and `C_strict` for plain, optional-use, and required-Skill. The console reports gross input only; cached and fresh remain raw telemetry fields (`fresh = gross - cached` when consistent). `quality` is continuous fitness in `[0, 1]`; `treatment:✓|✗` answers treatment adherence; `codemap-used:✓|✗` answers observed Codemap use. The observed Codex CLI exposes no supported per-cell provider prompt-cache reset/disable, so this runner's six-permutation counterbalancing mitigates order exposure without claiming cache elimination. That balance is specific to `run-codex-structural.py`; the shared policy other lanes use randomizes order deterministically without balancing the permutations, and the agentic lanes are not covered by this paragraph. Machine telemetry and manifest IDs use these same canonical names.

The installed-Skill treatment binds a compact Skill and requires one successful canonical query; an explicit full-Skill file read is optional audit telemetry, not productive-use ceremony. The direct CLI remains intentionally bare, but top-level CLI help and query help expose valid subcommands and explicit count semantics. `undocumented` distinguishes declaration totals from unique symbols; `uncovered` identifies its static-query coverage semantics. These usability fixes are part of the shared product surface and are tested independently from the paid provider study.

The earlier remediation candidate had deterministic source-transfer proof: its 74-file Codemap package passed package-manifest validation with manifest SHA-256 `fa1a89d86fedc3bc94ce54167378a138065b4a81057ef47007d81bec4adef57f`, and a disposable Codex install/runtime probe passed for the six Codemap skills against schema 12. The active candidate and its exact source/cache Skill hash are recorded in the current integration-study status above. No authentication source was read.

<details>
<summary>Manual equivalent — paste-safe, no inline <code>#</code> comments (interactive zsh does not strip them and passes them as args)</summary>

```bash
cd ~/Workspace/Borda.local
REPO=.sandbox/pytorch-lightning
[ -d "$REPO/.git" ] || git clone --depth 1 --branch 2.6.5 https://github.com/Lightning-AI/pytorch-lightning.git "$REPO"
git -C "$REPO" reset --hard 2.6.5 && git -C "$REPO" clean -fd
CM=plugins/codemap-py/bin/codemap-py

"$CM" index --root "$REPO"
python benchmarks/run-codemap-cli.py --repo-path "$REPO" --report
python benchmarks/run-claude-structural.py --repo-path "$REPO" --run-all --model haiku
python benchmarks/run-claude-structural.py --repo-path "$REPO" --run-all --model sonnet
python benchmarks/run-claude-structural.py --repo-path "$REPO" --run-all --model opus
python benchmarks/run-claude-agentic.py "$REPO" --run-all --report
```

</details>

- **Order**: validate frozen index → query (gates the index, no LLM) → real-codebase → agentic. Per-benchmark options live in each section's **Quick start** below.

- **Scale**, by regime — `tasks-bench.json` holds 60 structural tasks, of which 5 are the RI series, so the count depends on which regime is running:

  - legacy default (no profile, RI gated out) = 55 × 2 legacy arms × 3 tiers = 330 model runs
  - legacy `--profile release` (RI included) = 60 × 2 × 3 = 360
  - provider-parity structural (`--provider-parity`, RI excluded from the shared population) = 55 × 3 canonical arms × 3 tiers, of which 45 tasks are preregistered headline blocks and 10 are diagnostics
  - Claude agentic launcher default = 16 × 3 × 3 = 144; Codex agentic launcher default = 16 × 3 × 2 = 96 (48 per model)

  These are separate provider studies with shared task, prompt, oracle, and scorer contracts.

- **Model tiers** (`MODELS` map in each runner): `haiku` → `claude-haiku-4-5`, `sonnet` → `claude-sonnet-5`, `opus` → `claude-opus-5`.

- **Agentic arms**: canonical runs use `A_plain`, `B_auto`, and `C_strict`. Legacy Claude `semble` / `combined` arms remain explicit historical compatibility paths and need the semble MCP configured.

- **Cheaper option**: swap the three bench lines for the tiered strategy (`--tiered`, see [Cost profiles](#cost-profiles)) — full suite on haiku, dev subset on sonnet, only cross-tier disagreements on opus.

- **Results** land in `benchmarks/results/` — `code-<date>.md`, `bench-<model>-<ts>.jsonl`, and agentic JSON (`.md` with `--report`).

## Agentic benchmark (shared Claude/Codex contract)

Runs the same committed 16 import-graph tasks under the canonical three arms. Claude runs all three model tiers by default; Codex runs one repetition by default and requires exact scope approval for any expanded repetition count.

| Arm        | Codex treatment                                                       | Claude treatment                                                      |
| ---------- | --------------------------------------------------------------------- | --------------------------------------------------------------------- |
| `A_plain`  | Codemap absent and inaccessible                                       | Codemap absent and inaccessible                                       |
| `B_auto`   | Direct Codemap CLI available; use is optional                         | Codemap available; use is optional                                    |
| `C_strict` | Codemap Skill available; at least one successful compact query needed | Codemap Skill available; at least one successful compact query needed |

The shared answer envelope is materialized from each task's committed `answer_contract`. The shared parser and oracle score exact fields plus continuous component fitness; `EREC`, `RREC`, and `DEFF` remain diagnostic metrics. Missing or malformed required fields score zero for that component without changing transport/admission status.

### Legacy Claude-only arms

Historical Claude compatibility runs can still be selected explicitly under these four arms; they are not part of the current provider-parity default and are not pooled with canonical A/B/C results:

| Arm        | Tools available                                                                           |
| ---------- | ----------------------------------------------------------------------------------------- |
| `plain`    | Grep / Glob / Bash only                                                                   |
| `codemap`  | + `/codemap:query` skill (structural AST index); semble blocked                           |
| `semble`   | + `mcp__semble__search` MCP tool (hybrid semantic + lexical search); Skill + Bash blocked |
| `combined` | Both `/codemap:query` and `mcp__semble__search`; no restrictions                          |

**Legacy prompt symmetry (2026-07-03)**: the four historical arms share one neutral base prompt — identical task framing, identical "Required answer format" block, and one shared efficiency sentence ("Answer in as few tool calls as possible; do not re-verify results you already have."). Arm supplements carry tool availability + invocation syntax only. Earlier versions steered arms asymmetrically (plain coached toward more grepping; codemap capped at 3 calls and forbidden to verify; semble/combined given prescriptive protocols) — that steering contaminated efficiency metrics, so results produced before this date are not comparable with new runs. Canonical A/B/C runs use the provider-neutral materialized prompt and answer contract described above.

**Ground truth (2026-07-03)**: expected rdeps come from an independent AST scan of the repo (absolute, aliased, `from`-import, and relative forms resolved), not from the codemap index. The index-derived list is kept as a diagnostic; divergence is printed per task as `[gt-divergence] BA-XX: ast=N index=M ...` — a divergence now signals a potential plugin bug instead of being invisible.

**Metrics**: tool call count, elapsed time, input tokens, exposure recall (erec), top-10 exposure recall (e@10), report recall (rrec), discovery efficiency (deff).

| Metric | What it measures                                                                                                                                                                      |
| ------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `erec` | Fraction of ground-truth rdeps found in agent output_text (tool results excluded; arm-fair)                                                                                           |
| `e@10` | erec restricted to the 10 most-central rdeps, ranked by reverse-dependency count (in-degree — how many modules import each), matching the "imported by the most modules" task wording |
| `rrec` | Fraction of ground-truth rdeps present in the agent final written answer only                                                                                                         |
| `deff` | Ground-truth reverse dependencies exposed per tool call: `erec_tp / max(tool_calls, 1)`                                                                                               |

<details>
<summary><strong>Tasks</strong></summary>

16 tasks: 4 types (fix / feature / refactor / review) x 4 difficulty tiers (simple / medium / hard / extreme). Difficulty maps to rdep count: simple 1-4 * medium 5-15 * hard 16-50 * extreme 50+.

| ID    | Type     | Difficulty | Primary module                                              | Scenario                                                                                |
| ----- | -------- | ---------- | ----------------------------------------------------------- | --------------------------------------------------------------------------------------- |
| BA-01 | fix      | simple     | `lightning.pytorch.callbacks.timer`                         | Timer bug: `timedelta` compared as float, premature training stop                       |
| BA-02 | fix      | medium     | `lightning.pytorch.core.optimizer`                          | LR scheduler fires twice per batch when `optimizer_step` overridden                     |
| BA-03 | fix      | hard       | `lightning.pytorch.utilities.model_helpers`                 | `is_overridden` returns True for inherited methods — silent callback errors             |
| BA-04 | fix      | extreme    | `lightning.pytorch.utilities.exceptions`                    | Rename `MisconfigurationException` to `LightningConfigError` — assess full blast radius |
| BA-05 | feature  | simple     | `lightning.pytorch.callbacks.finetuning`                    | Add `freeze_until_epoch` — scope callers before coding                                  |
| BA-06 | feature  | medium     | `lightning.fabric.utilities.load`                           | Add `map_location` to checkpoint loaders — assess caller integration surface            |
| BA-07 | feature  | extreme    | `lightning.fabric.utilities.rank_zero`                      | Add `group` parameter to rank-zero logging — find dual-importer consistency risk        |
| BA-08 | feature  | extreme    | `lightning.fabric.utilities.types`                          | Add `ReduceOp` protocol, deprecate `torch.distributed.ReduceOp`                         |
| BA-09 | refactor | simple     | `lightning.pytorch.callbacks.lr_finder`                     | Extract `_lr_find` helper into standalone function — classify callers                   |
| BA-10 | refactor | medium     | `lightning.fabric.plugins.environments.cluster_environment` | Rename `creates_processes_externally` — enumerate all call sites                        |
| BA-11 | refactor | hard       | `lightning.fabric.utilities.distributed`                    | Replace barrier wrappers with `DistributedBarrier` context manager                      |
| BA-12 | refactor | hard       | `lightning.pytorch.callbacks`                               | Split `callbacks.__init__` into training/evaluation sub-modules                         |
| BA-13 | review   | simple     | `lightning.pytorch.strategies.deepspeed`                    | PR adds ZeRO-3 CPU offload — verify isolation                                           |
| BA-14 | review   | medium     | `lightning.fabric.plugins.precision.utils`                  | PR makes `_convert_fp_tensor` dtype arg keyword-only — quantify coupling                |
| BA-15 | review   | hard       | `lightning.pytorch.utilities`                               | PR removes 3 deprecated symbols — identify non-migrated callers                         |
| BA-16 | review   | extreme    | `lightning.pytorch.utilities.rank_zero`                     | PR replaces `rank_zero_warn` with deduplicating variant — full risk assessment          |

</details>

### Quick start

```bash
# 1. Install deps (source of truth: pyproject [dependency-groups] bench)
pip install --group pyproject.toml:bench   # or: uv sync --only-group bench

# 2. Build codemap index once (excluded from benchmark timing)
python plugins/codemap-py/bin/scan-index --root /path/to/repo

# 3. Run the shared 16-task canonical A/B/C suite across all Claude model tiers
python benchmarks/run-claude-agentic.py --repo-path /path/to/repo --run-all --report

# 4. Spot-check one task
python benchmarks/run-claude-agentic.py --repo-path /path/to/repo \
    --tasks "['BA-01']" --arm plain --model haiku

# Run only non-semble arms (if semble not configured)
python benchmarks/run-claude-agentic.py --repo-path /path/to/repo --run-all --arm plain
python benchmarks/run-claude-agentic.py --repo-path /path/to/repo --run-all --arm codemap
```

<details>
<summary><strong>Enabling the semble arm (required for semble + combined)</strong></summary>

See [semble docs](https://github.com/MinishLab/semble) for full MCP server documentation. One-time setup:

```bash
claude mcp add semble -s user -- uvx --from "semble[mcp]" semble
```

`-s user` registers it globally (all projects). Use `-s project` to scope to this repo only.

**Verify** — the preflight check in `run-claude-agentic.py` will raise a `RuntimeError` with instructions if semble is not found.

</details>

<details>
<summary><strong>CLI flags</strong></summary>

| Flag                                                                | Default         | Description                                                              |
| ------------------------------------------------------------------- | --------------- | ------------------------------------------------------------------------ |
| `--repo-path PATH`                                                  | required        | Absolute path to the repo under test                                     |
| `--index PATH`                                                      | auto-detected   | Override index path (default: `<repo>/.cache/scan/<name>.json`)          |
| `--arm plain\|codemap\|semble\|combined\|A_plain\|B_auto\|C_strict` | canonical A/B/C | Run a single legacy or canonical arm only                                |
| `--model haiku\|sonnet\|opus`                                       | all three       | Run a single model tier only                                             |
| `--tasks "['BA-01','BA-02',...]"`                                   | all 16          | Run specific task IDs (Python list literal — e.g. `"['BA-01','BA-02']"`) |
| `--run-all`                                                         | off             | Run all tasks (required unless `--tasks` given)                          |
| `--report`                                                          | off             | Write markdown report to `results/` after run                            |
| `--repeat N`                                                        | `1`             | Repeat each selected cell; values above one require `--scope-sha256`     |
| `--scope-sha256 SHA`                                                | none            | Admit the exact derived nondefault canonical scope                       |
| `--resolve-scope`                                                   | off             | Print the selected canonical scope and SHA-256 without running a model   |
| `--dry-run`                                                         | off             | Print the selected plan without invoking Claude or writing model results |

</details>

### Output

Each run prints one coloured line; canonical labels are `A_plain`, `B_auto`, and `C_strict`:

```
[NN/TT] BA-01 (fix) | haiku  | C_strict | elapsed= 45.2s | tokens= 120.3k | calls= 3 (grep=  0; glob= 0; bash=  0; skill= 1; semble= 0) | erec= 94% rrec= 88%  sc=100%
```

Colour: canonical `A_plain` = yellow · `B_auto` = cyan · `C_strict` = magenta; legacy `semble`/`combined` colors remain for explicitly selected compatibility runs; red = failure.

JSON snapshot written to `results/agentic-YYYY-MM-DD[-N].json` after every run (partial results survive interruptions). Markdown report written to `results/agentic-YYYY-MM-DD[-N].md` with `--report`.

### Failure conditions

| Condition              | Meaning                                                                                                                   |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| `timeout`              | claude subprocess exceeded 300 s                                                                                          |
| `non-zero exit`        | claude returned non-success subtype                                                                                       |
| `codemap no-call`      | codemap arm never called the Skill tool                                                                                   |
| `semble no-call`       | semble arm never called `mcp__semble__search` or `mcp__semble__find_related`                                              |
| `degenerate_grep_loop` | codemap arm made zero skill calls but ≥70% of calls were grep/bash-grep — index ignored, fell back to plain-arm behaviour |

______________________________________________________________________

## Real-codebase benchmark (`run-claude-structural.py`)

Measures whether `scan-query` structural access reduces token usage and improves recall on pre-implementation structural research — symbol lookup, call-graph navigation, code review metrics, code quality health checks, and blast-radius assessment before modifying code.

**Benchmark philosophy**: this benchmark measures the *pre-implementation structural research* phase — locating symbols, enumerating callers, assessing blast radius before any code is written. It is not an end-to-end developer workflow benchmark. Tasks have rigid output format constraints (`benchmark_shaped: true`) or qualitative-only ground truth (`scoreable: false`), which determines whether a run contributes to the accuracy denominator. The D-series tasks model a concrete production safety gate: a developer must enumerate ≥70% of a function’s callers before modifying it. Missing 30%+ callers in real code means blind refactoring — broken call sites, silent regressions. The 0.70 threshold exists because that is the practical boundary, not to produce a metric.

Two arms run the same tasks:

| Arm       | Tools available                      |
| --------- | ------------------------------------ |
| `plain`   | Grep / Bash / Glob / Read only       |
| `codemap` | + scan-query (via PATH) + Skill tool |

**Primary metric**: `token_ratio = codemap_input_tokens / plain_input_tokens` per task. Values below 1.0 mean codemap arm used fewer tokens.

**Secondary**: per-arm accuracy — fraction of *scored* tasks where the key metric matches ground truth within tolerance. Incomplete and contaminated runs are excluded from the denominator and reported separately.

### Task series

60 tasks in `tasks-bench.json`, eleven series:

| Series | Type                   | Tasks        | What the agent must find                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| ------ | ---------------------- | ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| SE     | `symbol_extraction`    | SE-01..SE-05 | Source file line range for a named symbol                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| FN     | `fn_call_graph`        | FN-01..FN-05 | Unique caller count for a function (static call graph)                                                                                                                                                                                                                                                                                                                                                                                                                          |
| RV     | `review_assistance`    | RV-01..RV-05 | Doc-gap counts, rdep counts, coverage gaps for code review                                                                                                                                                                                                                                                                                                                                                                                                                      |
| CQ     | `code_quality`         | CQ-01..CQ-05 | Coupling, broken xrefs, combined doc+coverage health                                                                                                                                                                                                                                                                                                                                                                                                                            |
| BR     | `develop_blast_radius` | BR-01..BR-09 | Caller recall ≥70% before modifying a function; developer workflow framing; calibratable via `/foundry:calibrate`. **n=9** — report accuracy as fractions. BR-06..BR-08 GT = fn-rdeps AST callers. BR-09 is a **high caller-fan-in** stratum member (`fanin_tier: "high"`, `profiles: ["fanin"]`) — `MisconfigurationException`, fan-in 102 (~2.8× the prior BR ceiling of 37); GT = qualified AST caller oracle. Codemap arm uses `scan-query` via Bash PATH (not Skill tool). |
| DG     | `debug_from_trace`     | DG-01..DG-06 | Root-cause function + file from a traceback or log line                                                                                                                                                                                                                                                                                                                                                                                                                         |
| FT     | `feature_scaffolding`  | FT-01..FT-05 | Which files to create or modify for a described new feature                                                                                                                                                                                                                                                                                                                                                                                                                     |
| RI     | `real_issue`           | RI-01..RI-05 | Files relevant to a real GitHub issue (recall ≥ 0.70)                                                                                                                                                                                                                                                                                                                                                                                                                           |
| DI     | `diff_impact`          | DI-01..DI-06 | Blast radius of a *staged* change: production callers (recall ≥ 0.70) + test modules to re-run (recall ≥ 0.70). Runner stages the change around both arms then reverts (refuses on a dirty tree). GT = pre-change AST caller oracle + test-import oracle.                                                                                                                                                                                                                       |
| GR     | `graph_*`              | GR-01..GR-04 | Graph queries: `central` top-N most-imported (set overlap ≥ 0.70), `path` A→B shortest import chain (unique-path pairs only), `fn-blast` depth-2 transitive callers (recall ≥ 0.70). GT = AST central / path / fn-blast oracles.                                                                                                                                                                                                                                                |
| MB     | `module_blast_radius`  | MB-01..MB-05 | Import fan-in: enumerate the modules that IMPORT a target module (its rdeps) — the module-level reverse of BR's per-function callers. Importer recall ≥ 0.70; module names matched by ≥2-component dotted form (never a bare leaf). Fan-in stratum (`profiles: ["fanin"]`) over import hubs. GT = AST importer oracle (`_module_importers_via_ast`), test modules excluded.                                                                                                     |

**SE — Symbol extraction.** Asks the agent to locate where a named symbol is defined and report its start line — the foundation of every "go-to-definition" and "find references" workflow in real development. Plain agents must grep the repo and read candidate files to confirm the match, which burns tokens and still fails when symbol names are ambiguous across modules or appear in strings. A codemap index stores each symbol's qualified name and source range directly, so a single `scan-query symbols` lookup returns the canonical location without reading any source file.

**FN — Function call graph.** Asks the agent to enumerate every unique function that calls a given target — the "who calls me?" question developers ask before refactoring an interface, adding a parameter, or deprecating a function. Plain agents rely on grep-based discovery, which counts raw string occurrences including imports and comments, and silently misses aliased imports and same-file callers. The codemap AST-derived call graph resolves qualified names structurally, producing an exact count unaffected by lexical ambiguity.

**RV — Review assistance.** Asks the agent to answer quantitative code-review questions — how many symbols lack docstrings, how many modules import a given module, how many public symbols have no test coverage — the metrics a reviewer needs to assess whether a change degrades coverage or widens blast radius. Plain agents must read entire module source files and cross-reference test files, a process that scales poorly and produces inconsistent counts due to varying output formats. The `scan-query` subcommands (`undocumented`, `rdeps`, `uncovered`) answer each question with a single structured JSON response containing an exact `count` and the full qualified-name list.

**CQ — Code quality.** Asks the agent to surface structural health metrics used at release gates — the most-coupled module, symbols with broken cross-references in docstrings, combined documentation and coverage deficits. Plain agents must invoke independent file reads for each metric and often miss cases requiring whole-graph reasoning such as transitive coupling. The codemap index exposes `coupled`, `xrefs --broken`, `undocumented`, and `uncovered` queries that inspect pre-built structural graphs and return ranked, quantified results.

**BR — Develop blast radius.** Asks the agent to enumerate all direct callers of a function *before* making a change — the most operationally critical series, since missing callers of a function being refactored ships silent breakage. Plain agents miss aliased callers, same-file callers unreachable by grep, and callers whose import path differs from the module name, requiring dozens of file reads to validate each hit. The codemap `fn-rdeps` subcommand returns the AST-derived caller list directly, reaching high recall without reading a single source file. Recall ≥ 0.70 is a _partial coverage threshold_ — a passing score can still miss up to 30% of direct callers. Do not interpret pass as a production-safety guarantee; for safety-critical refactors, require near-exhaustive enumeration or explicitly bound missed-caller count.

**DI — Diff impact.** Stages a *scripted synthetic change* to a widely-called function (a new keyword-only parameter, a body edit) before the task, then asks the agent to assess the blast radius: which production callers are affected and which test modules must be re-run. The runner applies the change once, runs BOTH arms against the identically-staged tree, and reverts every touched path with `git checkout -- <path>` in a `finally` block — so neither arm sees a different tree and the change never outlives the task. The series refuses to run against a dirty target tree (a `DirtyTreeError`), because reverting a path the user had already modified would silently clobber their edits. Ground truth is the *pre-change* AST caller oracle (`_callers_via_ast`) unioned with the test modules importing the changed module (`_test_modules_importing_via_ast`) — both independent of scan-query. Correctness requires caller recall ≥ 0.70 AND test-file recall ≥ 0.70; the plain arm answers by grep/read over the same staged change. The codemap arm may use `diff-impact` (structural blast radius of the git change set) directly.

**GR — Graph navigation.** Three whole-graph queries a developer runs before a cross-cutting change: `central` (the top-N most-imported modules — where a breaking change hurts most; GT `_central_via_ast`, set overlap ≥ 0.70), `path` (a shortest import chain A→B — how two modules are coupled; GT `_import_path_via_ast`, scored on ordered-chain match, and pairs are chosen where `_shortest_path_is_unique` so the oracle path is the only valid answer), and `fn-blast` (the depth-2 transitive caller closure of a function — the indirect blast radius; GT `_fn_blast_via_ast`, recall ≥ 0.70). Plain agents must build the import graph by hand across dozens of reads; the codemap `central` / `path` / `fn-blast` subcommands answer each from the pre-built graph in one call.

**MB — Module blast radius.** Asks the agent to enumerate every module that IMPORTS a target module — its import fan-in (reverse dependencies). This is the module-level reverse of BR's per-function caller enumeration: before changing a widely-imported hub (an exceptions module, a shared types module), a developer needs the set of modules that would be affected. The five MB tasks target import hubs in `pytorch-lightning` (`utilities.exceptions`, `fabric.utilities.types`, `utilities.rank_zero`, `fabric.utilities.imports`, `trainer.states`), each imported by dozens of production modules. Plain agents must grep every `import`/`from` statement across the tree and dedupe by module, missing re-export aliases and relative imports; the codemap `rdeps` subcommand returns the AST-derived importer list directly. Ground truth is the AST importer oracle (`_module_importers_via_ast`, the reverse of `_central_via_ast`) with test modules excluded; correctness is importer recall ≥ 0.70, and a candidate module counts only via a ≥2-component dotted form (a bare leaf name never scores).

### Quick start

```bash
# 1. Install deps (source of truth: pyproject [dependency-groups] bench)
pip install --group pyproject.toml:bench   # or: uv sync --only-group bench

# 2. Build index once
python plugins/codemap-py/bin/scan-index --root ./<repo-dir>

# 3. Run all 60 tasks, both arms, haiku model
python benchmarks/run-claude-structural.py \
    --repo-path ./<repo-dir> \
    --run-all --model haiku

# 4. Run one series (e.g. symbol tasks only)
python benchmarks/run-claude-structural.py \
    --repo-path ./<repo-dir> \
    --task-type symbol_extraction --arm codemap --model haiku

# 5. Spot-check one task
python benchmarks/run-claude-structural.py \
    --repo-path ./<repo-dir> \
    --tasks "['SE-01']" --arm plain --model haiku
```

<details>
<summary><strong>CLI flags</strong></summary>

| Flag                                                   | Default       | Description                                                                                                                                                                                              |
| ------------------------------------------------------ | ------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--repo-path PATH`                                     | auto          | Path to repo clone (default: `repo.local_path` from `tasks-bench.json`)                                                                                                                                  |
| `--index-path PATH`                                    | auto          | Override index; checks `.cache/codemap/` then `.cache/scan/`                                                                                                                                             |
| `--tasks "['SE-01','FN-02',...]"`                      | all           | Run specific task IDs (Python list literal — e.g. `"['SE-01','FN-02']"`)                                                                                                                                 |
| `--task-type TYPE`                                     | all           | Filter by type: `symbol_extraction`, `fn_call_graph`, `review_assistance`, `code_quality`, `develop_blast_radius`, `debug_from_trace`, `feature_scaffolding`, `real_issue`                               |
| `--arm plain\|codemap\|A_plain\|B_auto\|C_strict\|all` | `all`         | Run one legacy arm, one canonical A/B/C arm, or both legacy arms                                                                                                                                         |
| `--model haiku\|sonnet\|opus`                          | `haiku`       | Model tier                                                                                                                                                                                               |
| `--run-all`                                            | off           | Required when `--tasks` and `--task-type` both absent                                                                                                                                                    |
| `--no-save`                                            | off           | Skip writing JSONL results to `results/bench-<model>-<ts>.jsonl`                                                                                                                                         |
| `--timeout N`                                          | model default | Per-run wall-clock timeout in seconds                                                                                                                                                                    |
| `--resume`                                             | off           | Reuse a matching prior result (same `task_id`/`arm`/`model` + `repo_sha`/`index_sha`/`task_hash` provenance) from `results/bench-*.jsonl` instead of re-executing it; reused lines carry `resumed: true` |
| `--profile dev\|release`                               | none          | Cost profile — `dev` = haiku-only stratified subset (fast regression signal), `release` = full matrix incl. RI. Absent → current behavior unchanged                                                      |
| `--tiered`                                             | off           | Tiered protocol (release companion): run one tier per `--model` (haiku full → sonnet dev-subset → opus disagreements). See **Cost profiles** below                                                       |
| `--dry-run`                                            | off           | Validate locked canonical inputs and print the planned A/B/C cells; never invoke Claude or write model results                                                                                           |

When `--resume` is set, provenance is fingerprinted per run: `repo_sha` = `git -C <repo> rev-parse HEAD` (or `"unknown"`), `index_sha` = sha256 of the index head-meta (`scan_version`, `scanned_at`, `git_sha`, `project`, `scan_root`), and `task_hash` = sha256 of the task's canonical JSON. These three fields are written on **every** result line (not only under `--resume`), so any prior run is resumable later. A resume match reuses the stored line verbatim and skips the `claude` subprocess entirely.

</details>

### Output

Per-run line printed during execution:

```
  ✓+ SE-01    codemap tok= 45230 got=      146 exp=      146
  ✓- SE-01    plain   tok= 89410 got=     None exp=      146
```

`✓` = subprocess success · `✗` = failure · `+` = quality correct · `-` = quality wrong · `!` = incomplete (budget exhausted) · `c` = contaminated (plain arm accessed codemap) · `?` = not evaluated.

Summary table printed after all runs:

```
Token ratio (codemap/plain):
  median = 0.37  mean = 1.05
  plain accuracy = 62.5%  (5/8 scored)
  plain incomplete = 1 (budget exhausted — not scored: BR-04)
  codemap accuracy = 87.5%  (7/8 scored)
```

**Interpreting results**: single-run accuracy is a point estimate with high per-task variance (≥5 of 8 tasks can flip between runs at n=1). The direction (codemap vs plain) is stable at n=1; per-task verdicts and magnitudes are not. Run ≥3 times and report mean ± stderr for reliable per-task comparison. Focus on `recall` and `token_ratio`, not the pass/fail threshold alone.

Results written to `results/bench-<model>-<YYYYMMDD-HHMMSS>.jsonl`. Each per-task JSONL record carries `scan_query_subcommands` (per-subcommand call counts, with batched inner `cmd`s attributed to their own counters) and `used_batch: bool` (whether the codemap arm invoked `scan-query batch` at least once — batch use is measured, never forced).

### Cost profiles

The full matrix (60 tasks × 2 arms × 3 model tiers) is expensive. Three cost levers trade coverage for spend; when none are passed the runner behaves exactly as before.

- **dev** (`--profile dev`) — haiku-only, a stratified ~12-task subset with ≥1 task per series (SE/FN/RV/CQ/BR/DG/FT + DI/GR, RI skipped; the MB fan-in stratum is likewise out of the dev subset). Purpose: a fast regression signal that a change did not break the runner or a series. Subset membership is declared per task in `tasks-bench.json` via a `"profiles": ["dev"]` tag, not hardcoded in the runner, so it can be re-stratified without a code change. The dev subset excludes the self-consistency tasks so its accuracy stays clean.

- **release** (`--profile release`) — the full matrix, including the RI (real_issue) series. RI is gated to release (or an explicit `--tasks`/`--task-type` selection) because those runs are ~2M-token outliers — the plain arm greps the whole tree — and would dominate the cost of a routine run.

  RI task provenance is a snapshot, not a reproducible derivation. `generate-tasks-real-issues.py` selects its issues from live GitHub ordered by most-recently-updated, so rerunning it later yields a different issue set as upstream activity reorders the query. The committed RI-01..RI-05 task objects are the record; treat the generator as the tool that produced that snapshot on one day rather than as a reproducible oracle. RI is outside the shared provider-parity population for this reason among others.

- **tiered** (`--tiered`, a release companion) — spend opus budget only where it adjudicates. Run one tier per invocation, escalating:

  ```bash
  python benchmarks/run-claude-structural.py --repo-path ./<repo> --tiered --model haiku   # full suite on haiku
  python benchmarks/run-claude-structural.py --repo-path ./<repo> --tiered --model sonnet  # dev subset on sonnet
  python benchmarks/run-claude-structural.py --repo-path ./<repo> --tiered --model opus    # only haiku/sonnet disagreements
  ```

  Each invocation reads the earlier tiers' results from the same `results/` dir (matched on `repo_sha` + `index_sha` provenance). The opus tier runs **only** the tasks where the haiku and sonnet `quality.correct` verdicts disagree — the cases a stronger model is worth paying for. Three separate invocations were chosen over one orchestrated run because it fits the runner's per-model structure and lets each tier's cost be inspected and resumed independently (`--resume`).

**Prompt-cache note.** Within one arm, tasks already execute serially against a stable system prefix (the shared neutral wrapper plus the arm's fixed tool section — identical across all tasks of that arm). A stable, repeated system prefix is exactly what prompt caching prices down, so serial-per-arm execution already benefits from cache pricing without any extra flag; the cost profiles reduce the *number* of runs, not the per-run cache economics.

### Ground truth

`tasks-bench.json` ships with pre-verified ground truth. To validate or refresh against a live index:

```bash
# Validate all tasks (exits 1 on any mismatch)
python benchmarks/generate-tasks-bench.py --repo-path ./<repo-dir>

# Validate single task
python benchmarks/generate-tasks-bench.py --repo-path ./<repo-dir> --task SE-01

# Refresh AST-oracle-backed ground truth (fn/br/rv/cq — every code_quality check now has an
# independent AST oracle; overwrites tasks-bench.json)
python benchmarks/generate-tasks-bench.py --repo-path ./<repo-dir> --update --verbose

# Also refresh the remaining scan-query-derived fields (symbol line ranges, coupled) — circular;
# prints warning + oracle diff
python benchmarks/generate-tasks-bench.py --repo-path ./<repo-dir> --update --update-from-tool --verbose
```

______________________________________________________________________

## Query benchmark (`run-codemap-cli.py`)

Validates `scan-query` directly — no LLM involved. Requires a pre-built index.

Seven suites run together, split into two tracks. **Primary** suites (C / A / L / Q) decide the verdict. **Self-consistency** suites (S / H / X) validate scan-query against ground truth that is itself partly scan-query-derived — they check determinism, not independent correctness, and are reported separately (excluded from the verdict).

| Suite             | Codes                      | What it measures                                                                                                                                                              |
| ----------------- | -------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **C** Coverage    | C1 C2 C3                   | AST-verified importers codemap finds beyond boundary-anchored grep (real set comparison since 2026-07-03)                                                                     |
| **A** Accuracy    | A1 A2 A3                   | Precision (vs AST-verified importer oracle) + recall floor (vs boundary-anchored grep) on rdeps queries — codemap is not penalized for importers grep cannot see              |
| **L** Latency     | L1 L2 L3 L4                | Wall-clock time for `central`, `rdeps`, index build, vs cold grep baseline                                                                                                    |
| **Q** Query-shape | Q_fix Q_feature Q_refactor | scan-query returns well-formed JSON with the fields develop/oss skills expect (`has_rdeps`+`has_deps`) — shape validation only; does NOT exercise the SKILL.md injection path |
| **S** Symbol      | S_SE-01..SE-05 S2          | `symbol` command returns correct start/end lines (ground truth: `tasks-bench.json`)                                                                                           |
| **H** Health      | H_CQ-\* H1 H2              | `undocumented`/`uncovered` totals match `tasks-bench.json` ground truth                                                                                                       |
| **X** Xrefs       | X_CQ-04 X1                 | `xrefs --broken` count + target set match `tasks-bench.json` ground truth                                                                                                     |

Suites S, H, X auto-skip (no error) when `tasks-bench.json` is absent.

**Deterministic correctness suites (D / B / R / K / U).** In addition to the seven tracks above, `run-codemap-cli.py` runs five deterministic correctness suites, and — unlike S/H/X — they **join the primary verdict**. Each builds a self-contained fixture repo in a tmp dir whose ground truth is KNOWN by construction (an exact importer count, an exactly-corrupted index, a single broken sphinx xref), so a pass is genuine independent-oracle correctness rather than agreement with a scan-query-derived snapshot. They assert the user-visible CLI contract offline (independent of `--repo-path`), and skip cleanly when `scan-index` is absent.

| Suite                 | What it asserts (known-by-construction fixture)                                                                                                                               |
| --------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **D** diff-impact     | changed module/symbol detection, risk tiers (HIGH ≥5 importers / MODERATE / LOW), test-impact union, `--base` scoping                                                         |
| **B** batch           | N valid + 1 invalid → exit 0, per-item order, invalid item isolated (`ok:false`), byte-equivalence of a batched result vs its standalone form                                 |
| **R** src_roots       | monorepo multi-root naming + collision winner under a configured root, `src_roots` meta recorded                                                                              |
| **K** self-check      | corrupt index variants (missing key / bad version / wrong type / truncated JSON) → exit 3 + parseable JSON error, never a partial serve                                       |
| **U** uncovered/xrefs | fixture with KNOWN counts (2 undocumented public fns, 1 broken sphinx xref) → exact counts — the deterministic replacement for the LLM bench's circular scan-query-derived GT |

This is the division of labour between the two runners: **deterministic correctness now lives in `run-codemap-cli.py`** (suites D/B/R/K/U, joining its primary verdict), while the **LLM bench (`run-claude-structural.py`) measures workflow efficiency** — token ratio, tool-call economy, and recall on tasks whose ground truth is independent of the index. Because suite U pins the uncovered/xref counts deterministically, the corresponding LLM tasks (`CQ-02` uncovered, `CQ-04` xrefs, `CQ-05` combined-health uncovered part) are **demoted to self-consistency** in the LLM bench: they still run and score, but are excluded from the headline accuracy aggregates (scoring the codemap arm against index-derived truth would measure agreement with itself) and reported in a separate self-consistency row.

### Quick start

```bash
# Run all suites against the target repo (auto-detects index)
python benchmarks/run-codemap-cli.py \
    --repo-path ./<repo-dir> \
    --report

# Explicit index path
python benchmarks/run-codemap-cli.py \
    --repo-path ./<repo-dir> \
    --index-path ./<repo-dir>/.cache/codemap/pytorch-lightning-master.json \
    --report

# Verify task modules exist in index before a full run
python benchmarks/run-codemap-cli.py \
    --repo-path ./<repo-dir> \
    --verify-tasks
```

Index resolution checks `.cache/codemap/<name>.json` first, then `.cache/scan/<name>.json`.

<details>
<summary><strong>CLI flags</strong></summary>

| Flag                | Default | Description                                                                      |
| ------------------- | ------- | -------------------------------------------------------------------------------- |
| `--repo-path PATH`  | auto    | Path to repo clone                                                               |
| `--index-path PATH` | auto    | Override index; checks `.cache/codemap/` then `.cache/scan/`                     |
| `--report`          | off     | Write markdown report to `results/code-YYYY-MM-DD[-N].md`                        |
| `--json-only`       | off     | Emit per-scenario JSONL + summary envelope only; suppress human logs + md report |
| `--verify-tasks`    | off     | Verify task primary_modules exist in index before running                        |

</details>

### Pass thresholds

| Code          | Threshold                                                                                                                                                                                   |
| ------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| C1            | coverage gap >= 10% — AST-verified importers codemap finds beyond boundary-anchored grep, as fraction of codemap importers (real set comparison since 2026-07-03; was a hardcoded constant) |
| C2            | infeasible path fraction >= 50% — fraction of A→B path queries where A is not a direct importer of B (one boundary grep cannot surface the path; real test since 2026-07-03)                |
| C3            | leverage ratio >= 2.0x                                                                                                                                                                      |
| A1            | precision >= 0.90, recall >= 0.85 (high-risk tasks)                                                                                                                                         |
| A2            | precision = 1.00 (low-risk tasks)                                                                                                                                                           |
| A3            | FP rate < 5%                                                                                                                                                                                |
| L1            | `central` median < 200 ms                                                                                                                                                                   |
| L2            | `rdeps` median < 100 ms                                                                                                                                                                     |
| L3            | amortised index build < 500 ms — build_ms / QUERIES_PER_SESSION (=10); expected to FAIL on large repos under this honest divisor, and the verdict owns that                                 |
| L4            | speedup >= 2x (warm-index vs cold grep; gate is warm-only, a build-inclusive variant is reported alongside for honesty)                                                                     |
| Q_fix/feature | JSON valid block present                                                                                                                                                                    |
| Q_refactor    | JSON valid + has_rdeps + has_deps                                                                                                                                                           |
| S\_\* / S2    | symbol found + start_line within +-3 of ground truth                                                                                                                                        |
| H1 / H2       | undocumented / uncovered total == ground truth (exact)                                                                                                                                      |
| X1            | xrefs --broken count + target set == ground truth (exact)                                                                                                                                   |

**Scoring honesty (2026-07-03)**: a scan-query error (crash / timeout / missing module) now fails its scenario instead of scoring a false precision=1.0. Accuracy precision is judged against an independent AST importer oracle, not raw grep. S/H/X are a self-consistency track and do not count toward the verdict; the primary verdict has genuine room to fail.

______________________________________________________________________

## Benchmark methodology

### Task series

The benchmark covers 60 tasks across 11 series:

| Series           | Type                        | What it measures                                     | Evaluator                                          |
| ---------------- | --------------------------- | ---------------------------------------------------- | -------------------------------------------------- |
| **SE** (5 tasks) | Symbol extraction           | Find file + line range of a function/class           | Line-tolerance (±5 lines)                          |
| **FN** (5 tasks) | Call graph count            | How many unique functions call target X              | Name-recall ≥ 0.70 against GT caller list          |
| **BR** (9 tasks) | Blast radius (caller list)  | Which specific functions call target X               | Recall ≥ 0.70 against GT caller list               |
| **RV** (5 tasks) | Review assistance           | Caller count for a function in a code review context | Integer extraction with ±10% tolerance             |
| **CQ** (5 tasks) | Code quality                | Undocumented / uncovered / unhealthy module metrics  | Recall ≥ 0.70 against GT metric                    |
| **DG** (6 tasks) | Debug from trace            | Identify root-cause file + function from traceback   | Recall ≥ 0.70 against GT symbols                   |
| **FT** (5 tasks) | Feature scaffolding         | List files that need editing for a new feature       | Recall ≥ 0.70 against GT file list                 |
| **RI** (5 tasks) | Real GitHub issues          | Locate relevant code for a real issue/PR             | Recall ≥ 0.70 against GT file list                 |
| **DI** (6 tasks) | Diff impact (staged change) | Callers + tests affected by a staged change          | Caller recall ≥ 0.70 AND test-file recall ≥ 0.70   |
| **GR** (4 tasks) | Graph navigation            | Central modules / import path / transitive callers   | Set overlap or recall ≥ 0.70 (path: ordered chain) |

### Two arms

Every task runs twice:

- **plain**: grep, read, bash only — no structural index, no `scan-query`
- **codemap**: same tools + `scan-query` structural index (fn-rdeps, symbol, rdeps, undocumented, uncovered, find-symbol)

Scoring is independent per arm. Token ratio = `codemap_input_tokens / plain_input_tokens` (< 1.0 = codemap cheaper).

### Ground truth establishment

- **Symbol tasks (SE)**: GT from reading source directly (file path + AST line range).
- **Call-graph tasks (FN, BR, RV) — 2026-07-03**: authoritative `fn_callers` GT is validated against an independent AST oracle (`_callers_via_ast` in `generate-tasks-bench.py`); the `scan-query fn-rdeps` result is kept as `fn_callers_scan` diagnostic. Oracle-vs-tool divergence prints a loud warning listing divergent qnames — divergence signals a potential plugin bug and is never auto-overwritten. fn-rdeps `count` == unique deduped callers (the plugin also emits an explicit `unique_caller_count` alias since codemap 0.15.0).
- **Diff-impact / graph tasks (DI, GR)**: ground truth is AST-oracle-only by construction — `_callers_via_ast` + `_test_modules_importing_via_ast` (DI), `_central_via_ast` / `_import_path_via_ast` / `_fn_blast_via_ast` (GR). scan-query is never consulted for their GT (it is the tool the codemap arm invokes). New tasks ship with `gt_pending: true` when the target repo is absent at authoring time; `generate-tasks-bench.py --update` computes and writes their GT deterministically against the target repo and clears the flag. The `path` series requires a *unique* shortest path (`_shortest_path_is_unique`) so the oracle answer is unambiguous.
- **Quality tasks (CQ)**: `CQ-01` undocumented GT uses an independent AST docstring oracle. `CQ-02` uncovered GT uses a documented simple-name AST approximation and is diagnostic. `CQ-03`–`CQ-05` remain self-consistency diagnostics and are excluded from headline correctness.
- **`--update` semantics (2026-07-29)**: default `--update` refreshes independent AST-oracle fields only; scan-query-derived diagnostic fields refresh only behind `--update-from-tool`, which prints a circularity warning and oracle diff before writing. Namespace normalization is applied before comparing or writing qualified names.
- **Residual oracle limits**: `RV-05` and `CQ-02` use an approximate uncovered oracle; RI uses offline/static issue evidence and `RI-05` is unscoreable. SE, FN/BR, RV-01–04, DG/FT, DI/GR, and MB use the independent or source-anchored classifications recorded in the locked provider-parity manifest.

### Known limitations

- **Arm ordering**: plain arm always runs before codemap on each task. Token metrics are unaffected. Wall-clock time metrics may be biased toward codemap (OS page cache warm on second run); treat token ratio as the primary efficiency signal.
- **Equal turn caps (2026-07-03)**: both arms get the same per-task max-turns budget (was plain×4 / codemap×2 per caller, which lowered the codemap/plain token ratio precisely on the highest-caller tasks). The token_ratio headline is now measured under symmetric caps.
- **Paired accuracy (2026-07-03)**: because extraction failures are excluded per-arm, per-arm accuracy is computed over different task subsets. The summary now also prints a paired accuracy — both arms scored over the tasks where BOTH extracted — with the paired-n stated; treat that as the comparable figure.
- **Hardware capture (2026-07-03)**: the query-benchmark report header and JSON envelope record platform / processor / cpu_count / python, since the latency thresholds (L1–L3) are hardware-calibrated and not comparable across machines without it.
- **RV recall > 1.0**: Scores above 1.0 (marked `^` in per-run log) indicate model over-counts, not evaluator error. In June 22 runs RV-03/04 both showed systematic over-count (`^1.1–1.25×`). RV-03 was a task-definition bug — sub-question asked for "fn-rdeps count field" (= 42 total call-site edges) but GT = 37 unique callers; fixed June 23 (prompt now asks for "distinct caller entries"; new runs show RV-03 codemap recall ≈ 1.0). RV-04 remains: `fn-rdeps count: 24` = 24 unique callers = GT, so over-count is pure model error (grep over-counting).
- **NaN in summary table**: A task shows `NaN` recall in the summary table for any of four reasons: (1) `extraction_failed == True` — evaluator regex cannot extract the target metric from model output (most common); (2) `quality.scored == False` — task is marked not scoreable (e.g. RI-05); (3) only one arm ran — no plain+codemap pair to compare; (4) `quality.recall` is None and `metric_got`/`metric_expected` are also None. In June 22 runs (44 tasks): plain arm extraction_failed on SE-05/CQ-01/CQ-05/RI-04 (haiku), FT-03 (sonnet), CQ-01/CQ-05 (opus). Codemap arm extraction_failed on SE-05/CQ-03/CQ-05 (haiku), FN-03 (sonnet), FN-03/RI-02 (opus). Extraction failures are excluded from the accuracy denominator. Count-based tasks (SE / CQ / count-branch RV) show `NaN` in the summary table recall columns; per-task recall is visible in the per-run log line (`recall=…`).
- **RV-02 scored by count (not recall)**: RV-02 asks *how many* modules import `rank_zero` (GT=64). Earlier framing scored it by enumerating all 64 callers — infeasible for one LLM response, so both arms scored low. It is now scored as a count within ±10% (`_int_close`, the same tolerance CQ count-checks use), keeping the task id `RV-02`. The blast-radius *enumeration* workflow it once approximated is now covered properly by the DI series (staged change, caller recall ≥0.70).
- **Opus FN-02 and BR-03 regressions (June 22 — fixed June 23)**: June 22 runs showed FN-02 codemap recall=0.027 and BR-03 codemap recall=0.042. Root causes were two evaluator bugs: (1) missing extraction forms for bold+numbered list output format (Form 9) and file-dump pointer resolution; (2) evaluator version mismatch. Fixed in evaluator v3 (June 23 re-runs: both tasks recall=1.000). See `results/bench-opus-20260623-003745.jsonl`.
- **Haiku RI token spirals (June 22 — fixed June 23)**: June 22 runs: RI-02/RI-04 codemap hit `error_max_turns` consuming 2.6–3.0M tokens. Root cause: `Bash(python3:*)` allowed on codemap arm only — agents spiralled into implement-validate mode writing repro scripts. Fixed by blocking `Bash(python3:*)` and `Bash(python:*)` on both arms. June 23 re-runs: RI-02/RI-04 codemap recall=1.000, 2.0–2.1M tokens. See `results/bench-haiku-20260623-003825.jsonl`.
- **Haiku BR-07 regression**: codemap recall=0.778 vs plain=0.889 (Δ=−0.11). Single instance; monitor for recurrence.
- **FN-series extraction failures**: FN-03 codemap extraction_failed on both sonnet and opus — evaluator cannot parse model output. Plain arm scores 1.000 on both models.
- **Partial filesystem isolation**: `Write`, `Edit`, and `NotebookEdit` are blocked on both arms. Runs where either arm reads benchmark answer files (`tasks-bench`, `benchmarks/results`, `/benchmarks/`) are flagged `answer_file_read` and excluded from scoring — visible in the summary line and JSONL `error` field. Agents can still read arbitrary paths outside the target repo (alternate checkouts, `~/.claude`, etc.); for cleanest runs use a disposable checkout and verify the JSONL tool-use log shows no stray reads.
- **Batch adoption is a signal, not forced**: the codemap arm may use `scan-query batch` (one process, one shared coverage block, a JSON array of `{cmd, args}`) but is never told to. Each run records `used_batch: bool` in the JSONL, and batched inner `cmd`s are attributed to their own subcommand counters (a batched `fn-rdeps` counts as `fn-rdeps`, not as an opaque `batch`) — so whether the model *chooses* batch is part of what is measured.
- **Guard-redundant-scan / avoidance chain not benchmarked here**: the codemap plugin's redundant-scan guard and the grep→scan-query avoidance chain are intentionally out of scope for this task-based benchmark — they are measured on real coding sessions by `/codemap:debrief-coding`'s avoidance join, not on synthetic tasks.

### Scope and out-of-scope

**What this benchmark measures:**

- Token reduction for structural queries (3–10× demonstrated across haiku/sonnet/opus)
- Structural recall on `fn_call_graph` and `develop_blast_radius` tasks
- Symbol lookup accuracy (exact line-range match)
- Code health metric retrieval (`undocumented`, `uncovered`, `xrefs-broken`)

**What this benchmark does NOT yet measure:**

- Native target-project test pass rate after fixes; canonical Claude and Codex executable stages use benchmark-owned behavior oracles because project dependencies are intentionally absent from the agent sandbox
- Semantic correctness beyond structural keyword matching
- Tasks sampled from real developer activity (issues, PRs, maintenance logs)
- Code quality judgment or review quality beyond structural metrics

`tasks-bench.json` contains 60 tasks across 11 series: structural research (SE / FN / RV / CQ / BR), debug trace analysis (DG), feature scaffolding (FT), real GitHub issues (RI), staged diff-impact (DI), graph queries (GR), and module import fan-in (MB). Core series model the pre-implementation structural research phase; DG/FT/RI cover broader developer workflows; DI/GR/MB add staged-change blast radius, whole-graph navigation, and module-level reverse-import blast radius. No tasks require a code output or a test run — the DI series stages a synthetic change but reverts it after both arms and never asks for a patch.

### Extensions

- **Tier E** (hard): End-to-end patch tasks (`tasks-patch.json`, PT-01–PT-05). Five historical revisions carry committed pre-fix coordinates, hidden reference patches, staged target-test fixtures, regression commands, and task-local index locks. Select them with the unified Codex `--tasks=PT` family selector or Claude `--study patch`; both five-task/15-cell no-model preflights and all five reference-patch lifecycles pass against the prepared frozen indexes. The current checksum-valid full scopes are `benchmarks/results/claude-patch-post-lifecycle-9e7bbb02bc3a` and `benchmarks/results/codex-patch-post-lifecycle-4119d30180f3/patch`. Both preserve every valid and invalid candidate outcome, prove strict-query delivery, patch transport, containment, clean rollback, targeted-oracle execution, regression safety, and cleanup, and are never pooled across providers/models. Earlier paid artifacts remain immutable diagnostic provenance only.

Patch scope admission fingerprints the absolute pytest launcher selected by the operator environment, its Python interpreter, pytest module/version, and installed pytest entry-point set. Every dry-run first admits each arm's Codex/Codemap integration against the clean historical worktree, then stages the frozen target-test fixture and executes its real regression=0/target=1 baseline predicate before printing a paid approval; paid execution follows the same order before model transport. This preserves the integration's clean-context invariant without weakening the patch fixture, and failures report the task, command, exit code, and bounded output. Target-project dependencies and pytest plugins intentionally come from that prospectively recorded runtime, while `PYTHONPATH` gives the disposable historical worktree source precedence. The emitted paid command carries the admitted launcher through `CODEMAP_BENCH_PATCH_PYTEST`, so an activated shell cannot silently switch the paid run to another pytest runtime.

Patch progress rows always render the semantic score to three decimals. The leading `✓` or `✗` reports pooling eligibility, while `patch` and `oracle` expose the executable gates; a visible score on an ineligible row never places that row in paired efficiency or quality aggregates.

The current Claude Patch scope has two valid A/C pairs (PT-01/02): equal 2/2 quality with strict C against A at `−14.8%/−26.9%/−28.4%/−33.3%/−28.8%` gross input, fresh input, output, commands, and elapsed time; strict C alone also passes PT-03 and PT-05, while PT-04 fails in every Claude arm. The current Codex Patch scope has three valid A/C pairs (PT-01/02/03): equal 3/3 quality with strict C against A at `−22.8%/−12.8%/−10.0%/−5.0%/−8.8%` gross input, fresh input, output, commands, and elapsed time. Codex PT-04/C and PT-05/C remain independent-oracle failures despite exact successful strict queries. The comparable strata are one repetition, one frozen Lightning family, and different provider/model transports; these results support quality parity and adaptive retrieval, not a universal Patch efficiency claim.

W5 broader generalization is explicitly deferred with zero paid cells. The program maintainer/parent owns reopening it for cross-repository external validity only after committed hash-locked issue/PR snapshots, deterministic generation and ordering, exact source/merge/pre-fix/test evidence, independently scored runtime/oracle contracts, and prospective limits on tasks, repositories, providers, models, repetitions, timeout, paid-cell count, and cost/time. Current terminal evidence therefore remains limited to Lightning exact revisions and named provider/model strata; no universal or cross-repository claim is made.

### Fix-task benchmark families (agentic benchmark)

Four suite files extend the benchmark from pure structural discovery into read-crop selection, executable edits, and patch application:

| Family            | Suite                   | Tasks       | Canonical P1 scoring                                         | Daily-work proxy                                                  |
| ----------------- | ----------------------- | ----------- | ------------------------------------------------------------ | ----------------------------------------------------------------- |
| `readcrop`        | `tasks-readcrop.json`   | RC-01–RC-06 | Source extraction oracle + context/token telemetry           | Read only the smallest sufficient source slice                    |
| `fix_single`      | `tasks-fix-single.json` | FS-01–FS-04 | Clean patch apply + exact path + independent behavior oracle | Single-file bug fix with independent executable validation        |
| `fix_multicaller` | `tasks-fix-multi.json`  | FM-01–FM-03 | Clean patch apply + exact paths + complete-caller AST oracle | Signature change + callers — codemap's edit-assist differentiator |

**Isolation**: the canonical Claude and Codex P1 paths create a benchmark-owned disposable worktree, expose only its checkout to the agent, capture the canonical Git diff outside the agent session, apply it to a second clean scoring worktree, run the independent oracle, and verify both cleanups. The original codebase is never mutated. The historical Claude `--study agentic` lane retains its earlier copy-and-`diff -ru` diagnostic scorer and is never pooled with canonical P1 evidence.

Claude `C_strict` requires two linked observations: the installed `/codemap-py:query-code` Skill must launch and its exact underlying `codemap-py query` or legacy `scan-query` command must complete successfully against the frozen checkout. Loading the Skill or merely attempting a denied command is not query evidence. The benchmark prepends the scope-locked repository plugin fixture to PATH and hashes the invoked `bin/codemap-py`; it never discovers a mutable user-cache version. Any attempt to rebuild the frozen index contaminates the cell. Absolute paths outside the disposable checkout are recorded as attempted diagnostics, but only a successful external access can leak bytes and exclude the cell from pooling. Canonical A/B/C cells disallow nested Agent/Task sessions because parent telemetry cannot account for child usage.

The selected artifact `results/claude-readcrop-aff1ece479cf` is checksum-valid diagnostic evidence but is rejected from admission. Its RC-01 query was permission-denied, while RC-02 changed into the plugin repository, retried a stale index, searched the user's home directory, and read another environment's installed Lightning source. Both answers scored correctly, but neither C cell satisfies the repaired strict treatment and the reported RC-02 token outlier is not valid efficiency evidence.

The follow-up artifact `results/claude-readcrop-cd23af087f68` is also checksum-valid diagnostic evidence and is rejected. Every C query was denied before execution: RC-01/C made 17 commands with 10 permission denials and RC-02/C made 18 commands with 8 permission denials, yielding zero successful Codemap calls despite `codemap=✓` in the historical renderer. The C input totals (`431.3k` and `356.9k`) are exactly the provider's uncached plus cache-creation plus cache-read accounting and reflect retry/cache churn, not Codemap result size. A/B failures in that artifact were also overstated when denied external path guesses were treated as successful contamination. The repaired parser reports Codemap use only after a successful query, separates attempted from successful external paths, and renders each interactive result row in one arm color with Rich semantic highlighting disabled.

The checksum-valid selected artifact `results/claude-readcrop-a27396a66a4e` validates the repaired permission route. Both C cells launch the installed Skill, execute the exact frozen query, remain checkout-isolated, and match A's quality. Across RC-01 and RC-02, C reduces aggregate gross input by 58.4%, fresh input by 55.8%, output by 43.5%, commands by 73.3%, and elapsed time by 39.9% relative to A. RC-01/B's displayed failure is a scorer-only false positive: the parser treated the slash in the relative `*/lightning/pytorch/core/*` shell glob as an absolute path even though the subsequent source read stayed in the disposable checkout. Current-parser replay admits the B cell; the immutable artifact is not rewritten. This selected two-task result validates treatment delivery but does not replace the full six-task Claude ReadCrop run.

The checksum-valid full artifact `results/claude-readcrop-8c605ce4f83e` completes 18/18 correct, compliant, uncontaminated, pooling-eligible cells with exact strict queries 6/6. At equal A/C quality, C reduces aggregate gross input by 25.74%, fresh input by 24.62%, output by 13.78%, commands by 39.39%, and elapsed time by 11.74%. Four tasks show clear C reductions; RC-01 is near gross-input parity, while RC-04/C costs 2.377× A input because Haiku misanchors the complete query's repository-relative `src/...` path under the installed Skill directory, retries two queries, and reads the full file. The first RC-04 query is exact, complete, and only 1,998 bytes, so this is not Codemap engine or payload cost. The production query contract now states that returned relative paths use the caller repository root and that a complete result must not trigger re-query or read/grep verification. B is canary-only; in this run it uses Codemap in five of six cells and reduces aggregate gross input by 31.81% versus A.

Only `query-code` uses command-specific Claude `allowed-tools`, and it declares both the PATH and installed absolute `codemap-py query` forms. The other Codemap Claude Skills intentionally declare generic `Bash`, which already covers their documented shell commands; command-specific duplicates are neither required for permission nor desirable for maintenance. Skill-declared permissions and the benchmark runner's outer headless `--allowedTools` policy remain separate enforcement layers.

**FM-03 is the revised cross-file test**: cooperative `Strategy.setup_environment` propagation spans `strategy.py`, `ddp.py`, `fsdp.py`, `deepspeed.py`, `model_parallel.py`, and `xla.py`; `single_xla.py` is intentionally excluded because it does not participate in this override contract. The prior `Strategy.setup` contract was invalid: these overrides are full replacements, so adding `super().setup` would repeat stateful setup. The strict Codemap arm uses an unlimited same-name symbol query to discover candidates, then verifies inheritance and the production package boundary from source; `fn-rdeps` is not an inheritance query. The plain arm must discover the same surface with ordinary repository tools. Exact changed-path validation captures whether the right files were actually changed. This remains the first benchmark family that directly measures whether Codemap reduces missed override candidates in a real multi-file edit, but only the revised task is admissible.

```bash
# Claude ReadCrop no-model admission; output includes the exact fresh paid command
python3 benchmarks/run-claude-agentic.py \
    --study readcrop \
    --repo-path /path/to/pytorch-lightning \
    --index /path/to/pytorch-lightning/.cache/codemap/pytorch-lightning.json \
    --model haiku \
    --tasks RC-01,RC-02 \
    --dry-run

# Claude Fix-Single no-model admission; output includes the exact fresh paid command
python3 benchmarks/run-claude-agentic.py \
    --study fix-single \
    --repo-path /path/to/pytorch-lightning \
    --index /path/to/pytorch-lightning/.cache/codemap/pytorch-lightning.json \
    --model haiku \
    --tasks FS-01,FS-02,FS-03,FS-04 \
    --dry-run

# Claude Fix-Multi no-model admission; omitted --tasks selects the full FM suite
python3 benchmarks/run-claude-agentic.py \
    --study fix-multi \
    --repo-path /path/to/pytorch-lightning \
    --index /path/to/pytorch-lightning/.cache/codemap/pytorch-lightning.json \
    --model haiku \
    --dry-run
```

**Scoring**: canonical Claude and Codex executable stages share the provider-neutral contracts and isolated patch executor. The baseline must fail its task-specific independent oracle; the patch must apply with ordinary `git apply --check`, touch exactly the allowed source paths in any order, pass that oracle, preserve the frozen source and index, and leave no worktree behind. Fix-Multi requires every caller or override declared by its contract. Task prompts preserve discovery as measured work rather than disclosing caller names or counts, and C receives a task-fit query: `fn-rdeps` for direct callers and unlimited `find-symbol` candidates plus source verification for overrides. Provider transport, native usage, and raw events remain provider-local; Claude reports unavailable native `tool_result_tokens` as `null` and the field is excluded rather than estimated. ReadCrop, Fix-Single, Fix-Multi, and Patch are separate strata and are never pooled with structural or historical agentic rows. The dry run is the only source of the paid scope hash: it emits a lowercase 16-character SHA-256 prefix accepted by `--paid-approval`; the complete 64-character scope remains recorded in provenance and is accepted too. Copy the exact command, use a fresh output directory, and do not use any separate paid-mode flag.

```bash
# Codex Fix-Single tasks: inspect the plan, then omit --dry-run for approved execution
python3 benchmarks/run-codex-structural.py --repo-path /path/to/pytorch-lightning --model gpt-5.6-luna --tasks FS-01,FS-02 --dry-run

# Codex Fix-Multi task: the same task-driven runner routes by FM-* ID
python3 benchmarks/run-codex-structural.py --repo-path /path/to/pytorch-lightning --model gpt-5.6-luna --tasks FM-01 --dry-run
```

Omitting `--tasks` runs all 73 locked tasks (219 A/B/C cells); use an explicit family, exact-ID, or mixed selector for a targeted nonpoolable scope. ReadCrop, Fix-Single, Fix-Multi, and Patch results retain separate stage artifacts and scorers.

## Results

`results/` holds all past run outputs:

| Pattern                                 | Source                                |
| --------------------------------------- | ------------------------------------- |
| `agentic-YYYY-MM-DD[-N].json`           | Agentic benchmark JSON snapshot       |
| `agentic-YYYY-MM-DD[-N].md`             | Agentic benchmark markdown report     |
| `bench-<model>-<YYYYMMDD-HHMMSS>.jsonl` | Real-codebase benchmark JSONL results |
| `code-YYYY-MM-DD[-N].md`                | Query benchmark markdown report       |

<!-- Related result tables — editing one figure means checking the others that restate the same run.
  Structural lane:
    - benchmarks/README.md § Structural results — every model on one cohort (cross-provider, binary, medians)
    - benchmarks/README.md § Spread behind those medians + § Voluntary Codemap use inside B_auto (same pairs, distributions)
    - benchmarks/README.md § Claude multi-model — paired accuracy and efficiency (all 55 tasks, with cost)
    - benchmarks/README.md § Codex results (headline cohort, mean quality, totals estimator, stage strata)
    - benchmarks/README.md § Codex results — gpt-5.6-sol (second Codex stratum, same estimators)
    - benchmarks/README.md § Codex results — gpt-5.6-terra (third Codex stratum, same estimators)
    - plugins/codemap-py/README.md § benchmark snapshot tables (Claude and Codex)
  Agentic lane:
    - benchmarks/README.md § Agentic results — every model on one cohort (cross-provider, binary, medians)
    - benchmarks/README.md § Claude multi-model — median change against A_plain
    - benchmarks/README.md § Codex agentic results (mean semantic quality)
    - benchmarks/README.md § Codex agentic re-executions — 2026-09-07 (two further Luna runs, same estimators)
    - benchmarks/README.md § Codex agentic strata — 2026-09-07 (first terra and sol agentic runs, same estimators)
    - benchmarks/README.md § Codex agentic balanced-order sweep — 2026-09-08 (all three strata, one launch; the set the cross-provider table publishes)
  A number changes here only when it is recomputed from an artifact under results/; the artifacts are
  immutable, so a corrected figure means a new run, never an edited row. -->

Every published measurement lives in this section, split into the two lanes that never share a cohort: structural tasks answered from the frozen index, and agentic blast-radius tasks. Each lane opens with the one table that puts every model on a single cohort, metric, and estimator, and is followed by the per-provider views that keep their own canonical scoring. Superseded, partial, and infrastructure-only runs are retained at the end of their lane rather than deleted, because they are the evidence behind harness fixes.

Every table below is measured on the corrected task suite against `pytorch-lightning` 2.6.5 at `be98784a1` with frozen index `3c584089…` (scan_version 13), one repetition per cell. Claude sources: `results/bench-haiku-20260906-090705.jsonl`, `results/bench-sonnet-20260906-091410.jsonl`, and `results/bench-opus-20260906-091619.jsonl` (structural, 55 tasks × 3 arms × 3 tiers = 495 cells), plus `results/code-2026-09-06.json` with the re-rendered `results/code-2026-09-06-rerender.md` (agentic, 16 tasks × 3 arms × 3 tiers = 144 cells). Codex sources: `results/codex-combined-20260906T085207Z/structural` (219 cells) and `.../agentic` (48 cells) for `gpt-5.6-luna`, `results/codex-combined-20260906T221515Z/structural/gpt-5.6-sol` (219 cells over 73 tasks in five stages) for `gpt-5.6-sol`, and `results/codex-combined-20260907T055156Z/structural` (219 cells over the same 73 tasks) for `gpt-5.6-terra`, all on Codex CLI 0.153.4. The `gpt-5.6-sol` and `gpt-5.6-terra` structural runs carry no agentic stage of their own. Four further Codex agentic executions are published, all 48 cells and all on Codex CLI 0.153.4: `results/codex-agentic-20260907T065010Z` and `results/codex-combined-20260907T055156Z/agentic` for `gpt-5.6-luna` — the first launched `--agentic --isolated` against a relocated copy of the locked index, the second against the locked index every other table uses — then `results/codex-agentic-20260907T140422Z` for `gpt-5.6-terra` and `results/codex-agentic-20260907T141122Z` for `gpt-5.6-sol`, the latter also isolated with a relocated index. The interrupted `results/codex-agentic-20260907T140408Z` holds zero cells and is not a source for anything here. Three further agentic executions — `results/codex-agentic-20260907T212926Z/gpt-5.6-luna`, `.../gpt-5.6-terra`, and `.../gpt-5.6-sol`, 48 cells each — are the balanced-order sweep that now supplies the cross-provider table's Codex rows; they run a different experiment revision from every source listed above.

Only the 18 cells whose task definition changed were re-executed. The runner's resume key is `(task_id, arm, model, repo_sha, index_sha, task_hash)`, and 53 of the 55 task hashes were untouched by the CQ-03 and GR-04 corrections, so 477 cells were reused from the earlier same-day execution and carry `resumed: true` in the artifacts. Repository, index fingerprint, arm contracts, and evaluator source are identical across reused and fresh rows; re-executing the unchanged 477 would have cost $99 and could not have changed their inputs.

Earlier result tables were removed rather than carried forward; what survives of those runs is kept in the superseded-runs block at the end of each lane.

### Structural results

Structural tasks are answered from the frozen index without editing the repository. The aggregated table comes first; the per-provider views below it differ in cohort, metric, or estimator and say so where they do.

#### Structural results — every model on one cohort — 2026-09-06

Every model stratum measured ran the same 55-task suite against the same frozen repository revision and index, so they belong in one table. The provider sections below each report their own canonical numbers, which differ from these because each provider's tooling reports a different cohort, metric, and estimator. This table removes all three differences: it is restricted to the 45-task headline cohort both providers share, counts a cell as correct or not rather than scoring it continuously, and takes per-task medians of treatment ÷ control. Every row is a within-model paired comparison over tasks where both of its arms produced a scored, parsed answer.

Admission also requires `treatment_adherence is True` on both cells of a pair, matching `result_eligibility` in `_bench_common/provider_parity_contracts.py`, so this table measures the arm as *delivered* rather than as assigned. That clause excludes five `C_strict` cells which answered the required-use prompt without ever calling Codemap (`compliance: false`, zero queries): Haiku `DG-05`, Haiku `FT-04`, Sonnet `BR-09`, one Luna cell, and Terra `FT-01`. An earlier revision of this table omitted the clause and published Haiku `C_strict` at 41 pairs (30/41 against 36/41, −73% tokens), Sonnet `C_strict` at 43 (36/43 against 40/43, −54%), and Luna `C_strict` at 44 (38/44 against 42/44, −30%); the eight other rows were unaffected, and on Sol the clause is a no-op. The three Claude cells scored correct in both arms, so they move the denominator and both numerators together; the Luna cell (`RV-01`) is incorrect in both arms, so dropping it lifts both of that row's accuracies from 86.4%/95.5% to 88.4%/97.7%. Terra `FT-01` is correct in both arms and read 1.20× its control, so dropping it moves that row from 41 pairs (36/41 against 38/41, −41% tokens) to 40. No row's gain changed sign.

Two consequences of the clause deserve stating, because neither is self-evident. One of the excluded cells was the distribution's worst outlier — the dropped Sonnet cell read 44.9× its control's input tokens, which is what a `C_strict` cell that ignores Codemap and explores by hand looks like; removing it cuts that row's log-spread from 1.210 to 0.984. And **the efficiency columns move further than the accuracy columns, always in the flattering direction**: Haiku `C_strict` gains seven points of apparent token saving (−73% to −80%) purely from dropping two cells that behaved like their control. That is a selection effect of the admission rule, not a measurement — the rule removes the ratios nearest 1.0 by construction, because a treatment cell that never used the treatment is the cell most likely to match its control.

| Treatment arm | Provider | Model  | Paired n | Control accuracy | Treatment accuracy |          Gain | Tokens | Cost | Time |
| ------------- | -------- | ------ | -------: | ---------------: | -----------------: | ------------: | -----: | ---: | ---: |
| C_strict      | Claude   | Haiku  |       39 |    71.8% (28/39) |      87.2% (34/39) | +15.4 pp (+6) |   −80% | −75% | −74% |
| C_strict      | Claude   | Sonnet |       42 |    83.3% (35/42) |      92.9% (39/42) |  +9.5 pp (+4) |   −57% | −52% | −53% |
| C_strict      | Claude   | Opus   |       43 |    88.4% (38/43) |      97.7% (42/43) |  +9.3 pp (+4) |   −19% | −23% | −31% |
| C_strict      | Codex    | Luna   |       43 |    88.4% (38/43) |      97.7% (42/43) |  +9.3 pp (+4) |   −29% |    — | −36% |
| C_strict      | Codex    | Terra  |       40 |    87.5% (35/40) |      92.5% (37/40) |  +5.0 pp (+2) |   −42% |    — | −51% |
| C_strict      | Codex    | Sol    |       45 |    93.3% (42/45) |      93.3% (42/45) |   0.0 pp (+0) |   −51% |    — | −49% |
| B_auto        | Claude   | Haiku  |       42 |    73.8% (31/42) |      85.7% (36/42) | +11.9 pp (+5) |   −49% | −58% | −56% |
| B_auto        | Claude   | Sonnet |       42 |    83.3% (35/42) |      97.6% (41/42) | +14.3 pp (+6) |   −59% | −44% | −50% |
| B_auto        | Claude   | Opus   |       44 |    88.6% (39/44) |      95.5% (42/44) |  +6.8 pp (+3) |   −30% | −28% | −31% |
| B_auto        | Codex    | Luna   |       44 |    86.4% (38/44) |      93.2% (41/44) |  +6.8 pp (+3) |   −33% |    — | −27% |
| B_auto        | Codex    | Terra  |       41 |    87.8% (36/41) |      95.1% (39/41) |  +7.3 pp (+3) |    −5% |    — | −20% |
| B_auto        | Codex    | Sol    |       44 |    93.2% (41/44) |      97.7% (43/44) |  +4.5 pp (+2) |    +1% |    — | −13% |

The three Codex strata are three separate studies, not a Codex average: `gpt-5.6-luna` ran on 2026-09-06, `gpt-5.6-sol` and `gpt-5.6-terra` on 2026-09-07, each once. Terra is the stratum whose first launch was refused on a paid-approval token; it was relaunched on 2026-09-07 and completed all 219 cells, so the sentence that once stood here — that terra had never run a paid cell — no longer holds.

The six `C` rows are directly comparable: every one requires the installed Codemap Skill, and the three Codex rows carry byte-identical `A_plain` and `C_strict` arm contracts (`arm_contract_hash` `936a684f…` and `83db65ba…`). Five of the six improve accuracy while reading less. `Sol` `C_strict` is the exception and the reason that sentence is not universal: it answers exactly the same 42 of 45 cells as its own control for 51% fewer gross input tokens, so on that stratum the required arm buys cost and not accuracy. It converts DI-01 and loses FT-01; on a 45-task cohort at one repetition per cell, a net of zero is a tie, not a demonstrated absence of effect. (DI-03 was previously named here as a third flip and a loss. It is neither: it is scored incorrect in both arms, and its continuous score rises from 0.490 to 0.900 — the largest quality gain `C_strict` makes on this stratum.) Terra converts DI-01 and DI-02 and loses nothing, which is the cleanest `C` row in the table: every task it changed, it changed upward.

The `B` rows are not comparable across providers, and grouping them together under one label is a presentation convenience rather than a claim — Claude's `B_auto` makes Codemap *optional* and measures whether a model reaches for it unprompted, while the frozen 2026-09-06 Codex Luna run's `B_auto` arm ran under a prompt that *required* a Codemap query, measuring whether the Skill packaging costs anything against raw command use instead. Reading a Claude `B` row against the Luna `B` row compares two different questions. The Luna `B` row is likewise not comparable to the other two Codex `B` rows, for exactly the reason this file predicted before the second run existed: the contract was changed to optional-use after Luna, and `Sol` and `Terra` are the Codex studies executed under it (`arm_contract_hash` `9b66c2da…` for Luna against `ae5f9a51…` for both of the others). Only the `Sol` and `Terra` `B` rows answer the same question as a Claude `B` row; the Luna `B` row must never be blended with them or read as their baseline.

Cost is empty for Codex because that runner captures no per-cell price; the Claude figures are each run's recorded `total_cost_usd`. Transports, harnesses, and prompt caching also differ by provider, so cross-provider gaps of a few points carry far less weight than the direction each row shows on its own.

The Tokens column is gross input, cached reads included, on every row. That distinction is load-bearing on the `Sol` rows, where roughly 91% of gross input is cached: the whole-run gross totals are 19,460,972 / 10,357,890 / 5,646,505 for `A_plain` / `B_auto` / `C_strict` against fresh totals of 1,812,844 / 1,502,978 / 1,046,441, so the 71% gross reduction from `A_plain` to `C_strict` is a 42% fresh reduction. On the 45-task cohort in the table the median per-task ratio is −51% gross but −36% fresh, and on the analysis's stricter three-arm cohort the median paired delta is −51,666 gross tokens against −4,186 fresh. Any dollar claim must be stated on fresh tokens or must say that it is not.

##### Spread behind those medians

A single median cannot say whether an arm saves reliably or saves on average because a few tasks saved enormously. This table reports the distribution of the same per-task input-token ratios that produce the Tokens column above, over the same pairs.

| Arm      | Provider | Model  | Paired n | Median | Geo mean |     p10–p90 |      Min–max | sd(log) | Cheaper in |
| -------- | -------- | ------ | -------: | -----: | -------: | ----------: | -----------: | ------: | ---------: |
| C_strict | Claude   | Haiku  |       39 | 0.204× |   0.215× | 0.031–1.384 |  0.010–2.296 |   1.461 |      32/39 |
| C_strict | Claude   | Sonnet |       42 | 0.431× |   0.398× | 0.112–1.184 |  0.046–2.559 |   0.984 |      32/42 |
| C_strict | Claude   | Opus   |       43 | 0.806× |   0.697× | 0.304–1.282 |  0.118–1.677 |   0.606 |      26/43 |
| C_strict | Codex    | Luna   |       43 | 0.711× |   0.566× | 0.176–1.505 |  0.033–2.112 |   0.962 |      29/43 |
| C_strict | Codex    | Terra  |       40 | 0.581× |   0.565× | 0.308–1.125 |  0.101–2.405 |   0.618 |      33/40 |
| C_strict | Codex    | Sol    |       45 | 0.486× |   0.496× | 0.258–1.066 |  0.054–1.174 |   0.596 |      39/45 |
| B_auto   | Claude   | Haiku  |       42 | 0.508× |   0.286× | 0.033–1.175 |  0.010–5.148 |   1.462 |      35/42 |
| B_auto   | Claude   | Sonnet |       42 | 0.412× |   0.407× | 0.104–1.183 | 0.047–14.359 |   1.143 |      31/42 |
| B_auto   | Claude   | Opus   |       44 | 0.700× |   0.697× | 0.322–1.256 |  0.121–1.610 |   0.603 |      27/44 |
| B_auto   | Codex    | Luna   |       44 | 0.667× |   0.578× | 0.187–1.292 |  0.097–2.860 |   0.749 |      34/44 |
| B_auto   | Codex    | Terra  |       41 | 0.947× |   0.888× | 0.434–1.439 |  0.218–2.160 |   0.482 |      23/41 |
| B_auto   | Codex    | Sol    |       44 | 1.014× |   0.991× | 0.535–1.951 |  0.205–2.740 |   0.555 |      19/44 |

- **Median**: middle per-task ratio of treatment ÷ control [below 1.0× = treatment spent less; this is the Tokens column above, restated as a ratio]
- **Geo mean**: geometric mean of the same ratios [the average appropriate to ratios]
- **p10–p90**: interior 80% of tasks, 10th to 90th percentile of the ratio [narrow = consistent]
- **Min–max**: full observed range, including the single most extreme task in each direction
- **sd(log)**: standard deviation of the log ratio [scale-free spread; a plain standard deviation would be dominated by the largest task]
- **Cheaper in**: tasks where the treatment arm spent less, out of the paired total

Three things follow that the headline table cannot show. **The median understates the typical proportional saving wherever spread is large** — Haiku `B_auto` reads −49% at the median but 0.286× as a geometric mean, because six of its 42 cells never called Codemap at all and sit at parity, dragging a mixture median away from either mode. **At least a tenth of tasks cost the treatment arm more in every one of the twelve token rows** — every p90 exceeds 1.0×, so "reads less" is a distributional claim, never a per-task guarantee. And **the arms are not equally reliable at equal medians**: Sonnet `C_strict` and Sol `C_strict` sit at 0.431× and 0.486× with sd(log) 0.984 against 0.596, so the same headline saving is delivered far more evenly on one stratum than the other.

The paired differences behind those ratios are heavy-tailed, and naming the tail is what makes it auditable. Every stratum's largest single saving is one task: `FN-01` on Haiku (3,956,490 gross input tokens unaided against 41,449 with `C_strict`) and `GR-03` on Sonnet, Luna, and Sol (for Sol, 645,074 against 35,010). Its largest loss is always a blast-radius task — `BR-08`, `CQ-01`, `BR-09`, `BR-01` depending on the stratum. Mean paired deltas are therefore much larger than medians: Haiku `C_strict` saves 659,533 input tokens on average but 229,590 at the median, with a standard deviation of 949,042 across tasks.

Wall-clock ratios behave the same way and are reported in the per-provider sections rather than repeated here. Every percentile is an order statistic over 41–45 single observations at one repetition per cell: it describes the observed spread across tasks, not uncertainty about the median.

##### Voluntary Codemap use inside `B_auto`

`B_auto` makes Codemap available without requiring it, so each `B_auto` row mixes cells that used the tool with cells that did not, and the mix differs sharply by model.

| Provider | Model  | Used Codemap | Median ratio, used | Median ratio, unused |
| -------- | ------ | -----------: | -----------------: | -------------------: |
| Claude   | Haiku  |        36/42 |             0.340× |               0.989× |
| Claude   | Sonnet |        39/42 |             0.362× |               1.142× |
| Claude   | Opus   |        44/44 |             0.700× |                    — |
| Codex    | Luna   |        44/44 |             0.667× |                    — |
| Codex    | Terra  |        34/41 |             0.942× |               0.947× |
| Codex    | Sol    |        26/44 |             0.974× |               1.016× |

On Haiku and Sonnet the two groups separate cleanly: cells that queried Codemap saved 66% and 64% at the median, cells that did not are indistinguishable from their control, and the published `B_auto` median describes neither group. Opus and Luna have no unused group to compare — Opus reached for the tool on all 44 admitted cells, and Luna's frozen `B_auto` contract required a query, so its 44/44 is contractual rather than behavioural. Sol and Terra are the informative negatives, and they agree: both are optional-use like Claude's, uptake was 26 of 44 and 34 of 41, and in both the two groups are indistinguishable (0.974× against 1.016× on Sol, 0.942× against 0.947× on Terra), so on the two strata that ran the optional contract, choosing to query bought no token saving. The null now reproduces on a second stratum with much higher uptake, which rules out low uptake as its explanation. In all six strata `A_plain` used Codemap on zero cells, so no control is contaminated.

#### Claude multi-model — paired accuracy and efficiency

This is the Claude-only view over all 55 tasks, with cost. For the cross-provider view on the shared 45-task headline cohort, see [Structural results — every model on one cohort](#structural-results--every-model-on-one-cohort--2026-09-06); the numbers there are smaller denominators and differ accordingly.

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

#### Codex results — 2026-09-06

`results/codex-combined-20260906T085207Z/structural` — `gpt-5.6-luna` at high reasoning effort, Codex CLI 0.153.4, all five stages completed, 219/219 cells persisted. Previous Codex tables were removed rather than carried forward: they were produced under earlier manifests, earlier arm-reporting code, and in one case a run that aborted at 33 of 219 cells when a second study was started against the clone the Claude suite still held. The launcher now takes an exclusive lock on the target clone for the duration of a study, so a second run against the same clone is refused before it spends anything rather than failing partway through on the other run's staged edits.

This is the Codex-only view, scored on mean semantic quality with the runner's own totals estimator. For the same run restated on the metric and estimator the Claude tables use, see [Structural results — every model on one cohort](#structural-results--every-model-on-one-cohort--2026-09-06).

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

#### Codex results — `gpt-5.6-sol` — 2026-09-07

`results/codex-combined-20260906T221515Z/structural/gpt-5.6-sol` — `gpt-5.6-sol` at high reasoning effort, Codex CLI 0.153.4, 219 cells over 73 tasks in five stages, one repetition per cell, a 600 s wall-clock cap per cell, against `pytorch-lightning` 2.6.5 at `be98784a1` with frozen index `3c5840893e9c939baa61a6c5ce95994ff69ffe4a67d225aeb412c73deb61e0c1`. Every stage is marked `completed` in its `run-metadata.json` and all 219 cells persisted; the structural stage ran from 2026-09-06T22:20:45Z to 2026-09-07T01:26:08Z. `execution.codex_cli` records `observed_version: codex-cli 0.153.4` against `reviewed_version: codex-cli 0.146.1`, so this executed on a CLI build the methodology had not reviewed, and `auth_source_recorded` is false. Full analysis: `.reports/benchmarks/codex-sol-paid-analysis.md`.

**This run is half of the launch it came from, and remains a one-stratum result.** Its sibling `gpt-5.6-terra` stratum ran no paid cell in that launch: the launcher refused with `ERROR: paid Codex mode requires CODEX_PAID_APPROVAL=…` before the first model call, leaving `structural/gpt-5.6-terra` with a run log and no telemetry. Terra was relaunched separately on 2026-09-07 and completed — [its section follows](#codex-results--gpt-56-terra--2026-09-07) — but it is a different study on a different launch, so nothing in this section may be restated as a two-stratum Codex result.

The binary paired rows on the shared 45-task headline cohort are in [Structural results — every model on one cohort](#structural-results--every-model-on-one-cohort--2026-09-06). On the run's own semantic-quality metric, over the stricter cohort the analysis uses — the 44 tasks where all three arms produced an admissible scored cell — mean quality is `A_plain` 0.9477, `B_auto` 0.9889, `C_strict` 0.9742, and the median is exactly 1.000 in every arm, with 36 of those 44 tasks scoring identically in all three arms. Paired per-task deltas on that cohort: `C_strict` against `A_plain` is +0.0265 mean and 0.0000 median at 6 wins / 36 ties / 2 losses, and `B_auto` against `A_plain` is +0.0412 mean and 0.0000 median at 7 / 36 / 1. Input tokens move where quality does not: `C_strict` is cheaper on 38 of those 44 tasks at a median paired delta of −51,666 gross tokens, while `B_auto` matches its control at +566. The honest reading is that the cost effect is real and large and the quality effect is indistinguishable from zero at this sample size.

Structural quality here is ceiling-bound: with medians of 1.000 in all three arms, this design can detect degradation and cannot detect improvement, so a `0.0 pp` gain is a tie against a saturated control rather than evidence that Codemap does nothing.

Medians alone make the two treatment arms look equally good, and per-task spread is what separates them. Over the same 44 pairs, the per-task input-token ratio against `A_plain` runs 0.254–1.066 between the 10th and 90th percentiles for `C_strict` (full range 0.054–1.174, cheaper on 38 of 44) against 0.535–1.951 for `B_auto` (full range 0.205–2.740, cheaper on 19 of 44). The dispersion itself is nearly the same — the standard deviation of the log ratio is 0.593 for `C_strict` and 0.549 for `B_auto` — so the difference is where the distribution sits, not how wide it is: the required arm's interval lies below parity and the optional arm's straddles it with a heavier losing tail. Wall clock behaves the same way, 0.272–1.029 against 0.439–1.315. On quality the asymmetry reverses: `B_auto`'s worst task loses 0.028 while `C_strict`'s worst loses 0.500, so the one large quality regression in this run belongs to the required arm.

Optional access is not failing for lack of uptake. Twenty-six of those 44 `B_auto` cells issued at least one successful Codemap query, and splitting the token ratio on that boundary barely moves it — 0.974× median when Codemap was queried against 1.016× when it was not. The optional arm queries Codemap *in addition to* ordinary exploration rather than instead of it, which is why availability alone buys nothing.

The four executable and extraction stages are separate, nonpoolable strata, and their token direction is heterogeneous enough that averaging them would invert the finding:

| Model | Stage      | Cells | A_plain correct | B_auto correct | C_strict correct | B tokens | C tokens | B time | C time |
| ----- | ---------- | ----: | --------------- | -------------- | ---------------- | -------: | -------: | -----: | -----: |
| Sol   | ReadCrop   |    18 | 5/6             | 5/6            | 5/6              |     +60% |     −34% |   +25% |   −34% |
| Sol   | Fix-Single |    12 | 4/4             | 4/4            | 4/4              |     +16% |     +15% |    +3% |   +16% |
| Sol   | Fix-Multi  |     9 | 3/3             | 3/3            | 3/3              |     −15% |     +34% |   −55% |   −41% |
| Sol   | Patch      |    15 | 5/5             | 5/5            | 4/5              |     −36% |     −67% |   −11% |   −23% |

Token and time columns are cohort totals restated as change against `A_plain`, the same estimator the Luna stage table above uses. Requiring Codemap costs more on localized and multi-file editing — `+15%` on Fix-Single and `+34%` on Fix-Multi — and saves on ReadCrop and Patch, so the four stages must be read one at a time. The Patch saving is carried almost entirely by two cells where `A_plain` spent 536,886 and 1,464,515 gross input tokens against `C_strict`'s 112,210 and 153,314. ReadCrop's 5/6 is RC-03 failing `primary_correct` identically in all three arms, not an arm difference.

`B_auto` abandons the tool wherever code has to be modified. Voluntary uptake is 5 of 6 cells in ReadCrop and 35 of 55 in the structural stage, but **0 of 12 across Fix-Single, Fix-Multi, and Patch** — not one editing cell chose to call Codemap. That is why the editing-stage `B` figures track `A_plain`, and why an optional rollout should not be expected to reproduce the `C_strict` result.

The run's only correctness failure in these four stages is PT-04 under `C_strict`, the Codemap-required arm, where `A_plain` and `B_auto` both passed — at 536,886 and 540,165 gross input tokens against `C_strict`'s 112,210. The strict arm was 4.8× cheaper and wrong. Luna lost the same cell in the same arm; two failures of one task at one repetition each still support no efficiency or quality claim in either direction, but they do cut against reading the strict arm as strictly safer.

What the `C_strict` evidence does and does not establish: `skill_delivery_observed` is `false` in all 219 cells, including every `C_strict` cell whose `codemap_delivery` reads `installed_skill`. The `_arm_compliance` contract sets the delivery prefix by construction and treats `skill_delivery_observed` as the separate observational signal, and it never fired. There is therefore no observational proof that the Skill file was read in any cell; the claim the data supports is that **required Codemap querying** produced the effect, not that the Skill was read. Delivery is also confounded with arm — structural `B_auto` used `direct_cli` in all 35 of its Codemap cells and `C_strict` used `installed_skill` in all 55 — so no `B`-versus-`C` difference can be attributed to strictness alone.

Data-quality notes for this run: one repetition per cell throughout, so no cell has a variance estimate and no significance testing was performed; the agentic-family stages carry 3 to 6 tasks each, where a single task flip moves a pass rate by 17 to 33 points. `A_plain` CQ-05 timed out at 600.12 s with zero tokens and null quality — it is a diagnostic-cohort cell and cannot reach any headline figure. `B_auto` RV-02 is the run's one `extraction_failed` cell and is the reason the `B` comparison pairs 44 tasks where `C` pairs 45. `canonical_telemetry_pooling_eligible` is `false` on the structural stage, which is a whole-file admission flag raised by those two run-level failures; it forbids pooling the canonical file wholesale, not per-cell filtering. Token figures throughout this section are gross input including cached reads — see the fresh-token caveat under the aggregated table.

This run has no agentic blast-radius stage, so `gpt-5.6-sol` has no row in the agentic lane below.

#### Codex results — `gpt-5.6-terra` — 2026-09-07

`results/codex-combined-20260907T055156Z/structural` — `gpt-5.6-terra` at high reasoning effort, Codex CLI 0.153.4, 219 cells over 73 tasks in five stages, one repetition per cell, a 600 s wall-clock cap per cell, against the same `pytorch-lightning` 2.6.5 revision `be98784a1` and the same frozen index `3c584089…` every other table here uses. The structural stage started at 2026-09-07T05:55:12Z; every stage is marked `completed` and all 219 cells persisted. Arm contracts are byte-identical to the `gpt-5.6-sol` run's on all three arms (`936a684f…`, `ae5f9a51…`, `83db65ba…`), so this is the second Codex study executed under the optional-use `B_auto` contract. As on Sol, `execution.codex_cli` records `observed_version: codex-cli 0.153.4` against `reviewed_version: codex-cli 0.146.1` and `auth_source_recorded` is false. Full analysis, together with the two Luna agentic re-executions from the same day: `.reports/benchmarks/codex-terra-agentic-paid-analysis.md`.

This is the stratum whose first launch, on 2026-09-06, was refused on a paid-approval token before any model call. The refusal cost nothing and left no telemetry; this section is the relaunch, and it is a separate study from the `gpt-5.6-sol` one it was originally launched beside.

The binary paired rows on the shared 45-task headline cohort are in [Structural results — every model on one cohort](#structural-results--every-model-on-one-cohort--2026-09-06). On the run's own mean-semantic-quality metric with the runner's totals estimator, over the headline cohort:

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

### Agentic results

Agentic tasks give the model a blast-radius question and a working repository. Both providers share the arm labels and the scorer here, so the aggregated table needs no arm translation. Every caveat under [Agentic measurement caveats](#agentic-measurement-caveats) applies to all tables in this lane.

#### Agentic results — every model on one cohort — 2026-09-06

All four models ran the same 16 blast-radius tasks through the same shared prompt materializer, response assessor, AST oracle, and semantic component scorer, under the same three arm labels — the Codex agentic runner maps its native homes onto `A_plain`, `B_auto`, and `C_strict`, so no arm renaming is involved here. This table scores each cell correct or not, rather than by the continuous semantic score the provider sections report, and takes per-task medians of treatment ÷ control. A pair is used only when both of its cells returned a scored, poolable answer.

| Treatment arm | Provider | Model  | Paired n | Control accuracy | Treatment accuracy |          Gain | Tokens | Cost | Time |
| ------------- | -------- | ------ | -------: | ---------------: | -----------------: | ------------: | -----: | ---: | ---: |
| C_strict      | Claude   | Haiku  |       16 |     18.8% (3/16) |      68.8% (11/16) | +50.0 pp (+8) |   −60% | −45% | −46% |
| C_strict      | Claude   | Sonnet |       15 |     53.3% (8/15) |      73.3% (11/15) | +20.0 pp (+3) |   −77% | −65% | −82% |
| C_strict      | Claude   | Opus   |       16 |    62.5% (10/16) |      68.8% (11/16) |  +6.2 pp (+1) |   −50% | −41% | −73% |
| C_strict      | Codex    | Luna   |       16 |    81.2% (13/16) |      87.5% (14/16) |  +6.2 pp (+1) |   −45% |    — | −44% |
| C_strict      | Codex    | Terra  |       16 |    75.0% (12/16) |      87.5% (14/16) | +12.5 pp (+2) |   −29% |    — | −29% |
| C_strict      | Codex    | Sol    |       16 |     56.2% (9/16) |      87.5% (14/16) | +31.2 pp (+5) |   −57% |    — | −57% |
| B_auto        | Claude   | Haiku  |       16 |     18.8% (3/16) |      68.8% (11/16) | +50.0 pp (+8) |   −70% | −51% | −53% |
| B_auto        | Claude   | Sonnet |       15 |     53.3% (8/15) |      86.7% (13/15) | +33.3 pp (+5) |   −72% | −56% | −76% |
| B_auto        | Claude   | Opus   |       16 |    62.5% (10/16) |      87.5% (14/16) | +25.0 pp (+4) |   −43% | −51% | −77% |
| B_auto        | Codex    | Luna   |       16 |    81.2% (13/16) |       50.0% (8/16) | −31.2 pp (−5) |   −17% |    — | −35% |
| B_auto        | Codex    | Terra  |       16 |    75.0% (12/16) |       37.5% (6/16) | −37.5 pp (−6) |   +16% |    — | −20% |
| B_auto        | Codex    | Sol    |       16 |     56.2% (9/16) |       31.2% (5/16) | −25.0 pp (−4) |    +1% |    — | −31% |

One row per stratum, and each row is one execution. All three Codex rows come from a single launch, `results/codex-agentic-20260907T212926Z`, which ran the three declared strata as sequential child studies of one 144-cell sweep; it is described under [Codex agentic balanced-order sweep](#codex-agentic-balanced-order-sweep--2026-09-08). Six earlier Codex agentic executions of the same 16 tasks exist and are **not** in this table — three Luna, one terra, one sol, plus the 2026-09-06 Luna run that lost twelve cells to the unrepaired prompt. They are reported in full under [Codex agentic re-executions](#codex-agentic-re-executions--2026-09-07) and [Codex agentic strata](#codex-agentic-strata--2026-09-07). They are separate studies, never an average and never a repetition design, so pooling or averaging them into these rows is wrong.

**The Codex rows and the Claude rows are no longer the same experiment revision.** The sweep carries `codex-agentic-verified-launcher-balanced-order-2026-09-07`; every Claude row still carries `codex-agentic-skill-imports-guidance-2026-08-09`. Three things changed on the Codex side and only there: arm order is now cyclically balanced rather than fixed lexical, the `C_strict` requirement text names a standalone query through the injected `CODEMAP_BIN` variable *or* the exact installed launcher (and the detector credits a provenance-verified absolute path, which the variable-only rule did not), and the frozen treatment is Codemap 0.34.0 rather than 0.33.1. Read a Claude row against a Codex row as spanning that boundary.

Unlike the structural lane, both `B` and `C` rows are comparable across providers here, because the agentic arms carry the same contract on both: `B_auto` makes Codemap available and optional, `C_strict` requires a compact query.

Every declared Codex stratum now has a row here, and every Codex row pairs 16 of 16 tasks. The sweep is the first Codex evidence in this file where the admission rule removes nothing at all: 144 of 144 cells scored, none incomplete, none contaminated, none non-adherent, no envelope loss. The strict arm lands on exactly 14 of 16 correct in all three strata while the controls scatter from 9 to 13, so the treatment result is close to stratum-independent here and the unaided result is not.

The adherence clause is not symmetric across providers in this lane and the asymmetry is worth stating: the Codex agentic artifacts carry `treatment_adherence`, the Claude ones carry only `codemap_compliant`, which is true in all 48 Claude `C_strict` cells and not evaluated for the optional `B_auto` arm — so no Claude row has ever moved under the rule and the clause has always bitten Codex rows alone. It removes nothing from these three rows. It removed six of Terra's sixteen `C_strict` cells in the superseded execution, and that turned out to be a detector limit as much as a compliance one: frozen native output shows those cells did query, through a per-cell absolute launcher the variable-only rule did not credit. The revised rule credits a provenance-verified absolute path, and terra is adherent on 16 of 16 here. Codex remains the only row set where the optional-use arm goes backwards, and it now does so in all **eight** of its executions, across all three declared strata: −18.2, −40.0, −25.0, −37.5, −37.5, −31.2, −37.5, and −25.0 points. That is the most reproducible result in this lane, and it points the opposite way from every Claude `B` row. Its token penalty does not reproduce — the same arm reads −17%, +16%, and +1% here against +45% on the superseded sol execution — so the accuracy direction is the finding and the token direction is not.

Binary correctness is a harsher reading than the semantic score: Haiku's control answers 18.8% of cells outright while scoring far higher component-wise, so the large Haiku gains are movement from partially-right to exactly-right. Cost is empty for Codex because that runner captures no per-cell price. Every [agentic measurement caveat](#agentic-measurement-caveats) applies to this table. The order caveat now splits by provider: the Claude rows ran a fixed `A_plain` → `B_auto` → `C_strict` order, the Codex rows a cyclic rotation that balances each arm across positions. Neither resets the provider prompt cache between cells, so elapsed remains the metric most exposed to order in both.

#### Claude multi-model — median change against `A_plain`

Per-task medians over the 16 blast-radius tasks, stated as change against the control: negative means the arm needed less. This is the Claude-only view; for the cross-provider view with accuracy, see [Agentic results — every model on one cohort](#agentic-results--every-model-on-one-cohort--2026-09-06).

| Tier   | Arm      | Elapsed | Cost | Input tokens | Tool calls |
| ------ | -------- | ------: | ---: | -----------: | ---------: |
| Haiku  | C_strict |    −46% | −45% |         −60% |       −62% |
| Haiku  | B_auto   |    −53% | −51% |         −70% |       −68% |
| Sonnet | C_strict |    −82% | −65% |         −77% |       −72% |
| Sonnet | B_auto   |    −76% | −56% |         −72% |       −62% |
| Opus   | C_strict |    −73% | −41% |         −50% |       −52% |
| Opus   | B_auto   |    −77% | −51% |         −43% |       −59% |

Evidence recall is at parity or better in every Codemap cell: the treatment arms answer 100% `erec`/`rrec` throughout, while `A_plain` drops below it on BA-03 (haiku 81%), BA-12 (haiku 89%), and BA-15 (85% on all three tiers). Sonnet's denominators are 15 rather than 16 because BA-12 `A_plain` hit the 600-second coordinate timeout and is excluded, not scored as a loss.

Tool *time* moves the other way — median +57% to +155%, more rather than less — because each index call costs more than a single grep. The win is in needing far fewer calls, not in each call being faster.

#### Codex agentic results — 2026-09-06

`results/codex-combined-20260906T085207Z/agentic` — `gpt-5.6-luna` at high reasoning effort, Codex CLI 0.153.4, 48/48 cells persisted, none incomplete, none contaminated.

| Model | Pair                | Cells | Control score | Treatment score |   Gain | Cells correct | Tokens | Time |
| ----- | ------------------- | ----: | ------------: | --------------: | -----: | ------------: | -----: | ---: |
| Luna  | A_plain vs C_strict |    16 |         0.929 |       **0.960** | +0.031 |    9 → **13** |   −48% | −47% |
| Luna  | A_plain vs B_auto   |    16 |         0.929 |           0.860 | −0.069 |         9 → 6 |   −21% | −33% |

Score is mean semantic quality across the 16 cells; the correct-cell count is how many of those cells were right outright. For this run beside the Claude models on one binary-scored cohort, see [Agentic results — every model on one cohort](#agentic-results--every-model-on-one-cohort--2026-09-06). `B_auto` is worse on both — it loses three cells the unaided control answered — so its cheaper tokens buy a real quality regression rather than a trade.

Restricted to pairs where both cells returned the strict answer envelope — the only poolable comparison — `C_strict` reads 0.935 → **0.996** over 9 pairs at −44% tokens and −45% time, and `B_auto` reads 0.937 → 0.857 over 11 pairs at −15% tokens and −32% time. The strict arm is better on both readings and the optional-use canary is worse on both; the direction does not depend on which subset is used.

Expected-importer recall was 0.990 for `A_plain` and `B_auto` and 1.000 for `C_strict`, in both the full agent text and the final report. Exposure hits per command (DEFF) were 2.00 / 1.76 / 2.42.

Twelve of the 48 cells lost the strict envelope and fall back to diagnostic bare-JSON recovery, which is not poolable: seven `C_strict`, three `B_auto`, two `A_plain`. This is a prompt-contract defect rather than a provider result — see [Task defects found, fixed, and what remains](#task-defects-found-fixed-and-what-remains). Three `C_strict` cells (BA-01, BA-13, BA-14) issued no Codemap call at all and are recorded as treatment not followed; BA-01's transcript narrates running the query it never ran.

#### Codex agentic re-executions — 2026-09-07

The same 16 blast-radius tasks were executed twice more on `gpt-5.6-luna`, at high reasoning effort on Codex CLI 0.153.4, 48/48 cells persisted in each:

- **`Luna⁺`** — `results/codex-agentic-20260907T065010Z`, launched `--agentic --isolated`, which builds a private run worktree and relocates the locked index into it (`e0ce11d9…`, the same index content at a different path). Started 2026-09-07T06:50:13Z.
- **`Luna⁺⁺`** — `results/codex-combined-20260907T055156Z/agentic`, the agentic stage of the combined launch whose structural half ran terra, against the shared clone and the locked index `3c584089…`. Started 2026-09-07T09:51:11Z.

Both ran the repaired `agentic_contracts` prompt; the 2026-09-06 run did not, so these two are executions of one contract and the older run is not a third repetition of it. The two 09-07 manifests differ only in the structural-manifest and launcher hashes they carry, not in any agentic material.

`Luna⁺⁺` held the `Luna` row of the [cross-provider agentic table](#agentic-results--every-model-on-one-cohort--2026-09-06) until the [balanced-order sweep](#codex-agentic-balanced-order-sweep--2026-09-08) replaced it; the row it held read 16 pairs, `11 → 13` correct at −45% tokens and −49% time. All three Luna executions are now reported here and nowhere else: one model with several studies published as adjacent rows reads as a repetition design, which it is not.

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

#### Codex agentic strata — 2026-09-07

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

#### Codex agentic balanced-order sweep — 2026-09-08

`results/codex-agentic-20260907T212926Z` — one launch, three sequential child studies, 144 of 144 cells persisted. This is the set the [cross-provider agentic table](#agentic-results--every-model-on-one-cohort--2026-09-06) publishes:

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

**Control performance is the volatile term, and the spread between strata is three cells rather than a capability ordering.** Sol's control differs from luna's on exactly three tasks — `BA-02` (0.574 against 1.000), `BA-09` (0.722), `BA-10` (0.846) — and matches it on the other thirteen, including the three every control fails identically in every execution (`BA-01` 0.889, `BA-12` 0.825, `BA-15` 0.877). The same sol stratum answered `BA-02`, `BA-09`, and `BA-10` correctly in `results/codex-agentic-20260907T141122Z`, for 12 of 16, and `BA-02` also flipped between terra's two executions, so these are unstable cells rather than a stratum ranking. The methodology manifest declares the three Codex strata as a list and assigns them no capability order, unlike the Claude tiers; nothing in this file supports reading `9 → 14` on sol as "the strongest model needed the most help". All three misses land in the same `ranking`/`rdep_counts` fields discussed below.

**`B_auto` regresses on all three strata again, and converts almost nothing.** Over the 48 optional-arm cells it turns exactly one control failure into a success (sol `BA-09`) and loses sixteen control successes. `BA-03`, `BA-07`, and `BA-08` are lost by the optional arm in every stratum. Its token penalty does not reproduce: paired medians are −17%, +16%, and +1% against +45% on the superseded sol run, so the accuracy direction is the reproducible half and the token direction is not. Three things changed in one step here, so nothing in this sweep attributes the token shift to any single cause.

**The optional arm's loss is confined to two answer components, and it is the same two everywhere.** Scoring the 144 cells component by component, `A_plain`, `B_auto`, and `C_strict` are at parity on every enumeration field — `production_importers` reads 0.988 / 0.991 / 1.000, and `buckets`, `overlap_importers`, and every `*_count` field sit at 1.000 in all three arms. The whole regression lives in the two ordered/counted fields: `ranking` reads 0.911 / 0.625 / 0.944 and `rdep_counts` 0.758 / 0.395 / 0.994. Twenty-one `B_auto` cells miss `ranking` against seven controls and five strict cells. So the optional arm finds *who imports what* as well as anyone; it ranks and counts them worse than the arm with no tool at all.

**The mechanism is the counting definition, not tool access.** Both treatment arms reach for `query --compact central` in a similar number of cells (21 for `B_auto`, 23 for `C_strict`), so this is not ignorance of the subcommand. Tracing luna `BA-03` command by command: all three arms return an identical importer list, and only the ranking differs — control and strict answer `lightning.pytorch.trainer.call`, `B_auto` answers `lightning.pytorch.core.module`, the module with the largest raw reverse-dependency count once `examples.*` and tests are included. The task ranks within `production_importers`. The strict arm's first command reads the Skill's `Need → Query` routing table before it queries anything; the optional arm's first commands are `--help`, `doctor --json`, and `list --help`, after which it improvises a per-candidate `rdeps` loop and ranks by the raw counts that loop returns. Luna `BA-15` shows the softer version of the same failure — the right five modules in the wrong order, scored 0.14 — and luna `BA-07` the harder one, where the ranked set contains modules outside the candidate set entirely. Having the counts without the contract that defines which counts is worse than having no counts.

**Discovery cost still separates the two treatment arms.** Heuristic help/discovery calls run 59, 50, and 53 in `B_auto` against 7, 3, and 3 in `A_plain` and 2, 2, and 1 in `C_strict`. The Skill-bound arm does not shop for help; the bare-CLI arm does, in every stratum.

**Uptake fields still disagree in the optional arm.** `codemap_used` reads 14, 13, and 13 of 16 while the stricter `codemap_calls` reads 5, 8, and 11 — the same chained-command shell style seen before, not a difference in whether the tool ran. In `C_strict` both fields read 16 of 16 everywhere, and `A_plain` reads zero under both, so no control is contaminated.

Expected-importer recall is 1.000 for `C_strict` in all three strata; `A_plain` reads 0.990, 0.990, and 0.970, and `B_auto` 0.990, 1.000, and 0.990. DEFF means are 2.45 / 1.85 / 3.82 on luna, 3.57 / 2.41 / 3.37 on terra, and 2.47 / 1.94 / 5.22 on sol — the strict arm leads on two of the three, which reverses the earlier pattern where the grep-driven control always led.

Cached input is 90%, 86%, and 89% of control gross, so the token columns are gross-input claims with the usual caveat; fresh-input paired medians are −21%, −7%, and −17% for `C_strict` against gross −45%, −29%, and −57%. The tail still dominates the cohort totals: luna's `BA-13` control alone spends 1,587,026 gross input tokens against the strict arm's 76,867, and sol's `BA-15` spends 561,558 against 106,016.

Arm order is balanced here, which the earlier tables' order caveat did not have; the provider prompt cache is still never reset, so rotation redistributes that exposure rather than removing it. Full per-task analysis: `.reports/benchmarks/codex-agentic-balanced-order-sweep-analysis.md`.

#### Why a treatment arm sometimes reads more than its control

Several rows above show the Codemap arm reading *more* input than the plain control — `+16%` on terra `B_auto` and `+1%` on sol `B_auto` in the balanced-order sweep, `+45%` on sol `B_auto` and `+7%` on terra `C_strict` in the superseded executions, `+15%` and `+34%` on the structural editing stages. A command-level replay of all 96 agentic cells and the 54 structural cells finds no token-accounting defect behind them: `token_accounting_inconsistent` is false in all 150 cells. Observed-use classification is a separate measurement issue for historical absolute launchers; reviewed per-coordinate provenance is required before replay credits one. The cause of the token differences is agent behaviour, and it has three parts.

**Gross input counts round-trips, not tool cost.** Each command is another model round-trip that re-sends the whole conversation, so cost follows how many commands an arm issues and how much text each drags in. Captured tool-output character counts are diagnostics, not native model-input token counts, so they do not establish how many input tokens a query contributed. The strict arm can issue 64% more commands than the control (10.5 against 6.4 per cell) and still read less (157k against 177k).

**The optional arm is additive rather than substitutive.** Captured command output is higher for `B_auto` than for the control or strict arm: it queries the index *and* explores by hand anyway. These captured-character diagnostics are not native model-input tokens, so they do not quantify the input-token contribution of a command. `B_auto` and `C_strict` issue the same number of commands; their gross-input gap remains an observed cohort difference rather than a tool-output-token attribution.

**Some cells bypass the tool and read the raw index file.** The locked index is on disk at `.cache/codemap/*.json`, and a cell that discovers it may `rg` or `jq` it directly. Terra `B_auto` `BA-06` pulled 262,151 tokens — about a megabyte of raw index — in a single `rg`, 65% of that cell's entire 401k input, two commands after the tool had already answered the same question in 1,494 tokens. That one command, not Codemap, is the largest single cost event in either run.

A fourth, milder pattern rides along: several cells spend two to four round-trips on `--help` and `doctor --json` before their first real query. None of these three mechanisms is a property of the tool, and all three are visible one command at a time in `native_attempt_events` in the frozen telemetry.

#### Agentic measurement caveats

These apply to every agentic result table in this file, on both providers, and to the frozen artifacts behind them. They do not apply to the structural lanes.

- **Arm order and provider cache.** Every agentic run except the [balanced-order sweep](#codex-agentic-balanced-order-sweep--2026-09-08) executed the arms in fixed `A_plain` → `B_auto` → `C_strict` order per task; those lanes were never counterbalanced. The sweep rotates the arms cyclically by locked task ordinal, so each arm holds each position across the cohort. No run of either kind resets the provider prompt cache between cells, so a later arm still inherits a warmed cache within its own task. Elapsed time is the metric most exposed: read the elapsed figures from the fixed-order tables as order-confounded, and those from the sweep as order-balanced but not cache-isolated. Input-token ratios are much less exposed but not immune in either case.
- **EREC/RREC absolutes.** The scorer behind the frozen artifacts credited an expected module when its name appeared anywhere inside the exposure or report text, including as a substring of an unrelated dotted name. Every `1.0000` recall reading above therefore includes free credit. The rule now anchors whole dotted names. The bias is arm-symmetric, so A/B/C deltas survive; the absolute values in frozen artifacts do not.
- **Quality-score floor.** The semantic score is an unweighted mean over components, several of which are enumerated fields a model can hit without doing the work. Absolute `aqs` and mean-semantic-score values therefore sit above true zero-work performance. This too is arm-symmetric.

<details>
<summary>Stopped, partial, and historical agentic runs</summary>

The stopped directory `results/codex-agentic-20260804T205639Z` is infrastructure-only evidence with zero model cells. The launcher previously opened its console capture inside the new result directory before the runner's strict launcher-only admission check, so the runner rejected the launcher's own file. Console capture now uses a private temporary file outside the result directory, while the strict admission invariant remains unchanged. Any supported-entrypoint failure preserves the reported artifact and prints the exact dry-run command plus a fresh timestamped paid command; reuse of an existing result directory remains forbidden.

The stopped directory `results/codex-agentic-20260805T122121Z` contains 14 successful transport rows but is infrastructure/scoring diagnostic evidence only. Runtime identity drift stopped admission before the fifteenth cell, and the superseded response path conflated strict-envelope failure with absent semantic and raw-text evidence. The prospective runner freezes plugin source bytes before the first cell, preserves identity evidence even when initial C admission fails, and records semantic validity, diagnostic recovery, pooling eligibility, EREC, RREC, and DEFF as separate fields.

The stopped directory `results/codex-agentic-20260805T144950Z` contains one successful A row and is infrastructure-only evidence. The snapshot archive preserved the B launcher bytes but stripped its executable mode from `0755` to `0600`, so B admission failed before a model call. The repaired archive writes executable inputs as private `0700`, writes other inputs as `0600`, records each mode in the snapshot ledger, and fails closed on later byte or mode drift.

The completed frozen run `results/codex-agentic-20260804T172617Z` is historical immutable evidence: it persisted 9/9 BA-01 cells under archived machine manifest `f8490d39e2dbade395600423e4096cee94d7f87d1ada4cbe0a876fa74052fa8c`. Direct-importer exposure and final-report recall were `1.0` in every arm and repetition. Relative to `A_plain`, `B_auto` reduced mean input tokens by 39.6%, output tokens by 58.4%, and elapsed time by 52.5%; the then-named `C_required` arm reduced them by 66.3%, 71.9%, and 69.6%, respectively. These bounded exploratory means from one task and three repetitions are not the current default, are not pooled with the 16-task study, and do not define provider-wide performance.

</details>

### Task defects found, fixed, and what remains

Two suite defects were found after the first execution and corrected before these numbers were taken; a third is a real tool gap and is deliberately still scored as a miss. Full evidence: `.reports/benchmarks/2026-09-06/analysis.md`.

- **CQ-03 — fixed.** Its `coupled --top 5` ground truth had drifted from the frozen index (`trainer` 49 against the index's 52, and a different fifth module), so `ordered_coupled_ranking` scored 0 in every arm of every model, including arms reporting the tool's own output verbatim. Regenerated from the frozen index. Haiku and Opus now score 1.000 in both Codemap arms.
- **GR-04 — fixed.** The prompt asked for "the 15 most-imported modules" while the ground truth was `central --top 15 --exclude-tests`, so answers including test helpers were scored down to a deterministic 10/15. The prompt now states the constraint. All three tiers now score 1.000 in both Codemap arms, and the unaided arm improves as well (Haiku 0.333 → 0.800, Sonnet 0.867 → 0.933, Opus 1.000).
- **CQ-05 — a genuine tool gap, left as a miss.** The task asks for unique public symbols with repeated declarations deduplicated; the independent AST oracle finds 9, while `uncovered` reports `unique_total: 20` for the same module — 11 of those 20 do have coverage under the oracle. The Codemap arms answer 20 and are marked wrong. Accepting the tool's own count as a second correct answer would have laundered an accuracy defect into a pass, so the fix belongs in `uncovered`.

One genuine model-behaviour miss survives on the corrected CQ-03: Sonnet's Codemap arms return the correct five modules with correct counts but re-sorted by `dep_count`, while the prompt asks for the tool's returned order. `ordered_coupled_ranking` scores that 0, correctly. Haiku and Opus preserve the returned order.

Twelve of 495 structural cells (2.4%) failed answer extraction and two control cells were dropped as contaminated (Haiku CQ-02, Sonnet CQ-03 — the unaided arm reaching Codemap material). All are excluded from both sides of every paired figure, which is why paired n varies between 47 and 51.

The Codex run surfaced two further harness defects, both of which cost the treatment arms rather than the control. Neither is rescored in place — the artifacts are immutable — so each fix takes effect prospectively. This run's `B_auto` arm predates the optional-use alignment and required a Codemap query, so the cells named below are not comparable to a future `B_auto` run:

- **The answer envelope was only implied — fixed, and the fix is now measured.** `answer_format_instruction` in `benchmarks/_bench_common/agentic_contracts.py` said "Return one JSON object containing exactly these labels" and then showed `BEGIN_ANSWER_JSON` / `END_ANSWER_JSON` inside a block explicitly labelled "Example using synthetic values only". Nothing instructed the model to wrap its own answer. Claude inferred it anyway (1 miss in 144 cells); Codex took it literally and returned bare JSON in 12 of 48. The instruction now states the wrap requirement outside the example, and all seven Codex agentic executions since — two on Luna, one each on terra and sol, then the three-stratum balanced-order sweep — lost **zero** cells to it, 336 cells with no envelope loss; see [Codex agentic re-executions](#codex-agentic-re-executions--2026-09-07), [Codex agentic strata](#codex-agentic-strata--2026-09-07), and [Codex agentic balanced-order sweep](#codex-agentic-balanced-order-sweep--2026-09-08). The 09-06 artifact keeps its twelve losses; they are not rescored.
- **Two correct counts were scored as extraction failures.** `_evaluate_rv`'s count patterns require the word order "N distinct/unique production callers". RV-04/B_auto wrote "24 production functions uniquely call …" and RV-05/C_strict answered "1. **11**" as a numbered sub-answer. Both numbers are the ground truth; neither matched a pattern. The control arm's phrasing on the same task did match, so the gap is arm-asymmetric by accident. Fixed: the patterns now accept a trailing qualifier and an enumerated sub-answer that carries the number alone. A replay of all 60 RV rows across both providers changes exactly these two and nothing else.

One further observation is *not* being changed, because the current behaviour is specified and tested: canonical query credit requires the literal `$CODEMAP_BIN` token, and `test_historical_exact_launcher_and_compound_forms_reject_native_item_contract` asserts that an absolute path to the very launcher the arm provisioned is rejected. RV-01 lost its treatment credit to that rule. The rule is defensible — it keeps delivery evidence to one exact command shape — but it scores the spelling of a command rather than the act, and the same regex backs the contamination signal for the control arm. Changing it is a contract decision, not a bug fix, so it stays open rather than being quietly flipped.

### Query benchmark — 2026-09-06

`PARTIAL`, 14 of 18 primary scenarios, with self-consistency `CONSISTENT` 14/14. The four misses are honest gate failures on this corpus, not crashes:

| Scenario | Measured                              | Gate     |
| -------- | ------------------------------------- | -------- |
| C1       | coverage gap 0.00 (0 verified extras) | ≥ 0.10   |
| C3       | leverage ratio 1.45 (45 cold/31 warm) | ≥ 2.0    |
| L2       | `rdeps` median 146 ms                 | ≤ 100 ms |
| L4       | cold 351 ms vs warm 291 ms → 1.21×    | ≥ 2.0×   |

L4 previously failed on undefined data: the timing helper counted `grep`'s "no matches" exit status 1 as a failed command, so the cold-baseline median was `NaN` and the speedup gate compared against nothing. Search tools now count exit 1 as a completed search. L1 (`central`, 142 ms against a 200 ms gate) and L2 together show roughly 140 ms of fixed process start-up per call, which is what both latency gates are really measuring.
