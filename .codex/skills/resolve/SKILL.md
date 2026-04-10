---
name: resolve
description: Minimal codex-native resolve loop. Use to apply review findings, rerun checks, and publish unresolved gaps with measurable gates.
---

# Resolve

Run a linear resolve loop for findings closure.

## Input Schema

```json
{
  "findings_source": "required path or explicit list",
  "target_scope": "required path/module",
  "done_when": "critical/high findings are either fixed or explicitly unresolved"
}
```

## Workflow (Exact Commands)

1. Create run directory.

```bash
TS=$(date -u +%Y-%m-%dT%H-%M-%SZ)
OUT_DIR=".reports/codex/resolve/$TS"
mkdir -p "$OUT_DIR"
```

2. Validate and copy findings source.

```bash
cp "$FINDINGS_SOURCE" "$OUT_DIR/findings-input.txt"
```

3. Apply fixes in priority order: `critical` -> `high` -> `medium`.
4. Run shared quality gates.

```bash
.codex/skills/_shared/run-gates.sh \
    --out "$OUT_DIR" \
    --lint "${LINT_CMD:-uv run --no-sync ruff check .}" \
    --format "${FORMAT_CMD:-uv run --no-sync ruff format --check .}" \
    --types "${TYPES_CMD:-uv run --no-sync mypy src/}" \
    --tests "${TESTS_CMD:-uv run --no-sync pytest -q}" \
    --review "${REVIEW_CMD:-git diff --check}"
```

5. Write unresolved findings to `$OUT_DIR/unresolved.txt`.
6. Write mandatory result artifact.

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

1. Missing findings source => fail.
2. Shared gate script missing => fail.
3. Critical unresolved findings => fail.
4. Result artifact missing => fail.

## Output Contract

Use shared gate schema from `../_shared/quality-gates.md`.

Minimum artifact payload:

```json
{
  "status": "pass|fail",
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
  "artifact_path": ".reports/codex/resolve/<timestamp>/result.json"
}
```
