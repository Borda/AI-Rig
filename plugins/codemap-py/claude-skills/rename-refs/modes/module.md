> Loaded by `rename-refs` Step 5 when `SUBCOMMAND=module`. Step references (Step 2, 3, 6, 7) point at `SKILL.md`.

**5a — File rename**:

```bash
# timeout: 3000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_CM_PROJ=$(git rev-parse --show-toplevel 2>/dev/null | xargs basename 2>/dev/null || echo "cm")
git rev-parse --is-inside-work-tree 2>/dev/null || { printf "⚠ non-git source — tracked-file module moves require Git tracking; stop before edits\n" >&2; exit 1; }
IFS= read -r OLD_MODULE_PATH < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-OLD_MODULE_PATH-${CSID}" 2>/dev/null || OLD_MODULE_PATH=""
# index path preferred — avoids dotted-path errors on src/ layouts
IFS= read -r SMOKE_INDEX < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-resolve-index-${CSID}" 2>/dev/null || SMOKE_INDEX=""
INDEX_PATH=""
if [ -n "$SMOKE_INDEX" ]; then
    python "${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/bin/cm_json.py" index-path --input "$SMOKE_INDEX" --module "$OLD_MODULE_PATH" --name old-index-path --output-prefix "codemap-${_CM_PROJ}"
    IFS= read -r INDEX_PATH < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-old-index-path-${CSID}" 2>/dev/null || INDEX_PATH=""
fi
if [ -n "$INDEX_PATH" ]; then
    old_file_path="$INDEX_PATH"
else
    # dotted-path conversion may be wrong for src/ layouts — verify before git mv
    old_file_path=$(echo "$OLD_MODULE_PATH" | tr '.' '/').py
fi
IFS= read -r NEW_REF < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-NEW_REF-${CSID}" 2>/dev/null || NEW_REF=""
# check basename not non-existence — index branch resolves real path (incl. __init__.py); non-existence guard never fires where needed
case "$old_file_path" in
    */__init__.py|__init__.py)
        printf "! %s resolves to a package __init__.py (%s) — package directory rename (git mv) is out of scope for this skill; use 'git mv %s %s' directly\n" \
            "$OLD_MODULE_PATH" \
            "$old_file_path" \
            "${OLD_MODULE_PATH//.//}" \
            "${NEW_REF//.//}" >&2
        exit 1
        ;;
esac
[ -f "$old_file_path" ] || { printf "! File not found: %s\n" "$old_file_path" >&2; exit 1; }
printf '%s\n' "$old_file_path" > "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-old-file-path-${CSID}"
git ls-files --error-unmatch -- "$old_file_path" >/dev/null 2>&1 || { printf "! Untracked source — tracked-file module moves require Git tracking\n" >&2; exit 1; }
git status --porcelain -- "$old_file_path"
```

- Output prefix `??` (untracked source): stop before edits and report that tracked-file module moves require Git tracking. Never add, commit, or stash automatically.
- Unmerged/conflicted paths: stop before edits; report the conflict.
- Otherwise, preserve uncommitted user edits and proceed with the tracked source. `git mv` retains its current bytes; a dirty status alone is not a stop condition.

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_CM_PROJ=$(git rev-parse --show-toplevel 2>/dev/null | xargs basename 2>/dev/null || echo "cm")
IFS= read -r old_file_path < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-old-file-path-${CSID}" 2>/dev/null || old_file_path=""
IFS= read -r NEW_MODULE_PATH < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-NEW_REF-${CSID}" 2>/dev/null || NEW_MODULE_PATH=""
IFS= read -r OLD_MODULE_PATH < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-OLD_MODULE_PATH-${CSID}" 2>/dev/null || OLD_MODULE_PATH=""
# index lookup catches collision + stale-index cases only — src/ prefix comes from old_file_path in the fallback below, not from this lookup
IFS= read -r SMOKE_INDEX < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-resolve-index-${CSID}" 2>/dev/null || SMOKE_INDEX=""
new_file_path=""
if [ -n "$SMOKE_INDEX" ]; then
    python "${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/bin/cm_json.py" index-path --input "$SMOKE_INDEX" --module "$NEW_MODULE_PATH" --name new-index-path --output-prefix "codemap-${_CM_PROJ}"
    IFS= read -r new_file_path < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-new-index-path-${CSID}" 2>/dev/null || new_file_path=""
fi
if [ -z "$new_file_path" ]; then
    # fallback dotted→path; import root = old_file_path minus whole dotted→/ relative path (bash % strip, not sed: "/" delim collides)
    # a dirname-based strip was a no-op for top-level modules → ".newtop.py" / "srcnewtop.py"
    _OLD_REL="$(printf '%s' "$OLD_MODULE_PATH" | tr '.' '/').py"
    _IMPORT_ROOT="${old_file_path%"$_OLD_REL"}"
    [ "${_IMPORT_ROOT}${_OLD_REL}" = "$old_file_path" ] || { printf "! Cannot derive import root: %s does not end in %s — stop before edits\n" "$old_file_path" "$_OLD_REL" >&2; exit 1; }
    case "$_IMPORT_ROOT" in ""|*/) ;; *) printf "! Import root '%s' is not a directory prefix of %s — stop before edits\n" "$_IMPORT_ROOT" "$old_file_path" >&2; exit 1 ;; esac
    new_file_path="${_IMPORT_ROOT}$(printf '%s' "$NEW_MODULE_PATH" | tr '.' '/').py"
fi
case "${new_file_path##*/}" in .*) printf "! Rename target basename starts with '.': %s — stop before edits\n" "$new_file_path" >&2; exit 1 ;; esac
case "$new_file_path" in */*) [ -d "${new_file_path%/*}" ] || { printf "! Rename target directory missing: %s — create the package first; stop before edits\n" "${new_file_path%/*}" >&2; exit 1; } ;; esac
# validate target before git mv — empty/colliding NEW_REF must never reach it silently
if [ -z "$new_file_path" ] || [ "$new_file_path" = "$old_file_path" ]; then
    printf "! Invalid rename target derived from NEW_REF='%s' — got empty or unchanged path\n" "$NEW_MODULE_PATH" >&2
    exit 1
fi
case "$new_file_path" in
    *.py) ;;
    *) printf "! Invalid rename target: '%s' does not end in .py\n" "$new_file_path" >&2; exit 1 ;;
esac
# explicit guard, not an accident of git mv's refusal — also catches a stale-index phantom new_file_path
[ -e "$new_file_path" ] || [ -L "$new_file_path" ] && { printf "! Rename target already exists: %s\n" "$new_file_path" >&2; exit 1; }
git ls-files --error-unmatch -- "$new_file_path" >/dev/null 2>&1 && { printf "! Rename target already tracked: %s\n" "$new_file_path" >&2; exit 1; }
git mv -- "$old_file_path" "$new_file_path" || { printf "! git mv failed: %s -> %s\n" "$old_file_path" "$new_file_path" >&2; exit 1; }
```

**5b — Direct imports**:

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_CM_PROJ=$(git rev-parse --show-toplevel 2>/dev/null | xargs basename 2>/dev/null || echo "cm")
IFS= read -r OLD_MODULE_PATH < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-OLD_MODULE_PATH-${CSID}" 2>/dev/null || OLD_MODULE_PATH=""
python "${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/bin/find_module_refs.py" --kind import --module "$OLD_MODULE_PATH"
# attribute uses after a parent import (`import mypackage` then `mypackage.old_name.func()`) carry no import line naming the module
python "${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/bin/find_module_refs.py" --kind attribute --module "$OLD_MODULE_PATH"
```

Edit each `import mypackage.old_name` → `import mypackage.new_name`, indented ones inside functions, `try` or `if TYPE_CHECKING:` blocks included, and each attribute use `mypackage.old_name.<attr>` → `mypackage.new_name.<attr>`. Cross-check that every Step 2 `imported_by` importer has at least one hit here or in 5c; an importer with none needs a whole-file read before you move on.

**5c — From-imports**:

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_CM_PROJ=$(git rev-parse --show-toplevel 2>/dev/null | xargs basename 2>/dev/null || echo "cm")
IFS= read -r OLD_MODULE_PATH < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-OLD_MODULE_PATH-${CSID}" 2>/dev/null || OLD_MODULE_PATH=""
python "${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/bin/find_module_refs.py" --kind from-import --module "$OLD_MODULE_PATH"
```

Edit each `from mypackage.old_name import` → `from mypackage.new_name import`, indented ones included.

**5d — `__init__.py` relative re-exports**:

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_CM_PROJ=$(git rev-parse --show-toplevel 2>/dev/null | xargs basename 2>/dev/null || echo "cm")
IFS= read -r OLD_MODULE_PATH < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-OLD_MODULE_PATH-${CSID}" 2>/dev/null || OLD_MODULE_PATH=""
IFS= read -r old_file_path < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-old-file-path-${CSID}" 2>/dev/null || old_file_path=""
# restrict to the real pkg dir from 5a's old_file_path — helper's dotted default misses src/ layouts
case "$old_file_path" in */*) _PKG_SCOPE="${old_file_path%/*}" ;; *) _PKG_SCOPE="." ;; esac
python "${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/bin/find_module_refs.py" --kind relative --module "$OLD_MODULE_PATH" --scope "$_PKG_SCOPE"
```

Edit each `from .old_name import` → `from .new_name import`, only in expected package directory; skip unrelated packages.

**5e — pyproject.toml / setup.cfg**:

```bash
# timeout: 3000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_CM_PROJ=$(git rev-parse --show-toplevel 2>/dev/null | xargs basename 2>/dev/null || echo "cm")
IFS= read -r OLD_MODULE_PATH < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-OLD_MODULE_PATH-${CSID}" 2>/dev/null || OLD_MODULE_PATH=""
# full path, not OLD_BASENAME — avoids false positives on names like 'utils'
python "${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/bin/find_module_refs.py" --kind config --module "$OLD_MODULE_PATH"
```

Edit matching old-module-path `packages` / `install_requires`. Never grep bare `OLD_BASENAME`; too broad.

**5f — Sphinx docstring `:mod:` refs**:

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_CM_PROJ=$(git rev-parse --show-toplevel 2>/dev/null | xargs basename 2>/dev/null || echo "cm")
IFS= read -r OLD_MODULE_PATH < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-OLD_MODULE_PATH-${CSID}" 2>/dev/null || OLD_MODULE_PATH=""
# full path first to narrow scope, fallback to basename if no match (helper says so on stderr)
python "${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/bin/find_module_refs.py" --kind sphinx-mod --module "$OLD_MODULE_PATH"
```

Edit each `:mod:` ref to new module path. Basename-only matches require surrounding expected-package context.
