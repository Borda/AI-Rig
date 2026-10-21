<!-- oss:resolve Step 8 phase1 — executed via: cat $_OSS_RESOLVE/modes/dispatch-phase1.md; execute -->

<!-- fragment — no <workflow> wrapper; executed inline by SKILL.md -->

<!-- Input: prelude state (SELECTED_ITEMS, IMPL_DIR, C1 split); loaded only when NO_CHALLENGE != true -->

<!-- Output: CHALLENGE_LOG populated via the prelude append block; SURVIVING_ITEMS; codemap maps (Structural prep) -->

### Phase 1: Challenge — parallel by domain (skip when `--no-challenge`)

Route by domain to foreground challenge agent:

| Item domain | Challenger |
| -- | -- |
| Architecture, API design, coupling | `foundry:challenger` |
| Code logic, correctness, edge cases | `foundry:sw-engineer` |
| Test coverage, assertions, regressions | `foundry:qa-specialist` |
| Default / unclassified | `foundry:challenger` |

**Early and late waves.** Phase 1 runs in two passes under the same rules. The **early wave** fires inside the Run 2 fan (SKILL.md Step 7b join), in the same response as the Step 7a conflict agent, over every selected item that is not a C1 candidate (see the effort classification). The **late wave** runs after the join and C1, over every C1 candidate C1 did not complete (UNCERTAIN, dirty-tree fall-through, a second medium item in a non-`each` mode); same chunking, prompt, verdict processing and log append, `<domain>` slugs suffixed `-late` so no filename collides. An empty set skips its wave; Phase 2 starts after the late wave.

Set `DOMAIN_CHALLENGER` from routing table: architecture/API/coupling/default → `foundry:challenger`; code logic/correctness/edge-cases → `foundry:sw-engineer`; test coverage/assertions/regressions → `foundry:qa-specialist`. Use agent-resolution.md fallback if foundry absent.

Group items by `DOMAIN_CHALLENGER`, preserving each item's original priority-order position within its group (stable partition — needed later so Phase 3's merge plan also respects each specialist's internal commit order).

- **Chunk each domain group at `CHALLENGE_CHUNK=12` items.** Per-item budget below is 4 tool calls, so 12 items ≈ 48 calls — inside the ~55–60 stall bound. A domain of `n_d` items splits into at least `ceil(n_d/12)` chunks of balanced size (sizes differ by at most one where file affinity allows), one more whenever whole-file packing would push a chunk past 12 (three files of 7 items → 3 chunks, not 2). No chunk ever holds more than 12 items.
  - Keep every file's items in one chunk while that file holds ≤12 items — shared file reads amortize, and verdicts on one file stay consistent. Fill chunks by whole files in priority order of each file's first item.
  - One file with more than 12 items → that file's items alone, priority order, cut into `ceil(n/12)` ordered chunks of ≤12 (full chunks first, remainder last) — the same cut as a Phase 2 chained group. Unlike a Phase 2 chain they fire concurrently with every other chunk: Phase 1 only reads, so no lineage exists to serialize. Verdicts split across those chunks still converge — Phase 2's file-ownership tiebreak hands every surviving item of that file to one specialist, whose links apply them in priority order.
  - Preserve priority order inside each chunk.
- One combined challenge call per chunk, covering ALL that chunk's items.
- Derive `<domain>` per chunk as a short kebab-case slug from the group's shared theme (e.g. `logic`, `tests`, `docs-api`); a domain split into several chunks appends `-<k>` (`logic-1`, `logic-2`). The slug is the delta between chunks, reused as `name="challenge-<domain>"`, as the prompt lead, and as the output filename suffix — every `<domain>` below means this per-chunk slug.
- `description` = 3–5 words naming that group's theme, never echoing `name` or the shared PR.
- Compose every group's labels in one pass and confirm the prompt leads differ in their first word — FleetView prints `name` plus the leading chars of prompt line 1, so a shared prefix there yields indistinguishable rows (task-lifecycle.md §Spawn slots):

Before building item lines, re-check every carried verifier verdict and parent-confirmation binding once against the source this wave reads. Choose the existing block by wave; do not run both for one wave:

| Wave | Freshness block and source |
| -- | -- |
| Early wave | Run the pinned PR-head block below; challengers read that same revision with `git show <PR_HEAD_OID>:<file>` while the merge is unfinished. |
| Late wave | Re-run the existing **C1 evidence freshness** block (`dispatch-prelude.md`), even when no C1 item ran; it uses checkout HEAD, which late challengers read after the join and any C1 commits. Do not reuse the original PR-head sentinel for this wave. |

The review confirmed findings at its recorded head. The selected block drops independent verifier and parent-confirmation shortcuts when the wave's source no longer matches, retaining evidence pointers for changed-gap investigation. Report mode without a PR uses checkout HEAD. The following block is for the early wave only:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r IMPL_DIR < "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" 2>/dev/null || IMPL_DIR=""
IFS= read -r REPORT_FILE < "${TMPDIR:-/tmp}/resolve-report-file-${CSID}" 2>/dev/null || REPORT_FILE=""
IFS= read -r PR_HEAD_OID < "${TMPDIR:-/tmp}/resolve-pr-head-oid-${CSID}" 2>/dev/null || PR_HEAD_OID=""
IFS= read -r PR_NUMBER < "${TMPDIR:-/tmp}/resolve-pr-number-${CSID}" 2>/dev/null || PR_NUMBER=""
[ -n "$IMPL_DIR" ] || { echo "! BLOCKED — IMPL_DIR sentinel missing"; exit 1; }
# no-PR report mode never runs Step 4, so a PR head oid left from an earlier run must not be compared
case "$PR_NUMBER" in ''|n/a|*[!0-9]*) PR_HEAD_OID="" ;; esac
if [ -f "$REPORT_FILE" ]; then
    python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/merge_action_items.py" --items "$IMPL_DIR/action-items.jsonl" --recheck-verdicts --review-dir "$(dirname "$REPORT_FILE")" --current-head "$PR_HEAD_OID" || exit 1  # timeout: 15000
else
    echo "→ no review report consumed — no verdicts to re-check"
fi
```

Build each item line from its `action-items.jsonl` record (Step 3c merge carries the reviewer's evidence through), read after the re-check above: append `[confirmed verify=<verify_file>]` only when `verify_verdict` is `CONFIRMED`, `[evidence=<source_file>]` when `source_file` is set, and `[thin]` when `thin` is true. Append `[parent-confirmed candidate=<candidate_id> validation=<parent_validation_file>]` only when `validation_status=CONFIRMED` and that pointer survived the re-check. A retained pointer without that status is historical evidence only. Parent confirmation covers only the linked review finding, never any additional GitHub request. Omit a marker whose field is absent. `[confirmed]` covers the review finding the item was built from; GitHub items never carry it.

```text
Agent(subagent_type="${DOMAIN_CHALLENGER}", name="challenge-<domain>", description="<3-5 words: this chunk's domain scope>", prompt="<domain>: two-part challenge for these review items.
Spotter evidence first — an item listing evidence=/verify= paths carries the reviewer's own reasoning; read those files before the code, then confirm against the code.
Part 1 — does the stated problem exist in the code as described? For [parent-confirmed], read the exact candidate in validation= and its checks; recheck source, contract and environment applicability, then reuse applicable proved checks without rerunning them. Still matching → evidence=VALID citing candidate and retained check. Stale or changed claim → investigate only that gap. For a GitHub item, apply retained proof only to the linked review finding and assess any distinct request separately. This parent proof never supplies the independent [confirmed] verifier marker. Other items WITHOUT [confirmed] require the full existence check.
The reviewer's assertion is itself an unproven claim, not evidence — 'reads like X' != 'is X'.
When a finding asserts a fact reading the referenced file alone can't settle (a name/identifier/version/count is wrong, non-standard, or inconsistent — license names, API/symbol names, version numbers, spec IDs), verify it via WebFetch/WebSearch against the actual authoritative source for that claim (the specific project/library/spec it names — not a generic registry) before ruling VALID. First inspect retained authoritative evidence and its revision/contract/environment binding. Still applicable → VALID despite failed fresh retrieval. Stale/inapplicable evidence and unavailable retrieval → UNVERIFIED with missing_capability, owner, next_check and closure; never REJECT because a source is unreachable/inconclusive. REJECT requires affirmative falsification. A later evidenced fix → ALREADY_FIXED with original/current revision and fix evidence.
[confirmed] items were already confirmed by an independent review-time verifier (its file is the verify= path). Do not re-prove them: only re-read <file:line> and check the code still matches the finding (the head may have moved). Still matching → evidence=VALID, rationale citing the verifier. No longer matching → run the full Part 1.
[thin] items are terse GitHub comments that no review finding covers: first locate the code they refer to (PR diff, named symbols). A request about docs, changelog, tests or process has no single code target: judge the request itself. A code request whose target stays unidentifiable → evidence=UNVERIFIED with owner/next_check/closure, never a fabricated refutation.
Part 2 — for EVERY item whose problem exists, is the suggested fix the right approach? Rejecting the repair never refutes the defect. Feasible but unperformed checks remain incomplete; the parent must finish authorized checks before implementation or record an actual unavailable boundary.
Read each referenced file at <file:line>. Read-only: run no tests. Max 4 tool calls per item (the 4th reserved for one WebFetch/WebSearch when a claim needs external verification), plus one read per evidence/verify file.
Early wave only: read the pinned PR revision whether or not there are file conflicts. Read every file with `git show <PR_HEAD_OID>:<file>` (substitute the sha from the `resolve-pr-head-oid` sentinel), never from disk — disk may hold conflict markers, clean-merged drift or an edit in progress, and `HEAD` itself moves at the merge commit.
Items:
<id>: <full_comment_text> (<file>:<line>) [confirmed verify=<verify_file>] [evidence=<source_file>] [parent-confirmed candidate=<candidate_id> validation=<parent_validation_file>] [thin]
...
Write full analysis to $IMPL_DIR/challenge-domain-<domain>.md using the Write tool.
Return ONLY compact JSON as your FINAL message (nothing after it):
{\"items\":[{\"id\":N,\"evidence\":\"VALID\"|\"REJECT\"|\"UNVERIFIED\"|\"ALREADY_FIXED\",\"evidence_rationale\":\"<one sentence>\",\"suggestion\":\"VALID\"|\"REJECT\",\"suggestion_rationale\":\"<one sentence>\",\"alternative\":\"<brief alternative or null>\",\"missing_capability\":\"<when unverified>\",\"owner\":\"<when unverified>\",\"next_check\":\"<when unverified>\",\"closure\":\"<observable closure when unverified>\",\"original_revision\":\"<when already fixed>\",\"current_revision\":\"<when already fixed>\",\"fix_evidence\":\"<evidenced change when already fixed>\"}]}")
```

**Fire every chunk's `Agent()` call in the same response turn** — read-only (no working-tree writes), safe to run concurrently regardless of file overlap between chunks. In the early wave the Step 7a conflict agent (`foundry:sw-engineer`) sits in that same response and holds one `CAP_OPUS` slot. More chunks than a tier's pool (`claude-config.md` §Parallel Spawn Ceilings: `CAP_OPUS=5` for `foundry:challenger`/`foundry:sw-engineer`, `CAP_SONNET=8` for `foundry:qa-specialist`) → fire the first wave up to each pool, the next wave as earlier chunks return. In that same response, arm the deadlines (SKILL.md §Agent wait discipline): write `$IMPL_DIR/agent-watch-challenge.tsv` with one row per fired chunk, `challenge-<domain><TAB><IMPL_DIR>/challenge-domain-<domain>.md<TAB>300` (`CHALLENGE_TIMEOUT_S`); a later wave rewrites the file with its own rows. Never poll for verdicts — no `ScheduleWakeup`, `ListAgents` or `Monitor` loop; run the watch check at each wake-up. A chunk `timed_out`, or one whose notification arrived without its JSON reply → ⏱ now, and every item in it is treated `UNCERTAIN` per the verdict rules below (one single-item retry each, armed in `agent-watch-challenge-retry.tsv`). **No item is ever dropped or implemented by a timeout alone** — the first retry is automatic; what happens after a second timeout is the user's decision.

**Challenge double-timeout gate** — fires only when a single-item retry also times out (a missing verdict blocked the run before this gate existed; the decision now goes to the user instead). Collect **every** item of this wave whose retry timed out, then ask **once** for all of them — one wait, never one per item. The items go in the question text itself, one line each (`#<id> · <domain> · <summary>`), never as reply text before the call: 5.5-family models may return reply text written before a tool call as an empty progress update, leaving the user to decide on bare ids. The gate is human idle, so the question text closes with the `/compact` hint line. Then invoke `AskUserQuestion` (actual tool call):

```text
"The challenge for <N> item(s) timed out twice: <ids>. What should happen to them?
  <for each item: #<id> · <domain> · <summary ≤60 chars>, one per line>
Long wait? `/compact` now — state persisted in <IMPL_DIR>, resume lossless."
  (a) Implement unchallenged — ⏱ noted in the Challenge Log and the final report
  (b) Drop them — record as skipped, not implemented
  (c) Retry the challenge once more
  (d) Stop the run before implementation
```

- (a) → for each id, write `$IMPL_DIR/challenge-verdicts-<domain>-retry-<id>.json` with the Write tool as `{"items":[{"id":<id>,"evidence":"VALID","evidence_rationale":"⏱ challenge timed out twice — implemented unchallenged (user choice)","suggestion":"VALID","suggestion_rationale":"⏱ challenge timed out — fix not evaluated","alternative":null}]}`, then run the shared append block with one `<id> as-suggested <domain>` line per id — implemented as with `--no-challenge`, ⏱ visible in the Challenge Log and the Step 11 report.
- (b) → run the drop block below with the chosen ids and exclude them from `SURVIVING_ITEMS`; Phase 3's skipped-item close-out and the Step 11 report then show them as skipped.
- (c) → one more single-item retry each (re-armed in `agent-watch-challenge-retry.tsv`); items that time out again come back to this same gate.
- (d) or unanswered → stop: spawn nothing, run the stop-path release fence in `dispatch-prelude.md` to release the branch mutex, report every selected item as pending, jump to Step 11. Never a silent default. In the early wave Step 7a may still be running: collect it and complete 7b (commit the merge) first, or Step 11 reports `merge uncommitted — finish with git commit, or git merge --abort`.

Drop block — (b) only; the ids are runtime values, so the block aborts unsubstituted:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
[ -f "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" ] && IFS= read -r IMPL_DIR < "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" || IMPL_DIR=""
[ -n "$IMPL_DIR" ] || { echo "! BLOCKED — IMPL_DIR sentinel missing; prelude never ran"; exit 1; }
_DROP_IDS="<space-separated item ids the user chose to drop>"
case "$_DROP_IDS" in *'<'*'>'*|"") echo "! BLOCKED — drop ids not substituted"; exit 1 ;; esac
for _id in $(printf '%s\n' "$_DROP_IDS"); do  # cmd-substitution splits under zsh too, a bare $VAR does not
    case "$_id" in *[!0-9]*) echo "! BLOCKED — drop id '$_id' is not numeric"; exit 1 ;; esac
    printf '%s\tchallenge timed out twice — dropped by user\n' "$_id" >> "$IMPL_DIR/skipped-items.txt"
done
echo "dropped: $_DROP_IDS"  # timeout: 3000
```

Immediately after each call returns, persist its raw JSON reply verbatim via the Write tool to `$IMPL_DIR/challenge-verdicts-<domain>.json` (same `<domain>` slug as the spawn) — the verdict-processing steps below and the challenge-log append (`dispatch-prelude.md`) both read it via `jq`, never by re-typing the reply's rationale/alternative text.

**Structural prep — fire in this same turn, concurrently with the challenge agents** (the codemap queries below are read-only and depend only on item *files*, known from Step 3b — not on any challenge verdict — so they run under the challenge agents' latency shadow, adding ~0 wall-clock; grouping in Phase 2 then finds its maps already warm). Keyed off all `SELECTED_ITEMS` (not yet-unknown `SURVIVING_ITEMS`) — a few queries for items challenge later drops are cheap and hidden under the agent latency; Phase 2 filters to survivors. Resolve each file to its canonical module name + build the whole-repo centrality map (`resolve_centrality.py`), then capture each module's **forward imports** (`deps`, fan-*out*, naturally small — never the 20-cap that truncates reverse `rdeps`):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
[ -f "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" ] && IFS= read -r IMPL_DIR < "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" || IMPL_DIR=""
[ -f "$IMPL_DIR/selected-items.txt" ] && IFS= read -r SELECTED_ITEMS < "$IMPL_DIR/selected-items.txt" || SELECTED_ITEMS=""
CODEMAP_MAPS="$IMPL_DIR/codemap-maps.json"
DEPS_MAP="$IMPL_DIR/codemap-deps.jsonl"; : > "$DEPS_MAP"
if command -v codemap-py >/dev/null 2>&1 && [ -f "$IMPL_DIR/action-items.jsonl" ]; then
    _FILES=$(for _id in $(printf '%s\n' "$SELECTED_ITEMS"); do  # cmd-substitution splits in both shells — bare `$VAR` is a silent 1-iteration no-op under zsh
        jq -b -r "select(.id == $_id) | .file // empty" "$IMPL_DIR/action-items.jsonl"
    done | paste -sd, -)  # timeout: 5000
    _ROOT=$(git rev-parse --show-toplevel 2>/dev/null); [ -n "$_ROOT" ] || _ROOT="$PWD"
    _IDX_FILE="${CODEMAP_INDEX_DIR:-$_ROOT/.cache/codemap}/$(basename "$_ROOT").json"
    _MAP_STAMP=$(python -c 'import pathlib,sys; p=pathlib.Path(sys.argv[1]); s=p.stat(); sys.stdout.write(f"{p.resolve().as_posix()}:{s.st_size}:{s.st_mtime_ns}")' "$_IDX_FILE" 2>/dev/null)
    _SAVED_STAMP=""
    [ -f "$IMPL_DIR/codemap-maps.stamp" ] && IFS= read -r _SAVED_STAMP < "$IMPL_DIR/codemap-maps.stamp"
    if [ -z "$_MAP_STAMP" ] || [ "$_MAP_STAMP" != "$_SAVED_STAMP" ] || ! python -c 'import json,sys; d=json.load(open(sys.argv[1])); sys.exit(not set(filter(None,sys.argv[2].split(","))).issubset(d["file_module"]))' "$CODEMAP_MAPS" "$_FILES" 2>/dev/null; then
        codemap-py query central --top 100000 2>/dev/null \
            | python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/resolve_centrality.py" --files "$_FILES" > "$CODEMAP_MAPS" 2>/dev/null \
            || : > "$CODEMAP_MAPS"
        printf '%s\n' "$_MAP_STAMP" > "$IMPL_DIR/codemap-maps.stamp"
    fi
    if [ -s "$CODEMAP_MAPS" ]; then
        for _m in $(python -c 'import json,sys; sys.stdout.write(" ".join(sorted({v for v in json.load(open(sys.argv[1]))["file_module"].values() if v})))' "$CODEMAP_MAPS"); do
            codemap-py query deps "$_m" 2>/dev/null \
                | python -c 'import json,sys; d=json.load(sys.stdin); print(json.dumps({d["module"]: d.get("direct_imports", [])}))' >> "$DEPS_MAP"  # timeout: 5000
        done
    fi
fi
```

Parse each group's per-item verdict array — same granularity as a single-item challenge, never relaxed by grouping:

- Missing item id, or a present element with empty/null `evidence_rationale` or `suggestion_rationale` → treat as UNCERTAIN. Re-dispatch it alone (single-item challenge call, same domain); persist that reply via the Write tool to a **separate** file, `$IMPL_DIR/challenge-verdicts-<domain>-retry-<id>.json` — never overwrite the group's own `challenge-verdicts-<domain>.json`, which still holds every sibling item's verdict this pass hasn't appended yet. The retry prompt carries the item's `evidence=`/`verify=` markers like the group call. The shared append block (`dispatch-prelude.md`) prefers the retry file for that id when present, and its `// "challenge agent returned no rationale after retry"` fallback covers the still-empty case exactly once, after the real retry — never before it.
- **Ask the spotter** — the retry is still UNCERTAIN (empty rationale again) and the item carries a `finding_id` and its `author` names a real agent type (not an `@login`, not `codex`; on a GitHub item annotated by the merge, the agent type after `+`): dispatch that agent type for this item alone, as `name="caucus-<id>"`, with the same two-part prompt and JSON contract plus its `evidence=` file. This is the reviewer role that raised the finding, consulted on its own claim — a domain challenger guessing twice adds no signal. Persist the reply via the Write tool to `$IMPL_DIR/challenge-verdicts-<domain>-caucus-<id>.json`; arm `agent-watch-challenge-caucus.tsv` with a 300 s row. A caucus `evidence=VALID` counts only when its rationale cites the code it read (`file:line`); a bare restatement of the original claim falls to the no-rationale default, because the author agreeing with itself is not verification. After that bounded retry/caucus, missing rationale remains incomplete: block that item with owner/recovery and leave it pending, never default VALID or REJECT. One caucus per item, never more.
- `evidence=UNVERIFIED` → list `<id> unverified <domain>`; exclude from SURVIVING_ITEMS, retain task pending, and carry owner/next_check/closure into the challenge log, resolution.jsonl and final Open Concerns. Do not implement or delete the obligation. Missing reviewer coverage, provenance limits, unavailable checks, confidence gaps, or a declined optional extra-review request remain process/evidence limits; never turn them into source action items or repeat a dismissed review request.
- `evidence=ALREADY_FIXED` → list `<id> already-fixed <domain>`; exclude from implementation, retain original/current identities and fix evidence, report separately from refutation. After the validated append block prints its task id, call `TaskUpdate(status="completed")`; no implementation commit is required for this evidenced historical closure.
- `evidence=REJECT` → print `⊘ #<id> evidence rejected: <reason from the persisted verdict file>`; set type `[challenged:reject]`; list `<id> rejected <domain>` for the shared append block (`dispatch-prelude.md`); drop from `SURVIVING_ITEMS`. The append block prints the task id to dispose (or explains why none exists in report mode); call `TaskUpdate(status="deleted")` on it.
- `evidence=VALID` + `suggestion=VALID` → list `<id> as-suggested <domain>`; use original suggestion for implementation
- `evidence=VALID` + `suggestion=REJECT` → list `<id> self-resolved <domain>`; self-resolve using `alternative` as guidance
- Batch the lines: one `challenge-log-now.txt` holding every settled item of a reply, then one run of the append block — never one run per item.

Items with `evidence=VALID` (appended via the shared append block as `as-suggested` or `self-resolved`) form `SURVIVING_ITEMS`. UNVERIFIED obligations remain pending with owner/next check; ALREADY_FIXED is historical closure, not refutation. Both remain visible in the final report and resolution ledger. Unknown statuses block, never default to REJECT or implementation.
