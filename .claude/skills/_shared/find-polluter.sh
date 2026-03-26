#!/usr/bin/env bash
# find-polluter.sh — binary-search test isolation
#
# Finds which test in a suite contaminates another test when run before it.
# Uses binary search: O(log N) runs instead of O(N).
#
# Usage:
#   bash .claude/skills/_shared/find-polluter.sh <failing-test-id> [test-dir]
#
# Arguments:
#   failing-test-id   pytest node ID of the test that fails due to contamination
#                     e.g. tests/test_foo.py::TestClass::test_method
#   test-dir          directory to search for candidate tests (default: tests)
#
# Example:
#   bash .claude/skills/_shared/find-polluter.sh tests/test_model.py::test_predict tests/
#
# Requirements: pytest available on PATH (or via `python -m pytest`)

set -euo pipefail

FAILING_TEST="${1:-}"
TEST_DIR="${2:-tests}"
PYTEST="python -m pytest"

# ── Validate input ─────────────────────────────────────────────────────────────

if [[ -z "$FAILING_TEST" ]]; then
  echo "Usage: $0 <failing-test-id> [test-dir]" >&2
  echo "  Example: $0 tests/test_foo.py::test_bar tests/" >&2
  exit 1
fi

if ! command -v python &>/dev/null; then
  echo "✗ python not found on PATH" >&2
  exit 1
fi

# ── Step 1: Verify the test passes in isolation ────────────────────────────────

echo "→ Checking $FAILING_TEST in isolation..."
if $PYTEST "$FAILING_TEST" -q --tb=short 2>&1 | grep -qE "^(PASSED|1 passed)"; then
  echo "✓ Passes in isolation — test-ordering contamination confirmed"
else
  echo "✗ $FAILING_TEST fails in isolation — not a test-ordering issue" >&2
  echo "  Fix the test itself before using this script." >&2
  exit 1
fi

# ── Step 2: Collect candidate tests ───────────────────────────────────────────

echo "→ Collecting candidates from $TEST_DIR..."
CANDIDATES_FILE=$(mktemp)
trap 'rm -f "$CANDIDATES_FILE"' EXIT

$PYTEST "$TEST_DIR" --collect-only -q 2>/dev/null \
  | grep "::" \
  | grep -v "^$FAILING_TEST\$" \
  | grep -v "^$" \
  > "$CANDIDATES_FILE" || true

TOTAL=$(wc -l < "$CANDIDATES_FILE" | tr -d ' ')

if [[ "$TOTAL" -eq 0 ]]; then
  echo "✗ No candidate tests found in $TEST_DIR" >&2
  exit 1
fi

echo "✓ Found $TOTAL candidates — starting binary search (up to $(( $(python3 -c "import math; print(math.ceil(math.log2($TOTAL + 1)))") )) rounds)"
echo ""

# ── Step 3: Binary search ──────────────────────────────────────────────────────

# Load candidates into array
mapfile -t ALL_TESTS < "$CANDIDATES_FILE"

LO=0
HI=${#ALL_TESTS[@]}
ROUND=0

while [[ $((HI - LO)) -gt 1 ]]; do
  ROUND=$((ROUND + 1))
  MID=$(( (LO + HI) / 2 ))
  COUNT=$((MID - LO))

  echo "  Round $ROUND: testing [$LO–$MID] ($COUNT tests)..."

  BATCH_FILE=$(mktemp)
  printf '%s\n' "${ALL_TESTS[@]:$LO:$COUNT}" > "$BATCH_FILE"

  # Run batch + failing test; check if failing test is contaminated
  if $PYTEST $(tr '\n' ' ' < "$BATCH_FILE") "$FAILING_TEST" -q --tb=no 2>&1 \
      | grep -qE "FAILED|ERROR"; then
    HI=$MID   # polluter is in [LO, MID)
  else
    LO=$MID   # polluter is in [MID, HI)
  fi

  rm -f "$BATCH_FILE"
done

POLLUTER="${ALL_TESTS[$LO]}"

# ── Step 4: Report ─────────────────────────────────────────────────────────────

echo ""
echo "✓ Polluter found after $ROUND rounds:"
echo ""
echo "  $POLLUTER"
echo ""
echo "Verify with:"
echo "  $PYTEST \"$POLLUTER\" \"$FAILING_TEST\" -v"
echo ""
echo "Next steps:"
echo "  1. Run the verify command above to confirm"
echo "  2. Check $POLLUTER for shared state mutation (module-level vars, fixtures, monkeypatches)"
echo "  3. Add proper teardown or use pytest fixtures with 'function' scope to isolate the state"
