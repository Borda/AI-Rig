<!-- file: consolidate-judge-prompt.md — consumers: audit/modes/consolidate.md -->

Judge instruction-merge clusters. Read-only toward sources: write only `<OUT>` and the proposal files.

Inputs: `<RUN_DIR>/consolidate/inventory.json` (item locations), `<RUN_DIR>/consolidate/clusters.json` (members under `items`, per-member `purposes`), `<RUN_DIR>/consolidate/vetted.json` (guard result: `tokens_before`, `merge_allowed`, `inbound`); your clusters: `<CLUSTERS>`.

Per cluster, read every member's text (`file`, `start_line`–`end_line`) and decide:

- `keep` — each member has a distinct decision surface: its own trigger, incident condition, exception, threshold or actor. A broad statement would fire where a narrow one does not, or stop firing where it must.
- `sharpen` — members stay separate but boundaries, wording or a cross-reference should tighten so they stop competing.
- `merge-prune` — ONE statement keeps every obligation. Only then. Never when the cluster's `merge_allowed` is false (a skill's or the agents' files never co-load unconditionally): `sharpen` or `keep` only.

Merge-prune rules, all mandatory:

- Every obligation, trigger, exception, threshold, literal (command, path, number), actor and ordering of every member survives. Modal strength never drops (`must`/`never`/`only`/`before` stay). Narrow conditions that encode an incident stay as conditions of the merged text, never generalised away.
- Choose the **surviving item** (the one whose location keeps the merged text); the others are deleted. Same stub/`_full` side as the members; never move text across a plugin or a load group. Prefer as survivor the member that other files cite (`inbound` in `vetted.json`).
- A deleted member that other files cite as `<file> §<Heading>` leaves those citations dangling: list each citing file under `repoint`, and the proposal states where the citation now points.
- The merged text is shorter than the members combined. Not shorter → `sharpen`.
- Write the merged text, as it would appear in the file, to `<RUN_DIR>/consolidate/proposals/<cluster id>.md`.
- Build the obligation map: one entry per obligation found in the members: `{"original":"<quote, ≤25 words>","from":"<item id>","to":"<exact words in the proposal that carry it>","modal_preserved":true|false}`. Any `false` or any member obligation missing from the map → the cluster is `sharpen` or `keep`, not `merge-prune`.
- Per member, name the trigger situation and confirm the merged text still states it.
- A `merge-prune` carries no `edits`: its whole change is the proposal. Files that cite a deleted heading go under `repoint`, never into an edit.

For a `sharpen`, list every edit under `edits`: `{"file":"<path>","lines":"<start>-<end>","action":"add|delete|rewrite|pointer","before":"<quoted text, empty for add>","after":"<replacement or added text>"}`. In a skill or agent cluster never carry text removed from one file into another (that is a merge): reword within each file. An edit that deletes or rewrites over a heading that `vetted.json` lists under `guarded` (a section's own heading or a level-5/6 heading inside it, with the files citing it) lists each citing file under `repoint`, or the rewrite keeps that heading line. An edit that deletes, rewrites or replaces text with a pointer is reviewed independently before it is reported; one that only adds text is not.

Write `<OUT>` as a JSON list: `[{"cluster":"<id>","decision":"keep|sharpen|merge-prune","shared":"<shared surface>","distinct":"<remaining distinct surface>","survivor":"<item id or empty>","proposal":"<path or empty>","obligation_map":[…],"repoint":["<citing file>",…],"edits":[…],"triggers":{"<item id>":"<trigger>"},"reason":"<one sentence>"}]`. An unused text field is `""` and an unused list is `[]`; never `null` — a null field makes the whole judgment malformed.

Return ONLY: `{"status":"done","file":"<OUT>","clusters":N,"merge_prune":N,"sharpen":N,"keep":N,"confidence":0.N}` — partial work returns what is written with `"partial":true`.
