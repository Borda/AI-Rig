Fix issues in `<file path>`. Listed fixes only — no other changes.

\<for each finding in this file, one bullet per fix>

- [SEVERITY] <specific fix description> Fix: \<what to change, with context to locate it>

Fix type reference:

- Broken cross-reference "foo" → replace with correct name (verify exists on disk)
- Hardcoded user path (`/Users/<name>/…`, `/home/<name>/…`) → `~/…` in prose and unquoted shell, `$HOME/…` inside double quotes
- Missing Confidence block → add `End your response with a ## Confidence block per CLAUDE.md output standards.` before closing `</workflow>` tag
- Broken bash block → fix syntax per description (add missing opening fence, fix 4-backtick closer, unescape angle brackets)
- Missing variable declaration → for sentinel data, insert `IFS= read -r VAR < "<sentinel-path>" 2>/dev/null || VAR=<default>` immediately after the block's `export CSID=…` line (add that line first if absent); otherwise declare the variable using its documented producer
- Stale cross-reference → replace `<old-name>` with `<correct-name>`
- Duplicate section → remove listed lines verbatim

No comments, docstrings, improvements beyond listed fixes.

Fix Action Hierarchy — work through in order before acting:

1. Reason: finding correct? If not, discard.
2. Relocate: correct content, wrong place? Move.
3. Consolidate: duplicates nearby? Merge.
4. Minimize: too long but valid? Compress.
5. Remove: only if none above apply.
