```bash
TARGET=$(echo "$ARGUMENTS" | awk '{print $2}') # optional target version
LAST_TAG=$(git describe --tags --abbrev=0 2>/dev/null || git rev-list --max-parents=0 HEAD)
RANGE="$LAST_TAG..HEAD"
```

### Pre-flight: gh authentication

```bash
# Fail fast with a clear message if gh is not authenticated
gh auth status 2>&1 || {
    echo "gh not authenticated — run 'gh auth login' first"
    exit 1
}
```

### Check 1: Repository state

```bash
# Uncommitted changes
git status --short

# Unreleased commits
git log $RANGE --oneline --no-merges
```

### Check 2: CI health

```bash
gh run list --branch "$(git rev-parse --abbrev-ref HEAD)" --limit 5 \
    --json status,conclusion,name 2>/dev/null || true
```

### Check 3: Open issues and PRs

```bash
# Issues with blocker or bug labels (high-severity candidates)
gh issue list --state open --limit 30 \
    --json number,title,labels 2>/dev/null || echo "[]"

# Open PRs targeting main — anything that should land before the release?
gh pr list --state open --base main --limit 20 \
    --json number,title,draft,reviewDecision 2>/dev/null || echo "[]"
```

### Check 4: Documentation alignment

```bash
# What files changed since last tag?
git diff $RANGE --name-only

# Did README or any docs change? If not, flag for manual review.
git diff $RANGE --name-only | grep -iE 'readme|\.md$|docs/' || echo "no docs changed"
```

Read `README.md` and verify: install/usage examples match current API, version references are not pinned to old releases, any deprecated APIs mentioned are still present (or have deprecation notes). If `docs/` exists, spot-check recently changed public API sections against the docs.

Check `CHANGELOG.md`: does it have an `[Unreleased]` entry or a section for `$TARGET` covering commits in `$RANGE`?

### Check 5: Version consistency

```bash
grep -rn '__version__\|^version\s*=' --include="*.py" --include="*.toml" \
    --include="*.cfg" --include="*.json" . 2>/dev/null | grep -v ".git" | head -15
```

All declarations must agree. If `$TARGET` was given, verify it matches (or flag it needs bumping).

### Check 6: Critical code signals

```bash
# Release-blocking TODOs outside test files
grep -rn "TODO.*release\|FIXME\|HACK\|XXX" --include="*.py" \
    --exclude-dir=".git" --exclude-dir="tests" . 2>/dev/null | head -10

# Dependency CVE scan (if available)
command -v pip-audit &>/dev/null && pip-audit --format=json 2>/dev/null |
python3 -c "import sys,json; d=json.load(sys.stdin); print(f'{len(d[\"dependencies\"])} deps, {sum(len(x[\"vulns\"]) for x in d[\"dependencies\"])} vulns')" 2>/dev/null || true
```

### Output

Print a readiness report:

```
## Release Readiness — [repo] [version or "next release"]
Date: [date] | Range: [last-tag]..HEAD ([N] commits)

| Check                 | Status | Detail |
|-----------------------|--------|--------|
| Working tree          | ✅ Clean / ⚠️ N files | [filenames if dirty] |
| CI (last 5 runs)      | ✅ Passing / ❌ N failing | [failing job names] |
| Blocking issues       | ✅ None / ❌ N open | [#N title] |
| Open PRs (main)       | ✅ None / ⚠️ N open | [PR titles] |
| README aligned        | ✅ / ⚠️ Review needed | [reason if flagged] |
| CHANGELOG entry       | ✅ Present / ❌ Missing | [section name or "add [Unreleased]"] |
| Version consistent    | ✅ / ⚠️ Mismatch | [files and values] |
| Dependency CVEs       | ✅ Clean / ⚠️ N vulns | [package names] |

### Verdict
**READY** — no blockers. Run `/release prepare <version>` to write artifacts.
— or —
**NEEDS ATTENTION** — N items before release:
- ❌ [blocking item]
- ⚠️ [recommended item]

### Next steps
[e.g., "resolve open PRs → re-run `/release audit v1.3.0` to verify → `/release prepare v1.3.0`"]
```
