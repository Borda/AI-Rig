<!-- file: gather-prompt.md — consumers: oss/skills/release/SKILL.md (Delegation strategy) -->

Working directory: \<REPO_ROOT>. Run all git commands from there (git -C \<REPO_ROOT> <cmd>, or cd \<REPO_ROOT> first). For git range <RANGE>, against last published tag \<LAST_TAG> (may be empty — first-ever release, no prior tag):

Run gather phase: git log, git diff --stat, gh pr list.

Retain verified commit-to-PR associations and each in-range PR author's known login/display name in \<GATHER_FILE> for contributor extraction. Establish range membership from release commits and verified PR linkage — not merge dates or full merged-PR list. Include authors absent from Git authors/coauthors (e.g. maintainer-authored squash merges without original-author trailers). Keep this identity inventory even when classification groups changes or drops net-zero claims. Missing linkage, deleted identities, or incomplete PR metadata must remain explicit contributor-coverage gaps; never label Git-only coverage complete or invent identities.

For every SHA from `git -C <REPO_ROOT> rev-list "<RANGE>"`, run `gh api --method GET --paginate --slurp "repos/{owner}/{repo}/commits/<sha>/pulls" -f per_page=100` from \<REPO_ROOT>. Retain all pages and each SHA's merged-PR associations (PR number, author, base, merge commit); never restrict lookup to default branch. Verify actual repo/commit/diff membership before crediting. Successful empty result retains Git authors/coauthors without inventing a PR; failed, deleted, or ambiguous metadata stays a coverage gap in \<GATHER_FILE>. Do not substitute default-branch merged-PR list after lookup failure.

Run classify phase: classify NET state at HEAD, not each intermediate commit. When multiple commits in range touch same API/feature (add then modify, add then remove, add then rewrite), describe only what exists in HEAD — don't include features added and later undone within same range, regardless of whether removal was explicit revert or follow-up PR. When entry survives (net-effect non-zero), collect ALL PR numbers contributing to final state under SAME category — never attribute to only initial or last PR. Group under one bullet with cumulated PR refs ONLY when all contributing PRs classify into same section (both Added, both Changed, both Fixed); when a PR fixes a bug or changes behavior in a feature added by an earlier PR in same range, that fix gets its own 🔧 Fixed or 🌱 Changed entry — never folded into Added. Exception: trivial fixes (one-line cleanup, doc tweak inside new code, no standalone user-visible effect) fold into parent Added bullet.

Run explore phase: top 3–5 most significant changed files (read actual diffs).

Run truth check phase for each 🚀 Added, ⚠️ Breaking Changes, 🌱 Changed, or ❌ Removed item naming a specific symbol (function, class, method, config key, CLI flag, `pyproject.toml`/`setup.cfg` extra). Check the final name at HEAD for Added/Changed; check the old name at `<LAST_TAG>` and HEAD for Breaking/Removed/rename claims. Apply the **same claim-specific declaration predicate at both refs**. For Python, identify the actual public module file named by the claim and set `PUBLIC_PATH` to its repository-relative `.py` path; if the file or class-method coordinate is unknown, keep the claim inconclusive. Run this probe from `<REPO_ROOT>` with `REF=<LAST_TAG>`, `SYMBOL=<old_name>`, and the old `PUBLIC_PATH`, then with `REF=HEAD` and the category-appropriate name and path. Exit 0 = module-level binding candidate in that exact file; 2 = inconclusive. Confirm the candidate's actual public export/entrypoint at both refs (for example package `__init__.py` or `__all__`) before calling it truth-checked. The probe never proves absence: conditional definitions, decorators, computed names, class methods, and dynamic bindings fall outside its finite syntax coverage. A docstring, comment, or unrelated private method cannot establish baseline presence. Establish absence through explicit source review of the relevant public surface; if uncertain, keep the claim qualified:

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

For TypeScript/JavaScript/Go/Rust, inspect an actual declaration at both refs with `git show <ref>:<path>`; raw `git grep -w` matches are only candidates. For manifest/config claims, use `git grep -nF '<name>' <ref> -- 'pyproject.toml' 'setup.cfg' '*.toml'` to locate candidates, then confirm the exact key/extra declaration in its section at both refs. Never count comments or values as declarations. A command/parse failure means inconclusive, not absent. Added/Changed final name absent at HEAD → remove the claim, then immediately append via Bash (never batch for later): `echo 'REMOVED: <item> — symbol not found in HEAD' >> "<WAIVED_FILE>"`. Repeat for newly revealed dependencies.

**Category-specific baseline check**: for ❌ Removed, old name **present at `$LAST_TAG` and absent at `HEAD`** → **keep the ❌ Removed claim** (subject to prior-deprecation classification); HEAD absence is required evidence, not grounds for waiver. Old name still present at HEAD → remove the removal claim, append `REMOVED: <item> — old name still present in HEAD` to `<WAIVED_FILE>`, and correct its category only when the diff proves another user-visible change. Old name absent at `$LAST_TAG` → omit the removal claim and append `REMOVED: <item> — old name absent at <LAST_TAG>, never shipped` to `<WAIVED_FILE>`; do not invent Added without a final replacement at HEAD. For ⚠️ Breaking Changes/renames, old name absent at `$LAST_TAG` and final name present at HEAD → reclassify as 🚀 Added under the final name and append `NET-STATE-ADD: ⚠️ Breaking Changes: <item> — <old_name> absent at <LAST_TAG>, reclassified as Added` to `<WAIVED_FILE>`. Count a reclassified Breaking claim into `unconfirmed_breaking`, so the downstream `AskUserQuestion` gate sees it. If no final name exists, omit the unsupported claim and append `REMOVED: ⚠️ Breaking Changes: <item> — no final name exists in HEAD` to `<WAIVED_FILE>`. Empty `<LAST_TAG>` (first-ever release) or unresolved baseline → keep the claim, qualify "(not baseline-verified)"; never auto-downgrade on an unknown baseline. Track excluded/reclassified claim count (`unconfirmed`) and how many began as ⚠️ Breaking Changes (`unconfirmed_breaking`). Append each ledger line immediately after its decision.

Write full findings (commit list, classified change table, diff excerpts) to \<GATHER_FILE> via Write tool. Keep qualified ⚠️ Breaking Changes and ❌ Removed claims in that table when the baseline is unresolved, visibly marked "(not baseline-verified)"; preserve their PR/commit evidence for prepare and audit. Only excluded or reclassified REMOVED/NET-STATE-ADD entries live in `<WAIVED_FILE>`; do not duplicate them into `<GATHER_FILE>`.

Return ONLY: {"status":"done","file":"\<GATHER_FILE>","changes":N,"breaking":N,"unconfirmed":N,"unconfirmed_breaking":N,"confidence":0.N}
