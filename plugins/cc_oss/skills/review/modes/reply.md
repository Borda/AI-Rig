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

**Part 2 compliance gate** (after shepherd returns — do NOT trust the return line alone):

```bash
# only when findings reference file:line — true LGTM has none
REPLY_OUT=".temp/output-reply-<PR#>-$(date -u +%Y-%m-%d).md"  # timeout: 5000
grep -qE '^\| *Importance *\| *Confidence *\| *File *\| *Line' "$REPLY_OUT" && echo "PART2_PRESENT=true" || echo "PART2_PRESENT=false"
```

`PART2_PRESENT=false` while the Step 5 report has ≥1 file:line finding → reply is non-compliant (findings folded into prose). Re-spawn `oss:shepherd` once with the same inputs plus: `"Part 2 table is MANDATORY — every file:line finding from the report must be its own row in the | Importance | Confidence | File | Line | Comment | table; do not fold file:line findings into Part 1 prose."` Re-check; if still absent, surface `⚠ Part 2 table missing — findings remain in prose` in the terminal summary.

End with `## Confidence` block per CLAUDE.md. Always last thing, regardless of `--reply`.

```bash
rm -f .temp/state/skill-contract.md  # skill complete (compaction-contract.md §Lifecycle)  # timeout: 5000
```
