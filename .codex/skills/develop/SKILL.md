---
name: develop
description: Minimal codex-native develop loop. Use for implementation tasks with linear plan-build-verify flow and measurable quality gates.
---

# Develop

Run a linear implementation loop with strict gates.

## Input Schema

```json
{
  "goal": "required implementation objective",
  "constraints": [
    "optional constraints"
  ],
  "done_when": "required acceptance statement"
}
```

## Workflow (Exact Commands)

1. Create run directory.

```bash
TS=$(date -u +%Y-%m-%dT%H-%M-%SZ)
OUT_DIR=".reports/codex/develop/$TS"
mkdir -p "$OUT_DIR"
```

2. Record baseline diff and branch.

```bash
git rev-parse --abbrev-ref HEAD >"$OUT_DIR/branch.txt"
git diff --stat >"$OUT_DIR/before.diffstat"
```

3. Implement minimal change.
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

5. Classify findings using `../_shared/severity-map.md`.
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

1. Missing `goal` or `done_when` => fail.
2. Shared gate script missing => fail.
3. Any critical finding => fail.
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
  "artifact_path": ".reports/codex/develop/<timestamp>/result.json"
}
```
