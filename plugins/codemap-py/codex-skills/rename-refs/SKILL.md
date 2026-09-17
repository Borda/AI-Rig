---
name: rename-refs
description: '`$codemap-py:rename-refs`: rename Python names; skip non-Python/unbuilt/local/grep/split/pkg-dir.'
---

> Before asking, read [User Questions](../../shared/codex-user-questions.md).

# Rename Refs

Atomically rename one Python symbol/module: definition, `__all__` exports, caller imports/calls (`fn-rdeps` plus line ranges), Sphinx refs in `.py`/`.rst`, optional pyDeprecate alias, or hard delete only with exhaustive zero callers.

## Interface

- `symbol <old_qname> <new_qname>`: function/class/method. qname is bare (`MyClass`) or qualified (`MyClass.method`), matched to symbol-local `qualified_name`. The module-qualified form (`module::symbol`) is not accepted by `find-symbol` and returns zero matches. Build `<module>::<qualified_name>` only for Step 2 `fn-rdeps`.
- `module <old_module_path> <new_module_path>`: dotted paths; rename file and imports.
- `--dry-run`: list sites, no edit.
- `--deprecate[=<decorator>]`: symbol only; old-name pyDeprecate `@deprecated` wrapper to new name; pyDeprecate required.
- `--since <ver>` / `--removed-in <ver>`: decorator versions, default `"?"`.
- `--remove-if-no-callers`: symbol only; delete only with exhaustive zero callers.

Reject `--deprecate` with `--remove-if-no-callers` before analysis. Static limits: report a `getattr(obj, "old_name")` search advisory; cross-repo callers require `--deprecate` plus a SemVer bump for public API.

NOT for: index build (`$codemap-py:scan-codebase`); query without rename intent (`$codemap-py:query-code`); non-Python; ABC/Protocol override renames (static imports do not track overrides—review `fn-rdeps`, then rename overrides explicitly). No `--index <path>`; use default project index. For monorepo packages, first run `$codemap-py:scan-codebase --root <pkg>`.

## Runtime note

Codex has no `bin/` PATH entry or plugin-root variable. Resolve the installed root once, substitute `PLUGIN_ROOT`, and retain it in reasoning; shell state does not persist. Route missing input, stale-index, ambiguous-match, delete/abort, and dry-run/apply/abort questions through User Questions; preserve every feasible choice and wait for a valid bound answer before dependent changes.

## Workflow

### 1. Parse and check freshness

Accept only the listed flags; reject other `--` tokens, and the incompatible pair. Then run:

```bash
PLUGIN_ROOT/bin/codemap-py query find-symbol "<old_ref>" --limit 0
```

Keep that result for Step 2. `index.stale: true` means mismatch; `index.query_complete: false` plus `index.completeness_reason: "stale"` corroborates. Ask to proceed with possibly incomplete callers or abort and re-run `$codemap-py:scan-codebase`. If the `index` block is absent, warn and proceed cautiously; never assume fresh.

### 2. Resolve targets

For `symbol`, reuse `matches`; each has `{name, qualified_name, type, module, path, start_line, end_line, source}`. Use path/lines for edits and `qualified_name` for exact filtering. Zero: report `Symbol '<old_ref>' not found` and stop. Multiple: list name/type/module/path, ask selection, wait. Then query:

```bash
PLUGIN_ROOT/bin/codemap-py query fn-rdeps "<module>::<qualified_name>"
```

It returns `{qname, called_by:[{caller, module, path}], count, index:{query_complete,...}}`; resolve caller line ranges via `query symbol <caller>` in Step 4. Read completeness forward-first: `index.query_complete`, then legacy `index.exhaustive` only if absent; not complete belongs in the report.

For `module`, query:

```bash
PLUGIN_ROOT/bin/codemap-py query rdeps "<old_module_path>"
```

Read completeness identically. Honor `--remove-if-no-callers` only when passed and complete.

### 3. Report and confirm

Print old→new, type/definition (symbol), static caller count/files, Step 4 doc-ref search, and `getattr`/cross-repo limits. If incomplete: "index non-exhaustive — some callers may not appear above."

With >50 callers, write full list to `.reports/codex/codemap-py/rename-refs-blast-<branch>-<YYYY-MM-DD>.md`, print it, edit first 50 only, and call 51–N "skipped callers" in Step 6. `<branch>` is `git branch --show-current | tr '/' '-'` (`main` if empty/detached). Never overwrite: append `-2`, `-3`, … until free; this file is the manual-edit record.

For `--remove-if-no-callers`, before edits: callers found → report count and stop (remove callers first or drop the flag); incomplete/missing completeness → report `$codemap-py:scan-codebase` required and stop; complete zero → ask delete/abort. Before deletion, verify `start_line` names the expected symbol; mismatch aborts. Delete definition plus preceding decorators, skip Step 4, and continue at Step 6.

For `--dry-run`, write would-change sites to `.reports/codex/codemap-py/rename-refs-dry-<branch>-<YYYY-MM-DD>.md` using the same branch and never-overwrite rule; print path, ask for re-invocation without `--dry-run` or stop. Otherwise ask apply/abort and stop on abort.

### 4. Apply symbol edits

1. At `start_line`, rename `def old_name(` / `class OldName(...)` / `class OldName:`; for methods, match inside class. Rename `@old_name.setter`/`.deleter` too: an orphaned descriptor raises `AttributeError` at class definition. Rename `@typing.overload def old_name(` in the file and sibling `.pyi`; `find-symbol` omits these.
2. In package `__init__.py`, rename quoted old names in `__all__`.
3. For each `called_by`, run `PLUGIN_ROOT/bin/codemap-py query symbol "<caller_qname>"`; within its range, fix each file's module import once, then qualified `X.old_name(` and bare `old_name(`. Never bare-replace outside the confirmed range. Warn/skip a caller absent from the index.
4. Search `.py`/`.rst` Sphinx `:func:`/`:class:`/`:meth:`/`:mod:`/`:attr:` old-name roles; edit only matching module context.
5. For `--deprecate`, after definition rename run `PLUGIN_ROOT/bin/gen_deprecation_wrapper.py`; insert after new definition. It maps class to `@deprecated_class(target=NewName, ...)` (keeps `isinstance`) and function/method to `@deprecated(target=new_fn, ...)`. Missing pyDeprecate makes target import fail; report advisory in Step 6.

### 5. Apply module edits

1. Refuse non-git, untracked, or uncommitted source; require add/commit/stash. `git mv <old_file_path> <new_file_path>`, preferring indexed path over dotted conversion (which can be wrong under `src/` layouts). Package directories (`__init__.py`) are out of scope: print direct `git mv`, do not attempt.
2. Rename direct `import mypackage.old_name` (including `as X`) and `from mypackage.old_name import ...`.
3. Rename same-package `__init__.py` `from .old_name import ...` after directory check.
4. Search full dotted path in `pyproject.toml`/`setup.cfg`, editing package/install requirements only; never bare basename.
5. Rename Sphinx `:mod:` full paths; basename fallback needs matching module context.

### 6. Re-scan and summary

```bash
PLUGIN_ROOT/bin/codemap-py index --incremental
PLUGIN_ROOT/bin/codemap-py query find-symbol "<old_ref>" --limit 0    # symbol
PLUGIN_ROOT/bin/codemap-py query rdeps "<old_ref>"                     # module
```

If re-scan fails, report it; results are advisory, not authoritative. Expect old name absent except deprecated alias. List other residual files as advisory (dynamic/string/template/out-of-scope refs). Report renamed pair, files, calls, doc refs, alias path/line when used; always flag `getattr` literal search, external consumers without alias, skipped callers, and residual hits when applicable.
