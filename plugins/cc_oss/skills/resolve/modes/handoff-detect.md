<!-- oss:resolve Step 1 Review-handoff auto-detect — executed inline: cat $_OSS_RESOLVE/modes/handoff-detect.md; execute -->

<!-- fragment — no <workflow> wrapper; executed inline by SKILL.md orchestrator -->

<!-- consumer: plugins/cc_oss/skills/resolve/SKILL.md (Step 1 Review-handoff auto-detect) -->

### Review-handoff auto-detect (when $ARGUMENTS is empty)

When `$ARGUMENTS` empty:

```bash
# oss lineage → .reports/review/pr-<N>/run-<NNN>/ (current) or .reports/review/<timestamp>/ (pre-rename, still readable); codex lineage → .reports/codex/review/
unsetopt nomatch 2>/dev/null  # zsh: unmatched glob stays literal as in bash, ls drops it; NOMATCH would abort the whole lookup
REVIEW_FILE=$(ls -t .reports/review/*/review-report.md .reports/review/*/*/review-report.md .reports/codex/review/*/review-notes.md 2>/dev/null | head -1)
if [ -z "$REVIEW_FILE" ]; then
    echo "No review output found in .reports/review/ or .reports/codex/review/ — run /review <PR#> first, or provide a PR number"
    exit 1
fi
case "$REVIEW_FILE" in
    .reports/codex/review/*)
        echo "! BLOCKED — newest review is codex-lineage ($REVIEW_FILE); this parser reads oss:review's .reports/review/pr-*/run-*/review-report.md section schema only, not codex's flat H1/H2/M1-bullet schema. Falling through would silently miss any blocking findings that review recorded. Provide a PR number explicitly (\`/oss:resolve <PR#>\`), or run /oss:review on this PR to produce a compatible report."
        exit 1
        ;;
esac
echo "→ Using: $REVIEW_FILE"
```

Read `$REVIEW_FILE`. Extract PR number — path first, header second, never the first digit run anywhere (a Title like "v2 parser" would yield 2):

- Path: `.reports/review/pr-<N>/run-<NNN>/review-report.md` → `<N>`
- Header: the YAML `PR: #<N>` line, else a `## Code Review: PR #<N>` heading
- Helper: `python "${CLAUDE_PLUGIN_ROOT}/bin/extract_review_pr.py" "$REVIEW_FILE"` prints `<N>` by that order, exit 1 and no output when none

PR found → set `$ARGUMENTS = <N>`, proceed PR mode. Print: `→ Resolved PR #<N> from review output.`

No PR number extractable → print: "Review output does not reference a PR — provide a PR number explicitly: `/oss:resolve <PR#>`" and exit 1.
