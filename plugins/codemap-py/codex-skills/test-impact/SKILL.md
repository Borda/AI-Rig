---
name: test-impact
description: '`$codemap-py:test-impact qname [--no-mocks]`: affected tests; skip caller/dependency query/exec.'
---

> Before asking, read [User Questions](../../shared/codex-user-questions.md).

# Test Impact

Find minimum static-analysis test set for one changed function or module; never run tests. Output ready-to-run `pytest` command.

- Function (`module::symbol`): BFS reverse calls, direct/transitive tests, plus `mock_patches`.
- Module (bare `module`): BFS reverse imports, plus mocks of any module symbol.

`not_covered` includes dynamic dispatch, hooks, string dispatch, as with `fn-blast`; surface it, never silently omit.

NOT for: all function callers (use `$codemap-py:query-code fn-rdeps <module::symbol> --exclude-tests`); module deps/blast radius (use `$codemap-py:query-code`); test execution.

## Runtime note

Codex has no `bin/` PATH entry or plugin-root variable. Resolve installed root once, substitute `PLUGIN_ROOT`, retain in reasoning; shell state doesn't persist.

## Inputs

`<qname> [--no-mocks]`: `qname` is `module::symbol` or bare dotted module; `--no-mocks` removes mock-only tests. If omitted, use User Questions to request the actual changed `module::symbol` or bare module, accepting `cancel` to stop; wait for the value. Use native free text unless grounded concrete targets can be offered; choosing a format name is not a target. Use first non-flag token only. If tokens remain after `--no-mocks`, warn one symbol accepted per invocation, each remainder needs another invocation.

## Workflow

### 1. Query (and build only when allowed)

```bash
PLUGIN_ROOT/bin/codemap-py query test-impact "<qname>" [--no-mocks]
```

If index missing and `SCAN_NO_AUTOBUILD=1`, report `codemap index missing and SCAN_NO_AUTOBUILD=1 — refusing to auto-build. Build it manually first: $codemap-py:scan-codebase` and stop. Otherwise run `PLUGIN_ROOT/bin/codemap-py index --incremental` in the foreground, then retry once.

### 2. Parse

Read JSON from CLI stdout; never assume first line is JSON — logs can surround it. Use `test_files`, `pytest_cmd`, `via_call`, `via_mock`, `index.not_covered`, `index.hint`.

### 3. Output

For `total == 0`, report: "No tests found via static analysis. Try the full suite or search for the symbol name directly under `tests/`."

For `total > 0`, print:

```text
## Test impact: <qname>

**Affected tests** (<total> files, <via_call> via call/import graph, <via_mock> via mocks):
<test_files as bullet list>

**Run:**
<pytest_cmd>

**Caveat:** dynamic-dispatch / hook-callback callers are not in the static graph — <hint>.
```

Include Caveat only for non-empty `not_covered`. With `total >= 5`, write the same content to `.reports/codex/codemap-py/test-impact-<branch>-<YYYY-MM-DD>.md` and print it. `<branch>` is `git branch --show-current | tr '/' '-'` (`main` if empty/detached). Never overwrite: append `-2`, `-3`, … until free.
