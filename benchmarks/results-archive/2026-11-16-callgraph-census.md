# Call-graph census: `fn-rdeps` against jedi, 2026-11-16

Edge recall and precision of codemap-py `fn-rdeps` callers on two pinned public repositories, measured against an independent static reference (jedi 0.20.1 `goto` from every candidate call site). Harness: `benchmarks/run-callgraph-census.py`; registry: `benchmarks/callgraph-census-repos.json`; full result: `2026-11-16-callgraph-census.json` beside this file.

## Pre-registration

- Registry written and hashed before any truth build or scoring: SHA-256 `e29181aac702f7cca1f266ce55959d9d2d75079ae971d4833e0eaf4cad4496f2`, 2026-11-16T12:01:59Z.
- Amended once after a scored run (registry `amendments` entry), new SHA-256 `c9439a9add7f8e4d5163f6689b9ec8d01e96a25376677c992d99c4b80abde939`, 2026-11-16T12:11:31Z. Only the reference tool's environment and search path changed; repositories, seed, strata, truth and scoring rules did not.
- Amendment reason: jedi picked its environment from `$VIRTUAL_ENV`, an environment that installs click and rich, and did not search `src/`. `click.edit(...)` in click's tests resolved to the installed copy's `click/termui.py:848` instead of the checkout's `src/click/termui.py:848`. That first run is invalid and kept only as a record: click recall 0.636 [0.579, 0.690], precision 0.167 [0.146, 0.190]; rich recall 0.261 [0.237, 0.286], precision 1.000 [0.988, 1.000]. The amended backend stops the run when any resolution lands in an installed package; the valid run raised none.
- Scanner: `HEAD` 77d2d79b8e3b3ddc6bc0ff6c750a71047171c64b, index generation 15. Python 3.12.16.

## Design

- Repositories: rich 3a109ccdffe5 (the import-census held-out member with the fewest `.py` files) and click e38188e9e205 (first pool member alphabetically outside that set).
- Targets: public top-level functions and direct methods of top-level classes (no qualified-name component starting with `_`), test modules included, module names as the index records them. click 991, rich 1267.
- Truth: each `ast.Call` with a `Name` or `Attribute` callee whose terminal identifier names a target or an import-bound name is resolved by jedi; each resolution to a target adds a (target, caller) edge, the caller being the enclosing codemap-py symbol. Imports, callbacks, base classes and annotations are not call positions. Module-scope call sites have no caller and are not scored (click 28, rich 475 sites).
- Sample: per repository, 60 targets per stratum of the reference tool's own distinct-caller count (0, 1, 2-5, 6+), topped up to 200; seed 20261116. Every truth caller name exists in the index symbol set (0 vocabulary gaps).

## Result

| Repository | Targets | Sample 0 / 1 / 2-5 / 6+ | Truth edges | fn-rdeps edges |   FN |  FP | Recall [95% CI]      | Precision [95% CI]   | Weighted recall | Weighted precision |
| ---------- | ------: | ----------------------: | ----------: | -------------: | ---: | --: | -------------------- | -------------------- | --------------: | -----------------: |
| click      |     991 |       60 / 60 / 60 / 33 |        1598 |           1252 |  347 |   1 | 0.783 [0.762, 0.802] | 0.999 [0.995, 1.000] |           0.763 |              0.999 |
| rich       |    1267 |       60 / 60 / 60 / 58 |        1211 |            308 |  903 |   0 | 0.254 [0.231, 0.280] | 1.000 [0.988, 1.000] |           0.270 |              1.000 |
| **pooled** |    2258 |                         |        2809 |           1560 | 1250 |   1 | 0.555 [0.537, 0.573] | 0.999 [0.996, 1.000] |                 |                    |

Intervals treat edges as independent; edges cluster per target and per repository, so the true uncertainty is wider. Weighted columns reweight strata by `N_h / n_h` (population estimate, point only).

## Missed edges by class

Registered precedence: dynamic, instance method, alias, re-export, decorator, other; an edge takes the highest class among its call sites.

| Class           | click | rich | pooled | Share of missed edges |
| --------------- | ----: | ---: | -----: | --------------------: |
| instance method |   343 |  755 |   1098 |                 87.8% |
| decorator       |     0 |  133 |    133 |                 10.6% |
| other           |     0 |   15 |     15 |                  1.2% |
| re-export       |     3 |    0 |      3 |                  0.2% |
| alias           |     1 |    0 |      1 |                  0.1% |
| dynamic         |     0 |    0 |      0 |                  0.0% |

- Instance-method misses are 1098 of 1250 missed edges (87.8%, Wilson [0.859, 0.895]); click 343/347 (98.8%), rich 755/903 (83.6%). Every one of these edges has only instance-method call sites. Receiver of those sites: an object expression in 1399, `self` 28, `super()` 27, `cls` 3.
- `decorator` is a label, not a cause: adjudicated cases are `ImportedClass.classmethod()` or `ImportedClass.staticmethod()` calls (`Measurement.get`, `Padding.unpack`, `Text.from_ansi`) that `fn-rdeps` does not report.
- `dynamic` is zero by construction: the reference resolves only `Name` and `Attribute` callees.
- The one extra edge is a scanner error (below).

## Adjudication

20 records, read against source: 13 drawn by the registered seed (6 missed per repository, 1 extra; rich has no extra edge), 7 purposive from minority classes.

| Repository | Edge (target ← caller)                                                                                | Class              | Verdict                                                                                                                                                                              |
| ---------- | ----------------------------------------------------------------------------------------------------- | ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| click      | `_termui_impl::Editor.edit_files` ← `tests.test_termui::test_editor_unclosed_quote`                   | instance method    | Real call on `Editor(...)` result; scanner miss                                                                                                                                      |
| click      | `core::Context.lookup_default` ← `tests.test_defaults::test_lookup_default_returns_hides_sentinel`    | instance method    | `ctx = click.Context(cmd)` then `ctx.lookup_default`; scanner miss                                                                                                                   |
| click      | `core::Group.get_command` ← `core::_complete_visible_commands`                                        | instance method    | `multi = t.cast(Group, ...)`; static target correct; scanner miss                                                                                                                    |
| click      | `core::Group.group` ← `tests.test_formatting::test_wrapping_long_options_strings`                     | instance method    | `@cli.group()` on a `click.group()` result; scanner miss                                                                                                                             |
| click      | `types::ParamType.fail` ← `types::_NumberRangeBase.convert`                                           | instance method    | Inherited `self.fail(...)`; scanner miss                                                                                                                                             |
| click      | `types::ParamType.to_info_dict` ← `types::_NumberRangeBase.to_info_dict`                              | instance method    | `super().to_info_dict()`; scanner miss                                                                                                                                               |
| click      | `_termui_impl::get_pager_file` ← `termui::echo_via_pager` (extra)                                     | resolved elsewhere | Scanner error: the call binds to `termui.get_pager_file` (module level); the `from ._termui_impl import get_pager_file` is local to the wrapper function but leaks into module scope |
| click      | `termui::get_pager_file` ← `termui::echo_via_pager` (purposive)                                       | re-export          | Same site, the matching miss; the label comes from the same leaked import binding; scanner miss                                                                                      |
| click      | `core::Group.get_command` ← `tests.typing.typing_aliased_group::AliasedGroup.get_command` (purposive) | re-export          | `click.Group.get_command(self, ...)` through the package re-export; scanner miss                                                                                                     |
| click      | `core::Group.get_command` ← `examples.aliases.aliases::AliasedGroup.get_command` (purposive)          | re-export          | Same pattern; scanner miss                                                                                                                                                           |
| rich       | `console::Console.begin_capture` ← `tests.test_live::test_growing_display_transient`                  | instance method    | Receiver from `create_capture_console()` return type; scanner miss                                                                                                                   |
| rich       | `console::Console.end_capture` ← `tests.test_tree::test_render_single_branch`                         | instance method    | `console = Console(...)`; scanner miss                                                                                                                                               |
| rich       | `console::Console.render` ← `align::Align.__rich_console__`                                           | instance method    | Parameter annotated `"Console"`; scanner miss                                                                                                                                        |
| rich       | `console::ConsoleOptions.update_width` ← `constrain::Constrain.__rich_measure__`                      | instance method    | Parameter annotated `"ConsoleOptions"`; scanner miss                                                                                                                                 |
| rich       | `table::Table.add_row` ← `examples.fullscreen::make_sponsor_message`                                  | instance method    | Receiver from classmethod `Table.grid(...)`; scanner miss                                                                                                                            |
| rich       | `text::Text.from_ansi` ← `tests.test_ansi::test_decode_example`                                       | decorator          | `Text.from_ansi(...)` on the imported class; scanner miss                                                                                                                            |
| rich       | `measure::Measurement.get` ← `padding::Padding.__rich_console__` (purposive)                          | decorator          | `Measurement.get(...)` classmethod on the imported class; scanner miss                                                                                                               |
| rich       | `padding::Padding.unpack` ← `tests.test_padding::test_unpack` (purposive)                             | decorator          | `Padding.unpack(3)` staticmethod; scanner miss                                                                                                                                       |
| rich       | `measure::Measurement.get` ← `align::Align.__rich_measure__` (purposive)                              | decorator          | Same pattern; scanner miss                                                                                                                                                           |
| rich       | `segment::Segment.apply_style` ← `align::Align.__rich_console__` (purposive)                          | decorator          | `Segment.apply_style(iter_segments, style)` classmethod on the imported class (`rich/align.py:232`); scanner miss                                                                    |

20 of 20 settled: 19 scanner misses, 1 scanner false positive, 0 reference-tool errors. The purposive records were picked by a scratch script that classed each edge over every same-name and import-bound call site of the caller; it labelled the `apply_style` edge `alias`, while the registered rule over the truth's own sites gives `decorator` (the census reports no rich `alias` miss). The class column shows the registered class.

## Limits

- The reference tool has its own error, which is not measured here beyond the adjudication. It left these candidate call sites unresolved: click 557 of 3561, rich 371 of 6505. Truth edges behind those sites are absent, so recall can be biased in either direction, and an unresolved site behind a real `fn-rdeps` edge would count as a false positive (one FP was observed).
- Only sites whose terminal name is a target name or an import-bound name are resolved. Calls through assignment aliases (`g = f; g()`) are absent from the truth.
- Two repositories, one snapshot each. Rich supplies 903 of the 1250 missed edges, 755 of them instance-method, so pooled figures lean on rich.
- The `decorator` and `re-export` labels are mechanical. Adjudication shows `decorator` misses are class-qualified calls to classmethods and staticmethods, and one `re-export` miss is a leaked function-local import.
- Before the hash, a 2-target `fn-rdeps` batch was run on a rich index built for the import census, to learn the output shape (`rich.console::Console.print`, `rich.markup::escape`). No other `fn-rdeps` output was seen before registration.
