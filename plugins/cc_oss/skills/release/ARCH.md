<!-- file: ARCH.md — consumers: none (documentation; never loaded at runtime) -->

# `/oss:release` — execution architecture

> **Documentation, not contract.** The skill never loads this file; `SKILL.md`, `modes/*.md`, and `templates/*.md` are the only normative sources. It exists so the run/gate/fan-out shape can be read without walking an 870-line `SKILL.md` plus seven mode/template files.
>
> **Keep it current.** Any change to block order, gate placement, agent fan-out, or what runs beside what must land here in the same commit. Every schema block carries a tag naming its section or phase; renaming or renumbering one updates the tag and the index at the end. A schema that disagrees with `SKILL.md`/`modes/*.md` is worse than none — those files win every time.

## Legend

```
▣ agent spawn      ◆ blocking user gate      ‖ same turn
FAN n  work splits into n lanes dispatched together
JOIN   lanes collected; nothing past it starts until all land
```

`Skill(bridge:review)`, `Skill(bridge:advise)`, and the `foundry:humanizer` pass are independent dispatches, not `Agent()` calls — named inline below, never marked ▣, always single sequential calls (never part of a FAN).

**No mode has an always-on gate.** Every ◆ below fires only on a flag, a repo-state check, or a classification outcome; a clean re-run can complete any mode with zero human interaction. `SKILL.md <notes>` caps every path at ≤4 sequential `AskUserQuestion` calls.

## Schema — `notes` (default; no mode token, or explicit `notes`)

```
SETUP  (§ Mode Detection, § Shared setup)
  task hygiene · mode detection · flags/range · shared setup
  |
GATHER  (§ Gather changes)  ▣ foundry:sw-engineer
  git log/diff · gh pr list · PR-association loop
  revert-pair detection + cross-cycle revert/pivot detection
  |
EXPLORE + VALIDATE DOCS  (§ Explore codebase, § Validate docs)
  explore codebase · doc-weight proportionality check
  |
CLASSIFY  (§ Classify each change, § Truth check,
           § Breaking-change classification)
  classify each change · truth check (loop ≤3)
  breaking-change classification
  |
VALIDATE MIGRATION DOCS  (§ Validate migration docs)
  |
CHANGELOG + CONTRIBUTORS  (§ Audit changelog, § Extract contributors)
  audit changelog · extract contributors — inline, sequential
  |
HIGHLIGHTS + MIGRATION GUIDE  (§ Identify highlights, § Draft migration guide)
  identify highlights · draft migration guide
  |
DEMO  (§ Generate release demo)
  generate release demo script
  |
SUMMARY  (§ Draft executive summary)
  draft executive summary
  |
DRAFT  (§ Write release draft, § Adversarial review)
  ▣ foundry:sw-engineer adversarial review (loop ≤3)
  write release draft — agent re-spawns each iteration
  |
POLISH  (release-draft-template.md § Semantic consistency review,
        § Polish and write to disk)
  ▣ oss:shepherd voice review
  Skill(foundry:humanizer) pass
  |
PUBLISH  (release-draft-template.md § Candidate validation and publication)
  final cross-artifact truth gate → provenance record
  marker refresh → publish
  |
◆ HUMAN GATE  (release-draft-template.md § Human gate)
  stop, hand off — `gh release create` is user-run
```

**Conditions not drawn above** (kept off the schema — see Gates and Degenerate cases): GATHER delegates only past a 50-commit size guard, else runs inline. CHANGELOG + CONTRIBUTORS never delegates in `notes` — the skill's only agent fan belongs to `prepare`/`audit` (below). DEMO is skipped for a bug-fix-only release. BREAKING-CHANGE CLASSIFICATION is skipped with no codemap v3 index. VALIDATE MIGRATION DOCS is skipped with no migration doc in the repo.

## Fan and join points

| Fans into | Lanes | Width bound by | Joins at |
| -- | -- | -- | -- |
| CHANGELOG + CONTRIBUTORS | 2 | fixed (Agent A + Agent B, `foundry:doc-scribe`) | draft assembly |

The only agent fan anywhere in this skill — `prepare`/`audit` modes only. `notes`/`demo` run both changelog audit and contributor extraction inline, sequentially, no delegation. `Skill(bridge:review)`, `Skill(bridge:advise)`, and the humanizer pass never fan; each is a single sequential call. Every width in this skill is a fixed constant — no flag, pool, or item count ever widens a spawn point (contrast `/oss:resolve`'s `DISPATCH_MODE`-scaled specialist width).

## Gates

| Gate | Always? | Blocks |
| -- | -- | -- |
| unknown flag | conditional | SETUP |
| no stable tags | conditional | SETUP → GATHER |
| breaking-reclassification evidence (`unconfirmed_breaking>0`) | conditional | any release artifact write |
| post-promotion baseline/approval (codemap proposes Added/Changed→Breaking) | conditional | CHANGELOG + CONTRIBUTORS onward, every artifact write |
| ready to run demo script? | conditional | `notes`' DEMO → SUMMARY |
| demo failing after 3 attempts | conditional | SUMMARY (`notes`) / `prepare`'s DEMO + SUMMARY |
| demo synthetic-fallback approval | conditional | `demo` mode's GENERATE SCRIPT write |
| DRAFT.md overwrite guard | conditional | DRAFT write (plain `notes`, no valid `--append` marker) |
| `--changelog` prepend confirm | conditional | `$CHANGELOG_FILE` write |
| pip-audit missing | conditional | dependency-CVE row only, never the whole verdict |

No gate is always-on in any mode.

## Mode branches

### audit `[version]`

```
SETUP  (phase 0)
  release-model guardrail (stable-branch vs linear)
  |
GATHER + EXPLORE  (phases 1, 1a, 1b)  ▣ foundry:sw-engineer
  deprecation-removal check (1a) · upstream review verdict check (1b)
  |
READINESS CHECKS  (phase 2)
  templates/audit-checks.md
    +------------------- FAN 2 -------------------+
    |                                              |
  ▣ foundry:doc-scribe                     ▣ foundry:doc-scribe
    (changelog audit)                        (contributors)
    |                                              |
    +--------------------- JOIN --------------------+
  |
ADVERSARIAL AUDIT  (phase 2a)
  Skill(bridge:review) — independent Codex pass, if bridge available
  |
OUTPUT  (§ Output routing, § Verdict line)
  .reports/release/$BRANCH-$DATE.md (not .temp/)
  verdict line + confidence block
```

**Footnote — the FAN 2 pair in audit mode.** `modes/audit.md`'s own prose never shows this spawn line; its Phase 2 just runs `templates/audit-checks.md` and interprets the output. It is drawn here on cross-reference: `SKILL.md`'s Audit changelog and Extract contributors sections both state "`prepare`/`audit` modes: delegated in parallel," and `templates/audit-checks.md`'s Findings-summary output requires every row from `$CHANGELOG_AUDIT_FILE`'s "Scope check" section — Agent A's own output.

### prepare `<version>`

```
READINESS AUDIT  (phase 1)  = audit schema above, embedded, scoped to $VERSION
  ▣ foundry:sw-engineer gather (audit's own Phase 1)
  verdict BLOCKED → stop, no artifacts written
  |
GATHER + CHANGELOG  (phase 2)  ▣ foundry:sw-engineer gather (re-run, full $RANGE)
  audit changelog — second FAN 2 occurrence, see footnote
  |
HIGHLIGHTS + MIGRATION  (phase 3)
  identify highlights → HIGHLIGHTS.md
  draft migration guide → MIGRATION.md
  |
DEMO + SUMMARY  (phase 4)
  generate demo script — runs directly, no "ready to run" ask
  draft executive summary → SUMMARY.md
  |
DRAFT  (phase 5)  ▣ foundry:sw-engineer adversarial review (loop ≤3)
  write release draft → DRAFT.md
  ▣ oss:shepherd voice review · Skill(foundry:humanizer) pass
  |
CONSOLIDATE  (phase 6)
  waived changes → releases/$VERSION/waived-changes.md
  |
OUTPUT  (§ Output)
  confidence block
```

**Footnote — double spawns.** GATHER is genuinely spawned twice: once inside the embedded readiness audit (audit's own Phase 1), once again for this mode's full-`$RANGE` classify pass — same `foundry:sw-engineer` gather prompt, two call sites (`modes/audit.md` Phase 1, `modes/prepare.md` Phase 2a). `modes/prepare.md`'s own Phase 2b names Agent A directly and triggers the same delegated changelog+contributors FAN documented above for `audit` — so that FAN can also occur twice in one `prepare` run, once embedded and once here.

### demo `[range]`

```
SETUP  (SKILL.md § Mode: demo)
  range from token, else $LAST_TAG..HEAD
  |
GATHER + EXPLORE  (phase 1)  ▣ foundry:sw-engineer
  pick 2–3 headline features
  classify / truth check / breaking-change classification never run here
  |
GENERATE SCRIPT  (phase 2)
  real-world data only by default
  fallback: document attempts → Skill(bridge:advise)
  → ◆ approve synthetic
  |
WRITE OUTPUT  (phase 3)
  $DEMO_OUT — never executed here; user runs `jupytext --to notebook`
```

GATHER delegates only past the same 50-commit size guard as `notes`. The fallback only fires when no real-world demo can be assembled: document every failed attempt, ask Codex for a viable approach if `bridge` is available, then a blocking gate before any synthetic content is written.

### `--append` (flag on `notes`, not a separate mode)

```
SETUP  (§ Shared setup)
  $RANGE resolves from the per-branch marker, not $LAST_TAG..HEAD
  zero new commits since marker → stop, exit 0
  |
GATHER … DRAFT   = notes schema above, unchanged (same § tags)
  |
MERGE  (release-draft-template.md § Append merge)
  Read+Edit against $APPEND_STAGE, never a parser
  apply add/remove plan → $APPEND_ITEMS_FILE
  ▣ oss:shepherd reviews the JSON plan, not the full draft
  |
POST-MERGE RE-VALIDATION  (release-draft-template.md § Post-merge re-validation)
  truth check · highlights re-rank · migration re-check · docs re-check
  re-run against the merged candidate, inline, no extra spawn
  |
PUBLISH  (release-draft-template.md § Candidate validation and publication)
  seal → truth gate repeats on the sealed candidate → publish
  |
◆ HUMAN GATE  (release-draft-template.md § Human gate)
  stop, hand off
```

Every write from CHANGELOG + CONTRIBUTORS onward targets the staged candidate (`$APPEND_STAGE/...`), never the live file. The DRAFT.md overwrite-guard gate is skipped entirely only when the marker is valid; the `--changelog` idempotency check inspects the candidate on every `notes` run regardless of marker state. A missing or invalidated marker (first use, or history rewritten by rebase/force-push) falls back to the normal full-range, full-overwrite `notes` path, overwrite guard included.

## Degenerate cases

- `audit` mode — never writes a release artifact; output is a terminal report plus `.reports/release/$BRANCH-$DATE.md`.
- `demo` mode — writes `.temp/release-demo-*.py` and stops; the script is never executed by the skill, only by the user via `jupytext`.
- Bug-fix-only release (no 🚀 Added items) — DEMO is skipped entirely, in every mode that would otherwise run it.
- No codemap v3 index — BREAKING-CHANGE CLASSIFICATION is skipped outright; human Classify labels stand, post-promotion gate never fires.
- No migration doc found in the repo — VALIDATE MIGRATION DOCS is skipped entirely.
- `oss:shepherd` unavailable — its voice-review spawn is skipped, draft used as written; the humanizer pass still runs regardless.
- `bridge` (Codex) unavailable — both `Skill(bridge:review)` and `Skill(bridge:advise)` are skipped, no finding added, never a blocking failure.
- `--append` with no valid marker — collapses to the same full-range, full-overwrite behavior as plain `notes`.
- `--append` with zero new commits since the marker — GATHER stops immediately, exit 0, before any classification runs.
- A prior interrupted `--append` publish (`journal.json` present) — SETUP recovers it and exits 0 before GATHER ever starts.

## Where this lives in `SKILL.md`

Index of the schema tags above, one row per block. `SKILL.md` has no numbered steps, so a tag names its section (`§ Gather changes`); mode files use their own phase numbers (`phase 2`).

| Block | `SKILL.md` / mode file section |
| -- | -- |
| SETUP | Mode Detection, Shared setup |
| GATHER | Gather changes |
| EXPLORE + VALIDATE DOCS | Explore codebase, Validate docs |
| CLASSIFY | Classify each change, Truth check, Breaking-change classification |
| VALIDATE MIGRATION DOCS | Validate migration docs |
| CHANGELOG + CONTRIBUTORS | Audit changelog, Extract contributors |
| HIGHLIGHTS + MIGRATION GUIDE | Identify highlights, Draft migration guide |
| DEMO | Generate release demo |
| SUMMARY | Draft executive summary |
| DRAFT | Write release draft, `modes/adversarial-review.md` |
| POLISH | `modes/release-draft-template.md` — Semantic consistency review, Polish and write to disk |
| PUBLISH | `modes/release-draft-template.md` — Candidate validation and publication |
| HUMAN GATE | `modes/release-draft-template.md` — Human gate |
| READINESS AUDIT / CONSOLIDATE (`prepare`) | `modes/prepare.md` Phase 1 (= `modes/audit.md`), Phase 6 |
| GATHER+EXPLORE / READINESS CHECKS / ADVERSARIAL AUDIT (`audit`) | `modes/audit.md` Phase 1–1b, Phase 2, Phase 2a |
| GATHER+EXPLORE / GENERATE SCRIPT / WRITE OUTPUT (`demo`) | `modes/demo.md` Phase 1, Phase 2, Phase 3 |
| MERGE / POST-MERGE RE-VALIDATION (`--append`) | `modes/release-draft-template.md` — Append merge, Post-merge re-validation |
