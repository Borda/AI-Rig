# Numbers & Claims Reference

Source of truth for numeric limits in `release/SKILL.md`. Update when number changes — no undocumented changes.

## Numeric Limits

### PR list limit — `--limit 500`

| Field | Value |
| -- | -- |
| Location | `Gather changes` → `gh pr list` |
| Current value | `500` |
| Floor | GitHub CLI default: **30** (silent truncation) |
| Evidence | `rules/external-data.md` §GitHub CLI: "set at least 10× higher than expected"; typical release = 2–4 sprints × 10–50 PRs/sprint = 40–200 PRs |
| If insufficient | At exactly 500 results, recover with `gh api --paginate --slurp`; otherwise the result is below the documented cap. |

### Release convention scan — `--limit 5`

| Field | Value |
| -- | -- |
| Location | `Write release draft` → `gh release list` for style-matching |
| Current value | `5` |
| Evidence | 5 covers interleaved stable/LTS/minor releases; recency + sample variety without latency penalty |
| If insufficient | Bump to 10; read-only, no correctness risk |

### Latest non-prerelease tag detection — `--limit 100`

| Field | Value |
| -- | -- |
| Location | `Write release draft` → `LATEST_TAG` detection |
| Current value | `100` |
| Evidence | GitHub release API reverse-chronological; gap between stable releases exceeding `--limit` returns wrong tag silently; 100 handles repos with ≥80 pre-release tags between stable cuts |
| If insufficient | Use `gh api --paginate 'repos/{owner}/{repo}/releases'` and select the first non-prerelease match; `gh release list` has no `--paginate` flag |

### Demo execution timeout — 10 minutes (`timeout: 600000`)

| Field | Value |
| -- | -- |
| Location | `Generate release demo` and `Mode: prepare / Phase 4a` |
| Current value | `600000` ms (10 min) |
| Evidence | `rules/claude-config.md` §Bash Timeouts: P90 for test suite = 3 min → 3× = 600000 ms; demo P90 ≈ 2–3 min (model load + inference + dataset); 3× headroom = 600000 ms |
| Gate | Demo must exit 0 within 10 min; if legitimately longer, redesign to use cached weights or smaller fixture |
| If wrong | Do not reduce below 300000 (5 min); if timing out, diagnose network I/O or cold-start deps |

## Count Guidance

Design choices — adjust when release output feels too sparse or dense.

### Highlights count — 3–5

| Field | Value |
| -- | -- |
| Location | `Identify highlights` |
| Range | 3 to 5 |
| Rationale | >5 = not highlight; \<3 = thin for non-trivial releases; range covers sparse (one breaking change) vs. dense (major version) releases |
| If wrong | Fewer than 3 for major version → expand; more than 5 → split into highlights + "Also in this release" |

### Demo headline features — 2–3

| Field | Value |
| -- | -- |
| Location | `Mode: demo / Phase 1` |
| Range | 2 to 3 |
| Rationale | Demo narrative; >3 sections fragment attention; \<2 thin for release demo |
| If wrong | Adjust per release density; >3 allowed for major versions with distinct user-visible features |

### Contributor summary — 3–6 words

| Field | Value |
| -- | -- |
| Location | `Extract contributors` |
| Range | 3 to 6 words |
| Rationale | Fits credits line without wrapping; specific enough to mean something; short enough to scan |
| If wrong | Expand to phrase if contribution spans multiple areas |

## Performance Claims — Fact-Check Gate

Quantitative claims ("2× faster", "50% memory reduction", "latency −30 ms") in commit messages or PR bodies need evidence before inclusion in release notes. Two tiers:

| Tier | Source | Inclusion rule |
| -- | -- | -- |
| Supported | PR body cites benchmark run, CI artifact, profiling output, or timing table | Include claim verbatim |
| Unsupported | Claim from commit subject only — no artifact linked | Rewrite to "improved performance" without number |

**Never include raw numeric claims from commit subjects alone** — commit subjects unreviewed author claims; PR body + artifacts ground truth.
