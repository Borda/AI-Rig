# Mode: External Distillation

<!-- file: external.md — consumers: distill/SKILL.md -->

Triggered when `$ARGUMENTS` begins with `external`. Analyse external plugin, skill, or agentic resource, produce structured adoption proposal for local Claude Code setup.

```bash
EXT_RUN_DIR=".temp/distill/$(date -u +%Y-%m-%dT%H-%M-%SZ)"
mkdir -p "$EXT_RUN_DIR"  # timeout: 5000
```

**E1: Classify and plan**

Identify source type:

- URL → `WebFetch` (skim landing page + follow key links: README, docs, manifests, agent/skill files)
- File path → `Read`
- Directory → `Glob` `*.md`, `*.js`, `*.json` then prioritise: manifests, README, agent/skill/rule/hook files

**E2: Fast read — structure and intent**

Skim headings, frontmatter, filenames, top-level examples. Extract: purpose, target user, top-level architecture, routing logic. ≤2 reads per top-level file.

**E3: Slow read — full content**

Read all agent/skill/rule/hook files end to end. For large sources prioritise: prompts, rules, validation gates, templates, docs. Use Glob + Read in parallel.

**E4: Extract mental model**

Record in working notes: source intent, architecture, routing, safety model, expected outputs, key design decisions.

**E5: Identify standout implementation details**

Use Grep for: hooks, validation gates, must/never constraints, fallback paths, scoring rubrics, unusual prompt patterns. Flag anything absent from local setup.

**E6: Source report**

Produce inline:

```text
## Source Report — <source>
Intent:       [one line]
Architecture: [one line]
Notable hacks: [bullets]
Risks / unclear assumptions: [bullets]
Candidate artifacts: [comma list]
```

**E7: Read live local setup**

Run in parallel:

- Glob + Read on `.claude/agents/*.md`, `.claude/skills/**/SKILL.md`, `.claude/rules/*.md`
- Glob on `plugins/*/` for installed plugins

**E8: Build local capability map**

Group local agents/skills/rules by responsibility, trigger conditions, gates, output formats. Note coverage gaps.

**E9–E10: Compare and split**

For each candidate from E6, compare against local capability map. Assign to a group:

- **Group A — Align + improve**: maps onto existing local agent/skill/rule, improves without structural change
- **Group B — Differentiated highlights**: novel pattern or design philosophy, doesn't map natively — interesting but requires larger structural work or conflicts with existing design

**E11: Score candidates**

Rate each on: impact (H/M/L) · fit (H/M/L) · duplication risk (none/low/high) · effort (S/M/L) · safety risk.

**E12: Adoption brainstorm table**

Exactly one of Adopt/Tweak/Discuss/Skip per item. "Local target" = specific file or directory.

- `source/hooks/task-log.js` — Group A · **Adopt as-is** · Local target: `.claude/hooks/task-log.js` · No local equivalent; identical purpose
- `source/skills/audit/SKILL.md` — Group A · **Tweak** · Local target: `.claude/skills/audit/SKILL.md` · Adapt trigger keyword to local naming
- `source/agents/doc-scribe.md` — Group B · **Discuss** · Local target: — · Overlaps existing agent; scope TBD
- `source/skills/old-util/SKILL.md` — Group B · **Skip** · Local target: — · Covered by local equivalent

**Install-as-is recommendation**

After scoring, apply this judgement:

- **Recommend install-as-is** when: (a) Group A has ≤2 candidates AND source has coherent standalone design, OR (b) cumulative edit effort is L (large) for ≥3 candidates
- If recommending: state justification — what source provides that local setup lacks, why cherry-picking would dilute value
- Present as explicit option in E13 (option b); omit if not recommended

**E12a: Challenger adversarial review**

Before presenting proposals to user, spawn **foundry:challenger** to adversarially review adoption table. Challenger surfaces: claimed benefits already covered locally, cost/benefit miscalculations, proposals adding complexity without measurable gain.

Substitute `$EXT_RUN_DIR` with computed value (from `EXT_RUN_DIR=` block at top of this mode file) before issuing Agent call — spawned agents receive text, not shell context.

> **Agent budget** — each spawn costs ~120,851 tok of fixed overhead (~73 tool-calls' worth) plus ~12.0 s/call, so work under ~73 calls is cheaper done inline: spawn nothing. Keep each agent near ~55 tool-calls; past ~60 they stall without returning an envelope, forcing reconstruction from disk. Every spawn prompt must require an envelope even on exhaustion — `partial: true` plus what was finished.

```text
Agent(subagent_type="foundry:challenger", prompt="
Challenge these adoption proposals from /distill external mode on <source>. For each candidate in the adoption table:
1. Strongest objection — is claimed benefit real? Already covered locally? Cost/benefit miscalculated?
2. Merit worth preserving even if proposal as stated is flawed
3. Verdict: ADOPT_AS_STATED | ADOPT_WITH_MODIFICATION | DISCARD | NEEDS_MORE_INFO

Apply mandatory refutation step to your own findings.

Adoption table:
<paste full adoption table from E12>

Local capability map (from E8):
<paste local capability map summary>

Write full analysis to <EXT_RUN_DIR>/challenger-review.md using Write tool.
Return ONLY compact JSON as final line: {\"status\":\"done\",\"findings\":N,\"severity\":{\"critical\":0,\"high\":N,\"medium\":N,\"low\":N},\"file\":\"<EXT_RUN_DIR>/challenger-review.md\",\"verdicts\":{\"<item-label>\":\"VERDICT\",...},\"confidence\":0.N,\"summary\":\"N candidates reviewed, N flagged\"}
")
```

After challenger returns: read `$EXT_RUN_DIR/challenger-review.md`. Annotate each adoption table row with challenger verdict — add **Verdict** column. Rows marked `DISCARD`: move to separate **Discarded by challenger** section below table with one-line reason. Rows marked `ADOPT_WITH_MODIFICATION`: update **Action** cell to `Tweak*`, add footnote with challenger's modification requirement. Confidence < 0.85: flag that group's findings with ⚠, surface named gap.

**Fallback when challenger is unavailable or fails** — `$EXT_RUN_DIR/challenger-review.md` doesn't exist after spawn returns, OR returned JSON envelope has `status != "done"`, OR agent itself missing (`foundry:challenger` not installed):

- Print: `⚠ Challenger review unavailable — proceeding without adversarial annotation. Manual review of adoption table recommended before E13 apply.`
- Skip per-row Verdict column and **Discarded by challenger** section
- Continue to E13 with unannotated adoption table
- Do NOT block workflow — challenger is advisory, not gating

**E13: Gate — AskUserQuestion**

Present source report + adoption table + install-as-is recommendation (when applicable). Then call `AskUserQuestion` tool — do NOT write options as plain text first. Map options directly into tool call arguments:

- question: "Apply external source candidates?" When install-as-is IS recommended, include all four options:
- (a) label: `Apply Group A candidates` — description: adopt-as-is and tweak items only
- (b) label: `Install as standalone plugin` — description: install external source as standalone plugin
- (c) label: `Review first` — description: walk through each candidate interactively
- (d) label: `Skip` — description: exit without changes

When install-as-is is NOT recommended, omit (b) and re-label to avoid gaps:

- (a) label: `Apply Group A candidates` — description: adopt-as-is and tweak items only
- (b) label: `Review first` — description: walk through each candidate interactively
- (c) label: `Skip` — description: exit without changes

**E14: Apply**

- Option (a): reuse existing distill apply path — conflict pre-check + AskUserQuestion gate + Edit + git diff safety net (per Step L4). Limit edits to confirmed Group A targets only.
- Option (b): print install command or path; don't apply automatically — plugin installation requires user action.

**E15: Verify and report**

Print changed files. Run `git diff HEAD -- <files>` (`# timeout: 5000`), show output. Surface unresolved Group B items as open questions for future distill runs. End with `## Confidence` block per CLAUDE.md output standards.

**E16: quality review — by file type**

Split changed file list from E14 into two groups; dispatch each to right reviewer (curator's NOT-for excludes hook/`.js` files):

- **`.md` files** (agents, skills, rules, READMEs, modes/templates): spawn `foundry:curator`
- **`.js` files** (hooks, helpers) and other code files (`.py`, `.ts`, `.sh`): spawn `foundry:sw-engineer` with hook-authoring specialization

Substitute `$EXT_RUN_DIR` with computed path from `EXT_RUN_DIR=` block above. Issue both spawns in one response when both groups non-empty (parallel review):

```text
# .md files only
Agent(subagent_type="foundry:curator", prompt="Review Claude config files modified by /distill external mode: <list .md files changed in E14>. Check: (1) structural integrity — XML tag balance, step numbering; (2) cross-ref validity — no broken agent/skill references; (3) content quality — no duplication of existing canonical content. Write your full findings to ${EXT_RUN_DIR}/curator-external-review.md using the Write tool. Return ONLY: {\"status\":\"done\",\"findings\":N,\"severity\":{\"critical\":N,\"high\":N,\"medium\":N,\"low\":N},\"file\":\"${EXT_RUN_DIR}/curator-external-review.md\",\"issues\":N,\"confidence\":0.N,\"summary\":\"<one-line>\"}")

# .js / code files only — curator NOT-for excludes hooks; use sw-engineer
Agent(subagent_type="foundry:sw-engineer", prompt="Apply the <hook-authoring> specialization from your agent definition. Review code files modified by /distill external mode: <list .js/.py/.ts/.sh files changed in E14>. Check: (1) file-header block present (PURPOSE, HOW IT WORKS, EXIT CODES); (2) exit-code semantics correct; (3) stdin pattern uses event-based accumulation; (4) subprocess calls use execFileSync/spawnSync with args array — no shell-string injection; (5) no unhandled exceptions escape. Write your full findings to ${EXT_RUN_DIR}/sw-engineer-external-review.md using the Write tool. Return ONLY: {\"status\":\"done\",\"findings\":N,\"severity\":{\"critical\":N,\"high\":N,\"medium\":N,\"low\":N},\"file\":\"${EXT_RUN_DIR}/sw-engineer-external-review.md\",\"issues\":N,\"confidence\":0.N,\"summary\":\"<one-line>\"}")
```

Critical findings returned by either reviewer: surface to user before marking complete. Non-critical findings: advisory only.
