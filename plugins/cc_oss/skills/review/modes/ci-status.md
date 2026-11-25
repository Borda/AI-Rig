# Observe CI on the reviewed PR head

Run this block once at the calling step. Step 0 keeps the initial observation for reviewers. Step 5 stores the final observation separately for the report. Continue the source review immediately after each observation. Only the user starts a rerun. Never wait for checks, poll, or ask the user to rerun them.

Every observation records a gate in a run-scoped state file (Step 0's flag parse deletes it): a readable observation records its own gate, an unavailable one records the gate it held. An unavailable observation keeps an earlier same-head `BLOCK` and names it in `CI_STATUS`, so a refresh that cannot read the reviewed head never turns an established failure into `PASS`. A record that cannot be written adds `gate record not written` to `CI_STATUS`; an unavailable observation in that state holds `BLOCK`. Only the initial observation starts without a record: the preflight snapshot grants it once, the observation consumes that grant, and the Step 5 block withdraws any grant left before its own observation. A missing, consumed or unconsumable grant marks a later observation, so a failed Step 5 destination switch cannot pass the final observation off as the initial one. A later observation that finds no complete record cannot rule out an earlier failure, so it holds `BLOCK` and names the missing record. With a recorded earlier `PASS`, an unavailable observation prints `PASS` and the reason CI is unavailable.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r PR_TAG < "${TMPDIR:-/tmp}/oss-review-pr-tag-${CSID}" 2>/dev/null || PR_TAG=""
if ! [[ "$PR_TAG" =~ ^[0-9]+$ ]]; then printf 'CI_STATUS=not applicable\nCI_GATE=PASS\n'; exit 0; fi
IFS= read -r SNAP_DIR < "${TMPDIR:-/tmp}/oss-review-snap-dir-${CSID}" 2>/dev/null || SNAP_DIR=""
IFS= read -r CI_DIR < "${TMPDIR:-/tmp}/oss-review-ci-dir-${CSID}" 2>/dev/null || CI_DIR=""
CI_GATE_FILE="${TMPDIR:-/tmp}/oss-review-ci-gate-${CSID}"
CI_GRANT_FILE="${TMPDIR:-/tmp}/oss-review-ci-initial-${CSID}"
HELD_GATE=""; HELD_STATUS=""
[ -f "$CI_GATE_FILE" ] && while IFS='=' read -r _SK _SV; do case "$_SK" in CI_GATE) HELD_GATE=$_SV ;; CI_STATUS) HELD_STATUS=$_SV ;; esac; done < "$CI_GATE_FILE"
# initial = consumed preflight's grant; lost, used or unconsumable grant = later observation, so no failed write fakes it
IFS= read -r CI_GRANT < "$CI_GRANT_FILE" 2>/dev/null || CI_GRANT=""
[ "$CI_GRANT" = granted ] && printf 'consumed\n' > "$CI_GRANT_FILE" || CI_GRANT=""
# failed or partial write leaves no complete record; only the initial observation may lack one
if [ "$HELD_GATE" = BLOCK ]; then
    HELD_NOTE=" · gate held from earlier same-head observation: $HELD_STATUS"
elif [ "$HELD_GATE" = PASS ] || { [ ! -f "$CI_GATE_FILE" ] && [ "$CI_GRANT" = granted ]; }; then
    HELD_GATE=PASS; HELD_NOTE=""
else
    HELD_GATE=BLOCK; HELD_STATUS="no complete gate record from the earlier observation"; HELD_NOTE=" · gate held: $HELD_STATUS"
fi
CI_GATE=$HELD_GATE
CI_OK=false
if [ -n "$SNAP_DIR" ] && [ -n "$CI_DIR" ] &&
   PR_URL=$(jq -ber '.url' "$SNAP_DIR/pr-meta.json") &&
   PR_HEAD_SHA=$(jq -ber '.headRefOid | select(test("^([0-9a-f]{40}|[0-9a-f]{64})$"))' "$SNAP_DIR/pr-meta.json") &&
   mkdir -p "$CI_DIR"; then
    CI_STATUS="unavailable (check data unavailable for reviewed head)"
    # The PR endpoint follows the branch. Discard checks if the head changed across the reads.
    CI_HEAD=$(gh pr view "$PR_URL" --json headRefOid --jq .headRefOid) || CI_HEAD=""
    CI_HEAD=${CI_HEAD%$'\r'}  # Windows gh may end the line with CR
    if [ "$CI_HEAD" = "$PR_HEAD_SHA" ]; then
        CI_OK=true
        for _CHECK_SET in checks checks-required; do
            _CHECK_FLAGS=(); [ "$_CHECK_SET" != "checks-required" ] || _CHECK_FLAGS=(--required)
            _CHECK_RC=0
            gh pr checks "$PR_URL" "${_CHECK_FLAGS[@]}" --json name,bucket > "$CI_DIR/$_CHECK_SET.json" 2> "$CI_DIR/$_CHECK_SET.err" || _CHECK_RC=$?  # timeout: 15000
            case "$_CHECK_RC" in
                0|1|8)
                    if [ ! -s "$CI_DIR/$_CHECK_SET.json" ] && grep -qE '^no (required )?checks reported on ' "$CI_DIR/$_CHECK_SET.err"; then
                        printf '[]\n' > "$CI_DIR/$_CHECK_SET.json"
                    fi
                    jq -se 'length == 1 and (.[0] | type == "array" and all(.[]; (.name | type == "string") and (.bucket | IN("pass","fail","pending","skipping","cancel"))))' "$CI_DIR/$_CHECK_SET.json" >/dev/null || CI_OK=false ;;
                *) CI_OK=false ;;
            esac
        done
        CI_HEAD=$(gh pr view "$PR_URL" --json headRefOid --jq .headRefOid) || CI_HEAD=""
        CI_HEAD=${CI_HEAD%$'\r'}
    fi
    # A rerun between the all-checks and required-checks responses can change a result on the same SHA.
    if [ "$CI_OK" = true ] && [ "$CI_HEAD" = "$PR_HEAD_SHA" ] && jq -se '(.[1] - .[0]) | length == 0' "$CI_DIR/checks.json" "$CI_DIR/checks-required.json" >/dev/null; then
        # A required failure blocks merge. Projects without required checks use the full set.
        CI_GATE=$(jq -bsr 'if (.[1] | length) > 0 then .[1] else .[0] end | if any(.[]; .bucket == "fail") then "BLOCK" else "PASS" end' "$CI_DIR/checks.json" "$CI_DIR/checks-required.json")
        CI_STATUS=$(jq -br 'if length == 0 then "no checks reported" elif all(.[]; .bucket == "pass") then "passing (\(length)/\(length))" else [group_by(.bucket)[] | ({"fail":"failing","pending":"pending","cancel":"cancelled","skipping":"skipped","pass":"passing"}[.[0].bucket]) + " — " + (map(.name | gsub("[[:cntrl:]]"; " ")) | join(", "))] | join(" · ") end' "$CI_DIR/checks.json")
    else
        CI_OK=false
        # A stale or partial pass must never be credited by the QA reviewer.
        printf 'null\n' > "$CI_DIR/checks.json"
        printf 'null\n' > "$CI_DIR/checks-required.json"
        if [ -n "$CI_HEAD" ] && [ "$CI_HEAD" != "$PR_HEAD_SHA" ]; then CI_STATUS="unavailable (PR head changed)"; fi
    fi
else
    CI_STATUS="unavailable (check snapshot unavailable)"
fi
if [ "$CI_OK" = true ]; then
    printf 'CI_GATE=%s\nCI_STATUS=%s\n' "$CI_GATE" "$CI_STATUS" > "$CI_GATE_FILE" || CI_STATUS="$CI_STATUS · gate record not written"
else
    # unwritable record may also have lost an earlier failure: an unavailable observation then blocks
    printf 'CI_GATE=%s\nCI_STATUS=%s\n' "$HELD_GATE" "$HELD_STATUS" > "$CI_GATE_FILE" || { CI_GATE=BLOCK; HELD_NOTE="$HELD_NOTE · gate record not written"; }
    CI_STATUS="$CI_STATUS$HELD_NOTE"
fi
printf 'CI_STATUS=%s\nCI_GATE=%s\n' "$CI_STATUS" "$CI_GATE"
```
