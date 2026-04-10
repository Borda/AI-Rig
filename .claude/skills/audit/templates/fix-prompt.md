Fix the following issues in `<file path>`. Apply only the listed fixes — do not change anything else.

\<for each finding in this file, one bullet per fix>

- [SEVERITY] <specific fix description> Fix: \<exactly what to change, with enough context to locate it>

Fix type reference:

- Broken cross-reference "foo" → replace with the correct name (verify it exists on disk)
- Inventory drift → update the relevant line to match disk state exactly
- Hardcoded path → replace `$HOME/path` with `.claude/path` or `~/path`
- Missing Confidence block → add `End your response with a ## Confidence block per CLAUDE.md output standards.` before the closing `</workflow>` tag
- Broken bash block → fix syntax per the description (add missing opening fence, fix 4-backtick closer, unescape angle brackets)
- Missing variable declaration → prepend `VAR="$(command)"` as the first line of the affected bash block
- Stale cross-reference → replace `<old-name>` with `<correct-name>`
- Duplicate section → remove the listed lines verbatim

Do not add comments, docstrings, or any other improvements beyond the listed fixes.

Fix Action Hierarchy — work through this order before acting:

1. Reason: is the finding actually correct? If not, discard it — do not act on a wrong finding.
2. Relocate: if the content is correct but in the wrong place, move it.
3. Consolidate: if it duplicates something nearby, merge.
4. Minimize: if it is too long but valid, compress.
5. Remove: only if none of the above apply.
