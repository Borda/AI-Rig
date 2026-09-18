<!-- file: memory.md — consumers: foundry:distill (SKILL.md reads this file directly; single source of truth) -->

# Mode: Memory Distillation

Triggered when `$ARGUMENTS == "memory"`. Read accumulated lessons and feedback, identify patterns to promote into durable governance — rule files, agent instruction updates, or skill workflow changes.

## Step L1: Collect raw material

Find and read all source material in parallel:

Use Read tool on `.notes/lessons.md` (skip if not found). Enumerate all project memory directories:

```bash
# timeout: 5000
# -print0/read -d '' handles spaces in paths
while IFS= read -r -d '' d; do
    slug=$(echo "$d" | sed 's|.*/projects/||;s|/memory$||')
    fb_count=$(find "$d" -maxdepth 1 -name "feedback_*.md" 2>/dev/null | wc -l | tr -d ' ')
    mem_file="$d/MEMORY.md"
    mem_tokens=$([ -f "$mem_file" ] && echo $(( $(wc -c < "$mem_file") / 3 )) || echo 0)
    echo "MEM_DIR: $slug | ${mem_tokens}k tokens | ${fb_count} feedback files | $d"
done < <(find "$HOME/.claude/projects" -maxdepth 3 -name "memory" -type d -print0 2>/dev/null | sort -z)
```

**If `PROJECT_FLAG == true`** (check model context from SKILL.md bash output): call `AskUserQuestion` with `multiSelect: true`. Build options from `MEM_DIR:` lines — label = `<slug> (tokens=<N>k)`, description = `<M> feedback files`. Max 4 options: more than 4 projects found, take 4 largest by token count, note in question text that remaining projects omitted. Always add final option label `Skip`, description `exit without changes`. Checked slugs: extract matching directory paths as working set.

**If `PROJECT_FLAG == false`**: use all directories from `MEM_DIR:` lines.

For each selected directory, use Glob tool with pattern `feedback_*.md` in that directory to list feedback files; read each with Read tool. Label each cluster of findings with its project slug. Also read `.claude/rules/` (Glob `rules/*.md`, path `.claude/`) to understand what's already captured as rule.

## Step L1b: Enrich with project context

Ground each project's feedback in its codebase context — prevents misclassifying project-specific constraints as general rules, and vice versa. Run all lookups in parallel across selected projects.

For each `<slug> | <path>` pair from working set, attempt to resolve project root:

```bash
# timeout: 5000
SLUG="<slug>"
# slug derivation: pwd | sed 's|[/.]|-|g' | sed 's/^-//'
# heuristic reversal: prepend / and replace - with / (works for simple paths)
CAND=$(echo "/$SLUG" | tr '-' '/')
if [ -d "$CAND" ]; then
    echo "PROJ_ROOT: $CAND"
else
    # fallback: search by last slug component (project name)
    LAST=$(echo "$SLUG" | grep -oE '[^-]+$')
    FOUND=$(find "$HOME" -maxdepth 5 -name "$LAST" -type d 2>/dev/null | head -1)
    echo "PROJ_ROOT: ${FOUND:-not_found}"
fi
```

If `PROJ_ROOT` resolved (≠ `not_found`):

- Read `$PROJ_ROOT/CLAUDE.md` (limit=60) — tech stack, project constraints, custom rules
- Run `git -C "$PROJ_ROOT" log --oneline -15 2>/dev/null` — recent work context
- Use Glob (pattern `todo_*.md`, path `$PROJ_ROOT/.plans/active/`) and read found files

Label collected context as `[Project grounding: <slug>]`, pass alongside feedback to Step L2.

If `PROJ_ROOT` not resolved: note `[Project grounding: <slug> — path not resolved]` in L2; treat ambiguous feedback for that project as `→ too narrow` unless evidence in feedback files themselves strong.

## Step L2: Cluster and classify

**Scope check**: material collected in Step L1 contains no lesson/feedback-shaped entries (no `.notes/lessons.md` findings, no `feedback_*.md` content resembling a correction, confirmation, or recurring pattern): report zero clusters, stop — don't force a disposition (e.g. `→ too narrow`) onto non-lesson input.

Group all lessons/feedback entries by domain. Use model reasoning to identify clusters of related items:

- **Git & commit discipline** (staging, branching, commit messages, push safety)
- **Testing & QA** (test patterns, mocking rules, coverage gaps)
- **Task management** (TaskCreate/TaskUpdate lifecycle, when to create tasks, when to mark complete, orchestrator vs. teammate task ownership)
- **Agent & skill config** (agent instructions, skill workflow, CLAUDE.md additions — excluding task tracking)
- **Communication & output** (tone, format, reporting)
- **Tool & permission use** (Bash vs native tools, settings.json)
- **Other** (project-specific, one-off)

For each lesson entry, classify disposition:

| Disposition | Meaning |
| -- | -- |
| `→ rule` | Recurring enough to warrant standalone `.claude/rules/<name>.md` file |
| `→ agent update` | Specific to one agent's instructions — edit that agent's `.md` file |
| `→ skill update` | Specific to one skill's workflow — edit that skill's `SKILL.md` |
| `→ already covered` | Already present verbatim (or near-verbatim) in existing rule, agent, or CLAUDE.md |
| `→ too narrow` | One-off, project-specific, or not generalizable — keep in memory only |

Thresholds:

- **`→ rule`**: 2+ distinct lessons on same topic, or single lesson applying across ≥3 agents/skills
- **`→ agent/skill update`**: lesson applies specifically to one file's behavior and not yet there
- **`→ already covered`**: exact principle including scope already in target file — mark, skip. Before marking, verify scope matches: same terminology doesn't imply same scope. Lesson adds conditions not in existing rule (new agent population, new trigger context, new edge case): classify as `→ rule` or `→ agent/skill update` instead.

**Duplicate detection**: before finalizing proposals, scan all lessons for identical insights expressed with different wording. Two or more lessons reduce to same principle: consolidate into one entry — don't propose separate changes for duplicate lessons. Flag consolidation explicitly in proposals table.

**Contradiction detection**: two lessons make mutually exclusive claims about same topic: flag both with ⚠ CONTRADICTION, don't classify either as → rule or → agent/skill update. Surface to user for resolution.

## Step L3: Generate proposals

Produce structured proposal table. Do not apply anything yet — report first.

````markdown
## Memory Distillation Proposals

### Summary
- Source files read: N (.notes/lessons.md + N feedback files)
- Total lessons: N
- Clusters: N domains

### Proposals

| # | Cluster | Lesson (condensed) | Disposition | Target |
|---|---------|-------------------|-------------|--------|
| 1 | Git | Never use git add -A; stage specific files | → already covered | rules/git-commit.md |
| 2 | Agent config | Agent description must include NOT-for clause | → rule | add to existing rule: rules/foundry-config.md |
| 3 | Communication | Flag blockers before starting, not mid-task | → already covered | rules/communication.md |

### New Rule Files Proposed (N)

#### rules/<name>.md
**Cluster**: [domain]
**Lessons consolidated**: [list lesson IDs, e.g., L1, L3, L7]
**Draft content**:
```markdown

## description: [one-line]

## [Rule heading]

[content distilled from the lessons]

```
**Why a rule file**: [applies broadly across agents/skills, not specific to one]

### Agent Instruction Updates Proposed (N)

#### agents/<name>.md
**Change**: [what to add/modify in the agent's instructions]
**Lesson source**: [which lesson(s) justify this]

### Skill Workflow Updates Proposed (N)

#### skills/<name>/SKILL.md
**Change**: [what step/note to add or modify]
**Lesson source**: [which lesson(s) justify this]

### Already Covered (N) — no action needed
- L2: [lesson] → already in [file]

### Too Narrow (N) — keep in memory
- L5: [lesson] → one-off, not generalizable
````

**Agent/skill name verification**: for each → agent update and → skill update row, verify: (1) proposed target file/agent name matches lesson content — lesson mentions foundry:qa-specialist, target must be qa-specialist.md, not a different agent; (2) agent name is plugin-prefixed, exists in roster from Step 1.

## Step L4: Apply (with confirmation)

Set up run directory before conflict checks:

```bash
RUN_DIR=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/make_run_dir.py" .reports/distill 2>/dev/null)  # timeout: 5000
echo "RUN_DIR=$RUN_DIR"  # bash vars don't persist; read from stdout
```

**Conflict pre-check** — before presenting question, run in parallel for every `→ rule` and `→ agent/skill update` proposal:

1. **Existing content grep**: use Grep to search target file (if already exists) for section heading or key phrase delta would insert near. Hit = potential collision with existing content.
2. **Cross-proposal collision**: two proposals both target same file and same section heading: mark both ⚠ CONFLICT.

Annotate each conflicting proposal row with ⚠. If conflicts found, print above question:

```text
⚠ Conflicts detected:
  - Proposal #N conflicts with existing content in <file>:<section> — both modify <topic>
  - Proposals #N and #M both target <file>:<section>
Review conflicts manually or select (b) to inspect each change before writing.
```

Print (annotated) proposal table. Then call `AskUserQuestion` tool — do NOT write options as plain text first. Map options directly into tool call arguments:

- question: "Apply proposals?"
- (a) label: `Apply non-conflicting` — description: write all `→ rule` and `→ agent/skill update` changes except ⚠ flagged proposals
- (b) label: `Review first` — description: show diff of each proposed change before writing
- (c) label: `Skip` — description: discard proposals and exit without changes

User selects (a), apply changes:

- **New rule files**: Write tool to create `.claude/rules/<name>.md` with drafted content
- **Agent updates**: Edit tool to insert new instruction into appropriate section of agent file
- **Skill updates**: Edit tool to insert new step/note in skill file

After applying:

1. Run cross-reference checks — use Grep to verify new rule files are referenced from `CLAUDE.md` or agent files that govern them (rule with project-wide applicability should appear as `See .claude/rules/<name>.md` reference in `CLAUDE.md`; agent-scoped rules should appear in relevant agent file)
2. Print compact apply summary:

```text
Applied N changes — <date>
  New rules:      N files — [names]
  Agent updates:  N files — [names]
  Skill updates:  N files — [names]
  Skipped:        N (already covered or too narrow)
```

3. Remind user: "Run `/foundry:setup` to propagate rule changes to `~/.claude/`"

4. **Git diff gate** — run after all writes complete:

```bash
git diff HEAD -- <space-separated list of changed files>  # timeout: 5000
```

Print diff. Anything unexpected appears: revert individual files before proceeding, `git checkout HEAD -- <file>`. Final safety net — changes recoverable until committed.

**Quality gate**: after edits in L4, proceed to L5 (`foundry:curator` review) before considering lesson applied. For agent or skill file edits specifically (not rule files), treat L5 curator findings as advisory — address any structural issues found before finalizing.

## Step L5: curator review

After applying changes, dispatch curator to audit created and modified config files. Substitute `$RUN_DIR` with value printed by Step L4 `RUN_DIR=` bash block above before issuing Agent call — spawned agents receive text, not shell context (same pattern as `external.md`'s `$EXT_RUN_DIR` substitution note):

> **Agent budget** — each spawn costs ~120,851 tok of fixed overhead (~73 tool-calls' worth) plus ~12.0 s/call, so work under ~73 calls is cheaper done inline: spawn nothing. Keep each agent near ~55 tool-calls; past ~60 they stall without returning an envelope, forcing reconstruction from disk. Every spawn prompt must require an envelope even on exhaustion — `partial: true` plus what was finished.

```text
Agent(subagent_type="foundry:curator", prompt="Review the following Claude config files just created or modified by /distill:memory: <list new rule files and updated agent/skill files from Step L4>. Check: (1) quality — rules are concrete, not vague; (2) duplication — no overlap with existing files; (3) NOT-for boundary clarity; (4) structural consistency. Write your full findings to <RUN_DIR>/curator-review.md using the Write tool. Return ONLY a compact JSON envelope: {\"status\":\"done\",\"findings\":N,\"severity\":{\"critical\":N,\"high\":N,\"medium\":N,\"low\":N},\"file\":\"<RUN_DIR>/curator-review.md\",\"issues\":N,\"confidence\":0.N,\"summary\":\"<one-line>\"}")
```

Surface curator findings as advisory block in terminal output. Don't block on curator findings — quality recommendations, not release gates.

End response with `## Confidence` block per CLAUDE.md output standards.
