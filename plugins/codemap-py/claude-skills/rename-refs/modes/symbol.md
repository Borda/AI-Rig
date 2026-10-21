> Loaded by `rename-refs` Step 4 when `SUBCOMMAND=symbol`. Step references (Step 2, 3, 5, 6, 7) point at `SKILL.md`.

**4a — Rename definition site**: Read find-symbol `path`; edit definition at `start_line`:

- `def old_name(` → `def new_name(`
- `class OldName(` / `class OldName:` → `class NewName(` / `class NewName:`
- Method: match `def old_method(self` within class body
- **Class-body bound** (methods only — `OLD_REF` dotted): resolve the enclosing class's line range before the descriptor/overload greps below, so they scope to this class, not the whole file:
  ```bash
  # timeout: 25000
  export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
  _CM_PROJ=$(git rev-parse --show-toplevel 2>/dev/null | xargs basename 2>/dev/null || echo "cm")
  IFS= read -r OLD_REF < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-OLD_REF-${CSID}" 2>/dev/null || OLD_REF=""
  CLS="${OLD_REF%.*}"
  _CLS_FILE="${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-cls-json-${CSID}"
  printf '{}\n' > "$_CLS_FILE"
  # find-symbol is unanchored re.IGNORECASE (query.py) — matches[0] may be a method or sibling class; cm_json keeps the exact class
  [ "$CLS" != "$OLD_REF" ] && codemap-py query --timeout 20 find-symbol "$CLS" --limit 0 > "$_CLS_FILE"
  python "${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/bin/cm_json.py" class-range --input "$_CLS_FILE" --qualified-name "$CLS" --output-prefix "codemap-${_CM_PROJ}" --name cls-range
  IFS= read -r CLS_RANGE < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-cls-range-${CSID}" 2>/dev/null || CLS_RANGE=""
  [ -n "$CLS_RANGE" ] || printf "⚠ could not bound to class body — verify each hit\n"
  ```
- **`@property` descriptors**: a getter/setter/deleter triple shares one `qualified_name` — `find-symbol` (Step 2) and its multi-match narrowing return only the one match picked there (the getter, by convention). After getter rename, grep the class body (bound above; whole-file if unresolved) for `@old_name.setter` / `@old_name.deleter`; rename to `@new_name.setter` / `@new_name.deleter`. Otherwise descriptor breaks: renamed getter + `@old_name.setter` raises `AttributeError` during class definition.
  ```bash
  export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
  _CM_PROJ=$(git rev-parse --show-toplevel 2>/dev/null | xargs basename 2>/dev/null || echo "cm")
  IFS= read -r OLD_NAME < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-OLD_NAME-${CSID}" 2>/dev/null || OLD_NAME=""
  # definition file from Step 2's find-symbol match
  IFS= read -r SYM_PATH < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-sym-path-${CSID}" 2>/dev/null || SYM_PATH=""
  [ -n "$SYM_PATH" ] || { echo "! BLOCKED — sym-path sentinel missing; re-run Step 2"; exit 1; }
  IFS= read -r CLS_RANGE < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-cls-range-${CSID}" 2>/dev/null || CLS_RANGE=""
  set -- $CLS_RANGE; CLS_START="${1:-}"; CLS_END="${2:-}"
  if [ -n "$CLS_START" ]; then
      grep -n "@${OLD_NAME}\.setter\|@${OLD_NAME}\.deleter" "$SYM_PATH" | awk -F: -v s="$CLS_START" -v e="$CLS_END" '$1>=s && $1<=e'  # timeout: 3000
  else
      grep -n "@${OLD_NAME}\.setter\|@${OLD_NAME}\.deleter" "$SYM_PATH"  # timeout: 3000
  fi
  ```
- **`@typing.overload` stubs**: after implementation rename, grep same file (bound to class body above, when resolved) + sibling `.pyi` (whole-file — independent line numbering, class bound doesn't apply) for `@overload`-decorated `def old_name(`. `find-symbol` returns only the implementation, not stubs/files. Rename all overload stubs to `new_name`:
  ```bash
  export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
  _CM_PROJ=$(git rev-parse --show-toplevel 2>/dev/null | xargs basename 2>/dev/null || echo "cm")
  IFS= read -r OLD_NAME < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-OLD_NAME-${CSID}" 2>/dev/null || OLD_NAME=""
  IFS= read -r SYM_PATH < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-sym-path-${CSID}" 2>/dev/null || SYM_PATH=""
  [ -n "$SYM_PATH" ] || { echo "! BLOCKED — sym-path sentinel missing; re-run Step 2"; exit 1; }
  IFS= read -r CLS_RANGE < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-cls-range-${CSID}" 2>/dev/null || CLS_RANGE=""
  set -- $CLS_RANGE; CLS_START="${1:-}"; CLS_END="${2:-}"
  if [ -n "$CLS_START" ]; then
      grep -n "@overload" "$SYM_PATH" | awk -F: -v s="$CLS_START" -v e="$CLS_END" '$1>=s && $1<=e' | grep -A1 "def $OLD_NAME("  # timeout: 3000
  else
      grep -n "@overload" "$SYM_PATH" | grep -A1 "def $OLD_NAME("  # timeout: 3000
  fi
  grep -n "@overload" "${SYM_PATH%.py}.pyi" 2>/dev/null | grep -A1 "def $OLD_NAME("  # timeout: 3000
  ```

**4b — `__all__` re-exports**:

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_CM_PROJ=$(git rev-parse --show-toplevel 2>/dev/null | xargs basename 2>/dev/null || echo "cm")
IFS= read -r OLD_NAME < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-OLD_NAME-${CSID}" 2>/dev/null || OLD_NAME=""
# scope to pkg tree — avoids same-name false positives; sym-path written by Step 2
IFS= read -r OLD_FILE_PATH < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-sym-path-${CSID}" 2>/dev/null || OLD_FILE_PATH=""
PKG_DIR=$(dirname "${OLD_FILE_PATH:-.}")
printf '%s\n' "$PKG_DIR" > "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-pkg-dir-${CSID}"
# init 4c import-dedup tracker before the caller loop
printf '' > "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-processed-import-files-${CSID}"
python "${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/bin/find_module_refs.py" --kind all-exports --name "$OLD_NAME" --scope "${PKG_DIR:-.}"
```

For each `__all__` hit, Edit `"old_name"` → `"new_name"`.

**4c — Import call sites** (per caller from `called_by`):

Track import-edited files in `PROCESSED_IMPORT_FILES` tmpfile; three callers in one file still need one import edit. The 4b block initializes it before the loop.

Before each caller-file import edit, check + mark:

```bash
# timeout: 3000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_CM_PROJ=$(git rev-parse --show-toplevel 2>/dev/null | xargs basename 2>/dev/null || echo "cm")
TARGET_FILE="<caller_path>"
case "$TARGET_FILE" in *'<'*'>'*|"") echo "! BLOCKED — substitute TARGET_FILE with the caller path"; exit 1 ;; esac
grep -qxF "$TARGET_FILE" "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-processed-import-files-${CSID}" && SKIP_IMPORT="true" || SKIP_IMPORT="false"
[ "$SKIP_IMPORT" = "false" ] && echo "$TARGET_FILE" >> "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-processed-import-files-${CSID}"
```

Edit imports only when `SKIP_IMPORT=false`.

Per caller (`called_by[i].caller` is `module::qualified_name`; `query symbol` never matches the `::` form — it returns not-found, so split it first):

1. Split `caller` at the first `::` into `<caller_module>` and `<caller_qualified>`. Run `codemap-py query symbols "<caller_module>"` once per distinct module — timeout: 10000; result `{module, path, symbols:[{qualified_name, start_line, end_line, ...}]}` (exact module key, full ranges, no cap)
   - Error, unknown module, or 0 entries with `qualified_name == <caller_qualified>` → stop; the preflight must resolve every known caller before edits. Unexpected drift after edits means an incomplete migration; report the partial state, never skip the caller or claim success
   - Filter `symbols[]` by exact `qualified_name == <caller_qualified>`; more than one match (e.g. `@overload`/`@property` siblings) is ambiguous and stops the operation
   - Use the answer's top-level `path` plus the matched entry's `start_line`, `end_line` for step 2
2. Within caller's line range in `path`:
   - **Priority**: (1) module-level imports (whole-file, once/file, may precede `start_line`); (2) qualified `X.old_name(` within start_line–end_line; (3) bare `old_name(` only within start_line–end_line
   - Edit bare `old_name(` → `new_name(` **only** within start_line–end_line — never bare-replace outside confirmed caller scope
   - **Import dedup**: `path` in `processed-import-files` (`SKIP_IMPORT=true`) → skip import edit; edit only in-range call sites

Module-level import fix (whole-file scope, once per file):

```bash
# timeout: 3000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_CM_PROJ=$(git rev-parse --show-toplevel 2>/dev/null | xargs basename 2>/dev/null || echo "cm")
IFS= read -r OLD_NAME < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-OLD_NAME-${CSID}" 2>/dev/null || OLD_NAME=""
# OLD_MODULE_PATH set by Step 2 only — symbol OLD_REF can't carry module; re-deriving here previously misplaced symbol name as module (bug)
IFS= read -r OLD_MODULE_PATH < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-OLD_MODULE_PATH-${CSID}" 2>/dev/null || OLD_MODULE_PATH=""
# dots literal, not any-char
_OLD_MODULE_GREP=$(printf '%s' "$OLD_MODULE_PATH" | sed 's/\./\\./g')
_OLD_BASENAME_GREP=$(printf '%s' "${OLD_MODULE_PATH##*.}" | sed 's/\./\\./g')
IMPORT_FILE="<caller_path>"
case "$IMPORT_FILE" in *'<'*'>'*|"") echo "! BLOCKED — substitute IMPORT_FILE with the caller path"; exit 1 ;; esac
grep -n "from ${_OLD_MODULE_GREP} import .*\b${OLD_NAME}\b\|from .*\\.${_OLD_BASENAME_GREP} import .*\b${OLD_NAME}\b\|^import .*\b${OLD_NAME}\b" "$IMPORT_FILE"
```

Edit matched import lines only after verifying `from <module>` references expected module; skip unrelated imports of `OLD_NAME`. Dedup block already updated `processed-import-files`.

**Non-call reference sites** (after the caller loop; every file in Step 2's `REF_SITES` list, callers included): the caller loop edits call sites inside caller ranges only, so a file that imports the symbol to subclass it, test it with `isinstance`, register it as a callback, or re-export it keeps the old name. Per file:

1. Find the line that brings `OLD_NAME` into the file: `from <SYM_MODULE> import …`, a relative `from .<module> import …`, or `from <ancestor package> import …` where that ancestor's `__init__.py` re-exports it (itself a listed site). Another origin → same-name symbol; leave the whole file unedited.
2. Origin confirmed → edit that import line (unless `processed-import-files` lists the file) and every listed whole-word `OLD_NAME` in the file: base classes, `isinstance` targets, callback values, `__all__` strings, re-export lines, annotations, comments and docstrings naming it. For a method, edit only attribute accesses on an instance of its class.
3. A listed line that cannot be classified stays unedited; Step 6 counts it and Step 7 reports it.

**4d — Sphinx docstring cross-refs**:

```bash
# timeout: 10000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_CM_PROJ=$(git rev-parse --show-toplevel 2>/dev/null | xargs basename 2>/dev/null || echo "cm")
IFS= read -r OLD_NAME < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-OLD_NAME-${CSID}" 2>/dev/null || OLD_NAME=""
# full module prefix — avoids false positive on same bare name elsewhere
IFS= read -r OLD_MODULE_PATH < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-OLD_MODULE_PATH-${CSID}" 2>/dev/null || OLD_MODULE_PATH=""
# matches bare name anywhere — verify module before edit
python "${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/bin/find_module_refs.py" --kind sphinx-symbol --name "$OLD_NAME"
```

For each match, replace `old_name`/`OldName` inside backtick-delimited role only after verifying expected module path (`${OLD_MODULE_PATH}` or qualified form); avoid same-name unrelated package refs.

**4e — Deprecation wrapper** (runs last, after 4d):

Run last, after 4d, unconditionally. Block reads `$DEPRECATE` sentinel; when not `true`, exits 0 immediately. Shell enforces gate. Insert `$DEPRECATION_CODE` only when block exits 0.

4a–4d are 1:1 substitutions — line counts preserved, so the Step-2 `end_line` stays valid throughout. 4e is the only step that inserts lines, hence it runs last; any future line-count-changing step must re-derive `end_line`.

Use `gen_deprecation_wrapper.py` to produce Python string; insert immediately after new definition in same file.

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_CM_PROJ=$(git rev-parse --show-toplevel 2>/dev/null | xargs basename 2>/dev/null || echo "cm")
IFS= read -r DEPRECATE < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-DEPRECATE-${CSID}" 2>/dev/null || DEPRECATE="false"
[ "$DEPRECATE" = "true" ] || { printf "ℹ DEPRECATE not set — skipping deprecation wrapper\n"; exit 0; }
IFS= read -r SYMBOL_TYPE < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-sym-type-${CSID}" 2>/dev/null || SYMBOL_TYPE=""
SYMBOL_TYPE="${SYMBOL_TYPE:-function}"

IFS= read -r OLD_NAME < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-OLD_NAME-${CSID}" 2>/dev/null || OLD_NAME=""
IFS= read -r NEW_NAME < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-NEW_NAME-${CSID}" 2>/dev/null || NEW_NAME=""
IFS= read -r DEPRECATE_DECORATOR < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-DEPRECATE_DECORATOR-${CSID}" 2>/dev/null || DEPRECATE_DECORATOR=""
IFS= read -r SINCE_VER < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-SINCE_VER-${CSID}" 2>/dev/null || SINCE_VER=""
IFS= read -r REMOVED_IN_VER < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-REMOVED_IN_VER-${CSID}" 2>/dev/null || REMOVED_IN_VER=""
if [ -n "$DEPRECATE_DECORATOR" ]; then
    DEPRECATION_CODE=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/bin/gen_deprecation_wrapper.py" \
        --decorator "$DEPRECATE_DECORATOR" \
        --old-name "$OLD_NAME" \
        ${REMOVED_IN_VER:+--removed-in "$REMOVED_IN_VER"}) || { echo "! gen_deprecation_wrapper failed — check symbol type and names"; exit 1; }
else
    DEPRECATION_CODE=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/bin/gen_deprecation_wrapper.py" \
        --type "$SYMBOL_TYPE" \
        --old-name "$OLD_NAME" \
        --new-name "$NEW_NAME" \
        ${SINCE_VER:+--since "$SINCE_VER"} \
        ${REMOVED_IN_VER:+--removed-in "$REMOVED_IN_VER"}) || { echo "! gen_deprecation_wrapper failed — check symbol type and names"; exit 1; }
fi
```

Insert `$DEPRECATION_CODE` immediately after new definition (`end_line` from Step 2). Target needs pyDeprecate; otherwise inserted `from deprecate import ...` raises import-time `ImportError`. Surface Step 6 advisory.

Method (`SYMBOL_TYPE=method`): the generated block is module-level — column-0 `def` plus a `from deprecate import` line. Pasted after a method it would end the class body early. Split it: put the import line with the module's other imports, and insert the decorator + stub after the new method's last line, re-indented to that method's own `def` indentation. Keep `target=<NEW_NAME>` bare: the class name is not bound yet while its body runs.

Type→decorator mapping:

- `"class"` → `@deprecated_class(target=NewName, ...)`; transparent proxy preserves `isinstance`
- `"function"` / `"method"` → `@deprecated(target=new_fn, ...)`; `...` body, pydeprecate forwards calls
