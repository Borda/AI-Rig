<!-- file: consolidate.md — consumers: audit/SKILL.md -->

# Mode: consolidate — foundry:audit

Triggered by `/foundry:audit [<scope>...] --consolidate`. Check 47. Finds narrow instruction sections that express one intent in different words and proposes fewer, broader ones, so the agent loads and navigates less. **Advisory only**: sources are never edited, no finding enters the fix gate; the only files written are run artifacts under `.temp/` and `.reports/` and the standard skill-state files (`.temp/state/preflight`, `.temp/state/skill-contract.md`), which are replaced or removed by name and never written through a link. Standalone: `audit_preflight.py` rejects it combined with `--upgrade`, `--adversarial` or `--efficiency`.

Owners (do not duplicate): verbatim/near-verbatim pairs → Checks 18c/33; single-file restatement → Check 29 and efficiency E8; agent-vs-agent roster overlap → Checks 20/46. This mode takes cross-file intent overlap inside one load group; a section covered by those owners is listed there, not re-reported here.

```text
CONSOLIDATE_MIN_GROUP_TOKENS=1500    # unblocked tokens a group needs before it is worth examining
CONSOLIDATE_MAX_GROUPS=12            # groups examined per run; truncation always reported
CONSOLIDATE_MIN_CLUSTER_TOKENS=400   # a smaller cluster cannot repay a judge; listed, not judged. A mergeable one must also hold half this beyond its largest member
CONSOLIDATE_MAX_JUDGED=20            # clusters judged per run, mergeable first then largest; the rest listed with the reason
CONSOLIDATE_JUDGE_BATCH=5            # clusters per judgment agent
CONSOLIDATE_REVIEW_BATCH=5           # proposals or edits per review agent
```

`--fast` only widens fan-out (`--batches 5`, the `CAP_OPUS` ceiling, instead of `MAX_BATCHES`); it never changes which groups are examined.

## Boundary — enforced

- Pre-flight prints `dispatch=mode:consolidate`; the skill then runs this mode and no other step: not Step 1 (configuration-only pre-commit validation), not Steps 1b–11, not the follow-up gate.
- Sources are never written. Writes: `$RUN_DIR/consolidate/`, the `consolidate-run-dir` sentinel, and the compaction contract (cleared in Phase 6).
- Never writes `audit-state-${CSID}/run-dir` (that arms the fix-gate hooks) and never writes `summary.jsonl`.
- Needs the plugin source tree: no `plugins/` dir → `! BLOCKED — --consolidate needs the plugin source tree; run it with --local in the checkout`.
- Every spawn follows `_shared/agent-spawn-protocol.md` §Deadlines: Write `$RUN_DIR/consolidate/agent-watch-<wave>.tsv` (`<name>\t<out file>\t900`) in the spawn response, end the turn, run `agent_watch.py --state-dir "$RUN_DIR/consolidate"` once per notification. Missed deadline → ⏱ `timed_out`, record, continue; never wait. Spawn names `cluster-N`, `judge-N`, `review-N`.

## Phase 1 — Inventory (zero LLM)

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name foundry) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
[ -d plugins ] || { echo "! BLOCKED — --consolidate needs the plugin source tree; run it with --local in the checkout"; exit 1; }
RUN_DIR=$(python "$PLUGIN_ROOT/bin/make_run_dir.py" .reports/audit)  # timeout: 5000
[ -z "$RUN_DIR" ] && { printf "! BREAKING: make_run_dir.py returned empty path\n"; exit 1; }
mkdir -p "$RUN_DIR/consolidate/proposals" "${TMPDIR:-/tmp}/audit-state-${CSID}"
python "$PLUGIN_ROOT/bin/consolidate_inventory.py" stash --root . --path "${TMPDIR:-/tmp}/audit-state-${CSID}/consolidate-run-dir" --text "$RUN_DIR"  # timeout: 5000 — replaces the file by name, never writes through a link
python "$PLUGIN_ROOT/bin/consolidate_inventory.py" inventory --root . --out "$RUN_DIR/consolidate/inventory.json"  # timeout: 60000
echo "RUN_DIR=$RUN_DIR"
```

Skill files are found by directory: the default `skills/` of every Claude plugin plus the directories its manifest adds (`skills` as a `./` path or a list of them, such as `./claude-skills/`; a folder holding `SKILL.md` directly is one skill with its fragments, and with `.` registered each child folder holding `SKILL.md` brings its own (the file makes the folder a skill, whatever the folder is called), a skill kept at the plugin root brings the Markdown beside it except `CLAUDE.md`, docs and the folders of other components; `.` names the `SKILL.md` at the root and the `SKILL.md` files one level down, both when both exist; a path leaving the plugin is ignored), the command files (a command is loaded when invoked, as a skill is, and is covered with the skills in one lazy group: every Markdown under `commands/`, or what a manifest `commands` path, list or `source` map names, which replaces the folder; inline `content` has no file), and the agent files of every Claude plugin (every Markdown under `agents/`, subfolders included; a manifest `agents` list replaces the folder), every Markdown under a skill except its `ARCH.md`, `README.md` and `CHANGELOG.md`, which are listed under `excluded`). `inventory.json` also records `digests`, the sha256 of every file it examined (this mode file included when run from the checkout), and `citation_digest`, one hash over every Markdown file under `plugins/` because any of them can cite a section, and `shared_digest`, one hash over the shared-file membership (which files are byte-identical copies, so which may not be merged): every later step rebuilds the inventory from the tree (same root and kinds) and compares the two documents whole, stopping when any part differs (a source, this mode file, a citing document, an added or removed instruction file, the shared-file manifest, or a hand-edited item, group or flag), so a run is never reported against text it did not examine and no derived fact is trusted; re-run `inventory` to start over.

Item fields the later phases rely on: `id`, `file`, `start_line`/`end_line`, `tokens`, `load` (hierarchy, paths, owner, side, pair), `group`, `inbound` (files citing the section as `<file> §<Heading>`), `blocked`. `group` is the **merge scope**: sections merge only inside one group. Always-loaded rules form one group per plugin, except `CLAUDE.src.md` (copied and user-edited, so its own group) and a rule that also ships a `-delta` variant (delivered one or the other, so each its own group). Scoped rules group by exact `paths:` globs, read from a block list, a flow list or one scalar; any other YAML spelling (folded or literal scalar, anchor, tag, duplicate key, or a `paths` key that is indented or inside a flow mapping) leaves the rule in a group of its own, so it is never merged. Lazy sections group by skill (every file the skill can load) or by the plugin's agents. `blocked` lists unresolved dependencies — shared-file copies, policy-sibling markers (resolved or not), frontmatter (routing or scope metadata); a blocked section is never clustered.

Merging is allowed only in `always` and `scoped` groups. A lazy group (skill or agents) holds files that load on different triggers, so its clusters may only be `sharpen` or `keep`: text moved from a mode file into the entrypoint would charge every other mode for it.

## Phase 2 — Select groups

Scope tokens come from the `scope=` line pre-flight printed and are a union, as in the audit skill's scope grammar: a kind (`rules`, `skills`, `agents`), `plugins` (everything), a plugin (`foundry` or `cc_foundry`), an agent file name, or a skill name each add the groups they name. No token = everything. A token that names nothing stops the run before any spend (`! BLOCKED`, exit 1). Substitute `SCOPE` with that line's tokens (an empty string when none) before running:

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name foundry) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r RUN_DIR < "${TMPDIR:-/tmp}/audit-state-${CSID}/consolidate-run-dir" 2>/dev/null || RUN_DIR=""
[ -n "$RUN_DIR" ] || { echo "! BLOCKED — consolidate run-dir sentinel missing; re-run /foundry:audit --consolidate"; exit 1; }
SCOPE='<scope tokens>'
case "$SCOPE" in *'<'*'>'*) echo "! BLOCKED — substitute SCOPE with the scope= tokens from pre-flight (empty string when none)"; exit 1;; esac
python "$PLUGIN_ROOT/bin/consolidate_inventory.py" select --inventory "$RUN_DIR/consolidate/inventory.json" --scope "$SCOPE" --min-tokens 1500 --max-groups 12 --batches 4 --out "$RUN_DIR/consolidate/selection.json"  # timeout: 30000
```

With `--fast` run the same block with `--batches 5`. `selection.json` holds `selected`, `batches` (group ids per curator, balanced by unblocked tokens), `not_examined` (each with its reason: single-file group, below token floor, group cap) and `unmatched_tokens`. Report every `not_examined` entry and unmatched token: the run states what it did not look at. Nothing selected → `! BLOCKED — no group in scope reaches the examination floor` and stop.

## Phase 3 — Cluster by purpose (`foundry:curator`)

One curator per batch, prompt from `consolidate-cluster-prompt.md`:

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name foundry) || exit $?; export PLUGIN_ROOT
python "$PLUGIN_ROOT/bin/load_mode.py" audit templates consolidate-cluster-prompt.md --local  # timeout: 5000
```

Substitute `<RUN_DIR>`, `<GROUPS>` (the batch's group ids), `<OUT>` (`<RUN_DIR>/consolidate/clusters-N.json`) and `<ELSEWHERE>` (`<RUN_DIR>/consolidate/elsewhere-N.json`) with literal values; scan the built prompt for those four placeholder names only (the output-schema examples in the templates carry other `<…>` text by design) before spawning. Prompt line 1 names the batch's group delta.

After the wave: merge and vet deterministically — no model judgment spent on a cluster the guard already rejects.

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name foundry) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r RUN_DIR < "${TMPDIR:-/tmp}/audit-state-${CSID}/consolidate-run-dir" 2>/dev/null || RUN_DIR=""
[ -n "$RUN_DIR" ] || { echo "! BLOCKED — consolidate run-dir sentinel missing; re-run /foundry:audit --consolidate"; exit 1; }
python "$PLUGIN_ROOT/bin/consolidate_inventory.py" collect --dir "$RUN_DIR/consolidate" --glob 'clusters-*.json' --out "$RUN_DIR/consolidate/clusters.json"  # timeout: 10000 — no match (every curator timed out) gives []
python "$PLUGIN_ROOT/bin/consolidate_inventory.py" validate --inventory "$RUN_DIR/consolidate/inventory.json" --clusters "$RUN_DIR/consolidate/clusters.json" --min-cluster-tokens 400 --max-judged 20 --out "$RUN_DIR/consolidate/vetted.json"  # timeout: 30000
_PRESERVE="run-dir=$RUN_DIR, inventory=$RUN_DIR/consolidate/inventory.json, clusters=$RUN_DIR/consolidate/clusters.json, vetted=$RUN_DIR/consolidate/vetted.json"
python "$PLUGIN_ROOT/bin/write_skill_contract.py" --keep-file "${TMPDIR:-/tmp}/audit-state-${CSID}/keep-items" "foundry:audit" "consolidate (after clustering)" "$RUN_DIR" "$_PRESERVE" "judge clusters with judge=true (Phase 4) → settle → independent review (Phase 5) → report (Phase 6); do not re-cluster"  # timeout: 5000
[ -s .temp/state/skill-contract.md ] || echo "⚠ compaction contract not written — a compaction now loses the run state"
```

A cluster with `ok: false` skips Phases 4–5 and goes to the report's "rejected by guard" table with its `reasons` (unsafe or duplicate id, fewer than two distinct members, members already claimed, one-file cluster, blocked member, mixed scope, flattened stub/`_full` pair, unknown member). A cluster with `ok: true, judge: false` is listed with its `skip_reason` (`below cluster floor`, `below saving floor` — a mergeable cluster whose members besides the largest hold under half the floor — or `judge cap`), not judged. Cross-scope merges are never proposed.

## Phase 4 — Judge (`foundry:curator`)

Only clusters with `judge: true` (`jq -r '.clusters[] | select(.judge) | .id' vetted.json`). Batches of `CONSOLIDATE_JUDGE_BATCH`, prompt from `consolidate-judge-prompt.md` loaded as in Phase 3, substituting `<RUN_DIR>`, `<CLUSTERS>` (the batch's cluster ids) and `<OUT>` (`<RUN_DIR>/consolidate/judgments-N.json`). Outputs per batch: `judgments-N.json` and one `proposals/<cluster-id>.md` per merge-prune cluster.

After the wave, settle deterministically. The script — not the judge — applies the rules:

- Merge proposals require a complete obligation map, cited-heading preservation and an eligible load group; unsafe merges become advice, never ready-to-apply changes.
- Sharpen edits stay inside their cluster sections, preserve cited headings and avoid blocked sections; moves and replacements require independent review, while a pure addition alone may skip it.
- Reject malformed, duplicate or ineligible judgments; retain missing judgments as `unjudged`. Revalidate inventory, citations, shared membership and source digests before trusting derived decisions.
- Writes stay within this run’s artifact directory and replace files by name. Before settling judgments, diagnosing a rejected judgment, reviewing proposed edits or interpreting an edge case, load the complete guard contract in `consolidate-guards.md`:

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name foundry) || exit $?; export PLUGIN_ROOT
python "$PLUGIN_ROOT/bin/load_mode.py" audit modes consolidate-guards.md --local  # timeout: 5000
```

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name foundry) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r RUN_DIR < "${TMPDIR:-/tmp}/audit-state-${CSID}/consolidate-run-dir" 2>/dev/null || RUN_DIR=""
[ -n "$RUN_DIR" ] || { echo "! BLOCKED — consolidate run-dir sentinel missing; re-run /foundry:audit --consolidate"; exit 1; }
python "$PLUGIN_ROOT/bin/consolidate_inventory.py" collect --dir "$RUN_DIR/consolidate" --glob 'judgments-*.json' --out "$RUN_DIR/consolidate/judgments.json"  # timeout: 10000 — no match (every judge timed out) gives []; settle then records the clusters as unjudged
python "$PLUGIN_ROOT/bin/consolidate_inventory.py" settle --inventory "$RUN_DIR/consolidate/inventory.json" --vetted "$RUN_DIR/consolidate/vetted.json" --judgments "$RUN_DIR/consolidate/judgments.json" --proposals-dir "$RUN_DIR/consolidate/proposals" --review-inputs "$RUN_DIR/consolidate/review-inputs.json" --out "$RUN_DIR/consolidate/settled.json"  # timeout: 30000
```

## Phase 5 — Independent review (`foundry:challenger`, always)

One challenger per `CONSOLIDATE_REVIEW_BATCH` entries of `review-inputs.json`, never a fork. Each entry carries only: its `digest` (a hash of exactly the change shown), the original sections' locations (file + line range), the citing files, and either the proposal file with its obligation map, survivor and `repoint` list (`kind: merge-prune`) or the edit list with its `repoint` list and `crosses_files` flag (`kind: sharpen`). Never the judge's reasoning: edits are copied with only `file`, `lines`, `action`, `before` and `after`. `review-inputs.json` empty → no review is spawned and the second settle below is skipped; `settled.json` already stands. Spawn `review-N`; prompt line 1 names the entry slice. Substitute `<RUN_DIR>`, `N` and the slice's cluster ids (the prompt also names `<RUN_DIR>/consolidate/review-inputs.json`, from which the reviewer takes only those entries); scan the built prompt for the placeholder names `<RUN_DIR>` and `<cluster ids>` only, as in Phase 3. Prompt:

> Adversarial review of instruction changes. Read-only: write only your findings file. Take the entries for clusters `<cluster ids>` from `<RUN_DIR>/consolidate/review-inputs.json`. For each, read the original sections at the given locations and the proposed text (merge proposal, or edit list), then try to prove the change loses something: an obligation, a trigger or incident condition, an exception, a threshold or literal, modal strength (`must`/`never`/`only`/`before`), an actor, an ordering, or a stop condition — or broadens scope so the rule fires where it should not — or leaves a citing file pointing at a deleted heading — or, for an entry with `crosses_files: true` (a skill or agent cluster), moves an obligation from one file into another under any wording — verdict `weakens`, quoting both places, because that is a merge and merging is not allowed there. Verdict per cluster: `holds` (nothing lost, with the check you ran), `weakens` (kept but softened or broadened — quote both), `loses` (an obligation is gone — quote it). Write `<RUN_DIR>/consolidate/review-N.json` as a list of `{"cluster":"<id>","digest":"<the entry's digest, copied unchanged>","verdict":"holds|weakens|loses","evidence":"<quotes>"}`. Return ONLY: `{"status":"done","file":"<path>","proposals":N,"holds":N,"weakens":N,"loses":N,"confidence":0.N}`

After the wave, merge the reviews and settle again with them:

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name foundry) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r RUN_DIR < "${TMPDIR:-/tmp}/audit-state-${CSID}/consolidate-run-dir" 2>/dev/null || RUN_DIR=""
[ -n "$RUN_DIR" ] || { echo "! BLOCKED — consolidate run-dir sentinel missing; re-run /foundry:audit --consolidate"; exit 1; }
python "$PLUGIN_ROOT/bin/consolidate_inventory.py" collect --dir "$RUN_DIR/consolidate" --glob 'review-[0-9]*.json' --out "$RUN_DIR/consolidate/reviews.json"  # timeout: 10000 — no match (every challenger timed out) gives []; the entries stay unvalidated
python "$PLUGIN_ROOT/bin/consolidate_inventory.py" settle --inventory "$RUN_DIR/consolidate/inventory.json" --vetted "$RUN_DIR/consolidate/vetted.json" --judgments "$RUN_DIR/consolidate/judgments.json" --proposals-dir "$RUN_DIR/consolidate/proposals" --reviews "$RUN_DIR/consolidate/reviews.json" --review-inputs "$RUN_DIR/consolidate/review-inputs.json" --out "$RUN_DIR/consolidate/settled.json"  # timeout: 30000
```

A verdict applies only to the change whose `digest` it carries; one with another or no digest (a stale or reused review file) is ignored, noted in `downgrades`, and leaves the entry `unvalidated`. The verdicts apply mechanically: `holds` keeps the decision; `weakens` → `rejected` with the quoted difference (never relabelled `sharpen`); `loses` → `keep`. A challenger that is missing, timed out or unavailable leaves its entries `unvalidated`: reported as such and **not** presented as ready to apply.

## Phase 6 — Report

Write `<RUN_DIR>/consolidate/report.md` from `settled.json` starting with the YAML `---` metadata block (`Title`, `Date`, `Scope`, `Focus`, `Agents`, `Outcome`, `Confidence`, `Next steps`, `Path`, plus `Groups examined`, `Groups not examined`, `Clusters`, `Merge-prune`), then, in order:

1. **Summary** — `totals.savings` per load class: tokens before/after for validated proposals; always-loaded savings separate from per-invocation (lazy) savings; `unvalidated` amounts shown apart, never added to validated ones.
2. **Proposals** — per `merge-prune` cluster: id, validation (`holds`/`unvalidated`), locations merged, surviving location, tokens before→after, `proposals/<id>.md`, `proposals/<id>.obligations.json`.
3. **Sharpen / keep** — one line each with the reason (distinct decision surface, incident condition, differing trigger); a sharpen that removes text shows its validation (`holds`/`unvalidated`); `downgrades` shown for any merge that fell back to sharpen; `rejected` entries show the reviewer's quote.
4. **Rejected by guard** — cluster id + `reasons` from `vetted.json`; clusters under the cluster floor.
5. **Blocked sections** — count by reason (shared-file, policy-sibling, unresolved sibling, frontmatter) and the largest ones; these need a coordinated multi-plugin edit and are out of this mode.
6. **Not examined** — `not_examined` and `unmatched_tokens` from `selection.json`, with unblocked tokens; items the curators routed to other owners (`elsewhere-N.json`); timed-out agents (⏱).
7. **Confidence** block per `quality-gates.md`. State that the judge's paraphrase discovery and its restraint on load-bearing rules are measured only by the controlled-fixture run (`tests/audit/fixtures/consolidate_fixture.json`), not by a regression suite, and that the report was built from the `settled.json` written after the reviews were merged (never from the pre-review one).

Then clear the contract: `rm -f .temp/state/skill-contract.md`.

Terminal: render the YAML block as the two-column table first, then `→ <report path>`, then a 2–3 sentence summary listing every merge-prune proposal. No follow-up gate and no fix option: nothing here is auto-fixable. Print as plain text, per proposal the user wants applied: `/foundry:manage update <skill|agent|rule name> "<change directive naming the proposal file>"` — `/foundry:manage` is user-run (`disable-model-invocation`), and any apply passes the instruction-file compression gate (`rules/markdown.md`) first. State the Codex counterpart decision: Claude side only; `codex-rig:audit` value-per-token applicability is a separate follow-up.
