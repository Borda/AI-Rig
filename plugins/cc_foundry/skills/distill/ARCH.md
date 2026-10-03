<!-- file: ARCH.md — consumers: none (documentation; never loaded at runtime) -->

# `/foundry:distill` — execution architecture

> **Documentation, not contract.** The skill never loads this file; `SKILL.md` and `modes/*.md` are the only normative sources. It exists so the shape — what fans out, what joins, what blocks — can be read without walking a 5-mode dispatcher across five separate files.
>
> **Keep it current.** Any change to block order, gate placement, fan-out width or what runs beside what lands here in the same commit. Every schema block carries a `(step N)` tag naming its step; renumbering a step updates the tag and the index at the end. A schema that disagrees with `SKILL.md`/`modes/*.md` is worse than none — those files win every time.

## Legend

```
▣ agent spawn      ◆ blocking user gate      ‖ same turn
FAN n  work splits into n lanes dispatched together
JOIN   lanes collected; nothing past it starts until all land
```

## Schema

Distill is not one pipeline — it is a dispatcher. A common prefix always runs, then the first whitespace token of `$ARGUMENTS` routes to exactly one of five mutually-exclusive modes, each schema'd on its own below.

```
SETUP  (flag-parsing blocks, before step 1)
  parse --keep · --eager · --project (3 sequential bash blocks — state
  doesn't persist across Bash calls, output re-read as context each time)
  |
INVENTORY  (step 1)
  Glob agents/skills — project-local + plugin-source + installed-cache
  (runs unconditionally, even for modes that never consult it)
  |
ROUTE  (step 2 — mode-token checks)
  first token of $ARGUMENTS → default | prune | memory | external
                                       | executables
```

## Fan and join points

| Fans into | Mode | Lanes | Width bound by | Joins at |
| -- | -- | -- | -- | -- |
| FREQUENCY SCAN (3× git log) | default | 3 | fixed | Glob/read lessons |
| CLASSIFY | prune | working-set size | 2+ files triggers spawn; 1 file runs inline; no explicit width cap in skill text | branch on `--eager` |
| SCORE | prune — eager | working-set size | same as CLASSIFY | WHICH ENTRIES TO PRUNE gate |
| APPLY (curator) | prune — eager | selected-project count | none stated in skill text | summary print |
| ENRICH | memory | selected-project count | `--project` selection, else every project found | CLUSTER + CLASSIFY |
| READ LOCAL ROSTER ‖ READ INSTALLED PLUGINS | external | 2 | fixed | CAPABILITY MAP |
| REVIEW .md ‖ REVIEW code | external | ≤2 | non-empty file-type group from APPLY's changed-file list | Confidence block |
| SCAN (inside LOCATE) | executables | plugin-dir count | only fires when no prior Check 33 report exists | CHECK33_FILES update, then PARSE CANDIDATES |
| EXTRACT | executables | selected-cluster count | one worktree per agent (`isolation: worktree`); no explicit width cap in skill text | RE-AUDIT |
| RE-AUDIT | executables | modified-file count | none stated in skill text | MEASURE + CONVERGE |

No mode states an explicit concurrency ceiling on its own fan-out (e.g. no `DISPATCH_MODE`-style wave batching) — every width above is bounded only by the size of the underlying dataset (projects, clusters, or files).

## Gates

| Gate | Mode | Always? | Blocks |
| -- | -- | -- | -- |
| PROJECT PICKER | prune, memory | conditional (`--project`) | which memory/project files enter the working set |
| WHICH ENTRIES TO PRUNE | prune — eager branch | always, inside eager branch | APPLY (curator) spawn |
| APPLY PRUNE EDITS? | prune — standard branch | always, inside standard branch | native Edit APPLY |
| APPLY PROPOSALS? | memory | always | native Write/Edit APPLY, then REVIEW |
| APPLY EXTERNAL SOURCE CANDIDATES? | external | always | APPLY step |
| EXTRACT CANDIDATES TO bin/? | executables | always | EXTRACT spawns |
| convergence-loop stop | executables | conditional — plateau/non-convergence or stricter caller-budget stop, open findings | COMMIT |

Default mode reaches no gate at all. Every other mode reaches exactly one always-on gate per branch taken, at most two total (PROJECT PICKER, then the branch's own apply gate).

## Mode branches

### Mode: default (no mode token matched)

```
FREQUENCY SCAN  (step 2)
  +----------------- FAN 3 -----------------+
  |              |              |           |
  git log        name-freq      author-freq
  (recent 50)    (changed-file) (author)
  |              |              |           |
  +----------------- JOIN ------------------+
  |
  Glob .plans/active/todo_*.md → read each
  read .notes/lessons.md
  write skill contract (compaction boundary)
  |
GAP ANALYSIS  (step 3)
  compare patterns against INVENTORY roster
  |
DUPLICATION CHECK  (step 4)
  overlap + anti-pattern checklist
  |
REPORT  (step 5)
  Agent/Skill Suggestions + Confidence block
```

Fully sequential except the one marked FAN. No gate, no agent spawn — the only one of the five modes that never asks and never dispatches.

### Mode: prune (`prune [--eager]`)

```
FIND MEMORY FILES  (§ Find memory file, § Short-circuit)
  find MEMORY.md under ~/.claude/projects
  none found → PRUNE_ABORT, stop (no gate, no spawn)
  |
◆ PROJECT PICKER  (§ If PROJECT_FLAG)        [--project only]
  |
CLASSIFY  (§ Parallel analysis across projects)  ▣ 1 per project — Drop/Trim/Keep
  width = working-set size; single-file set runs inline instead
  |
  branch on --eager — the two tracks below are mutually exclusive
  |
-- eager ----------------------------------------------------------
SCORE  (step P-eager-1)  ▣ 1 per project — 2-axis Usage×Impact
  width = working-set size; single-file set runs inline instead
  print consolidated scored table
◆ WHICH ENTRIES TO PRUNE  (step P-eager-2)  tier or item numbers
APPLY  (step P-eager-3)  ▣ foundry:curator per project — selected items only
  print summary

-- default (standard) ----------------------------------------------
ADVISORY REPORT  (steps P1, P2)  read all memory files, print proposal
◆ APPLY PRUNE EDITS?  (step P3)
APPLY  (step P3, on approval)  Edit tool per project (native, parallel) — only on approval
  print summary
```

Only one track runs per invocation, selected by `--eager` at CLASSIFY's join. Both close on the same shape: one gate, one apply, one summary print.

### Mode: memory (`memory [--eager]`)

```
COLLECT  (step L1)
  read .notes/lessons.md
  enumerate memory dirs + feedback-file counts
  |
◆ PROJECT PICKER  (step L1)                   [--project only]
  |
  read feedback_*.md per project · read .claude/rules/*.md
  |
  +--------------------- FAN n -----------------------+
  |   n = selected project count (or all, no --project)|
ENRICH  (step L1b)
  per-project root resolution + CLAUDE.md/git-log/plans read
  |
  +--------------------- JOIN -------------------------+
  |
CLUSTER + CLASSIFY  (step L2)
  group lessons by domain · disposition · dup/contradiction check
  |
PROPOSALS  (step L3)
  print proposal table (no writes yet)
  make run dir → conflict pre-check (parallel Grep across proposals)
  |
◆ APPLY PROPOSALS?  (step L4)
  |
APPLY  (step L4)
  native Write/Edit → cross-reference check → git diff gate
  |
REVIEW  (step L5)  ▣ foundry:curator — single call, all changed files
  |
Confidence block
```

One always-on gate. Everything after ENRICH's join is a single thread — the conflict pre-check inside PROPOSALS is in-block detail, not a second fan.

### Mode: external (`external <source> [--eager]`)

```
SOURCE READ  (steps E1–E6)
  classify source (URL/path/dir) → fast read → slow read
  → mental model → source report
  |
  +--------------------- FAN 2 ------------------------+
  |                                                     |
READ LOCAL ROSTER  (step E7)          READ INSTALLED PLUGINS  (step E7)
  Glob+Read agents/skills/rules        Glob plugins/*/
  |                                                     |
  +--------------------- JOIN --------------------------+
  |
CAPABILITY MAP  (steps E8–E11)
  build local map → compare → split Group A/B → score
  |
ADOPTION TABLE  (step E12)
  install-as-is judgement
  |
CHALLENGE  (step E12a)  ▣ foundry:challenger — adversarial review of adoption table
  fallback: report/JSON missing or agent absent → proceed unannotated,
  print ⚠, don't block
  |
◆ APPLY EXTERNAL SOURCE CANDIDATES?  (step E13)
  |
APPLY  (step E14)
  Group A (native Edit, reuses memory mode's conflict-check + gate)
  or print install command (standalone-plugin option)
  |
VERIFY  (step E15)
  git diff, report Group B as open questions
  |
  +--------------------- FAN 2 ------------------------+
  |  step E16 — only when both file-type groups exist  |
REVIEW .md  ▣ foundry:curator      REVIEW code  ▣ foundry:sw-engineer
  |                                                     |
  +--------------------- JOIN --------------------------+
  |
Confidence block
```

No second token after `external` → `! MISSING`, stop before any of the above runs. CHALLENGE's fallback never blocks — a missing or failed challenger prints one `⚠` line and the gate still fires on an unannotated adoption table.

### Mode: executables (`executables [--eager] [<path>]`)

```
LOCATE  (step E1)
  find latest Check 33 report (or path argument)
  |
  no report found
  +----------------- FAN n ------------------+
  |  n = plugin dirs under scan root         |
  SCAN  ▣ foundry:curator per plugin dir
    runs Check 33 Phase B2 protocol inline
  +----------------- JOIN --------------------+
  |
PARSE CANDIDATES  (step E2)
  HIGH/MEDIUM default; +LOW with --eager; PROSE-verdict candidates
  no qualifying candidates → stop (✓ report, Confidence)
  |
CANDIDATE TABLE  (step E3)
  print table · capture before-numbers (blocks-before.jsonl)
  |
◆ EXTRACT CANDIDATES TO bin/?  (step E3)
  |
EXTRACT  (step E4)  ▣ foundry:sw-engineer per selected cluster
  isolation: worktree, surgical-edit constraint
  |
RE-AUDIT  (step E5)  ▣ foundry:curator per modified file
  |
MEASURE + CONVERGE  (step E6)
  capture after-numbers
  Adversarial Convergence Loop (strictly decreasing integer score, ▣ foundry:challenger)
  ◆ AskUserQuestion      [only on stop — plateau/non-convergence/cap]
  |
COMMIT  (step E6)
  two commits: (1) scripts + tests + call-site edits, (2) rule changes
```

SCAN only fires when LOCATE finds no prior report — with one on disk, LOCATE is a pure file-locate and the fan never dispatches. The convergence loop is one agent iterating up to three sequential rounds, not a fan — no lanes run together there.

## Degenerate cases

- **Default mode** collapses to nothing extra by design — it is the only mode with zero gates and zero spawns regardless of input, always ending in a read-only report.
- **`prune` with no `MEMORY.md` anywhere** — `PRUNE_ABORT` stops before the PROJECT PICKER gate; no spawn ever issues.
- **`prune`/`prune --eager` with a single-project working set** — the per-project CLASSIFY/SCORE spawn is skipped; the same analysis runs inline in the main thread.
- **`external` with no second token** — `! MISSING` stop; the mode's own steps never begin.
- **`external` with `foundry:challenger` unavailable or failing** — CHALLENGE's fallback prints one `⚠` line and proceeds straight to the gate with an unannotated adoption table; the gate still fires.
- **`executables` with a prior `/audit --efficiency` report on disk** — LOCATE's inline SCAN spawn never fires; LOCATE becomes a pure file-locate.
- **`executables` with zero qualifying candidates** (from either an existing report or the inline scan) — stops at PARSE CANDIDATES with `✓ No extraction candidates`; the gate and every downstream spawn are unreached.

## Where this lives in `SKILL.md`

Index of the schema tags above, one row per block. A bare `step N` is a `SKILL.md` step; mode files use their own labels (`step L2` in `memory.md`, `step E4` in `external.md` or `executables.md` — the mode heading says which file), and `prune.md`, which has few numbered steps, is tagged by section name.

| Block | Where |
| -- | -- |
| SETUP | `SKILL.md` — flag-parsing bash blocks |
| INVENTORY | `SKILL.md` Step 1 |
| ROUTE | `SKILL.md` Step 2 — mode-token checks |
| Mode: default | `SKILL.md` Steps 2–5 |
| Mode: prune | `modes/prune.md` — named sections, P1–P3, P-eager-1–3 |
| Mode: memory | `modes/memory.md` — L1, L1b, L2–L5 |
| Mode: external | `modes/external.md` — E1–E16 |
| Mode: executables | `modes/executables.md` — E1–E6 |
