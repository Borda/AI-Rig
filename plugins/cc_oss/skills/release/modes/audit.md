<!-- oss:release Mode: audit — executed via: cat "$SKILL_DIR/modes/audit.md"; execute -->

<!-- Variables available: $SKILL_DIR, $_OSS_SHARED, $LAST_TAG, $BRANCH, $DATE, $RANGE, $VERSION, $REPO_ROOT, $SOURCE_TAG_REF, $CHERRY_PICK_SUBJECTS -->

**Trigger**: `/release audit [version]`

**Purpose**: Pre-release readiness check — surfaces outstanding work, alignment gaps, blockers before cutting release

```bash
# LAST_TAG, REPO_ROOT, SKILL_DIR from Shared setup above
# audit mode: REST = optional version token (not range); RANGE defaults to LAST_TAG..HEAD
RANGE="${RANGE:-$LAST_TAG..HEAD}"
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r SOURCE_TAG_REF < "${TMPDIR:-/tmp}/release-setup-${CSID}/SOURCE_TAG_REF" 2>/dev/null || SOURCE_TAG_REF=""
IFS= read -r CHERRY_PICK_SUBJECTS < "${TMPDIR:-/tmp}/release-setup-${CSID}/CHERRY_PICK_SUBJECTS" 2>/dev/null || CHERRY_PICK_SUBJECTS=""
RELEASE_MODE="linear"; PENDING_CHERRY_PICKS=0
[ -n "$SOURCE_TAG_REF" ] && RELEASE_MODE="stable-branch" && PENDING_CHERRY_PICKS=$(printf '%s\n' "$CHERRY_PICK_SUBJECTS" | grep -c . 2>/dev/null || echo 0)
echo "release mode: $RELEASE_MODE (source=$SOURCE_TAG_REF, pending=$PENDING_CHERRY_PICKS)"
```

### Phase 0: Release-model guardrail

`$RELEASE_MODE=stable-branch` (i.e. `$SOURCE_TAG_REF` non-empty) → HEAD does not descend from develop/main's tip; commits merged there after `$LAST_TAG` are **expected** to be absent from this branch unless their subject matches `$CHERRY_PICK_SUBJECTS`. Never diff this branch against `develop`/`main`/any non-ancestor branch and report the gap as blocking "drift" — that comparison assumes a linear model this branch isn't using. A PR or commit missing from HEAD in this mode is normal, not evidence of an accidental cut, and must never by itself produce a `critical`/`BLOCKED` finding.

The only legitimate cherry-pick-mode red flag is a **Truth check** hit (`modes/classify-truth-check.md`, run inside the Phase 1 gather subagent): a symbol referenced by an in-range commit (docstring, import, call site) but not defined anywhere in HEAD. That is a real dangling-reference bug — scope the finding to that specific commit/PR only. It is evidence that one commit is incomplete, never evidence that the whole selective cut was accidental — don't generalize a single dangling reference into a branch-wide divergence verdict.

`$RELEASE_MODE=linear` → this branch is a direct descendant of the branch it was cut from; standard fast-forward/ancestor expectations apply, and unexpected missing commits are worth investigating as `NEEDS_ATTENTION` (not automatically `BLOCKED` — confirm with the user before treating a gap as unintentional; branches get selectively rebased or filtered for reasons a diff alone can't show).

### Phase 1: Gather and explore changes

Use **Delegation strategy** above — spawn gather subagent for `$RANGE`, run gather/explore/validate phases, write findings to `GATHER_FILE`. Read returned JSON envelope only. Audit agent (Phase 2) reads `GATHER_FILE` directly — don't pull into main context.

### Phase 1a: Deprecation-removal check

Verify all APIs scheduled for removal at `$TARGET` absent from HEAD. Runs after gather, before readiness checks.

**Step 1** — find all scheduled removals from two sources (run in parallel):

```bash
# Source A: pyDeprecate remove_in= markers (includes deprecated_class, deprecated_instance)
git -C "$REPO_ROOT" grep -n 'remove_in=' HEAD -- '*.py' 2>/dev/null | grep -v '^\s*#'  # timeout: 5000

# Source B: CHANGELOG 🗑️ Deprecated entries — prior releases may name removal version in prose
CHANGELOG_FILE=$(find "$REPO_ROOT" -maxdepth 2 -name "CHANGELOG.md" 2>/dev/null | head -1)
[ -f "$CHANGELOG_FILE" ] && grep -n '🗑️\|Deprecated\|remove_in\|scheduled for removal\|will be removed' "$CHANGELOG_FILE" 2>/dev/null | head -60  # timeout: 3000
```

**Step 2** — for each `remove_in="V.W"` found, compare with `$TARGET`:

- Skip version comparison when `$TARGET` empty or `"next"` — report informational only
- OVERDUE = `remove_in ≤ $TARGET`:

```bash
# stdlib version parse — no packaging dep
python3 -c "
import sys, re
def parse_ver(v):
    v = v.strip('\"v ')
    parts = [int(x) for x in re.sub(r'[^0-9.]', '', v).split('.') if x]
    return tuple(parts) if parts else (0,)
rv, tv = parse_ver(sys.argv[1]), parse_ver(sys.argv[2])
print('OVERDUE' if rv <= tv else 'FUTURE')
" "<remove_in_value>" "${TARGET:-0}"  # timeout: 5000
```

**Step 3** — for each OVERDUE item, check if old symbol still present in HEAD. Read 3-line context around `remove_in=` match to extract deprecated function or class name (typically decorated name 1–2 lines above), then:

```bash
# definition-level only — comments/docstrings may reference removed names
git -C "$REPO_ROOT" grep -n "^def <symbol>\|^class <symbol>\|    def <symbol>\|    class <symbol>" HEAD -- '*.py' 2>/dev/null  # timeout: 3000
```

**Outcomes per symbol**:

- Absent from HEAD → ✓ correctly removed
- Still present AND OVERDUE → ✗ **CRITICAL** — add to Phase 2 findings table as: `` | Scheduled removal overdue | ✗ `<symbol>` still present (remove_in="V.W") | <file:line> | critical | ``
- `$TARGET` not set → surface OVERDUE candidates as informational (`⚠ remove_in="V.W" scheduled but target version unknown`)

### Phase 1b: Upstream review verdict check

Verify no blocking `/oss:review` (or codex-lineage review) verdict already exists for the current branch before declaring readiness — closes the gap where `audit` re-derives blockers a prior review already found instead of surfacing them immediately as a pre-flight failure.

```bash
REVIEW_FILE=$(ls -t .reports/review/*/review-report.md .reports/review/*/*/review-report.md .reports/codex/review/*/review-notes.md 2>/dev/null | head -1)
```

- No match → skip this check (informational: no prior review on file).
- Match under `.reports/review/pr-*/run-*/review-report.md` or the legacy pre-rename `.reports/review/*/review-report.md` (both oss lineage) → grep its `Outcome:` YAML field. `✗` or `⚠ NEEDS_ATTENTION` with unresolved blocking findings → add to Phase 2 Findings summary: `| Upstream review blocking | ✗ <REVIEW_FILE> reports <Outcome value> | <path> | critical |`.
- Match under `.reports/codex/review/*/review-notes.md` (codex lineage) → grep `Recommendation:` / `Blocking findings:` lines. `needs-more-work` with non-empty `Blocking findings:` → same critical finding, quoting the blocking finding IDs.
- Only act on a review whose header/scope names the branch or PR currently being released — a review for a different branch/PR is not a blocker here; note it as informational context instead.

### Phase 2: Readiness checks

Execute all checks from `templates/audit-checks.md`. Checks cover: version consistency across manifests, docs/CHANGELOG alignment, open blocking issues, dependency CVE scan, unreleased commits since last tag, changelog scope (commits landed via a non-PR branch merge inside `$RANGE` — catches an unrelated already-released commit re-appearing in this release's section by accident, see Audit changelog's Scope check in `SKILL.md`).

Every `critical` row from the Scope check (already-released commit landed a second time) → add to the Findings summary table below, `severity: critical` — this is the check that must be visible to the reviewer before the PR merges, not something the audit file alone carries.

```bash
cat "$SKILL_DIR/templates/audit-checks.md"  # timeout: 5000
```

After readiness table, if issues found, append **Findings summary** table:

| # | Issue | Location | Severity |
| -- | -- | -- | -- |
| 1 | <what is wrong> | <section or file> | critical/high/medium/low |

Every finding needs explicit location, severity, action — matches structured output format of `notes` and `changelog` modes.

### Phase 2a: Codex adversarial audit (if available)

Checklist checks above (version/docs/CVE/deprecation) are static and can't catch semantic gaps — e.g. a public example contradicting the release's own behavior change, or a migration note describing the wrong version. When Codex is installed, dispatch it as an independent adversarial pass over the same `RANGE` before declaring a verdict.

```bash
CODEX_STATUS=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/check_bridge.py" --status 2>/dev/null || echo "absent")  # timeout: 5000
if [ "$CODEX_STATUS" = "available" ]; then CODEX_AVAILABLE=1; echo "bridge@borda-ai-rig available"; else CODEX_AVAILABLE=0; echo "⚠ bridge@borda-ai-rig is ${CODEX_STATUS} — skipping adversarial audit pass"; fi
```

`CODEX_AVAILABLE=0` → skip this phase entirely, no finding added.

`CODEX_AVAILABLE=1` →

```text
Skill(skill="bridge:review", args="Read-only adversarial release-readiness audit. Working directory: <REPO_ROOT>. Range: <RANGE>. Target version: <TARGET or 'next'>. Treat every user-facing claim as wrong until proven correct by reading the actual source at HEAD.

Check specifically: (1) do public docs/examples touched or implied by this range still match current default behavior — not just changed files, but examples elsewhere in docs/ that call the same code path; (2) does the migration guide (if any) place new behavior under the correct version step, not backported into an earlier historical entry; (3) does the changelog/PR narrative account for every commit in range, including ones bundled into another PR; (4) any new or changed optional dependency reaching a platform without a recorded review/disposition.

Write full findings to <ADVERSARIAL_AUDIT_FILE> using the Write tool. Return ONLY: {\"status\":\"done\",\"file\":\"<path>\",\"critical\":N,\"high\":N,\"medium\":N,\"low\":N,\"confidence\":0.N}")
```

Expand `<REPO_ROOT>`, `<RANGE>`, `<TARGET>`, `<ADVERSARIAL_AUDIT_FILE>` (`.temp/release-codex-audit-$BRANCH-$DATE.md`) to literal values before spawning.

Read the returned file. Every `critical`/`high` finding → add a row to the Findings summary table above (`severity: critical` or `high`), quoting the finding text. `medium`/`low` → append as `> ⚠ Codex audit notes: <summary>` below the table, non-blocking.

### Output routing

Write full report to `.reports/release/$BRANCH-$DATE.md` (create dir with `mkdir -p .reports/release` if needed) — **not** `.temp/`. Overrides quality-gates default `.temp/output-...` path. Print verdict line and executive summary to terminal per quality-gates rules.

### Verdict line (mandatory final output)

Print exactly one verdict line immediately before `## Confidence` block so callers (e.g. `prepare` Phase 1) can pattern-match without parsing prose:

- `verdict: READY` — no CRITICAL or HIGH findings
- `verdict: NEEDS_ATTENTION` — one or more HIGH findings, no CRITICAL
- `verdict: BLOCKED` — one or more CRITICAL findings (also written when readiness checks cannot complete)

End response with `## Confidence` block per CLAUDE.md output standards.
