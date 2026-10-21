---
name: rename-refs
description: |
  Atomic rename of Python symbols or modules via the structural index — static callers, import sites, __all__ re-exports, Sphinx cross-refs; optional deprecated alias (--deprecate) or hard-delete (--remove-if-no-callers).
  TRIGGER: "rename X to Y" (function/class/method/module), "move module X to Y", "update all references to X".
  SKIP: non-Python; index not built (/codemap-py:scan-codebase first); local-variable rename; grep-only rename wanted; 1:N symbol splits; package directory rename (git mv).
argument-hint: symbol <old_qname> <new_qname> [--dry-run] [--deprecate[='@deprecated(...)'|'@deprecated_class(...)'] | --no-deprecate] [--since <ver>] [--removed-in <ver>] [--remove-if-no-callers] | module <old_module_path> <new_module_path> [--dry-run]
allowed-tools: Bash, Read, Edit, Write, AskUserQuestion
model: sonnet
effort: medium
---

<objective>

Atomically rename Python symbol/module. Coverage:

- Definition site (def/class line)
- `__all__` re-exports in `__init__.py` files
- Import call sites across all callers (fn-rdeps + symbol line-range narrowing)
- Non-call references — imports, base classes, `isinstance` targets, callback values, ancestor `__init__.py` re-exports — in every file that imports the symbol's module or a re-exporting ancestor package (`rdeps`)
- Sphinx docstring cross-refs across `.py` and `.rst`
- Optional pyDeprecate `@deprecated` alias (`--deprecate`; needs `pyDeprecate`)
- Optional hard-delete when exhaustive=true + zero callers

**Subcommands**:

- `symbol <old_qname> <new_qname>` — function/class/method. qname bare (`MyClass`) or qualified (`MyClass.method`); matches symbol-local `qualified_name`. Module-qualified form (`module::symbol`) is not accepted by `find-symbol`.
- `module <old_module_path> <new_module_path>` — dotted path (`mypackage.old_name`). Renames file + all import lines.

**Flags**:

- `--dry-run` — show change sites; no edits
- `--deprecate[=<decorator>]` — symbol only: preserve old name as pyDeprecate `@deprecated` wrapper → new; needs `pyDeprecate`. Bare derives decorator from symbol type; value pins it, single-quoted (`--deprecate='@deprecated_class(...)'`). `--no-deprecate` declines the wrapper.
- `--since <ver>` / `--removed-in <ver>` — deprecation decorator values; default `"?"`
- `--remove-if-no-callers` — symbol only: confirm then delete definition when exhaustive=true + zero callers

**Hard limits** (static-analysis boundary):

- `getattr(obj, "old_name")` — statically unbound string; Step 6 emits grep advisory
- Cross-repo callers — out of scope; public APIs need `--deprecate` + semver bump
- Importers beyond one re-export hop, string references, and same-name symbols of another origin — never edited; Step 6 lists every whole-word `OLD_NAME` left in the project's Python files and Step 7 reports the count

IDE/LSP coverage preview → `--dry-run`. Only 1:1 renames.

NOT for: index builds (`/codemap-py:scan-codebase`); query-only work (`/codemap-py:query-code`); non-Python; ABC/Protocol symbols with subclass overrides. Static imports don't track overrides: manually review `fn-rdeps`; rename overrides explicitly. No `--index <path>`: always default project index. Monorepo `--root <pkg>` index from `/codemap-py:scan-codebase` isn't auto-resolved — `resolve_proj_index.py` derives PROJ only from git-root basename. Before rename, confirm path via `resolve_index_env.py` or keep `--root` consistent with default scans.

</objective>

<workflow>

## Step 0: Parse arguments

**Question delivery**: every `AskUserQuestion` uses at most 6 plain lines (target 600 characters, hard cap 1,500), no table, pipe layout, or code fence. Include the decision headline, counts and named content file path when applicable. Options carry labels and one-line descriptions, no previews; preserve existing answer mapping and keep the recommended option second. For new candidate/blast artifacts below, use `$CSID` for `<session-id>` and probe the path before Writing; if taken, append `-2`, then `-3`, … until free. Keep that resolved path for the matching question.

Parse `$ARGUMENTS` in one Bash block; `parse_rename_args.py` tokenizes it once (shell quoting rules), validates it and writes every value to a project-qualified sentinel. Later steps read sentinels because shell vars die at each Bash() boundary.

The arguments are pasted as text into the double-quoted word handed to `parse_rename_args.py` below, so a `"`, `` ` `` or `$` in them ends or expands that word (`--deprecate="@deprecated_class(...)"` is a bash syntax error before the helper runs). If the arguments contain any of them, do not run the block: print `! Quote --deprecate values with single quotes (--deprecate='@deprecated_class(...)') and pass versions via --since/--removed-in; a double quote, backtick or $ breaks argument parsing. Re-invoke.` and stop.

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_CM_PROJ=$(git rev-parse --show-toplevel 2>/dev/null | xargs basename 2>/dev/null || echo "cm")
# one shlex parse — awk/grep/sed re-splits fired on flags inside values; refs reach git mv, so validated there
python "${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/bin/parse_rename_args.py" "$ARGUMENTS" --output-prefix "codemap-${_CM_PROJ}" || exit 1
```

Non-zero exit → print the helper's stderr unchanged; stop. It rejects: missing/unknown subcommand (Usage line); missing old/new ref; flag-shaped ref (`looks like a flag, not a target — check argument order`); a ref that is not a dotted Python name, incl. `module::symbol` (own message); symbol-only flags under `module`; `--since`/`--removed-in` without a value; unquoted text left over; `--deprecate` with `--remove-if-no-callers` (`conflicting flags`). Unknown `--` tokens print `! Unknown flag(s): --<token>. Supported flags: --dry-run, --deprecate[=<decorator>], --no-deprecate, --since, --removed-in, --remove-if-no-callers. Re-invoke with corrected flags.` Never AskUserQuestion here: fail-fast keeps the worst-case AskUserQuestion path at 4 (STALE-index, multiple-matches, apply/dry-run, hard-delete confirmation).

## Step 1: Validate index

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_CM_PROJ=$(git rev-parse --show-toplevel 2>/dev/null | xargs basename 2>/dev/null || echo "cm")
python "${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/bin/resolve_index_env.py" \
    --output-prefix "codemap-${_CM_PROJ}" 2>/dev/null  # timeout: 5000
IFS= read -r PROJ < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-resolve-proj-${CSID}" 2>/dev/null || PROJ=""
IFS= read -r INDEX < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-resolve-index-${CSID}" 2>/dev/null || INDEX=""
[ -n "$PROJ" ] || { printf "! resolve_index_env.py failed — check Python availability and CLAUDE_PLUGIN_ROOT\n"; exit 1; }
[ -n "$INDEX" ] || { echo "! index not found — run /codemap-py:scan-codebase first"; exit 1; }
_SMOKE_FILE="${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-smoke-json-${CSID}"
python "${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/bin/check_index_smoke.py" --index-path "$INDEX" > "$_SMOKE_FILE"  # timeout: 10000
# python not jq — jq absent on stock Windows/CI; ok!=true (missing/corrupt index → {"ok":false,"stale":false}) or stale absent/null → unknown, never fresh
python "${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/bin/cm_json.py" stale --input "$_SMOKE_FILE" --output-prefix "codemap-${_CM_PROJ}" --name rename-STALE
IFS= read -r STALE < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-STALE-${CSID}" 2>/dev/null || STALE="unknown"
# emit — the branch below reads a printed value, not an invisible var
printf 'STALE=%s\n' "$STALE"
```

- `STALE=true` → `AskUserQuestion`: (a) Proceed anyway (callers may be incomplete); (b) Abort (first re-run /codemap-py:scan-codebase). Abort → print "Run `/codemap-py:scan-codebase` then re-invoke"; stop.
- `STALE=unknown` (JSON parse failed, `ok` not true, or `stale` absent/null) → print `⚠ Could not determine index freshness — proceeding but callers may be incomplete` and repeat it in Step 7's summary (a mid-run print can arrive as an empty progress update); continue cautiously, never treat fresh.

## Step 2: Resolve targets

All structural queries/rebuilds use `codemap-py` dispatcher, never `scan-query`/`scan-index`. Both routes use same in-engine shared query/exclusive scan leases, preventing mid-rename scan races. Dispatcher also owns interpreter probe; aliases skip it (`127` when no eligible CPython, including invalid `CODEMAP_PYTHON`), deprecated shims removed no earlier than `1.0.0`. Every block uses PATH-literal `codemap-py query …` / `codemap-py index …`; expansion-bearing path misses bare-name allow prefix, prompts each call. If bare command unavailable, use installed plugin's absolute `bin/codemap-py` launcher interactively.

**Symbol subcommand**:

```bash
# timeout: 25000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_CM_PROJ=$(git rev-parse --show-toplevel 2>/dev/null | xargs basename 2>/dev/null || echo "cm")
IFS= read -r OLD_REF < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-OLD_REF-${CSID}" 2>/dev/null || OLD_REF=""
_FS_SENTINEL="${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-find-symbol-json-${CSID}"
codemap-py query --timeout 20 find-symbol "$OLD_REF" --limit 0 > "$_FS_SENTINEL" || { printf '! find-symbol failed (exit %d) — stop before any source edit\n' "$?"; exit 1; }
# find-symbol is an unanchored pattern — keep exact qualified_name matches when any exist, so a unique exact target never asks
python "${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/bin/cm_json.py" narrow --input "$_FS_SENTINEL" --qualified-name "$OLD_REF"
```

`find-symbol` returns `matches`, each `{name, qualified_name, type, module, path, start_line, end_line, source}` (same as `query-code` `symbol`). Edits use `path`, `start_line`, `end_line`; exact filter uses `qualified_name` (applied above when any match is exact); Step 4e reads `.matches[0].type`.

- 0 matches → print `! Symbol '$OLD_REF' not found. Verify with: codemap-py query find-symbol <pattern>`; stop.
- Multiple → Write the full candidate list (name/type/module/path) to `.temp/rename-refs-candidates-<session-id>.md` first; print it once as the last reply text before `AskUserQuestion`. The question text carries a plain candidate-count summary and `→ <candidate list path>`; options identify the candidate with a one-line description, no previews. At most 4 options: more than 4 candidates → offer the first 3 plus `Abort — re-invoke with a more specific qualified name`. After choice, narrow sentinel so every downstream `.matches[0]` is correct:
  ```bash
  # timeout: 5000
  export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
  _CM_PROJ=$(git rev-parse --show-toplevel 2>/dev/null | xargs basename 2>/dev/null || echo "cm")
  CHOSEN_IDX="<0-based index of the match selected via AskUserQuestion>"
  case "$CHOSEN_IDX" in *[!0-9]*|"") echo "! BLOCKED — substitute CHOSEN_IDX with the chosen 0-based index"; exit 1 ;; esac
  # out of range → exit 4, answer left as is — never fall through to an unnarrowed matches[0]
  python "${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/bin/cm_json.py" narrow --input "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-find-symbol-json-${CSID}" --index "$CHOSEN_IDX" || exit 1
  ```

Full qname: `<module>::<qualified_name>` (e.g. `mypackage.auth::validate_token`).

```bash
# timeout: 25000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_CM_PROJ=$(git rev-parse --show-toplevel 2>/dev/null | xargs basename 2>/dev/null || echo "cm")
# one parse of the narrowed answer; 4b/4e read sym-path/sym-type back
python "${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/bin/cm_json.py" first-match --input "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-find-symbol-json-${CSID}" --output-prefix "codemap-${_CM_PROJ}" --field module=sym-module --field qualified_name=sym-qname --field path=sym-path --field type=sym-type
IFS= read -r SYM_MODULE < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-sym-module-${CSID}" 2>/dev/null || SYM_MODULE=""
IFS= read -r SYM_QNAME < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-sym-qname-${CSID}" 2>/dev/null || SYM_QNAME=""
[ -n "$SYM_MODULE" ] && [ -n "$SYM_QNAME" ] || { printf "! could not extract module/qualified_name from find-symbol result\n" >&2; exit 1; }
# wire OLD_MODULE_PATH here for 4c — OLD_REF can't carry module for symbol rename, don't re-derive
printf '%s\n' "$SYM_MODULE" > "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-OLD_MODULE_PATH-${CSID}"
_RDEPS_FILE="${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rdeps-json-${CSID}"
# error → rc≠0 or {"error":…}; summarised as 0 callers it would let the rename skip every caller
codemap-py query --timeout 20 fn-rdeps "${SYM_MODULE}::${SYM_QNAME}" > "$_RDEPS_FILE" || { printf '! fn-rdeps failed (exit %d) — callers unknown; stop before any source edit\n' "$?"; exit 1; }
grep -q '^{"error"' "$_RDEPS_FILE" && { printf '! fn-rdeps returned an error — callers unknown; stop before any source edit\n'; head -c 400 "$_RDEPS_FILE"; exit 1; }
# forward-first, fail-closed — query_complete wins even when false (a `//` fallback let legacy true arm the delete gate on an incomplete graph); zero-caller method → `hint` blocks delete (obj.method()/dynamic dispatch unresolved statically). Rules: cm_json.py rdeps_values
python "${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/bin/cm_json.py" rdeps-summary --kind fn --input "$_RDEPS_FILE" --output-prefix "codemap-${_CM_PROJ}"
IFS= read -r RDEP_COUNT < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rdep-count-${CSID}" 2>/dev/null || RDEP_COUNT="0"
IFS= read -r EXHAUSTIVE < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-EXHAUSTIVE-${CSID}" 2>/dev/null || EXHAUSTIVE="false"
IFS= read -r RDEP_HINT < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-RDEP_HINT-${CSID}" 2>/dev/null || RDEP_HINT=""
IFS= read -r INDEX_HINT < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-INDEX_HINT-${CSID}" 2>/dev/null || INDEX_HINT=""
IFS= read -r REMOVE_IF_ZERO < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-REMOVE_IF_ZERO-${CSID}" 2>/dev/null || REMOVE_IF_ZERO="false"
IFS= read -r DRY_RUN < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-DRY_RUN-${CSID}" 2>/dev/null || DRY_RUN="false"
# emit — Step 3 guards decide on a printed value, not an invisible sentinel read
printf 'REMOVE_IF_ZERO=%s DRY_RUN=%s RDEP_COUNT=%s EXHAUSTIVE=%s\nRDEP_HINT=%s\nINDEX_HINT=%s\n' "$REMOVE_IF_ZERO" "$DRY_RUN" "$RDEP_COUNT" "$EXHAUSTIVE" "$RDEP_HINT" "$INDEX_HINT"
```

`fn-rdeps` returns `{qname, called_by:[{caller, module, path}], count, index:{query_complete, hint, ...}}`, plus a top-level `hint` for a method or constructor with zero static callers.

- `called_by` has **no line numbers**; `caller` is `module::qualified_name`, which `symbol` never matches. Step 4c splits it at `::` and resolves the range via `codemap-py query symbols <module>` filtered by exact `qualified_name`.
- `EXHAUSTIVE` first reads `result["index"]["query_complete"]`; only if absent, use legacy `result["index"]["exhaustive"]`; neither → `false`. Incomplete → note in blast report.
- `RDEP_HINT` non-empty → zero static callers is not zero callers: instance calls (`obj.method()`), overrides, subclass constructors, and implicit protocol (dunder) calls such as `len(x)` or `x == y` are never resolved statically. Print it in the blast report.
- `INDEX_HINT` (`index.hint`) names the search for references the static AST never sees, such as hook-registered callers. Print it in the blast report; it never blocks the rename on its own.

`called_by` holds call edges only: a subclass base, `isinstance` target, callback value, plain import or ancestor `__init__.py` re-export never appears in it. List every whole-word `OLD_NAME` in the files that call the symbol, define it, import its module, or import an ancestor package that may re-export it (one `batch` of `rdeps`, module names mapped to paths by its `list` item); Step 4c edits from this list:

```bash
# timeout: 25000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_CM_PROJ=$(git rev-parse --show-toplevel 2>/dev/null | xargs basename 2>/dev/null || echo "cm")
IFS= read -r SYM_MODULE < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-sym-module-${CSID}" 2>/dev/null || SYM_MODULE=""
IFS= read -r OLD_NAME < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-OLD_NAME-${CSID}" 2>/dev/null || OLD_NAME=""
[ -n "$SYM_MODULE" ] && [ -n "$OLD_NAME" ] || { echo "! BLOCKED — sym-module or OLD_NAME sentinel missing; re-run Step 2"; exit 1; }
_T="${TMPDIR:-/tmp}/codemap-${_CM_PROJ}"
# rdeps of the module + every ancestor package: a re-export lives in an ancestor __init__.py, its users import the package
_ITEMS='{"cmd":"list","args":["--limit","0"]}'; _M="$SYM_MODULE"
while :; do _ITEMS="$_ITEMS,{\"cmd\":\"rdeps\",\"args\":[\"$_M\"]}"; case "$_M" in *.*) _M="${_M%.*}" ;; *) break ;; esac; done
printf '[%s]\n' "$_ITEMS" > "${_T}-ref-batch-in-${CSID}"
codemap-py query --timeout 20 batch "${_T}-ref-batch-in-${CSID}" > "${_T}-ref-batch-json-${CSID}" || { printf '! importer batch failed (exit %d) — reference sites unknown; stop before any source edit\n' "$?"; exit 1; }
# exit 1 = unreadable answer or unmappable batch; a partial site list would silently skip importers
python "${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/bin/find_module_refs.py" --kind name --name "$OLD_NAME" --files-from-json "${_T}-rdeps-json-${CSID}" --files-from-json "${_T}-find-symbol-json-${CSID}" --files-from-json "${_T}-ref-batch-json-${CSID}" > "${_T}-ref-sites-${CSID}" || exit 1
printf 'REF_SITES='; wc -l < "${_T}-ref-sites-${CSID}" | tr -d ' '
cat "${_T}-ref-sites-${CSID}"
```

Each printed `path:line:text` is a candidate reference site. Hits in comments, docstrings and strings are included; hits naming a same-name symbol imported from another module are not this symbol and stay unedited. For a method, `OLD_NAME` is the bare method name: only an attribute access on an instance of the renamed method's class is a reference.

**Module subcommand**:

```bash
# timeout: 25000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_CM_PROJ=$(git rev-parse --show-toplevel 2>/dev/null | xargs basename 2>/dev/null || echo "cm")
IFS= read -r OLD_REF < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-OLD_REF-${CSID}" 2>/dev/null || OLD_REF=""
OLD_MODULE_PATH="$OLD_REF"
printf '%s\n' "$OLD_MODULE_PATH" > "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-OLD_MODULE_PATH-${CSID}"
_RDEPS_FILE="${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rdeps-json-${CSID}"
codemap-py query --timeout 20 rdeps "$OLD_REF" > "$_RDEPS_FILE" || { printf '! rdeps failed (exit %d) — importers unknown; stop before any source edit\n' "$?"; exit 1; }
grep -q '^{"error"' "$_RDEPS_FILE" && { printf '! rdeps returned an error — importers unknown; stop before any source edit\n'; head -c 400 "$_RDEPS_FILE"; exit 1; }
# same forward-first, fail-closed read as the symbol branch; rdeps carries no method hint, so --kind module overwrites it empty (an earlier symbol run's hint never reaches this gate)
python "${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/bin/cm_json.py" rdeps-summary --kind module --input "$_RDEPS_FILE" --output-prefix "codemap-${_CM_PROJ}"
IFS= read -r RDEP_COUNT < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rdep-count-${CSID}" 2>/dev/null || RDEP_COUNT="0"
IFS= read -r EXHAUSTIVE < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-EXHAUSTIVE-${CSID}" 2>/dev/null || EXHAUSTIVE="false"
# module never deletes — parse_rename_args.py rejects --remove-if-no-callers for module
REMOVE_IF_ZERO="false"
IFS= read -r DRY_RUN < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-DRY_RUN-${CSID}" 2>/dev/null || DRY_RUN="false"
IFS= read -r INDEX_HINT < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-INDEX_HINT-${CSID}" 2>/dev/null || INDEX_HINT=""
# emit — Step 3 guards decide on a printed value, not an invisible sentinel read
printf 'REMOVE_IF_ZERO=%s DRY_RUN=%s RDEP_COUNT=%s EXHAUSTIVE=%s\nRDEP_HINT=%s\nINDEX_HINT=%s\n' "$REMOVE_IF_ZERO" "$DRY_RUN" "$RDEP_COUNT" "$EXHAUSTIVE" "" "$INDEX_HINT"
```

Returns `{imported_by:[...], index:{query_complete,...}}`; extract `RDEP_COUNT`, `EXHAUSTIVE`.

## Step 3: Blast-radius report + confirmation gate

Write the full report below to a free `.temp/rename-refs-blast-<session-id>.md` path first. Before each confirmation call below, print the relevant saved report once as the last reply text before the question; question text still carries the rename/deletion headline, caller/site counts and `→ <report path>`. Do not put the report or caller list in option previews.

Build this report (print it at the confirmation gate, after guards and output-path selection):

```
Rename: <OLD_REF> → <NEW_REF>
Type: symbol | module
[symbol] Definition: <path>:<start_line>-<end_line>

Static callers: N (across N files)
  - src/foo/bar.py   (caller: module::fn)
  - src/foo/baz.py   (caller: module::other_fn)
  - tests/test_foo.py

[symbol] Reference sites (imports, bases, isinstance, callbacks, re-exports): N lines across N files
  - pkg/c.py:4   class Child(OldName):

Docstring refs: (will grep :func:/:class:/:meth:/:mod:/:attr: in Step 4d)

[if DEPRECATE]
Deprecation wrapper: <OLD_REF> kept as @deprecated alias → <NEW_REF>

⚠ Not covered (hard limits — grep advisories in Step 8):
  - getattr("old_name") dynamic dispatch
  - Cross-repo consumers
[if not EXHAUSTIVE]
⚠ Index non-exhaustive — some callers may not appear above
[if RDEP_HINT]
⚠ <RDEP_HINT>
[if INDEX_HINT]
ℹ Outside the static AST: <INDEX_HINT>
```

Caller count alone never blocks a complete migration. Enumerate and preflight every known caller; for a long list, save the complete inventory to a free branch-scoped `.temp/output-rename-refs-blast-<branch>-<YYYY-MM-DD>.md` path and print its path. Derive `<branch>` from `git branch --show-current`, replacing `/` with `-` (`main` if empty/detached). Never overwrite: probe before Writing and append `-2`, `-3`, … until free. The inventory supports confirmation and complete execution; it never permits a partial migration.

Before apply confirmation, resolve every known caller before any source edit: for symbols, query every caller and verify its unique source range and expected imports/calls; for modules, inspect every dependent file and matching import. For a missing, ambiguous or stale site, stop before any source edit with source unchanged. Retain these locations for apply; recheck them before mutation. The complete preflight applies to both subcommands regardless of caller count.

**`--remove-if-no-callers` guards** — evaluated before any rename edits, on the `REMOVE_IF_ZERO=… DRY_RUN=… RDEP_COUNT=… EXHAUSTIVE=…` and `RDEP_HINT=…` lines printed by Step 2's last block (symbol: fn-rdeps block; module: rdeps block):

- `REMOVE_IF_ZERO=true` AND `RDEP_COUNT > 0` → print `! --remove-if-no-callers: N callers found. Remove all callers first or omit flag.`; stop **entire rename operation**.
- `REMOVE_IF_ZERO=true` AND `EXHAUSTIVE=false` → print `! --remove-if-no-callers requires exhaustive=true. Run /codemap-py:scan-codebase to ensure full coverage.`; stop **entire rename operation**.
- `REMOVE_IF_ZERO=true` AND `RDEP_HINT` non-empty → print `! --remove-if-no-callers: 0 static callers is not proof for a method or constructor — <RDEP_HINT>`; stop **entire rename operation**. Hint names a search → append `Run that search; delete manually only if it finds no reference.` Protocol-method hint (a dunder such as `__len__`/`__eq__`, no search) → append `Python calls it implicitly; never delete it on zero callers.` — an empty search is expected there, never a licence to delete. Zero static callers of a method is the hint's case, not evidence of none; never delete on it.
- `REMOVE_IF_ZERO=true` AND `RDEP_COUNT == 0` AND `EXHAUSTIVE=true` AND `RDEP_HINT` empty → first run the non-call reference check below (both `DRY_RUN` values). `fn-rdeps` counts call edges only; imports, base classes, `isinstance`, callback values and re-exports never appear in it. Any printed hit other than the definition's own `def`/`class` line → print `! --remove-if-no-callers: <N> non-call reference(s) to <OLD_NAME> — delete would leave ImportError/NameError`; stop **entire rename operation**.
  ```bash
  # timeout: 15000
  export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
  _CM_PROJ=$(git rev-parse --show-toplevel 2>/dev/null | xargs basename 2>/dev/null || echo "cm")
  IFS= read -r OLD_NAME < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-OLD_NAME-${CSID}" 2>/dev/null || OLD_NAME=""
  [ -n "$OLD_NAME" ] || { echo "! BLOCKED — OLD_NAME sentinel missing; re-run from Step 0"; exit 1; }
  grep -rnw --include='*.py' --include='*.pyi' --exclude-dir=.git --exclude-dir=.venv --exclude-dir=node_modules -- "$OLD_NAME" . | head -50
  ```
- `REMOVE_IF_ZERO=true` AND `RDEP_COUNT == 0` AND `EXHAUSTIVE=true` AND `RDEP_HINT` empty AND no non-call hit AND `DRY_RUN=false` → `AskUserQuestion`: (a) Delete `$OLD_REF` — confirmed no callers; (b) Abort — keep file. Abort: stop. Delete: find-symbol line range, then `Read` block bounds. Verify `start_line` contains expected bare `OLD_NAME` or qualified `OLD_REF`; mismatch → print `! Symbol name mismatch at line <start_line>: expected <OLD_NAME>, index may be stale — run /codemap-py:scan-codebase first`; abort without delete. Only then `Edit`: remove definition from `def`/`class` through final body, including immediately preceding `@decorator` lines. Skip Steps 4a–4d. Print `ℹ Symbol had no callers — removed $OLD_REF without rename`; go Step 6.
- `REMOVE_IF_ZERO=true` AND `RDEP_COUNT == 0` AND `EXHAUSTIVE=true` AND `RDEP_HINT` empty AND no non-call hit AND `DRY_RUN=true` → record "would delete `$OLD_REF` — no callers, exhaustive" as a line in the dry-run report below; fall through to the `--dry-run` block. No `AskUserQuestion`, no `Edit` — dry-run means no edits, including the delete.
- Otherwise (`REMOVE_IF_ZERO=false`): proceed with normal rename flow.

**`--dry-run`**: print a free branch-scoped output path first (`$DRY_OUT` below), then write the report:

```bash
# timeout: 3000
python "${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/bin/anti_overwrite_path.py" .temp output-rename-refs-dry --branch-date
```

Write report to the printed `$DRY_OUT` path via Write, beginning with YAML header:

```yaml
---
Title:      rename-refs dry-run — <OLD_REF> → <NEW_REF>
Date:       <YYYY-MM-DD>
Scope:      <project name>
Focus:      dry-run: sites that would be changed
Agents:     codemap-py:rename-refs
Outcome:    DRY_RUN — no edits applied; <N callers, M import sites, K docstring refs>
Confidence: <exhaustive|partial>
Next steps: re-invoke without --dry-run to apply; or abort
Path:       → <the resolved $DRY_OUT path>
---
```

[if reached via the zero-callers delete path above] `Title` → `rename-refs dry-run — would delete <OLD_REF>`; `Outcome` → `DRY_RUN — no edits applied; would delete <OLD_REF> — zero callers, exhaustive`; body records only that one line, no caller/import/docstring counts.

`AskUserQuestion` after printing the saved `$DRY_OUT` report once as the last reply text before the call; question text carries the dry-run headline, caller/import/docstring counts (or only the would-delete line for the delete path), and `→ <resolved $DRY_OUT>`, no previews: (a) Apply for real — re-invoke without --dry-run; (b) Done — keep the report without edits. Stop.

Otherwise `AskUserQuestion`: print the saved blast-radius report once as the last reply text before the call; question text carries the rename headline, caller count, any cap/incomplete-index warning, and `→ <resolved blast report path>`, no previews. (a) Apply edits — rename the listed sites; (b) Abort — keep the current code. Abort → stop.

## Step 4: Apply edits — symbol rename

Skip to Step 5 if `SUBCOMMAND=module`.

> loads: symbol.md

Steps 4a–4e (definition site, `__all__` re-exports, import call sites, Sphinx cross-refs, deprecation wrapper) live in `modes/symbol.md`; load it and follow it in order:

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_CM_PROJ=$(git rev-parse --show-toplevel 2>/dev/null | xargs basename 2>/dev/null || echo "cm")
IFS= read -r SUBCOMMAND < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-SUBCOMMAND-${CSID}" 2>/dev/null || SUBCOMMAND=""
[ "$SUBCOMMAND" = "symbol" ] || { printf 'ℹ SUBCOMMAND=%s — skip Step 4\n' "$SUBCOMMAND"; exit 0; }
cat "${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/claude-skills/rename-refs/modes/symbol.md"
```

## Step 5: Apply edits — module rename

Skip to Step 6 if `SUBCOMMAND=symbol`.

> loads: module.md

Stop conditions carried by 5a: non-git source, untracked source, or unmerged/conflicted path → stop before edits; preserve uncommitted user edits on a tracked source — dirty status alone is never a stop.

Steps 5a–5f (file rename, direct imports, from-imports, `__init__.py` relative re-exports, `pyproject.toml`/`setup.cfg`, Sphinx `:mod:` refs) live in `modes/module.md`; load it and follow it in order:

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_CM_PROJ=$(git rev-parse --show-toplevel 2>/dev/null | xargs basename 2>/dev/null || echo "cm")
IFS= read -r SUBCOMMAND < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-SUBCOMMAND-${CSID}" 2>/dev/null || SUBCOMMAND=""
[ "$SUBCOMMAND" = "module" ] || { printf 'ℹ SUBCOMMAND=%s — skip Step 5\n' "$SUBCOMMAND"; exit 0; }
cat "${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/claude-skills/rename-refs/modes/module.md"
```

## Step 6: Re-scan + verify

```bash
# timeout: 400000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_CM_PROJ=$(git rev-parse --show-toplevel 2>/dev/null | xargs basename 2>/dev/null || echo "cm")
# dispatcher not scan-index alias (rationale: Step 2). --incremental re-parses changed files only.
codemap-py index --incremental --timeout 360
_scan_rc=$?
if [ "$_scan_rc" -ne 0 ]; then
    printf "! codemap-py index --incremental failed (exit %d) — verification may be incomplete; run /codemap-py:scan-codebase for full rebuild\n" "$_scan_rc"
    # don't skip — stale results are advisory only
fi
IFS= read -r OLD_REF < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-OLD_REF-${CSID}" 2>/dev/null || OLD_REF=""
codemap-py query --timeout 20 find-symbol "$OLD_REF" --limit 0  # timeout: 25000
```

For `module`:

```bash
# timeout: 25000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_CM_PROJ=$(git rev-parse --show-toplevel 2>/dev/null | xargs basename 2>/dev/null || echo "cm")
IFS= read -r OLD_REF < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-OLD_REF-${CSID}" 2>/dev/null || OLD_REF=""
codemap-py query --timeout 20 rdeps "$OLD_REF"
```

Expected: old name absent, except deprecated alias with `--deprecate`. `find-symbol` is an unanchored pattern, so count only matches whose `qualified_name` equals `OLD_REF`; a new name containing the old one (`old_fn` → `old_fn_v2`) is not a residual.

For `symbol`, the index check above never sees a reference — only definitions. Count every whole-word `OLD_NAME` still in `*.py`/`*.pyi` files (a function or class: the whole tree, VCS, virtualenv and cache directories skipped; a method: Step 2's reference-site files, since a bare method name such as `run` recurs across unrelated classes). With `--deprecate`, the alias stub's own `def`/`class` line is not counted:

```bash
# timeout: 30000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_CM_PROJ=$(git rev-parse --show-toplevel 2>/dev/null | xargs basename 2>/dev/null || echo "cm")
IFS= read -r SUBCOMMAND < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-SUBCOMMAND-${CSID}" 2>/dev/null || SUBCOMMAND=""
[ "$SUBCOMMAND" = "symbol" ] || { printf 'ℹ SUBCOMMAND=%s — no symbol residual check\n' "$SUBCOMMAND"; exit 0; }
IFS= read -r OLD_NAME < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-OLD_NAME-${CSID}" 2>/dev/null || OLD_NAME=""
IFS= read -r SYM_PATH < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-sym-path-${CSID}" 2>/dev/null || SYM_PATH=""
IFS= read -r SYM_TYPE < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-sym-type-${CSID}" 2>/dev/null || SYM_TYPE=""
IFS= read -r DEPRECATE < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-DEPRECATE-${CSID}" 2>/dev/null || DEPRECATE="false"
[ -n "$OLD_NAME" ] || { echo "! BLOCKED — OLD_NAME sentinel missing; re-run from Step 0"; exit 1; }
_T="${TMPDIR:-/tmp}/codemap-${_CM_PROJ}"
_ALIAS=""; [ "$DEPRECATE" = "true" ] && _ALIAS="$SYM_PATH"
if [ "$SYM_TYPE" = "method" ]; then
    python "${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/bin/find_module_refs.py" --kind name --name "$OLD_NAME" ${_ALIAS:+--alias-in "$_ALIAS"} --files-from-json "${_T}-rdeps-json-${CSID}" --files-from-json "${_T}-find-symbol-json-${CSID}" --files-from-json "${_T}-ref-batch-json-${CSID}" > "${_T}-residual-${CSID}" || exit 1
else
    python "${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/bin/find_module_refs.py" --kind name --name "$OLD_NAME" ${_ALIAS:+--alias-in "$_ALIAS"} > "${_T}-residual-${CSID}" || exit 1
fi
printf 'RESIDUAL_COUNT='; wc -l < "${_T}-residual-${CSID}" | tr -d ' '
cat "${_T}-residual-${CSID}"
```

Classify each residual line: an unedited reference to the renamed symbol (an import from its module or a re-exporting package, a base, a call, an `isinstance` target, a callback value, a comment or docstring naming it) or a same-name symbol of another origin. Any unedited reference makes the migration incomplete (Step 7). Hard-limit cases: dynamic refs, template strings, out-of-scope config strings.

Before claiming completion, verify the renamed provider and every known caller with the relevant import and call checks. An unresolved known caller, failed check or unavailable verification leaves the migration incomplete; list the exact remaining consumer and next action. Index results alone do not establish that consumers still work.

## Step 7: Summary

Print:

```
[if any known consumer remains unresolved, any Step 6 residual is an unedited reference, or verification failed/unavailable]
⚠ Rename incomplete — M of N callers verified; R unedited reference(s) to <OLD_NAME>:
    <file>:<line>
  <remaining consumers, failed checks and next actions>
[else]
✓ Renamed: <OLD_REF> → <NEW_REF> — residual whole-word references: RESIDUAL_COUNT (each a same-name symbol of another origin; 0 when none)
  Files changed: N
  Call sites updated: M
  Docstring refs updated: K
  [if DEPRECATE] Deprecation alias added at: <path>:<line>

Advisory — check manually (outside static analysis coverage):
  - getattr("<old_name>") dynamic dispatch: grep -rn '"<old_name>"' src/
  [if cross-repo public API and DEPRECATE not used]
  - External consumers: update CHANGELOG; use --deprecate alias until next major release
  [if residual hits from Step 6 re-scan]
  - Residual same-name references (another origin — left unedited):
      <file>:<line>
```

</workflow>
