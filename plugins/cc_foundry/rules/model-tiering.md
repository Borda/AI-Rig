---
description: Model tiering for Agent() spawns and skill frontmatter — tier ladder (script, Haiku, Sonnet, Opus), checkable escalation triggers, no model pin on inline skills, lean brief-file delegation, author cost checklist; loads on first access of a skill or agent file
paths:
  - '**/skills/**/*.md'
  - '**/agents/**/*.md'
---

## Tier ladder — start at the lowest tier a task class has passed calibration for

| Tier | Runs | Use for |
| -- | -- | -- |
| 0 script | `bin/*.py`, no model | fetch, paginate, dedupe, filter, count — any fixed command list |
| 1 `haiku` | one shot, no tool loop, input prefetched to a file | mechanical classification, extraction, formatting |
| 2 `sonnet` | main thread, default specialists | orchestration, judgment, tests, docs, lint |
| 3 `opus` | reasoning specialists | high-risk items, adversarial review, architecture, ambiguous cross-file design |

- **Escalate one tier at a time, one retry per tier, only on a checkable trigger**: missing or schema-invalid envelope · failed completeness count (items out ≠ items in) · `## Confidence` below 0.85 · the same failure signature twice · an item flagged high-risk. The retry reuses the brief file plus the prior output path, never the transcript. No trigger, no escalation — "looks hard" is not one.
- **De-escalate only on evidence**: a `foundry:calibrate` result at the cheaper tier at least equal to the current one on the same inputs (`agents/curator.md` model policy). Haiku 5.5 bills 5× above 100K prompt tokens — keep Haiku runs under 100K.
- **Effective tier** = the agent's frontmatter pin unless the call passes `Agent(model=…)`; the override is how one call site drops a tier without touching the agent (§Parallel Spawn Ceilings in `agent-spawn.md` counts it).

## No model pin on inline skills

A `model:` on a skill without `context: fork` replaces the session model for the rest of that turn and the next turn reverts, so the whole cached prompt prefix is rewritten when the model changes — measured $1.4 to $1.5 per `/oss:resolve` run in an Opus session, of which removing the pin saves about $0.4 (the former Sonnet turns now run on Opus); the large saving needs the session itself on Sonnet. `model:` belongs on agents and `context: fork` skills only. The session picks its model once; never switch the main-thread model mid-session. Measure with `/foundry:audit --efficiency --measured` (Phase B3, check R1).

## Delegate lean — what a spawn receives

Every subagent starts at a floor of about 61K tokens (the global instructions included) that no prompt trims, so pay it once per batch, not per item. Keep the rest small:

1. **Brief in a file**, not the prompt body: goal, acceptance test, input paths with line ranges, constraints, output path, envelope schema. No session history, prior reasoning or inlined file contents.
2. **Script the deterministic prefix.** A spawn prompt that lists five or more literal `gh`/`git` commands is a `bin/` script not yet written; the agent then reads the file it produced.
3. **Bound tool output** (`--jq`, `head`, line ranges): every result is re-read on every later call.
4. **Envelope back, full output to a file**; never fork; the fewest tools the agent type allows.
5. **Batch items per agent** up to about 55 calls (`claude-config.md` §Agent/Skill Spawn Discipline).
6. **State a tool budget** for classification-only spawns ("Read and Write only"). Unbounded, they drift into repository archaeology — measured: 25 calls, 61K to 143K context, for work that needed about 8.

## Author checklist — before shipping a skill or agent

- Inline skill: no `model:`. Agent or `context: fork` skill: `model:` set.
- Every `Agent()` spawn has a tier on purpose — the pin, or `model=` — and Opus only where a trigger above names it.
- No spawn prompt lists five or more literal CLI calls; the script exists in `bin/` and is wired from the skill.
- Every spawn prompt names its input file, output path, envelope and tool budget.
- Bash blocks and `AskUserQuestion` gates follow the turn-batching and idle-gate rules (`claude-config.md` §Turn Batching, `compaction.md`).
- Measured, not guessed: an inline `model:` pin change is checked with `/foundry:audit --efficiency --measured` before and after (it keeps a baseline and prints the delta): no model switch inside the skill's own span, pre-gate median not higher. The audit asks which projects to read: choose the repositories where the skill actually runs. Spawn tiers, briefs and scripts are measured with `foundry:profile` before and after.
