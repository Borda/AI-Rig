## Inline Plan Generation Protocol

**Trigger**: complexity = [COMPLEXITY_TERMS] AND no `--plan` supplied AND `ACCEPT_NO_PLAN=false`. Skip if complexity = small OR `ACCEPT_NO_PLAN=true`.

**Steps**:

1. Inform: "Complexity: \[[COMPLEXITY_TERMS]\] — generating [SKILL_VERB] plan before [SKILL_VERB]..."
2. Spawn **foundry:sw-engineer** (model=sonnet), produce structured plan:
   - [PLAN_SECTIONS] — filled by calling skill context below
   - Affected files: list with per-file change description
   - Risks: [RISK_FOCUS] — filled by calling skill context below
   - Approach: ordered steps with clear checkpoints
   - Write to `.plans/active/plan-<slug>-$(date -u +%Y-%m-%dT%H-%M-%SZ).md` where slug = first 4 words of task/goal
3. Present plan summary to user (first 10 lines of plan)
4. Invoke `AskUserQuestion`:
   - (a) **Proceed** — [PROCEED_TEXT]
   - (b) **Stop** — review/edit plan at `<path>` before continuing; re-invoke with `--plan <path>` when ready
   - (c) **Abort** — cancel
5. On (b) or (c): stop
6. On (a): set `PLAN_FILE=<path>`, persist it (`echo "$PLAN_FILE" > "${TMPDIR:-/tmp}/dev-plan-file-${CSID}"`) so compaction-contract boundary reads in feature/fix/refactor resolve it across Bash calls; continue to next step

### Skill contexts (substitute when calling this protocol)

**feature**:

- `[COMPLEXITY_TERMS]` = medium/large
- `[SKILL_VERB]` = implementation
- `[PLAN_SECTIONS]` = Summary (2–3 sentences: what needs to change and why), Affected files, Risks (breaking changes, blast radius, performance implications), Approach (ordered implementation steps numbered)
- `[RISK_FOCUS]` = breaking changes, blast radius, performance implications
- `[PROCEED_TEXT]` = continue implementation using this plan

**fix**:

- `[COMPLEXITY_TERMS]` = medium/large
- `[SKILL_VERB]` = fix
- `[PLAN_SECTIONS]` = Root cause summary (from Step 1 analysis), Affected files, Risks (breaking changes, blast radius, regressions), Approach (ordered fix steps)
- `[RISK_FOCUS]` = breaking changes, blast radius, regressions
- `[PROCEED_TEXT]` = continue with fix using this plan

**refactor**:

- `[COMPLEXITY_TERMS]` = medium/wide
- `[SKILL_VERB]` = refactor
- `[PLAN_SECTIONS]` = Refactor goal and motivation, Affected files, Risks (API surface changes, caller impact, test coverage gaps), Approach (ordered refactor steps with clear checkpoints)
- `[RISK_FOCUS]` = API surface changes, caller impact, test coverage gaps
- `[PROCEED_TEXT]` = continue refactor using this plan
