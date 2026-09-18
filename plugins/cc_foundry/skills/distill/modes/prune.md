# Mode: Memory Pruning

<!-- file: prune.md — consumers: distill/SKILL.md -->

Triggered when `$ARGUMENTS == "prune"`. Locate, evaluate, and trim project memory file.

**Find memory file:**

<!-- Note: if the auto-memory path convention changes, update this slug derivation. -->

<!-- Slug-divergence guard: `resolve_memory_dir.py` is the single source of truth for the memory directory and `MEMORY.md` filename. Any consumer that reads or writes project memory (e.g. distill's `memory` mode — `modes/memory.md`) MUST resolve the same path via this script — do NOT hardcode an alternate slug or filename here or elsewhere; divergence causes silent split-brain between writer and reader. -->

```bash
# timeout: 5000
FOUND=$(find "$HOME/.claude/projects" -maxdepth 3 -name "MEMORY.md" -path "*/memory/MEMORY.md" 2>/dev/null | sort)
if [ -z "$FOUND" ]; then
    echo "PRUNE_ABORT"
    echo "PRUNE_ABORT_REASON: no memory files found under ~/.claude/projects/"
else
    echo "PRUNE_FOUND"
    echo "$FOUND" | while IFS= read -r f; do
        slug=$(echo "$f" | sed 's|.*/projects/||;s|/memory/MEMORY.md||')
        tokens=$(( $(wc -c < "$f" 2>/dev/null || echo 0) / 3 ))
        echo "PRUNE_ENTRY: $slug | ${tokens}k tokens | $f"
    done
fi
```

**Short-circuit**: after block runs, scan for `PRUNE_ABORT` (exact-line match). If present, stop prune mode, end with Confidence block. Otherwise collect all `PRUNE_ENTRY:` lines — each has format `<slug> | <N>k tokens | <path>`.

**If `PROJECT_FLAG == true`** (interactive picker): call `AskUserQuestion` with `multiSelect: true`. Build options from `PRUNE_ENTRY` lines — label = `<slug> (tokens=<N>k)`, description = `prune this project's memory`. Max 4 options: more than 4 projects found, take 4 largest by token count, note in question text that remaining projects were omitted (user can re-run). Always add a final option with label `Skip`, description `exit without changes`. Checked slugs: extract matching `<path>` fields as working set.

**If `PROJECT_FLAG == false`**: use all `<path>` fields from PRUNE_ENTRY lines as working set.

**Parallel analysis across projects** — working sets with 2+ files: spawn one analysis agent per project simultaneously. Single-file working sets: run P1–P2 inline (no spawn).

Spawn one `Agent` per project with `model="sonnet"` (mechanical Drop/Trim/Keep classification — no reasoning tier needed; no schema — returns text analysis):

```text
Read MEMORY.md at <absolute-path>.
Also read .claude/CLAUDE.md in the current working directory (limit=40) to identify overlap — anything already there need not live in memory.
Evaluate every section using these criteria:
  Drop: stale/no-longer-accurate, fully duplicated in CLAUDE.md, resolved one-time issues
  Trim: accurate but contains rationale/history no longer needed day-to-day; keep operational facts only
  Keep: rules applied every session, project-specific facts absent from CLAUDE.md
Return structured analysis (no prose):
PROJECT: <slug>
DROP: <section-name> — <one-line reason>
TRIM: <section-name> — <what to keep vs remove>
KEEP: <section-name>
CONFIDENCE: 0.N
```

Wait for all agents to complete. Merge into consolidated proposal list keyed by slug, labeling each section with its project slug.

**If `$EAGER == true`**: skip P1–P3 below, execute P-eager steps:

**P-eager-1**: spawn one scoring agent per project in parallel with `model="sonnet"` (structured two-dimension scoring — no reasoning tier needed); working sets with 2+ files; inline for single file. Each agent scores every section in its assigned MEMORY.md:

```text
Read MEMORY.md at <absolute-path>.
Score every section on two dimensions:
  Usage likelihood: High (every session) | Moderate (occasional) | Low (rare/one-off)
  Impact if missing: High (wrong behavior) | Moderate (degraded output) | Low (no effect)
  Tier: P0=keep (High×High or High×Moderate) | P1=trim (Moderate×Moderate or mixed) | P2=drop (Low on either)
  Action: if content belongs in rules/*.md or agent file → "→ rule"
Return ONLY (one TSV line per section, no prose):
PROJECT:<slug>  #:<n>  Section:<name>  Usage:<tier>  Impact:<tier>  Tier:P<n>  Action:<keep|drop|trim|→ rule>
```

Collect responses from all projects. Assign sequential `#` IDs across all projects. Print one consolidated scored table (all projects, `#` as primary sort):

- **Usage likelihood**: High = needed every session · Moderate = occasional · Low = rare/one-off
- **Impact if missing**: High = wrong behavior without it · Moderate = degraded output · Low = no effect
- **Tier** (derived): P0 = keep · P1 = trim candidate · P2 = drop/convert candidate
  - P0: High×High or High×Moderate
  - P1: any Moderate×Moderate or mixed High/Low signal
  - P2: Low usage OR Low impact (especially both)
- **Action**: entries whose content could live in `rules/*.md` or an agent file → mark `→ rule` in Action column

```text
| #  | Project | Section | Usage likelihood | Impact if missing | Tier | Action      |
|----|---------|---------|-----------------|-------------------|------|-------------|
| 1  | slug-a  | ...     | High            | High              | P0   | Keep        |
| 2  | slug-b  | ...     | Low             | Low               | P2   | Drop        |
| 3  | slug-a  | ...     | Moderate        | High              | P1   | Trim        |
| 4  | slug-b  | ...     | Low             | High              | P2   | → rule      |

Legend:
  Usage likelihood — High: every session · Moderate: occasional · Low: rare/one-off
  Impact if missing — High: wrong behavior · Moderate: degraded · Low: no effect
  Tier — P0: keep · P1: trim candidate · P2: drop/convert candidate
  Action — "→ rule" entries can be promoted to rules/*.md then dropped from memory
```

**P-eager-2**: Call `AskUserQuestion` tool — do NOT write question as plain text:

- question: "Which entries to prune? Select tier or type item numbers (e.g. 2, 4, 7)."
- (a) label: `All P2` — description: drop all tier-P2 entries; apply `→ rule` conversions as proposals
- (b) label: `All P1 + P2` — description: trim P1 entries and drop P2 entries
- (c) label: `Specific items` — description: enter item numbers in next message; applies only those
- (d) label: `Skip` — description: leave MEMORY.md untouched; user edits manually

User picks (c): print "Enter item numbers (e.g. 2, 4, 7):", wait for next message; resolve item numbers against # column before proceeding.

**P-eager-3**: Spawn one **foundry:curator** agent per project in parallel. Group selected `#` items by project slug; each agent receives only the items for its project. Substitute absolute memory file path inline before issuing each Agent call:

```text
Read MEMORY.md at <absolute-path>.
Apply these prune actions (sections identified by # from scored table — only the items for this project):
  <list: # — section name — action (Drop | Trim | Convert to rule)>
Rules:
- Drop: remove entire section including heading
- Trim: keep operational directive only (1 line max per entry); remove rationale/backstory
- Convert to rule: remove section from MEMORY.md; print proposed rule file content inline in response for user review before writing — do NOT write the rule file
Write MEMORY.md changes using the Edit tool.
Return ONLY: {"status":"done","project":"<slug>","sections_dropped":N,"sections_trimmed":N,"rule_conversions":N,"confidence":0.N}
```

Wait for all curator agents to complete. Collect results. Print consolidated summary:

```text
Pruned MEMORY.md — <date>
  Projects processed: N
  Dropped: N sections total — [project: names, ...]
  Trimmed: N sections total — [project: names, ...]
  Rule conversions proposed: N — [project: names, ...] (review and write manually or via /manage)
  Kept:    N sections unchanged
  Saved:   ~N lines total
```

End response with `## Confidence` block per CLAUDE.md output standards.

```bash
rm -f .temp/state/skill-contract.md  # clear contract — skill complete (compaction-contract.md §Lifecycle)  # timeout: 5000
```

**Otherwise** (`$EAGER == false`) — standard read-only advisory flow:

**Evaluate each section against these criteria:**

- **Drop**: content no longer accurate (removed features, resolved one-time issues, superseded decisions), or fully duplicated in CLAUDE.md
- **Trim**: sections still accurate but containing implementation history or rationale no longer needed day-to-day — keep operational facts (what/where), drop why-it-was-built backstory
- **Keep**: rules actively applied every session; project-specific facts absent from CLAUDE.md; anything model needs to act correctly

**Memory-write gate** — project CLAUDE.md `Memory Policy` prohibits auto-writes to MEMORY.md. Prune mode runs read-only by default, produces advisory diff/report rather than applying edits silently:

**P1**: Read all memory files (parallel for 2+ files). Analyse each for stale, redundant, and verbose entries.

**P2**: Print consolidated proposed prune report across all projects:

```text
Prune proposals (apply manually unless explicitly approved below):

[Project: <slug>]
  Drop  — <section name>: <reason>
  Trim  — <section name>: <what to remove vs keep>

[Project: <slug>]
  ...
```

**P3**: Call `AskUserQuestion` — do NOT write question as plain text. Map options directly into tool call:

- question: "Apply prune edits across all N project memory files?"
- (a) label: `Apply now` — description: apply all proposals to all memory files in parallel
- (b) label: `Show diff first` — description: print line-by-line preview before applying any change
- (c) label: `Skip` — description: leave all MEMORY.md files untouched; user will edit manually

Only after user picks (a) (or (b) followed by approval) may Edit be invoked on memory files. **Never apply prune edits silently.** Apply edits to all projects in parallel with Edit tool (one project per call, concurrent).

Print consolidated summary after applying (or after user declines):

```text
Pruned MEMORY.md — <date>
  Projects: N
  Dropped: N sections total — [project: names, ...]
  Trimmed: N sections total — [project: names, ...]
  Kept:    N sections unchanged
  Saved:   ~N lines total
```

End response with `## Confidence` block per CLAUDE.md output standards.

```bash
rm -f .temp/state/skill-contract.md  # clear contract — skill complete (compaction-contract.md §Lifecycle)  # timeout: 5000
```
