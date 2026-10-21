---
name: rename-refs
description: '`$codemap-py:rename-refs`: rename Python names; skip non-Python/unbuilt/local/grep/split/pkg-dir.'
---

> Before asking, read [User Questions](../../shared/codex-user-questions.md).

# Rename Refs

Atomically rename one Python symbol/module: definition, `__all__` exports, caller imports/calls (`fn-rdeps` plus line ranges), non-call references (imports, base classes, `isinstance` targets, callback values, ancestor `__init__.py` re-exports) in files importing the symbol's module or a re-exporting ancestor package, Sphinx refs in `.py`/`.rst`, optional pyDeprecate alias, or hard delete only with exhaustive zero callers.

## Interface

- `symbol <old_qname> <new_qname>`: function/class/method. qname is bare (`MyClass`) or qualified (`MyClass.method`), matched to symbol-local `qualified_name`. The module-qualified form (`module::symbol`) is not accepted by `find-symbol` and returns zero matches. Build `<module>::<qualified_name>` only for Step 2 `fn-rdeps`.
- `module <old_module_path> <new_module_path>`: dotted paths; rename file and imports.
- `--dry-run`: list sites, no edit.
- `--deprecate[=<decorator>]`: symbol only; old-name pyDeprecate `@deprecated` wrapper to new name; pyDeprecate required.
- `--no-deprecate`: symbol only; declines the wrapper; no conflict with `--remove-if-no-callers`.
- `--since <ver>` / `--removed-in <ver>`: decorator versions, default `"?"`.
- `--remove-if-no-callers`: symbol only; delete only with exhaustive zero callers, no zero-caller method `hint`, and no non-call reference.

Reject `--deprecate` with `--remove-if-no-callers` before analysis. Static limits: report a `getattr(obj, "old_name")` search advisory; cross-repo callers require `--deprecate` plus a SemVer bump for public API.

NOT for: index build (`$codemap-py:scan-codebase`); query without rename intent (`$codemap-py:query-code`); non-Python; ABC/Protocol override renames (static imports don't track overrides—review `fn-rdeps`, then rename overrides explicitly). No `--index <path>`; use default project index. For monorepo packages, first run `$codemap-py:scan-codebase --root <pkg>`.

## Runtime note

Codex has no `bin/` PATH entry or plugin-root variable. Resolve installed root once, substitute `PLUGIN_ROOT`, retain in reasoning; shell state doesn't persist. Route missing input, stale-index, ambiguous-match, delete/abort, and dry-run/apply/abort questions through User Questions; preserve every feasible choice, wait for valid bound answer before dependent changes.

## Workflow

> Stop routing follows source edit state, including a deletion or `git mv`. Before the first source write, any stop ends this invocation; re-invoke only after correcting the reported prerequisite. Never finish a stopped rename by hand with grep, direct edits or a file move, in any language. A placeholder-substitution self-check only retries its block after substituting the named value. If a stop fires after a source write, stop further rename edits, finish the Step 6 checks that can still run, then report `⚠ Rename incomplete` in Step 7 with the changed files and remaining work; never restart an earlier edit step. A failed incremental re-scan is advisory: continue available verification and repeat the warning in Step 7; unavailable verification still makes the rename incomplete.

### 1. Parse and check freshness

Check for `.py`/`.pyi` files from the repository root (current directory outside Git), excluding `.git`, `.venv`, `venv` and `node_modules`. If none exist, report `! UNSUPPORTED — rename-refs is Python-only; stop, do not rename by hand` and end the invocation before index queries.

Accept only the listed flags; reject other `--` tokens, symbol-only flags under `module`, and the incompatible pair. Then run:

```bash
PLUGIN_ROOT/bin/codemap-py query find-symbol "<old_ref>" --limit 0
```

Keep that result for Step 2. `index.stale: true` means mismatch; `index.query_complete: false` plus `index.completeness_reason: "stale"` corroborates. Ask to proceed with possibly incomplete callers or abort and re-run `$codemap-py:scan-codebase`. If the `index` block is absent, warn and proceed cautiously; never assume fresh.

### 2. Resolve targets

For `symbol`, reuse `matches`; each has `{name, qualified_name, type, module, path, start_line, end_line, source}`. Use path/lines for edits and `qualified_name` for exact filtering. Zero: report `Symbol '<old_ref>' not found` and stop. Multiple: list name/type/module/path, ask selection, wait. Then query:

```bash
PLUGIN_ROOT/bin/codemap-py query fn-rdeps "<module>::<qualified_name>"
```

It returns `{qname, called_by:[{caller, module, path}], count, index:{query_complete,...}}`, plus `hint` for a method or constructor with zero static callers; resolve caller line ranges via `query symbol <caller>` in Step 4. Read completeness forward-first: `index.query_complete`, then legacy `index.exhaustive` only if absent; not complete belongs in the report. A `hint` means zero static callers is not zero callers — instance calls (`obj.method()`), overrides, subclass constructors, and implicit protocol (dunder) calls such as `len(x)` or `x == y` are never resolved statically; print it in the report. Also print `index.hint`, the search for references outside the static AST such as hook-registered callers; it never blocks the rename on its own.

`called_by` holds call edges only: a subclass base, `isinstance` target, callback value, plain import or ancestor `__init__.py` re-export never appears in it. Save the narrowed `find-symbol` answer and the `fn-rdeps` answer as `find-symbol.json` and `fn-rdeps.json` in a private temporary directory (`mktemp -d`). Write `batch-in.json` there holding `{"cmd":"list","args":["--limit","0"]}` plus one `{"cmd":"rdeps","args":["<pkg>"]}` for the symbol's module and for each ancestor package (`a.b.c` → `a.b.c`, `a.b`, `a`): a re-export lives in an ancestor `__init__.py`, and its users import the package. Then list every whole-word old name in the files that call, define or import it:

```bash
PLUGIN_ROOT/bin/codemap-py query batch "<tmp>/batch-in.json" > "<tmp>/batch.json"
"PLUGIN_ROOT/bin/python" "PLUGIN_ROOT/bin/find_module_refs.py" --kind name --name "<old_name>" --files-from-json "<tmp>/fn-rdeps.json" --files-from-json "<tmp>/find-symbol.json" --files-from-json "<tmp>/batch.json"
```

A non-zero exit from either means reference sites are unknown: stop before any source edit. Each printed `path:line:text` is a candidate site; comments, docstrings and strings are included, and a same-name symbol imported from another module stays unedited. For a method, only attribute accesses on an instance of its class count. Report the site count with the callers in Step 3.

For `module`, query:

```bash
PLUGIN_ROOT/bin/codemap-py query rdeps "<old_module_path>"
```

Read completeness identically.

### 3. Report and confirm

Print old→new, type/definition (symbol), static caller count/files, Step 4 doc-ref search, and `getattr`/cross-repo limits. If incomplete: "index non-exhaustive — some callers may not appear above."

Caller count alone never blocks a complete migration. Enumerate and preflight every known caller; for a long list, save the complete inventory to `.reports/codex/codemap-py/rename-refs-blast-<branch>-<YYYY-MM-DD>.md` and print its path. `<branch>` is `git branch --show-current | tr '/' '-'` (`main` if empty/detached). Never overwrite: append `-2`, `-3`, … until free. The inventory supports confirmation and complete execution; it never permits a partial migration.

For `--remove-if-no-callers`, before edits: callers found → report count and stop (remove callers first or drop the flag); incomplete/missing completeness → report `$codemap-py:scan-codebase` required and stop; zero with a `hint` → report that zero static callers is not proof for a method or constructor, print the hint's search, and stop (delete manually only if it finds no reference); a protocol-method (dunder) hint names no search — Python calls it implicitly, so report it is never deleted on zero callers and stop; complete zero without a `hint` → first run the non-call reference check, also under `--dry-run`. `fn-rdeps` counts call edges only: imports, base classes, `isinstance`, callback values and re-exports never appear in it. Search every `.py`/`.pyi` file outside `.git`, `.venv` and `node_modules` for `<old_name>` (last dotted part of `<old_qname>`) as a whole word, at most 50 hits:

```bash
grep -rnw --include='*.py' --include='*.pyi' --exclude-dir=.git --exclude-dir=.venv --exclude-dir=node_modules -- "<old_name>" . | head -50
```

Any hit other than the definition's own `def`/`class` line → report `<N> non-call reference(s) to <old_name> — delete would leave ImportError/NameError` and stop. No such hit → ask delete/abort; under `--dry-run`, record "would delete `<old_qname>` — no callers, exhaustive" in the dry-run report instead, with no question and no edit. Before deletion, verify `start_line` names the expected symbol; mismatch aborts. Delete definition plus preceding decorators, skip Step 4, and continue at Step 6.

For `--dry-run`, write would-change sites to `.reports/codex/codemap-py/rename-refs-dry-<branch>-<YYYY-MM-DD>.md` using the same branch and never-overwrite rule; print path, ask for re-invocation without `--dry-run` or stop. Otherwise ask apply/abort and stop on abort.

Before apply confirmation, resolve every known caller before any source edit: for symbols, query every caller and verify its unique source range and expected imports/calls; for modules, inspect every dependent file and matching import. For a missing, ambiguous or stale site, stop before any source edit with source unchanged. Retain these locations for apply; recheck them before mutation. The complete preflight applies to both subcommands regardless of caller count.

### 4. Apply symbol edits

1. At `start_line`, rename `def old_name(` / `class OldName(...)` / `class OldName:`; for methods, match inside class. Rename `@old_name.setter`/`.deleter` too: an orphaned descriptor raises `AttributeError` at class definition. Rename `@typing.overload def old_name(` in the file and sibling `.pyi`; `find-symbol` omits these.
2. In package `__init__.py`, rename quoted old names in `__all__`.
3. For each `called_by`, split `caller` (`module::qualified_name`; `query symbol` never matches that form) at `::`, run `PLUGIN_ROOT/bin/codemap-py query symbols "<caller_module>"` once per module, and keep the single entry whose `qualified_name` equals the caller's; none or several stops the operation. Within its range, fix each file's module import once, then qualified `X.old_name(` and bare `old_name(`. Never bare-replace outside the confirmed range. Use the complete preflight inventory; never skip a known caller. Unexpected drift stops the operation; report any partial edits as incomplete, not a successful rename. Then, for every file in the Step 2 reference-site list (callers included): find the line bringing the old name in — `from <module> import …`, a relative import, or `from <ancestor package> import …` whose `__init__.py` re-exports it. Another origin is a same-name symbol: leave that file unedited. Origin confirmed: edit that import line once and every listed whole-word occurrence — base classes, `isinstance` targets, callback values, `__all__` strings, re-export lines, annotations, comments and docstrings naming it; for a method, only attribute accesses on its class's instances. A line that cannot be classified stays unedited for Step 6 to report.
4. Search `.py`/`.rst` Sphinx `:func:`/`:class:`/`:meth:`/`:mod:`/`:attr:` old-name roles; edit only matching module context.
5. For `--deprecate`, after definition rename run `PLUGIN_ROOT/bin/gen_deprecation_wrapper.py`; insert after new definition. It maps class to `@deprecated_class(target=NewName, ...)` (keeps `isinstance`) and function/method to `@deprecated(target=new_fn, ...)`. Missing pyDeprecate makes target import fail; report advisory in Step 6.

### 5. Apply module edits

1. For non-git or untracked source, stop before edits and report that tracked-file module moves require Git tracking; never add, commit, or stash automatically. For tracked source, preserve uncommitted user edits and use `git mv -- <old_file_path> <new_file_path>` after confirming that the source exists, the destination is unoccupied on disk and in the index, and neither path is conflicted. Do not force a move. Prefer indexed path over dotted conversion (which can be wrong under `src/` layouts). Package directories (`__init__.py`) are out of scope: print direct `git mv`, do not attempt.
2. Rename direct `import mypackage.old_name` (including `as X`) and `from mypackage.old_name import ...`, at line start or indented inside a function, `try` or `if TYPE_CHECKING:` block, plus attribute uses after a parent import (`mypackage.old_name.func()` following `import mypackage`), which carry no import line naming the module.
3. Rename same-package `__init__.py` `from .old_name import ...` after directory check.
4. Search full dotted path in `pyproject.toml`/`setup.cfg`, editing package/install requirements only; never bare basename.
5. Rename Sphinx `:mod:` full paths; basename fallback needs matching module context.

### 6. Re-scan and verify

```bash
PLUGIN_ROOT/bin/codemap-py index --incremental
PLUGIN_ROOT/bin/codemap-py query find-symbol "<old_ref>" --limit 0    # symbol
PLUGIN_ROOT/bin/codemap-py query rdeps "<old_ref>"                     # module
```

For `symbol`, `find-symbol` sees definitions only, never references. Count every whole-word old name still in `*.py`/`*.pyi` files: the whole tree for a function or class; for a method, add the three Step 2 `--files-from-json` arguments, since a bare method name recurs across unrelated classes. With `--deprecate`, add `--alias-in "<definition path>"` so the alias stub's own `def`/`class` line is not counted:

```bash
"PLUGIN_ROOT/bin/python" "PLUGIN_ROOT/bin/find_module_refs.py" --kind name --name "<old_name>"
```

Classify each hit: an unedited reference to the renamed symbol (import from its module or a re-exporting package, base, call, `isinstance` target, callback value, comment or docstring naming it) or a same-name symbol of another origin.

Verify the renamed provider and every known caller with the project's relevant import/call checks. Unresolved known callers, any unedited reference, a post-write stop, or failing/unavailable verification means incomplete migration. If re-scan fails, continue available checks; index results are advisory, not authoritative. Expect old name absent except deprecated alias.

### 7. Summary

For an incomplete migration, report `⚠ Rename incomplete` with the changed files, verified/known caller counts, each unedited reference as `file:line`, failed or unavailable checks, and next actions. Repeat any re-scan warning. Otherwise, a success line always states the residual whole-word count, each residual being a same-name symbol of another origin, and never reads as an unqualified `Renamed`. List residual hits as advisory (dynamic/string/template/out-of-scope refs). Report renamed pair, files, calls, doc refs, alias path/line when used; always flag `getattr` literal search, external consumers without alias, skipped callers, and residual hits when applicable.
