<!-- file: classify-truth-check.md — consumers: release/SKILL.md (Classify each change + Truth check + Breaking-change classification phases; loaded once, all three phases read from that single load) -->

## Classify each change

**Net-state principle**: classify only HEAD state, not development journey. Feature added then removed within range = net effect zero — omit. Same for added-then-renamed: a symbol/config-key/extra introduced and renamed within range never shipped under the old name — classify as 🚀 Added under the final name, never ⚠️ Breaking Changes for the rename. A squash-merge commit body narrating internal branch history (e.g. "rename X to Y") is not evidence the rename crossed a release boundary — check the baseline tag (Truth check, below) before trusting it.

**Cross-cycle extension** (`--append` only): the Net-state principle above applies within `$RANGE`; Gather changes' "Cross-cycle revert/pivot detection" extends it *across* append cycles — a revert or symbol pivot that supersedes a bullet a PRIOR cycle already wrote into `DRAFT.md`/`$CHANGELOG_FILE` nets to a removal of that stale entry, not an additive one. See Gather changes for the detection rule; classify each in-scope item against `CROSS_CYCLE_MATCH` before finalizing this table.

**PR accumulation**: list ALL contributing PR numbers for net-surviving entry. **Same category only** — two PRs merge under one bullet only when both classify into SAME section. Later PR fixing bug in same-range feature = own 🔧 Fixed entry. **Trivial-fix exception**: fix or doc tweak with no standalone user-visible effect folds into parent Added bullet. When in doubt: separate entries safer

**Commit-label distrust**: `fix:`/`feat:`/etc. type prefix and subject line are self-reported by the author, not verified — a commit titled `fix: progress bar` can in fact reintroduce a feature, and a mislabeled subject slips through unnoticed more often than a mislabeled body trailer. Classify from the actual diff (files touched, symbols added/changed, net behavior at HEAD), never from the type prefix or subject text alone — treat both as a hint to check, not a verdict.

**SHA tracking (for provenance)**: alongside PR numbers, also retain the full 40-char commit sha(s) contributing to each net-surviving entry — already visible in context from Gather changes' `git log $RANGE --no-merges --format="--- %H%n%B"` output. Needed downstream by the Provenance record step (`release-draft-template.md` "Post-write bookkeeping"), which derives each sha's content-stable `git patch-id --stable` for the actual store key — a bullet folding N squashed commits keeps all N shas, each mapped to that one bullet's final text once written.

Section order (fixed): 🚀 Added → ⚠️ Breaking Changes → 🌱 Changed → 🗑️ Deprecated → ❌ Removed → 🔧 Fixed → 🔒 Security → 🔄 Reverted

| Category | Section | What goes here |
| -- | -- | -- |
| New Features | 🚀 Added | User-visible additions |
| Breaking Changes | ⚠️ Breaking Changes | Existing code stops working immediately — no prior deprecation period. Prior release deprecated → ❌ Removed instead. |
| Improvements | 🚀 Added or 🌱 Changed | Enhancements to existing behavior |
| Performance | 🚀 Added / 🔧 Fixed / 🌱 Changed | Quantitative claims require benchmark evidence; else rewrite to "improved performance" without number. |
| Deprecations | 🗑️ Deprecated | Old API still works; scheduled removal; replacement exists |
| Removals | ❌ Removed | Previously deprecated — users had warning. Not ⚠️ Breaking Changes. |
| Bug Fixes | 🔧 Fixed | Correctness fixes |
| Security | 🔒 Security | Security fixes + CVE dep updates. Security-intent keywords in body always classify here regardless of commit type. OMIT-INTERNAL does NOT apply. |
| Internal | *(omit)* | Refactors, CI/tooling, deps, housekeeping — omit unless user-impacting |
| Reverted | 🔄 Reverted | Introduced AND reverted within range (REVERT_SET) — net effect zero |

**Same-release feature+fix dedup**: 🔧 Fixed targeting code introduced same release = never shipped = fold into 🚀 Added or omit. **Exception**: fix's commit body itself states a caller-visible behavior change (e.g. "changes behavior for any caller currently relying on...") → still surface a ⚠️ Breaking Changes entry, or a labeled breaking-behavior callout within the Added bullet — never silently absorbed into plain feature prose.

**Breaking vs Deprecated vs Removed**: old call still works → Deprecated. Deprecated in prior release, now removed → Removed. **Prior-deprecation body-signal**: commit body contains "deprecated in vX", "previously deprecated", "was deprecated", "emits DeprecationWarning since", or "deprecated since" → treat as Removed regardless of `feat!:`/`BREAKING CHANGE:` markers. **Bug fixed to match spec**: classify as 🌱 Changed when users relied on buggy behavior; ⚠️ Breaking Changes only if load-bearing, causes widespread breakage.

**OMIT-INTERNAL body-signal override**: commit body contains "No code changes", "no user-facing changes", "internal only", "no public API changes", "internal buffer changes only", "internal restructure" OR all paths under `.github/`, `ci/`, `scripts/`, `Makefile`, `*.yml` under `.github/` → classify as Internal. **Exception**: BREAKING CHANGE footer or confirmed user-visible breakage overrides.

**Cherry-pick annotation (stable-branch mode)**: when `$CHERRY_PICK_SUBJECTS` set, match subject against it. Match → append "(backported from $SOURCE_TAG_REF)". Subject-text matching is heuristic — verify manually for generic subjects.

**Self-correction discipline**: present only final corrected table — no intermediate classifications.

## Truth check

Gate — runs after Classify, before Audit changelog.

**Scope**: 🚀 Added, ⚠️ Breaking Changes, 🌱 Changed, ❌ Removed naming a symbol. Skip: 🔧 Fixed, 🔒 Security, 🗑️ Deprecated, 🔄 Reverted. A ❌ Removed claim needs the opposite HEAD result from Added/Changed: the old name must be gone.

**File set**: not source extensions alone — a public surface can be a `pyproject.toml`/`setup.cfg` extra or config key, invisible to symbol-only tooling (codemap's `fn-rdeps` included). Check code definitions in source (`*.py *.ts *.js *.go *.rs`) and exact key/extra declarations in manifest/config files (`pyproject.toml setup.cfg *.toml`) separately. A mention in a docstring, comment, or reference is not a definition.

For each in-scope change, check the name appropriate to its category — final name at HEAD for 🚀 Added/🌱 Changed, old name at the last published tag for ⚠️ Breaking Changes/❌ Removed/renames. Use the **same claim-specific declaration predicate at both refs**: for Python, identify the actual public module file named by the claim and set `PUBLIC_PATH` to its repository-relative `.py` path. Unknown path or class-method coordinate → inconclusive and manual inspection; never scan unrelated files for a bare name. The AST probe below proves only a module-level binding candidate in that exact file, including direct definitions, assignments, imports, and literal module-level `add_argument` flags. Exit 0 = candidate binding present; 2 = inconclusive, including every unmatched, unsupported, or parse/command failure. A candidate is not public-export proof: confirm the claimed export/entrypoint at `$LAST_TAG` and HEAD (for example package `__init__.py` or `__all__`) before marking the claim truth-checked. Set `REF` and `SYMBOL` for the old name at `$LAST_TAG`, then the category-appropriate name at `HEAD`; use its corresponding `PUBLIC_PATH` at each ref. Codemap may help locate a current candidate but cannot verify a past ref from a HEAD index. Establish absence through explicit source review of the relevant public surface; if it remains uncertain, keep the claim qualified.

```bash
python - "$REF" "$SYMBOL" "$PUBLIC_PATH" <<'PY'
import ast
import subprocess
import sys

ref, symbol, path = sys.argv[1:]
if not path.endswith(".py") or path.startswith("/") or ".." in path.split("/"):
    sys.exit(2)
source = subprocess.run(["git", "show", f"{ref}:{path}"], capture_output=True)
if source.returncode:
    print(source.stderr.decode("utf-8", errors="replace"), file=sys.stderr)
    sys.exit(2)
try:
    tree = ast.parse(source.stdout)
except (SyntaxError, UnicodeError) as error:
    print(f"Cannot parse {ref}:{path}: {error}", file=sys.stderr)
    sys.exit(2)
declarations = tree.body
named = any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == symbol for node in declarations)
assigned = any(
    isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == symbol for target in node.targets)
    or isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == symbol
    or isinstance(node, ast.Import) and any((alias.asname or alias.name.split(".", 1)[0]) == symbol for alias in node.names)
    or isinstance(node, ast.ImportFrom) and any((alias.asname or alias.name) == symbol for alias in node.names)
    for node in declarations
)
flag = any(
    isinstance(node, ast.Expr)
    and isinstance(node.value, ast.Call)
    and isinstance(node.value.func, ast.Attribute)
    and node.value.func.attr == "add_argument"
    and any(isinstance(arg, ast.Constant) and arg.value == symbol for arg in node.value.args)
    for node in declarations
)
if named or assigned or flag:
    sys.exit(0)
sys.exit(2)
PY
```

Reload `$LAST_TAG` from `${TMPDIR:-/tmp}/release-setup-${CSID}/LAST_TAG` before the baseline probe (Check 41); `$LAST_TAG` is the published boundary, never `$RANGE`'s start in `--append` mode. If unresolved, keep the claim qualified, not baseline-verified. For TypeScript/JavaScript/Go/Rust, inspect a declaration at each ref in `git show <ref>:<path>`; do not count a raw `git grep -w` hit as definition evidence. For a manifest/config claim, use `git grep -nF '<name>' <ref> -- 'pyproject.toml' 'setup.cfg' '*.toml'` to locate candidates at both refs, then confirm the exact key/extra declaration and relevant section; a mention in a comment or value does not count. A command or parse failure is inconclusive, not absence.

**Category-specific outcomes**: 🚀 Added/🌱 Changed final name present at HEAD → keep (note "truth-checked"); absent → remove from the classified table and append (reload `$WAIVED_FILE` per "Waived changes ledger" below, exact prefix matching `templates/gather-prompt.md`'s delegated-path phrasing):

```bash
echo "REMOVED: <item> — symbol not found in HEAD" >> "$WAIVED_FILE"
```

Cannot determine current state → keep with "(not HEAD-verified)" qualifier.

For ❌ Removed, old name **present at `$LAST_TAG` and absent at `HEAD`** → **keep the ❌ Removed claim** (subject to prior-deprecation classification); HEAD absence is required evidence, never a reason to waive it. Old name still present at HEAD → remove the removal claim, append `REMOVED: <item> — old name still present in HEAD` to `$WAIVED_FILE`; correct its category only when the diff proves another user-visible change. If the old name is absent at `$LAST_TAG`, it did not ship under that name: omit the removal claim and append `REMOVED: <item> — old name absent at <LAST_TAG>, never shipped` to `$WAIVED_FILE`. Do not invent an Added entry unless a final replacement exists at HEAD. An unresolved baseline keeps the qualified claim; it does not prove the removal.

For ⚠️ Breaking Changes / rename claims specifically: `LAST_TAG` unresolved → keep, qualify "(not baseline-verified)", never auto-downgrade. Old name absent at `$LAST_TAG` and a final name exists at HEAD → reclassify as 🚀 Added (final name only), append:

```bash
echo "NET-STATE-ADD: ⚠️ Breaking Changes: <item> — <old_name> absent at <LAST_TAG>, reclassified as Added" >> "$WAIVED_FILE"
```

— never keep as Breaking without review. This started life as a ⚠️ Breaking Changes claim: in delegated (`prepare`/`audit`) mode it must still count into the returned envelope's `unconfirmed_breaking` and pass the Delegation strategy's item-evidence `AskUserQuestion` gate. Inline modes apply that same gate after Truth check. If no final name exists, omit the unsupported claim and record `REMOVED: ⚠️ Breaking Changes: <item> — no final name exists in HEAD` instead.

Gate loop (max 3 iterations): truth-check → remove unverified → re-run on updated set → after 3 iterations surface remaining unverified claims and proceed.

Runs before Identify highlights — highlights and demo must never reference unverified items.

## Waived changes ledger

Every exclusion made against a baseline/HEAD check — `REMOVED:`, `NET-STATE-ADD:` here; `CROSS_CYCLE_MATCH:` (Gather changes) and `POST_MERGE_REMOVE:` (`release-draft-template.md`) elsewhere in this skill — is a claim that a real commit made it into range but did not survive into the release the reader sees. Dropping it silently loses that audit trail; it goes to a dedicated ledger instead, never `$GATHER_FILE` (subagent-owned, retried independently — an orchestrator-side append would mix writers and vanish on a gather retry).

`$WAIVED_FILE` (`.temp/release-waived-$BRANCH-$DATE-<unique suffix>`) is created atomically once per invocation, alongside `$GATHER_FILE`, before Gather changes runs (`SKILL.md`'s Delegation strategy step 1 for `prepare`/`audit`; the `notes`/`demo` inline setup block otherwise). The sentinel identifies this invocation's ledger across fresh shells; later same-day runs get another file and preserve the earlier evidence. Every phase that appends a waived-changes line reloads it from that sentinel rather than redefining it:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r WAIVED_FILE < "${TMPDIR:-/tmp}/release-waived-${CSID}" 2>/dev/null || WAIVED_FILE=""
```

`notes`/`demo`/plain `--append` modes: this file **is** the deliverable — no further copy. `prepare` mode: consolidated into `releases/$VERSION/waived-changes.md` at the end of the pipeline (see `modes/prepare.md` Phase 6).

## Breaking-change classification

Gate — runs after Truth check, before Audit changelog. Labels each diff-derived public symbol **Breaking** (external caller) or **internal** (same-package caller only), and drafts migration evidence lines.

**Codemap-gated** — `fn-rdeps` needs a v3 index. No index → skip; keep the human Classify labels as-is.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# Reload RANGE (Check 41: fresh shell)
IFS= read -r RANGE < "${TMPDIR:-/tmp}/release-range-${CSID}" 2>/dev/null || RANGE=""
CODEMAP_OK=$(codemap-py query list 2>/dev/null | wc -l)  # timeout: 5000
# 0 = no index → skip this phase entirely (human Classify labels stand)
```

When `CODEMAP_OK` non-zero:

1. Extract changed public symbols (diff-derived, `__init__.py` surface):
   ```bash
   CHANGED_SYMBOLS=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/extract_changed_symbols.py" "$RANGE")  # timeout: 15000
   [ -z "$CHANGED_SYMBOLS" ] && echo "No changed public symbols — skipping Breaking classification"
   ```
2. Resolve each bare name to a `module::symbol` qname (skip test modules) and build one `fn-rdeps --exclude-tests` batch query per resolved qname. Removed public name (no `find-symbol` match) → still add its `<pkg>::<name>` qname so `fn-rdeps` errors and the helper labels it Breaking-removed. Write the query array to `$QUERIES_FILE`:
   ```json
   [{"cmd": "fn-rdeps", "args": ["<module>::<symbol>", "--exclude-tests"]}]
   ```
3. Classify in one batched pass (one process, one coverage block) — pipe batch output through the classifier:
   ```bash
   export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
   IFS= read -r BRANCH < "${TMPDIR:-/tmp}/release-setup-${CSID}/BRANCH" 2>/dev/null || BRANCH=""
   IFS= read -r DATE < "${TMPDIR:-/tmp}/release-setup-${CSID}/DATE" 2>/dev/null || DATE=""
   BREAKING_FILE=".temp/release-breaking-$BRANCH-$DATE.json"
   mkdir -p .temp  # timeout: 5000
   codemap-py query batch "$QUERIES_FILE" 2>/dev/null \
     | python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/classify_breaking.py" > "$BREAKING_FILE"  # timeout: 15000
   echo "${BREAKING_FILE:-}" > "${TMPDIR:-/tmp}/release-breaking-file-${CSID}"
   ```

`classify_breaking.py` output: `{breaking:[{symbol,package,external_callers|reason}], internal:[...], query_complete, migration_lines}`. "External caller" = caller whose top-level package differs from the symbol's own package.

**Apply**:

- Every `breaking` symbol not already under ⚠️ Breaking Changes → treat as a proposed promotion, citing its external callers as evidence. Do not move it or draft migration output until the gate below accepts it.
- `migration_lines` = the affected call-site draft — carry into **Draft migration guide** (`breaking_callers` findings) only for an approved Breaking classification; each external call site gets a before→after entry.
- `internal` symbols → leave under their human Classify label (🚀 Added / 🌱 Changed); a same-package-only caller is not a downstream break.
- `query_complete:false` → label the evidence "possibly-incomplete (codemap coverage partial)" rather than dropping it; do not silently trust it as exhaustive.

**Post-promotion baseline and item-approval gate** — runs after codemap classification, before Audit changelog and every release artifact. For each proposed Added/Changed → ⚠️ Breaking Changes promotion, re-run the category-specific Truth check on the old public behavior/name at `$LAST_TAG` and the relevant HEAD surface; earlier Added/Changed truth evidence and the delegated gather envelope do not verify a later Breaking label. Show the exact item, baseline and HEAD evidence or uncertainty, external caller evidence, and proposed final category through `AskUserQuestion`. If the old behavior did not ship at `$LAST_TAG`, retain Added/Changed and omit its Breaking migration line; if baseline or HEAD evidence is inconclusive, do not claim a verified Breaking classification. Proceed only after explicit approval of the final category for **each** proposed promotion; ambiguous or absent response aborts before artifact writes. Record the decision and evidence in the gather findings used by downstream phases so highlights, changelog, and migration use the same accepted category. This gate also applies if a delegated classifier proposes a promotion during `prepare`/`audit`; complete it before Phase 2b writes the changelog.

This phase can block on a promoted claim with missing evidence or approval; internal-only results remain under their original category.
