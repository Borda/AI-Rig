<!-- file: consolidate-cluster-prompt.md — consumers: audit/modes/consolidate.md -->

Cluster instruction sections by intent. Read-only: write only `<OUT>` and `<ELSEWHERE>`.

Inputs: `<RUN_DIR>/consolidate/inventory.json`; your groups: `<GROUPS>`.

1. From `items`, take those whose `group` is one of your groups and whose `blocked` list is empty. Skip blocked items entirely.
2. For each, read its text (`file`, lines `start_line`–`end_line`) and write one sentence stating the **intent** — the situation it governs and the directive it gives — in your own words, not the section's wording.
3. Group items whose intents point the same direction (same situation class, same kind of directive). Clusters stay inside one group: never mix groups.
4. A cluster has 2+ items from 2+ different files, and no item appears in two clusters. A section that stands alone is not listed.
5. Do not cluster, and list under `<ELSEWHERE>` instead:
   - verbatim or near-verbatim pairs (Checks 18c/33 own them);
   - repetition inside one file with no other file involved (Check 29 / efficiency E8 own it);
   - agent descriptions whose only overlap is routing boundaries (Checks 20/46 own it).
6. Cover every unblocked item exactly once: in a cluster, in `<ELSEWHERE>`, or counted under `unclustered`.

Write `<OUT>` as a JSON list: `[{"id":"<batch>-<slug>-<n>","group":"<group>","items":["<item id>",…],"purposes":{"<item id>":"<one sentence>"},"direction":"<shared direction in one sentence>"}]`. No other keys. Cluster ids use letters, digits, `-` and `_` only, and are unique across the whole run: start with the batch number. Write `<ELSEWHERE>` as `[{"items":["<item id>",…],"owner":"18c/33|29/E8|20/46","note":"<one line>"}]`.

Return ONLY: `{"status":"done","file":"<OUT>","clusters":N,"unclustered":N,"elsewhere":N,"confidence":0.N}` — on running short of budget return what is written with `"partial":true`.
