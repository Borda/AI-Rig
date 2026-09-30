<!-- file: ARCH.md — consumers: none (documentation; never loaded at runtime) -->

# `/research:topic` — execution architecture

> **Documentation, not contract.** The skill never loads this file; `SKILL.md` and `modes/*.md` are the only normative sources. It exists so the shape — what fans out, what joins, what blocks — can be read without walking the full skill plus its two mode files.
>
> **Keep it current.** Any change to block order, gate placement, fan-out width or what runs beside what lands here in the same commit. Every schema block carries a `(step N)` tag naming its step; renumbering a step updates the tag and the index at the end. A schema that disagrees with `SKILL.md` is worse than none — `SKILL.md` wins every time.

## Legend

```
▣ agent spawn      ◆ blocking user gate      ‖ same turn
FAN n  work splits into n lanes dispatched together
JOIN   lanes collected; nothing past it starts until all land
```

## Schema

Branches once, at mode dispatch, into three mutually exclusive paths; every path converges on the same terminal gate (own rail schema each, under Mode branches below).

```
SETUP  (step 1)
  codebase context · flag case-fold · unsupported-flag check
  · --keep extraction · per-phase tasks created upfront
  ◆ unknown flag?                                [conditional]
  |
MODE DISPATCH  (step 1 — early dispatch)
  first non-flag word: `plan` / `--team` / neither (default)
  |
  +---- plan ----------> PLAN PATH     [modes/plan.md]
  |
  +---- --team --------> TEAM PATH     [modes/team.md]
  |
  +---- neither -------> DEFAULT PATH  [steps 2–3]
  |
  (exactly one path runs; all three converge below)
  |
◆ FOLLOW-UP GATE  (§ Follow-up gate)                 (always)
```

## Fan and join points

| Fans into | Lanes | Width bound by | Joins at |
| -- | -- | -- | -- |
| DEFAULT PATH: LITERATURE SEARCH ‖ CODEBASE CHECK | 2 | fixed | REPORT |
| TEAM PATH: RESEARCHER teammates | 2–3 | lead's judgment of method-family count — no pool cap | CONSOLIDATE (3 teammates only) |

The default-path fan is free — it rides an idle window the orchestrator already had, so it adds no spawn of its own beyond the one agent call: the literature search needs no codebase signal, the Grep pass needs no paper data — both read only `$ARGUMENTS`. Wins: search + codebase check in one turn instead of two.

The team-path fan is not free — each teammate is a real spawn, costed at the agent-budget note in both mode files (~120,851 tok fixed overhead, ~12.0 s per call).

## Gates

| Gate | Always? | Blocks |
| -- | -- | -- |
| unknown flag | conditional | MODE DISPATCH, and by extension the whole run |
| **FOLLOW-UP GATE** | **always** | workflow end — hook-denied while `$REPORT_OUT`/`$PLAN_OUT` is missing or empty |

Every path costs at most 2 `AskUserQuestion` calls (unknown-flag + terminal), and the terminal one always fires exactly once regardless of which path ran.

## Mode branches

### Default path — neither `plan` nor `--team`

```
DEFAULT PATH  (steps 2–3)
  |
  +--------------------- FAN 2 ----------------------+
  |                                                  |
LITERATURE SEARCH  (step 2a)             CODEBASE CHECK  (step 2b)
  ▣ web-explorer
  SOTA search → AGENT_OUT                   Grep existing impls
  |                                                  |
  +--------------------- JOIN -----------------------+
  |
REPORT  (step 3)
  synthesize findings → REPORT_OUT, print header table,
  Confidence block
```

### `--team` path — `modes/team.md`, skips steps 2–3

```
TEAM PATH  (modes/team.md, after step 1)
  |
  FAN 2-3  (team.md workflow item 2)  ▣▣ / ▣▣▣ researcher teammates, one per method
    cluster — each researches independently, lead routes
    findings across teammates for cross-challenge
  |
  cross-challenge routing  (team.md workflow items 3–5)
  JOIN — only when 3 teammates spawned
  |
CONSOLIDATE  (team.md workflow item 6)  ▣ consolidator (3 teammates) · lead direct (2)
  synthesize → REPORT_OUT, print header table
```

### `plan` path — `modes/plan.md`, skips steps 2–3

```
PLAN PATH  (modes/plan.md, after step 1)
  |
READ RESEARCH  (step P1)
  read prior research report
  auto-detect latest under .reports/research/, or path
  given after `plan`
  |
CODEBASE ANALYSIS  (step P2)
  ▣ solution-architect — map method onto codebase
  |
SYNTHESIZE PLAN  (step P3)
  phased plan → PLAN_OUT, print header table
```

## Degenerate cases

All collapse to inline work with no special handling:

- `foundry:web-explorer` not installed — orchestrator runs the SOTA search inline with `WebSearch`/`WebFetch`; no spawn.
- Web-explorer's envelope declines or redirects the task (out-of-scope citation) — treated identically to absent; orchestrator falls back inline, never forwarded to `research:scientist`.
- Both `plan` and `--team` present — `plan` wins (plan mode has no team variant); one `⚠` warning line printed, `--team` ignored.
- `~/.claude/TEAM_PROTOCOL.md` or `modes/team.md` missing — team mode aborts with `! MISSING`, falls back to single-agent mode.
- `modes/plan.md` missing — plan mode aborts with `! MISSING`; no fallback (plan mode has no non-file path).
- foundry plugin absent entirely — every `foundry:X` substituted with `general-purpose` per `agent-resolution.md`'s fallback table.
- Team mode with 2 teammates — lead synthesizes the report itself; no consolidator spawn.
- Work under the ~73 tool-call inline threshold (typical single-topic run) — no spawn happens in any path; search, teammates, and codebase mapping all conducted inline instead.

## Where this lives in `SKILL.md`

Index of the schema tags above, one row per block. A bare `step N` is a `SKILL.md` step; `plan.md` uses its own `P1`–`P3`; `team.md` has a numbered workflow list instead of step headings, so its tags name the list item.

| Block | Steps |
| -- | -- |
| SETUP | Step 1 (context, flag case-fold, unsupported-flag check, --keep) |
| MODE DISPATCH | Step 1 (early dispatch) |
| DEFAULT PATH | Steps 2, 3 |
| LITERATURE SEARCH | Step 2a |
| CODEBASE CHECK | Step 2b |
| REPORT | Step 3 |
| TEAM PATH | modes/team.md |
| RESEARCHER teammates | team.md workflow item 2 |
| CONSOLIDATE | team.md workflow item 6 |
| PLAN PATH | modes/plan.md |
| READ RESEARCH | plan.md Step P1 |
| CODEBASE ANALYSIS | plan.md Step P2 |
| SYNTHESIZE PLAN | plan.md Step P3 |
| FOLLOW-UP GATE | Follow-up gate section |
