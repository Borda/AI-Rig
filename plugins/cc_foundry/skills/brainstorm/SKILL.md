---
name: brainstorm
description: Iterative brainstorming skill for turning fuzzy ideas into approved tree documents. Diverges into branches, deepens and prunes them over many rounds, saves a tree doc. Run breakdown on the tree to distill it into a spec via guided questions.
argument-hint: <fuzzy idea or feature goal> [--tight|--deep] [--type <type>] [--keep "<items>"] | breakdown <tree-or-spec-file>
disable-model-invocation: true
allowed-tools: Read, Write, Bash, Grep, Agent, TaskCreate, TaskUpdate, TaskList, AskUserQuestion, Skill
effort: medium
---

<objective>

Turn unformed idea into branching exploration tree, then distill into spec. Idea mode = pure divergence — grow tree of directions, deepen promising branches, prune others, save result. No premature convergence. Run `breakdown` on tree when ready: asks distillation questions, writes spec section-by-section. NOT for implementation or code-gen — see `develop` plugin (requires `develop` plugin).

> **HARD GATE:** Do NOT take any implementation action — writing code, creating files, scaffolding — until user approves design (spec). Applies regardless of perceived simplicity. Simple idea can have short tree and spec, but process never skipped. </objective>

<inputs>

- **$ARGUMENTS**: required — fuzzy idea, goal, or feature request in any form; one sentence enough

- **`--tight`** — reduced-ceremony mode: see per-step caps below — 5/5/1 bounds vs default 10/10/2. Good for well-scoped ideas where problem already understood.

- **`--deep`** — extended-ceremony mode: 15/15/3 bounds vs default 10/10/2. Good for ambiguous problems where more exploration valuable.

- Default (no flag): behaviour unchanged — 10/10/2 bounds.

- **`--type <type>`** — optional type hint for idea mode. One of: `application` (app/service with users/endpoints), `workflow` (automation, pipeline, script), `utility` (helper library, tool, CLI), `config` (`.claude/` agents/skills/rules), `research` (investigation, survey, experiment design). Affects Step 1 scan patterns and Step 2 question framing. Omit if unsure — skill works without it.

- **`breakdown <tree-or-spec-file>`** — breakdown mode: read already-saved tree (`Status: tree`) or spec (`Status: draft`). For tree: ask distillation questions, write spec section-by-section. For spec: scan for blocking open questions then generate ordered action plan. Skips Steps 1–6 entirely.

</inputs>

<compaction>
- Key boundary 1: end of Step 4 (tree doc saved to .plans/blueprint/), before Step 5 tree review.
- Preserve at boundary 1: tree file path (.plans/blueprint/YYYY-MM-DD-<slug>.md), sidecar path (if viewer active).
- Terminal path: Step 6 option (a) approval — suggest breakdown and stop.
</compaction>

<workflow>

**Task hygiene**: load and follow the protocol below.

```bash
# loads: compaction-contract.md
# audit-skip: resilience-replication
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/load_shared_doc.py" foundry skills/_shared task-hygiene.md  # timeout: 5000
```

**Task tracking**: Before Step 1, create TaskCreate entries for all 6 steps (context scan, clarifying questions, build tree, save tree, tree review, present + gate). Then print session plan to user:

> **Brainstorming: \<goal from $ARGUMENTS>** Plan: context scan → clarifying questions → build tree → save tree doc → review → approval gate. Starting with a codebase scan...

## Step 0: Parse flags

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
KEEP_ITEMS=""
if [[ "$ARGUMENTS" =~ --keep[[:space:]]\"([^\"]+)\" ]]; then
    KEEP_ITEMS="${BASH_REMATCH[1]}"
fi
ARGUMENTS=$(echo "$ARGUMENTS" | sed 's/--keep "[^"]*"//g')
rm -f .temp/state/skill-contract.md  # clear stale contract (compaction-contract.md §Lifecycle)  # timeout: 5000
echo "$KEEP_ITEMS" > "${TMPDIR:-/tmp}/brainstorm-state-keep-items-${CSID}"
```

## Step 1: Context scan

**Unsupported flag check** — after all supported flags extracted (`--tight`, `--deep`, `--type`, `--keep`), scan `$ARGUMENTS` for remaining `--<token>` tokens. If found: print `` ! Unknown flag(s): `--<token>`. Supported: `--tight`, `--deep`, `--type`, `--keep`. `` then invoke `AskUserQuestion` — (a) **Abort** (stop, re-invoke with correct flags) · (b) **Continue ignoring** (skip unknown flags, proceed). On Abort: stop.

Gather project context before asking anything:

- Read `README.md` and relevant files under `docs/`
- Grep for keywords from `$ARGUMENTS` across `src/` or project root
- Identify: related code that already exists, stated non-goals in docs, prior design decisions

**Type-aware scan patterns** (when `--type` declared):

- `application`: look for existing routes, controllers, components, API endpoints, auth middleware
- `workflow`: look for existing scripts, pipelines, CI configs, scheduled jobs, automation files
- `utility`: look for existing utils/, helpers/, lib/ directories and similar functions
- `config`: look for `.claude/` agents, skills, rules, and `settings.json` entries
- `research`: look for existing notes, benchmarks, prior experiment results, and related papers/tickets

When no `--type` declared, perform generic scan.

**Existing codebase guidance**: when project has existing code, note patterns in use (naming, architecture, data flow) — Step 3 branches follow established patterns unless idea explicitly requires changing them. Where existing code has problems affecting work (e.g., file grown too large, unclear boundaries), note as open threads — do not propose unrelated refactoring, but flag targeted improvements serving current goal.

Goal: understand constraints so questions targeted, not generic. If idea already exists or out of scope, say so immediately and stop.

**Scope check**: before asking clarifying questions, assess request size. If idea describes multiple independent subsystems (e.g., "build a platform with chat, file storage, billing, and analytics"), flag immediately — do not spend questions refining details of oversized scope. Help user decompose into sub-ideas: what are independent pieces, how do they relate, what order to tackle them? Then proceed with first sub-idea through normal idea mode flow.

**Live viewer init**: ask user whether to create live viewer before proceeding. Call `AskUserQuestion`:

- question: "Create live tree viewer for this session?"
- (a) label: `Yes — create viewer` — description: create JSON sidecar and serve tree viewer in browser
- (b) label: `No — skip viewer` — description: proceed without viewer; tree still saved to disk at Step 4

On **(b)**: skip viewer creation and the print launch note below; set `SIDECAR=""` and skip all Write-to-sidecar steps throughout (Steps 3–4).

On **(a)**:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# timeout: 3000
mkdir -p .plans/blueprint
SIDECAR=".plans/blueprint/brainstorm-$(date -u +%Y-%m-%dT%H-%M-%SZ).json"
echo "$SIDECAR" > "${TMPDIR:-/tmp}/brainstorm-state-sidecar-${CSID}"
echo "$SIDECAR"
```

**Persistence note**: shell variables do NOT persist across separate Bash calls. The `echo "$SIDECAR" > "${TMPDIR:-/tmp}/brainstorm-state-sidecar-${CSID}"` step above writes the path to a state file. At the top of every subsequent Bash block that references `$SIDECAR` (Steps 3–4), re-read it:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r SIDECAR < "${TMPDIR:-/tmp}/brainstorm-state-sidecar-${CSID}" 2>/dev/null || SIDECAR=""
```

If `$SIDECAR` is empty after re-read, treat as viewer opt-out and skip all sidecar Write steps.

Record `$SIDECAR` path — referenced in Steps 3 and 4. Write initial JSON to that path using the Write tool:

```json
{
  "schema_version": 1,
  "session_status": "active",
  "updated_at": "<current ISO timestamp>",
  "session": { "title": "<raw $ARGUMENTS text>", "slug": "", "started_at": "<current ISO timestamp>" },
  "tree": { "id": "root", "label": "<raw $ARGUMENTS text>", "status": "open", "core_idea": "", "tension": "", "trades_away": "", "skill_lean": "", "children": [] },
  "ui": { "active_node_id": null, "last_error": null }
}
```

On write failure: log `> Viewer write failed: <reason>` inline and continue.

Print launch note:

> **Live tree viewer**: resolve scripts dir (works both pre- and post-install — `$CLAUDE_PLUGIN_ROOT` points at the installed cache when the plugin is installed; the fallback supports development against the source tree):
>
> ```bash
> _BRAINSTORM_SCRIPTS="${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/skills/brainstorm/scripts"
> echo "Viewer HTML: $_BRAINSTORM_SCRIPTS/tree-viewer.html"
> ```
>
> Because the viewer lives under the plugin cache (read-only) while `$SIDECAR` lives under the project's `.plans/blueprint/`, the static server's document root must cover both paths. Easiest reliable option: serve from `$HOME` with `python -m http.server 8080 --directory "$HOME"` (or `npx serve "$HOME"`), then open `http://localhost:8080/<relative-path-from-HOME-to-tree-viewer.html>?state=<absolute-or-HOME-relative-sidecar-path>`. If you prefer serving from the project root, copy or symlink the viewer HTML into a project-local directory first so the URL path resolves under the same document root as `$SIDECAR`.

## Step 2: Clarifying questions

Use `AskUserQuestion` for every clarifying question — renders interactive prompt inline, not plain text.

Rules:

- **Hybrid pacing**: the FIRST `AskUserQuestion` call batches the 2–3 orientation questions whose framing does not depend on any prior answer — the type-aware openers below when `--type` is declared, otherwise the broadest scoping questions. Same batching pattern as `modes/breakdown.md` Step D2. Every question after that first call is asked **one at a time** — call `AskUserQuestion` once, wait for the answer, then decide whether another question is needed; those later questions are the ones that must adapt to what was just said, and batching them would forfeit that adaptation
- Always use **multiple-choice** options in `AskUserQuestion`: list lettered choices so user can reply with just "a", "b", or "c"; mark recommended option with **★** (e.g., `a) Option A ★ recommended`) so user has sensible default
- Maximum **10 questions** (5 in `--tight` mode, 15 in `--deep` mode) — after limit, proceed to Step 3 with what you have
- After question 3 (and every subsequent question), always include **escape hatch option**: `x) Enough questions — let's start building the tree` so user can move on if problem already well-defined
- No solution proposals during this step — only gather information
- After each answer — for the batched first call, once after its whole answer set returns — briefly restate updated problem understanding in 1–2 sentences before asking next question or proceeding — simple acknowledgment ("Got it", "Understood") does not count; restatement must name what is now known about problem (e.g., "So the goal is X and the constraint is Y.")
- After restatement, add skill's own perspective in blockquote labelled **Skill's read:** — 1–2 sentences on what directions this answer opens up, what it makes more or less likely, or what tension it surfaces. Active hypothesis, not neutral summary (e.g., `> **Skill's read:** This makes me think the core challenge is X, which points toward approaches like Y`). Write as skill speaking.

**Gate**: do not proceed to Step 3 until problem well-defined or maximum question count reached. Aim for at least 3 questions to build enough context for rich tree.

**Type-aware question framing** (when `--type` declared): lead with type-appropriate questions first:

- `application`: ask about users (who uses it?), scale, and integration points before general questions
- `workflow`: ask about triggers (what starts it?), inputs, outputs, and failure handling first
- `utility`: ask about callers (who uses this library/tool?), interface shape, and scope of responsibility
- `config`: ask whether this targets existing agent/skill or is new, and what gap it fills in current setup
- `research`: ask about hypothesis or question being investigated, and what constitutes useful finding

## Step 3: Build the tree

Full creative session — grow, deepen, and prune tree of directions. Tree is output; convergence happens later in `breakdown`. Runs as loop of **tree operations**.

### Pre-seeding exchange

Before presenting formal branches, run brief free-form idea exchange — 2–3 rapid rounds. Goal: surface intuitions about direction before committing to structure. Like tennis rally — Claude serves first, user returns, branches emerge from what lands.

1. State skill's opening hypothesis: 1–2 sentences on where problem looks most interesting or tricky. Not a branch — just a read.
2. Immediately present **3–5 initial branches** (see Seeding the tree below) in the same message — no separate round-trip. Each branch is numbered (1, 2, 3, …) and ★ on the most promising one.
3. Call a single `AskUserQuestion` for the **reaction choice** (≤4 options per call per AQQ cap): "How does this look?" with exactly four options: (a) ★ recommended — pick branches to focus on (reply naming 1–3 branch numbers in free text) · (b) Not quite — let me redirect (reply describing the redirect) · (c) add more branches first · (d) skip focus selection — start tree ops with all branches open. The branch list is in the message body for reference; users name branches by number rather than by sub-option letter so the AQQ stays at 4 options regardless of branch count.
4. Proceed to **Tree operations loop**:
   - (a) picked: user's free-text reply names 1–3 branches → set those as `▶️` focus; others remain `💭` open
   - (b) picked: regenerate 2–3 fresh branches reflecting the redirect and re-enter step 3 (one re-entry allowed; second redirect proceeds with whatever branches exist)
   - (c) picked: generate 2–3 additional branches with different framing and re-enter step 3
   - (d) picked: enter tree ops with all branches `💭` open and no initial focus

**Skip on re-entry**: when looping back from Step 6 (b) "Needs more exploration", skip BOTH the pre-seeding exchange AND the Seeding the tree section — go straight to the Tree operations loop with existing branches as starting state. Do NOT re-seed (do NOT present new top-level branches as if starting fresh); user's previous tree is the operand for re-entry operations (add, close, deepen, etc.).

### Seeding the tree

Present **3–5 initial branches** (top-level directions) in the same message as the opening hypothesis (see pre-seeding exchange step 2 above). For each include:

- **Name**: short label
- **Core idea**: 2–3 sentences — what makes this branch distinct
- **Tension it resolves**: which aspect of problem this branch prioritises
- **What it trades away**: what gets harder or left unsolved
- **Skill's lean**: short honest opinion — what makes branch interesting or worth exploring, and any reservation skill has about it (e.g., "Interesting because it sidesteps the auth problem entirely, but risky if the data model isn't flexible.")

**YAGNI filter**: when generating branches, actively prune speculative "we might need this later" directions — include only branches that directly address stated problem. Flag any branch requiring features or scale not mentioned in clarifying questions as "speculative" in its **What it trades away** line.

Write **Opening framing** paragraph (2–3 sentences): skill's initial read on problem space — core tension, which branch(es) most promising and why, one thing it's uncertain about. Not recommendation to converge — divergence still goal — but honest perspective to spark reaction.

The reaction AskUserQuestion is defined in pre-seeding exchange above (4 options: focus / redirect / more branches / skip). When the reaction is (b) redirect: generate 2–3 new branches incorporating the described direction. When (c) add more: generate 2–3 fresh branches with genuinely different framing.

User may select **1–3 branches** to mark as initial focus via the free-text reply to option (a). All other branches start as `💭` open too — not closed yet, just not initial focus.

After user selects initial focus, write sidecar immediately with all branch details populated — set `core_idea`, `tension`, and `trades_away` on every branch node before first tree operation begins. Do not wait for first operation to write branch content into sidecar.

### Tree operations loop

After seeding, enter operations loop. Each iteration:

1. Show current **tree summary** (see format below)

2. Write **Skill's moment** — 2–3 sentences of skill's current read: which open branches look most interesting and why, what closed branches revealed about problem, and what skill would explore next if it had a vote. Make specific to current tree state (refer to actual branch names by their labels). Gives user something to react to before choosing operation.

3. Call `AskUserQuestion` with **four operation-category** options (AQQ cap is 4 per call — pick a category here; if the category needs a specific operation, ask one follow-up AQQ enumerating the operations within it):

   - (a) **per-branch operation** — deepen, reject, accept, or merge a branch (follow-up AQQ enumerates the four per-branch operations and asks which branch)
   - (b) **tree-level operation** — add a new top-level branch, or reopen a closed one (follow-up AQQ enumerates the two tree-level operations)
   - (c) **back to idea stacking** — free-form exchange, then return here (no follow-up needed)
   - (d) **ready** — save tree and proceed to Step 4

   When (a) is picked, the follow-up AQQ (still capped at 4 options) lists:

   - a) deepen [branch name] — add sub-directions
   - b) reject [branch name] — close with a reason
   - c) accept [branch name] — mark as the chosen direction
   - d) merge [branch name] + [branch name] — combine into one

   When (b) is picked, the follow-up AQQ lists:

   - a) add a new top-level branch — explore a fresh angle
   - b) reopen [branch name] — revisit a closed branch

   For per-branch operations that need a branch name, ask the user for the branch number/name in the follow-up AQQ's free-text reply rather than enumerating each branch as a separate option (keeps every AQQ ≤ 4).

4. **Write viewer state** (after any operation except Ready; skip entirely if `$SIDECAR` is empty — viewer opt-out): overwrite `$SIDECAR` with current full tree state using Write tool; set `ui.active_node_id` to just-operated node's `id`; update `updated_at` to current ISO timestamp; update `session.title` to current brainstorm title. All branch objects in JSON — regardless of status (open, rejected, merged, resolved) — must retain `core_idea`, `tension`, and `trades_away` fields; these are set at seeding time and must not be dropped when branch status changes. On write failure: log `> Viewer write failed: <reason>` inline and continue; on next successful write, set `ui.last_error: "<reason>"`.

**Operations**:

- **Deepen (a)**: generate 2–3 sub-branches under named branch. Sub-branches use same format as top-level branches. Ask which one(s) to focus on. After executing, write 1–2 sentences reacting to what deepening this branch opens up — what new tensions or opportunities sub-branches reveal.
- **Reject (b)**: mark named branch as ⛔ (rejected) with user's reason shown after `—`. Add one-line entry to pruning log. Ask if reason captures it correctly before proceeding. After executing, write 1–2 sentences reacting to what rejecting this branch reveals — what it tells us about where exploration is headed.
- **Accept (c)**: mark named branch as ✅ (resolved) with note explaining why chosen as direction. Do NOT add to pruning log (accepted, not pruned). After executing, write 1–2 sentences on what this choice commits to — what it confirms and implicitly rules out.
- **Merge (d)**: synthesise two named branches into single hybrid branch; present merged description; mark originals as 🔗 with `[merged -> <number>: <new-branch-name>]` immediately in tree summary shown after merge, and in all subsequent tree summaries. When writing merged branch state to sidecar, use field name `merged_into_id` with value equal to target branch's `id` field (e.g., `"b6"`), not a label string. After executing, write 1–2 sentences on what merge suggests about where idea is heading — what synthesis makes clearer or harder.
- **Add (e)**: generate 1–2 fresh top-level branches with directions not yet represented in tree. After executing, write 1–2 sentences on why new angle matters — what gap it fills or challenges in existing branches.
- **Reopen (f)**: change ⛔ (rejected) or ✅ (resolved) back to 💭 (open) on named branch; note re-opening reason. After executing, write 1–2 sentences on what reopening this branch might change — what it puts back on table.
- **Idea stacking (g)**: pause tree operations and enter brief free-form exchange — same format as pre-seeding exchange (2–3 rounds max). Useful when exploration feels stuck or new angle just surfaced but isn't fully formed yet. After exchange, return to tree operations loop with any new angles incorporated as branches or sub-branches. Does NOT consume operation slot — counter unchanged.
- **Ready (h)**: exit loop, proceed to Step 4.

### Tree summary format

Always show tree summary **before** calling `AskUserQuestion`:

```text
Tree: <title>
├─ ▶️ Branch 1: <name>
│  ├─ 💭 1.1: <name>
│  └─ ⛔ 1.2: <name> — <reason>
├─ 💭 Branch 2: <name>
│  ├─ ▶️ 2.1: <name>
│  │  ├─ 💭 2.1.1: <name>
│  │  └─ ⛔ 2.1.2: <name> — <reason>
│  └─ ⛔ 2.2: <name> — <reason>
├─ ✅ Branch 3: <name> — <reason chosen>
└─ 🔗 Branch 4: <name> [merged -> <number>: <new-branch-name>]
Open: N · Rejected: N · Resolved: N · Merged: N
Legend: ▶️ active focus · 💭 open · ⛔ rejected · ✅ resolved/accepted · 🔗 merged
```

Use `├─`, `│  ├─`, `└─` for tree rendering. Show sub-branches indented one level per depth. Sub-branches use hierarchical dot notation: branch 2 splits into 2.1, 2.2, …; those split further into 2.1.1, 2.1.2, … Prefix each branch with status emoji: ▶️ for branch currently operated on (most recently deepened, or selected as initial focus during seeding), 💭 for all other open branches, ⛔ for rejected branches (show reason after `—`), ✅ for resolved/accepted branches (show reason after `—`), 🔗 for merged branches (show merge target as `[merged -> <number>: <new-branch-name>]`). Legend line always last.

### Loop bounds

- Maximum **10 operations** (5 in `--tight` mode, 15 in `--deep` mode) (round = one operation; idea stacking (g) does not count)
- After limit: show tree state, call `AskUserQuestion` with: a) Save tree as-is ★ recommended / b) Do 2 more operations then save
- **Gate**: do not proceed to Step 4 until user selects "Ready" or max reached with at least 2 rejected branches (1 in `--tight`, 3 in `--deep`); resolved/accepted branches do not count toward this minimum (they are the goal, not waste); if fewer than required rejected branches exist, prompt: "The tree has few rejected branches — consider rejecting 1–2 that are clearly not the right direction before saving."

## Step 4: Save tree

Assemble tree state and write to `.plans/blueprint/YYYY-MM-DD-<slug>.md` using Write tool (creates directory if absent). Slug derived from title (kebab-case, max 5 words). If file already exists at target path (e.g., same day, same slug after restart), append counter suffix (`-2`, `-3`, etc.) rather than overwriting.

```markdown
# <title>

**Date**: YYYY-MM-DD
**Status**: tree

## Root idea

[The original user input and the refined problem understanding built up during Step 2 — 2–3 sentences.]

## Branches

[For each branch in the tree, using this structure:]

### Branch N: <name> [open | rejected — <reason> | resolved — <reason> | merged into <name>]

**Core idea**: ...
**Tension it resolves**: ...
**What it trades away**: ...

[Sub-branches nested as H4 headings if present:]

#### N.1: <sub-branch name> [open | rejected — <reason> | resolved — <reason>]

**Core idea**: ...

[Deeper splits nest as H5 headings, e.g. `##### N.1.1: <name>`]

## Pruning log

- Branch N rejected: <reason>
- Sub-branch N.1 rejected: <reason>
[One bullet per rejected or merged branch (NOT resolved/accepted), in the order they were closed.]

## Resolved branches

- Branch N accepted: <reason/decision note>
[One bullet per ✓ resolved branch.]

## Open threads

[Unanswered questions, untested combinations, and constraints that surfaced during Step 3. Each thread is a one-line bullet.]
```

**Gate**: do not proceed to Step 5 until file written and path confirmed.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
_TREE_FILE=$(find .plans/blueprint -name "*.md" -maxdepth 1 -not -name "*-spec.md" 2>/dev/null | sort -r | head -1)
IFS= read -r _SIDECAR < "${TMPDIR:-/tmp}/brainstorm-state-sidecar-${CSID}" 2>/dev/null || _SIDECAR=""
IFS= read -r _KEEP < "${TMPDIR:-/tmp}/brainstorm-state-keep-items-${CSID}" 2>/dev/null || _KEEP=""
_PRESERVE="tree-file=$_TREE_FILE"
[ -n "$_SIDECAR" ] && _PRESERVE="$_PRESERVE, sidecar=$_SIDECAR"
[ -n "$_KEEP" ] && _PRESERVE="$_PRESERVE; user-keep: $_KEEP"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/write_skill_contract.py" "foundry:brainstorm" "tree-review (after tree saved to disk)" "n/a" "$_PRESERVE" "curator tree review (Step 5) → approval gate (Step 6)"  # timeout: 5000
```

**Sidecar finalise** (skip if `$SIDECAR` is empty — viewer opt-out): using Write tool, write full current JSON content (same as `$SIDECAR`) with `session_status: "complete"` to `.plans/blueprint/<final-slug>.json` (same slug as `.md` file, `.json` extension). Then also overwrite `$SIDECAR` with `session_status: "complete"`. Do NOT move or rename `$SIDECAR` — open browser tabs keep polling original timestamp-slug path.

## Step 5: Tree review

Before spawning, pre-compute output path:

```bash
# timeout: 3000
TS=$(date -u +%Y-%m-%dT%H-%M-%SZ)
mkdir -p ".temp/brainstorm/$TS"
OUTPUT_PATH=".temp/brainstorm/$TS/curator-review.md"
```

Spawn **foundry:curator** with tree-focused prompt. Substitute `$OUTPUT_PATH` value (pre-computed above) for `<output-path>` template slot and the actual tree file path for `<tree-file>` before passing prompt — do NOT pass literal `$OUTPUT_PATH` variable name or the bare `<output-path>` / `<tree-file>` placeholder strings in the prompt string. Example substitutions: `<output-path>` → `.temp/brainstorm/2026-05-20T10-30-00Z/curator-review.md`; `<tree-file>` → `.plans/blueprint/2026-05-20-my-idea.md`:

```markdown
Read .plans/blueprint/<tree-file>. Audit for tree quality only (do NOT audit `.claude/` config files — scope is the brainstorm tree only):
- Root idea: is the original problem clearly stated in the "Root idea" section?
- Branch depth: do open branches have enough detail (not just a name)?
- Closure quality: are closure reasons substantive (not just "not chosen" or "skipped")?
- Coverage: are there obvious high-level directions completely missing from the tree?
- Open threads: are there unresolved questions worth capturing?
Write your full findings to <output-path> using the Write tool. The file must begin with a YAML metadata block:
---
Brainstorm Review — [tree topic]
Date:     [YYYY-MM-DD]
Verdict:  READY | NEEDS_REFINEMENT | BLOCKED
Findings: [N]
Confidence: [score] — [key gaps]
Next steps: /foundry:manage create | /develop:feature (requires `develop` plugin)
Path:       → <output-path>
---
Then the full findings below.
Return ONLY a compact JSON envelope: {"status":"done","findings":N,"severity":{"critical":N,"high":N,"medium":N,"low":N},"file":"<path>","confidence":0.N,"summary":"<one-line>"}
```

**Passive health monitoring**: Agent tool is synchronous — Claude awaits curator's response natively. If foundry:curator does not return within 15 min, surface any partial output already written to `$OUTPUT_PATH` (under `.temp/brainstorm/`) with ⏱ marker and continue to Step 6 with incomplete review noted.

> Note: synchronous Agent calls do not support mid-call extensions per CLAUDE.md §6 — simplified monitoring is intentional for synchronous spawns.

If `findings > 0`: add missing details, improve closure reasons, or add open threads as needed — loop back to Step 5 (max 2 revision cycles per Step 6 approval cycle; counter resets each time Step 3 re-entry is triggered from Step 6 option b). After 2 cycles with remaining findings, surface unresolved issues to user and proceed to Step 6 anyway.

**Gate**: do not proceed to Step 6 until `findings == 0` or 2 revision cycles exhausted.

## Step 6: Present and gate

Show tree file path and compact tree summary (same format as Step 3). Then call `AskUserQuestion` tool — do NOT write options as plain text first. Map options directly into tool call arguments:

- question: "How does the exploration tree look?"
- (a) label: `Tree looks good — ready to distill` — description: proceed to distillation (★ recommended)
- (b) label: `Needs more exploration` — description: describe what to add or close; loop back to Step 5
- (c) label: `Start over` — description: back to clarifying questions

**Gate**: do not exit until user approves.

On (b): return to Step 3 with existing tree state — add requested branches or close specified ones, then loop back to Step 5. Use reduced cap of **3 additional operations** for this re-entry (not fresh full budget reset); cap resets only at start of Step 3, not on re-entry. On (c): loop back to Step 2. (Max 3 approval cycles as guideline — track in context; if 3 cycles pass without convergence, surface unresolved concerns to user.)

On approval, suggest: `/brainstorm breakdown .plans/blueprint/<file>` to distill tree into spec.

```bash
rm -f .temp/state/skill-contract.md  # clear contract — skill complete (compaction-contract.md §Lifecycle)  # timeout: 5000
```

> Tree file is durable record of exploration. Share with teammates or use as context for future `/brainstorm` sessions on related ideas.

## Mode: Breakdown

**Trigger**: `$ARGUMENTS` starts with `breakdown ` followed by a tree-or-spec file path. Skips Steps 1–6 entirely.

Resolve the modes dir, then read and execute `breakdown.md` — it carries distillation mode (Steps D1–D4) and action plan mode (Steps B1–B3):

```bash
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/load_mode.py" brainstorm modes breakdown.md  # timeout: 5000
```

> loads: modes/breakdown.md Execute the mode loaded above matching the file's `**Status**:` field (`tree` → distillation; `draft` → action plan).

</workflow>

<notes>

- **No code at any point** — skill produces tree documents and specs only; implementation out of scope
- **`disable-model-invocation: true`** — slash-only entry; skill requires literal $ARGUMENTS (idea text or `breakdown <file>` path); auto-dispatch from other skills would receive no arguments and fail
- **foundry:curator scope in Step 5** — spawn prompt must constrain scope to tree quality explicitly; do not let it audit `.claude/` config files
- **.plans/blueprint/ directory** — created if absent; filenames use `YYYY-MM-DD-<kebab-slug>.md` format; tree files use base slug; spec files append `-spec` to slug to avoid collision
- **Status field**: tree documents use `Status: tree`; spec documents use `Status: draft`; breakdown auto-detects which path to take
- **Breakdown heading convention**: distillation mode uses D-prefix steps (D1–D4); action plan mode uses B-prefix steps (B1–B3)
- **Exploration notes in spec**: Section 6 derived from tree's Pruning log — intentional context for future readers; do not remove in foundry:curator review
- **Interaction budget**: idea mode — worst case: 10 (`--tight`) / 20 (default) / 30 (`--deep`) (pre-seeding+branch selection merged to 1 call, saves ~1; Step-2 hybrid pacing batches the first 3 orientation questions into 1 call, saving 2 more); breakdown distillation — max 2 calls (D2 batched) + 1 call (D3 full-spec approval) ≈ 3–5; typical sessions use ~6–12 total AskUserQuestion calls across both. <!-- Branch-path audit: confirm no single branch through the workflow asks more than 4 AskUserQuestion calls in one response — communication.md 4-question-per-call cap is per invocation, not per branch; multiple sequential AskUserQuestion calls in one branch path are permitted but should be reviewed for batching opportunities. -->
- **Flag modes**: `--tight` / `--deep` scale question and operation caps (5/15 vs default 10); `--type` enables type-aware scan and question framing in Steps 1–2; flags apply to idea mode only, ignored in breakdown
- **Follow-up**: after spec approval in distillation mode → if targeting `.claude/` config: `/foundry:manage update <name> <spec-file>`; for application or mixed changes: `/brainstorm breakdown .plans/blueprint/<spec-file>` for action plan
- **Rejected vs resolved distinction**: ⛔ marks branches dismissed as wrong direction; ✅ marks branch explicitly chosen as direction. Resolved branches do not count toward minimum-rejected-branches gate — they are the goal. Pruning log captures rejected only; resolved branches go in separate "Resolved branches" section.
- **Idea stacking (g) vs pre-seeding exchange**: both are free-form tennis rallies but serve different purposes — pre-seeding runs once before branches exist to seed directions; idea stacking can be invoked at any point during tree ops when exploration feels stuck or half-formed thought needs to be batted around before committing to a branch. Neither consumes an operation slot.
- **Confidence block**: idea mode is conversational session and produces a file (not an inline report) — Confidence block requirement from CLAUDE.md output standards applies to analysis responses only; omitted by design for Steps 1–6. foundry:curator spawn in Step 5 returns its own Confidence block, surfaced in review but not re-emitted to user.

</notes>
