<!-- oss:resolve Step 3b — executed inline: cat $_OSS_RESOLVE/modes/pr-intelligence.md; execute -->

<!-- fragment — no <workflow> wrapper; executed inline by SKILL.md orchestrator -->

<!-- consumer: plugins/cc_oss/skills/resolve/SKILL.md (Step 3b) -->

## Step 3b: PR intelligence

Fetch full PR metadata in one call:

```bash
gh pr view <PR_NUMBER> \
    --json number,title,body,author,labels,isDraft,state,headRefName,baseRefName,headRepositoryOwner,headRepository,isCrossRepository,url,closingIssuesReferences
```

Extract and record:

- `HEAD_REF` — source branch name (`.headRefName`)
- `BASE_REF` — target branch name (`.baseRefName`, e.g. `main`, `develop`)
- `PR_AUTHOR` — contributor's GitHub login (`.author.login`)
- `HEAD_REPO_OWNER` — owner of fork/head repo (`.headRepositoryOwner.login`)
- `BASE_REPO_OWNER` — owner of base repo; from `.url` via `split("/")[3]` or `gh repo view --json owner -q .owner.login`
- `IS_FORK` — `.isCrossRepository` (`true` = fork PR, `false` = same-repo branch)
- `CLOSING_ISSUES` — linked issue numbers (`.closingIssuesReferences[].number`)
- `PR_TITLE` — `.title`
- `PR_BODY` — `.body` (short; kept in-context as motivation prompt seed)
- `PR_LABELS` — `.labels[].name | join(",")` (comma-separated label names; empty string if none)

Set up implementation work directory and fetch repo name (used throughout the workflow):

```bash
# absolute path required — subagents may have different CWD; relative path silently loses files
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
[ -z "$IMPL_DIR" ] && IMPL_DIR=$(mktemp -d)  # timeout: 3000
[[ "$IMPL_DIR" = /* ]] || IMPL_DIR=$(mktemp -d)
mkdir -p "$IMPL_DIR"  # timeout: 3000
echo "$IMPL_DIR" > "${TMPDIR:-/tmp}/resolve-impl-dir-${CSID}"  # persist at creation — Step 3d gate can idle past a compaction; sentinel is how Step 8 finds the dir
REPO_NAME=$(gh repo view --json name --jq .name 2>/dev/null)  # timeout: 6000
```

### Thread intelligence (subagent)

Infer `INTEL_AGENT` from `PR_LABELS` + `PR_TITLE` (lowercase, first match wins) using the same routing table as `action-item-dispatch.md`:

| Signal keywords in labels/title | `INTEL_AGENT` |
| -- | -- |
| `test`, `spec`, `pytest`, `coverage` | `foundry:qa-specialist` |
| `doc`, `readme`, `changelog`, `sphinx` | `foundry:doc-scribe` |
| `lint`, `style`, `format`, `ruff`, `mypy`, `typing`, `type hint`, `annotation`, `annotate`, `docstring`, `comments` | `foundry:linting-expert` |
| (no match / mixed) | `foundry:sw-engineer` |

**`--agent` override applies to `INTEL_AGENT`**: when caller passes `--agent <name>`, resolved agent overrides routing table for `INTEL_AGENT` as well as Step 8 implementation. Bridge implementation skill is never a classification agent — fall back to routing table for `INTEL_AGENT`.

Read the normalized Step 1 agent override before choosing the routing-table default:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_AGENT_FILE="${TMPDIR:-/tmp}/resolve-agent-override-${CSID}"
[ -f "$_AGENT_FILE" ] || { echo "! BLOCKED — agent override sentinel missing; Step 1 parser never ran"; exit 1; }
IFS= read -r _AGENT_OVERRIDE < "$_AGENT_FILE" || _AGENT_OVERRIDE=""
if [ -n "$_AGENT_OVERRIDE" ] && [ "$_AGENT_OVERRIDE" != "bridge:implement" ]; then
    echo "INTEL_AGENT=$_AGENT_OVERRIDE"
else
    echo "INTEL_AGENT=route-by-table"
fi
```

Use the printed override as `INTEL_AGENT` exactly when present; `route-by-table` means choose from the table above, including when `--agent bridge:implement` was explicit. Do not reparse cleaned `$ARGUMENTS` or prefix the saved value again. Substitute the chosen agent literally in the `Agent` call below.

Apply `agent-resolution.md` fallback to `INTEL_AGENT` (foundry absent → substitute with `general-purpose` + role prefix).

Raw PR discussion — all `--comments`, formal reviews, inline code comments — can be thousands of tokens on active PR. Offload fetching + classification to subagent; orchestrator context stays small. Subagent writes structured output to `$IMPL_DIR/`; orchestrator reads only compact envelope, loads classified table from file.

```text
Agent(subagent_type="${INTEL_AGENT}", prompt="
Fetch and classify PR #<PR_NUMBER> review feedback for the /oss:resolve workflow.

Inputs (substitute literal values — agent does not inherit shell variables):
- PR: #<PR_NUMBER>  (repo: <BASE_REPO_OWNER>/<REPO_NAME>)
- PR title: <PR_TITLE>
- PR body: <PR_BODY>
- Linked issues: <CLOSING_ISSUES>  # comma-separated issue numbers; may be empty
- Contributor: @<PR_AUTHOR>
- Output dir: <IMPL_DIR>           # expand to absolute path before spawning
- Shared rules: <_OSS_SHARED>/github-review-parsing.md   # expand to absolute path before spawning

<!-- loads: github-review-parsing.md -->

Read <_OSS_SHARED>/github-review-parsing.md first. Follow its fetch-completeness rule (both
endpoints below mandatory, neither alone enough), its collapsed-`<details>`-block
expansion rule (a review body listing suppressed/nested findings is never one item), and its
cross-round dedup rule (same file+line recurring across reviews/timestamps merges to one item)
for everything below.

Fetch (each gh call timeout 15000 ms; run as Bash):
1. gh pr view <PR_NUMBER> --comments
2. gh api repos/<BASE_REPO_OWNER>/<REPO_NAME>/pulls/<PR_NUMBER>/reviews
3. gh api repos/<BASE_REPO_OWNER>/<REPO_NAME>/pulls/<PR_NUMBER>/comments
4. Resolved-thread databaseId list via GraphQL with full pagination:
   Use query with pageInfo{hasNextPage,endCursor} on reviewThreads(first:100,after:\$after).
   Loop until hasNextPage=false; accumulate databaseId values for isResolved=true threads.
   On GraphQL failure → treat as empty list.
5. For each issue number in CLOSING_ISSUES: gh issue view <N> --json title,body

Assign location field per source (determines GitHub resolvability):
  Source 1 (gh pr view --comments) → location: discussion (PR main-thread; no GitHub "Resolve conversation" button)
  Source 2 (gh api .../reviews) top-level body — apply github-review-parsing.md's collapsed-block
    expansion FIRST: each nested finding inside a `<details>` block becomes its own item, not
    the review body as one unit. Every expanded (or bare, no-block) Source 2 item →
    location: discussion (review-body text, its `url` is the review's, never a real comment
    thread — no resolve button, and the resolved-thread `[done]` check below can never apply
    to it). Never promote a Source 2 item to location: inline even when it names the same
    file+line as a Source 3 comment — the cross-round dedup pass below already merges that
    pair and keeps Source 3's inline occurrence; promoting here preempts that pass and is
    redundant with it.
  Source 3 (gh api .../comments) → location: inline (code-review thread; "Resolve conversation" button available)
  [report] items (no GitHub source) → location: report
Key invariant: location tracks "does this comment have a resolvable PullRequestReviewThread?" not which endpoint returned it.

Synthesize contribution motivation (2–3 sentences using PR body + linked issues):
what problem contributor solving, why this approach, expected user-visible outcome.
Becomes priority lens for conflict resolution.
**PR body = stated intent; thread = authoritative record**: PR descriptions often drift from actual implementation when reviewers request changes mid-review. When PR body conflicts with what thread discussion/reviewer requests agreed, thread wins. Use thread consensus for what was actually implemented, not original PR description.

Classify EVERY comment using these codes:
  [gh][req]      change required before merge (reviewer with write access / maintainer)
  [gh][suggest]  improvement, non-blocking
  [gh][question] open question — needs answer before deciding what code to write
  [done]         location:inline thread isResolved=true OR subsequent commit/reply addressed it; location:discussion — no isResolved signal; mark [done] only if a subsequent reply clearly addresses it (discussion items will otherwise remain pending — GitHub has no resolve button for them)
  [info]         praise / acknowledgement / emoji-only — skip
  [self-review]  /oss:review finding — not a GitHub commenter

Per location:inline comment: if its REST 'id' (= GraphQL databaseId) appears in resolved-thread
list → mark [done] without reading content. All others: apply codes above.
Per location:discussion comment: skip resolved-thread list entirely — PR discussion comments have no resolvable PullRequestReviewThread; apply classification codes directly.

**Deprecation false-positive filter**: Before finalising any action item whose `full_comment_text` requests adding a deprecation warning (keywords: "deprecate", "deprecation", "DeprecationWarning", "deprecated") for a removed argument, parameter, or function:
1. Determine the removed symbol name from comment context or diff.
2. Get latest release tag: `LATEST_TAG=$(git describe --tags --abbrev=0 2>/dev/null || gh release list --limit 1 --json tagName --jq '.[0].tagName' 2>/dev/null)`  # timeout: 6000
3. Read the tagged file content, not the tag commit patch: `git show "${LATEST_TAG}:${file_path}"`  # timeout: 6000. Treat a failed read or path missing at the tag as unknown, not as proof that the symbol was unreleased. A rename may put the released symbol at another path.
4. **Symbol found in tagged content** → keep original classification ([gh][req] or [gh][suggest]); this is evidence that deprecation is needed.
5. **Symbol absent from tagged content** → keep original classification and add Notes "release status unknown — inspect older tags and renamed paths". Absence from one release cannot prove the symbol was never released.
6. **No tag or unreadable tagged file** → keep original classification and add Notes "release history or path missing — deprecation status unknown". Never downgrade solely from missing release evidence.

ACTION_ITEM fields: id (sequential int starting at 1), type, change, severity, author,
summary (≤60 chars, truncated at word boundary with …), file, line, url (html_url from
API, blank for report items), full_comment_text, location, origin.
  - change ∈ {code,test,docs,config,ci,style,refactor,perf,architecture}; default=code when ambiguous. `perf` = latency/memory/throughput/allocation-focused comment; `architecture` = API design, module boundary, coupling, interface-shape comment. Keep in sync with `_shared/review-section-taxonomy.md`'s resolve `change` column and `action-item-dispatch.md`'s `change` → `IMPL_AGENT` table.
  - severity ∈ 1..5 (5=highest); [req] floor=3
  - location ∈ {inline, discussion, report}; inline = code-review comment (GitHub "Resolve conversation" button available); discussion = PR main-thread comment (no resolve button — cannot be marked resolved in GitHub UI); report = /review finding (no GitHub source)
  - origin ∈ {posted, suppressed-block}; default=posted. `suppressed-block` per github-review-parsing.md rule 2 — a finding pulled out of a review's collapsed/suppressed section rather than posted directly; carries the bot's own lower-confidence signal, never silently indistinguishable from a posted finding downstream.

**Cross-round dedup pass** (github-review-parsing.md rule 3 — run before writing anything below):
group two classified items only when ALL three true — same file, wording is a
close/near-identical match, AND position consistent with recurrence (exact line match, OR
lines differ by an amount explainable by an intervening push, OR either item has no line).
Wording match never optional: same file + same/nearby line + unrelated wording never groups
— two unrelated findings can legitimately share or sit near a line. Collapse each group to ONE
ACTION_ITEM — keep most-resolvable occurrence's location/url (inline over discussion over
report), highest severity seen in group, union of classification codes if they differ. Number
of groups collapsed (group size > 1) is the `<N> recurring findings merged` count in the
Sources block below — no separate variable needed, already literal text in the file this
step writes.

Write THREE files using the Write tool (expand <IMPL_DIR> to the literal path above):

1. <IMPL_DIR>/pr-intelligence.md
   Sources block: Mode=pr · PR=#<PR_NUMBER> · GitHub=Read — PR body · <N> comments ·
   <N> reviews · <N> inline code comments · <N> recurring findings merged · Report=not used
   Motivation paragraph (2–3 sentences).
   Table header: ### Action Items — PR #<PR_NUMBER>
   Columns: # | Type | Change | Severity | Author | Status | Summary | Notes
   Truncation: Summary ≤60 chars, Notes ≤45 chars (use — when empty). Notes carries commit SHA for [done] rows and classification verdicts — never file:line, already held by the file/line fields.
   Status: every row starts pending. Write `pending` for `location: inline` and `location: report` rows; write `pending · thread (no GH resolve)` verbatim for `location: discussion` rows — GitHub has no Resolve button for PR main-thread comments, and this suffix is the only place that distinction is visible now that there is no Loc column. The location field itself stays in action-items.jsonl for resolve routing and gets no column.
   MUST render as markdown table. Example rows (inline, then discussion):
   | 1 | [gh][req] | code | 4 | @reviewer | pending | rename param x to count | — |
   | 2 | [gh][suggest] | docs | 2 | @reviewer | pending · thread (no GH resolve) | clarify README setup step | — |

2. <IMPL_DIR>/action-items.jsonl
   One compact JSON object per line, one ACTION_ITEM each.
   Fields: id, type, change, severity, author, summary, file, line, url, full_comment_text, location, origin.

3. <IMPL_DIR>/pr-vars.sh
   ONLY these assignments, one per line, each value single-quoted, no shell metacharacters:
     RESOLVED_THREAD_IDS_COUNT='<int>'
     ACTION_ITEMS_TOTAL='<int>'
     ACTION_ITEMS_REQ='<int>'
     ACTION_ITEMS_SUGGEST='<int>'
     ACTION_ITEMS_DONE='<int>'
     ACTION_ITEMS_INLINE='<int>'
     ACTION_ITEMS_DISCUSSION='<int>'
     PR_MOTIVATION='<motivation text; replace any literal single-quotes in text with spaces>'

DO NOT print table, motivation, or raw comment data in final message — write to files only.
Return ONLY this compact JSON as your FINAL message (nothing after it):
{\"status\":\"done\",\"items\":N,\"req\":N,\"suggest\":N,\"done\":N,\"deduped\":N,\"files\":[\"<IMPL_DIR>/pr-intelligence.md\",\"<IMPL_DIR>/action-items.jsonl\",\"<IMPL_DIR>/pr-vars.sh\"]}
")
```

> **Health monitoring** — CLAUDE.md §6: checkpoint before spawn; poll every 5 min; hard cutoff 15 min (tighten: use `CHALLENGE_TIMEOUT_S=300` from `<constants>` as the polling interval). On timeout ⏱: fall back to inline execution (fetch GitHub data directly in orchestrator context, classify inline) with explicit warning — never silently produce empty ACTION_ITEMS.

Validate and source vars after agent returns:

```bash
# only VAR='value' lines — mirrors parse-resolve-args.py defence-in-depth
if grep -qvE "^[A-Z_][A-Z0-9_]*='[^']*'$" "$IMPL_DIR/pr-vars.sh"; then
    echo "! BLOCKED — pr-vars.sh has unexpected output; refusing to source"
    cat "$IMPL_DIR/pr-vars.sh"
    exit 1
fi
. "$IMPL_DIR/pr-vars.sh"
[ "${RESOLVED_THREAD_IDS_COUNT:-0}" = "0" ] && echo "⚠ Could not fetch resolved thread status — some items may already be resolved; review table carefully"  # timeout: 3000
```

Read `$IMPL_DIR/pr-intelligence.md`, then put its full contents (Sources block + motivation + every action item table row) in an **assistant user-facing reply**, not Bash/tool stdout, immediately before Step 3d's AskUserQuestion. This is the only ACTION_ITEMS table in pure `pr` mode; Output-Routing `.temp` diversion does **not** apply (selection-driving, read-in-context; canonical exemption in SKILL.md Step 3c). Orchestrator context now holds *classified* table (~500–1000 tokens) rather than raw PR thread (often 5000–20000+ tokens on active PRs). Later steps read per-item details from `$IMPL_DIR/action-items.jsonl` when `full_comment_text` or other fields needed:

```bash
_ID="<id>"
case "$_ID" in ''|*[!0-9]*) echo "! BLOCKED — item id placeholder not substituted or non-numeric"; exit 1 ;; esac
jq -c ". | select(.id == $_ID)" "$IMPL_DIR/action-items.jsonl"  # timeout: 5000
```

### `[question]` item handling

Answer `[question]` items resolvable from code — **no `AskUserQuestion` in this step**. Classify inline: code directly answers question → reclassify `[req]` or `[suggest]` per reviewer intent; answer reveals known limitation or deferred work → keep `[question]` tag, append brief answer note. Unresolvable from code → keep `[question]` unchanged. All `[question]` items flow into Step 3d for user selection — user selecting one there implicitly approves implementation. Never self-promote without code evidence
