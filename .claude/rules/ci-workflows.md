---
description: GitHub Actions CI workflow rules — SHA pinning, Python matrix
paths:
  - .github/workflows/**/*.yml
---

## Action Version Pinning

Prefer **semantic version tags** (`@v4`) — they are readable, meaningful, and track the action maintainer's intended stable release. SHA pinning is optional and reserved for workflows with strict supply-chain requirements.

```yaml
# Preferred — semantic version tag
uses: actions/checkout@v4

# Optional — SHA pin for strict supply-chain hardening (add tag comment)
uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683  # v4

# Wrong — branch ref (mutable, dangerous)
uses: actions/checkout@main
```

To find the latest tag for an action:

```bash
gh api repos/<owner>/<action-repo>/tags --jq '.[0].name'
```

To resolve the SHA for a tag (if pinning is required):

```bash
gh api repos/<owner>/<action-repo>/git/ref/tags/<tag> --jq '.object.sha'
```

Severity tiers:

- **critical** — branch/named refs (`@main`, `@master`, `@latest`) — mutable, supply-chain risk
- **high** — no version ref at all (bare action name with no `@`)
- **low** — SHA pin without an accompanying `# vN` comment (opaque, unreadable)

## Python Matrix

- Python matrix must start at **3.10** minimum (Python 3.9 reached EOL Oct 2025)
- Always test on at least 2 Python versions
- Recommended matrix: `['3.10', '3.11', '3.12', '3.13']`

## Other Rules

- `fail-fast: false` on matrix jobs — early exit hides failures in other cells
- Never `continue-on-error: true` on required status checks
- Gate image pushes: `push: ${{ github.event_name != 'pull_request' }}`
