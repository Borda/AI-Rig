<!-- file: scope-detection.md — consumers: review/SKILL.md (Step 1 file scope detection) -->

## File scope detection logic

Executed as its own bash block after Step 1's `gh` fetch — fresh shell, so `CHANGED_FILES` rehydrated from sentinel that block writes.

### Mode flag assignment

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# multi-line payload — `read` would take the first path only
CHANGED_FILES=$(cat "${TMPDIR:-/tmp}/oss-review-changed-files-${CSID}" 2>/dev/null)
if [ -z "$CHANGED_FILES" ]; then
    echo "! BLOCKED — changed-files sentinel empty or missing; Step 1 gh fetch did not complete. Not the same as 'no relevant files changed' — refusing to skip the review silently."
    exit 1
fi
PY_FILES=$(echo "$CHANGED_FILES" | grep '\.py$' || true)
DOC_FILES=$(echo "$CHANGED_FILES" | grep -E '\.(md|rst)$' || true)
CICD_FILES=$(echo "$CHANGED_FILES" | grep -E '\.github/(workflows|actions)/' || true)
# notebooks, manifests, other config, non-Actions .github files: Step 0 keeps them CODE — never skipped, never folded into a docs/CI-only lineup
OTHER_CODE_FILES=$(echo "$CHANGED_FILES" | grep -E '\.(ipynb|txt|toml|cfg|ini|ya?ml|lock)$|(^|/)(Makefile|Pipfile|Jenkinsfile|package[^/]*\.json)$|(^|/)\.github/' | grep -vE '\.github/(workflows|actions)/' || true)
# cicd-steward is GitHub-Actions-only: warn on other CI configs instead of spawning it on them or skipping silently
OTHER_CI_FILES=$(echo "$CHANGED_FILES" | grep -E 'azure-pipelines\.yml|\.circleci/config\.yml|Jenkinsfile|\.travis\.yml|\.gitlab-ci\.yml' || true)
[ -z "$OTHER_CI_FILES" ] || echo "⚠ non-GitHub-Actions CI files changed — outside oss:cicd-steward scope, code lineup only: $(echo "$OTHER_CI_FILES" | tr '\n' ' ')"
if [ -z "$PY_FILES" ] && [ -z "$DOC_FILES" ] && [ -z "$CICD_FILES" ] && [ -z "$OTHER_CODE_FILES" ]; then
    echo "No Python, documentation, CI/CD, notebook, manifest or config files changed — skipping review"
    exit 0
fi
[ -z "$PY_FILES$OTHER_CODE_FILES" ] && [ -z "$DOC_FILES" ] && [ -n "$CICD_FILES" ] && CICD_ONLY_MODE=true || CICD_ONLY_MODE=false
[ -z "$PY_FILES$OTHER_CODE_FILES" ] && [ -z "$CICD_FILES" ] && [ -n "$DOC_FILES" ] && DOCS_ONLY_MODE=true || DOCS_ONLY_MODE=false
if [ -z "$PY_FILES$OTHER_CODE_FILES" ] && [ -n "$DOC_FILES" ] && [ -n "$CICD_FILES" ]; then
    DOCS_CICD_MODE=true
else
    DOCS_CICD_MODE=false
fi
# persisted in this block: the flags die with the shell. Appended, not truncated — SKILL.md Step 0 already wrote PR_TYPE/DOCS_TYPING_MODE/TESTS_CI_MODE to this file
IFS= read -r CLEAN_ARGS < "${TMPDIR:-/tmp}/oss-review-pr-tag-${CSID}" 2>/dev/null || CLEAN_ARGS=""
{
    echo "CICD_ONLY_MODE=$CICD_ONLY_MODE"
    echo "DOCS_ONLY_MODE=$DOCS_ONLY_MODE"
    echo "DOCS_CICD_MODE=$DOCS_CICD_MODE"
} >> "${TMPDIR:-/tmp}/oss-review-mode-flags-${CLEAN_ARGS}-${CSID}"
```

### Reload pattern (Step 2 and later blocks)

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _PR_TAG < "${TMPDIR:-/tmp}/oss-review-pr-tag-${CSID}" 2>/dev/null || _PR_TAG="unknown"
_REVIEW_MODE_FILE="${TMPDIR:-/tmp}/oss-review-mode-flags-${_PR_TAG}-${CSID}"
[ -f "$_REVIEW_MODE_FILE" ] && . "$_REVIEW_MODE_FILE"
```
