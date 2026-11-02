# codemap-py — development notes

Technical detail for contributors, maintainers, and agents. User-facing install, usage, and limits live in [README.md](README.md).

## Contents

- [Layout](#layout)
- [Hooks and registration](#hooks-and-registration)
- [Telemetry and debrief internals](#telemetry-and-debrief-internals)
- [Codex question routing](#codex-question-routing)
- [Skill rosters and docs](#skill-rosters-and-docs)
- [Benchmark narrative](#benchmark-narrative)

## Layout

| Path | Contents |
| -- | -- |
| [`bin/`](bin/README.md) | Shipped launchers, helpers, and compatibility shims. |
| [`scripts/`](scripts/README.md) | Deterministic package builds, validation, and install probes. |
| `claude-skills/`, `codex-skills/` | Per-runtime skill rosters; `claude-skills/_shared/` holds the live consumer contracts. |
| `hooks/` | Hook modules and the `claude-hooks.json` / `codex-hooks.json` registrations. |
| `shared/` | Runtime-neutral contracts such as `capability-contract.md` and `codex-user-questions.md`. |
| `src/` | Scanner, query engine, and integration engine. |
| `tests/` | Pytest suites, including packaging and manifest checks. |

## Hooks and registration

- Claude hook commands invoke the packaged Codemap launcher with `--run-hook` and a supported hook basename; the launcher verifies the supported CPython runtime before dispatch.
- Hook launchers select a verified Python 3.11–3.14 runtime and pass `--run-hook` only for supported hook basenames. Versioned executable names are probed before running the hook; Linux, macOS and native Windows use the same closed dispatch contract.
- Codex hooks cover ambient preamble/guard behavior and runtime-scoped tool records; the Codex host provides no Codemap skill-start hook.
- The shared Claude/Codex prompt hook stays silent for a missing index when scanner exclusions leave no indexable `.py` or `.pyi` sources; an existing index still refreshes after the final source deletion.
- A missing-index automatic-query directive retains the bounded package/packaging marker condition (`__init__.py`, `pyproject.toml`, or `setup.py`); no universal `__init__.py` requirement is implied. Existing indexes with real source retain normal status and refresh behavior.
- Prompt status and missing-index sentinels are scoped per runtime and filename-safe session identity, falling back to `shared` when absent.
- Resolver doctests preserve the caller's `TMPDIR`, including empty and absent values, and Python's temporary-directory cache, so downstream plugin tests retain their original temporary-directory boundary.
- Python hook module docstrings describe event inputs, local side effects, output envelopes, and failure handling. Pure helper examples run as doctests in the repository pytest suite; hook lifecycle and filesystem behavior use pytest fixtures.

## Telemetry and debrief internals

- Timeout alarms are cleaned up after embedded calls. Refresh children retain an available host session from hook payloads, and Codex background refresh keeps its own trigger.
- Batch children are logical answers, not extra CLI invocations; failed and unjoinable children are counted separately in JSON and text.
- Legacy records missing project identity or terminal outcome remain untouched and counted, but cannot support joins; shared log destinations never replace a record's originating project root.
- New Grep/Glob records preserve `search_path` and producer-observed `search_scope`. Legacy logs remain untouched, and unknown cases do not count toward `structural_search_count`; `unknown_count` is reported beside it, overall and per runtime.
- Bash search records: literal operands are checked at hook time (relative ones against the event's working directory), operand-less `rg` uses that directory, and `grep` skips a directory operand unless recursive.
- Unscoped Bash records fall back to the target, cut at 200 characters, so later flags or paths are unavailable; recursive-looking ones outside own-file inspection stay `unknown`, never presumed directory searches.
- `join_avoidance.py` remains a shape proxy, not proof of redundant work.

## Codex question routing

- Codex uses structured question controls first; the root uses a permitted question route with meaningful presets and built-in custom input, and children hand decisions back to the root.
- Transport discovery follows the necessity check. Existing answers, scope/digest confirmation and runtime permission remain authoritative.
- Async questions are used only where current host evidence establishes usable controls; tool acceptance alone is insufficient, and unknown async usability is unsuitable. An explicit current host contract establishing rendered controls, text input, and answer lifetime permits first use.
- Before fallback, discover deferred native question tools through the current catalog or tool search, including `functions.exec` `ALL_TOOLS` when exposed, and record route eligibility. Select the route before asking.
- A synchronous mode error does not establish async unavailability. Explicit higher-priority host requirements for plain text remain binding and are reported as policy restrictions, not missing tools.
- Rejected question calls resume at the pending decision without replaying delivered report context.
- A user-reported dismissed question leaves its decision pending; the dismissed transport stays unsuitable for later required questions in that host until delivery is verified again.
- A text-delivered async question stays pending without a second control or repeated menu. The plain-chat fallback is delivered once in the final response, never in both commentary and final.
- Detailed approval/recovery rules load only when needed; see [`shared/codex-user-questions.md`](shared/codex-user-questions.md).

## Skill rosters and docs

- Both skill rosters use concise, instruction-first prose while retaining command syntax, routing, stop rules, safety gates, and runtime notes for installed-root resolution, PATH behavior, and each host's confirmation mechanism.
- Claude executable fences remain byte-identical so compression cannot change shell behavior.
- Skill boundaries: `scan-codebase` builds or refreshes the whole index, optionally from a consistent `--root`; `query-code` reads dependencies, callers, symbols, paths, quality flags, tests, or diff impact from an existing index; `test-impact` identifies structurally affected tests and emits a pytest command without executing it; `rename-refs` applies or previews one rename with confirmation and a re-scan verification pass; `integration` audits, plans, applies, syncs, or demos consumer wiring and active query guidance; `debrief-coding` summarizes local cross-runtime telemetry, timing, completeness, and repeated-search avoidance.
- The [rendered Codemap-py page](https://borda.github.io/AI-Rig/codemap-py/) projects the README into the documentation site; [`CHANGELOG.md`](CHANGELOG.md) records versioned changes.

## Benchmark narrative

Full per-cohort and per-stratum narrative moved out of the README, which keeps a summary table. Current values, methods, and limitations are authoritative in [`benchmarks/README.md`](../../benchmarks/README.md#results).

The benchmark record gives Codemap a measurable, bounded value proposition: on structural questions with unresolved dependency or caller scope, a required Codemap Skill reduced context and elapsed time in every model stratum measured so far, and improved the aggregate semantic answer score in five of the six. The sixth, the 2026-09-07 Codex `gpt-5.6-sol` stratum, is a quality tie against a control already at a median score of 1.000 — the cost reduction reproduced there and the quality gain did not.

Those are historical scored-cell summaries, not an all-assigned pass rate; the record retains unfavorable and unobserved cells and explains when adaptive routing should skip Codemap.

The latest saved agentic reports use `reporting_version=agentic-graded-v2`. Their `quality` averages graded credit over all assigned cells and zeroes execution, formatting, contamination, treatment, and unobserved failures; `exact_pass` separately requires complete correctness and admission. The extended-suite snapshot below also exposes raw semantic credit so compliance failures do not masquerade as content errors. Legacy `component` means remain secondary diagnostics, not interchangeable with graded credit.

Canonical efficiency compares only pairs where both cells pass exactly; improvements, regressions, both-fail outcomes, and unavailable usage remain separate. Historical snapshots retain their original contracts and are not pooled with the latest runs.

<!-- Related result tables — a figure here restates a run measured in the benchmark suite. Editing one means checking:
  - plugins/codemap-py/README.md § All models on one cohort (both tables below)
  - plugins/codemap-py/README.md § Structural navigation snapshot, § Agentic navigation snapshot, § Codex navigation snapshot, § Codex gpt-5.6-sol snapshot, § Codex gpt-5.6-terra snapshot, § Extended-suite agentic snapshot
  - benchmarks/README.md § Results — structural and agentic lanes, per-provider views included
  Figures are recomputed from immutable artifacts under benchmarks/results/; a corrected number means a new run. -->

### All models on one cohort

Measured 2026-09-06. Both providers and every model stratum measured on one cohort, one metric, and one estimator: the shared task cohort, a cell counted correct or not, and per-task medians of treatment ÷ baseline restated as change against the baseline. The per-provider snapshots below keep their own canonical scoring and therefore differ.

Structural, on the 45-task headline cohort both providers share:

| Treatment arm | Provider | Model | Paired n | Baseline accuracy | Treatment accuracy | Gain | Input tokens | Cost | Elapsed |
| -- | -- | -- | -: | -: | -: | -: | -: | -: | -: |
| C_strict | Claude | Haiku | 39 | 71.8% (28/39) | 87.2% (34/39) | +15.4 pp (+6) | −80% | −75% | −74% |
| C_strict | Claude | Sonnet | 42 | 83.3% (35/42) | 92.9% (39/42) | +9.5 pp (+4) | −57% | −52% | −53% |
| C_strict | Claude | Opus | 43 | 88.4% (38/43) | 97.7% (42/43) | +9.3 pp (+4) | −19% | −23% | −31% |
| C_strict | Codex | Luna | 43 | 88.4% (38/43) | 97.7% (42/43) | +9.3 pp (+4) | −29% | — | −36% |
| C_strict | Codex | Terra | 40 | 87.5% (35/40) | 92.5% (37/40) | +5.0 pp (+2) | −42% | — | −51% |
| C_strict | Codex | Sol | 45 | 93.3% (42/45) | 93.3% (42/45) | 0.0 pp (+0) | −51% | — | −49% |
| B_auto | Claude | Haiku | 42 | 73.8% (31/42) | 85.7% (36/42) | +11.9 pp (+5) | −49% | −58% | −56% |
| B_auto | Claude | Sonnet | 42 | 83.3% (35/42) | 97.6% (41/42) | +14.3 pp (+6) | −59% | −44% | −50% |
| B_auto | Claude | Opus | 44 | 88.6% (39/44) | 95.5% (42/44) | +6.8 pp (+3) | −30% | −28% | −31% |
| B_auto | Codex | Luna | 44 | 86.4% (38/44) | 93.2% (41/44) | +6.8 pp (+3) | −33% | — | −27% |
| B_auto | Codex | Terra | 41 | 87.8% (36/41) | 95.1% (39/41) | +7.3 pp (+3) | −5% | — | −20% |
| B_auto | Codex | Sol | 44 | 93.2% (41/44) | 97.7% (43/44) | +4.5 pp (+2) | +1% | — | −13% |

The six `C` rows are directly comparable — every one requires the installed Skill, and the three Codex rows carry byte-identical `A_plain` and `C_strict` arm contracts. Five of them improve accuracy while reading less. `Sol` `C_strict` is the exception: it answers exactly the same 42 of 45 cells as its own baseline for 51% fewer gross input tokens, a cost win and a quality tie against a baseline already at a median score of 1.000. `Terra` `C_strict` converts two cells and loses none.

A median cannot say whether an arm saves reliably or saves because a few tasks saved enormously, so the same per-task token ratios are restated below as distributions over the same pairs.

| Arm | Provider | Model | Paired n | Median | Geo mean | p10–p90 | sd(log) | Cheaper in |
| -- | -- | -- | -: | -: | -: | -: | -: | -: |
| C_strict | Claude | Haiku | 39 | 0.204× | 0.215× | 0.031–1.384 | 1.461 | 32/39 |
| C_strict | Claude | Sonnet | 42 | 0.431× | 0.398× | 0.112–1.184 | 0.984 | 32/42 |
| C_strict | Claude | Opus | 43 | 0.806× | 0.697× | 0.304–1.282 | 0.606 | 26/43 |
| C_strict | Codex | Luna | 43 | 0.711× | 0.566× | 0.176–1.505 | 0.962 | 29/43 |
| C_strict | Codex | Terra | 40 | 0.581× | 0.565× | 0.308–1.125 | 0.618 | 33/40 |
| C_strict | Codex | Sol | 45 | 0.486× | 0.496× | 0.258–1.066 | 0.596 | 39/45 |
| B_auto | Claude | Haiku | 42 | 0.508× | 0.286× | 0.033–1.175 | 1.462 | 35/42 |
| B_auto | Claude | Sonnet | 42 | 0.412× | 0.407× | 0.104–1.183 | 1.143 | 31/42 |
| B_auto | Claude | Opus | 44 | 0.700× | 0.697× | 0.322–1.256 | 0.603 | 27/44 |
| B_auto | Codex | Luna | 44 | 0.667× | 0.578× | 0.187–1.292 | 0.749 | 34/44 |
| B_auto | Codex | Terra | 41 | 0.947× | 0.888× | 0.434–1.439 | 0.482 | 23/41 |
| B_auto | Codex | Sol | 44 | 1.014× | 0.991× | 0.535–1.951 | 0.555 | 19/44 |

- **Median**: middle per-task ratio of treatment ÷ baseline [below 1.0× = treatment spent less]
- **Geo mean**: geometric mean of the same ratios [the average appropriate to ratios]
- **p10–p90**: interior 80% of tasks [narrow = consistent]
- **sd(log)**: standard deviation of the log ratio [scale-free spread]
- **Cheaper in**: tasks where the treatment arm spent less, out of the paired total

Every one of the twelve rows has a p90 above 1.0×, so at least a tenth of tasks cost the Codemap arm more: reading less is a distributional result, never a per-task guarantee. Similar medians also do not mean equal reliability — Sonnet and Sol `C_strict` sit at 0.431× and 0.486× with sd(log) 0.984 against 0.596. And where an arm mixes tool users with non-users, the median describes neither: Haiku's `B_auto` cells that queried Codemap saved 66% at the median while the six that did not sit at parity.

The `B` rows are not comparable across providers, and the Luna `B` row is not comparable to the other two Codex ones. Claude's `B_auto` makes Codemap optional and measures unprompted reach; the frozen Luna run's `B_auto` was executed under a prompt that required the direct CLI, so it answers a different question. The contract was then changed to optional-use, and `Sol` and `Terra` are the Codex runs under it — so only those two `B` rows ask the Claude question, and the Luna `B` row must not be blended with them. Cost is empty for Codex because that runner captures no per-cell price.

The optional arm's central negative now has two independent measurements. On both optional-contract strata, splitting the `B_auto` token ratio on whether the cell actually queried Codemap changes nothing — 0.974× against 1.016× on `Sol` at 26 of 44 uptake, 0.942× against 0.947× on `Terra` at 34 of 41 — so availability alone buys no saving, and low uptake is not the reason.

Three things bound how the Codex rows may be stated. Each stratum is one study at one repetition per cell, never a Codex average. Input tokens are gross: roughly 91% of `Sol`'s and 85% of `Terra`'s gross input is cached, so `Sol`'s 71% whole-run gross reduction is a 42% fresh reduction and `Terra`'s 55% is 30% — any dollar claim must be made on fresh tokens or say that it is not. And `skill_delivery_observed` is false in all 219 cells of each, including every `C_strict` one, so the supported claim is that required Codemap *querying* produced the effect, not that the Skill file was read.

Agentic, on the 16 shared blast-radius tasks, where both providers use the same arm labels and the same scorer:

Three quality columns and three cost columns. Quality: `Accuracy` is the binary oracle verdict, `Score` the continuous semantic score with partial credit, `RREC` the expected-importer recall of the final report. Cost: input tokens, dollars, wall-clock. `Score` and `RREC` read `baseline → treatment` over the same paired cells as the accuracy columns.

| Treatment arm | Provider | Model | Paired n | Baseline accuracy | Treatment accuracy | Gain | Score | RREC | Input tokens | Cost | Elapsed |
| -- | -- | -- | -: | -: | -: | -: | -: | -: | -: | -: | -: |
| C_strict | Claude | Haiku | 16 | 18.8% (3/16) | 68.8% (11/16) | +50.0 pp (+8) | 0.714 → 0.936 | 0.97 → 1.00 | −60% | −45% | −46% |
| C_strict | Claude | Sonnet | 15 | 53.3% (8/15) | 73.3% (11/15) | +20.0 pp (+3) | 0.917 → 0.946 | 0.99 → 1.00 | −77% | −65% | −82% |
| C_strict | Claude | Opus | 16 | 62.5% (10/16) | 68.8% (11/16) | +6.2 pp (+1) | 0.924 → 0.940 | 0.99 → 1.00 | −50% | −41% | −73% |
| C_strict | Codex | Luna | 16 | 81.2% (13/16) | 87.5% (14/16) | +6.2 pp (+1) | 0.974 → 0.990 | 0.99 → 1.00 | −45% | — | −44% |
| C_strict | Codex | Terra | 16 | 75.0% (12/16) | 87.5% (14/16) | +12.5 pp (+2) | 0.967 → 0.973 | 0.99 → 1.00 | −29% | — | −29% |
| C_strict | Codex | Sol | 16 | 56.2% (9/16) | 87.5% (14/16) | +31.2 pp (+5) | 0.914 → 0.994 | 0.97 → 1.00 | −57% | — | −57% |
| B_auto | Claude | Haiku | 16 | 18.8% (3/16) | 68.8% (11/16) | +50.0 pp (+8) | 0.714 → 0.892 | 0.97 → 1.00 | −70% | −51% | −53% |
| B_auto | Claude | Sonnet | 15 | 53.3% (8/15) | 86.7% (13/15) | +33.3 pp (+5) | 0.917 → 0.968 | 0.99 → 1.00 | −72% | −56% | −76% |
| B_auto | Claude | Opus | 16 | 62.5% (10/16) | 87.5% (14/16) | +25.0 pp (+4) | 0.924 → 0.990 | 0.99 → 1.00 | −43% | −51% | −77% |
| B_auto | Codex | Luna | 16 | 81.2% (13/16) | 50.0% (8/16) | −31.2 pp (−5) | 0.974 → 0.881 | 0.99 → 0.99 | −17% | — | −35% |
| B_auto | Codex | Terra | 16 | 75.0% (12/16) | 37.5% (6/16) | −37.5 pp (−6) | 0.967 → 0.842 | 0.99 → 1.00 | +16% | — | −20% |
| B_auto | Codex | Sol | 16 | 56.2% (9/16) | 31.2% (5/16) | −25.0 pp (−4) | 0.914 → 0.830 | 0.97 → 0.99 | +1% | — | −31% |

**Every required-Skill cell on every model of both providers reports full recall**, and no baseline does. The binary and continuous columns disagree in one place worth naming: Claude's optional arm scores above its required arm on Sonnet and Opus, while Codex's optional arm collapses on all three strata — the same provider split the accuracy columns show, now visible in partial credit as well as pass/fail.

Two further evidence metrics are measured on every cell and left to the [benchmark results](../../benchmarks/README.md#results) rather than shown here: `EREC`, the same recall over the whole transcript instead of the final answer, which differs from `RREC` in one cell of the entire record, and `DEFF`, an exposure-hit count per command that compares within a model only. The recall absolutes are inflated by substring credit in the frozen scorer, so the arm-to-arm difference carries and the level does not.

One row per stratum, one execution per row. All three Codex rows come from a single 144-cell launch completed 2026-09-08 that ran the declared strata as sequential child studies. Six earlier Codex agentic executions of the same 16 tasks exist and are deliberately not listed — three Luna, one terra, one sol, plus the 2026-09-06 Luna run under the unrepaired prompt. They are separate studies, not repetitions, so they are neither averaged in nor shown as rivals; the [benchmark results](../../benchmarks/README.md#results) report every one of them in full.

**A later Codex run inverts the accuracy half of the Luna rows, and the efficiency half survives it.** On 2026-09-08 the same 16 tasks re-ran on the Luna stratum under prompts that state the answer population explicitly — how a module is named, which modules count as tests, that the module under analysis is excluded from its own importer list, what each dependency count counts, and how ties break in a ranking — instead of leaving those rules to be inferred.

The baseline then answers all sixteen tasks and the required-Skill arm twelve, so the `+6.2 pp` in the Luna row below reads `−25.0 pp` under the disambiguated prompt. Three tasks that every baseline had failed in every stratum of every earlier run all pass, which is why this is read as prompt disambiguation rather than run-to-run noise: a large part of the Codex accuracy gain in this table was the treatment arm compensating for ambiguity the baseline had to guess at.

The required arm's remaining misses are the agent mis-transcribing or mis-ordering answers the tool returned correctly, not wrong data from the tool. Every one of them was assembly work outside the query — counting a returned list by eye, subtracting one call from another, filtering a repository-wide ranking against a candidate set by hand — because ranking or counting *within a candidate set* had no query behind it.

Version 0.35.0 adds it (`central --among`, plus `importer_count` and `excluded_test_importer_count` on `rdeps`); those additions do not change the historical figures in this paragraph. The extended-suite snapshot later in this README includes observed use of these query capabilities, but does not isolate their individual causal effect. The other two strata were then re-run on byte-identical prompts and both follow: every baseline answers all sixteen tasks, the required arm thirteen and fifteen, so the `+12.5 pp` and `+31.2 pp` in the rows below read `−18.8 pp` and `−6.2 pp`.

Only part of the resource result carries across strata — output tokens fall 48–54% and wall-clock 23–42% in all three, while input tokens read −35%, +15%, and −8% and hold no direction. The table below stands as measured; treat every Codex accuracy figure in it as measured under prompts that did not state the answer population, and treat the token saving as an output-token and latency claim rather than an input-token one.

**The Codex rows and the Claude rows are no longer the same experiment revision.** Three things changed on the Codex side and only there: arm order is now cyclically balanced rather than fixed lexical, the required-arm contract accepts a standalone query through the injected `CODEMAP_BIN` variable *or* the exact installed launcher (the detector previously credited only the variable form, which is what made six terra cells look non-adherent), and the frozen treatment is Codemap 0.34.0 rather than 0.33.1. Read a Claude row against a Codex row as spanning that boundary.

`Sol` is the widest strict-arm gap the record holds — nine of sixteen cells correct in the baseline against fourteen with the Skill, at 57% fewer input tokens — and it is wide because that stratum's baseline did badly, not because its strict arm did unusually well. The strict arm lands on exactly 14 of 16 in all three strata while the baselines read 13, 12, and 9. Baseline performance is the volatile term: the same sol stratum answered 12 of 16 in its earlier execution and 9 here, on identical tasks.

Binary correctness is harsher than the semantic score the snapshots below report, so the large Haiku gains are movement from partially-right to exactly-right rather than from nothing. Every Codex row now pairs 16 of 16, with no cell dropped for a missing envelope, an incomplete answer, or a skipped required query — the first sweep in this record where the admission rule removes nothing.

Codex remains the only row set where the optional-use arm regresses, and it regresses in all eight of the executions run under the two revisions this table spans, converting one baseline failure across 48 optional-arm cells while losing sixteen baseline successes. That regression is a prompt effect rather than a property of the arm, and stating the answer population removes it in every stratum: one lost cell on the first re-run stratum, none at all on the second, and on the third a single cell whose answer was correct and which failed on a harness cleanup check rather than on its content.

Its token penalty does not reproduce (−17%, +16%, +1% here against +45% on the earlier sol run), so the accuracy direction is the finding and the token direction is not. Scored field by field, the optional arm's loss sits entirely in the two ordered/counted answers: it matches both other arms on every enumeration field (`production_importers` 0.991 against the baseline's 0.988 and the Skill arm's 1.000) and collapses on `ranking` (0.625 against 0.911 and 0.944) and `rdep_counts` (0.395 against 0.758 and 0.994).

It finds who imports what as well as anyone and then ranks them worse than the arm with no tool at all — because it ranks by whatever raw reverse-dependency counts its improvised loop returns, including tests and examples, while the question asks within the production importer set. The Skill arm reads its `Need → Query` routing table before its first query; the bare-CLI arm spends its first round-trips on `--help` and `doctor` and then improvises. Having the counts without the contract that says which counts is worse than having no counts.

A command-level replay of the earlier runs found no measurement fault behind the token side either: the optional arm is additive, querying the index and then exploring by hand anyway, so it drags 19.0k tokens of command output into each cell against the baseline's 7.1k. The required arm does the opposite — more commands than the baseline, but each returning a median 140–184 tokens against a grep's ~1,100, so it ends up reading less overall.

Agentic elapsed figures stay cache-exposed: the Codex arm order is now balanced, the Claude one is not, and neither resets the provider cache between cells. Full cohort, estimator, and caveat detail: [benchmark results](../../benchmarks/README.md#results).

<a id="codex-structural-2026-08-07"></a> <a id="claude-structural-2026-09-06"></a>

### Extended-suite agentic snapshot

**More accurate answers at lower cost are demonstrated—not just fewer tokens.** In the latest Claude Haiku study, optional Codemap raises semantic credit from **77.9% to 97.3%**, with exactly correct answers increasing from **6/20 to 18/20**, while whole-arm gross input falls **53.6%** and cost falls **46.4%** ($4.77 → $2.56). Opus gains **3.61 percentage points** of semantic credit with either treatment. These are observed within-model improvements, not guarantees for every model or task.

Six separate model studies ran on 2026-09-08–09: 20 graph-query tasks × three arms × one repetition, 360 cells total. The extended tasks test deeper enumeration and ranking, not behavioral change-impact correctness. Different provider manifests and treatment delivery prevent pooling; earlier snapshots remain historical.

Semantic credit below means raw graded answer credit summed over all 20 assigned tasks, with unavailable answers contributing zero, **before treatment-compliance gating**. Exact counts are separately shown for admitted results, which require correct answers, valid formatting, complete execution, no contamination, and treatment adherence.

| Provider | Model | Plain semantic credit | Optional semantic credit | Required semantic credit | Admitted exact A / B / C |
| -- | -- | -: | -: | -: | -- |
| Claude | Opus | 96.3% | 99.9% | 99.9% | 18 / 19 / 19 |
| Claude | Sonnet | 99.2% | 97.9% | 97.6% | 18 / 18 / 14 |
| Claude | Haiku | 77.9% | 97.3% | 94.0% | 6 / 18 / 6 |
| Codex | Luna | 100.0% | 99.9% | 94.9% | 20 / 18 / 18 |
| Codex | Terra | 95.0% | 100.0% | 99.9% | 19 / 20 / 19 |
| Codex | Sol | 85.0% | 100.0% | 99.9% | 17 / 20 / 18 |

Exact counts are out of 20; A = plain, B = optional, C = required Skill. Claude semantic exact counts differ from admitted exact for Sonnet required (17 versus 14) and Haiku required (14 versus 6). Haiku plain has 19 scored answers; Codex Luna required has 19, Terra plain 19, and Sol plain 17; all other arms have 20. All scored Codex plain answers are semantically exact, so higher all-assigned treatment credit there reflects answer availability, not demonstrated semantic improvement among scored answers.

**The efficiency benefit is real but model-dependent.** On canonical pairs where both answers pass exactly, including admission checks, required Codemap reduces median output tokens and elapsed time across all three Codex strata:

| Codex model | Both-exact pairs | Output tokens | Elapsed time | Gross input |
| -- | -: | -: | -: | -: |
| Luna | 18 | −15.2% | −9.1% | +3.8% |
| Terra | 18 | −41.2% | −33.5% | +26.0% |
| Sol | 15 | −19.8% | −15.7% | +4.5% |

These are medians of paired percentage changes, not whole-arm savings. Gross input increases in each required Codex stratum. Claude optional also shortens correct-answer latency, but Opus total cost increases despite its modest accuracy gain. Claude's recorded “fresh” residue excludes cache creation and is not comparable with Codex fresh input.

**The remaining opportunity is reliable delivery, not a need to invent an accuracy gain.** Sonnet's near-perfect plain arm retains a small semantic advantage. Lower Claude tiers sometimes omit required compact syntax or use command forms whose successful results are not recognized; eight of Haiku's fourteen strict violations still contain exactly correct answers. Thus its 94.0% raw semantic credit and 30.0% admitted quality describe different properties. They must not be collapsed into either “the answers were wrong” or “the strict integration works.”

Regressions also include real missing facts, count/ranking mistakes, and Codex execution/formatting failures. Two product issues remain actionable: filtered centrality output can fall back to an unfiltered count for a module with only test importers, and the deep-ranking naming disagreement is closed on the oracle side: a real scan names a package outside a configured source root from its own package root, which is the only name that resolves at runtime, and the benchmark oracle now derives module names by the same rule instead of by repository-relative path.

The large earlier optional Codex collapse is not reproduced, but Luna still loses two exactly correct tasks. Working-tree fixes require prospective validation; no historical score is rewritten.

**Bottom line:** this run demonstrates a substantial semantic-accuracy and cost benefit for Haiku optional, a smaller semantic gain for Opus, and consistent correct-answer output/latency reductions for Codex required. One repetition on one graph-centric repository limits generalization; selective routing and dependable query delivery remain important.

Full per-cell evidence, canonical paired medians, and regression distinctions: [benchmark results](../../benchmarks/README.md#results).

### Structural navigation snapshot

On 2026-09-06, three Claude tiers ran 55 structural tasks across all three arms, 495 cells in one repetition against a frozen repository revision and a prebuilt index. `A_plain` is the no-Codemap baseline, `B_auto` makes Codemap optional, and `C_strict` requires the installed Skill. Accuracy is paired: each percentage pair is computed only over the tasks where both arms of that pair produced a scored, parsed answer, so both figures share one denominator, shown in parentheses as cells answered correctly. Gain is treatment minus baseline in percentage points, with the cell delta beside it.

Input tokens, cost, and elapsed are per-task medians of treatment ÷ baseline restated as change against the baseline: negative means Codemap needed less.

| Tier | Pair | Paired n | Baseline accuracy | Treatment accuracy | Gain | Input tokens | Cost | Elapsed |
| -- | -- | -: | -: | -: | -: | -: | -: | -: |
| Haiku | A_plain vs C_strict | 47 | 74.5% (35/47) | 87.2% (41/47) | +12.7 pp (+6) | −65% | −71% | −54% |
| Haiku | A_plain vs B_auto | 48 | 75.0% (36/48) | 85.4% (41/48) | +10.4 pp (+5) | −48% | −52% | −40% |
| Sonnet | A_plain vs C_strict | 50 | 84.0% (42/50) | 92.0% (46/50) | +8.0 pp (+4) | −52% | −40% | −46% |
| Sonnet | A_plain vs B_auto | 49 | 83.7% (41/49) | 95.9% (47/49) | +12.2 pp (+6) | −52% | −43% | −48% |
| Opus | A_plain vs C_strict | 50 | 88.0% (44/50) | 96.0% (48/50) | +8.0 pp (+4) | −21% | −11% | −24% |
| Opus | A_plain vs B_auto | 51 | 88.2% (45/51) | 94.1% (48/51) | +5.9 pp (+3) | −23% | −23% | −25% |

The lift is largest where unaided navigation is weakest and narrows as the tier strengthens, while the efficiency gap persists at every tier. On the safety-grade view — caller-enumeration answers with recall at or above 0.90 — the weakest tier moves from 10/14 unaided to 14/14 under the required Skill; the two stronger tiers already reach 14/14 in every arm.

This run is descriptive and non-poolable: one repetition, one repository revision, one prebuilt index, and it retains contaminated, incomplete, and extraction-failed cells rather than filtering them. Twelve cells failed answer extraction and two baseline cells were dropped as contaminated, which is why the paired denominators vary. Two task defects found after the first execution were corrected and their cells re-measured; a third finding is a real gap in the tool's uncovered-symbol counting and is still scored as a miss rather than explained away.

See the [canonical structural result and limitations](../../benchmarks/results-archive/2026-09-06-claude-structural.md).

<a id="codex-agentic-2026-08-07"></a>

### Agentic navigation snapshot — 2026-09-06

The same three tiers completed 144 cells across 16 shared import-graph tasks and the same three arms. Values are per-task median savings against `A_plain`; positive means the arm needed less.

| Tier | Arm | Elapsed | Cost | Input tokens | Tool calls |
| -- | -- | -: | -: | -: | -: |
| Haiku | C_strict | 46% | 45% | 60% | 62% |
| Haiku | B_auto | 53% | 51% | 70% | 68% |
| Sonnet | C_strict | 82% | 65% | 77% | 72% |
| Sonnet | B_auto | 76% | 56% | 72% | 62% |
| Opus | C_strict | 73% | 41% | 50% | 52% |
| Opus | B_auto | 77% | 51% | 43% | 59% |

Evidence recall holds at parity or better in every Codemap cell, while the baseline drops below it on four tasks — three in both fields (BA-03, BA-12, BA-15) and one, Sonnet's BA-07, in the final report alone, where the module was surfaced during the run and left out of the answer. Time spent inside tools moves the other way — an index call costs more than a single grep — so the saving comes from needing far fewer calls, not from faster ones.

This study is exploratory and non-poolable for the same reasons as the structural one, plus fixed arm order and provider-cache exposure; one baseline cell hit its coordinate timeout and is excluded rather than scored as a loss. See the [canonical agentic result and measurement caveats](../../benchmarks/results-archive/2026-09-06-claude-agentic.md).

<a id="codex-structural-2026-09-06"></a> <a id="codex-agentic-2026-09-06"></a>

### Codex navigation snapshot — 2026-09-06

This is the first of three Codex strata measured; `gpt-5.6-sol` and `gpt-5.6-terra` have their own snapshots below. One `gpt-5.6-luna` study ran the same shared task contracts through the Codex CLI: 219 structural-family cells across all three arms, plus 48 agentic cells. Structural accuracy below is paired over the 45-task headline cohort, on the same denominator rule as the Claude table.

This run's `B_auto` arm was executed under a prompt that required a Codemap query; the contract has since changed to optional-use to match Claude's `B_auto`, so a future Codex `B_auto` run must not be blended with these numbers.

| Model | Pair | Paired n | Baseline accuracy | Treatment accuracy | Gain | Cells correct | Input tokens | Elapsed |
| -- | -- | -: | -: | -: | -: | -: | -: | -: |
| Luna | A_plain vs C_strict | 43 | 93.0% | 98.9% | +6.0 pp | 38 → 42 | −53% | −51% |
| Luna | A_plain vs B_auto | 44 | 91.3% | 97.4% | +6.1 pp | 38 → 41 | −42% | −42% |

Codex accuracy is the mean semantic quality score over the paired cells rather than a pass count, so the percentage and the correct-cell count move independently. Its token and elapsed figures are cohort totals restated as change against the baseline, where the Claude table above uses per-task medians.

The four executable and extraction stages stay separate and nonpoolable. ReadCrop matches the Claude result — equal correctness, 37% less input under the required Skill. Fix-Single and Fix-Multi are the counterexample: identical correctness at 45% and 29% more input than the control, so a fully localized edit with no unresolved structural fact is not a Codemap workload. Patch is 5/5 unaided and 4/5 under the strict arm, the single loss being a wrong fix rather than a tooling failure.

Agentically, the strict arm scores 0.960 against the control's 0.929 at 48% less input, answering 13 of 16 cells correctly against the control's 9, while the optional-use canary regresses to 0.860 and 6 of 16. Two defects in the measurement harness, both costing the treatment arms, were found in this run and are fixed prospectively rather than rescored. See the [canonical Codex result and limitations](../../benchmarks/results-archive/2026-09-06-codex-structural.md).

### Codex `gpt-5.6-sol` snapshot — 2026-09-07

A second Codex stratum ran the same shared task contracts: 219 structural-family cells over 73 tasks in five stages, all three arms, one repetition per cell, Codex CLI 0.153.4 at high reasoning effort, against the same frozen repository revision and index as every other table here. Its `A_plain` and `C_strict` arm contracts are byte-identical to the `gpt-5.6-luna` run's; its `B_auto` contract is the newer optional-use one and is not the contract Luna's `B_auto` ran under. The observed CLI build is 0.153.4 against a reviewed 0.146.1, so this ran on a build the methodology had not reviewed.

**Read this as one stratum, not as a Codex result.** The sibling `gpt-5.6-terra` stratum of the same launch ran no paid cell — its launcher refused on a paid-approval token before the first model call — so this launch is half complete. Terra was relaunched and completed on 2026-09-07; that is a separate study with its own snapshot below, never a second half of this one.

Structural accuracy is in the cross-provider table above. On the run's own semantic-quality metric, over the 44 tasks where all three arms produced an admissible scored cell, mean quality is 0.9477 unaided, 0.9889 optional, and 0.9742 strict, with a median of exactly 1.000 in every arm and 36 of the 44 tasks scoring identically in all three. The strict arm is cheaper on 38 of those 44 tasks.

Because the control is already saturated, this design can detect degradation and cannot detect improvement, and the quality effect is indistinguishable from zero at one repetition per cell with no significance testing.

Per-task spread separates the two treatment arms where the medians do not. Against the unaided baseline, the input-token ratio for the strict arm runs 0.254–1.066 between the 10th and 90th percentiles and is cheaper on 38 of 44 tasks; the optional arm runs 0.535–1.951 and is cheaper on 19 of 44, with a worst task at 2.74× the baseline. The two distributions are about equally wide — the difference is that the strict arm's sits below parity while the optional arm's straddles it.

Uptake does not explain the gap: 26 of the 44 optional cells did query Codemap, and their median ratio (0.974×) is barely below the cells that did not (1.016×), because the optional arm queries Codemap in addition to exploring the repository the ordinary way.

The four executable and extraction stages stay separate and must not be averaged together — their token direction is not consistent:

| Stage | Cells | A_plain correct | B_auto correct | C_strict correct | B tokens | C tokens |
| -- | -: | -- | -- | -- | -: | -: |
| ReadCrop | 18 | 5/6 | 5/6 | 5/6 | +60% | −34% |
| Fix-Single | 12 | 4/4 | 4/4 | 4/4 | +16% | +15% |
| Fix-Multi | 9 | 3/3 | 3/3 | 3/3 | −15% | +34% |
| Patch | 15 | 5/5 | 5/5 | 4/5 | −36% | −67% |

Token columns are cohort totals against `A_plain`. Requiring Codemap costs 15% more input on single-file edits and 34% more on multi-file edits for identical correctness, which is the same counterexample the Luna run produced and the reason production guidance skips Codemap for a fully localized edit with no unresolved structural fact. The single Patch loss is the strict arm failing a cell both other arms passed, at a quarter of their input — one task at one repetition supports no claim either way, but it is not evidence that requiring the tool is safer.

The optional arm abandons the tool wherever code has to be modified: uptake is 5 of 6 ReadCrop cells and 35 of 55 structural cells, but 0 of 12 across Fix-Single, Fix-Multi, and Patch. An optional rollout should not be expected to reproduce the strict-arm result on editing work.

Delivery is confounded with arm — the optional arm always used the direct CLI and the strict arm always the installed Skill — so no optional-versus-strict difference can be attributed to strictness alone. Full cohort, estimator, and caveat detail is in the benchmark record's `gpt-5.6-sol` Codex section.

### Codex `gpt-5.6-terra` snapshot — 2026-09-07

The third Codex stratum ran the same shared task contracts: 219 structural-family cells over 73 tasks in five stages, all three arms, one repetition per cell, Codex CLI 0.153.4 at high reasoning effort, against the same frozen repository revision and index as every other table here. All three arm contracts are byte-identical to the `gpt-5.6-sol` run's, so this is the second study under the optional-use `B_auto` contract.

As on Sol, the observed CLI build is 0.153.4 against a reviewed 0.146.1. This is the stratum whose first launch was refused on a paid-approval token; the relaunch is a separate study, not the missing half of the Sol launch.

Structural accuracy is in the cross-provider table above. On the run's own semantic-quality metric over the headline cohort, mean quality is 91.6% unaided against 97.2% strict over 40 pairs, and 91.8% against 98.4% optional over 41 pairs. **Neither treatment arm turns a correct answer wrong**: the strict arm converts DI-01 and DI-02, the optional arm those two plus DI-06, and the only quality regression anywhere is BR-07 at −0.056 in both arms.

The paired counts are 40 and 41 rather than 45 because four control cells were lost — `CQ-01` left a command item open, `FT-05` and `DI-05` exited non-zero with zero tokens, and `FT-03` failed answer extraction — plus one strict cell dropped for non-adherence. Every lost control cell is one the unaided arm failed to complete, so removing them raises the baseline and makes the gains above conservative.

| Stage | Cells | A_plain correct | B_auto correct | C_strict correct | B tokens | C tokens |
| -- | -: | -- | -- | -- | -: | -: |
| ReadCrop | 18 | 5/6 | 6/6 | 6/6 | +101% | +10% |
| Fix-Single | 12 | 4/4 | 4/4 | 4/4 | −12% | +3% |
| Fix-Multi | 9 | 2/3 | 3/3 | 3/3 | −5% | −2% |
| Patch | 15 | 3/5 | 3/5 | 4/5 | −20% | −9% |

Token columns are cohort totals against `A_plain`. ReadCrop inverts here — the strict arm reads 10% more where Luna and Sol read 37% and 34% less — while converting the one cell the control got wrong. Editing stages sit near parity in both directions and support no claim at 3 to 5 tasks each. The one Patch task solved only under the strict arm, and the one failed by all three arms, are single observations that run opposite to Sol's single Patch loss.

Optional uptake repeats the Sol pattern exactly: 4 of 6 ReadCrop cells, 48 of 55 structural cells, and 0 of 12 across the three editing stages. Delivery is again confounded with arm, and `skill_delivery_observed` is false in all 165 structural cells. Full cohort, estimator, and caveat detail is in the benchmark record's `gpt-5.6-terra` Codex section.
