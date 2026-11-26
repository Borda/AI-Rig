<!-- file: verdict-states.md — consumers: plugins/cc_oss/skills/review/templates/consolidator-prompt.md (loaded via cat), plugins/cc_oss/bin/find_review_report.py (enforces the impossible states and Readiness ceilings), plugins/cc_oss/skills/review/modes/acceptance-gate.md (terminal publisher) -->

# Review verdict states

Truth table version: 1

A version-one review header carries three independent axes. Each answers its own question, so a mix that looks contradictory side by side is usually a valid state:

- `Gate:` — CI merge gate at the reviewed head: `REVIEW_V1_PASS` / `REVIEW_V1_BLOCK` from the same-head required checks (`modes/ci-status.md`), or `REVIEW_V1_REJECT_<GROUND> @<sha>`, a premise rejection from the acceptance gate.
- `Outcome:` — source judgment of the code (source-5-v2), or literal `null` when no reviewer could judge.
- `Readiness:` — verification state: `complete`, `blocked` (an unavailable prerequisite after feasible checks) or `incomplete` (a reviewer missing or a feasible check undone).

`judged` below means `APPROVE`, `MINOR_EDITS`, `MAJOR_REVISION` or `INSUFFICIENT_BUT_PROMISING` (archived source-5-v1 reports: `NEEDS_WORK` for level 3).

## Valid states

| State | Gate | Outcome | Readiness | Meaning |
| -- | -- | -- | -- | -- |
| `judged-complete` | PASS or BLOCK | judged | complete | Source judged, every feasible check done. `BLOCK` beside a judged outcome is normal: CI fails at the head, the source judgment stands on its own. |
| `judged-blocked` | PASS or BLOCK | judged | blocked | Source judged; an unavailable prerequisite (`hardware`, `approval`, `external`, or a failed provisioning step) left a candidate UNVERIFIED or a reviewer undecidable. |
| `approve-blocked` | PASS or BLOCK | APPROVE | blocked | No required source change, verification unfinished. Summary and Next steps state the open verification, never an unconditional approval or merge-readiness claim. |
| `judged-incomplete` | PASS or BLOCK | judged | incomplete | Source judged; a reviewer is missing (an unslotted promoted unit included) or a feasible check is undone. |
| `null-blocked` | PASS or BLOCK | null | blocked | A completed reviewer could not judge the source and nothing else is owed. |
| `null-incomplete` | PASS or BLOCK | null | incomplete | A completed reviewer could not judge, and a reviewer is missing or a candidate is pending. |
| `premise-reject` | REJECT_GROUND @sha | REJECT | complete | Initial premise-only rejection, or a late rejection with no open work in the retained review. |
| `late-premise-reject-incomplete` | REJECT_GROUND @sha | REJECT | incomplete | The retained source review still has owed reviewers or feasible checks. |
| `late-premise-reject-blocked` | REJECT_GROUND @sha | REJECT | blocked | An unavailable prerequisite prevents completing the retained review. |

## Impossible states

| State | Gate | Outcome | Readiness | Why impossible | Write instead | Enforced by |
| -- | -- | -- | -- | -- | -- | -- |
| `null-complete` | PASS or BLOCK | null | complete | No judgment means the source verification cannot be complete. | `null-blocked` or `null-incomplete` | intake |
| `reject-without-reject-gate` | PASS or BLOCK | REJECT | complete | A source rejection publishes only through the terminal publisher. | `premise-reject` through Stage 1, or `MINOR_EDITS`/`MAJOR_REVISION` for fixable defects | intake |
| `reject-gate-without-reject` | REJECT_GROUND @sha | judged | complete | The gate records a premise rejection that the outcome denies. | `premise-reject` | intake |
| `null-without-undecidable-reviewer` | PASS or BLOCK | null | blocked | A null outcome needs a completed reviewer that could not judge. | The judgment the completed reviewers support | intake |
| `complete-with-open-work` | PASS or BLOCK | judged | complete | An UNVERIFIED or null candidate is still open. | `judged-blocked` or `judged-incomplete` | intake |
| `blocked-with-owed-work` | PASS or BLOCK | judged | blocked | A missing reviewer is owed, which outranks blocked. | `judged-incomplete` | intake |
| `blocked-by-provisionable-tooling` | PASS or BLOCK | judged | blocked | An UNVERIFIED `package` candidate without `provisioning_failure` is a feasible check left undone. | Provision and run the check, or record the candidate null (`judged-incomplete`) | producer |
| `approve-with-required-change` | PASS or BLOCK | APPROVE | complete | A CONFIRMED critical or high finding is a required source change. | `MINOR_EDITS` or `MAJOR_REVISION` by evidenced impact | producer |
| `complete-with-unslotted-promoted-unit` | PASS or BLOCK | judged | complete | `lineup.md` records a promoted unit that found no slot, so a required reviewer is missing. | `judged-incomplete` with that unit's roles as `presence: missing` | producer |

Enforced by:

- `intake` — `find_review_report.py` refuses the report at `/oss:resolve` intake and in the review's publication gate, and `--assess` grades the gap.
- `producer` — only the publication gate (`--producer`) refuses it, so fresh publications cannot carry it while saved reports keep reading. The gate needs every field a `producer` row reads: an UNVERIFIED candidate without `missing_capability_kind` is refused outright (a candidate carried verbatim by a fill-gaps plan, passed as `--fill-gaps`, excepted), so omitting the field never evades `blocked-by-provisionable-tooling`.

The publication gate also refuses a report that lacks an `Unreviewed dimension: <unit>` line for a unit `lineup.md` records as `dropped` or `promoted-unslotted`. Every preselected role on a `promoted-unslotted` row needs its own `presence: missing` assessment, even when another gap already makes Readiness `incomplete`. It refuses a fresh publication with no `lineup.md` as well; only a fill-gaps run (`--fill-gaps`) writes none.

## Confidence ceilings

The aggregate confidence is the median of the numeric reviewer confidences, capped by the lowest ceiling whose named coverage gap applies. Ceilings only: no floor, no variance target, no stacking.

| Named coverage gap | Ceiling | Enforced by |
| -- | -- | -- |
| Readiness `incomplete` | 0.70 | producer |
| Readiness `blocked` | 0.80 | producer |
| PR code not executed on a code PR (no test, probe or overlay run at the reviewed head) | 0.80 | consolidator |
| A unit dropped by `FANOUT_MAX` without a promotion trigger | 0.85 | consolidator |

A Codex lane reply without a numeric envelope confidence, or one that is null, failed or blocked, never enters the median.
