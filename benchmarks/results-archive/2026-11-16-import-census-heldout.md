# Import-edge census, held-out repositories — 2026-11-16

Accuracy of codemap-py import edges on five repositories outside the baseline set, measured with `run-import-census.py` at codemap-py `77d2d79b8e3b3ddc6bc0ff6c750a71047171c64b` (index generation 15), the same scanner as the [2026-11-16 baseline](2026-11-16-import-census.md). Machine-readable record with every disagreement edge and its class: [`2026-11-16-import-census-heldout.json`](2026-11-16-import-census-heldout.json).

## Pre-registration

The repository list was fixed in [`import-census-heldout-repos.json`](../import-census-heldout-repos.json) before any census scoring. At scanner commit `77d2d79b` no tracked file under `benchmarks/` or `plugins/codemap-py/` names any of the five selected repositories, and none is among the baseline repositories the script-directory and project-root-qualified resolution rules were derived from. Pool: flask, click, rich, httpx, black, sphinx, scrapy, fastapi. Rule: keep repositories with 100 to 3000 `.py` files and take the first five alphabetically that are not among the five baseline repositories. Written fallback, not needed: widen the band to 50 to 5000, then take the next pool members alphabetically, never a repository outside the pool. Counts are the `.py` blobs in the GitHub recursive tree of each default-branch head, read with `gh api` GET calls (no tree was truncated).

| Repository        | Commit                                     | `.py` files | Selected |
| ----------------- | ------------------------------------------ | ----------: | -------- |
| psf/black         | `8f4a058eeb2c664ef1cc2847d3882fc4a82fe3fa` |         441 | yes      |
| pallets/click     | `e38188e9e205005d5af5f9e59df51904c984586a` |          91 | no       |
| fastapi/fastapi   | `a1b3be01aadc61c67edc09eed4f6cc1fcc3d3b8f` |        1223 | yes      |
| pallets/flask     | `49943b5a94413a530761350f04e896f76e8d620e` |          83 | no       |
| encode/httpx      | `b5addb64f0161ff6bfe94c124ef76f6a1fba5254` |          60 | no       |
| Textualize/rich   | `3a109ccdffe5b2d0e30684a304fbd388e99ce57b` |         212 | yes      |
| scrapy/scrapy     | `e63a574c767d7a9438c21db158f605f1c44c6e6a` |         546 | yes      |
| sphinx-doc/sphinx | `035a07c32c3241fd3356f4d827800c1f88525272` |         775 | yes      |

Pre-scoring checkpoint: registry SHA-256 `e5065bd42b004e1836317bca7e75a73c1cea477af3aaff5bad5236b38f4ae44b` recorded at 2026-11-16T11:36:34Z, before the census started at 11:36:49Z; the hash was unchanged after scoring. The registry was not committed before scoring, so the checkpoint rests on the recorded hash and timestamp rather than on a commit.

## Result

Command: `python benchmarks/run-import-census.py --scanner-ref HEAD --coordinates benchmarks/import-census-heldout-repos.json`, with `HEAD` resolving to the commit above. No model ran and no repository code was executed. Truth rules and intervals are those of the baseline record: static `ast` plus stdlib `PathFinder` truth over the repository root, `src/` and a script's own directory; parent packages are not edges; 95% Wilson intervals.

| Group    | Repository | Sources | Truth edges | codemap-py edges |  FN |  FP | Recall [95% CI]          | Precision [95% CI]       |
| -------- | ---------- | ------: | ----------: | ---------------: | --: | --: | ------------------------ | ------------------------ |
| held-out | black      |     441 |         166 |              188 |   0 |  22 | 1.000 [0.977, 1.000]     | 0.883 [0.829, 0.921]     |
| held-out | fastapi    |    1223 |        1801 |             1818 |   0 |  17 | 1.000 [0.998, 1.000]     | 0.991 [0.985, 0.994]     |
| held-out | rich       |     212 |         730 |              750 |   0 |  20 | 1.000 [0.995, 1.000]     | 0.973 [0.959, 0.983]     |
| held-out | scrapy     |     546 |        2485 |             2520 |   0 |  35 | 1.000 [0.998, 1.000]     | 0.986 [0.981, 0.990]     |
| held-out | sphinx     |     775 |        1895 |             2101 |   2 | 208 | 0.999 [0.996, 1.000]     | 0.901 [0.887, 0.913]     |
| held-out | **pooled** |    3197 |        7077 |             7377 |   2 | 302 | **1.000 [0.999, 1.000]** | **0.959 [0.954, 0.963]** |

Disagreement classes:

| Group    | Repository | Parent-package FP | Other FP | Script-directory FN | Non-package-path FN | Other FN | Parent-package-adjusted precision [95% CI] |
| -------- | ---------- | ----------------: | -------: | ------------------: | ------------------: | -------: | ------------------------------------------ |
| held-out | black      |                22 |        0 |                   0 |                   0 |        0 | 1.000 [0.980, 1.000]                       |
| held-out | fastapi    |                17 |        0 |                   0 |                   0 |        0 | 1.000 [0.998, 1.000]                       |
| held-out | rich       |                20 |        0 |                   0 |                   0 |        0 | 1.000 [0.995, 1.000]                       |
| held-out | scrapy     |                35 |        0 |                   0 |                   0 |        0 | 1.000 [0.998, 1.000]                       |
| held-out | sphinx     |               197 |       11 |                   0 |                   2 |        0 | 0.995 [0.991, 0.997]                       |
| held-out | **pooled** |               291 |       11 |                   0 |                   2 |        0 | 0.999 [0.997, 0.999]                       |

## Reading

- Pooled recall is 7075/7077. Strict precision is 7075/7377; 291 of the 302 false positives are implicit parent-package `__init__.py` edges, which codemap-py records by design and the truth excludes by definition.
- Strict precision is lowest on black (0.883) because black has few real import edges (166 truth edges over 441 sources, 367 of them formatter test data under `tests/data/`), so its 22 parent-package edges weigh more; adjusted precision is 1.000.
- Both misses are in sphinx and share one cause: `tests/roots/test-ext-viewcode/spam/__init__.py` imports `.mod1` and `.mod2`, but codemap-py did not index that file. Two fixture roots each hold a package named `spam`; the index keeps one module per dotted name and records the dropped file in its `collisions` list (three names, four dropped `__init__.py` files in sphinx). The rule class `non_package_path` is structural; the cause was read from the index.
- The eleven sphinx "other" false positives, checked against source: nine are absolute imports between files of one sphinx test fixture root (for example `from spam.mod1 import Class3` in `tests/roots/test-ext-viewcode/spam/mod3.py`, `import circular_import.a`, `from target.overload import Bar`), and each of those three roots' `conf.py` inserts its directory into `sys.path`. A tenth is `from example.sphinx import DummyClass` in `tests/test_extensions/test_ext_inheritance_diagram.py`, which first inserts `tests/roots/test-ext-inheritance_diagram` into `sys.path`. The static truth does not model these run-time path insertions, so the ten edges are runtime-plausible and count as a truth limitation, as in the baseline's werkzeug `examples/` class. One is a probable scanner error: `tests/roots/test-ext-autodoc/autodoc_dummy_module.py` runs `from dummy import *`, and codemap-py resolved it to `tests/roots/test-inheritance/dummy/__init__.py`, the only `dummy` package in the repository, which lives in a different fixture root.
- Coverage: black has 18 files under `tests/data/` that neither the truth nor the scanner could parse under Python 3.12 (14 in `cases/` with newer grammar such as template strings, type-parameter defaults or `python315.py`; 4 in `miscellaneous/` named as invalid or legacy-syntax input, for example `invalid_header.py` and `python2_detection.py`); both sides skip them. One black file under a `.definitely_exclude` directory is outside the index, and three black edges into `src/_black_version.pyi` fall outside the `.py` universe and are left out of the score. In sphinx four universe files are unindexed (the collision drops above).

## Limits

- One snapshot of five repositories at their default-branch heads on 2026-11-16; the truth is static and differs from a resolver that installs each repository and runs its test configuration.
- The pooled interval treats edges as independent; edges cluster within repositories and fixture roots, so the pooled interval is narrower than the between-repository spread supports. Per-repository intervals are the safer reading.
- Parse coverage depends on the interpreter: this run used Python 3.12, so files that need a newer grammar are skipped on both sides.
- Classes are structural rules; only the sphinx disagreements were adjudicated by hand.
