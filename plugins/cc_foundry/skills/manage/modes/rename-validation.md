# Mode: Rename Occurrence Validation

<!-- file: rename-validation.md — consumers: manage/SKILL.md -->

Triggered after cross-reference propagation (Step 5, rename mode only). Scan for remaining occurrences with word-boundary matching to cut noise from short/common names:

```bash
rg --fixed-strings -n '\b<old-name>\b' plugins/ .claude/ README.md docs/ 2>/dev/null \
  || grep -rn "\b<old-name>\b" plugins/ .claude/ README.md docs/ 2>/dev/null \
  | grep -v ".git/" | grep -v "__pycache__"  # timeout: 10000
```

Grep returns **zero hits**: report "✓ No remaining occurrences of `<old-name>` found." Proceed.

**Large hit set gate** — hits exceed 50: invoke `AskUserQuestion` before classifying: "Found N occurrences of `<old-name>` — this name may be too generic for safe automated classification. Proceed with classification or abort?" Options: (a) Proceed · (b) Abort. On abort: stop, report to user.

Hits within limit: read a 5-line context window (2 lines before + matched line + 2 lines after) per hit with Read tool, assign each hit a stable integer `id` (1…N). Spawn a **`haiku`-model** `Agent` to classify in batches of ≤30 hits — pass `model="haiku"` explicitly. Before spawning, resolve entity's canonical surface forms from rename context: slash-command form (`` `/foundry:<old-name>` `` or `` `/<old-name>` ``), `subagent_type` value, file-path pattern (`.claude/agents/<old-name>.md`, `.claude/skills/<old-name>/`). Include as `<entity_context>` in prompt.

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

- **Genuine reference** → Apply Edit tool fix targeting exact token at classified line — do NOT use `replace_all: true` on whole file; replace only that line's occurrence.
- **False positive** → Skip; log haiku's reason.
- **Ambiguous** → Collect for user escalation.

After all haiku fixes and user-resolved fixes applied, run one final grep to confirm:

```bash
rg --fixed-strings -n '\b<old-name>\b' plugins/ .claude/ README.md docs/ 2>/dev/null \
  || grep -rn "\b<old-name>\b" plugins/ .claude/ README.md docs/ 2>/dev/null \
  | grep -v ".git/" | grep -v "__pycache__"  # timeout: 10000
```

Remaining hits must exactly equal documented false-positive set (by file+line). Any remaining hit not in false-positive list is unresolved genuine reference — loop classification once more for those, or flag in Step 10 as requiring manual review.

Collect ambiguous hits, invoke `AskUserQuestion` — show file + 5-line context per hit, ask: "Is this a real reference to `<old-name>` that should be updated, or a false positive?" Batch max 4 per call; loop if more. Apply user-confirmed fixes before final grep.
