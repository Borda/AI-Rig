---
description: GitHub Actions CI workflow rules — SHA pinning, Python matrix
paths:
  - .github/workflows/**/*.yml
---

## Action Version Pinning

Prefer **semantic version tags** (`@v4`) — they are readable, meaningful, and track the action maintainer's intended stable release. SHA pinning is optional and reserved for workflows with strict supply-chain requirements.

Preferred — semantic version tag:

```yaml
uses: actions/checkout@v4
```

Optional (but more secure) — SHA pin for strict supply-chain hardening (add tag comment):

```yaml
uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683  # v4
```

Wrong — branch ref (mutable, dangerous):

```yaml
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

- Python matrix must start at **3.11** minimum (canonical source: `.claude/rules/python-code.md`) <!-- verified: 2026-04-04 -->
- Always test on at least 2 Python versions
- Recommended matrix: `['3.11', '3.12', '3.13', '3.14']` <!-- verified: 2026-04-04 --> — note: `3.14` is pre-release (alpha); use `allow-failures: true` or a separate experimental matrix cell until 3.14 reaches stable <!-- re-check by 2026-10-01 -->
- Always set `fail-fast: false` on the strategy block — early exit hides failures in other matrix cells

## Other Rules

- Never `continue-on-error: true` on required status checks
- Gate image pushes with an `if:` condition on the push step:
  ```yaml
    - name: Push Docker image
      run: docker push myimage:latest
      if: ${{ github.event_name != 'pull_request' }}
  ```
