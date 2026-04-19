**Re: Compress agent-checks markdown to caveman format**

# Agent Checks — 8, 13

______________________________________________________________________

## Check 19 — Model tier appropriateness

Three capability tiers:

| Tier | Model | Example agents |
| --- | --- | --- |
| Plan-gated | `opusplan` | solution-architect, oss:shepherd, self-mentor |
| Implementation | `opus` | sw-engineer, qa-specialist, research:scientist, perf-optimizer |
| Diagnostics / writing | `sonnet` | web-explorer, doc-scribe, research:data-steward |
| High-freq diagnostics | `haiku` | linting-expert, oss:ci-guardian |

Extract declared models:

```bash
printf "%-30s %s\n" "AGENT" "MODEL"
for f in .claude/agents/*.md; do # timeout: 5000
    name=$(basename "$f" .md)
    model=$(awk '/^---$/{c++; if(c==2)exit} c==1 && /^model:/{sub(/^model: /,""); print}' "$f")
    printf "%-30s %s\n" "$name" "${model:-(inherit)}"
done
```

Use model reasoning. Classify each agent by tier from `<role>`, `description`, workflow body. Cross-ref vs declared model:

- `focused-execution` + `opus`/`opusplan` → **medium** (potential overkill)
- `deep-reasoning` + `sonnet` → **high** (likely underpowered)
- **Orchestration signal**: workflow body contains `Spawn`, `Agent tool`, or explicit sub-agent delegation → classify `deep-reasoning` regardless of description — `sonnet` on orchestrating agent → **high**
- `plan-gated` + `sonnet` → **high**
- `focused-execution` + `haiku` → **not a finding**

**Important**: CLAUDE.md `## Agent Teams` specifies models for team-mode spawn — NOT mandate for agent frontmatter. Don't flag frontmatter models as violations for differing from CLAUDE.md team-mode spec.

**Report only** — never auto-fix. Model assignments may be intentional trade-offs.

______________________________________________________________________

## Check 20 — Agent description routing alignment

Canonical roster-consistency check. Three routing sub-checks + one decision check. All **report-only**.

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

**20a — Overlap analysis**: Per agent pair, assess domain overlap. Flag pairs where descriptions alone don't disambiguate → **medium** finding per ambiguous pair.

**20b — NOT-for clause coverage**: Per high-overlap pair from 20a, check at least one agent has "NOT for" exclusion referencing other or its domain. Missing disambiguation → **medium**.

**20c — Trigger phrase specificity**: Per agent, check description's first clause states exclusive domain. Vague opener → **low**.

**20d — Keep / sharpen / merge-prune decision**: Per overlap pair from 20a, explicit roster judgment:

- **keep** — both agents own distinct acceptance criteria or review surfaces
- **sharpen** — both stay, but one/both descriptions / NOT-for clauses / handoff notes need tightening
- **merge-prune** — pair differs mostly by tone, examples, or tool list — not decision surface

### Decision rules:

- Different tools alone → no separate role justified
- Different examples alone → no separate role justified
- Distinct acceptance criteria, escalation paths, or review surfaces → separate roles justified
- Two agents swappable on realistic task with no material output difference → **merge-prune** candidate unless another file makes boundary explicit

Every Check 20 finding must include: overlapping pair, shared surface, remaining distinct surface (if any), decision (`keep`, `sharpen`, `merge-prune`), concrete fix path.

Fix reference: run `/calibrate routing` to verify description overlap translates to actual routing confusion.
