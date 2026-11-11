# Mode: Rename Occurrence Validation

<!-- file: rename-validation.md — consumers: manage/SKILL.md -->

Triggered after cross-reference propagation (Step 5, rename mode only). Scan for remaining occurrences with word-boundary matching to cut noise from short/common names:

Derive scope and canonical forms from the entity's actual location. For project-local entities, use `/<old-name>` for a skill, `<old-name>` as an agent's `subagent_type`, and its `.claude/` file path; never infer `/foundry:<old-name>` or `foundry:<old-name>` from a same-named local entity. Scan only `.claude/`, project `README.md`, and project `docs/`; classify documentation hits under the same rules below before any edit. If the actual entity lives under `plugins/`, use that plugin's declared namespace and restrict the scan to its `SOURCE_ROOT` plus its README; do not scan sibling plugins or project `docs/`. Source renames are currently rejected by Step 1's `--local` guard. The blocks below use project scope; for a plugin-source scan, substitute only its `SOURCE_ROOT` and README as the search targets in both commands.

```bash
rg -n -w --fixed-strings '<old-name>' .claude/ README.md docs/ 2>/dev/null \
  || grep -rn "\b<old-name>\b" .claude/ README.md docs/ 2>/dev/null \
  | grep -v ".git/" | grep -v "__pycache__"  # timeout: 10000
```

Grep returns **zero hits**: report "✓ No remaining occurrences of `<old-name>` found." Proceed.

**Large hit set gate** — hits exceed 50: invoke `AskUserQuestion` before classifying: "Found N occurrences of `<old-name>` — this name may be too generic for safe automated classification. Proceed with classification or abort?" Options: (a) Proceed · (b) Abort. On abort: stop, report to user.

Hits within limit: read a 5-line context window (2 lines before + matched line + 2 lines after) per hit with Read tool, assign each hit a stable integer `id` (1…N). Spawn `Agent(subagent_type="general-purpose", model="haiku", name="rename-hits-<batch>", description="Classify scoped rename references", prompt="<batch prompt below>")` for batches of ≤30 hits. Run batches sequentially, one in flight; overwrite `.temp/manage/agent-watch-manage.tsv` with `rename-hits-<batch>\t-\t900` in each spawn response and apply the constants deadline check before consuming that batch. Pass the location-derived canonical forms above as `<entity_context>`; include explicitly excluded same-named plugin/local entities.

Haiku agent prompt (one spawn per batch of ≤30 hits):

```
Classify grep hits for a rename: `<old-name>` → `<new-name>` (type: <agent|skill|rule|hook>).
Canonical surface forms for this entity: <entity_context>

For each hit output exactly one JSON object per line (no prose):
{"id":<N>,"file":"...","line":<N>,"verdict":"genuine"|"false_positive"|"ambiguous","reason":"one sentence"}

Classification rules — word match alone is NOT sufficient; read context:
- genuine: matches a canonical surface form; clearly names this specific entity (slash-command, subagent_type, NOT-for/TRIGGER cross-ref, dispatch directive, README table row)
- false_positive: generic English word used differently, unrelated comment, example string, sentence where the word means something else entirely
- ambiguous: context too short, name too generic, or evidence conflicts

Hits:
--- HIT {id} ---
file: {file}
line: {line}
context:
  {line-2}: ...
  {line-1}: ...
> {line}:   <matched line>
  {line+1}: ...
  {line+2}: ...
```

**JSON parse fallback**: malformed JSON or missing `id` fields in output: retry once, appending parse error to prompt. On second failure, mark all unresolved hits `"ambiguous"`, escalate to user.

Collect all batch results. Classify each hit:

- **Genuine reference** → Count files containing confirmed genuine references. **≤3 files**: apply Edit tool fixes targeting exact tokens at classified lines. **>3 files**: spawn `foundry:curator` sequentially with the Step 5 prompt listing only classified genuine file:line occurrences, exact tokens and substitutions; track its normal 900 s deadline. Never replace all bare-name matches in a file. Re-read each target line before editing and require its classified context to remain unchanged; otherwise reclassify it. The curator must leave false positives and ambiguous hits untouched.
- **False positive** → Skip; log haiku's reason.
- **Ambiguous** → Collect for user escalation.

After all haiku fixes and user-resolved fixes applied, run one final grep to confirm:

```bash
rg -n -w --fixed-strings '<old-name>' .claude/ README.md docs/ 2>/dev/null \
  || grep -rn "\b<old-name>\b" .claude/ README.md docs/ 2>/dev/null \
  | grep -v ".git/" | grep -v "__pycache__"  # timeout: 10000
```

Remaining hits must exactly equal documented false-positive set (by file+line). Any remaining hit not in false-positive list is unresolved genuine reference — loop classification once more for those, or flag in Step 10 as requiring manual review.

Collect ambiguous hits, invoke `AskUserQuestion` — one question per hit. Keep the file:line and small renaming diff in the question text as plain old → new text, with the decision: "Is this a real reference to `<old-name>` that should be updated, or a false positive?" Keep the question text to at most 6 plain lines (target 600 characters, hard cap 1,500), with no table, no pipe layout and no code fence. A longer diff or the full 5-line context goes to `.temp/manage/rename-hit-<id>.md` first and is printed once, rendered as normal Markdown, as the last reply text before the question call; its question text still carries file:line, the proposed change and `→ .temp/manage/rename-hit-<id>.md`. Options: real reference · false positive, label plus a one-line action description, no `preview`. Batch max 4 per call; loop if more; when batched, save each long hit first and print those hits together once immediately before the call. Apply user-confirmed fixes before final grep.
