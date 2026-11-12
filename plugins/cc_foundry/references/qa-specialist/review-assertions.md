## Verify Before Asserting

Never claim a pattern exists without confirming via Grep/Glob first — applies to all findings referencing codebase-wide patterns.

**Occurrence thresholds** — when asserting established pattern:

- > 10 occurrences → Established (flag new code that deviates as finding)
- 3–10 occurrences → Emerging (list whether intentional under `Contract questions:` for the caller — not blocking finding)
- < 3 occurrences → Not established (skip pattern claims entirely)

**Conditional context loading** — load extra context based on diff or target contents:

| Diff Contains | Context to Load |
| -- | -- |
| DB queries (`SELECT`, `.filter(`, `session.query`) | Check schema; look for N+1 patterns *[perf-optimizer domain — observation only, not a qa defect]* |
| Auth logic (`password`, `token`, `jwt`, `session`, `bcrypt`) | Grep for token storage patterns; verify no secrets in logs |
| File uploads or `open()` calls | Check for size limits and path traversal prevention |
| External API calls (`requests.`, `httpx.`, `aiohttp.`) | Check timeout, retry, error handling *[sw-engineer domain — observation only]* |
| New `import`/`from` packages | Verify package exists in `pyproject.toml` / `requirements*.txt` |
| `os.system(`, `subprocess.*`, `shlex` | Check shell-injection: verify `shell=False` (or kwarg absent); args must be list, not f-string or concatenated string; `shlex.quote()` only valid when `shell=True` strictly unavoidable |

**Domain-boundary rule**: rows tagged `[perf-optimizer domain]` or `[sw-engineer domain]` surface as observations, not qa defects. Don't count in coverage-gap totals; redirect substantive findings to owning agent — file under Style/Quality Observations (see `<reporting-format>` below).

**Uncertainty markers** — display-only aliases for `[critical]/[high]/[medium]/[low]`; use in prose annotations only, never as primary severity label in coverage-gap findings. Scope: QA report prose only — distinct from terminal-output severity markers (`!` = critical, `⚠` = warning, `✓` = pass) in `communication.md`:

- `🔴 Must fix:` (alias: `[critical]`) — critical finding, verified via Grep/Read
- `⚠️ High risk:` (alias: `[high]`) — likely runtime failure or persistent flakiness; no emoji alias in bracket notation, use `[high]` directly
- `❓ To verify:` (alias: `[medium]`) — pattern claim needing maintainer confirmation
- `💡 Consider:` (alias: `[low]`) — optional improvement, non-blocking
