<!-- file: ARCH.md — consumers: none (documentation; never loaded at runtime) -->

# `/oss:review` — execution architecture

> **Documentation, not contract.** The skill never loads this file; `SKILL.md` and `modes/*.md` are the only normative sources. It exists so the shape — what fans out, what joins, what blocks — can be read without walking a 1000-line skill.
>
> **Keep it current.** Any change to block order, gate placement, fan-out width or what runs beside what lands here in the same commit. Every schema block carries a `(step N)` tag naming its `SKILL.md` step; renumbering a step updates the tag and the index at the end. A schema that disagrees with `SKILL.md` is worse than none — `SKILL.md` wins every time.

## Legend

```
▣ agent spawn      ◆ blocking user gate      ‖ same turn
FAN n  work splits into n lanes dispatched together
JOIN   lanes collected; nothing past it starts until all land
```

## Schema

```
SETUP  (step 0)
  flags · gh PR snapshot (view/diff/checks) · PR_TYPE classify
  |
◆ EXISTING REPORT GATE  (step 0 — existing-report guard)  [conditional]
  (a) reuse, stop · (b) re-run · (c) reply-draft → REPLY
  |
PRE-FLIGHT  (step 1)
  ◆ report path, no --reply?                          [conditional]
      → redirect to REPLY, or stop
  codemap gates (build/stale prompt)                   [conditional]
  ◆ unknown flag?                                      [conditional]
  worktree entry (EnterWorktree)                    [WT_ENABLED only]
  file-scope detect → CICD_ONLY / DOCS_ONLY / DOCS_CICD / Python
  |
ACCEPTANCE GATE  (step 1 — acceptance gate)
  Stage 1 REJECT  ▣ foundry:challenger (conduct/license only)
      confirmed → write report, skip to REPORT HEADER + FOLLOW-UP GATE
  Stage 2 BLOCK (CI red) — not terminal, fan-out still runs
  |
  +--------------------- FAN 2 ----------------------+
  |                                                  |
AGENT LAUNCH  (step 2)                  ECOSYSTEM + OSS CHECKS  (step 3)
  ▣ 1 batch                               ecosystem impact  (step 3a)
  Agent 0 blind-solve [FEATURE/MIXED]       (gh code search, ~10-30s)
    (step 1 — Agent 0)                    OSS signal checks  (step 3b)
  bridge:review (Codex)                     (script)
  issue agent (all linked issues)
  ≤3 ranked dimension agents
  + qa-specialist (pinned)
  |                                                  |
  +--------------------- JOIN -----------------------+
  |
CROSS-VALIDATE  (step 4)
  ▣ ≤3 verifiers, one per critical/blocking finding
  |
CONSOLIDATE  (step 5)  ▣ sw-engineer / doc-scribe / qa-specialist, by PR_TYPE
  REPORT HEADER  (step 5b)  print (hook-enforced)
  |
CODEX DELEGATION  (step 6)  optional, no gate
  |
WORKTREE EXIT  (step 7)  (if entered)
  REPLY_MODE=true  → REPLY
  REPLY_MODE=false → below
  |
◆ FOLLOW-UP GATE  (step 7a)  /oss:resolve suggestion
  [skipped: resolve already running for this PR]
  |
CONFIDENCE BLOCK  (step 7b) · stop

REPLY  (step 8)  (--reply, or an existing-report/direct-path redirect)
  ▣ oss:shepherd — draft contributor reply
  Confidence block · stop
```

## Fan and join points

| Fans into | Lanes | Width bound by | Joins at |
| -- | -- | -- | -- |
| AGENT LAUNCH ‖ ECOSYSTEM + OSS CHECKS | 2 | fixed | CROSS-VALIDATE |
| AGENT LAUNCH: Agent 0 blind-solve | 0–1 | FEATURE/MIXED scope only | before JOIN |
| AGENT LAUNCH: bridge:review (Codex) | 0–1 | bridge availability | before JOIN |
| AGENT LAUNCH: issue agent | 0–1 | PR body links issues (one spawn covers all, cap 3) | before JOIN |
| AGENT LAUNCH: dimension agents | ≤`FANOUT_MAX` (3) ranked + qa-specialist pinned outside cap | scope preselection, then relevance ranking; `--full` = no cap, every survivor | before JOIN |
| CROSS-VALIDATE | ≤3 | critical/blocking finding count (batched ≤2/verifier beyond 3) | CONSOLIDATE |

The top fan is free — it rides an idle window AGENT LAUNCH already had. ECOSYSTEM + OSS CHECKS is issued on an AGENT LAUNCH wake-up rather than a dedicated turn, once `PR_BASE` is bound; neither lane waits on the other's output.

**The boundary.** CROSS-VALIDATE cannot join the top fan — its verifiers target the critical/blocking findings AGENT LAUNCH's agents produce, so it must wait for those findings to exist.

`PR_TYPE` overrides the AGENT LAUNCH lineup before ranking ever runs — see Mode branches below; both `DOCS_TYPING` and `TESTS_CI` skip the challenger and Agent 0 regardless of scope.

## Gates

| Gate | Always? | Blocks |
| -- | -- | -- |
| existing review report already on disk | conditional | SETUP, before PRE-FLIGHT or any fan-out |
| report path passed, no `--reply` | conditional (`DIRECT_PATH_MODE` only) | PRE-FLIGHT entry — redirects to REPLY or stops |
| codemap index missing/stale | conditional | PRE-FLIGHT |
| unknown flag(s) | conditional | PRE-FLIGHT |
| **FOLLOW-UP GATE** | **always** (default, non-reply path) | CONFIDENCE BLOCK |

Normal PR-review path (`REPLY_MODE=false`, `DIRECT_PATH_MODE=false`) costs at most 4 `AskUserQuestion` calls; `--reply` and direct-path modes cost 0–1.

## Mode branches

| Mode | Schema |
| -- | -- |
| `DOCS_TYPING` (annotation-only `.py`, no logic) | PRE-FLIGHT skips file-scope/SCOPE classification; AGENT LAUNCH = doc-scribe only, challenger off; CONSOLIDATE = doc-scribe |
| `TESTS_CI` (test files + CI config only) | same skip; AGENT LAUNCH = qa-specialist + linting-expert, challenger off; CONSOLIDATE = qa-specialist |
| `CICD_ONLY` (no `.py`/`.md`/`.rst` changed) | AGENT LAUNCH narrows to cicd-steward + bridge + challenger (if enabled) |
| `DOCS_ONLY` (no `.py` changed) | AGENT LAUNCH narrows to doc-scribe + bridge + challenger (if enabled); sw-engineer and the issue agent never spawn |
| `DOCS_CICD` (no Python at all) | AGENT LAUNCH narrows to cicd-steward + doc-scribe + bridge + challenger (if enabled) |

## Degenerate cases

All collapse to a shorter path with no special handling beyond what is described above:

- **`--reply`** (`REPLY_MODE=true`) — WORKTREE EXIT skips the FOLLOW-UP GATE and CONFIDENCE BLOCK; jumps straight to REPLY's `oss:shepherd` spawn, Confidence block lives there instead.
- **Direct-report path** (`DIRECT_PATH_MODE=true`, a `.md` report path instead of a PR#) — skips SETUP's PR-snapshot fetch through CONSOLIDATE entirely: either an `AskUserQuestion` redirect (no `--reply`) or straight to REPLY.
- **Existing-report guard, option (a)** — reuses the prior report and stops before PRE-FLIGHT ever runs. Option (c) — redirects straight to REPLY, same skip as the direct-report path.
- **Stage 1 REJECT** (ACCEPTANCE GATE) — skips AGENT LAUNCH through CONSOLIDATE entirely (no fan-out, no consolidator); the orchestrator writes the report itself and jumps to REPORT HEADER's print + the FOLLOW-UP GATE.
- **No Python, doc, or CI/CD files changed at all** — file-scope detection exits the skill in PRE-FLIGHT, before any fan-out.

## Where this lives in `SKILL.md`

Index of the `(step N)` tags in the schema above, one row per block.

| Block | Steps |
| -- | -- |
| SETUP | 0 |
| EXISTING REPORT GATE | 0 (existing-report guard) |
| PRE-FLIGHT | 1 |
| ACCEPTANCE GATE | 1 (acceptance gate) |
| AGENT LAUNCH | 1 (Agent 0 gather), 2 |
| ECOSYSTEM + OSS CHECKS | 3a, 3b |
| CROSS-VALIDATE | 4 |
| CONSOLIDATE | 5, 5b |
| CODEX DELEGATION | 6 |
| WORKTREE EXIT | 7 |
| FOLLOW-UP GATE | 7a |
| CONFIDENCE BLOCK | 7b |
| REPLY | 8 |
