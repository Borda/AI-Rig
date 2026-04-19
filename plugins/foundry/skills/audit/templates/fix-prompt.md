**Re: Compress prompt template into caveman format**

Fix issues in `<file path>`. Listed fixes only — no other changes.

\<for each finding in this file, one bullet per fix>

- [SEVERITY] <specific fix description> Fix: \<what to change, with context to locate it>

Fix type reference:

- Broken cross-reference "foo" → replace with correct name (verify exists on disk)
- Inventory drift → update line to match disk state exactly
- Hardcoded path → replace `$HOME/path` with `.claude/path` or `~/path`
- Missing Confidence block → add `End your response with a ## Confidence block per CLAUDE.md output standards.` before closing `</workflow>` tag
- Broken bash block → fix syntax per description (add missing opening fence, fix 4-backtick closer, unescape angle brackets)
- Missing variable declaration → prepend `VAR="$(command)"` as first line of affected bash block
- Stale cross-reference → replace `<old-name>` with `<correct-name>`
- Duplicate section → remove listed lines verbatim

No comments, docstrings, or improvements beyond listed fixes.

Fix Action Hierarchy — work through order before acting:

1. Reason: finding correct? If not, discard — don't act on wrong finding.
2. Relocate: content correct but wrong place? Move it.
3. Consolidate: duplicates something nearby? Merge.
4. Minimize: too long but valid? Compress.
5. Remove: only if none above apply.
