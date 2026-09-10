# 🗂️ codemap-py — structural answers for Python codebases

codemap-py builds a local, static index of a Python project so maintainers can answer "what imports this?", "what calls this function?", "which tests are likely affected?", and "where is the highest coupling?" before changing code. It is useful when a task has unresolved structural scope; a fully localized edit with no such question should skip it.

The package ships the same six skills for Claude Code and Codex: scan the project, query the index, find affected tests, rename references, inspect integration, and debrief Claude telemetry. The runtime adapters share the capability contract but keep their host-specific invocation and path rules.

Consumer plugins ship synchronized copies of the shared context and gate guidance; they never load private files from another installed plugin. The reference context batch resolves its default index from the repository root, including when invoked from a subdirectory.

<details>
<summary><strong>Contents</strong></summary>

- [Quick start](#-quick-start)
- [What it solves](#-what-it-solves)
- [Adaptive use](#-adaptive-use)
- [Prerequisites and supported runtimes](#-prerequisites-and-supported-runtimes)
- [Build and query the index](#-build-and-query-the-index)
- [Honest limits](#-honest-limits)
- [Benchmark evidence](#-benchmark-evidence)
- [Six skills](#-six-skills)
- [Integration with other plugins](#-integration-with-other-plugins)
- [Configuration](#-configuration)
- [Compatibility and exit codes](#-compatibility-and-exit-codes)
- [Upgrade, uninstall, and migration](#-upgrade-uninstall-and-migration)
- [Maintainer documentation](#-maintainer-documentation)
- [Contributing and feedback](#-contributing-and-feedback)

</details>

## ⚡ Quick start

Install the plugin in the runtime you use:

```bash
# Claude Code
claude plugin marketplace add Borda/AI-Rig
claude plugin install codemap-py@borda-ai-rig

# OpenAI Codex
codex plugin marketplace add Borda/AI-Rig
codex plugin add codemap-py@borda-ai-rig
```

Start a fresh runtime session after installation. Build an index, then ask the first useful structural question:

```text
# Claude Code
/codemap-py:scan-codebase
/codemap-py:query-code rdeps mypackage.auth

# Codex
$codemap-py:scan-codebase
$codemap-py:query-code rdeps mypackage.auth
```

The direct CLI is also available to a project checkout or to Claude's installed `bin/` PATH. Its first query has the same shape:

```bash
codemap-py index
codemap-py query --compact rdeps mypackage.auth
```

From a source checkout, call the Python entrypoint instead:

```bash
python plugins/codemap-py/scripts/codemap_py_entry.py index
python plugins/codemap-py/scripts/codemap_py_entry.py query --compact rdeps mypackage.auth
python plugins/codemap-py/scripts/codemap_py_entry.py doctor --json
```

**Windows uses this Python entrypoint directly** — `bin/codemap-py` is a `#!/bin/sh` launcher and does not execute there; `bin/codemap-py.cmd` is the installed-package equivalent. macOS and Linux may use either form.

Codex does not add the plugin's `bin/` directory to PATH; use the `$codemap-py:*` skills or resolve the installed plugin root as their runtime instructions describe.

## 🎯 What it solves

Without a structural index, a refactor often starts with repeated file searches to discover importers, callers, and tests. That exploration can miss a reverse dependency or spend time reading files that do not answer the question. codemap-py makes those relationships queryable from one local JSON index and gives each result coverage and freshness metadata.

The index is not a replacement for reading source or running tests. It is a narrow, fast source of structural evidence that helps choose the next inspection or verification step.

## 🔗 Adaptive use

Use the smallest route that answers the unresolved question:

| Question                                                    | Route                                                        |
| ----------------------------------------------------------- | ------------------------------------------------------------ |
| Exact file and symbol are known; no structural fact remains | Skip codemap-py and edit or inspect directly                 |
| Which modules import a module?                              | `rdeps <module>`                                             |
| Which modules does it import?                               | `deps <module>`                                              |
| Which production functions directly call a function?        | `fn-rdeps <module::symbol> --exclude-tests`                  |
| Which functions transitively depend on it?                  | `fn-blast <module::symbol>`                                  |
| Which modules have the highest reverse-dependency count?    | `central --top N --exclude-tests`                            |
| What source slice and imports define a symbol?              | `symbol <name> --with-imports`                               |
| Which tests are structurally affected?                      | `/codemap-py:test-impact` or `$codemap-py:test-impact`       |
| Is the integration wiring and runtime evidence healthy?     | `/codemap-py:integration audit` or `$codemap-py:integration` |

For an explicit request for structural context, query even when an edit looks small. A lifecycle boundary such as a callback, hook, cancellation path, cleanup path, or state transfer also needs source and the named test or oracle; a complete structural result does not prove runtime behavior.

Resolve the installed launcher once and retain its literal for the workflow. Independent standalone read-only queries may run concurrently only against a prepared stable index with self-heal disabled (`SCAN_NO_AUTOBUILD=1`); dependent queries wait, and refresh/self-heal/index writes are serial. There is no arbitrary total-query cap for facts required to finish; only targeted correction retries are bounded and recurring correction failures stop with an explicit gap. A complete untruncated result settles its own fact, not sibling dimensions, and an independent AST/oracle read remains valid.

`rdeps` and `deps` answer opposite directions. Query names and paths are relative to the project being queried, not the installed plugin. After a custom-root scan, retain the emitted index path and query with `--index <emitted-index-path> --root <same-root>`: `--root` controls path resolution only and does not select the index.

`fn-rdeps` reports incoming call edges; it does not discover inheritance or same-name override relationships. Use `find-symbol '<ClassSuffix>\.<method>$' --exclude-tests --limit 0` to gather same-name override candidates, then verify ancestry and package boundaries in source.

## ✅ Prerequisites and supported runtimes

- Claude Code or Codex, depending on the runtime you are installing into.
- CPython `>=3.11,<3.15` for the shipped dispatcher and launchers. The CLI checks this before importing the package and exits `127` when no eligible interpreter is available. Set `CODEMAP_PYTHON` to an eligible interpreter when PATH discovery is not enough.
- Git is recommended for branch-aware freshness checks and incremental rebuilds. Outside Git, content hashes provide a fallback.
- The core scanner and query engine use the Python standard library. `coverage>=7.4` is optional and is needed only for `scan-index --with-coverage` and the `coverage`/`coverage-gap` queries.

The scanner is intended for Python projects. It parses `.py` and `.pyi` files with `ast.parse`; a `.py` implementation takes precedence over a sibling `.pyi`, while a stub without an implementation contributes declarations and imports but no call edges. It also records selected Sphinx references from `.rst` files, `docs/**/*.md`, and supported root configuration files for cross-reference and freshness checks. It does not index TypeScript, Go, Rust, or other non-Python source as Python modules.

## 🗂️ Build and query the index

The canonical CLI is:

```text
codemap-py index [--root PATH] [--incremental] [--with-coverage PATH] [--timeout N]
codemap-py query [global flags] <subcommand> ...
codemap-py doctor [--json]
codemap-py integrate {audit,plan,apply,sync,demo} ...
```

`--incremental` requires an existing v3-or-newer index. `--with-coverage` reads a `.coverage` SQLite file when the optional dependency is available. Query help is authoritative for the complete subcommand and flag list; the current groups include module imports (`deps`, `rdeps`, `central`, `coupled`, `path`), symbol lookup (`symbol`, `symbols`, `find-symbol`), call graphs (`fn-deps`, `fn-rdeps`, `fn-central`, `fn-blast`), test/mock/fixture/subprocess relationships, documentation and coverage checks, dead-code checks, `diff-impact`, and `batch`.

Most query results include an `index` block. Read it before treating a list as final:

- `stale` reports that the index may no longer describe the working tree.
- `query_complete` describes graph coverage for the queried direction; it does not mean a display list was not truncated.
- `confidence`, `truncated`, and `total_available` describe result completeness. The default display limit is bounded for several list commands; use `--limit 0` where that command supports it when the full set matters.
- `not_covered` names static-analysis blind spots and should remain in the final reasoning.

Queries check freshness and may perform a bounded incremental self-heal unless `SCAN_NO_AUTOBUILD=1` disables query-time writes. An explicit scan is the predictable choice after a clone, a large change, a branch switch, or when a query reports stale/degraded coverage.

## 🧭 Honest limits

The graph is static AST evidence. It can miss dynamic dispatch, hook and callback registration, string-based dispatch, `getattr` lookups, `importlib.import_module`, `__import__`, and lazy-loading patterns. Import and call results therefore do not establish runtime behavior, external consumers, test pass status, or inheritance correctness. `rename-refs` calls out dynamic references, cross-repository callers, ABC/Protocol overrides, and caller lists above its edit cap as manual review items.

Files that fail to parse are marked degraded rather than silently treated as complete. Untracked files, root mismatches, collisions, stale hashes, and list caps can all reduce confidence. Review the returned coverage metadata and inspect the relevant source and tests before making a behavior or deletion decision.

Possible future work includes broader dynamic-behavior evidence, deeper cross-language support, and richer freshness diagnostics. Those are opportunities rather than promises; the current contract remains static Python analysis with explicit coverage metadata.

Claude's optional Python hooks provide ambient index status, session-sharded telemetry, skill-start records, and a narrow redundant-import-grep guard. Codex ships hooks for session seeding, ambient preamble/guard behavior, and runtime-scoped tool records, while its host does not provide a Codemap skill-start hook. The hooks suppress expected parsing and filesystem failures and are not required for indexing or querying; this is not a guarantee against every malformed event. Skill/tool logging expects mapping-shaped `tool_input` when that field is truthy. The runtime difference is an evidence boundary, not a query-engine capability difference.

Performance and token use vary with repository size, model, index freshness, query choice, and whether an agent continues exploring after a result. Historical benchmark runs are exploratory and repository/model-specific; they do not establish universal savings or quality guarantees. See the [benchmark record](https://github.com/Borda/AI-Rig/blob/main/benchmarks/README.md) for methods and caveats.

## 📈 Benchmark evidence

The benchmark record gives Codemap a measurable, bounded value proposition: on structural questions with unresolved dependency or caller scope, a required Codemap Skill reduced context and elapsed time in every model stratum measured so far, and improved the aggregate semantic answer score in five of the six. The sixth, the 2026-09-07 Codex `gpt-5.6-sol` stratum, is a quality tie against a control already at a median score of 1.000 — the cost reduction reproduced there and the quality gain did not. Those are historical scored-cell summaries, not an all-assigned pass rate; the record retains unfavorable and unobserved cells and explains when adaptive routing should skip Codemap.

The latest saved agentic reports use `reporting_version=agentic-graded-v2`. Their `quality` averages graded credit over all assigned cells and zeroes execution, formatting, contamination, treatment, and unobserved failures; `exact_pass` separately requires complete correctness and admission. The extended-suite snapshot below also exposes raw semantic credit so compliance failures do not masquerade as content errors. Legacy `component` means remain secondary diagnostics, not interchangeable with graded credit. Canonical efficiency compares only pairs where both cells pass exactly; improvements, regressions, both-fail outcomes, and unavailable usage remain separate. Historical snapshots retain their original contracts and are not pooled with the latest runs.

<!-- Related result tables — a figure here restates a run measured in the benchmark suite. Editing one means checking:
  - plugins/codemap-py/README.md § All models on one cohort (both tables below)
  - plugins/codemap-py/README.md § Structural navigation snapshot, § Agentic navigation snapshot, § Codex navigation snapshot, § Codex gpt-5.6-sol snapshot, § Codex gpt-5.6-terra snapshot, § Extended-suite agentic snapshot
  - benchmarks/README.md § Results — structural and agentic lanes, per-provider views included
  Figures are recomputed from immutable artifacts under benchmarks/results/; a corrected number means a new run. -->

### All models on one cohort

Measured 2026-09-06. Both providers and every model stratum measured on one cohort, one metric, and one estimator: the shared task cohort, a cell counted correct or not, and per-task medians of treatment ÷ baseline restated as change against the baseline. The per-provider snapshots below keep their own canonical scoring and therefore differ.

Structural, on the 45-task headline cohort both providers share:

| Treatment arm | Provider | Model  | Paired n | Baseline accuracy | Treatment accuracy |          Gain | Input tokens | Cost | Elapsed |
| ------------- | -------- | ------ | -------: | ----------------: | -----------------: | ------------: | -----------: | ---: | ------: |
| C_strict      | Claude   | Haiku  |       39 |     71.8% (28/39) |      87.2% (34/39) | +15.4 pp (+6) |         −80% | −75% |    −74% |
| C_strict      | Claude   | Sonnet |       42 |     83.3% (35/42) |      92.9% (39/42) |  +9.5 pp (+4) |         −57% | −52% |    −53% |
| C_strict      | Claude   | Opus   |       43 |     88.4% (38/43) |      97.7% (42/43) |  +9.3 pp (+4) |         −19% | −23% |    −31% |
| C_strict      | Codex    | Luna   |       43 |     88.4% (38/43) |      97.7% (42/43) |  +9.3 pp (+4) |         −29% |    — |    −36% |
| C_strict      | Codex    | Terra  |       40 |     87.5% (35/40) |      92.5% (37/40) |  +5.0 pp (+2) |         −42% |    — |    −51% |
| C_strict      | Codex    | Sol    |       45 |     93.3% (42/45) |      93.3% (42/45) |   0.0 pp (+0) |         −51% |    — |    −49% |
| B_auto        | Claude   | Haiku  |       42 |     73.8% (31/42) |      85.7% (36/42) | +11.9 pp (+5) |         −49% | −58% |    −56% |
| B_auto        | Claude   | Sonnet |       42 |     83.3% (35/42) |      97.6% (41/42) | +14.3 pp (+6) |         −59% | −44% |    −50% |
| B_auto        | Claude   | Opus   |       44 |     88.6% (39/44) |      95.5% (42/44) |  +6.8 pp (+3) |         −30% | −28% |    −31% |
| B_auto        | Codex    | Luna   |       44 |     86.4% (38/44) |      93.2% (41/44) |  +6.8 pp (+3) |         −33% |    — |    −27% |
| B_auto        | Codex    | Terra  |       41 |     87.8% (36/41) |      95.1% (39/41) |  +7.3 pp (+3) |          −5% |    — |    −20% |
| B_auto        | Codex    | Sol    |       44 |     93.2% (41/44) |      97.7% (43/44) |  +4.5 pp (+2) |          +1% |    — |    −13% |

The six `C` rows are directly comparable — every one requires the installed Skill, and the three Codex rows carry byte-identical `A_plain` and `C_strict` arm contracts. Five of them improve accuracy while reading less. `Sol` `C_strict` is the exception: it answers exactly the same 42 of 45 cells as its own baseline for 51% fewer gross input tokens, a cost win and a quality tie against a baseline already at a median score of 1.000. `Terra` `C_strict` converts two cells and loses none.

A median cannot say whether an arm saves reliably or saves because a few tasks saved enormously, so the same per-task token ratios are restated below as distributions over the same pairs.

| Arm      | Provider | Model  | Paired n | Median | Geo mean |     p10–p90 | sd(log) | Cheaper in |
| -------- | -------- | ------ | -------: | -----: | -------: | ----------: | ------: | ---------: |
| C_strict | Claude   | Haiku  |       39 | 0.204× |   0.215× | 0.031–1.384 |   1.461 |      32/39 |
| C_strict | Claude   | Sonnet |       42 | 0.431× |   0.398× | 0.112–1.184 |   0.984 |      32/42 |
| C_strict | Claude   | Opus   |       43 | 0.806× |   0.697× | 0.304–1.282 |   0.606 |      26/43 |
| C_strict | Codex    | Luna   |       43 | 0.711× |   0.566× | 0.176–1.505 |   0.962 |      29/43 |
| C_strict | Codex    | Terra  |       40 | 0.581× |   0.565× | 0.308–1.125 |   0.618 |      33/40 |
| C_strict | Codex    | Sol    |       45 | 0.486× |   0.496× | 0.258–1.066 |   0.596 |      39/45 |
| B_auto   | Claude   | Haiku  |       42 | 0.508× |   0.286× | 0.033–1.175 |   1.462 |      35/42 |
| B_auto   | Claude   | Sonnet |       42 | 0.412× |   0.407× | 0.104–1.183 |   1.143 |      31/42 |
| B_auto   | Claude   | Opus   |       44 | 0.700× |   0.697× | 0.322–1.256 |   0.603 |      27/44 |
| B_auto   | Codex    | Luna   |       44 | 0.667× |   0.578× | 0.187–1.292 |   0.749 |      34/44 |
| B_auto   | Codex    | Terra  |       41 | 0.947× |   0.888× | 0.434–1.439 |   0.482 |      23/41 |
| B_auto   | Codex    | Sol    |       44 | 1.014× |   0.991× | 0.535–1.951 |   0.555 |      19/44 |

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

| Treatment arm | Provider | Model  | Paired n | Baseline accuracy | Treatment accuracy |          Gain |         Score |        RREC | Input tokens | Cost | Elapsed |
| ------------- | -------- | ------ | -------: | ----------------: | -----------------: | ------------: | ------------: | ----------: | -----------: | ---: | ------: |
| C_strict      | Claude   | Haiku  |       16 |      18.8% (3/16) |      68.8% (11/16) | +50.0 pp (+8) | 0.714 → 0.936 | 0.97 → 1.00 |         −60% | −45% |    −46% |
| C_strict      | Claude   | Sonnet |       15 |      53.3% (8/15) |      73.3% (11/15) | +20.0 pp (+3) | 0.917 → 0.946 | 0.99 → 1.00 |         −77% | −65% |    −82% |
| C_strict      | Claude   | Opus   |       16 |     62.5% (10/16) |      68.8% (11/16) |  +6.2 pp (+1) | 0.924 → 0.940 | 0.99 → 1.00 |         −50% | −41% |    −73% |
| C_strict      | Codex    | Luna   |       16 |     81.2% (13/16) |      87.5% (14/16) |  +6.2 pp (+1) | 0.974 → 0.990 | 0.99 → 1.00 |         −45% |    — |    −44% |
| C_strict      | Codex    | Terra  |       16 |     75.0% (12/16) |      87.5% (14/16) | +12.5 pp (+2) | 0.967 → 0.973 | 0.99 → 1.00 |         −29% |    — |    −29% |
| C_strict      | Codex    | Sol    |       16 |      56.2% (9/16) |      87.5% (14/16) | +31.2 pp (+5) | 0.914 → 0.994 | 0.97 → 1.00 |         −57% |    — |    −57% |
| B_auto        | Claude   | Haiku  |       16 |      18.8% (3/16) |      68.8% (11/16) | +50.0 pp (+8) | 0.714 → 0.892 | 0.97 → 1.00 |         −70% | −51% |    −53% |
| B_auto        | Claude   | Sonnet |       15 |      53.3% (8/15) |      86.7% (13/15) | +33.3 pp (+5) | 0.917 → 0.968 | 0.99 → 1.00 |         −72% | −56% |    −76% |
| B_auto        | Claude   | Opus   |       16 |     62.5% (10/16) |      87.5% (14/16) | +25.0 pp (+4) | 0.924 → 0.990 | 0.99 → 1.00 |         −43% | −51% |    −77% |
| B_auto        | Codex    | Luna   |       16 |     81.2% (13/16) |       50.0% (8/16) | −31.2 pp (−5) | 0.974 → 0.881 | 0.99 → 0.99 |         −17% |    — |    −35% |
| B_auto        | Codex    | Terra  |       16 |     75.0% (12/16) |       37.5% (6/16) | −37.5 pp (−6) | 0.967 → 0.842 | 0.99 → 1.00 |         +16% |    — |    −20% |
| B_auto        | Codex    | Sol    |       16 |      56.2% (9/16) |       31.2% (5/16) | −25.0 pp (−4) | 0.914 → 0.830 | 0.97 → 0.99 |          +1% |    — |    −31% |

**Every required-Skill cell on every model of both providers reports full recall**, and no baseline does. The binary and continuous columns disagree in one place worth naming: Claude's optional arm scores above its required arm on Sonnet and Opus, while Codex's optional arm collapses on all three strata — the same provider split the accuracy columns show, now visible in partial credit as well as pass/fail. Two further evidence metrics are measured on every cell and left to the [benchmark results](https://github.com/Borda/AI-Rig/blob/main/benchmarks/README.md#results) rather than shown here: `EREC`, the same recall over the whole transcript instead of the final answer, which differs from `RREC` in one cell of the entire record, and `DEFF`, an exposure-hit count per command that compares within a model only. The recall absolutes are inflated by substring credit in the frozen scorer, so the arm-to-arm difference carries and the level does not.

One row per stratum, one execution per row. All three Codex rows come from a single 144-cell launch completed 2026-09-08 that ran the declared strata as sequential child studies. Six earlier Codex agentic executions of the same 16 tasks exist and are deliberately not listed — three Luna, one terra, one sol, plus the 2026-09-06 Luna run under the unrepaired prompt. They are separate studies, not repetitions, so they are neither averaged in nor shown as rivals; the [benchmark results](https://github.com/Borda/AI-Rig/blob/main/benchmarks/README.md#results) report every one of them in full.

**A later Codex run inverts the accuracy half of the Luna rows, and the efficiency half survives it.** On 2026-09-08 the same 16 tasks re-ran on the Luna stratum under prompts that state the answer population explicitly — how a module is named, which modules count as tests, that the module under analysis is excluded from its own importer list, what each dependency count counts, and how ties break in a ranking — instead of leaving those rules to be inferred. The baseline then answers all sixteen tasks and the required-Skill arm twelve, so the `+6.2 pp` in the Luna row below reads `−25.0 pp` under the disambiguated prompt. Three tasks that every baseline had failed in every stratum of every earlier run all pass, which is why this is read as prompt disambiguation rather than run-to-run noise: a large part of the Codex accuracy gain in this table was the treatment arm compensating for ambiguity the baseline had to guess at. The required arm's remaining misses are the agent mis-transcribing or mis-ordering answers the tool returned correctly, not wrong data from the tool. Every one of them was assembly work outside the query — counting a returned list by eye, subtracting one call from another, filtering a repository-wide ranking against a candidate set by hand — because ranking or counting *within a candidate set* had no query behind it. Version 0.35.0 adds it (`central --among`, plus `importer_count` and `excluded_test_importer_count` on `rdeps`); those additions do not change the historical figures in this paragraph. The extended-suite snapshot later in this README includes observed use of these query capabilities, but does not isolate their individual causal effect. The other two strata were then re-run on byte-identical prompts and both follow: every baseline answers all sixteen tasks, the required arm thirteen and fifteen, so the `+12.5 pp` and `+31.2 pp` in the rows below read `−18.8 pp` and `−6.2 pp`. Only part of the resource result carries across strata — output tokens fall 48–54% and wall-clock 23–42% in all three, while input tokens read −35%, +15%, and −8% and hold no direction. The table below stands as measured; treat every Codex accuracy figure in it as measured under prompts that did not state the answer population, and treat the token saving as an output-token and latency claim rather than an input-token one.

**The Codex rows and the Claude rows are no longer the same experiment revision.** Three things changed on the Codex side and only there: arm order is now cyclically balanced rather than fixed lexical, the required-arm contract accepts a standalone query through the injected `CODEMAP_BIN` variable *or* the exact installed launcher (the detector previously credited only the variable form, which is what made six terra cells look non-adherent), and the frozen treatment is Codemap 0.34.0 rather than 0.33.1. Read a Claude row against a Codex row as spanning that boundary.

`Sol` is the widest strict-arm gap the record holds — nine of sixteen cells correct in the baseline against fourteen with the Skill, at 57% fewer input tokens — and it is wide because that stratum's baseline did badly, not because its strict arm did unusually well. The strict arm lands on exactly 14 of 16 in all three strata while the baselines read 13, 12, and 9. Baseline performance is the volatile term: the same sol stratum answered 12 of 16 in its earlier execution and 9 here, on identical tasks.

Binary correctness is harsher than the semantic score the snapshots below report, so the large Haiku gains are movement from partially-right to exactly-right rather than from nothing. Every Codex row now pairs 16 of 16, with no cell dropped for a missing envelope, an incomplete answer, or a skipped required query — the first sweep in this record where the admission rule removes nothing. Codex remains the only row set where the optional-use arm regresses, and it regresses in all eight of the executions run under the two revisions this table spans, converting one baseline failure across 48 optional-arm cells while losing sixteen baseline successes. That regression is a prompt effect rather than a property of the arm, and stating the answer population removes it in every stratum: one lost cell on the first re-run stratum, none at all on the second, and on the third a single cell whose answer was correct and which failed on a harness cleanup check rather than on its content. Its token penalty does not reproduce (−17%, +16%, +1% here against +45% on the earlier sol run), so the accuracy direction is the finding and the token direction is not. Scored field by field, the optional arm's loss sits entirely in the two ordered/counted answers: it matches both other arms on every enumeration field (`production_importers` 0.991 against the baseline's 0.988 and the Skill arm's 1.000) and collapses on `ranking` (0.625 against 0.911 and 0.944) and `rdep_counts` (0.395 against 0.758 and 0.994). It finds who imports what as well as anyone and then ranks them worse than the arm with no tool at all — because it ranks by whatever raw reverse-dependency counts its improvised loop returns, including tests and examples, while the question asks within the production importer set. The Skill arm reads its `Need → Query` routing table before its first query; the bare-CLI arm spends its first round-trips on `--help` and `doctor` and then improvises. Having the counts without the contract that says which counts is worse than having no counts. A command-level replay of the earlier runs found no measurement fault behind the token side either: the optional arm is additive, querying the index and then exploring by hand anyway, so it drags 19.0k tokens of command output into each cell against the baseline's 7.1k. The required arm does the opposite — more commands than the baseline, but each returning a median 140–184 tokens against a grep's ~1,100, so it ends up reading less overall. Agentic elapsed figures stay cache-exposed: the Codex arm order is now balanced, the Claude one is not, and neither resets the provider cache between cells. Full cohort, estimator, and caveat detail: [benchmark results](https://github.com/Borda/AI-Rig/blob/main/benchmarks/README.md#results).

<a id="codex-structural-2026-08-07"></a> <a id="claude-structural-2026-09-06"></a>

### Extended-suite agentic snapshot

**More accurate answers at lower cost are demonstrated—not just fewer tokens.** In the latest Claude Haiku study, optional Codemap raises semantic credit from **77.9% to 97.3%**, with exactly correct answers increasing from **6/20 to 18/20**, while whole-arm gross input falls **53.6%** and cost falls **46.4%** ($4.77 → $2.56). Opus gains **3.61 percentage points** of semantic credit with either treatment. These are observed within-model improvements, not guarantees for every model or task.

Six separate model studies ran on 2026-09-08–09: 20 graph-query tasks × three arms × one repetition, 360 cells total. The extended tasks test deeper enumeration and ranking, not behavioral change-impact correctness. Different provider manifests and treatment delivery prevent pooling; earlier snapshots remain historical.

Semantic credit below means raw graded answer credit summed over all 20 assigned tasks, with unavailable answers contributing zero, **before treatment-compliance gating**. Exact counts are separately shown for admitted results, which require correct answers, valid formatting, complete execution, no contamination, and treatment adherence.

| Provider | Model  | Plain semantic credit | Optional semantic credit | Required semantic credit | Admitted exact A / B / C |
| -------- | ------ | --------------------: | -----------------------: | -----------------------: | ------------------------ |
| Claude   | Opus   |                 96.3% |                    99.9% |                    99.9% | 18 / 19 / 19             |
| Claude   | Sonnet |                 99.2% |                    97.9% |                    97.6% | 18 / 18 / 14             |
| Claude   | Haiku  |                 77.9% |                    97.3% |                    94.0% | 6 / 18 / 6               |
| Codex    | Luna   |                100.0% |                    99.9% |                    94.9% | 20 / 18 / 18             |
| Codex    | Terra  |                 95.0% |                   100.0% |                    99.9% | 19 / 20 / 19             |
| Codex    | Sol    |                 85.0% |                   100.0% |                    99.9% | 17 / 20 / 18             |

Exact counts are out of 20; A = plain, B = optional, C = required Skill. Claude semantic exact counts differ from admitted exact for Sonnet required (17 versus 14) and Haiku required (14 versus 6). Haiku plain has 19 scored answers; Codex Luna required has 19, Terra plain 19, and Sol plain 17; all other arms have 20. All scored Codex plain answers are semantically exact, so higher all-assigned treatment credit there reflects answer availability, not demonstrated semantic improvement among scored answers.

**The efficiency benefit is real but model-dependent.** On canonical pairs where both answers pass exactly, including admission checks, required Codemap reduces median output tokens and elapsed time across all three Codex strata:

| Codex model | Both-exact pairs | Output tokens | Elapsed time | Gross input |
| ----------- | ---------------: | ------------: | -----------: | ----------: |
| Luna        |               18 |        −15.2% |        −9.1% |       +3.8% |
| Terra       |               18 |        −41.2% |       −33.5% |      +26.0% |
| Sol         |               15 |        −19.8% |       −15.7% |       +4.5% |

These are medians of paired percentage changes, not whole-arm savings. Gross input increases in each required Codex stratum. Claude optional also shortens correct-answer latency, but Opus total cost increases despite its modest accuracy gain. Claude's recorded “fresh” residue excludes cache creation and is not comparable with Codex fresh input.

**The remaining opportunity is reliable delivery, not a need to invent an accuracy gain.** Sonnet's near-perfect plain arm retains a small semantic advantage. Lower Claude tiers sometimes omit required compact syntax or use command forms whose successful results are not recognized; eight of Haiku's fourteen strict violations still contain exactly correct answers. Thus its 94.0% raw semantic credit and 30.0% admitted quality describe different properties. They must not be collapsed into either “the answers were wrong” or “the strict integration works.”

Regressions also include real missing facts, count/ranking mistakes, and Codex execution/formatting failures. Two product issues remain actionable: filtered centrality output can fall back to an unfiltered count for a module with only test importers, and the deep-ranking naming disagreement is closed on the oracle side: a real scan names a package outside a configured source root from its own package root, which is the only name that resolves at runtime, and the benchmark oracle now derives module names by the same rule instead of by repository-relative path. The large earlier optional Codex collapse is not reproduced, but Luna still loses two exactly correct tasks. Working-tree fixes require prospective validation; no historical score is rewritten.

**Bottom line:** this run demonstrates a substantial semantic-accuracy and cost benefit for Haiku optional, a smaller semantic gain for Opus, and consistent correct-answer output/latency reductions for Codex required. One repetition on one graph-centric repository limits generalization; selective routing and dependable query delivery remain important.

Full per-cell evidence, canonical paired medians, and regression distinctions: [benchmark results](https://github.com/Borda/AI-Rig/blob/main/benchmarks/README.md#results).

### Structural navigation snapshot

On 2026-09-06, three Claude tiers ran 55 structural tasks across all three arms, 495 cells in one repetition against a frozen repository revision and a prebuilt index. `A_plain` is the no-Codemap baseline, `B_auto` makes Codemap optional, and `C_strict` requires the installed Skill. Accuracy is paired: each percentage pair is computed only over the tasks where both arms of that pair produced a scored, parsed answer, so both figures share one denominator, shown in parentheses as cells answered correctly. Gain is treatment minus baseline in percentage points, with the cell delta beside it. Input tokens, cost, and elapsed are per-task medians of treatment ÷ baseline restated as change against the baseline: negative means Codemap needed less.

| Tier   | Pair                | Paired n | Baseline accuracy | Treatment accuracy |          Gain | Input tokens | Cost | Elapsed |
| ------ | ------------------- | -------: | ----------------: | -----------------: | ------------: | -----------: | ---: | ------: |
| Haiku  | A_plain vs C_strict |       47 |     74.5% (35/47) |      87.2% (41/47) | +12.7 pp (+6) |         −65% | −71% |    −54% |
| Haiku  | A_plain vs B_auto   |       48 |     75.0% (36/48) |      85.4% (41/48) | +10.4 pp (+5) |         −48% | −52% |    −40% |
| Sonnet | A_plain vs C_strict |       50 |     84.0% (42/50) |      92.0% (46/50) |  +8.0 pp (+4) |         −52% | −40% |    −46% |
| Sonnet | A_plain vs B_auto   |       49 |     83.7% (41/49) |      95.9% (47/49) | +12.2 pp (+6) |         −52% | −43% |    −48% |
| Opus   | A_plain vs C_strict |       50 |     88.0% (44/50) |      96.0% (48/50) |  +8.0 pp (+4) |         −21% | −11% |    −24% |
| Opus   | A_plain vs B_auto   |       51 |     88.2% (45/51) |      94.1% (48/51) |  +5.9 pp (+3) |         −23% | −23% |    −25% |

The lift is largest where unaided navigation is weakest and narrows as the tier strengthens, while the efficiency gap persists at every tier. On the safety-grade view — caller-enumeration answers with recall at or above 0.90 — the weakest tier moves from 10/14 unaided to 14/14 under the required Skill; the two stronger tiers already reach 14/14 in every arm.

This run is descriptive and non-poolable: one repetition, one repository revision, one prebuilt index, and it retains contaminated, incomplete, and extraction-failed cells rather than filtering them. Twelve cells failed answer extraction and two baseline cells were dropped as contaminated, which is why the paired denominators vary. Two task defects found after the first execution were corrected and their cells re-measured; a third finding is a real gap in the tool's uncovered-symbol counting and is still scored as a miss rather than explained away. See the [canonical structural result and limitations](https://github.com/Borda/AI-Rig/blob/main/benchmarks/results-archive/2026-09-06-claude-structural.md).

<a id="codex-agentic-2026-08-07"></a>

<details>
<summary><strong>Agentic navigation snapshot — 2026-09-06</strong></summary>

The same three tiers completed 144 cells across 16 shared import-graph tasks and the same three arms. Values are per-task median savings against `A_plain`; positive means the arm needed less.

| Tier   | Arm      | Elapsed | Cost | Input tokens | Tool calls |
| ------ | -------- | ------: | ---: | -----------: | ---------: |
| Haiku  | C_strict |     46% |  45% |          60% |        62% |
| Haiku  | B_auto   |     53% |  51% |          70% |        68% |
| Sonnet | C_strict |     82% |  65% |          77% |        72% |
| Sonnet | B_auto   |     76% |  56% |          72% |        62% |
| Opus   | C_strict |     73% |  41% |          50% |        52% |
| Opus   | B_auto   |     77% |  51% |          43% |        59% |

Evidence recall holds at parity or better in every Codemap cell, while the baseline drops below it on four tasks — three in both fields (BA-03, BA-12, BA-15) and one, Sonnet's BA-07, in the final report alone, where the module was surfaced during the run and left out of the answer. Time spent inside tools moves the other way — an index call costs more than a single grep — so the saving comes from needing far fewer calls, not from faster ones. This study is exploratory and non-poolable for the same reasons as the structural one, plus fixed arm order and provider-cache exposure; one baseline cell hit its coordinate timeout and is excluded rather than scored as a loss. See the [canonical agentic result and measurement caveats](https://github.com/Borda/AI-Rig/blob/main/benchmarks/results-archive/2026-09-06-claude-agentic.md).

</details>

<a id="codex-structural-2026-09-06"></a> <a id="codex-agentic-2026-09-06"></a>

<details>
<summary><strong>Codex navigation snapshot — 2026-09-06</strong></summary>

This is the first of three Codex strata measured; `gpt-5.6-sol` and `gpt-5.6-terra` have their own snapshots below. One `gpt-5.6-luna` study ran the same shared task contracts through the Codex CLI: 219 structural-family cells across all three arms, plus 48 agentic cells. Structural accuracy below is paired over the 45-task headline cohort, on the same denominator rule as the Claude table. This run's `B_auto` arm was executed under a prompt that required a Codemap query; the contract has since changed to optional-use to match Claude's `B_auto`, so a future Codex `B_auto` run must not be blended with these numbers.

| Model | Pair                | Paired n | Baseline accuracy | Treatment accuracy |    Gain | Cells correct | Input tokens | Elapsed |
| ----- | ------------------- | -------: | ----------------: | -----------------: | ------: | ------------: | -----------: | ------: |
| Luna  | A_plain vs C_strict |       43 |             93.0% |              98.9% | +6.0 pp |       38 → 42 |         −53% |    −51% |
| Luna  | A_plain vs B_auto   |       44 |             91.3% |              97.4% | +6.1 pp |       38 → 41 |         −42% |    −42% |

Codex accuracy is the mean semantic quality score over the paired cells rather than a pass count, so the percentage and the correct-cell count move independently. Its token and elapsed figures are cohort totals restated as change against the baseline, where the Claude table above uses per-task medians.

The four executable and extraction stages stay separate and nonpoolable. ReadCrop matches the Claude result — equal correctness, 37% less input under the required Skill. Fix-Single and Fix-Multi are the counterexample: identical correctness at 45% and 29% more input than the control, so a fully localized edit with no unresolved structural fact is not a Codemap workload. Patch is 5/5 unaided and 4/5 under the strict arm, the single loss being a wrong fix rather than a tooling failure.

Agentically, the strict arm scores 0.960 against the control's 0.929 at 48% less input, answering 13 of 16 cells correctly against the control's 9, while the optional-use canary regresses to 0.860 and 6 of 16. Two defects in the measurement harness, both costing the treatment arms, were found in this run and are fixed prospectively rather than rescored. See the [canonical Codex result and limitations](https://github.com/Borda/AI-Rig/blob/main/benchmarks/results-archive/2026-09-06-codex-structural.md).

</details>

<details>
<summary><strong>Codex <code>gpt-5.6-sol</code> snapshot — 2026-09-07</strong></summary>

A second Codex stratum ran the same shared task contracts: 219 structural-family cells over 73 tasks in five stages, all three arms, one repetition per cell, Codex CLI 0.153.4 at high reasoning effort, against the same frozen repository revision and index as every other table here. Its `A_plain` and `C_strict` arm contracts are byte-identical to the `gpt-5.6-luna` run's; its `B_auto` contract is the newer optional-use one and is not the contract Luna's `B_auto` ran under. The observed CLI build is 0.153.4 against a reviewed 0.146.1, so this ran on a build the methodology had not reviewed.

**Read this as one stratum, not as a Codex result.** The sibling `gpt-5.6-terra` stratum of the same launch ran no paid cell — its launcher refused on a paid-approval token before the first model call — so this launch is half complete. Terra was relaunched and completed on 2026-09-07; that is a separate study with its own snapshot below, never a second half of this one.

Structural accuracy is in the cross-provider table above. On the run's own semantic-quality metric, over the 44 tasks where all three arms produced an admissible scored cell, mean quality is 0.9477 unaided, 0.9889 optional, and 0.9742 strict, with a median of exactly 1.000 in every arm and 36 of the 44 tasks scoring identically in all three. The strict arm is cheaper on 38 of those 44 tasks. Because the control is already saturated, this design can detect degradation and cannot detect improvement, and the quality effect is indistinguishable from zero at one repetition per cell with no significance testing.

Per-task spread separates the two treatment arms where the medians do not. Against the unaided baseline, the input-token ratio for the strict arm runs 0.254–1.066 between the 10th and 90th percentiles and is cheaper on 38 of 44 tasks; the optional arm runs 0.535–1.951 and is cheaper on 19 of 44, with a worst task at 2.74× the baseline. The two distributions are about equally wide — the difference is that the strict arm's sits below parity while the optional arm's straddles it. Uptake does not explain the gap: 26 of the 44 optional cells did query Codemap, and their median ratio (0.974×) is barely below the cells that did not (1.016×), because the optional arm queries Codemap in addition to exploring the repository the ordinary way.

The four executable and extraction stages stay separate and must not be averaged together — their token direction is not consistent:

| Stage      | Cells | A_plain correct | B_auto correct | C_strict correct | B tokens | C tokens |
| ---------- | ----: | --------------- | -------------- | ---------------- | -------: | -------: |
| ReadCrop   |    18 | 5/6             | 5/6            | 5/6              |     +60% |     −34% |
| Fix-Single |    12 | 4/4             | 4/4            | 4/4              |     +16% |     +15% |
| Fix-Multi  |     9 | 3/3             | 3/3            | 3/3              |     −15% |     +34% |
| Patch      |    15 | 5/5             | 5/5            | 4/5              |     −36% |     −67% |

Token columns are cohort totals against `A_plain`. Requiring Codemap costs 15% more input on single-file edits and 34% more on multi-file edits for identical correctness, which is the same counterexample the Luna run produced and the reason production guidance skips Codemap for a fully localized edit with no unresolved structural fact. The single Patch loss is the strict arm failing a cell both other arms passed, at a quarter of their input — one task at one repetition supports no claim either way, but it is not evidence that requiring the tool is safer.

The optional arm abandons the tool wherever code has to be modified: uptake is 5 of 6 ReadCrop cells and 35 of 55 structural cells, but 0 of 12 across Fix-Single, Fix-Multi, and Patch. An optional rollout should not be expected to reproduce the strict-arm result on editing work.

Delivery is confounded with arm — the optional arm always used the direct CLI and the strict arm always the installed Skill — so no optional-versus-strict difference can be attributed to strictness alone. Full cohort, estimator, and caveat detail is in the benchmark record's `gpt-5.6-sol` Codex section.

</details>

<details>
<summary><strong>Codex <code>gpt-5.6-terra</code> snapshot — 2026-09-07</strong></summary>

The third Codex stratum ran the same shared task contracts: 219 structural-family cells over 73 tasks in five stages, all three arms, one repetition per cell, Codex CLI 0.153.4 at high reasoning effort, against the same frozen repository revision and index as every other table here. All three arm contracts are byte-identical to the `gpt-5.6-sol` run's, so this is the second study under the optional-use `B_auto` contract. As on Sol, the observed CLI build is 0.153.4 against a reviewed 0.146.1. This is the stratum whose first launch was refused on a paid-approval token; the relaunch is a separate study, not the missing half of the Sol launch.

Structural accuracy is in the cross-provider table above. On the run's own semantic-quality metric over the headline cohort, mean quality is 91.6% unaided against 97.2% strict over 40 pairs, and 91.8% against 98.4% optional over 41 pairs. **Neither treatment arm turns a correct answer wrong**: the strict arm converts DI-01 and DI-02, the optional arm those two plus DI-06, and the only quality regression anywhere is BR-07 at −0.056 in both arms.

The paired counts are 40 and 41 rather than 45 because four control cells were lost — `CQ-01` left a command item open, `FT-05` and `DI-05` exited non-zero with zero tokens, and `FT-03` failed answer extraction — plus one strict cell dropped for non-adherence. Every lost control cell is one the unaided arm failed to complete, so removing them raises the baseline and makes the gains above conservative.

| Stage      | Cells | A_plain correct | B_auto correct | C_strict correct | B tokens | C tokens |
| ---------- | ----: | --------------- | -------------- | ---------------- | -------: | -------: |
| ReadCrop   |    18 | 5/6             | 6/6            | 6/6              |    +101% |     +10% |
| Fix-Single |    12 | 4/4             | 4/4            | 4/4              |     −12% |      +3% |
| Fix-Multi  |     9 | 2/3             | 3/3            | 3/3              |      −5% |      −2% |
| Patch      |    15 | 3/5             | 3/5            | 4/5              |     −20% |      −9% |

Token columns are cohort totals against `A_plain`. ReadCrop inverts here — the strict arm reads 10% more where Luna and Sol read 37% and 34% less — while converting the one cell the control got wrong. Editing stages sit near parity in both directions and support no claim at 3 to 5 tasks each. The one Patch task solved only under the strict arm, and the one failed by all three arms, are single observations that run opposite to Sol's single Patch loss.

Optional uptake repeats the Sol pattern exactly: 4 of 6 ReadCrop cells, 48 of 55 structural cells, and 0 of 12 across the three editing stages. Delivery is again confounded with arm, and `skill_delivery_observed` is false in all 165 structural cells. Full cohort, estimator, and caveat detail is in the benchmark record's `gpt-5.6-terra` Codex section.

</details>

<details>
<summary><strong>Extended operational reference</strong></summary>

### 🔗 Integration protocol

The integration engine is source-owned and authenticated. Its modes have distinct evidence and mutation boundaries:

- `audit` is the bounded read-only route. It reports observed provider and consumer versions, managed blocks, index identity, runtime-scoped logs, usage, findings, bounded provider content identity, same-version content drift, and an explicit `session_catalog: unobservable` state when the native listing has no session provenance.
- `plan` writes an inspectable candidate and SHA-256.
- `apply` changes only an approved managed block in checked-in consumer source.
- `sync` installs only an approved local candidate or immutable release through the native runtime CLI.
- `demo` records audit evidence plus one representative structural smoke query; it does not claim a plain-vs-structural comparison, token savings, or current-session activation.

These routes never edit installed caches directly, write global Codex instructions, publish a release, or push Git. For Codex, `shared/codemap-py-integration.md` is metadata-only; the active consumer contract owns launcher validation, query guidance, and one-artifact reuse. Audit reports provider identity and active-guidance reachability/content separately, with static-reference limits; it cannot claim live fresh-session activation or semantic currency from hashes alone. After a runtime sync, follow the host's fresh-session guidance.

Consumer integrations should treat Codemap as optional structural context. For the two currency states, choose one explicit route:

- Missing index: build it in the foreground, continue without it, or stop and ask for a later scan.
- Stale index: refresh, continue with the stale-data caveat, or skip retrieval.
- Unavailable launcher: use the host's normal source exploration rather than silently claiming that no callers or tests exist.

### 🧭 Skill boundaries and shared truth claims

Both runtime rosters expose the same six capabilities; only invocation syntax and host tool bindings differ. The shared contract is [`shared/capability-contract.md`](https://github.com/Borda/AI-Rig/blob/main/plugins/codemap-py/shared/capability-contract.md), and it is the authority for exit codes, completeness metadata, and static-analysis caveats.

| Skill            | Use it for                                                                                               | It does not do                                                                             |
| ---------------- | -------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| `scan-codebase`  | Build or refresh the whole Python index, optionally from a consistent `--root`.                          | Answer a query or validate runtime behavior.                                               |
| `query-code`     | Read dependencies, callers, symbols, paths, quality flags, tests, or diff impact from an existing index. | Rename symbols, rebuild explicitly requested indexes, or replace source/test verification. |
| `test-impact`    | Identify structurally affected tests and emit a pytest command; it does not execute that command.        | Prove tests pass or resolve dynamic dispatch invisible to the static graph.                |
| `rename-refs`    | Apply or preview one Python symbol/module rename with a confirmation and re-scan verification pass.      | Guarantee dynamic, cross-repository, or inheritance references are covered.                |
| `integration`    | Audit, plan, apply, sync, or demo the supported consumer wiring and active query guidance.               | Mutate remote services, global instructions, or an installed cache directly.               |
| `debrief-coding` | Summarize local cross-runtime Codemap telemetry, timing, completeness, and repeated-search avoidance.    | Build/query the index or validate installation health.                                     |

Direction and scope rules:

- `rdeps` and `deps` point in opposite directions.
- To order or threshold modules a previous query returned, pass them to `central --among <a,b,c>` rather than filtering a repository-wide `central` ranking by hand; `unmatched` names every requested module the ranking did not cover, and a scoped ranking returns every candidate unless `--top` is given.
- `rdeps` reports `importer_count`, and `excluded_test_importer_count` alongside it under `--exclude-tests`, so both totals come from one call; never derive one by subtracting a second call from the first.
- For direct production callers, use `fn-rdeps <module::symbol> --exclude-tests`; use `fn-blast` only for an explicitly transitive request.
- For direct test-module importers, use `rdeps` and filter test modules; reserve `test-impact` for transitive affected-test selection.
- A method-name match is only an override candidate; verify ancestry and package boundaries in source.

### 🗂️ Index lifecycle and completeness

Index location and refresh rules:

- Default index: `.cache/codemap/<project>.json`.
- `CODEMAP_INDEX_DIR` changes only the parent directory and keeps the project basename as the filename.
- After a custom-root scan, query with `--index <emitted-index-path> --root <same-root>`; `--root` is path resolution only and does not select the index. With an explicit root, the guard admits only that exact default or `CODEMAP_INDEX_DIR`-override emitted path outside the caller project; arbitrary sibling files remain rejected.
- Prompt freshness runs its indexed dirty-path check from the Git root, so a nested session detects root-level `.py`, `.pyi`, `.rst`, and nested documentation Markdown changes before starting its bounded refresh.
- Normal queries may perform a bounded incremental self-heal; `SCAN_NO_AUTOBUILD=1` makes a missing index a hard refusal and prevents implicit writes.

Every query exposes an `index` block. Follow this sequence:

1. Query first; do not spend a call on an unconditional pre-scan or freshness probe.
2. Read `query_complete` as direction-scoped graph coverage, not as a promise that a bounded display list is untruncated.
3. Inspect `confidence`, `truncated`, and `total_available`; use `--limit 0` where supported when the complete list matters.
4. After a complete, untruncated result, do not re-query, read, or grep for the same structural fact; sibling queries for distinct dimensions still run when required. Source-body reads remain valid for distinct implementation or runtime details and explicit independent AST/oracle work.
5. For an incomplete or degraded result, use only a targeted fallback for the named gap (`stale`, `degraded`, `not_covered`, or similar); use `test-impact` when the open question is test choice.

`stale`, degraded modules, untracked files, root mismatches, and collisions lower confidence and require source/test review.

### 🔍 Troubleshooting checklist

Use this order when a route is inconclusive:

1. Dispatcher `127`: inspect `CODEMAP_PYTHON` and the eligible CPython range before debugging imports.
2. Missing or stale index: run `codemap-py index --root PATH`, then repeat the query from the same project root.
3. Capped list: inspect its completeness metadata and rerun with a supported larger limit.
4. Hooks, callbacks, dynamic imports, string dispatch, lazy loading, or inheritance: read the source and named test/oracle regardless of a complete static result.
5. Integration drift: run `codemap-py integrate audit --json`, inspect observed findings, and create a fresh stage-specific plan before applying or syncing anything.

<details>
<summary><strong>Complete integration mode reference</strong></summary>

The integration skill is a thin, source-owned adapter over `codemap-py integrate`. It has a closed consumer set: Claude consumers `foundry`, `oss`, `develop`, and `research`, and Codex consumer `codex-rig`. It does not discover arbitrary plugins or invoke another runtime's model.

| Mode    | Supported arguments                                                                                         | Writes or mutates                                                                          |
| ------- | ----------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| `audit` | \[`--runtime {claude,codex,both}`\] \[`--json`\] \[`--since YYYY-MM-DD`\]                                   | Nothing; reports observed provider/consumer/index/log evidence, findings, and remediation. |
| `plan`  | \[`--runtime ...`\] \[`--consumers <csv>`\] \[`--source {local-candidate,release}`\] \[`--out <artifact>`\] | A reviewable plan artifact containing targets, hashes, argv, and rollback identities.      |
| `apply` | `--plan <artifact> --approve <sha256>`                                                                      | Approved managed blocks in checked-in consumer source only.                                |
| `sync`  | `--source {local-candidate,release} --plan <artifact> --approve <sha256> [--runtime ...]`                   | Approved local runtime plugin state through the native runtime CLI.                        |
| `demo`  | \[`--runtime {claude,codex,both}`\]                                                                         | Audit plus one structural smoke query; disposable evidence only.                           |

`audit` has this contract:

- Defaults to `both`; supports `--runtime claude|codex|both`, JSON schema 2 (`codemap-py.integration.v2`), and `--since YYYY-MM-DD`.
- Reports `pass`, `warn`, or `fail`; exits `0`, `1`, or `2` for completed status/syntax semantics.
- Records stable findings such as `runtime_log_isolation_bypassed`, `runtime_identity_missing`, `runtime_logs_not_observed`, `managed_block_invalid`, `split_index_roots`, `index_stale_or_unknown`, and `index_degraded`.
- Remediation values are advisory (`plan_apply`, `plan_sync`, `source_maintenance`, `provider_release_required`, `scan_codebase`, `observe_next_session`, `none`) and are never executable artifacts.
- `--runtime claude` scopes to the four Claude consumers; `--runtime codex` scopes to `codex-rig`.
- `--approve` is valid only with `apply` or `sync`, a saved plan, and the exact SHA-256 printed for that plan.
- Active consumer guidance is checked separately from provider metadata. Missing, unreachable, or outdated guidance is reported as source maintenance or an approved `plan_sync` action; static references prove reachability only, not semantic loading or current-session activation. Installed-byte identity and session evidence remain separate.

Mutation boundaries:

- `plan` is non-mutating.
- `apply` refuses path escapes, symlinks, installed-cache roots, dirty overlap, foreign markers, and body hashes that were not generated by the engine.
- `sync` uses either a deterministic local candidate or an immutable release identity and reports a journal for partial failure.
- Both mutation modes leave Git commits and pushes to the maintainer.

```text
/codemap-py:integration audit --runtime both --json
/codemap-py:integration plan --consumers foundry,oss --out .reports/integrate/plan.json
/codemap-py:integration apply --plan .reports/integrate/plan.json --approve <printed-sha256>
/codemap-py:integration sync --source release --plan .reports/integrate/plan.json --approve <printed-sha256> --runtime codex
/codemap-py:integration demo --runtime both
```

</details>

<details>
<summary><strong>Scan-codebase flags, performance, and exclusions</strong></summary>

The scan skill dispatches `codemap-py index`; it does not answer a query. Its verified CLI surface is:

| Flag                   | Contract                                                                                                   |
| ---------------------- | ---------------------------------------------------------------------------------------------------------- |
| `--root PATH`          | Scan the selected project root instead of the Git root or current directory.                               |
| `--incremental`        | Re-parse changed files against an existing v3-or-newer index; without one, fall back to a full scan.       |
| `--with-coverage PATH` | Attach per-symbol line coverage from a `.coverage` SQLite file when `coverage>=7.4` is installed.          |
| `--timeout N`          | Set a hard Unix `SIGALRM` timeout in seconds; `0` means no limit and Windows does not provide this signal. |

Scan behavior and routing:

- The full scan walks Python files with `ast.parse`, records imports, symbols, calls, hashes, source-root metadata, and degraded-file reasons, then writes one JSON index.
- Incremental mode compares stored Git blob hashes or non-Git content hashes.
- Build duration varies with repository size and filesystem; the scanner reports indexed and degraded counts, so the README does not promise fixed timings.
- Run a full scan after clone or a large structural change, and use incremental mode after smaller changes or when a currency gate requests it.

```text
/codemap-py:scan-codebase
/codemap-py:scan-codebase --incremental
/codemap-py:scan-codebase --root services/api
codemap-py index --with-coverage .coverage
```

Built-in pruning excludes `.git`, virtual environments, build/dist/cache trees, `node_modules`, scratch/report directories, and dot-directories. Add project-specific exclusions in either form:

```toml
[tool.codemap]
exclude = ["vendor-copy", "generated/*.py"]
src_roots = ["packages/core/src", "services/api/src"]
```

```text
# .codemapignore: one directory name or fnmatch path per line
vendor-copy
generated/*.py
```

Exclusion semantics:

- A bare name prunes matching directories anywhere; a path or glob matches a project-relative file path.
- Exclusions are recorded in `excluded_roots` and do not trigger incremental rebuilds.
- `src_roots` is ordered: the first matching root determines module naming and collision priority.
- The index records effective roots and any deterministic module-name collisions so a result is not mistaken for a complete graph.

</details>

<details>
<summary><strong>Query-code subcommands and completeness contract</strong></summary>

The query CLI reads an existing index and emits JSON. The complete subcommand surface is grouped below; feature-gated commands report that an older index must be rebuilt rather than silently returning an incomplete answer.

Additional query contracts:

- The module group also includes `import-types <module>`.
- `deps` accepts `--stdlib`, `--third-party`, or `--internal`; `rdeps`, `central`, and `coupled` accept `--entity TYPE` for indexed project, test, docs, or example entities. `rdeps --limit N` previews static `imported_by` only; `dynamic_imported_by` and `config_refs` remain exhaustive. Default `rdeps` and `rdeps --limit 0` return every static importer.
- The path query returns exit 0 with a null path and reason `no-import-path` when known modules are disconnected; unknown-module and filesystem failures remain errors.
- Symbol responses expose `stale` and `stale_reason` when recorded line ranges no longer match source.
- Function results carry call-edge resolution (`import`, `local`, `self`, `star`, or `unresolved`), while `fn-rdeps` reports distinct caller count rather than raw call-site multiplicity.

| Group                          | Commands and purpose                                                                                                                                                                            |
| ------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Modules                        | `deps <module>`, `rdeps <module> [--limit N]`, `path <from> <to>`, `central [--top N]`, `coupled [--top N]`, `list [--limit N]`, `packages`                                                     |
| Symbols                        | `symbol <name> [--limit N] [--exclude-tests] [--with-imports]`, `symbols <module>`, `find-symbol <regex> [--limit N] [--exclude-tests]`                                                         |
| Calls                          | `fn-deps <module::symbol>`, `fn-rdeps <module::symbol> [--exclude-tests]`, `fn-central [--top N] [--exclude-tests]`, `fn-blast <module::symbol>`                                                |
| Tests and edges                | `test-impact <module[::symbol]> [--no-mocks]`, `mock-rdeps <module[::symbol]>`, `fixture-rdeps <fixture>`, `fixture-graph <test-file>`, `subprocess-deps <module>`, `subprocess-rdeps <module>` |
| Coverage and docs              | `coverage <module[::symbol]>`, `coverage-gap [module] [--all] [--threshold P]`, `uncovered [module] [--all] [--sort loc, name, or module] [--top N]`, `undocumented [module] [--all]`           |
| Cross-references and dead code | `xrefs <symbol-or-module> [--broken]`, `dead-symbols [--min-loc N]`, `dead-modules`                                                                                                             |
| Composite                      | `diff-impact [--base REF] [--diff-file PATH]`, `batch [JSON-ARRAY or JSON-PATH or stdin]`                                                                                                       |

Every query accepts these global flags before or after the subcommand:

- `--index PATH` selects the index file.
- `--root PATH` resolves paths only; it does not retarget or rebuild an index, and a mismatch forces `query_complete=false`.
- `--timeout N` sets the query timeout.
- `--no-heal` answers from the existing index without bounded query-time refresh.
- `--verbose-coverage` keeps the full coverage block on every query.
- `--compact` reduces repeated coverage metadata.

Choose direction deliberately:

- `rdeps` means importers; `deps` means imports.
- `fn-rdeps` means direct callers; `fn-blast` means transitive callers.
- `central` ranks reverse imports; `coupled` ranks internal import count.
- Use `symbol --with-imports` for a source slice plus its module imports, `find-symbol` for a regex, and `path` for the shortest import chain.
- `find-symbol` name matches are override candidates, not inheritance proof.

Batch and diff behavior:

- Batch input is a JSON array of objects such as `[{"cmd":"rdeps","args":["mypackage.auth"]}]`, passed inline as the argument, as a path to a file holding one, or on stdin via `-` (the default).
- Items execute in one process and share one index load/coverage process; nested `batch` and `diff-impact` items are rejected. Each item retains its own `result.index` completeness, truncation, total, and scope metadata, while per-item failures remain on that item; the top-level index is a conservative summary and cannot upgrade an item. Skills keep standalone queries as the default.
- `diff-impact` derives changed modules, per-module reverse dependencies/coupling, function callers, and a union of affected tests from a Git ref or unified diff.

Coverage metadata is intentionally dieted after the first query in a process. Keep these distinctions when interpreting results:

- Use `--verbose-coverage` when each result must carry the full block.
- `query_complete` is direction-scoped graph coverage, not a guarantee that a list is untruncated.
- Read `confidence`, `truncated`, and `total_available`.
- `symbol`, `find-symbol`, and list-like commands default to bounded output and accept `--limit 0` where documented.
- A complete graph with a capped display is still only a displayed slice; a truncated `rdeps` preview never settles exhaustive callers.

```text
codemap-py query --compact rdeps mypackage.auth --exclude-tests
codemap-py query fn-rdeps mypackage.auth::validate --exclude-tests
codemap-py query symbol validate --with-imports --limit 0
codemap-py query find-symbol '^Auth.*Handler$' --exclude-tests --limit 0
codemap-py query batch - < requests.json
```

</details>

<details>
<summary><strong>Test impact and rename-refs contracts</strong></summary>

The `test-impact` skill has a deliberately narrow contract:

- Accepts exactly one target, either a bare module or `module::symbol`, plus `--no-mocks`.
- Identifies structurally affected test files, preserves the index completeness and `not_covered` caveats, and emits a pytest command for the maintainer to review and run.
- Does not execute tests, find every caller, or prove runtime behavior.
- For more than one target, use separate invocations; the skill warns rather than silently combining them.

```text
/codemap-py:test-impact mypackage.auth::validate --no-mocks
$codemap-py:test-impact mypackage.auth
```

`rename-refs` has two explicit subcommands:

```text
/codemap-py:rename-refs symbol <old-qname> <new-qname> [--dry-run] [--deprecate[=<decorator>]] [--since <version>] [--removed-in <version>] [--remove-if-no-callers]
/codemap-py:rename-refs module <old-module> <new-module> [--dry-run]
```

Rename behavior and safety gates:

- The symbol route updates a one-to-one Python definition and statically visible references; the module route renames a file/module and import lines.
- `--dry-run` previews without editing.
- `--deprecate` is symbol-only and defaults to the project deprecation decorator; `--since` and `--removed-in` add the version window.
- `--remove-if-no-callers` is a hard safety gate: it is honored only when the caller graph is complete, zero callers are found, and the user confirms removal.
- The workflow refuses ambiguous or one-to-many matches, stale or degraded coverage, path escapes, dynamic references, cross-repository callers, and caller sets above its edit cap; inspect source and tests for those cases.
- A successful edit is followed by an explicit rescan and verification step.

```text
/codemap-py:rename-refs symbol mypackage.auth::validate mypackage.auth::verify --dry-run
/codemap-py:rename-refs symbol mypackage.auth::validate mypackage.auth::verify --deprecate --since 2.1 --removed-in 3.0
/codemap-py:rename-refs module mypackage.old_utils mypackage.utils
```

</details>

<details>
<summary><strong>Debrief-coding telemetry and anonymization</strong></summary>

`debrief-coding` reads local JSONL telemetry and writes a diagnostic report; it does not build or query the index. Its collection and report contract is:

- Flags: `--since YYYY-MM-DD`, `--session ID`, `--anonymize`, and `--output PATH` (default `.reports/codemap/debrief-<date>.md`).
- Claude records CLI, skill, and tool layers under `.cache/codemap/logs/`; Codex hooks record runtime-scoped CLI and tool shards but have no skill-start hook, so skill telemetry and some cross-layer joins can be unavailable.
- Flat legacy records remain unattributed.
- Reports include overall, per-runtime, and unattributed usage summaries, refresh provenance, timing, completeness, and repeated-search avoidance.
- `token_measurement` is unavailable because host hooks provide no token usage. Debrief does not measure token savings or verify live fresh-session activation.
- Set `CODEMAP_LOGGING=false` to disable logging. Logs rotate at the implementation-defined size limit.

The report summarizes command, skill, and search/read activity, timing, result counts, completeness reasons, per-runtime usage, and repeated-search avoidance. `join_avoidance.py` performs the offline join with a bounded time window; an avoidance event means a search/read names a module that a complete Codemap query already answered, not that the agent's runtime answer is incorrect.

Anonymization behavior:

- `--anonymize` pseudonymizes qualified names with a project-local salt at `.cache/codemap/logs/.salt`, scrubs qualified names inside error and stderr text, hashes `not_covered` values, and writes export JSONL separately.
- Never share the salt with an anonymized export.
- Anonymization protects names in the supported log fields; it is not a guarantee that arbitrary free text contains no identifying information.

```text
/codemap-py:debrief-coding
/codemap-py:debrief-coding --since 2026-08-01 --session <session-id>
/codemap-py:debrief-coding --anonymize --output .reports/codemap/debrief-shareable.md
```

</details>

<details>
<summary><strong>Scanner, query, and index architecture</strong></summary>

Scanner and query architecture:

- The scanner is dependency-free Python: it walks the selected root, parses `.py` and `.pyi` files with the standard-library AST, resolves imports and selected call edges, computes module/function graph counts, captures supported documentation references, and writes an atomic versioned JSON index.
- A `.py` implementation takes precedence over a sibling stub; an unpaired stub contributes declarations/imports but no implementation call edges.
- Parse or encoding failures are retained as degraded module records instead of being silently dropped.
- Incremental refreshes still track changed documentation for freshness and documentation references, but only `.py` and `.pyi` entries become module records, so documentation changes no longer contaminate module degradation counts and a subsequent refresh self-heals the index.

The query engine loads the same JSON under a read lease, performs bounded freshness checks or self-heal, dispatches one subcommand, and returns a primary result plus an `index` coverage block. The block includes method, confidence, query completeness, truncation, totals, degraded count, stale/root-mismatch state, and `not_covered` blind spots. The index is a cache, not a daemon or runtime tracer; no static result proves dynamic dispatch, external consumers, inheritance, test pass status, or behavior.

The default index contains modules, relative paths, symbols and line ranges, direct imports, calls and resolution tags, test/entity classification, source-root metadata, exclusions, collisions, scan version, and Git blob or non-Git content hashes. The JSON format is version-gated: call-graph, fixture, subprocess, documentation, dead-code, and coverage queries refuse unsupported index versions with an upgrade/rebuild instruction.

</details>

<details>
<summary><strong>Index locations, non-Git roots, and currency</strong></summary>

Index location and currency:

- By default the index is `.cache/codemap/<project>.json`, where `<project>` is the selected root basename.
- `CODEMAP_INDEX_DIR` changes the base to `<override>/<project>.json`; use separate override directories when two projects share a basename.
- `CODEMAP_COORDINATION_DIR` moves only the `.index-rw` read/write gate to the named directory and leaves the index where it resolved. Set it when the index directory cannot take writes — a read-only or shared mount, or a sandbox that grants write access to the gate alone. It names that directory itself, so give each index its own: two sharing one directory share a registry mutex and serialise against each other.
- `--index PATH` selects a specific query file, while `--root PATH` only controls path resolution.
- `SCAN_NO_AUTOBUILD=1` disables implicit query refresh and makes missing indexes a structured manual-build error.
- Git repositories use stored Git blob hashes and the repository revision for fast currency checks. Non-Git projects use content hashes, so incremental scans and stale detection still work without `git`.
- A custom root is recorded as `scan_root`; subsequent scans and queries must use that same tree or explicitly select the matching index. Multiple configured `src_roots` are ordered and recorded to make module naming reproducible.
- The integration and consumer currency gates distinguish a missing index from stale data: Gate A offers build, continue without Codemap, or abort; Gate B offers refresh, continue with an explicit stale caveat, or abort.
- There is no post-commit hook requirement. Explicit scan remains available for CI and benchmarks where build cost should be controlled.

```bash
CODEMAP_INDEX_DIR=<absolute-cache-dir> codemap-py index --root <project-root>
SCAN_NO_AUTOBUILD=1 codemap-py query --index <matching-index> rdeps mypackage.auth
```

</details>

<details>
<summary><strong>Named troubleshooting cases</strong></summary>

| Symptom                                 | Evidence-led response                                                                                                                                                  |
| --------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `127` from a launcher                   | Check `CODEMAP_PYTHON`, PATH, and the supported CPython range before inspecting imports.                                                                               |
| `index not found` or empty results      | Confirm the selected root has Python files, check `CODEMAP_INDEX_DIR`, then run an explicit scan.                                                                      |
| stale or `root_mismatch`                | Re-scan the exact root and query from that root or pass the matching `--index`; do not report the graph as complete.                                                   |
| `query_complete=false`                  | Read `completeness_reason`, `degraded`, `untracked`, `collision`, and `not_covered`; investigate only the named gap.                                                   |
| capped result                           | Inspect `truncated` and `total_available`, then rerun with a supported larger `--limit` or `--limit 0`.                                                                |
| `upgrade required`                      | Rebuild the index with the current scanner; feature-gated graph data cannot be inferred from an older file.                                                            |
| degraded modules                        | Inspect the recorded path/reason; generated or syntax-invalid files remain outside reliable graph coverage.                                                            |
| `scan-query` not found                  | Use the skill, resolve the installed launcher path, or add the package `bin/` directory to PATH; Codex does not inject it automatically.                               |
| integration missing/outdated            | Run `integration audit`, inspect observed evidence and the source-owned managed block, then create a fresh stage-specific plan rather than editing an installed cache. |
| dynamic hook/callback/override behavior | Treat static results as candidates; inspect implementation and named tests/oracles because AST edges cannot prove runtime behavior.                                    |

</details>

</details>

## 🔧 Six skills

Both runtimes expose these names:

| Skill            | Purpose                                                                                              |
| ---------------- | ---------------------------------------------------------------------------------------------------- |
| `scan-codebase`  | Build or refresh the Python structural index. Explicit invocation; it does not answer a query.       |
| `query-code`     | Select and render a structural query without rebuilding or editing source.                           |
| `test-impact`    | Identify affected test files and emit a pytest command; it does not run tests.                       |
| `rename-refs`    | Rename a Python symbol or module using static caller/import evidence, with confirmation and caveats. |
| `integration`    | Audit, plan, apply, sync, or demo the supported consumer wiring and runtime evidence.                |
| `debrief-coding` | Analyze local cross-runtime Codemap telemetry, optionally producing an anonymized report.            |

Claude uses `/codemap-py:<skill>`. Codex uses `$codemap-py:<skill>`. Both skill rosters use concise, instruction-first prose while retaining command syntax, routing, stop rules, safety gates, and runtime notes for installed-root resolution, PATH behavior, and each host's confirmation mechanism. Claude executable fences remain byte-identical so compression cannot change shell behavior.

## 🔗 Integration with other plugins

The integration engine has an explicit closed consumer set:

- Claude consumers: `foundry`, `oss`, `develop`, and `research`.
- Codex consumer: `codex-rig`.

`/codemap-py:integration audit` or `$codemap-py:integration` reports observed installed versions, roots, protocol compatibility, managed blocks, runtime-scoped telemetry, and wiring state without guessing at unavailable runtime facts.

Mode boundaries:

- `plan` writes an inspectable artifact.
- `apply` updates only approved managed blocks in checked-in consumer source.
- `sync` installs only the approved local candidate or immutable release through the native runtime CLI.
- Both mutation modes require the plan SHA-256 and never push Git, publish a release, edit installed caches directly, or write Codex global instructions.
- `demo` records disposable evidence.

## ⚙️ Configuration

The default index path is `.cache/codemap/<project>.json`, where `<project>` is the project-root basename. Set `CODEMAP_INDEX_DIR` to an absolute override directory to use `<override>/<project>.json`; separate colliding project names with separate override directories. `SCAN_NO_AUTOBUILD=1` keeps query and test-impact routes from creating or refreshing an index implicitly.

`.claude-plugin/permissions-allow.json` lists the tool calls the skills expect to be pre-approved. `.claude-plugin/permissions-deny.json` is its counterpart — the operations that must stay denied no matter how broad the allow list becomes: destructive shell and git commands (`rm -rf`, `sudo`, `ssh`, `chmod 777`, branch and tag deletion, force-push, `claude --dangerously-skip-permissions`) plus every public-GitHub write (`gh issue`/`pr`/`release`/`gist` create, edit, merge, delete, and `gh api` with `POST`, `PATCH`, `PUT` or `DELETE`). Neither file is merged into `~/.claude/settings.json` automatically. The sibling `cc_*` plugins each merge their own pair from their `/<plugin>:setup` skill; this plugin ships no setup skill, so the merge is manual. Both commands are additive and idempotent — `unique` keeps existing entries from duplicating, and nothing is ever removed:

```bash
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}"
[ -f ~/.claude/settings.json ] || printf '{}\n' > ~/.claude/settings.json
cp ~/.claude/settings.json ~/.claude/settings.json.bak
jq --slurpfile perms "$PLUGIN_ROOT/.claude-plugin/permissions-allow.json" \
    '.permissions.allow = ((.permissions.allow // []) + $perms[0] | unique)' \
    ~/.claude/settings.json > ~/.claude/settings.json.tmp && mv ~/.claude/settings.json.tmp ~/.claude/settings.json
jq --slurpfile deny "$PLUGIN_ROOT/.claude-plugin/permissions-deny.json" \
    '.permissions.deny = ((.permissions.deny // []) + $deny[0] | unique)' \
    ~/.claude/settings.json > ~/.claude/settings.json.tmp && mv ~/.claude/settings.json.tmp ~/.claude/settings.json
```

The first two lines cover a first install, where `~/.claude/settings.json` may not exist yet, and leave a backup at `~/.claude/settings.json.bak`; run `jq empty ~/.claude/settings.json` afterwards and restore from that backup if it does not parse. Deny wins over allow in Claude Code, so the order of the two merges does not matter. Deny entries are prefix matches, so they stop the documented command forms rather than every possible flag ordering.

Use `--root PATH` when the Python tree is a subproject or monorepo component. The scan names the index from that root's basename, and later queries must use the same root or an explicit matching index. `--root` on query controls file-path resolution; it does not retarget an index built for a different tree, and a mismatch is reported rather than silently accepted.

## 🔢 Compatibility and exit codes

`scan-index` and `scan-query` remain compatibility aliases for the canonical `codemap-py index` and `codemap-py query` launchers. New skill and documentation examples use the canonical dispatcher. The `.cache/codemap/` layout and `CODEMAP_*` variables remain compatible with the renamed product.

```text
! BREAKING — the Claude skill namespace changed from `/codemap:*` to `/codemap-py:*`. Every saved prompt, alias, or automation invoking a `/codemap:scan-codebase`-style trigger stops resolving.
Fix: update each call site to `/codemap-py:<skill>`. `scan-index`/`scan-query`, `.cache/codemap/`, and every `CODEMAP_*` variable are unaffected and keep working unchanged.
```

```text
! BREAKING — the `path` query changed its no-path result shape. A legitimate "no import path exists" answer now returns `{"path": null, "reason": "no-import-path"}` at exit 0; the former `{"error": "No import path found."}` key is gone. Any consumer branching on that `error` key silently misreads a valid empty result as a failure.
Fix: test `path === null` or read `reason`. Genuine failures (unknown module) still use the non-zero `error` contract, so the two cases are now distinguishable.
```

|  Exit | Meaning                                                                              |
| ----: | ------------------------------------------------------------------------------------ |
|   `0` | Successful request, including a valid empty result.                                  |
|   `1` | Runtime, index, filesystem, or integration failure.                                  |
|   `2` | Invalid command syntax, option, or approval.                                         |
|   `3` | Requested module or symbol is not indexed where the command distinguishes that case. |
| `127` | No eligible CPython interpreter was found by the dispatcher.                         |

## ⬆️ Upgrade, uninstall, and migration

Upgrade through the runtime's normal plugin manager and start a fresh session:

```bash
claude plugin install codemap-py@borda-ai-rig
codex plugin add codemap-py@borda-ai-rig
```

The second command applies to Codex; run only the command for the runtime you use. Consumer wiring is source-owned and managed through the integration contract; an upgrade does not inject files into an installed cache.

The direct successor to the old `codemap` plugin is `codemap-py`. Do not run both identities in one session — the legacy plugin does not implement the shared-index read/write gate and is rejected as a concurrent producer. Before switching, note the installed `codemap` version so a rollback has a known target. Then close old sessions, uninstall or disable the old plugin, install `codemap-py`, start a fresh session, and run the runtime's integration audit. The project cache is retained and revalidated; no migration step deletes it.

### Rolling back

1. Uninstall or disable `codemap-py` and close its sessions.
2. Reinstall the old `codemap` release from the immutable rollback source — commit `08e06b7a`, legacy `codemap` `0.24.1`.
3. Start a fresh session.
4. Verify the old `/codemap:*` commands resolve again against the retained `.cache/codemap/` project cache.

Rollback never deletes or rewrites the project cache; it is only ever read and revalidated.

### Uninstall

```bash
claude plugin uninstall codemap-py
codex plugin remove codemap-py@borda-ai-rig
```

Run only the command for the runtime you installed into.

## 📚 Maintainer documentation

- [`bin/README.md`](https://github.com/Borda/AI-Rig/blob/main/plugins/codemap-py/bin/README.md) documents shipped launchers, helpers, and compatibility shims.
- [`scripts/README.md`](https://github.com/Borda/AI-Rig/blob/main/plugins/codemap-py/scripts/README.md) documents deterministic package builds, validation, and install probes.
- [The rendered Codemap-py page](https://borda.github.io/AI-Rig/codemap-py/) projects this README into the documentation site.
- [`CHANGELOG.md`](https://github.com/Borda/AI-Rig/blob/main/plugins/codemap-py/CHANGELOG.md) records versioned runtime and documentation changes.

## 🙏 Contributing and feedback

Python hook module docstrings describe their event inputs, local side effects, output envelopes, and failure handling. Pure helper examples run as doctests in the repository pytest suite; hook lifecycle and filesystem behavior use pytest fixtures.

Open an issue in [Borda/AI-Rig](https://github.com/Borda/AI-Rig) with the codemap-py version, CPython version, command, project layout, and the complete error or coverage block. Keep benchmark task IDs and repository-specific fixtures in benchmark evidence, not in shipped plugin docs. Changes to skills, hooks, manifests, or runtime contracts require synchronized README updates and the plugin checks described in the repository authoring guidance.

<a id="claude-agentic-2026-08-04"></a> <a id="codex-structural-2026-08-03"></a> <a id="three-model-comparison"></a>

> Historical benchmark anchors are retained for links from the benchmark record. Current values, methods, and limitations belong in [`benchmarks/README.md`](https://github.com/Borda/AI-Rig/blob/main/benchmarks/README.md); this plugin README intentionally does not duplicate run-specific tables.
