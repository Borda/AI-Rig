<!-- oss:resolve Step 3d — executed inline: cat $_OSS_RESOLVE/modes/item-selection.md; execute -->

<!-- fragment — no <workflow> wrapper; executed inline by SKILL.md orchestrator -->

<!-- consumer: plugins/cc_oss/skills/resolve/SKILL.md (Step 3d) -->

## Step 3d: User item selection

<!-- branch: main-path — item-selection (always fires in step 3d; 1 item = two calls: bulk + commit-mode + dispatch (no item question), then push + topic-group-when-grouped follow-up; 2-3 items = two calls: bulk + items + commit-mode + dispatch, then push + topic-group-when-grouped follow-up; 4-6 and 7-9 = two calls: bulk + balanced checkboxes (2-3 items each), then commit-mode + topic-group + dispatch + push follow-up; 10-18 = three: two pages of bulk + balanced checkboxes (Call 2 never a 1-option question) + the same follow-up; every selection call leads with bulk; ≥19 = two: bulk + commit-mode + topic-group + dispatch, then push follow-up; zero pending with closed items = bulk + commit-mode + topic-group + dispatch, then push; zero pending without closed items and with a PR = one push-only call; push question omitted when no PR number exists) -->

! IMPORTANT — invoke `AskUserQuestion` tool directly. Never write options as plain text.

Pending items = ACTION_ITEMS where `status` is `pending` or absent and type contains neither `[info]` nor legacy `[done]`. Closed items = `status` `resolved` or `addressed`: they stay in the table but never enter checkboxes, bulk options, or the pending count — the user pulls one in only by typing its id (see "Type something" below). With ≥1 closed item, the bulk question's text says so in every selection call (**Bulk question text** below) — never as reply text before the picker: 5.5-family models may return it as an empty progress update, and in the closed-only branch this hint is the only usage instruction the user gets.

- **Zero pending, closed items present** → follow the dedicated slot-table row below: no item checkboxes; ask the existing bulk menu, commit-mode, topic-group, and dispatch questions. The bulk menu's "Type something" field accepts explicit closed IDs in PR and report modes alike. Name the closed-item count in Q1; save the full table before the call and print the table once as the last text of the reply message that issues Step 3d’s AskUserQuestion. Bulk sets hold no closed IDs, so (a)/(b)/(c) select only the typed IDs; typed IDs join any answer and (d) yields to them, while (d) with nothing typed stops as usual. Typed IDs follow the ordinary bulk-action resolution and commit-mode rules; then ask the push follow-up when a PR number exists — without a PR number there is no follow-up. If no IDs were selected, discard commit/group/dispatch answers and continue with the empty selection after the push follow-up.

- **Zero pending, no closed items** → set `SELECTED_ITEMS` empty; when a PR number exists, ask the **Push question** below alone — the Steps 5–7 merge commit still needs push intent. Then continue to Step 3e for `pr`/`pr+report`, or skip Step 3e in `report` mode. This bypass never applies to a list containing resolved/addressed items.

Sort all pending items by severity descending (most impactful first).

**Single stream — no agent beside this gate.** Dispatch nothing here: the conflicted files (Step 5) wait, still staged and unresolved, and Steps 6–7a fire right after the answers, in the Run 2 fan (**Step 7b join**). The picker carries the plain pending-item list (**Items in the question text** below), which the hook checks, so it is the only thing in flight.

Longest idle window of the run sits here (median ~15 min, measured up to 16 h) — long enough for the prompt cache to expire, so the next turn rewrites the whole context at write rate. Persist a resume contract first, then carry the hint in the gate's question text so the user can `/compact` while waiting (skill can't trigger compaction itself):

```bash
# compaction boundary 0 — before the Step 3d idle gate (compaction-contract.md §Lifecycle)
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _IMPL_DIR < "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}" 2>/dev/null || _IMPL_DIR=""
IFS= read -r _PR_NUMBER < "${TMPDIR:-/tmp}/resolve-pr-number-${CSID}" 2>/dev/null || _PR_NUMBER="n/a"
_PRESERVE="pr=$_PR_NUMBER, impl-dir=$_IMPL_DIR, intel=$_IMPL_DIR/pr-intelligence.md, items=$_IMPL_DIR/action-items.jsonl, vars=$_IMPL_DIR/pr-vars.sh"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/write_skill_contract.py" --keep-sentinel resolve-keep-items "oss:resolve" "item selection (Step 3d gate)" "$_IMPL_DIR" "$_PRESERVE" "resume: re-read action-items.jsonl + pr-intelligence.md, re-issue Step 3d AskUserQuestion; Steps 6-7a not yet dispatched — Step 5 left the merge staged, resolve it in the Run 2 fan (Step 7b join)"  # timeout: 5000
```

The hint line `` Long wait? `/compact` now — state persisted in <IMPL_DIR>, resume lossless. `` goes into the `question` text of Step 3d's first question — Call 1's bulk question (**Bulk question text** below), or the push question on the push-only path. Never as reply text, never Bash stdout: 5.5-family models may return reply text written before a tool call as an empty progress update, and tool output is not reliably shown to the user.

**Show the table exactly once per selection call — before the picker**: Q1 lists the items in question text. Write the whole headed ACTION_ITEMS table to `$IMPL_DIR/action-items-table.md` with the Write tool, before the call; refresh it for every selection call so it matches the rows being offered. Include every pending row plus closed rows, headed by the Sources block, contribution motivation where applicable, and merge summary when merged. Render the table as a normal Markdown table, outside code fences. Print the table once as the last text of the reply message that issues Step 3d’s AskUserQuestion, never in an option’s `preview`, never Bash/tool stdout. A resumed or second selection call lists the items again in Q1 and prints its saved table before that call. Do not print it again after the answer. A denied call re-issues without a table reprint; correct the question text and call again.

**Items in the question text — MANDATORY, the gate's source of truth**: every selection call (Call 1, Call 2, closed-only and ≥19) puts the single-select bulk question first. Its `question` text lists every pending item, most severe first, one plain line `#<id> [<type>] <summary up to 50 chars>`; use the stored type codes (e.g. `[gh][req]`) and collapse summary newlines and pipe characters to spaces. Closed items are named by count only unless their lines fit after the pending list and fixed notices. Options carry labels and one-line descriptions saying what each does with these items, with **no `preview` on any option**. The full table remains in the saved file and the pre-picker reply. **Hook-enforced**: `hooks/enforce-resolve-table.js` checks Q1's pending ids or the over-budget form below, a 1,500-character whole-question limit, and optional comparisons: identical previews are denied; a present preview must be a plain comparison with no table or HTML within the host preview cap (2000 characters, 12 rendered lines, wrapping at 86 characters). Options and reply text before the call never supply the item list. On denial, correct Q1's question text and issue the corrected call; never re-send the unchanged call.

**Question budget — MANDATORY**: the whole `question` text, including all fixed notices and the real table path, is at most **1,500 characters**. Reserve those fixed lines first. If every pending line fits, include them all. Otherwise list the highest-severity pending lines that fit, followed by `Pending: <type> <N> · <type> <N>` with exact counts for every pending type, then the remaining fixed lines; always retain at least the top item and the table path. Do not use a table, pipe layout, HTML or a fence in the question. This overflow decision uses text length, independently of the ≥19 context-budget threshold; never hide an item in a file when the whole pending list fits. If even one item plus the required notices/path cannot fit, stop and report the question-budget conflict instead of omitting required context.

**Cap mechanics — read before building any call**: the tool cap is **4 questions per call**. The `Submit` tab is NOT a question — a 4-question call renders 5 tabs. Never stop at 3 questions believing the cap is reached, and never over-pack a question past 3 items to avoid opening a 4th. Within one question, `AskUserQuestion` appends "Type something" outside the option list, so 3 items + Type something = 4 visible rows; that is the **≤3 items/question** limit, a separate constraint from the 4-question cap.

**Call layout — literal slot template, pick by pending-item count** (each AskUserQuestion window is pure human idle, median ~15 min — merge whenever the 4-question cap allows):

| Pending | Call 1 slots | Follow-up call |
| -- | -- | -- |
| 0, closed items present | Q1 bulk · Q2 commit-mode · Q3 topic-group · Q4 dispatch | Q1 push, only when a PR number exists |
| 0, no closed items | Q1 push, only when a PR number exists; otherwise no call | None |
| 1 | Q1 bulk · Q2 commit-mode · Q3 dispatch — no item question | Q1 push · Q2 topic-group, only when commit mode = (b) |
| 2-3 | Q1 bulk · Q2 items · Q3 commit-mode · Q4 dispatch | Q1 push · Q2 topic-group, only when commit mode = (b) |
| 4-6 | Q1 bulk · Q2-Q3 items (2-3 each, balanced) | Q1 commit-mode · Q2 topic-group · Q3 dispatch · Q4 push |
| 7-9 | Q1 bulk · Q2-Q4 items (2-3 each, balanced) | Q1 commit-mode · Q2 topic-group · Q3 dispatch · Q4 push |
| 10-18 | Q1 bulk · Q2-Q4 items (first 3 questions of the balanced split, 8-9 items) → Call 2: Q1 bulk · Q2-Q4 items (remaining questions, 2-3 each) | Q1 commit-mode · Q2 topic-group · Q3 dispatch · Q4 push |
| ≥19 | context-budget mode below — no item checkboxes exist | Q1 push, only when a PR number exists |

Checkbox mode holds at most 18 items (2 calls × 3 questions × 3 items). Decide the mode from the pending count **before** building Call 1; never widen a question past 3 items and never open a Call 3 to stretch checkbox mode further.

The two explicit zero-pending rows take precedence over the 1 and 2-3 rows. Item checkboxes start at 2 pending items: `AskUserQuestion` rejects a question with fewer than 2 options, so a lone pending item gets no item question — the bulk action selects it, or "Type something" with its id — and every item question holds 2–3 options (**Item checkbox questions** below). The dispatch question is asked in **every** run with selectable pending or closed items and always shares a call with the commit-mode question, so the user sets how to commit and how to parallelize together. The 2-3 row spends its last slot on it, and topic-group moves to the follow-up, asked there only when commit mode = (b). The 4–18 pending-item bands ask commit-mode, topic-group, dispatch, and push together in one follow-up call; the topic-group answer is discarded unless commit mode = (b).

For lists with pending or resolved/addressed items, the push question rides a follow-up call; zero pending with no closed items instead uses its push-only first call. Run 2 never stops to ask about pushing. When no PR number exists (`report` mode without a PR# in its header), Step 10 never runs: omit the push question, and the 1 and 2-3 follow-up then fires only when commit mode = (b).

Bulk action resolving to (d) Skip all → discard the commit-mode, topic-group, **and dispatch** answers from the same call and issue no follow-up call (nothing will be committed, no specialist will be dispatched, and the run jumps to Step 11 without pushing). This satisfies the distinct-menus rule below — menus stay separate questions; only the round-trips merge.

**Bulk action — hard rule**: single-select, fixed options, **present in every selection call without exception** — Call 1 and Call 2 alike, always Q1, before any item-checkbox question: its question text lists the pending items, so the items are visible on the first screen. A selection call without a bulk page, or with it behind the checkboxes, is a defect, never a valid compression. Never put items in it as options; items go in the question text, not in `preview`. Items span ≤3 groups per call regardless of how many type categories exist.

```text
Bulk-action question — multiSelect: FALSE (single-select only — user picks one bulk action, not a checklist)
"<pending item lines and fixed notices below>"
  (a) +All [req] — queue the N required items
  (b) +All [suggest] — queue the N suggested items
  (c) ALL (req + suggest) — queue all N pending items
  (d) Skip all — skip all items, exit
```

**Bulk question text** — use only these fixed lines in this order, each notice at most one short sentence; the model adds no other lines. These lines belong only in the question text:

- Pending item lines `#<id> [<type>] <summary up to 50 chars>`, most severe first; past the whole-question budget, the top lines that fit followed by `Pending: <type> <N> · <type> <N>`.

1. `→ To pick items yourself, leave this unanswered and tick them on the next tabs.` — only with item-checkbox questions; (a)–(c) add checked/typed IDs and (d) yields to them, as **Bulk-action resolution** defines.
2. `→ N pending items — selecting in 2 calls; a bulk choice here ends selection after this call` — Call 1 of the two-call layout.
3. `→ N resolved/addressed items not in bulk options — type their ids to include` — when closed items exist; optional closed-item lines may follow the pending lines only if the whole text still fits.
4. `→ N pending items — selecting more than 50 runs them all in this pass: a long run` — the ≥19 call with more than 50 pending; challenge stays at ≤12 items/agent and implementation uses waves.
5. `` Long wait? `/compact` now — state persisted in <IMPL_DIR>, resume lossless. `` — Call 1 only.
6. `→ Full item table: <IMPL_DIR>/action-items-table.md` — every selection call, using the real path within the 1,500-character whole-question budget.

**ESSENTIAL — exactly these 4 options, verbatim, never substitute and never add** (empirically motivated: an observed run emitted an invented `Use my checked picks (Recommended)` option and dropped `+All [suggest]`). The checked-picks path needs no option — it is the "unanswered" branch below. Every selection call carries this menu; a call that omits it must be re-issued. Each option has its label and a one-line description with the applicable item count, and no `preview` field. Keep the recommended option second under the recommended-second convention; preserve the (a)–(d) answer mappings and the four labels.

**Bulk-action resolution** — a bulk answer never discards an explicit pick: (a)–(c) take the union of their bulk set, every checked ID from the item questions, and every item id typed in any "Type something" field; (d) yields to any checked or typed ID. The pick-yourself hint accompanies every call with item checkboxes:

- (a) → `SELECTED_ITEMS` = all pending `[req]` IDs ∪ checked IDs ∪ typed IDs; skip Call 2 in two-call flow; proceed to commit-mode resolution
- (b) → `SELECTED_ITEMS` = all pending `[suggest]` IDs ∪ checked IDs ∪ typed IDs; skip Call 2 in two-call flow; proceed to commit-mode resolution
- (c) → `SELECTED_ITEMS` = all pending [req+suggest] IDs ∪ typed IDs (every checked ID is already pending); skip Call 2; proceed to commit-mode resolution (do NOT hardcode `COMMIT_MODE` — scope and commit mode are orthogonal; user still chooses granularity)
- (d) with no ID checked or typed → stop; print `→ All items skipped.`; jump to Step 11 (merged flow: discard the commit-mode answer from the same call). (d) with any checked or typed ID → resolve exactly as unanswered below: the explicit picks win.
- unanswered / "Type something" → use checked IDs from the item questions, plus every item id typed in any "Type something" field; proceed to commit-mode resolution; `COMMIT_MODE = each` (default)
- Typed IDs are the only way a closed (`resolved`/`addressed`) item is selected, under every answer above; typed ids naming `[info]`, legacy `[done]`, or unknown items are dropped with a one-line note.

**Item checkbox questions**: each `multiSelect: true`, header "Items to implement:", labels: `<type> #<id>: <summary>` (≤55 chars), description: `@<author>` + for `location: discussion` items append `· thread (no GH resolve)` — no `file:line`: the location is already in `action-items.jsonl` and just takes up the option's space. Fill in severity order. **Every item question holds 2–3 options — never 1, never 4**: `AskUserQuestion` rejects a question with fewer than 2 options, and 3 items + "Type something" is the row limit. Split n pending items (2–18) into `q = ceil(n/3)` questions as evenly as possible: the first `n mod q` questions take `n//q + 1` items, the rest `n//q` (4 → 2+2, 5 → 3+2, 7 → 3+2+2, 10 → 3+3+2+2, 13 → 3+3+3+2+2, 16 → 3+3+3+3+2+2). Never fill greedily to 3 and leave a 1-item remainder. >9 pending items: two calls, announced by item 2 of Call 1's **Bulk question text** above; build each call from the slot table above:

- **Call 1** = Q1 bulk action + Q2-Q4 item checkboxes (the first three questions of the balanced split — 8 or 9 items).
- **Call 2** = Q1 bulk action + Q2-Q4 item checkboxes (the split's remaining questions, 2–3 items each — never a 1-option question) — the bulk menu repeats here, it is not carried over from Call 1, and so does its pending-item list in Q1’s question text, with no option previews.
- A Call 1 bulk answer that resolves to (a)–(c), or to (d) with nothing checked or typed, → skip Call 2 entirely (scope already resolved; Call 1's line 2 told the user). Unanswered, or (d) yielding to picks → Call 2 as usual.
- ≥19 pending → context-budget mode below instead, decided before Call 1; never open a Call 3.

**≥19 pending items — context-budget mode**: no per-item checkboxes in this branch. **MANDATORY, in this order — render first, ask second; Q1 carries the item list in question text:** (1) render the compressed table (type · id · summary ≤40 chars · file) with every row in `$IMPL_DIR/action-items-table.md` per **Question budget**, before the call; print the table exactly once as the last text of the reply message that issues Step 3d’s AskUserQuestion, never in an option’s `preview`, never Bash/tool stdout; same non-decorative/no-compression-substitute rule as Step 3c (Output-Routing exemption applies — never divert to `.temp`); (2) then issue ONE call: Q1 bulk action · Q2 commit-mode · Q3 topic-group · Q4 dispatch (all four slots; no item checkboxes exist in this mode), with Q1’s question text listing every pending item that fits, or the top items and pending counts by type past the budget, followed by the fixed lines including the table path and, with more than 50 pending, the long-run notice; options carry no preview; (3) once the bulk answer resolves to anything but (d) Skip all and a PR number exists, issue the follow-up call: Q1 push. Threshold is 19 because checkbox mode tops out at 18 — this branch takes the whole layout, never a partial checkbox pass.

<!-- branch: main-path — commit-mode (same call in the 1 and 2-3 item layouts; follow-up call with topic-group and dispatch for 4–18 pending items; skipped only when bulk action = (d) skip) -->

**Commit mode** — placed per the slot table above: same call in the closed-only branch and for 1–3 pending items, follow-up call (paired with topic-group and dispatch) for 4–18 pending items, one shared call in context-budget mode. In the follow-up flow ask it immediately after the bulk action resolves to (a), (b), (c), or unanswered (skip only when (d) skip-all). Commit mode is always the user's choice; item scope ((c) = all items) never implies a commit mode:

```text
AskUserQuestion: "Commit mode for selected items:"
  (a) Each item separately — one commit per action item (default)
  (b) By topic group — group related items into themed commits (grouping strategy asked next)
  (c) All at once — single commit after all items
  (d) Stage only — no commits; stay staged on PR branch (⚠ cannot cleanly restore to $SAVED_BRANCH after Step 11; governs Step 8 action-item commits only — the Steps 5–7 merge commit is unconditional and always created)
```

**ESSENTIAL — all 4 options mandatory, never emit fewer than 4** (empirically motivated: LLMs tend to drop (b) By topic group and (d) Stage only — both must appear every time). Distinct menu from bulk-action question, never merge or pull its options in — this menu sets commit MODE (how to commit), bulk action sets item SCOPE (which items). Sharing one AskUserQuestion call is fine; sharing one menu never is.

Set `COMMIT_MODE`:

- (a) → `each`
- (b) → `grouped`
- (c) → `all`
- (d) → `stage`
- unanswered → `each` (default)

**Topic-group question** — always present in the SAME call as the commit-mode menu wherever the slot table leaves room (the follow-up call for 4–18 pending items, the closed-only branch, and the ≥19 single call): the commit-mode answer is unknown when that call is built, so the question is asked unconditionally there and its answer discarded silently unless commit mode resolves to (b) — same pattern as the skip-all discard. For 1–3 pending items it rides the follow-up call beside the push question, asked only when commit mode = (b): the 2-3 first call is full, and the 1-item first call leaves its fourth slot free on purpose — with one pending item a grouping strategy matters only when typed closed ids widen the selection, so it is not asked unconditionally. Options are grouping strategies, not free-text labels: the orchestrator already knows each item's `change` category and `file`, so it proposes concrete groupings and only falls back to typing. Typed labels are collected here rather than at Step 8 — the same decision over the same item list (HEAD's Step 8 label question listed `SELECTED_ITEMS`), and a label question after implementation would park the unattended Run 2.

```text
Topic-group question — multiSelect: FALSE
"If 'By topic group' — how should items group?"
  (a) By change domain — one commit per `change` category (perf, docs, test, ...)
  (b) By file/module — one commit per touched file or package
  (c) By specialist domain — mirrors the Step 8 Phase 2 dispatch groups
  (d) Let me type labels — via "Type something" as `<id>=<topic>` pairs, e.g. `1=style 2=logic 3=tests`
```

Set `GROUP_STRATEGY`: (a) → `domain` · (b) → `file` · (c) → `specialist` · typed `<id>=<topic>` pairs → `labels` · typed `auto` → `domain` (same `change`-field mapping) · unanswered → `domain` (default). `COMMIT_MODE` ≠ `grouped` → discard; `GROUP_STRATEGY` unused.

**(d) picked with no pairs typed, and commit mode = (b)** → the labels are still owed: ask the **Topic-label question** below in the next Step 3d call (alone, or beside any question that call still has room for). It is the same question Step 8 used to ask after implementation, over the same `SELECTED_ITEMS` list — asked now so Run 2 stays unattended:

```text
AskUserQuestion: "Assign a topic label to each implemented item (e.g. style, logic, tests, docs, config).
Items implemented:
  <for each item in SELECTED_ITEMS: "#<id>: <summary>">
Type a topic for each item ID (e.g. '1=style 2=logic 3=tests'), or type 'auto' to infer labels from change field."
```

- Typed `<id>=<topic>` pairs → `labels`. Typed `auto` → `domain` (the same `change`-field mapping).
- Skipped (empty response or blank) → fall back to `each`, exactly as the Step 8 question did: run the `(a)` each commit-mode block instead of `grouped` and print `⚠ no labels typed — committing each item separately`.

`labels` only: write the pairs with the Write tool to `$IMPL_DIR/group-labels.tsv`, one `<id>\t<topic>` row per pair — numeric ids only, topic lowercased with every character outside `[a-z0-9-]` replaced by `-`. The Write tool, not a shell `echo`: the topics are user free text, and Step 8's group commit reads this file instead of asking again.

**Dispatch-granularity question** — placed per the slot table above, asked when pending or resolved/addressed items are available (omitted on the empty-list push-only path; bulk action = (d) skip-all discards the answer, as for topic-group). It sets **wave width and sub-group size only**: specialist routing, the file-ownership tiebreak, and the import-coupling merge are correctness guards, never widened or dropped by any answer. The per-spawn item cap is width, so `(c)` widens it from 5 to 8 — never drops it: no answer hands one agent more than 8 items, and one file's overflow runs as chained links (`dispatch-phase2.md`).

```text
Dispatch-granularity question — multiSelect: FALSE
"Phase 2 runs specialists in isolated worktrees. How should the work spread?"
  (a) Auto — one worktree per specialist, split at ≤5 items, pool-capped waves (Recommended)
  (b) Sequential — same groups, one worktree at a time
  (c) Per specialist — one worktree per specialist, split at ≤8 items (fewer spawns; >8 items still split)
  (d) Custom — show the computed groups first, then choose from these
```

Set `DISPATCH_MODE`:

- (a) → `auto` · (b) → `sequential` · (c) → `per-specialist` · (d) → `preview` · unanswered → `auto` (default)
- (a) is both default and recommended, so it stays first — deliberate exception to the recommended-second convention: the (a)–(d) letters key the fixed sentinel blocks below and the preview gate's re-ask.
- Groups cannot be shown here: Phase 2 forms them from `SURVIVING_ITEMS` after Phase 1's challenge verdicts, so a concrete list does not exist at Step 3d. (d) Custom is the only path to approving real groups and costs one extra gate at the Phase 1 → Phase 2 boundary; the other three answers keep this run gate-free from here to dispatch.
- `preview` is not a width. At that boundary `dispatch-phase2.md` lists the formed groups as plain lines in the question text of its re-ask, saves their table and prints it once as the last text of the reply message that issues the (a)/(b)/(c) re-ask, then the orchestrator runs the matching block below a second time to record the resolved width.

`(a)` auto:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo auto > "${TMPDIR:-/tmp}/resolve-dispatch-mode-${CSID}"  # timeout: 3000
```

`(b)` sequential:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo sequential > "${TMPDIR:-/tmp}/resolve-dispatch-mode-${CSID}"  # timeout: 3000
```

`(c)` per specialist:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo per-specialist > "${TMPDIR:-/tmp}/resolve-dispatch-mode-${CSID}"  # timeout: 3000
```

`(d)` custom — records `preview`, the internal value the boundary gate keys on:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo preview > "${TMPDIR:-/tmp}/resolve-dispatch-mode-${CSID}"  # timeout: 3000
```

Persist every answer once the menus resolve — Step 8's merge fence passes `--commit-mode` to `merge_specialist_batch.py`, its after-loop grouping reads the strategy (and `group-labels.tsv` for `labels`), Phase 2 reads the dispatch mode, and Steps 10–11 read the push and post-PR answers; none survives a fence boundary or a compaction on its own.

<!-- policy-sibling: plugins/CLAUDE.md §Blueprint Blocks (canonical), plugins/cc_foundry/agents/challenger.md, plugins/cc_oss/skills/resolve/modes/item-selection.md (Step 3d), plugins/cc_oss/skills/resolve/modes/push.md (Step 10), plugins/cc_oss/skills/review/modes/acceptance-gate.md (reject gate) -->

Run **exactly one** commit-mode block — the one matching the user's answer — then, for `grouped` only, exactly one strategy block. Never edit a block's text to a different value: an edited block misses the blueprint manifest, and a block run unedited silently persists the wrong mode (a real run selected grouped, landed 12 per-item commits because the old single block carried `each` as its literal default).

`(a)` each:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo each > "${TMPDIR:-/tmp}/resolve-commit-mode-${CSID}"  # timeout: 3000
```

`(b)` grouped:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo grouped > "${TMPDIR:-/tmp}/resolve-commit-mode-${CSID}"  # timeout: 3000
```

`(c)` all:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo all > "${TMPDIR:-/tmp}/resolve-commit-mode-${CSID}"  # timeout: 3000
```

`(d)` stage:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo stage > "${TMPDIR:-/tmp}/resolve-commit-mode-${CSID}"  # timeout: 3000
```

Strategy — `grouped` only; skip otherwise (Step 0 already wrote the `domain` default):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo domain > "${TMPDIR:-/tmp}/resolve-group-strategy-${CSID}"  # timeout: 3000
```

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo file > "${TMPDIR:-/tmp}/resolve-group-strategy-${CSID}"  # timeout: 3000
```

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo specialist > "${TMPDIR:-/tmp}/resolve-group-strategy-${CSID}"  # timeout: 3000
```

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo labels > "${TMPDIR:-/tmp}/resolve-group-strategy-${CSID}"  # timeout: 3000
```

<!-- branch: main-path — push (follow-up call in every band; one push-only call only at zero pending with no closed items; omitted when no PR number exists) -->

**Push question** — placed per the slot table above; asked whenever a PR number exists. It records **push intent only**, plus the post-PR action. It is never push authorization: commits do not exist yet, so the scope the push-safety rule (`git-commit.md`, "Never push without explicit user confirmation") requires the user to see — diff stat, commit count, last subject — cannot be shown here. Intent "push" means Step 10 asks the scope-bearing push confirmation; an explicit "don't push" is itself an answer, and Step 10 then skips its question silently. Name what is knowable now — target `$FORK_REMOTE/$HEAD_REF` when Step 4 already ran (`pr`, `pr+report`), otherwise `the head branch of PR #<N>` (`report` mode checks out after this gate) — plus the selected item count and commit mode.

```text
Push question — multiSelect: FALSE
"After the lint/QA gate passes, push to <target>? (Step 10 shows the diff stat and asks you to confirm before pushing.)"
  (a) Push (confirm at Step 10), then open the PR in the browser
  (b) Push (confirm at Step 10) (Recommended)
  (c) Don't push — I'll push manually; open the PR in the browser
  (d) Don't push — I'll push manually
```

Unanswered or dismissed → run **no** push block and **no** post-PR block: the push sentinel stays `unset` and post-PR stays at Step 1's `skip`, so Step 10 asks both push and post-PR at push time exactly as before — no answer is never a silent skip. Otherwise run **exactly one** push block and **exactly one** post-PR block — never edit a block's value (same trap as the commit-mode blocks above: an unedited default silently wins):

| Answer | Push block | Post-PR block |
| -- | -- | -- |
| (a) | push | open |
| (b) | push | skip |
| (c) | skip | open |
| (d) | skip | skip |
| unanswered / dismissed | none — stays `unset`, Step 10 asks push + post-PR | none |

Push = push:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo push > "${TMPDIR:-/tmp}/resolve-push-auth-${CSID}"  # read back at Step 10  # timeout: 3000
```

Push = skip:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo skip > "${TMPDIR:-/tmp}/resolve-push-auth-${CSID}"  # read back at Step 10  # timeout: 3000
```

Post-PR = open:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo open > "${TMPDIR:-/tmp}/resolve-post-pr-action-${CSID}"  # read back at the end of Step 11  # timeout: 3000
```

Post-PR = skip:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
echo skip > "${TMPDIR:-/tmp}/resolve-post-pr-action-${CSID}"  # read back at the end of Step 11  # timeout: 3000
```

**Batch the answer blocks.** The matching commit-mode, strategy, dispatch, push and post-PR blocks each write a different file, so issue all of them in **one response**, never one block per turn. Run the confirm block below in the next response, beside the Step 7b join's first tool call — the echoed line must match the user's answers before Step 3e starts:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _CM < "${TMPDIR:-/tmp}/resolve-commit-mode-${CSID}" 2>/dev/null || _CM="unset"
IFS= read -r _GS < "${TMPDIR:-/tmp}/resolve-group-strategy-${CSID}" 2>/dev/null || _GS="unset"
IFS= read -r _DM < "${TMPDIR:-/tmp}/resolve-dispatch-mode-${CSID}" 2>/dev/null || _DM="unset"
IFS= read -r _PA < "${TMPDIR:-/tmp}/resolve-push-auth-${CSID}" 2>/dev/null || _PA="unset"
IFS= read -r _PP < "${TMPDIR:-/tmp}/resolve-post-pr-action-${CSID}" 2>/dev/null || _PP="unset"
echo "commit-mode=$_CM group-strategy=$_GS dispatch-mode=$_DM push=$_PA post-pr=$_PP"  # timeout: 3000
```

**No per-pass item cap** — every selected ID enters Step 3e and Step 8 in this pass; never trim a selection or defer a remainder to a rerun. Step 8 bounds load per agent instead (`dispatch-prelude.md` §Caps: ≤12 items per challenge agent, ≤5 per implementation spawn — ≤8 under `per-specialist`, one file's overflow chained — wave-paced). **Large-selection notice** — rides in the ≥19 call's Q1 question text (**Bulk question text** line 4, more than 50 pending): a field the call itself renders, read while the user is still choosing the scope. Never as reply text before a tool call (5.5-family models may return that as an empty progress update) and never in a task subject (task tools may be disabled, and a subject update lands after the user's last answer). After bulk resolution, more than 50 selected → Step 11's final report repeats it as one line, `→ <N> items selected — all ran in this pass; challenge fanned out at ≤12 items/agent.` No question, no trim.
