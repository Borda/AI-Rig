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
CICD_FILES=$(echo "$CHANGED_FILES" | grep -E '\.github/(workflows|actions)/|azure-pipelines\.yml|\.circleci/config\.yml|Jenkinsfile|\.travis\.yml|\.gitlab-ci\.yml' || true)
if [ -z "$PY_FILES" ] && [ -z "$DOC_FILES" ] && [ -z "$CICD_FILES" ]; then
    echo "No Python, documentation, or CI/CD files changed — skipping review"
    exit 0
fi
[ -z "$PY_FILES" ] && [ -z "$DOC_FILES" ] && [ -n "$CICD_FILES" ] && CICD_ONLY_MODE=true || CICD_ONLY_MODE=false
[ -z "$PY_FILES" ] && [ -z "$CICD_FILES" ] && [ -n "$DOC_FILES" ] && DOCS_ONLY_MODE=true || DOCS_ONLY_MODE=false
if [ -z "$PY_FILES" ] && [ -n "$DOC_FILES" ] && [ -n "$CICD_FILES" ]; then
    DOCS_CICD_MODE=true
else
    DOCS_CICD_MODE=false
fi
```

### Persist mode flags across bash blocks

Bash state lost between SKILL.md code blocks — Step 2 EXPECTED_FILE construction reads these back via sourcing mode-flags file.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r CLEAN_ARGS < "${TMPDIR:-/tmp}/oss-review-pr-tag-${CSID}" 2>/dev/null || CLEAN_ARGS=""
echo "$CLEAN_ARGS" > "${TMPDIR:-/tmp}/oss-review-pr-tag-${CSID}"
_REVIEW_MODE_FILE="${TMPDIR:-/tmp}/oss-review-mode-flags-${CLEAN_ARGS}-${CSID}"
{
    echo "CICD_ONLY_MODE=$CICD_ONLY_MODE"
    echo "DOCS_ONLY_MODE=$DOCS_ONLY_MODE"
    echo "DOCS_CICD_MODE=$DOCS_CICD_MODE"
} > "$_REVIEW_MODE_FILE"
```

### Reload pattern (Step 2 and later blocks)

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _PR_TAG < "${TMPDIR:-/tmp}/oss-review-pr-tag-${CSID}" 2>/dev/null || _PR_TAG="unknown"
_REVIEW_MODE_FILE="${TMPDIR:-/tmp}/oss-review-mode-flags-${_PR_TAG}-${CSID}"
[ -f "$_REVIEW_MODE_FILE" ] && . "$_REVIEW_MODE_FILE"
```
