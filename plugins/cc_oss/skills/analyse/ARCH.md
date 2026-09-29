<!-- file: ARCH.md — consumers: none (documentation; never loaded at runtime) -->

# `/oss:analyse` — execution architecture

> **Documentation, not contract.** The skill never loads this file; `SKILL.md` and `modes/*.md` are the only normative sources. It exists so the shape — what fans out, what joins, what blocks — can be read without walking the mode files one by one.
>
> **Keep it current.** Any change to block order, gate placement, fan-out width or what runs beside what lands here in the same commit. A schema that disagrees with `SKILL.md` is worse than none — `SKILL.md` wins every time.

## Legend

```
▣ agent spawn      ◆ blocking user gate      ‖ same turn
FAN n  work splits into n lanes dispatched together
JOIN   lanes collected; nothing past it starts until all land
```

## Schema

```
SETUP
  flag parse · agent resolution
  |
  ◆ unknown flag?                              [conditional]
  |
MODE DISPATCH
  number    → THREAD MODE
  vitality  → VITALITY MODE
  ecosystem → ECOSYSTEM MODE
```

Full detail for each branch — they diverge too far past this point to share one schema — lives under **Mode branches** below.

## Fan and join points

| Fans into | Lanes | Width bound by | Joins at |
| -- | -- | -- | -- |
| THREAD FETCH (`gh` calls matching `$TYPE`) | 2–4 | matched type block: issue 2 · PR 4 · discussion 1 | WIDE-NET DUP SEARCH |
| VITALITY AXIS SCORE (`oss:repo-warden`) | 3 | fixed — three axis groups, not a flag-driven width | ASSEMBLE SCORES |
| VITALITY ADVERSARIAL REVIEW (`foundry:challenger` + `bridge:review`) | 2 | `CODEX_AVAILABLE`; collapses to 1 (challenger only) when 0 | rework decision, each of ≤ `REWORK_MAX` (2) iterations |

THREAD FETCH's lanes are plain `gh` calls, not agent spawns, so that fan is free — it rides bash calls the orchestrator already had in the same turn. Both VITALITY fans are genuine `Agent()` spawns; `modes/vitality.md` notes each one costs ~120,851 tok of fixed overhead (~73 tool-calls' worth) regardless of the work inside it, which is why DATA FETCH and CODEX INDEPENDENT REVIEW stay single spawns rather than folding into a wider fan.

## Gates

| Gate | Always? | Blocks |
| -- | -- | -- |
| unknown `--<token>` flag | conditional | start of run, all modes |
| **FOLLOW-UP** | conditional — fires unless `REPLY_MODE=true` (thread only) | end of run → Confidence block |

Reply mode (`--reply`, thread only) never reaches the follow-up gate: SHEPHERD REPLY jumps straight to the Confidence block. Every other path (thread without `--reply`, vitality, ecosystem) always hits it. `TYPE=unknown`, the direct-report-path misuse, and the DIRECT_PATH_MODE bad-combo check are hard `exit 1` stops, not `AskUserQuestion` gates.

## Mode branches

### Thread mode (no `--reply`)

```
CACHE CHECK
  numeric args only; hit skips primary fetch (wide-net still live)
  |
DETECT TYPE
  TYPE=unknown → hard stop (exit 1, not a gate)
  |
FETCH  ‖ parallel gh calls, width by type
  issue 2 · PR 4 · discussion 1
  |
WIDE-NET DUP SEARCH
  sequential, re-fetches title independently
  |
REPRO CHECK  ▣ 1 agent                    [HAS_REPRO=true only]
  sw-engineer default, qa-specialist on pytest patterns
  |
STALE-SYMBOL CHECK
  optional, codemap
  |
WRITE REPORT
  |
◆ FOLLOW-UP GATE                                        always
  |
CONFIDENCE BLOCK
```

### Thread mode (`--reply`)

```
DIRECT REPORT PATH?
  bad combo (path, no --reply) → hard exit 1, not a gate
  path + --reply + file exists → SHEPHERD REPLY (skip below)
  |
FRESH REPORT CHECK
  no drift → SHEPHERD REPLY (skip fetch chain)
  drift or report missing
  |
[ CACHE CHECK → DETECT TYPE → FETCH → WIDE-NET DUP SEARCH →
  REPRO CHECK → STALE-SYMBOL CHECK → WRITE REPORT — identical
  chain to THREAD MODE (no --reply) above ]
  |
SHEPHERD REPLY  ▣ oss:shepherd
  |
CONFIDENCE BLOCK
  (no AskUserQuestion anywhere in this path)
```

### Vitality mode

```
DATA FETCH  ▣ oss:gh-scraper
  |
  +--------------------- FAN 3 -----------------------+
  |
AXIS SCORE  ▣▣▣ repo-warden × 3 (Groups A / B / C)
  each reads DATA_FILE independently, no shared state
  |
  +--------------------- JOIN -------------------------+
  |
ASSEMBLE SCORES
  |
REPORT GENERATION
  scaffold + optional codemap structural signals
  |
  QUICK_MODE=true → skip to TERMINAL SCORECARD
  (Codex + Adversarial Review become "skipped (--quick)")
  |
CODEX INDEPENDENT REVIEW  ▣ bridge:review    [CODEX_AVAILABLE=1 only]
  |
  +--------------------- FAN 2 -----------------------+
  |                                                    |
CHALLENGER  ▣ 1 agent                    CODEX REVIEW  ▣ bridge:review
  stress-tests scoring, evidence           same report, no shared input
  |                                                    |
  +--------------------- JOIN --------------------------+
  |
  needs_rework → ▣ sw-engineer per flagged section,
  fresh spawn, loop ≤ REWORK_MAX (2) back to CHALLENGER/CODEX REVIEW
  |
TERMINAL SCORECARD
  |
◆ FOLLOW-UP GATE                                             always
  |
CONFIDENCE BLOCK
```

### Ecosystem mode

```
SEARCH PYPI REVERSE-DEPS
  gh api search/code — from mypackage import
  |
SEARCH CONDA-FORGE
  gh api search/code — feedstock meta.yaml
  |
WRITE REPORT
  |
◆ FOLLOW-UP GATE                                        always
  |
CONFIDENCE BLOCK
```

Sequential throughout, zero spawns — no `FAN` belongs on this mode's schema.

## Degenerate cases

- `--quick` (vitality only) — CODEX INDEPENDENT REVIEW and ADVERSARIAL REVIEW skipped entirely; total spawns drop to 4 (gh-scraper + 3 repo-wardens); single-pass scorecard at capped confidence.
- Ecosystem mode itself — no type detection, no cache layer, no Reproduction Check; two sequential `gh api search/code` calls, a report write, and the always-on follow-up gate; no agent spawn anywhere in this mode.
- Numeric mode, cache hit with no drift (`FAST_PATH=true`) — skips the primary `gh` fetch entirely; PR mode's reviews/inline-comments/checks/diff still fetch live regardless (never cached).
- `--reply` with an existing fresh report — skips the whole fetch chain outright, jumps straight to SHEPHERD REPLY.
- `path/to/report.md --reply` (direct path mode) — skips auto-detection and analysis entirely; branches straight to SHEPHERD REPLY once the file's existence is confirmed.
- `CODEX_AVAILABLE=0` — CODEX INDEPENDENT REVIEW never runs, and ADVERSARIAL REVIEW spawns only `foundry:challenger` (width 1, not 2).
- `REWORK_VERDICT=pass` on the first pass — the loop exits after one iteration; the per-section rework spawn never fires.
- `HAS_REPRO=false` (thread mode) — REPRO CHECK skips its spawn entirely; report shows "No Example" instead of a reproduction verdict.

## Where this lives in `SKILL.md`

Navigation only; the step numbers carry no meaning at this level.

| Block | Steps |
| -- | -- |
| SETUP (agent resolution, flag parse) | Agent Resolution, Step 1 |
| DIRECT REPORT PATH / FRESH REPORT CHECK | Step 2 |
| CACHE CHECK | Step 3 |
| DETECT TYPE | Step 4 |
| MODE DISPATCH | Step 5 |
| THREAD FETCH … WRITE REPORT | `modes/thread.md` |
| VITALITY DATA FETCH … TERMINAL SCORECARD | `modes/vitality.md` Steps 1–7 |
| ECOSYSTEM SEARCH … WRITE REPORT | `modes/ecosystem.md` |
| FOLLOW-UP GATE | Step 6a |
| CONFIDENCE BLOCK | Step 6b, Step 7 |
| SHEPHERD REPLY | Step 7 |
