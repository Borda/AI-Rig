# Call-graph census: instance-method call inference (2026-11-16)

Item 1.12 build measurement. Same harness, registry and seed as the 1.3 census (`2026-11-16-callgraph-census.md`): registry `callgraph-census-repos.json` sha256 `c9439a9add7f8e4d5163f6689b9ec8d01e96a25376677c992d99c4b80abde939`, seed 20261116, reference tool jedi 0.20.1, Python 3.12.16. The scanner was measured as a working tree (`--scanner-dir`), index generation 16, not a commit. Full numbers: `2026-11-16-callgraph-census-instance-inference.json`.

## Verdict: kept

Target fixed before the build: pooled recall at least 0.655 (0.10 above 0.555), pooled precision point estimate at least 0.99, held-out import-edge census unchanged. All three hold.

| Repository | Truth edges | fn-rdeps edges pre / post | FN pre / post | FP pre / post | Recall pre | Recall post [95% CI] | Precision post [95% CI] | Weighted R / P post |
| ---------- | ----------: | ------------------------: | ------------: | ------------: | ---------: | -------------------- | ----------------------- | ------------------- |
| click      |        1598 |               1252 / 1452 |     347 / 147 |         1 / 1 |      0.783 | 0.908 [0.893, 0.921] | 0.999 [0.996, 1.000]    | 0.899 / 0.999       |
| rich       |        1211 |                 308 / 959 |     903 / 264 |        0 / 12 |      0.254 | 0.782 [0.758, 0.804] | 0.987 [0.978, 0.993]    | 0.781 / 0.956       |
| pooled     |        2809 |               1560 / 2411 |    1250 / 411 |        1 / 13 |      0.555 | 0.854 (2398/2809)    | 0.995 (2398/2411)       | -                   |

- Baseline re-run on the unchanged working tree (scanner snapshot, digest `48c685ae…`) reproduced 1.3 exactly: 1559/2809 recall, 1559/1560 precision.
- Post run on the final code (digest `5cbea8d6…`) is identical to the first post run; three scored runs in total (baseline, post, final re-check).
- Held-out import census (`import-census-heldout-repos.json`, 5 repositories, 3194 modules): `direct_imports`, `rdep_count` and module status identical for every module before and after; census pooled and per-repository blocks identical.

## New false positives, adjudicated

All 12 new rich extras are classed `reference_unresolved` by the harness (jedi resolved no definition at the site). Every site was read in the pinned rich checkout; each is a real call of a classmethod through the imported class, so the scanner edge is correct and the reference tool missed it:

| Target                       | Callers (site)                                                                                                                                                                                                                                                                                                                                                                         |
| ---------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `rich.style::Style.null`     | `Tree.__rich_console__` (tree.py:95), `Table._render` (table.py:302), `Table.get_row_style` (table.py:814), `Text.render` (text.py:724), `AnsiDecoder.__init__` (ansi.py:124), `ANSISyntaxTheme.__init__` and `PygmentsSyntaxTheme.get_style_for_token` and `Syntax._get_number_styles` (syntax.py:152/175/176/593), `tests.test_style::test_empty` (:78), `test_without_color` (:208) |
| `rich.color::Color.from_rgb` | `rich.__main__::ColorBox.__rich_console__` (__main__.py:28-29), `tests.test_color::test_from_rgb` (:85)                                                                                                                                                                                                                                                                                |

The one click extra is the pre-existing 1.3 finding (`get_pager_file` function-local import leaking into module scope), unchanged. Scored precision counts all 13 as false positives; adjudicated scanner precision is 2410/2411.

## Remaining misses

| Repository | instance_method |   decorator |       other | re_export | alias | Receiver kinds of remaining instance-method sites                       |
| ---------- | --------------: | ----------: | ----------: | --------: | ----: | ----------------------------------------------------------------------- |
| click      |   145 (was 343) |           0 |           0 | 1 (was 3) |     1 | object expression 159, `super()` 4 (was 371 / `self` 28 / `super()` 25) |
| rich       |   246 (was 755) | 6 (was 133) | 12 (was 15) |         0 |     0 | object expression 303, `cls` 3 (was 1028 / `cls` 3 / `super()` 2)       |

Inherited `self.m()` misses are gone. Remaining object-expression receivers come from function return values, containers, unannotated parameters, attributes of other objects and module-level variables: out of scope for this local, per-function inference, never guessed.

## Method

- Per file (`scanner/receivers.py`): typed call records from `x = Class(...)`, `Class(...).m()`, annotated parameters and locals (`C`, `"C"`, `Optional[C]`, `C | None`, `Union[C, None]`), `self.attr` bound in the class from either, and `super().m()`; class bases as imported. A name bound any other way in the scope (other call result, loop, `with`, `except`, import, nested def, `global`, `del`) gives no record; a declared annotation governs plain rebinds.
- Whole index (`call_inference.py`): the receiver must resolve (through symbol aliases) to an indexed class; the method is looked up in its C3 linearisation, stopping at any base the index does not hold. Recorded class-member edges naming no symbol (`Text.from_ansi()` through an imported class, inherited `self.m()`) are repaired the same way.
- Inferred edges carry `"inferred": "<rule>"`; recorded edges are unchanged.
