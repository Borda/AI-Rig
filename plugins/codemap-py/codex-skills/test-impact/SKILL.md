---
name: test-impact
description: '`$codemap-py:test-impact [qname] [--no-mocks]`: affected tests for a target or working-tree changes; skip caller/dependency query/exec.'
---

> Before asking, read [User Questions](../../shared/codex-user-questions.md).

# Test Impact

Find minimum static-analysis test set for a changed function/module or the union of working-tree Python changes; never run tests. Output ready-to-run `pytest` command.

- Function (`module::symbol`): BFS reverse calls, direct/transitive tests, plus `mock_patches`.
- Module (bare `module`): BFS reverse imports, plus mocks of any module symbol.

`not_covered` includes dynamic dispatch, hooks, string dispatch, as with `fn-blast`; surface it, never silently omit.

NOT for: all function callers (use `$codemap-py:query-code fn-rdeps <module::symbol> --exclude-tests`); module deps/blast radius (use `$codemap-py:query-code`); test execution.

## Runtime note

Codex has no `bin/` PATH entry or plugin-root variable. Resolve installed root once, substitute `PLUGIN_ROOT`, retain in reasoning; shell state doesn't persist. Invoke Python helpers through the quoted packaged launcher; on native Windows use `bin/python.cmd` with host-shell quoting. `test_impact_union.py` resolves `codemap-py` on PATH: prepend the resolved plugin's `bin/` only for that helper process so it selects the same installed launcher, never change persistent PATH. The block below uses POSIX environment syntax; on native Windows supply the equivalent child-process environment with the host shell's quoting and path separator.

## Inputs

`[<qname>] [--no-mocks]`: `qname` is `module::symbol` (dotted or once-defined bare name resolves; module-suffix or bare method name → candidates) or bare dotted module; `--no-mocks` removes mock-only tests. If omitted, inspect the working-tree change set in Step 1 before asking. Use first non-flag token only. If tokens remain after `--no-mocks`, warn one symbol accepted per invocation, each remainder needs another invocation. Reject unknown flags and malformed quoting.

## Workflow

### 1. Resolve scope

For an explicit target, continue to Step 2. For an omitted target, capture stdout and stderr plus the exit status from `PLUGIN_ROOT/bin/codemap-py query diff-impact` into private temporary files. This inspects tracked staged and unstaged Python changes against `HEAD`; untracked files are excluded. Summarize the captured stdout with the packaged helper, substituting the actual status, paths and a bare session-scoped output prefix:

```bash
"PLUGIN_ROOT/bin/python" "PLUGIN_ROOT/bin/summarize_test_impact.py" --diff-impact --rc <status> --stderr-file "<captured-stderr>" --output-prefix "<prefix>" < "<captured-stdout>"
```

Only after a successful query and parsed summary, count all changed Python files: `len(changed_modules) + len(unmapped_files) > 1` selects the whole-change route. Use `test_impact.test_files`, `test_impact.total`, `test_impact.pytest_cmd` and top-level `index`; label the scope `working-tree changes`, skip Step 2, and omit `via_call`/`via_mock` counts rather than inventing them.

Report `unmapped_files` and every `changed_modules[].errors` entry as coverage gaps even when continuing to a question. Failed subqueries may be omitted from the default union without listed errors: always label it a static selection, never proof of complete coverage, and retain dynamic-dispatch/hook/string-dispatch caveats even when `index.not_covered` is absent.

For this whole-change route with `--no-mocks`, use `bin/test_impact_union.py` instead of the default diff union, which includes mock-only tests. Pass the original captured diff-impact stdout, not the compact summary: the helper batches each changed module and each `changed_symbols` target with `--no-mocks`, deduplicates test files, and emits a host-quoted pytest command. Run only after a successful parsed diff-impact result; any failed batch query stops the route, never yields an empty test set or a single selected module. Preserve diff gaps, `diff_index`, batch `index` and every `target_caveats` entry in Step 4.

```bash
PATH="PLUGIN_ROOT/bin:$PATH" "PLUGIN_ROOT/bin/python" "PLUGIN_ROOT/bin/test_impact_union.py" "<captured-stdout>"
```

A nonzero diff-impact exit or unparsable result → relay the diagnostic and `whole-change analysis unavailable`, then use User Questions below; never render an empty set from that failure or reuse the failed capture for a union. With zero or one changed Python file, also use User Questions to request the actual changed `module::symbol` or bare module, accepting `cancel` to stop; wait for the value. Offer up to three grounded targets from changed Python files, otherwise native free text; choosing a format name is not a target. Preserve the original `--no-mocks` flag after the answer.

### 2. Query (and build only when allowed)

```bash
PLUGIN_ROOT/bin/codemap-py query test-impact "<qname>" [--no-mocks]
```

If index missing and `SCAN_NO_AUTOBUILD=1`, report `codemap index missing and SCAN_NO_AUTOBUILD=1 — refusing to auto-build. Build it manually first: $codemap-py:scan-codebase` and stop. Otherwise run `PLUGIN_ROOT/bin/codemap-py index --incremental` in the foreground, then retry once.

### 3. Parse

Read JSON from CLI stdout; never assume first line is JSON — logs can surround it. Treat `test_files` as the authoritative argv list. `pytest_cmd` is quoted for a POSIX shell on POSIX hosts and PowerShell on native Windows; for another shell, build its command from `test_files` with that shell's quoting, never reuse incompatible escaping. Use `test_files`, `pytest_cmd`, `via_call`, `via_mock`, `index.not_covered`, `index.hint`. Non-zero exit is never an empty result: error JSON is on stdout. With `rejected_target` (typo, ambiguous name, module not indexed) report `error` plus every `candidates`/`suggestions` entry and ask for one exact target — no rebuild advice. Exit 2 without it is a rejected argument. Only other failures (missing/corrupt index) get rebuild advice.

Read `index.query_complete`, `index.stale` and `index.completeness_reason` from the same answer. A missing signal (`null`) stays absent, never coerced to a boolean; `completeness_reason` may be absent on compact complete answers by design. Non-JSON output is a loud failure, never a zero-test default.

### 4. Output

For a single target, prepend `Scope: covers <qname> only; N other changed modules not analysed.` to either branch and any written report. Count tracked staged and unstaged Python changes against `HEAD`, excluding the selected module using indexed paths; when mapping/counting is unreliable, say `other changed modules not analysed; count unknown`. Never describe that command as covering the entire change. For a whole-change result, use `working-tree changes` as the title and report scope, retain every coverage gap above, and omit the call/mock breakdown.

For both output branches and written reports, surface `query_complete: false` or `stale: true` independently of `total` and `not_covered`: prepend `⚠ Index completeness: <completeness_reason>` or `index incomplete/stale — see refresh output` when no reason is present. For `--no-mocks` unions, inspect both `diff_index` and batch `index` plus each target's caveats. Render only parsed successful results; do not re-query to estimate counts.

For `total == 0`, report: "No tests found via static analysis. Try the full suite or search for the symbol name directly under `tests/`." Retain scope, static-selection and coverage-gap caveats; incomplete/stale evidence means the index could not prove affected tests, never proof that none exist.

For `total > 0`, print:

```text
## Test impact: <qname>

**Affected tests** (<total> files, <via_call> via call/import graph, <via_mock> via mocks):
<test_files as bullet list>

**Run:**
<pytest_cmd>

**Caveat:** dynamic-dispatch / hook-callback callers are not in the static graph — <hint>.
```

Include the static-analysis Caveat for non-empty `not_covered`, and always for whole-change analysis. Completeness and coverage-gap caveats above apply independently on both branches. With `total >= 5`, write a YAML `---` block (`Title`, `Date`, `Scope`, `Focus`, `Agents`, `Outcome`, `Confidence`, `Next steps`, `Path`) followed by the same content to `.reports/codex/codemap-py/test-impact-<branch>-<YYYY-MM-DD>.md` and print it. `<branch>` is `git branch --show-current | tr '/' '-'` (`main` if empty/detached). Never overwrite: append `-2`, `-3`, … until free.
