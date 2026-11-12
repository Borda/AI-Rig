<!-- file: delegation-strategy.md — consumers: SKILL.md -->

<!-- fragment — execute inline at the matching workflow step -->

<!-- consumer: /oss:release -->

## Delegation strategy

`prepare` and `audit` delegate gather/explore/validate (pre-compute, envelope validation, ledger reconcile, Phases 5–6 parallel delegation); `notes` and `demo` delegate only through the Size guard below. Load the procedure before the first delegated step — an inline `notes`/`demo` run under the size guard never needs it:

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r RELEASE_MODE < "${TMPDIR:-/tmp}/release-mode-${CSID}" 2>/dev/null || RELEASE_MODE=""
IFS= read -r RANGE < "${TMPDIR:-/tmp}/release-range-${CSID}" 2>/dev/null || RANGE=""
IFS= read -r LAST_TAG < "${TMPDIR:-/tmp}/release-setup-${CSID}/LAST_TAG" 2>/dev/null || LAST_TAG=""
case "$RELEASE_MODE" in notes|demo) [ "$(git rev-list --count --end-of-options "${RANGE:-${LAST_TAG:-HEAD~20}..HEAD}" 2>/dev/null)" -gt 50 ] 2>/dev/null || exit 0 ;; esac  # size guard: ≤50 commits runs inline, procedure never loaded
# reload SKILL_DIR (Check 41)
IFS= read -r SKILL_DIR < "${TMPDIR:-/tmp}/release-setup-${CSID}/SKILL_DIR" 2>/dev/null || SKILL_DIR=""
python "$PLUGIN_ROOT/bin/render_plugin_root.py" "$SKILL_DIR/modes/delegation.md"  # timeout: 5000
```

<!-- loads: modes/delegation.md -->

Follow above and execute. The breaking-waiver gate and unconfirmed notification below apply in every mode: delegated runs after the ledger reconcile in `modes/delegation.md`, inline runs after Truth check (below).

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r UNCONFIRMED_BREAKING < "${TMPDIR:-/tmp}/release-unconfirmed-breaking-${CSID}" 2>/dev/null || UNCONFIRMED_BREAKING=""
```

If `$UNCONFIRMED_BREAKING` is nonzero, require review before any release artifact is written.

- Read this invocation's `$WAIVED_FILE` and show every `REMOVED:` or `NET-STATE-ADD:` entry marked ⚠️ Breaking Changes, with its old name, baseline/HEAD evidence, and proposed category.
- Delegated runs require the typed envelope count and marked ledger lines to agree exactly at the ledger reconcile in `modes/delegation.md`; inline runs derive the count from the same marked ledger lines after Truth check. A count alone is not item evidence.
- Invoke `AskUserQuestion` with one question per item, at most 4 per call (more items → successive calls of ≤4): **Approve the proposed classification** or **Abort release preparation**.
- Proceed only after explicit approval of every item; an absent/ambiguous answer aborts.
- Do not describe the automatic reclassification as already user-confirmed.
- Apply this gate in both delegated `prepare`/`audit` runs before Phase 2b; inline `notes`/`demo`/`--append` runs use the same rule immediately after Truth check.

When `unconfirmed > 0`, surface removed/reclassified items as notification in the run's final reply after every required breaking-waiver approval has completed — the Bash output below is not reply text the user reads, and text written before a tool call can arrive as an empty progress update. Read the waived-changes ledger, not `$GATHER_FILE`:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r WAIVED_FILE < "${TMPDIR:-/tmp}/release-waived-${CSID}" 2>/dev/null || WAIVED_FILE=""
IFS= read -r UNCONFIRMED < "${TMPDIR:-/tmp}/release-unconfirmed-${CSID}" 2>/dev/null || UNCONFIRMED=0
if [ "${UNCONFIRMED:-0}" -gt 0 ] 2>/dev/null && [ -n "$WAIVED_FILE" ] && [ -f "$WAIVED_FILE" ]; then
    WAIVED_ITEMS=$(grep -E '^(REMOVED|NET-STATE-ADD):' "$WAIVED_FILE" | head -20)  # timeout: 3000
    echo "Truth check removed/reclassified ${UNCONFIRMED} unverified claim(s) (category-specific HEAD/baseline check):"
    echo "$WAIVED_ITEMS"
fi
```

Pass `$GATHER_FILE` path to artifact phase — do NOT read gather file into main context. The ledger is a separate, smaller file — grepping it is not a gather-file read.

`notes` and `demo` modes: skip delegation — single-pass; run gather/explore/validate inline. **Size guard**: estimate commit count with `git rev-list --count --end-of-options ${RANGE:-${LAST_TAG:-HEAD~20}..HEAD} 2>/dev/null`. If >50, delegate to `foundry:sw-engineer` subagent same as prepare mode — inline gather with >50 commits causes context flood. Run the block below on every `notes`/`demo` run, inline or delegated, before Gather changes: it defines `GATHER_FILE` (which the envelope-validation block in `modes/delegation.md` resolves) and creates this invocation's `WAIVED_FILE` ledger, which inline Truth check reloads (`modes/classify-truth-check.md` "Waived changes ledger"):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# reload BRANCH/DATE (Check 41)
IFS= read -r BRANCH < "${TMPDIR:-/tmp}/release-setup-${CSID}/BRANCH" 2>/dev/null || BRANCH=""
IFS= read -r DATE < "${TMPDIR:-/tmp}/release-setup-${CSID}/DATE" 2>/dev/null || DATE=""
GATHER_FILE=".temp/release-gather-$BRANCH-$DATE.md"
mkdir -p .temp  # timeout: 5000
WAIVED_FILE=$(mktemp ".temp/release-waived-$BRANCH-$DATE-XXXXXXXX") || exit 1  # one atomic, distinct ledger per invocation; preserve earlier runs
echo "$WAIVED_FILE" > "${TMPDIR:-/tmp}/release-waived-${CSID}"  # persist for Gather changes' cross-cycle detection + Truth check (Check 41)
```
