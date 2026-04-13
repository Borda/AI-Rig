# Agent Checks — 8, 13

______________________________________________________________________

## Check 19 — Model tier appropriateness

Three capability tiers:

| Tier                  | Model      | Example agents                                                     |
| --------------------- | ---------- | ------------------------------------------------------------------ |
| Plan-gated            | `opusplan` | solution-architect, oss:oss-shepherd, self-mentor                  |
| Implementation        | `opus`     | sw-engineer, qa-specialist, research:ai-researcher, perf-optimizer |
| Diagnostics / writing | `sonnet`   | web-explorer, doc-scribe, research:data-steward                    |
| High-freq diagnostics | `haiku`    | linting-expert, oss:ci-guardian                                    |

Extract declared models:

```bash
printf "%-30s %s\n" "AGENT" "MODEL"
for f in .claude/agents/*.md; do # timeout: 5000
    name=$(basename "$f" .md)
    model=$(awk '/^---$/{c++; if(c==2)exit} c==1 && /^model:/{sub(/^model: /,""); print}' "$f")
    printf "%-30s %s\n" "$name" "${model:-(inherit)}"
done
```

Using model reasoning, classify each agent into a tier based on its `<role>`, `description`, and workflow body content. Cross-reference against declared model:

- `focused-execution` agent using `opus` or `opusplan` → **medium** (potential overkill)
- `deep-reasoning` agent using `sonnet` → **high** (likely underpowered)
- **Orchestration signal**: if the agent's workflow body contains `Spawn`, `Agent tool`, or explicit sub-agent delegation, classify as `deep-reasoning` tier regardless of description — `sonnet` on an orchestrating agent → **high**
- `plan-gated` agent using `sonnet` → **high**
- `focused-execution` agent using `haiku` → **not a finding**

**Important**: CLAUDE.md's `## Agent Teams` section specifies models for team-mode spawn instructions — it is NOT a mandate for agent frontmatter. Do NOT flag frontmatter models as violations because they differ from CLAUDE.md's team-mode model spec.

**Report only** — never auto-fix. Model assignments may be intentional trade-offs.

______________________________________________________________________

## Check 20 — Agent description routing alignment

This is the canonical roster-consistency check. Three routing sub-checks plus one decision check, all **report-only**.

Extract all agent descriptions:

```bash
printf "%-25s %s\n" "AGENT" "DESCRIPTION"
for f in .claude/agents/*.md; do # timeout: 5000
    name=$(basename "$f" .md)
    desc=$(awk '/^---$/{c++; if(c==2)exit} c==1 && /^description:/{sub(/^description: /,""); print}' "$f")
    printf "%-25s %s\n" "$name" "$desc"
done
```

### Apply model reasoning:

**20a — Overlap analysis**: For each pair of agents, assess domain overlap. Flag pairs where descriptions alone do not disambiguate → **medium** finding per ambiguous pair.

**20b — NOT-for clause coverage**: For each high-overlap pair from 20a, check whether at least one agent has a "NOT for" exclusion clause referencing the other or its domain. Missing disambiguation → **medium**.

**20c — Trigger phrase specificity**: For each agent, check whether the description's first clause states an exclusive domain. A vague opener → **low**.

**20d — Keep / sharpen / merge-prune decision**: For every overlap pair from 20a, make an explicit roster judgment:

- **keep** — both agents own distinct acceptance criteria or review surfaces
- **sharpen** — both agents should remain, but one or both descriptions / NOT-for clauses / handoff notes need tightening
- **merge-prune** — the pair differs mostly by tone, examples, or tool list, not by decision surface

### Decision rules:

- Different tools alone do not justify separate roles
- Different examples alone do not justify separate roles
- Distinct acceptance criteria, escalation paths, or review surfaces do justify separate roles
- If two agents could be swapped on a realistic task with no material change in expected output, treat the pair as a **merge-prune** candidate unless another file makes the boundary explicit

Every Check 20 finding must include the overlapping pair, the shared surface, the remaining distinct surface (if any), the decision (`keep`, `sharpen`, or `merge-prune`), and a concrete fix path.

Fix reference: run `/calibrate routing` to verify whether description overlap translates to actual routing confusion.
