<!-- Consumer: plugins/cc_oss/skills/review/SKILL.md (Step 8); fragment executed inline. -->

## Step 8: Draft contributor reply (only when --reply)

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _OSS_SHARED < "${TMPDIR:-/tmp}/review-oss-shared-${CSID}" 2>/dev/null || _OSS_SHARED=""  # reload (Check 41)
cat "$_OSS_SHARED/shepherd-reply-protocol.md"  # timeout: 5000
```

`shepherd-reply-protocol.md` (loaded above) — apply invocation pattern and terminal summary format.

Spawn with:

- Report path: `REVIEW_FILE` from the flags sentinel (`"${TMPDIR:-/tmp}/oss-review-flags-${CSID}"`, read as `KEY=value` lines, never sourced) when set — the direct-report fast path and Step 0's existing-report redirect both store it there; unset → the review output file from Step 5
- PR number and contributor handle: from Step 1 `gh pr view` output
- Output path: `.temp/output-reply-<PR#>-$(date -u +%Y-%m-%d).md`

Before spawning, freeze the output path once and pass that exact literal path to shepherd:

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r PR_NUM < "${TMPDIR:-/tmp}/oss-review-pr-tag-${CSID}" 2>/dev/null || PR_NUM=""
[ -f "${TMPDIR:-/tmp}/oss-review-flags-${CSID}" ] && while IFS='=' read -r _SK _SV; do case "$_SK" in REVIEW_FILE) REVIEW_FILE=$_SV ;; esac; done < "${TMPDIR:-/tmp}/oss-review-flags-${CSID}"
# Direct-report mode stores a report path in the PR tag; derive its PR number from the report instead.
[[ "$PR_NUM" =~ ^[0-9]+$ ]] || PR_NUM=$(python "$PLUGIN_ROOT/bin/extract_review_pr.py" "$REVIEW_FILE")  # timeout: 5000
[[ "$PR_NUM" =~ ^[0-9]+$ ]] || { echo "! BLOCKED — reply PR number unavailable"; exit 1; }
mkdir -p .temp
REPLY_OUT="$(pwd)/.temp/output-reply-${PR_NUM}-$(date -u +%Y-%m-%d).md"
printf '%s\n' "$REPLY_OUT" > "${TMPDIR:-/tmp}/oss-review-reply-out-${CSID}"
```

**Part 2 compliance gate** (after shepherd returns — do NOT trust the return line alone):

```bash
# only when findings reference file:line — true LGTM has none
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r REPLY_OUT < "${TMPDIR:-/tmp}/oss-review-reply-out-${CSID}" 2>/dev/null || REPLY_OUT=""  # timeout: 5000
[ -n "$REPLY_OUT" ] && [ -f "$REPLY_OUT" ] || { echo "! BLOCKED — shepherd reply output missing at frozen spawn path"; exit 1; }
grep -qE '^\| *Importance *\| *Confidence *\| *File *\| *Line' "$REPLY_OUT" && echo "PART2_PRESENT=true" || echo "PART2_PRESENT=false"
```

`PART2_PRESENT=false` while the Step 5 report has ≥1 file:line finding → reply is non-compliant (findings folded into prose). Re-spawn `oss:shepherd` once with the same inputs plus: `"Part 2 table is MANDATORY — every file:line finding from the report must be its own row in the | Importance | Confidence | File | Line | Comment | table; do not fold file:line findings into Part 1 prose."` Re-check; if still absent, surface `⚠ Part 2 table missing — findings remain in prose` in the terminal summary.

End with `## Confidence` block per CLAUDE.md. Always last thing, regardless of `--reply`.

```bash
rm -f .temp/state/skill-contract.md  # skill complete (compaction-contract.md §Lifecycle)  # timeout: 5000
```
