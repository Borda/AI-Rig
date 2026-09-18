# Mode: Ecosystem Impact (for library maintainers)

<workflow>

Replace `mypackage` in commands below with actual package name (e.g. from `gh repo view --json name --jq .name`).

```bash
gh api "search/code" --field "q=from mypackage import language:python" \
    --jq '[.items[].repository.full_name] | unique | .[]'

# Requires johnnydep: pip install johnnydep (not installed by default — skip if unavailable)
# johnnydep mypackage --fields=name --reverse 2>/dev/null || echo "johnnydep not available — skipping PyPI reverse deps"

gh api "search/code" --field "q=mypackage repo:conda-forge/*-feedstock filename:meta.yaml" \
    --jq '[.items[].repository.full_name] | .[]'
```

Produce:

```markdown
---
Ecosystem Impact — [change description]
Consumers:   [N known downstream users of changed API]
Risk:        [High / Medium / Low]
Top action:  [single most urgent recommendation]
→ saved to [skill-specific path]
---

## Ecosystem Impact: [change description]

### Downstream Consumers Found
- [repo]: uses [specific API being changed]

### Breaking Risk
- **High** — ≥5 known consumers of changed API, OR any consumer in a major package (>10k weekly downloads on PyPI)
- **Medium** — 2–4 known consumers, OR API changed without deprecation cycle
- **Low** — ≤1 consumer, OR purely additive change (no removal/signature change)
- **Risk**: [High/Medium/Low] — [N] known consumers; [apply threshold above]
- Migration path: [available / needs documentation]

### Recommended Communication
- [create migration guide / add deprecation warning / notify maintainers directly]
```

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# Reload TODAY — fresh shell (Check 41); repeated `date` calls may straddle midnight
IFS= read -r TODAY < "${TMPDIR:-/tmp}/analyse-today-${CSID}" 2>/dev/null || TODAY=$(date +%Y-%m-%d)
mkdir -p .reports/analyse/ecosystem  # timeout: 5000
REPORT_FILE=".reports/analyse/ecosystem/output-analyse-ecosystem-$TODAY.md"
# Sentinel rewritten here, before the report exists — enforce-analyse-header.js gates SKILL.md Step 6a on this path
echo "$REPORT_FILE" > "${TMPDIR:-/tmp}/analyse-report-file-${CSID}"  # timeout: 5000
echo "[analyse] report → $REPORT_FILE"
```

Write full report to `$REPORT_FILE` (echoed above) via Write tool — **no full analysis to terminal**.

**Hook-enforced**: `hooks/enforce-analyse-header.js` (PreToolUse on `AskUserQuestion`) denies SKILL.md Step 6a's follow-up question while `$REPORT_FILE` missing/empty. Denial `oss:analyse report gate` = write never happened — write report, print header block, re-issue question. Hook checks report exists only, not header printed; print step below is that check.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# Reload _OSS_SHARED (Check 41: fresh shell; set by parent analyse/SKILL.md)
IFS= read -r _OSS_SHARED < "${TMPDIR:-/tmp}/analyse-oss-shared-${CSID}" 2>/dev/null || _OSS_SHARED=""
cat "$_OSS_SHARED/terminal-summaries.md"  # timeout: 5000
```

Compact terminal summary template (loaded above). File absent → warn: "run /foundry:setup — printing plain terminal output instead." Use **Ecosystem Impact Summary** template. Replace `[skill-specific path]` with `$REPORT_FILE`. Terminal block: `---` own line, entity line next, `→ saved to <path>` at end, `---` close. Print lines 1–6 of report file, append `→ saved to <path>`. Report already has block — no separate prepend

</workflow>

<notes>

- **GitHub search rate limit**: `gh api search/code` rate-limited ~30 req/min; `--paginate` may hit secondary limit on large sets — add `sleep 2` between pages if needed
- **PyPI download counts**: johnnydep not installed by default; skip if unavailable; alt: libraries.io API for reverse deps
- **Risk threshold calibration**: thresholds (5 consumers = High) guidelines for OSS Python libs; adjust for internal/enterprise repos where even 1 consumer may be critical
- **conda-forge**: feedstock search returns repo names (`conda-forge/mypackage-feedstock`), not actual dependent packages — treat as 1 known consumer per feedstock found

</notes>
