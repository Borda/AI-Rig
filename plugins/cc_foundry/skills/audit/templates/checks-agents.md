# Agent Checks — 8, 13

## Check 19 — Model tier appropriateness

Three capability tiers:

| Tier | Model | Example agents |
| -- | -- | -- |
| Plan-gated | `opusplan` | solution-architect, oss:shepherd, curator |
| Implementation | `opus` | sw-engineer, research:scientist, perf-optimizer |
| Diagnostics / writing | `sonnet` | web-explorer, doc-scribe, research:data-steward, oss:cicd-steward, qa-specialist |
| High-freq diagnostics | `haiku` | linting-expert |

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

**Important**: CLAUDE.md `## Agent Teams` specifies models for team-mode spawn — NOT a mandate for agent frontmatter. Don't flag frontmatter models as violations for differing from CLAUDE.md team-mode spec.

**Report only** — never auto-fix. Model assignments may be intentional trade-offs.

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

**20a — Overlap analysis**: per agent pair, assess domain overlap. Flag pairs where descriptions don't disambiguate → **medium** per ambiguous pair.

**20b — NOT-for clause coverage**: per high-overlap pair from 20a, check at least one agent has "NOT for" exclusion referencing other or its domain. Missing disambiguation → **medium**.

**20c — Trigger phrase specificity**: per agent, check description's first clause states exclusive domain. Vague opener → **low**.

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

## Check 46 — Roster boundary alignment

Holistic roster-level analysis. Subsumes former `/distill review` mode. Run as part of `foundry:audit agents`.

**46a — Per-pair overlap scan**: for every agent pair, compute scope overlap from descriptions + NOT-for clauses. Default threshold: **>50%** shared scope → flag. With `--eager`: threshold drops to **>30%**; any single shared named capability also flags as boundary issue.

```bash
# Extract all agent descriptions for model reasoning
printf "%-25s %s\n" "AGENT" "DESCRIPTION"
for f in .claude/agents/*.md plugins/*/agents/*.md 2>/dev/null; do  # timeout: 5000
    [ -f "$f" ] || continue
    name=$(basename "$f" .md)
    desc=$(awk '/^---$/{c++; if(c==2)exit} c==1 && /^description:/{sub(/^[^:]*: /,""); print}' "$f")
    printf "%-25s %s\n" "$name" "$desc"
done
```

Use model reasoning to score each pair: `overlap_pct` = fraction of one agent's scope covered by the other. Flag pairs exceeding threshold.

**46b — Coverage gap detection**: scan agent descriptions for task domains with no clear owner. Coverage gap = realistic task type where no agent's TRIGGER applies and no NOT-for exclusion explains the gap.

Examples of coverage gap signals:

- "Who handles X?" produces no confident agent → gap
- Two agents exclude a domain ("NOT for Y") but no agent includes it → gap

**46c — Sharpen Boundary section** (always include when ≥1 overlap pair found; required when `--eager`):

```markdown
### Sharpen Boundary

| Pair | Shared capability | Recommended split |
|------|------------------|-------------------|
| agent-a / agent-b | <specific capability> | <which agent owns it; what NOT-for to add to the other> |
```

**Report format** (report only — no auto-fix):

```markdown
## Check 46 — Roster Boundary Alignment

### Overlap Findings (threshold: >50% [or >30% with --eager])
- **agent-a / agent-b** — overlap: ~N% — <shared domain> — decision: keep|sharpen|merge-prune

### Coverage Gaps
- <task domain>: no clear owner — <which agent is closest; what's missing>

### Sharpen Boundary
| Pair | Shared capability | Recommended split |
```

Severity: **medium** per overlap pair above threshold; **low** per coverage gap; **medium** per missing Sharpen entry when `--eager`.
