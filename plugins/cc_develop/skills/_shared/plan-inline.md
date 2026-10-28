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
3. Build and share a short plan summary, including the path to the complete plan. Reuse the user's existing authorization to proceed with the calling skill; do not ask for routine plan approval. If the user requested plan-only, wait, or explicit plan approval, honor that instruction. Ask only for a genuinely missing protected decision or authority, preserving architecture, security, data and other applicable permission boundaries.
4. Set `PLAN_FILE=<path>`, persist it (`echo "$PLAN_FILE" > "${TMPDIR:-/tmp}/dev-plan-file-${CSID}"`) so compaction-contract boundaries resolve it, then record its hash (`python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/plan_hash.py" record "$PLAN_FILE"  # timeout: 5000`) for the Final Report drift check; continue to the calling skill's plan challenge.

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
- Classification — refactor has no separate classification step, so classify here from Step 1's scope analysis, after the Scope gate: `small` (one module, ≤3 files, no public-API rename), `wide` (any Scope-gate trigger left standing: 3+ modules, 5+ files, or a public-API rename), `medium` (everything between: 2 modules, or exactly 4 files in one module). The skip rule "small complexity" in the Trigger line reads this result.
- `[SKILL_VERB]` = refactor
- `[PLAN_SECTIONS]` = Refactor goal and motivation, Affected files, Risks (API surface changes, caller impact, test coverage gaps), Approach (ordered refactor steps with clear checkpoints)
- `[RISK_FOCUS]` = API surface changes, caller impact, test coverage gaps
- `[PROCEED_TEXT]` = continue refactor using this plan
