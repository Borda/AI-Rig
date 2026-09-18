<!-- file: audit-checks.md — consumers: oss skills/release/modes/audit.md (Phase 2), oss skills/release SKILL.md (Write release draft phase pre-flight) -->

```bash
eval "$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/parse-skill-flags.py" --flags changelog,summary,migration,append "$ARGUMENTS")"  # timeout: 5000
TARGET=$(echo "$CLEAN_ARGS" | awk '{print $2}')  # optional target version, positional — flags stripped first
# use caller RANGE if set (branch-aware detection, Shared setup)
# fallback: git describe, stable-tag-only filter — matches skill detection
if [ -z "$RANGE" ]; then
    LAST_TAG=$(git describe --tags --abbrev=0 --exclude='*rc*' --exclude='*dev*' --exclude='*alpha*' --exclude='*beta*' 2>/dev/null || git rev-list --max-parents=0 HEAD)
    RANGE="$LAST_TAG..HEAD"
fi
```

### Data gathering (Checks 1, 2, 3, 4a, 4b, 5, 6 + gh-auth preflight)

In `bin/run_audit_checks.py` — emits sectioned output (`--- check: <name> ---` banners): repo state (`git status`, unreleased commits), CI health (`gh run list`), open issues+PRs, files changed in range, version-declaration grep, release-blocking TODO/FIXME/HACK grep, pip-audit CVE scan. Invokes `bin/parse_audit_json.py` to summarize pip-audit JSON. Caller captures one buffer for parsing:

```bash
# loads: run_audit_checks.py, parse_audit_json.py
AUDIT_OUT=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/run_audit_checks.py" \
    --repo "$(gh repo view --json nameWithOwner --jq .nameWithOwner 2>/dev/null)" \
    ${TARGET:+--tag "$TARGET"} \
    ${RANGE:+--range "$RANGE"})  # timeout: 60000
echo "$AUDIT_OUT"
```

Script exits `2` if `gh` not authenticated — surface as `BLOCKED` verdict immediately, skip interpretive steps below.

### Check 4a interpretation (after data is in `$AUDIT_OUT`)

Read `README.md`: install/usage match current API, version refs not stale, deprecated APIs have notes. `docs/` exists → read all changed public API sections.

Check `CHANGELOG.md`: `[Unreleased]` or `$TARGET` section covers `$RANGE` commits?

### Check 4b: Doc weight ratio (🚀 Added features)

For each 🚀 Added entry in CHANGELOG `$TARGET`/`[Unreleased]` naming a significant new entity (public skill, command, agent, submodule, mode): compute **doc weight** for it and 2–3 comparable existing features (same task type, mode category, conceptual peer) in relevant README/docs.

Doc weight = `header_score + coverage_score + example_score`:

- `header_score`: H2 = 3, H3 = 2, H4/deeper = 1, no heading = 0
- `coverage_score`: `min(non_blank_lines_in_section / 5, 5)` — non-blank lines from feature heading to next same-or-higher heading
- `example_score`: fenced code blocks in section, capped at 3

Weight ratio = `new_feature_weight / mean(comparable_weights)`. Flag **HIGH** (UNDERTREATED) if ratio < 0.5. Report: `- [UNDERTREATED] <feature>: weight N vs peers M1/M2 (ratio R)`. Add to findings table, `severity: high`.

### Check 5 interpretation

All version declarations in `version-consistency` section must match. `$TARGET` given → verify or flag needs bump.

### Check 6 interpretation: pip-audit gap (after data is in `$AUDIT_OUT`)

`run_audit_checks.py` stays non-interactive — if `pip-audit` missing, prints machine-readable line (`pip-audit-status: not-installed`) instead of prompting. Check:

```bash
echo "$AUDIT_OUT" | grep -q '^pip-audit-status: not-installed' && PIP_AUDIT_MISSING=1 || PIP_AUDIT_MISSING=0
```

`PIP_AUDIT_MISSING=1` → invoke `AskUserQuestion`: "pip-audit not installed — CVE dependency scan will be skipped. Install now (`pip install pip-audit`) and rerun the scan?" Options: (a) Skip CVE check for this run (b) Install and rerun — Recommended.

- **Install and rerun**: install, re-run only CVE step (no need to re-run full data-gathering script — gh/git checks already in `$AUDIT_OUT`):
  ```bash
  pip install pip-audit  # timeout: 90000
  pip-audit --format=json | python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/parse_audit_json.py"  # timeout: 30000
  ```
  Fill Dependency CVEs row with real `N deps, M vulns` result, same as any successful scan.
- **Skip**: Dependency CVEs row stays `⚠ pip-audit not installed — CVE scan skipped` — informational soft gap, same convention as Phase 1a's "target version unknown" case (`modes/audit.md`): never added to Findings summary table as own finding, doesn't block `READY` alone.

### Output

Print readiness report:

```markdown
---
repo: [repo-name]
date: [YYYY-MM-DD]
version: [version or "next"]
range: [range]
mode: [linear | stable-branch (cherry-pick)]
commits: [N non-merge commits]
verdict: [READY | NEEDS_ATTENTION | BLOCKED]
---

## Release Readiness — [repo] [version or "next release"]
Date: [date] | Range: [last-tag]..HEAD ([N] commits)

| Check            | Status | Detail |
|------------------|--------|--------|
| Release mode     | Linear / Stable-branch (cherry-pick) | [N pending cherry-pick subjects, or "—" if linear — informational only, never blocking by itself] |
| Working tree     | ✓ Clean / ⚠ N files | [filenames if dirty] |
| CI (last 5 runs) | ✓ Passing / ✗ N failing | [failing job names] |
| Blocking issues  | ✓ None / ✗ N open | [#N title] |
| Open PRs (main)  | ✓ None / ⚠ N open | [PR titles] |
| README aligned   | ✓ / ⚠ Review needed | [reason if flagged] |
| CHANGELOG entry  | ✓ Present / ✗ Missing | [section name or "add [Unreleased]"] |
| Changelog scope  | ✓ Clean / ✗ N flagged | [sha + non-PR merge sha, or "already released in <section>"] |
| Version consistent  | ✓ / ⚠ Mismatch | [files and values] |
| Dependency CVEs     | ✓ Clean / ⚠ N vulns | [package names] |
| Scheduled removals  | ✓ All removed / ✗ N still present | [symbol names with `remove_in` version] |
| Doc proportionality | ✓ / ⚠ N features undertreated | [feature names — no dedicated section / no example / thin coverage] |
| Upstream review verdict | ✓ None blocking / ✗ Blocking | [review report path + outcome, Phase 1b] |
| Codex adversarial audit | ✓ Clean / ⚠ N findings / — skipped (codex unavailable) | [finding summary, Phase 2a] |

### Verdict
**READY** — no blockers. Run `/release prepare <version>` to write artifacts.
— or —
**NEEDS_ATTENTION** — N items before release:
- ✗ [blocking item]
- ⚠ [recommended item]

### Next steps
[e.g., "resolve open PRs → re-run `/release audit v1.3.0` to verify → `/release prepare v1.3.0`"]
```

**Terminal output** — after writing report file, print readiness check table (`| Check | Status | Detail |` rows only, no YAML header, no verdict prose) directly to terminal, inline in Claude response. Mandatory even when audit runs as sub-phase of `/release prepare` — never treat table as intermediate pipeline output or route only to report file; must appear in terminal before prepare proceeds to Phase 2.
