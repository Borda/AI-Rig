---
name: rename-refs
description: |
  Atomic rename of Python symbols or modules via the structural index — static callers, import sites, __all__ re-exports, Sphinx cross-refs; optional deprecated alias (--deprecate) or hard-delete (--remove-if-no-callers).
  TRIGGER: "rename X to Y" (function/class/method/module), "move module X to Y", "update all references to X".
  SKIP: non-Python; index not built (/codemap-py:scan-codebase first); local-variable rename; grep-only rename wanted; 1:N symbol splits; package directory rename (git mv).
argument-hint: symbol <old_qname> <new_qname> [--dry-run] [--deprecate[="@deprecated(...)"|"@deprecated_class(...)"]] [--since <ver>] [--removed-in <ver>] [--remove-if-no-callers] | module <old_module_path> <new_module_path> [--dry-run]
allowed-tools: Bash, Read, Edit, Write, AskUserQuestion
model: sonnet
effort: medium
---

<objective>

Atomically rename Python symbol/module. Coverage:

- Definition site (def/class line)
- `__all__` re-exports in `__init__.py` files
- Import call sites across all callers (fn-rdeps + symbol line-range narrowing)
- Sphinx docstring cross-refs across `.py` and `.rst`
- Optional pyDeprecate `@deprecated` alias (`--deprecate`; needs `pyDeprecate`)
- Optional hard-delete when exhaustive=true + zero callers

**Subcommands**:

- `symbol <old_qname> <new_qname>` — function/class/method. qname bare (`MyClass`) or qualified (`MyClass.method`); matches symbol-local `qualified_name`. Module-qualified form (`module::symbol`) is not accepted by `find-symbol`.
- `module <old_module_path> <new_module_path>` — dotted path (`mypackage.old_name`). Renames file + all import lines.

**Flags**:

- `--dry-run` — show change sites; no edits
- `--deprecate[=<decorator>]` — symbol only: preserve old name as pyDeprecate `@deprecated` wrapper → new; needs `pyDeprecate`. Bare derives decorator from symbol type; value pins it (`--deprecate="@deprecated_class(...)"`).
- `--since <ver>` / `--removed-in <ver>` — deprecation decorator values; default `"?"`
- `--remove-if-no-callers` — symbol only: confirm then delete definition when exhaustive=true + zero callers

**Hard limits** (static-analysis boundary):

- `getattr(obj, "old_name")` — statically unbound string; Step 6 emits grep advisory
- Cross-repo callers — out of scope; public APIs need `--deprecate` + semver bump

IDE/LSP coverage preview → `--dry-run`. Only 1:1 renames.

NOT for: index builds (`/codemap-py:scan-codebase`); query-only work (`/codemap-py:query-code`); non-Python; ABC/Protocol symbols with subclass overrides. Static imports don't track overrides: manually review `fn-rdeps`; rename overrides explicitly. No `--index <path>`: always default project index. Monorepo `--root <pkg>` index from `/codemap-py:scan-codebase` isn't auto-resolved — `resolve_proj_index.py` derives PROJ only from git-root basename. Before rename, confirm path via `resolve_index_env.py` or keep `--root` consistent with default scans.

</objective>

<workflow>

## Step 0: Parse arguments

Parse `$ARGUMENTS` in one Bash block; write tokens to project-qualified tmpfiles. Later steps read tmpfiles because shell vars die at each Bash() boundary:

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_CM_PROJ=$(git rev-parse --show-toplevel 2>/dev/null | xargs basename 2>/dev/null || echo "cm")

SUBCOMMAND=$(echo "$ARGUMENTS" | awk '{print $1}')
OLD_REF=$(echo "$ARGUMENTS" | awk '{print $2}')
NEW_REF=$(echo "$ARGUMENTS" | awk '{print $3}')

echo "$ARGUMENTS" | grep -q -- '--dry-run'            && DRY_RUN="true"  || DRY_RUN="false"
echo "$ARGUMENTS" | grep -q -- '--remove-if-no-callers' && REMOVE_IF_ZERO="true" || REMOVE_IF_ZERO="false"

# POSIX sed, not PCRE — works on macOS BSD sed
SINCE_VER=$(echo "$ARGUMENTS" | sed -n 's/.*--since \([^ ]*\).*/\1/p' || echo "")
REMOVED_IN_VER=$(echo "$ARGUMENTS" | sed -n 's/.*--removed-in \([^ ]*\).*/\1/p' || echo "")

case "$SUBCOMMAND" in
    symbol|module) ;;
    *) printf "Usage: /codemap-py:rename-refs symbol <old> <new> [flags] | module <old_path> <new_path> [--dry-run]\n" >&2; exit 1 ;;
esac

# reject empty/flag-shaped OLD_REF/NEW_REF here — unvalidated reaches git mv, risks destructive target
[ -n "$OLD_REF" ] && [ -n "$NEW_REF" ] || { printf "! Usage: /codemap-py:rename-refs %s <old> <new> [flags] — old or new ref missing\n" "$SUBCOMMAND" >&2; exit 1; }
case "$OLD_REF" in --*) printf "! OLD_REF '%s' looks like a flag, not a target — check argument order\n" "$OLD_REF" >&2; exit 1 ;; esac
case "$NEW_REF" in --*) printf "! NEW_REF '%s' looks like a flag, not a target — check argument order\n" "$NEW_REF" >&2; exit 1 ;; esac

# --deprecate (alias) + --remove-if-no-callers (delete) conflict
echo "$ARGUMENTS" | grep -q -- '--deprecate' && [ "$REMOVE_IF_ZERO" = "true" ] && { printf "⚠ conflicting flags: --deprecate creates alias, --remove-if-no-callers deletes target; these are incompatible\n" >&2; rm -f "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-REMOVE_IF_ZERO-${CSID}"; exit 1; }

printf '%s\n' "$SUBCOMMAND"     > "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-SUBCOMMAND-${CSID}"
printf '%s\n' "$OLD_REF"        > "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-OLD_REF-${CSID}"
printf '%s\n' "$NEW_REF"        > "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-NEW_REF-${CSID}"
printf '%s\n' "$DRY_RUN"        > "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-DRY_RUN-${CSID}"
printf '%s\n' "$REMOVE_IF_ZERO" > "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-REMOVE_IF_ZERO-${CSID}"
printf '%s\n' "$SINCE_VER"      > "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-SINCE_VER-${CSID}"
printf '%s\n' "$REMOVED_IN_VER" > "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-REMOVED_IN_VER-${CSID}"

OLD_NAME="${OLD_REF##*::}"; OLD_NAME="${OLD_NAME##*.}"
NEW_NAME="${NEW_REF##*::}"; NEW_NAME="${NEW_NAME##*.}"
printf '%s\n' "$OLD_NAME" > "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-OLD_NAME-${CSID}"
printf '%s\n' "$NEW_NAME" > "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-NEW_NAME-${CSID}"
```

Parse bare or decorator-valued `--deprecate`:

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_CM_PROJ=$(git rev-parse --show-toplevel 2>/dev/null | xargs basename 2>/dev/null || echo "cm")
PARSE_OUT=$(python3 "${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/bin/parse_deprecate_args.py" --arguments="$ARGUMENTS" 2>"${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-parse-deprecate-err-${CSID}") || { _PARSE_RC=$?; printf "! parse_deprecate_args.py failed (exit %d): %s\n" "$_PARSE_RC" "$(cat "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-parse-deprecate-err-${CSID}" 2>/dev/null)" >&2; exit 1; }
FLAG_FILE=$(printf '%s\n' "$PARSE_OUT" | sed -n 1p)   # pid-qualified path (SEC-M7)
DEC_FILE=$(printf '%s\n' "$PARSE_OUT" | sed -n 2p)
[ -n "$FLAG_FILE" ] || { printf "! parse_deprecate_args.py returned no paths\n" >&2; exit 1; }
IFS= read -r DEPRECATE < "$FLAG_FILE" 2>/dev/null || DEPRECATE="false"
IFS= read -r DEPRECATE_DECORATOR < "$DEC_FILE" 2>/dev/null || DEPRECATE_DECORATOR=""
printf '%s\n' "$DEPRECATE"           > "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-DEPRECATE-${CSID}"
printf '%s\n' "$DEPRECATE_DECORATOR" > "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-DEPRECATE_DECORATOR-${CSID}"
```

Reject `$ARGUMENTS` `--` tokens outside allowlist (`--dry-run`, `--deprecate`, `--since`, `--removed-in`, `--remove-if-no-callers`). Print `! Unknown flag(s): --<token>. Supported flags: --dry-run, --deprecate[=<decorator>], --since, --removed-in, --remove-if-no-callers. Re-invoke with corrected flags.`; stop. Never AskUserQuestion: fail-fast keeps worst-case AQQ path at 4 (STALE-index, multiple-matches, apply/dry-run, hard-delete confirmation).

## Step 1: Validate index

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_CM_PROJ=$(git rev-parse --show-toplevel 2>/dev/null | xargs basename 2>/dev/null || echo "cm")
python3 "${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/bin/resolve_index_env.py" \
    --output-prefix "codemap-${_CM_PROJ}" 2>/dev/null  # timeout: 5000
IFS= read -r PROJ < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-resolve-proj-${CSID}" 2>/dev/null || PROJ=""
IFS= read -r INDEX < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-resolve-index-${CSID}" 2>/dev/null || INDEX=""
[ -n "$PROJ" ] || { printf "! resolve_index_env.py failed — check Python availability and CLAUDE_PLUGIN_ROOT\n"; exit 1; }
[ -n "$INDEX" ] || { echo "! index not found — run /codemap-py:scan-codebase first"; exit 1; }
SMOKE_JSON=$(python3 "${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/bin/check_index_smoke.py" --index-path "$INDEX")  # timeout: 10000
# python3 not jq — jq absent on stock Windows/CI; already required by every bin/ helper here
# stale is bool or null (check_index_smoke.py) — normalize case + null, then emit so the branch below reads a printed value, not an invisible var
STALE=$(printf '%s' "$SMOKE_JSON" | python3 -c "import sys,json; v=json.load(sys.stdin).get('stale'); print('unknown' if v is None else str(v).lower())" 2>/dev/null || echo "unknown")
printf 'STALE=%s\n' "$STALE"
```

- `STALE=true` → `AskUserQuestion`: (a) Proceed anyway (callers may be incomplete); (b) Abort (first re-run /codemap-py:scan-codebase). Abort → print "Run `/codemap-py:scan-codebase` then re-invoke"; stop.
- `STALE=unknown` (JSON parse failed, or `stale` absent/null) → print `⚠ Could not determine index freshness — proceeding but callers may be incomplete`; continue cautiously, never treat fresh.

## Step 2: Resolve targets

All structural queries/rebuilds use `codemap-py` dispatcher, never `scan-query`/`scan-index`. Both routes use same in-engine shared query/exclusive scan leases, preventing mid-rename scan races. Dispatcher also owns interpreter probe; aliases skip it (`127` when no eligible CPython, including invalid `CODEMAP_PYTHON`), deprecated shims removed no earlier than `1.0.0`. Every block uses PATH-literal `codemap-py query …` / `codemap-py index …`; expansion-bearing path misses bare-name allow prefix, prompts each call. If bare command unavailable, use installed plugin's absolute `bin/codemap-py` launcher interactively.

**Symbol subcommand**:

```bash
# timeout: 25000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_CM_PROJ=$(git rev-parse --show-toplevel 2>/dev/null | xargs basename 2>/dev/null || echo "cm")
IFS= read -r OLD_REF < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-OLD_REF-${CSID}" 2>/dev/null || OLD_REF=""
FIND_SYMBOL_JSON=$(codemap-py query --timeout 20 find-symbol "$OLD_REF" --limit 0)
printf '%s\n' "$FIND_SYMBOL_JSON" > "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-find-symbol-json-${CSID}"
```

`find-symbol` returns `matches`, each `{name, qualified_name, type, module, path, start_line, end_line, source}` (same as `query-code` `symbol`). Edits use `path`, `start_line`, `end_line`; exact filter uses `qualified_name`; Step 4e reads `.matches[0].type`.

- 0 matches → print `! Symbol '$OLD_REF' not found. Verify with: codemap-py query find-symbol <pattern>`; stop.
- Multiple → `AskUserQuestion` listing candidate name/type/module/path. After choice, narrow sentinel so every downstream `.matches[0]` is correct:
  ```bash
  # timeout: 5000
  export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
  _CM_PROJ=$(git rev-parse --show-toplevel 2>/dev/null | xargs basename 2>/dev/null || echo "cm")
  CHOSEN_IDX="<0-based index of the match selected via AskUserQuestion>"
  _FS_SENTINEL="${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-find-symbol-json-${CSID}"
  _TMP="${_FS_SENTINEL}.narrowed"
  python3 -c "import json,sys; d=json.load(open(sys.argv[1])); json.dump({'pattern': d.get('pattern'), 'matches': [d['matches'][int(sys.argv[2])]], 'count': 1}, open(sys.argv[3], 'w'))" "$_FS_SENTINEL" "$CHOSEN_IDX" "$_TMP" && mv "$_TMP" "$_FS_SENTINEL"
  ```

Full qname: `<module>::<qualified_name>` (e.g. `mypackage.auth::validate_token`).

```bash
# timeout: 25000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_CM_PROJ=$(git rev-parse --show-toplevel 2>/dev/null | xargs basename 2>/dev/null || echo "cm")
IFS= read -r FIND_SYMBOL_JSON <  "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-find-symbol-json-${CSID}" 2>/dev/null || FIND_SYMBOL_JSON="{}"
SYM_MODULE=$(printf '%s' "$FIND_SYMBOL_JSON" | python3 -c "import sys,json; m=json.load(sys.stdin).get('matches') or [{}]; print(m[0].get('module',''))" 2>/dev/null || echo "")
SYM_QNAME=$(printf '%s' "$FIND_SYMBOL_JSON" | python3 -c "import sys,json; m=json.load(sys.stdin).get('matches') or [{}]; print(m[0].get('qualified_name',''))" 2>/dev/null || echo "")
printf '%s\n' "$SYM_MODULE" > "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-sym-module-${CSID}"
printf '%s\n' "$SYM_QNAME"  > "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-sym-qname-${CSID}"
[ -n "$SYM_MODULE" ] && [ -n "$SYM_QNAME" ] || { printf "! could not extract module/qualified_name from find-symbol result\n" >&2; exit 1; }
# wire OLD_MODULE_PATH here for 4c — OLD_REF can't carry module for symbol rename, don't re-derive
printf '%s\n' "$SYM_MODULE" > "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-OLD_MODULE_PATH-${CSID}"
RDEPS_JSON=$(codemap-py query --timeout 20 fn-rdeps "${SYM_MODULE}::${SYM_QNAME}")
RDEP_COUNT=$(printf '%s' "$RDEPS_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('count',0))" 2>/dev/null || echo "0")
# forward-first, fail-closed — query_complete wins even when false. jq `//` falls through on false too: false query_complete got overridden by true legacy exhaustive, arming destructive gate on incomplete graph.
EXHAUSTIVE=$(printf '%s' "$RDEPS_JSON" | python3 -c "import sys,json; i=json.load(sys.stdin).get('index',{}); v=i.get('query_complete', i.get('exhaustive', False)); print('true' if v is True else 'false')" 2>/dev/null || echo "false")
printf '%s\n' "$RDEP_COUNT"  > "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rdep-count-${CSID}"
printf '%s\n' "$EXHAUSTIVE"  > "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-EXHAUSTIVE-${CSID}"
```

`fn-rdeps` returns `{qname, called_by:[{caller, module, path}], count, index:{query_complete,...}}`.

- `called_by` has **no line numbers**; Step 4c runs `codemap-py query symbol <caller>` per entry.
- `EXHAUSTIVE` first reads `result["index"]["query_complete"]`; only if absent, use legacy `result["index"]["exhaustive"]`; neither → `false`. Incomplete → note in blast report.

**Module subcommand**:

```bash
# timeout: 25000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_CM_PROJ=$(git rev-parse --show-toplevel 2>/dev/null | xargs basename 2>/dev/null || echo "cm")
IFS= read -r OLD_REF < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-OLD_REF-${CSID}" 2>/dev/null || OLD_REF=""
OLD_MODULE_PATH="$OLD_REF"
printf '%s\n' "$OLD_MODULE_PATH" > "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-OLD_MODULE_PATH-${CSID}"
RDEPS_JSON=$(codemap-py query --timeout 20 rdeps "$OLD_REF")
RDEP_COUNT=$(printf '%s' "$RDEPS_JSON" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('imported_by') or []))" 2>/dev/null || echo "0")
# same forward-first, fail-closed read as the symbol branch
EXHAUSTIVE=$(printf '%s' "$RDEPS_JSON" | python3 -c "import sys,json; i=json.load(sys.stdin).get('index',{}); v=i.get('query_complete', i.get('exhaustive', False)); print('true' if v is True else 'false')" 2>/dev/null || echo "false")
printf '%s\n' "$RDEP_COUNT"  > "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rdep-count-${CSID}"
printf '%s\n' "$EXHAUSTIVE"  > "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-EXHAUSTIVE-${CSID}"
# --remove-if-no-callers needs explicit pass AND exhaustive
IFS= read -r REMOVE_IF_ZERO_ARG < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-REMOVE_IF_ZERO-${CSID}" 2>/dev/null || REMOVE_IF_ZERO_ARG="false"
[ "$REMOVE_IF_ZERO_ARG" = "true" ] && [ "$EXHAUSTIVE" != "true" ] && printf '%s\n' "false" > "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-REMOVE_IF_ZERO-${CSID}"
```

Returns `{imported_by:[...], index:{query_complete,...}}`; extract `RDEP_COUNT`, `EXHAUSTIVE`.

## Step 3: Blast-radius report + confirmation gate

Print:

```
Rename: <OLD_REF> → <NEW_REF>
Type: symbol | module
[symbol] Definition: <path>:<start_line>-<end_line>

Static callers: N (across N files)
  - src/foo/bar.py   (caller: module::fn)
  - src/foo/baz.py   (caller: module::other_fn)
  - tests/test_foo.py

Docstring refs: (will grep :func:/:class:/:meth:/:mod:/:attr: in Step 4d)

[if DEPRECATE]
Deprecation wrapper: <OLD_REF> kept as @deprecated alias → <NEW_REF>

⚠ Not covered (hard limits — grep advisories in Step 8):
  - getattr("old_name") dynamic dispatch
  - Cross-repo consumers
[if not EXHAUSTIVE]
⚠ Index non-exhaustive — some callers may not appear above
```

**Budget gate**: caller count > 50 → derive BRANCH and a free (non-colliding) output path first:

```bash
# timeout: 3000
BRANCH=$(git branch --show-current 2>/dev/null | tr '/' '-'); BRANCH="${BRANCH:-main}"
# never overwrite — only record of callers 51–N; same-day re-run would destroy manual-edit list
BLAST_OUT=".temp/output-rename-refs-blast-${BRANCH}-$(date +%Y-%m-%d).md"; _n=1
while [ -e "$BLAST_OUT" ]; do _n=$((_n+1)); BLAST_OUT=".temp/output-rename-refs-blast-${BRANCH}-$(date +%Y-%m-%d)-${_n}.md"; done
printf '%s\n' "$BLAST_OUT"
```

Write all callers to printed `$BLAST_OUT`, beginning with YAML header:

```yaml
---
Title:      rename-refs blast-radius — <OLD_REF>
Date:       <YYYY-MM-DD>
Scope:      <project name>
Focus:      caller enumeration (capped at 50 for edit pass)
Agents:     codemap-py:rename-refs
Outcome:    <N callers found; exhaustive: true/false>
Confidence: <exhaustive|partial>
Next steps: apply edits for callers 1–50; callers 51–N in this file require manual edit
Path:       → <the resolved $BLAST_OUT path>
---
```

Print path + count, then `⚠ >50 callers — capping edit pass at first 50. Callers 51–N listed in blast file as manual advisories.` Edit first 50 only. Step 7 labels residual old-name hits for 51–N "skipped callers"; expected, not missed dynamics.

**`--remove-if-no-callers` guards** — evaluated before any rename edits:

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_CM_PROJ=$(git rev-parse --show-toplevel 2>/dev/null | xargs basename 2>/dev/null || echo "cm")
IFS= read -r REMOVE_IF_ZERO < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-REMOVE_IF_ZERO-${CSID}" 2>/dev/null || REMOVE_IF_ZERO="false"
IFS= read -r DRY_RUN < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-DRY_RUN-${CSID}" 2>/dev/null || DRY_RUN="false"
IFS= read -r RDEP_COUNT < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rdep-count-${CSID}" 2>/dev/null || RDEP_COUNT="0"
IFS= read -r EXHAUSTIVE < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-EXHAUSTIVE-${CSID}" 2>/dev/null || EXHAUSTIVE="false"
# emit — the gate below decides on a printed value, not an invisible sentinel read
printf 'REMOVE_IF_ZERO=%s DRY_RUN=%s RDEP_COUNT=%s EXHAUSTIVE=%s\n' "$REMOVE_IF_ZERO" "$DRY_RUN" "$RDEP_COUNT" "$EXHAUSTIVE"
```

- `REMOVE_IF_ZERO=true` AND `RDEP_COUNT > 0` → print `! --remove-if-no-callers: N callers found. Remove all callers first or omit flag.`; stop **entire rename operation**.
- `REMOVE_IF_ZERO=true` AND `EXHAUSTIVE=false` → print `! --remove-if-no-callers requires exhaustive=true. Run /codemap-py:scan-codebase to ensure full coverage.`; stop **entire rename operation**.
- `REMOVE_IF_ZERO=true` AND `RDEP_COUNT == 0` AND `EXHAUSTIVE=true` AND `DRY_RUN=false` → `AskUserQuestion`: (a) Delete `$OLD_REF` — confirmed no callers; (b) Abort — keep file. Abort: stop. Delete: find-symbol line range, then `Read` block bounds. Verify `start_line` contains expected bare `OLD_NAME` or qualified `OLD_REF`; mismatch → print `! Symbol name mismatch at line <start_line>: expected <OLD_NAME>, index may be stale — run /codemap-py:scan-codebase first`; abort without delete. Only then `Edit`: remove definition from `def`/`class` through final body, including immediately preceding `@decorator` lines. Skip Steps 4a–4d. Print `ℹ Symbol had no callers — removed $OLD_REF without rename`; go Step 6.
- `REMOVE_IF_ZERO=true` AND `RDEP_COUNT == 0` AND `EXHAUSTIVE=true` AND `DRY_RUN=true` → record "would delete `$OLD_REF` — no callers, exhaustive" as a line in the dry-run report below; fall through to the `--dry-run` block. No `AskUserQuestion`, no `Edit` — dry-run means no edits, including the delete.
- Otherwise (`REMOVE_IF_ZERO=false`): proceed with normal rename flow.

**`--dry-run`**: derive branch and a free output path first, then write the report:

```bash
# timeout: 3000
BRANCH=$(git branch --show-current 2>/dev/null | tr '/' '-'); BRANCH="${BRANCH:-main}"
DRY_OUT=".temp/output-rename-refs-dry-${BRANCH}-$(date +%Y-%m-%d).md"; _n=1
while [ -e "$DRY_OUT" ]; do _n=$((_n+1)); DRY_OUT=".temp/output-rename-refs-dry-${BRANCH}-$(date +%Y-%m-%d)-${_n}.md"; done
printf '%s\n' "$DRY_OUT"
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

Print path; `AskUserQuestion`: (a) Apply for real (re-invoke without --dry-run); (b) Done. Stop.

Otherwise `AskUserQuestion`: (a) Apply edits; (b) Abort. Abort → stop.

## Step 4: Apply edits — symbol rename

Skip to Step 5 if `SUBCOMMAND=module`.

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
  CLS_RANGE=""
  if [ "$CLS" != "$OLD_REF" ]; then
      CLS_JSON=$(codemap-py query --timeout 20 find-symbol "$CLS" --limit 0)
      # find-symbol is unanchored re.IGNORECASE (query.py) — matches[0] may be a method or sibling class; filter exact
      CLS_RANGE=$(printf '%s' "$CLS_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); cls=sys.argv[1]; m=next((x for x in d.get('matches',[]) if x.get('type')=='class' and x.get('qualified_name')==cls), None); print(str(m['start_line'])+' '+str(m['end_line']) if m else '')" "$CLS" 2>/dev/null)
  fi
  printf '%s\n' "$CLS_RANGE" > "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-cls-range-${CSID}"
  [ -n "$CLS_RANGE" ] || printf "⚠ could not bound to class body — verify each hit\n"
  ```
- **`@property` descriptors**: a getter/setter/deleter triple shares one `qualified_name` — `find-symbol` (Step 2) and its multi-match narrowing return only the one match picked there (the getter, by convention). After getter rename, grep the class body (bound above; whole-file if unresolved) for `@old_name.setter` / `@old_name.deleter`; rename to `@new_name.setter` / `@new_name.deleter`. Otherwise descriptor breaks: renamed getter + `@old_name.setter` raises `AttributeError` during class definition.
  ```bash
  export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
  _CM_PROJ=$(git rev-parse --show-toplevel 2>/dev/null | xargs basename 2>/dev/null || echo "cm")
  IFS= read -r OLD_NAME < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-OLD_NAME-${CSID}" 2>/dev/null || OLD_NAME=""
  IFS= read -r CLS_RANGE < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-cls-range-${CSID}" 2>/dev/null || CLS_RANGE=""
  set -- $CLS_RANGE; CLS_START="${1:-}"; CLS_END="${2:-}"
  if [ -n "$CLS_START" ]; then
      grep -n "@${OLD_NAME}\.setter\|@${OLD_NAME}\.deleter" "<path>" | awk -F: -v s="$CLS_START" -v e="$CLS_END" '$1>=s && $1<=e'  # timeout: 3000
  else
      grep -n "@${OLD_NAME}\.setter\|@${OLD_NAME}\.deleter" "<path>"  # timeout: 3000
  fi
  ```
- **`@typing.overload` stubs**: after implementation rename, grep same file (bound to class body above, when resolved) + sibling `.pyi` (whole-file — independent line numbering, class bound doesn't apply) for `@overload`-decorated `def old_name(`. `find-symbol` returns only the implementation, not stubs/files. Rename all overload stubs to `new_name`:
  ```bash
  export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
  _CM_PROJ=$(git rev-parse --show-toplevel 2>/dev/null | xargs basename 2>/dev/null || echo "cm")
  IFS= read -r OLD_NAME < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-OLD_NAME-${CSID}" 2>/dev/null || OLD_NAME=""
  IFS= read -r CLS_RANGE < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-cls-range-${CSID}" 2>/dev/null || CLS_RANGE=""
  set -- $CLS_RANGE; CLS_START="${1:-}"; CLS_END="${2:-}"
  if [ -n "$CLS_START" ]; then
      grep -n "@overload" "<path>" | awk -F: -v s="$CLS_START" -v e="$CLS_END" '$1>=s && $1<=e' | grep -A1 "def $OLD_NAME("  # timeout: 3000
  else
      grep -n "@overload" "<path>" | grep -A1 "def $OLD_NAME("  # timeout: 3000
  fi
  grep -n "@overload" "<path%.py>.pyi" 2>/dev/null | grep -A1 "def $OLD_NAME("  # timeout: 3000
  ```

**4b — `__all__` re-exports**:

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_CM_PROJ=$(git rev-parse --show-toplevel 2>/dev/null | xargs basename 2>/dev/null || echo "cm")
IFS= read -r OLD_NAME < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-OLD_NAME-${CSID}" 2>/dev/null || OLD_NAME=""
IFS= read -r FIND_SYMBOL_JSON < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-find-symbol-json-${CSID}" 2>/dev/null || FIND_SYMBOL_JSON="{}"
# scope to pkg tree — avoids same-name false positives
OLD_FILE_PATH=$(printf '%s' "$FIND_SYMBOL_JSON" | python3 -c "import sys,json; m=json.load(sys.stdin).get('matches') or [{}]; print(m[0].get('path') or '.')" 2>/dev/null || echo ".")
PKG_DIR=$(dirname "$OLD_FILE_PATH")
printf '%s\n' "$PKG_DIR" > "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-pkg-dir-${CSID}"
_GREP_SCOPE="${PKG_DIR:-.}"
grep -rn "\"$OLD_NAME\"\|'$OLD_NAME'" --include="__init__.py" "$_GREP_SCOPE"
```

For each `__all__` hit, Edit `"old_name"` → `"new_name"`.

**4c — Import call sites** (per caller from `called_by`):

Track import-edited files in `PROCESSED_IMPORT_FILES` tmpfile, and skipped callers (query miss) in a sibling tmpfile — Step 7 reads it for the completion summary; three callers in one file still need one import edit. Initialize both before loop:

```bash
# timeout: 3000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_CM_PROJ=$(git rev-parse --show-toplevel 2>/dev/null | xargs basename 2>/dev/null || echo "cm")
printf '' > "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-processed-import-files-${CSID}"
printf '' > "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-skipped-callers-${CSID}"
```

Before each caller-file import edit, check + mark:

```bash
# timeout: 3000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_CM_PROJ=$(git rev-parse --show-toplevel 2>/dev/null | xargs basename 2>/dev/null || echo "cm")
TARGET_FILE="<caller_path>"
grep -qxF "$TARGET_FILE" "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-processed-import-files-${CSID}" && SKIP_IMPORT="true" || SKIP_IMPORT="false"
[ "$SKIP_IMPORT" = "false" ] && echo "$TARGET_FILE" >> "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-processed-import-files-${CSID}"
```

Edit imports only when `SKIP_IMPORT=false`.

Per caller (`called_by[i].caller` already `module::function`; pass directly to `codemap-py query`):

1. Run `codemap-py query symbol "<caller_qname>"` — timeout: 10000; result `{symbols:[{path, start_line, end_line, qualified_name, ...}]}`
   - 0 matches → log `⚠ symbol not found for caller <caller_qname> — skipping caller`, append `<caller_qname>` to the skipped-callers tmpfile (`echo "<caller_qname>" >> "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-skipped-callers-${CSID}"`), and continue
   - Filter `symbols[]` by `qualified_name`: exact preferred, else first
   - Use matched entry's `path`, `start_line`, `end_line` for step 2
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
grep -n "from ${_OLD_MODULE_GREP} import .*\b${OLD_NAME}\b\|from .*\\.${_OLD_BASENAME_GREP} import .*\b${OLD_NAME}\b\|^import .*\b${OLD_NAME}\b" "<file>"
```

Edit matched import lines only after verifying `from <module>` references expected module; skip unrelated imports of `OLD_NAME`. Dedup block already updated `processed-import-files`.

**4d — Sphinx docstring cross-refs**:

```bash
# timeout: 10000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_CM_PROJ=$(git rev-parse --show-toplevel 2>/dev/null | xargs basename 2>/dev/null || echo "cm")
IFS= read -r OLD_NAME < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-OLD_NAME-${CSID}" 2>/dev/null || OLD_NAME=""
# full module prefix — avoids false positive on same bare name elsewhere
IFS= read -r OLD_MODULE_PATH < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-OLD_MODULE_PATH-${CSID}" 2>/dev/null || OLD_MODULE_PATH=""
# matches bare name anywhere — verify module before edit
grep -rn ":func:\`[^']*$OLD_NAME[^']*\`\|:class:\`[^']*$OLD_NAME[^']*\`\|:meth:\`[^']*$OLD_NAME[^']*\`\|:mod:\`[^']*$OLD_NAME[^']*\`\|:attr:\`[^']*$OLD_NAME[^']*\`" --include="*.py" --include="*.rst" .
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
IFS= read -r FIND_SYMBOL_JSON < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-find-symbol-json-${CSID}" 2>/dev/null || FIND_SYMBOL_JSON="{}"
SYMBOL_TYPE=$(printf '%s' "$FIND_SYMBOL_JSON" | python3 -c "import sys,json; m=json.load(sys.stdin).get('matches') or [{}]; print(m[0].get('type') or 'function')" 2>/dev/null || echo "function")

IFS= read -r OLD_NAME < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-OLD_NAME-${CSID}" 2>/dev/null || OLD_NAME=""
IFS= read -r NEW_NAME < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-NEW_NAME-${CSID}" 2>/dev/null || NEW_NAME=""
IFS= read -r DEPRECATE_DECORATOR < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-DEPRECATE_DECORATOR-${CSID}" 2>/dev/null || DEPRECATE_DECORATOR=""
IFS= read -r SINCE_VER < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-SINCE_VER-${CSID}" 2>/dev/null || SINCE_VER=""
IFS= read -r REMOVED_IN_VER < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-REMOVED_IN_VER-${CSID}" 2>/dev/null || REMOVED_IN_VER=""
if [ -n "$DEPRECATE_DECORATOR" ]; then
    DEPRECATION_CODE=$(python3 "${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/bin/gen_deprecation_wrapper.py" \
        --decorator "$DEPRECATE_DECORATOR" \
        --old-name "$OLD_NAME" \
        ${REMOVED_IN_VER:+--removed-in "$REMOVED_IN_VER"}) || { echo "! gen_deprecation_wrapper failed — check symbol type and names"; exit 1; }
else
    DEPRECATION_CODE=$(python3 "${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/bin/gen_deprecation_wrapper.py" \
        --type "$SYMBOL_TYPE" \
        --old-name "$OLD_NAME" \
        --new-name "$NEW_NAME" \
        ${SINCE_VER:+--since "$SINCE_VER"} \
        ${REMOVED_IN_VER:+--removed-in "$REMOVED_IN_VER"}) || { echo "! gen_deprecation_wrapper failed — check symbol type and names"; exit 1; }
fi
```

Insert `$DEPRECATION_CODE` immediately after new definition (`end_line` from Step 2). Target needs pyDeprecate; otherwise inserted `from deprecate import ...` raises import-time `ImportError`. Surface Step 6 advisory.

Type→decorator mapping:

- `"class"` → `@deprecated_class(target=NewName, ...)`; transparent proxy preserves `isinstance`
- `"function"` / `"method"` → `@deprecated(target=new_fn, ...)`; `...` body, pydeprecate forwards calls

## Step 5: Apply edits — module rename

**5a — File rename**:

```bash
# timeout: 3000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_CM_PROJ=$(git rev-parse --show-toplevel 2>/dev/null | xargs basename 2>/dev/null || echo "cm")
git rev-parse --is-inside-work-tree 2>/dev/null || { printf "⚠ not a git repo — cannot use git mv; rename file manually\n" >&2; exit 1; }
IFS= read -r OLD_MODULE_PATH < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-OLD_MODULE_PATH-${CSID}" 2>/dev/null || OLD_MODULE_PATH=""
# index path preferred — avoids dotted-path errors on src/ layouts
IFS= read -r SMOKE_INDEX < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-resolve-index-${CSID}" 2>/dev/null || SMOKE_INDEX=""
INDEX_PATH=""
if [ -n "$SMOKE_INDEX" ]; then
    INDEX_PATH=$(python3 -c "import json,sys; idx=json.load(open(sys.argv[1])); print(next((m.get('path','') for m in idx.get('modules',[]) if m.get('name')==sys.argv[2]), ''))" "$SMOKE_INDEX" "$OLD_MODULE_PATH" 2>/dev/null || echo "")
fi
if [ -n "$INDEX_PATH" ]; then
    old_file_path="$INDEX_PATH"
else
    # dotted-path conversion may be wrong for src/ layouts — verify before git mv
    old_file_path=$(echo "$OLD_MODULE_PATH" | tr '.' '/').py
fi
# MED-15: check basename not non-existence — index branch resolves real path (incl. __init__.py); non-existence guard never fires where needed
case "$old_file_path" in
    */__init__.py|__init__.py)
        printf "! %s resolves to a package __init__.py (%s) — package directory rename (git mv) is out of scope for this skill; use 'git mv %s %s' directly\n" \
            "$OLD_MODULE_PATH" \
            "$old_file_path" \
            "$(echo "$OLD_MODULE_PATH" | tr '.' '/')" \
            "$(echo "$(cat "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-NEW_REF-${CSID}")" | tr '.' '/')" >&2
        exit 1
        ;;
esac
[ -f "$old_file_path" ] || { printf "! File not found: %s\n" "$old_file_path" >&2; exit 1; }
printf '%s\n' "$old_file_path" > "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-old-file-path-${CSID}"
git status --porcelain "$old_file_path"
```

- Output prefix `??`: print `! File is untracked — add to git first: git add "<old_file_path>"`; stop.
- Any other non-empty prefix (M, A, D, R, C, U): print `! File has uncommitted changes — commit or stash before module rename.`; stop.
- Output empty (clean tracked file): proceed.

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
    new_file_path=$(python3 -c "import json,sys; idx=json.load(open(sys.argv[1])); print(next((m.get('path','') for m in idx.get('modules',[]) if m.get('name')==sys.argv[2]), ''))" "$SMOKE_INDEX" "$NEW_MODULE_PATH" 2>/dev/null || echo "")
fi
if [ -z "$new_file_path" ]; then
    # fallback dotted→path, src/ prefix from old_file_path — bash % strip not sed: multi-dot path collides w/ sed's "/" delim
    _OLD_MODULE_DIR=$(echo "${OLD_MODULE_PATH%.*}" | tr '.' '/')
    _OLD_DIRNAME=$(dirname "$old_file_path")
    _OLD_PREFIX="${_OLD_DIRNAME%"$_OLD_MODULE_DIR"}"
    new_file_path="${_OLD_PREFIX}$(echo "$NEW_MODULE_PATH" | tr '.' '/').py"
fi
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
[ -e "$new_file_path" ] && { printf "! Rename target already exists: %s\n" "$new_file_path" >&2; exit 1; }
git mv "$old_file_path" "$new_file_path" || { printf "! git mv failed: %s -> %s\n" "$old_file_path" "$new_file_path" >&2; exit 1; }
```

**5b — Direct imports**:

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_CM_PROJ=$(git rev-parse --show-toplevel 2>/dev/null | xargs basename 2>/dev/null || echo "cm")
IFS= read -r OLD_MODULE_PATH < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-OLD_MODULE_PATH-${CSID}" 2>/dev/null || OLD_MODULE_PATH=""
# dots literal, not any-char
_OLD_MODULE_GREP=$(printf '%s' "$OLD_MODULE_PATH" | sed 's/\./\\./g')
grep -rn "^import ${_OLD_MODULE_GREP}\b\|^import ${_OLD_MODULE_GREP} as " --include="*.py" .
```

Edit each `import mypackage.old_name` → `import mypackage.new_name`.

**5c — From-imports**:

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_CM_PROJ=$(git rev-parse --show-toplevel 2>/dev/null | xargs basename 2>/dev/null || echo "cm")
IFS= read -r OLD_MODULE_PATH < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-OLD_MODULE_PATH-${CSID}" 2>/dev/null || OLD_MODULE_PATH=""
# dots literal, not any-char
_OLD_MODULE_GREP=$(printf '%s' "$OLD_MODULE_PATH" | sed 's/\./\\./g')
grep -rn "^from ${_OLD_MODULE_GREP} import\|^from ${_OLD_MODULE_GREP} as " --include="*.py" .
```

Edit each `from mypackage.old_name import` → `from mypackage.new_name import`.

**5d — `__init__.py` relative re-exports**:

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_CM_PROJ=$(git rev-parse --show-toplevel 2>/dev/null | xargs basename 2>/dev/null || echo "cm")
IFS= read -r OLD_MODULE_PATH < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-OLD_MODULE_PATH-${CSID}" 2>/dev/null || OLD_MODULE_PATH=""
OLD_BASENAME="${OLD_MODULE_PATH##*.}"
# restrict to pkg dir — avoids false positives elsewhere
OLD_PKG_DIR=$(echo "${OLD_MODULE_PATH%.*}" | tr '.' '/')
grep -rn "from \.*[^.]*\.${OLD_BASENAME} import\|from \.${OLD_BASENAME} import\|from \.${OLD_BASENAME} as " --include="__init__.py" "${OLD_PKG_DIR:-.}" 2>/dev/null
```

Edit each `from .old_name import` → `from .new_name import`, only in expected package directory; skip unrelated packages.

**5e — pyproject.toml / setup.cfg**:

```bash
# timeout: 3000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_CM_PROJ=$(git rev-parse --show-toplevel 2>/dev/null | xargs basename 2>/dev/null || echo "cm")
IFS= read -r OLD_MODULE_PATH < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-OLD_MODULE_PATH-${CSID}" 2>/dev/null || OLD_MODULE_PATH=""
# full path, not OLD_BASENAME — avoids false positives on names like 'utils'
grep -rn "${OLD_MODULE_PATH}" pyproject.toml setup.cfg 2>/dev/null
```

Edit matching old-module-path `packages` / `install_requires`. Never grep bare `OLD_BASENAME`; too broad.

**5f — Sphinx docstring `:mod:` refs**:

```bash
# timeout: 5000
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_CM_PROJ=$(git rev-parse --show-toplevel 2>/dev/null | xargs basename 2>/dev/null || echo "cm")
IFS= read -r OLD_MODULE_PATH < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-rename-OLD_MODULE_PATH-${CSID}" 2>/dev/null || OLD_MODULE_PATH=""
OLD_BASENAME="${OLD_MODULE_PATH##*.}"
# full path first to narrow scope, fallback to basename if no match
grep -rn ":mod:\`[^']*${OLD_MODULE_PATH}[^']*\`" --include="*.py" --include="*.rst" . 2>/dev/null || \
grep -rn ":mod:\`[^']*${OLD_BASENAME}[^']*\`" --include="*.py" --include="*.rst" .
```

Edit each `:mod:` ref to new module path. Basename-only matches require surrounding expected-package context.

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

Expected: old name absent, except deprecated alias with `--deprecate`.

Old name outside deprecated alias → list residual files in Step 7 advisory. Hard-limit cases: dynamic refs, template strings, out-of-scope config strings.

## Step 7: Summary

Read the skipped-callers tmpfile (`${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-skipped-callers-${CSID}`, from 4c) before printing — its content plus the Step 3 cap decide the header below.

Print:

```
[if cap hit at Step 3, OR skipped-callers tmpfile non-empty]
⚠ Renamed (PARTIAL) — M of N callers updated; <cap residual + skipped-caller count> require manual edit
[else]
✓ Renamed: <OLD_REF> → <NEW_REF>
  Files changed: N
  Call sites updated: M
  Docstring refs updated: K
  [if DEPRECATE] Deprecation alias added at: <path>:<line>

Advisory — check manually (outside static analysis coverage):
  - getattr("<old_name>") dynamic dispatch: grep -rn '"<old_name>"' src/
  [if cross-repo public API and DEPRECATE not used]
  - External consumers: update CHANGELOG; use --deprecate alias until next major release
  [if caller count was capped at 50]
  - Skipped callers (51–N): edit these manually — listed in .temp/output-rename-refs-blast-* file (see blast-radius report from Step 3)
  [if skipped-callers tmpfile non-empty]
  - Skipped callers (symbol not found at rename time): <caller_qname list from tmpfile> — edit these manually
  [if residual hits from Step 6 re-scan]
  - Residual index hits (likely dynamic/string refs):
      <file>:<line>
```

</workflow>
