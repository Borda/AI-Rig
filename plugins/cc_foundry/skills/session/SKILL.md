---
name: session
description: 'Session state that outlives a context reset — `dump` sweeps the live conversation and writes a compact handover doc (goal, decisions + why, lessons, standing instructions, files-touched table, outstanding items, next step), then prints `/clear`; the `session-restore.js` SessionStart hook re-injects it automatically. `park` stashes a diverging idea mid-session without derailing; `sweep` audits the conversation for unlanded work. TRIGGER when: user says "dump the session", "handover before clear", "save state before clearing", "carry this over", "I want to clear but keep the plan", "park this for later", "what did we defer", "anything unfinished before I close". SKIP: surviving auto-compact at 85% (that is the skill contract in `.temp/state/skill-contract.md` per `compaction.md`, written by the running skill); reviving a *finished* conversation (Claude Code native `/resume`).'
argument-hint: dump [name] | recall [name] | list | park <idea> | sweep | drop <item>
allowed-tools: Read, Write, Edit, Glob, Grep, Bash, TaskList, AskUserQuestion
effort: low
model: sonnet
---

<objective>

Carry the *durable* part of a session across `/clear`, hold open loops until they land. `dump` composes a small handover doc from the live conversation, writes it to `.claude/state/session/<slug>.md`, ends by printing `/clear`. The `session-restore.js` hook (SessionStart, matcher `clear`) injects that doc into the fresh session, so restore is free. Implementation detail — diffs, tool output, exploration transcript, abandoned approaches — dropped on purpose; only the decision that settled an approach survives.

Runs **inline, never `context: fork`**. Conversation history *is* the authoritative source for what changed, what was decided, what never landed; a forked run would see none of it.

NOT for: auto-compact survival (`compaction.md` skill contract); reviving a finished conversation (native `/resume`). Boundary table in the notes section below.

</objective>

<inputs>

- **$ARGUMENTS**: required. Six modes:
  - `dump [name]` — sweep, compose and write handover doc, set `LATEST` pointer, print `/clear` next step. `name` optional; unnamed dumps derive a slug from branch + UTC timestamp.
  - `recall [name]` — print a stored handover into context, mark it consumed. Named, or `LATEST` when omitted. Manual path for dumps the hook skips.
  - `list` — table of stored handovers (slug, age, consumed) plus open parked items.
  - `park <idea>` — append one open-loop item to `.claude/state/session/PARKED.md`. No dump needed; works any time.
  - `sweep` — read conversation for unlanded ideas, unanswered questions, pending tasks; report them, offer to park.
  - `drop <item>` — fuzzy-match a parked item, remove it, log the closure.

</inputs>

<constants>

- Store dir: `.claude/state/session/` (project-local; `.claude/state/` gitignored, same as `session-context.md`)
- Item store: `.claude/state/session/PARKED.md` — one bullet per open item; survives every dump, never auto-deleted
- Pointer file: `.claude/state/session/LATEST` — plain text, slug of most recent unconsumed dump. Blank or missing = nothing pending.
- Closure log: `.claude/state/session/dropped.jsonl` — one JSON line per `drop`
- Doc size cap: ~1.5K tokens (~75 lines). Compress prose, never drop a decision.
- Hook auto-restore window: unconsumed **and** `created` within 30 min. Outside it, `recall` is the manual path.
- Hook truncation threshold: ~8000 chars — above it hook injects `## Goal` + files table + `## Next step` + a `→ /foundry:session recall <slug>` pointer instead of the whole doc.
- Stale threshold: 14 days (`⚠ stale` prefix when listing) — applies to handover docs **and** parked items
- Delete threshold: 30 days — handover docs only, swept during `list`. **Parked items never auto-deleted**; only `drop` removes one.
- Files-table row cap: 25 (over that, group by directory, state elided count)

</constants>

<workflow>

**Task hygiene**: load and follow the protocol below.

```bash
# audit-skip: resilience-replication
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/load_shared_doc.py" foundry skills/_shared task-hygiene.md  # timeout: 5000
```

## Step 0: Validate and dispatch mode

Extract first word of `$ARGUMENTS` as `MODE`.

If `MODE` matches:

- `dump` (alias: `save`) → **Mode: dump**
- `recall` (aliases: `restore`, `load`) → **Mode: recall**
- `list` → **Mode: list**
- `park` → **Mode: park**
- `sweep` → **Mode: sweep**
- `drop` (alias: `archive`) → **Mode: drop**

**Unsupported flag check** — after extracting mode token, scan `$ARGUMENTS` for remaining `--<token>` patterns. Found: print `` ! Unknown flag(s): `--<token>`. Supported modes: dump, recall, list, park, sweep, drop. `` then invoke `AskUserQuestion` — (a) **Abort** (stop, re-invoke correctly) · (b) **Continue ignoring** (skip unknown flags, proceed with recognized mode).

Otherwise (empty, unrecognized, misspelled): use `AskUserQuestion`:

> "Which session mode did you want?"
>
> Options: (a) `dump [name]` — write the handover doc now, (b) `park <idea>` — stash one open loop without derailing, (c) `sweep` — audit the conversation for unlanded work, (d) `list` — stored handovers and open items

## Step 1 / Mode: dump

### Substep 1a: Derive slug and timestamp

```bash
git branch --show-current 2>/dev/null; date -u +%Y-%m-%dT%H:%M:%SZ; date -u +%Y-%m-%dT%H-%M-%SZ  # timeout: 5000
```

Three lines out: branch, ISO `created` stamp, filesystem-safe stamp. Slug: `<name>` from `$ARGUMENTS` when given (lowercase, non-alphanumerics → `-`); otherwise `<branch with / → ->-<filesystem-safe stamp>`. Empty branch (detached HEAD): `detached`. Reject a `name` containing `/` or `..` — re-ask via `AskUserQuestion` rather than writing outside store dir. `LATEST`, `PARKED`, `dropped` are reserved slugs; re-ask on those too.

### Substep 1b: Sweep for unlanded work

Run **Step 5 / Mode: sweep** internally, plus Read `.claude/state/session/PARKED.md` (skip silently if absent). Union becomes doc's `## Outstanding` section. Nothing found and no parked items: omit section.

### Substep 1c: Gather file evidence

```bash
git diff --stat HEAD 2>/dev/null; echo "--- committed since session start ---"; git log --name-only --pretty=format:'%h %s' --since="8 hours ago" 2>/dev/null | head -60  # timeout: 5000
```

> git parses the relative date itself — no BSD/GNU `date` flag split needed.

**Sourcing precedence for files table** — union of four sources, in this order:

1. **Conversation history — authoritative.** Every `Edit`/`Write` of this session is visible to this skill; that's the touched-file list, and the only source that also knows *what* each change was (`Change`) and whether it landed (`State`).
2. `git diff --stat HEAD` → `Ref` column (`+N/-M`). A file with no diff entry and no commit is `Ref: uncommitted-new`, or dropped when it was temp scratch.
3. `git log --name-only --since=…` → files already committed this session, no longer appearing in `git diff HEAD`.
4. `.claude/state/session-context.md` → `## Files Modified This Session` — **fallback only**. `task-log.js` writes that section from its PreCompact branch, firing at 85% auto-compact threshold; a session dumping before any compaction has no such section, or a stale one from a prior session. Read only when sources 1–3 come up empty.

**Column rules**: `Change` ≤6 words, what changed, never why (why belongs in `## Decisions`). `State` ∈ `done` / `wip` / `needs-test` / `reverted`. Cap 25 rows — over that, group by directory, state elided count explicitly, never silently truncate (`quality-gates.md`). Scratchpad and `.temp/` paths are never rows; belong in `## Artifacts`.

### Substep 1d: Gather artifacts and open tasks

```bash
find .temp .reports .experiments .developments -maxdepth 2 -type d -mtime -1 2>/dev/null | head -20  # timeout: 5000
```

Call `TaskList` for tasks still `in_progress` or `pending` — they feed `## Next step` and `## Outstanding`, not a section of their own (task state survives on its own).

### Substep 1e: Compose the doc

Fill this template. Omit any section with nothing real in it — an empty heading is noise next session pays for.

```markdown
---
slug: <slug>
created: <ISO8601-UTC>
consumed: false
branch: <git branch>
---

## Goal
<the task/plan in 1–3 lines>

## Decisions
- <decision> — why: <reason>

## Lessons / corrections
- <correction received> — rule going forward: <rule>

## Standing instructions
- <programmatic-level directive that must keep applying>

## Files touched

| File | Change | State | Ref |
| --- | --- | --- | --- |
| `path/to/a.py` | added retry guard | done | +42/-3 |
| `path/to/b.md` | README sync | wip | +8/-0 |

## Outstanding
- **<slug>** — <one-line summary>. Why: <one sentence>. Next: <what to ask or do>.

## Artifacts

| Path | Kind | Note |
| --- | --- | --- |
| `.reports/<skill>/<ts>/report.md` | report | consolidated findings |
| `.temp/<skill>/<ts>/` | run-dir | agent handover files |

## Next step
<single concrete next action>

## Dropped deliberately
implementation detail, tool output, exploration transcript
```

**Written in ultra-caveman tier** (`plugins/CLAUDE.md` §Writing Style) — this doc is re-injected into a fresh context on every restore, so every word is a recurring cost. Explicit exclusions: no diffs, no code bodies, no command output, no per-file reasoning, no history of abandoned approaches — only the decision that settled them.

### Substep 1f: Write the doc and the pointer

Use **Write tool** for both (it creates `.claude/state/session/` on demand; no `mkdir` permission gap):

1. `.claude/state/session/<slug>.md` — composed doc. A slug that already exists is overwritten only after `AskUserQuestion` confirms; otherwise append `-2`, `-3`, … .
2. `.claude/state/session/LATEST` — slug alone, one line.

`PARKED.md` is **not** cleared by a dump — doc copies items, doesn't consume them. An item leaves only via `drop`.

### Substep 1g: Print the next step

Terminal only, short. Confirm path, row count of files table, then:

```text
→ .claude/state/session/<slug>.md (N files, M decisions, K outstanding)
Next: clear the context. I cannot run it for you — Claude Code exposes no programmatic slash invocation, so the line below is yours to send. The SessionStart hook re-injects this doc automatically on the other side.

/clear
```

The literal `/clear` is the **last line of the reply** — nothing after it, so it's one copy away.

End with a `## Confidence` block per `quality-gates.md` — score on: files table backed by conversation evidence (not guessed), decisions captured with their reasons, sweep run before composing, next step concrete enough to act on cold.

## Step 2 / Mode: recall

### Substep 2a: Resolve the target

Named → `.claude/state/session/<name>.md`. Unnamed → read `.claude/state/session/LATEST` (Read tool), use the slug it holds; blank or missing: print `No pending handover. → /foundry:session list`, stop.

Missing target file: print the miss, fall through to a `list` render so user sees what does exist.

### Substep 2b: Print it into context

Read the doc, print its body verbatim, frontmatter stripped, under a one-line banner naming slug and age. **Terminal only — never route this to a file**: landing it in context *is* the mechanism, output-routing to `.temp/` would defeat it.

### Substep 2c: Mark consumed

1. Edit tool on the doc: `consumed: false` → `consumed: true` (frontmatter only).
2. Read `.claude/state/session/LATEST` (reuse the Substep 2a read when the target was unnamed) and compare its content to the resolved slug. **Only when they match**: Write tool on `.claude/state/session/LATEST` with empty content — hook treats blank and missing alike, so nothing re-injects on next `/clear`. When `LATEST` points to a different, still-unconsumed slug: leave it untouched — recalling an unrelated named doc must never silently drop the pointer to a different pending handover.

End with a `## Confidence` block per `quality-gates.md` — score on: target resolved unambiguously, doc printed intact, consumed marker written.

## Step 3 / Mode: list

### Substep 3a: Sweep expired handover docs (≥ 30 days)

```bash
find .claude/state/session -maxdepth 1 -name '*.md' ! -name 'PARKED.md' -mtime +30 -delete 2>/dev/null; echo "sweep done"  # timeout: 5000
```

> `PARKED.md` excluded by name — parked items have no TTL, only `drop` removes them.

### Substep 3b: Collect and render

Glob `.claude/state/session/*.md` **excluding `PARKED.md`** (it holds bullets, not frontmatter), Read each one's frontmatter for `slug`, `created`, `consumed`, `branch`. Age comes from `created`, not file mtime — marking a doc consumed rewrites the file, would reset mtime. Read `PARKED.md` separately for items table; its ages come from each bullet's `Raised:` date.

```markdown
## Session store — <today's date>

### Handovers

| Slug | Age | Branch | Consumed |
| --- | --- | --- | --- |
| `plan-x` | 12 min | main | no |
| `⚠ stale refactor-auth` | 16 d | feat/auth | yes |

### Parked (<N> open)

- [ ] **retry-backoff** — revisit exponential vs linear. Raised: 2026-08-10.
- [ ] ⚠ stale **split-ratio** — 80/20 vs 70/30 never settled. Raised: 2026-07-20.

→ /foundry:session recall <slug> to print a handover back into context
→ /foundry:session drop <item> to close a parked item
```

`⚠ stale` prefix at ≥14 days, both tables. Nothing in either: `No stored handovers, no parked items.`

End with a `## Confidence` block per `quality-gates.md` — score on: glob returned a result (even empty), every frontmatter parsed, ages computed from `created` / `Raised:`.

## Step 4 / Mode: park

Payload is everything after `park ` in `$ARGUMENTS`. Empty payload: run **Mode: sweep** instead, offer its findings as parking candidates, rather than asking for text conversation already holds.

Derive a short kebab slug from payload, then Read `.claude/state/session/PARKED.md` (absent: start a fresh file with `# Parked items` heading), Edit/Write one bullet appended at end:

```markdown
# Parked items

- **<short slug>** — <one-line summary>. Raised: <YYYY-MM-DD>. Why: <one sentence>. Next: <what to ask or do when revisiting>.
```

Date from `date -u +%Y-%m-%d` (fold into any bash call this step already makes). Slug already present: update that bullet rather than adding a near-duplicate, say so.

Print one line: `Parked: <slug>` plus current open count. Terminal only.

## Step 5 / Mode: sweep

Read the **conversation**, not the filesystem. Nothing to run — history already in context. Collect, in this order:

| Type | Trigger |
| -- | -- |
| Unanswered question | Claude asked, user sent a new top-level request instead of answering |
| Deferred exploration | "come back to that", "park this", "later" — idea named, not pursued |
| Diverging idea | feature/design idea raised while solving something else |
| Unfinished task | `TaskList` entry still `pending` / `in_progress` |
| Stated-but-unlanded | a change discussed and agreed, with no Edit/Write backing it in this session |

Call `TaskList` for fourth row. Detection stays **behavioural** — a new top-level request without an answer to prior question — never semantic-similarity scoring.

Render:

```markdown
## Unlanded — <N> items

| # | Item | Type | Why it did not land |
| --- | --- | --- | --- |
| 1 | retry backoff shape | deferred | user said "later, after the bench lands" |
```

Then `AskUserQuestion`: (a) park all · (b) park a subset (list the numbers) · (c) skip. Selecting (a) or (b) runs **Mode: park** for each chosen item in the same turn.

When a `session-restore.js`-injected handover doc is present at the start of this conversation (a freshly restored session), its `## Decisions` and `## Outstanding` sections are part of the candidate set too — an item listed there and not yet re-confirmed as landed in the live turns since restore still counts as stated-but-unlanded. Do not require post-restore Edit/Write evidence for something the restored doc's own `## Files touched` table already marked `done`.

Sweep sees the **current conversation only**. A finished session's unlanded ideas are unreachable — native `/resume` revives the conversation itself; this skill never mines transcript JSONL.

End with a `## Confidence` block per `quality-gates.md` — score on: every row traceable to a concrete conversation turn (not inferred), `TaskList` consulted, no already-landed work listed as outstanding.

## Step 6 / Mode: drop

### Substep 6a: Fuzzy-match the target

Match everything after `drop ` against slugs and summaries in `.claude/state/session/PARKED.md`. Ambiguous (2+ equally close): list them, `AskUserQuestion` to disambiguate. No match: render parked list, stop.

### Substep 6b: Remove the bullet and log the closure

Edit tool on `PARKED.md` — remove only matched bullet, leave rest untouched. Then append one audit line:

```bash
TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
jq -n --arg ts "$TS" --arg item "<matched slug>" '{"ts":$ts,"item":$item,"action":"dropped"}' >> .claude/state/session/dropped.jsonl  # timeout: 5000
```

> jq escapes the slug — a bullet holding quotes or braces can never break the log line.

Print `Dropped: <slug>` plus remaining open count. Terminal only.

End with a `## Confidence` block per `quality-gates.md` — score on: match unambiguous, only the matched bullet removed, audit line appended.

</workflow>

<notes>

**Why not one command.** A skill cannot invoke `/clear` — Claude Code exposes no programmatic slash invocation (that's Agent SDK `query()` only), and neither keybindings, `UserPromptSubmit`, nor `UserPromptExpansion` can trigger one either. So `dump` + `/clear` stays two steps; the mechanism buys its keep by making the *third* step (restore) automatic.

**Why inline, not forked.** `context: fork` skills can't read conversation history. Files table's `Change` and `State` columns, decisions, lessons, and the whole of `sweep` come from that history — a forked run would have nothing to write.

**Two state mechanisms — they do not overlap.**

| Mechanism | Survives | Trigger | Store |
| -- | -- | -- | -- |
| Skill contract (`compaction.md`) | in-flight skill phase state | auto-compact at 85% | `.temp/state/skill-contract.md` |
| Session handover (this skill) | plan, decisions, lessons, files table, open loops | explicit `dump` before `/clear` | `.claude/state/session/` |

**Restore is best-effort, dump is not.** Hook injects only when pointer exists, doc unconsumed, and under 30 minutes old — a deliberately narrow window, so an old dump never ambushes an unrelated session. Everything outside it is reachable by `/foundry:session recall <slug>`, which has no age gate. The four cases landing there: dumped and walked away (>30 min); doc over ~8K chars (hook injects head plus a pointer); fresh terminal rather than a `/clear` (matcher is `clear`); wanting the same doc a second time (first restore set `consumed: true`).

**`recall`, not `resume`.** Claude Code's native `/resume` revives a whole past conversation — strictly better recall than any document. This mode name stays clear of it deliberately; the two aren't alternatives.

**Parking is a command, not a behaviour.** The predecessor made parking an automatic behavioural rule documented only inside this file, which loads only on invocation — so it never fired, and store held zero items for four months. `park` is now an explicit mode with an explicit store. Nothing writes to `PARKED.md` unless user asks for it, no TTL deletes from it.

**Scope**: store is project-local — `.claude/state/session/` lives inside working tree, so nothing leaks across projects.

</notes>
