---
name: audit
description: Minimal codex-native audit loop. Use to scan codex configuration/workflow drift and emit ranked gaps with measurable gates.
---

# Audit

Run a linear configuration and workflow audit loop.

## Input Schema

```json
{
  "scope": "config|skills|agents|all",
  "target": "optional path",
  "done_when": "drift and broken references are ranked with gate result"
}
```

## Workflow (Exact Commands)

1. Create run directory.

```bash
TS=$(date -u +%Y-%m-%dT%H-%M-%SZ)
OUT_DIR=".reports/codex/audit/$TS"
mkdir -p "$OUT_DIR"
```

2. Collect inventory.

```bash
find .codex -maxdepth 4 -type f | sort >"$OUT_DIR/inventory.txt"
```

3. Run shared quality gates.

```bash
.codex/skills/_shared/run-gates.sh \
    --out "$OUT_DIR" \
    --lint "${LINT_CMD:-bash -lc 'if command -v ruff >/dev/null 2>&1; then ruff check .codex; else UV_CACHE_DIR=${UV_CACHE_DIR:-/tmp/codex-uv-cache} uv run --no-sync ruff check .codex; fi'}" \
    --format "${FORMAT_CMD:-bash -lc 'if command -v ruff >/dev/null 2>&1; then ruff format --check .codex; else UV_CACHE_DIR=${UV_CACHE_DIR:-/tmp/codex-uv-cache} uv run --no-sync ruff format --check .codex; fi'}" \
    --types "${TYPES_CMD:-true}" \
    --tests "${TESTS_CMD:-true}" \
    --review "${REVIEW_CMD:-git diff --check}"
```

4. Detect drift and broken references.

```bash
rg -n "config_file|skills/|quality-gates|run-gates.sh|write-result.sh" .codex >"$OUT_DIR/reference-scan.txt"
```

5. Audit spawn-pattern coverage and overlap in `AGENTS.md` (instruction-level check).

```bash
rg -n "^### Spawn $(.+) when:" .codex/AGENTS.md >"$OUT_DIR/spawn-sections.txt"
rg -n "Automatic spawn patterns \\(all agents\\)|Collaboration team patterns" .codex/AGENTS.md >"$OUT_DIR/spawn-policy-sections.txt"
```

6. Classify findings using `../_shared/severity-map.md`.
7. Write mandatory result artifact.

```bash
.codex/skills/_shared/write-result.sh \
    --out "$OUT_DIR/result.json" \
    --status "$STATUS" \
    --checks-run "lint,format,types,tests,review" \
    --checks-failed "$CHECKS_FAILED" \
    --critical "$CRITICAL" \
    --high "$HIGH" \
    --medium "$MEDIUM" \
    --low "$LOW" \
    --confidence "$CONFIDENCE" \
    --artifact-path "$OUT_DIR/result.json"
```

## Fail-fast Rules

1. Missing `.codex` inventory => fail.
2. Shared gate script missing => fail.
3. Broken config/skill references in critical paths => fail.
4. Missing spawn coverage for any configured agent => fail.
5. Unclear or overlapping spawn intent without explicit collaboration-team guidance => fail.
6. Result artifact missing => fail.

## Output Contract

Use shared gate schema from `../_shared/quality-gates.md`.

Minimum artifact payload:

```json
{
  "status": "pass|fail|timeout",
  "checks_run": [
    "lint",
    "format",
    "types",
    "tests",
    "review"
  ],
  "checks_failed": [],
  "findings": {
    "critical": 0,
    "high": 0,
    "medium": 0,
    "low": 0
  },
  "confidence": 0.0,
  "artifact_path": ".reports/codex/audit/<timestamp>/result.json",
  "recommendations": [],
  "follow_up": []
}
```
