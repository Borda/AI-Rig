---
description: Agent() spawn rules — three-slot delta-first labels, end the turn after spawning with per-agent deadlines, parallel spawn ceilings by model tier; injected by rule-inject.js on the first Agent() call of a session or subagent, loads on first access of a skill or agent file
paths:
  - '**/skills/**/*.md'
  - '**/agents/**/*.md'
---

## Spawn slots — three fields, no repeats

<!-- policy-sibling: plugins/cc_foundry/rules/task-lifecycle.md (§Spawn slots stub) -->

**The principle: a label's only job is to distinguish this row from its siblings.** Text repeated on every row of a batch carries zero information no matter how true or well-written — the reader already knows it, and it consumes the same display budget as text that would tell them something. A rendered row has room for one line; every character of shared text spends that room on nothing, pushes the distinguishing text past the truncation.

This is a property of the batch, not of any single spawn. The same string can be the perfect label for one agent and worthless for five, and nothing in one spawn's own arguments reveals which case it is. So the test is never "is this label accurate?" — it's "does this label differ from what the other rows will print?". Enumerated cases below (PR, repo, verb, role word) are recurring instances, not the rule: anything shared is waste, including shared text these examples never name.

`Agent()` takes three label-bearing slots: `name`, `description`, prompt line 1. Each holds different content. Filling all three from one string wastes two of them.

**The rendered label is prompt line 1, not `description`.** Observed in a real FleetView pane: each row printed `name` plus the leading chars of prompt line 1; the `description` strings set on those same spawns (`arch + SOLID audit`, `coverage + OWASP scan`) didn't appear in that pane at all. Treat prompt line 1 as the slot the user actually reads, the one truncation cuts. `description` may surface in other views — keep it correct — but never rely on it to differentiate rows.

| Slot | Role | Carries | Cap |
| -- | -- | -- | -- |
| `name` | address | delta as a unique kebab-case handle — what `SendMessage` types: `review-arch`, `fix-auth-module` | one line |
| `description` | scope | what work this row does that its siblings do not — adds detail `name` lacks | 3–5 words |
| prompt line 1 | task + visible label | task statement, **delta first**; **only** slot allowed to carry the batch-shared target (PR, repo, branch, run) — and only after the delta, never leading | ≤12 words |

`name` necessarily encodes the delta — it must be unique. `description` shares that stem and expands it; it never merely restates it.

Order every slot **delta-first**: whatever differs across this batch — dimension, directory, module, plugin, task ID, issue number. FleetView truncates the tail, never the head, so a batch differing only past the cut has no labels at all. Never buy room by dropping the delta. One spawn in the batch → nothing is shared, so the target *is* the delta, leads every slot.

**Compose the batch's labels in one pass, never one spawn at a time.** A per-spawn author can't see what its siblings will say, so each one independently reaches for the same framing and the batch converges on an identical prefix. Write all `name`/`description`/prompt-line-1 triples together, before issuing any `Agent()` call, in three steps:

1. **Name the shared context once** — the PR, repo, branch, run, role word, and verb that every row in this batch would carry.
2. **Strike it from every label.** It belongs in the prompt body, which each agent reads in full; it never belongs in a slot that gets truncated.
3. **Keep only the delta** — the dimension, module, directory, finding ID, or issue number that answers "which of these rows is this one?". Read the finished triples as a column, top to bottom: if two rows share their opening words, the strike in step 2 was incomplete.

**Pre-spawn check** (mandatory, cheap, on the composed batch): read the three strings of every spawn side by side. Three fail conditions — `description` adds nothing `name` didn't already say; any slot carries a value every sibling also carries (the PR, the repo, the role word); or prompt line 1 opens with anything shared across the batch, including the verb. Any of them → rewrite before spawning.

Worked ✗/✓ triples — preamble-first prompt, 34 shared chars leading prompt line 1, fixed — in `_full/task-lifecycle.md` §Spawn slots.

`description` is required — the tool rejects the call without it. Omitted `name` → harness assigns one, `SendMessage` can't address that agent.

Boilerplate (`Task tracking:`, `Compact Instructions:`, TEAM_PROTOCOL read, run-dir preamble, envelope spec) goes **after** prompt line 1 — including a preamble a template calls "prepend to every prompt". Task line still first.

Slots and caps come from the live `Agent()` schema in context, not this table alone. Schema carries a field this rule omits → follow the schema, fix the rule. Never drop a field because the rule predates it.

**Binds authored templates too, not just live composition.** An `Agent(...)` call documented inside a skill/mode `.md` file is itself a spawn — the same three-slot, delta-first, pre-spawn-check discipline applies to the template text an author writes, not only to a spawn composed live at execution time. A template that omits `name=`/`description=` entirely, or opens prompt line 1 with shared framing ("Effort level: …. Implement …") instead of the group's delta, reproduces this failure at every future run, silently, until read. One compliant batch spawn elsewhere in the same file does not cover a second: check each batch-spawn site in the file independently.

## After spawning: end the turn

<!-- policy-sibling: plugins/cc_foundry/rules/task-lifecycle.md (§After spawning stub) -->

`Agent()` never blocks. Every spawn runs in the background; the harness re-invokes the orchestrator on completion. No `run_in_background` parameter, no blocking mode.

Nothing to wait through. **Spawn, finish the turn, stop.** The notification is the resume signal.

Forbidden while agents are in flight, every skill:

- No-op calls held open only to keep the turn alive — `Bash(true)`, `Bash(:)`, an `echo` nobody reads, a re-`ls` of a listed directory.
- Text-only waiting turns — "Waiting.", "Standing by.", "Still waiting.", "Waiting on the consolidator."
- Any `sleep`, foreground or backgrounded, and any `while`/`until` poll loop. Foreground `sleep` is harness-blocked; the loop never runs.
- Fixed-interval polling prose ("poll every 5 minutes", "check every `$MONITOR_INTERVAL` seconds") — an interval needs a clock the orchestrator does not have.
- Any waiting tool — `ScheduleWakeup`, `ListAgents`, `Monitor` — and any liveness probe (`find <run-dir> -newer <sentinel>`) used to wait on a spawned agent.

Instead, a **per-agent deadline**: in the spawn response, write the batch's `agent-watch-<batch>.tsv` (one row per agent: name, deliverable, deadline seconds); on every completion or idle notification, run `agent_watch.py` once and act on every row (`_shared/agent-spawn-protocol.md` §Deadlines).

On the notification: an agent whose notification arrived without its deliverable (empty or missing output file, idle without its envelope) is `timed_out` at once — surface with ⏱, never wait for it further, never silently omit. A ⏱ only informs: it never answers, skips or defaults a user question, and never prompts one asking whether to keep waiting.

## Parallel Spawn Ceilings — by Model Tier

<!-- policy-sibling: plugins/cc_foundry/rules/claude-config.md (§Parallel Spawn Ceilings stub) -->

Separate from the per-spawn threshold in `claude-config.md` §Agent/Skill Spawn Discipline: a ceiling on how many `Agent()` calls of a given model tier may be **in flight at once**, summed across every step/phase due in the same response. Independent pools per tier (cheaper/faster tiers tolerate wider fan-out; expensive reasoning tiers stay narrow) — not one shared total. Grow toward a tier's ceiling in waves of ≤5, never a sudden jump straight to it.

| Tier | Ceiling | Covers |
| -- | -- | -- |
| `haiku` | 20 | Cheap/fast mechanical passes (`foundry:humanizer`, `oss:gh-scraper`, `oss:repo-warden`) |
| `sonnet` | 8 | Most execution-tier agents (`foundry:qa-specialist`, `doc-scribe`, `linting-expert`, `web-explorer`, `creator`, `oss:cicd-steward`, `oss:shepherd`, `research:data-steward`) |
| `opus` | 5 | Reasoning-tier agents (`foundry:challenger`, `curator`, `sw-engineer`, `perf-optimizer`, `solution-architect`, `research:scientist`) |

A skill firing several phases in one response (e.g. `/foundry:audit --adversarial`) caps each phase's batch at its agent's tier ceiling — a `foundry:curator` (opus) phase at 5 however wide a `--fast`-style flag would push it, a `foundry:qa-specialist` (sonnet) phase at 8. Tiers in one wave draw from separate pools: an opus-tier and a sonnet-tier phase may run together without summing against one shared number.

Pool = spawn's **effective** model, not the agent's frontmatter default: an `Agent(..., model="sonnet")` override on an opus-pinned agent (e.g. `/oss:resolve` (requires `oss` plugin) `foundry:sw-engineer` groups with no `xhigh` item) draws from the `sonnet` pool.

> Full detail (worked ✓/✗ slot, spawn-prompt, and FleetView examples) in `_full/task-lifecycle.md`. Read before composing multi-agent spawn prompts:
>
> ```bash
> RULE_FULL="$(ls -td ~/.claude/plugins/cache/borda-ai-rig/foundry/*/rules/_full/task-lifecycle.md 2>/dev/null | head -1)"; [ -z "$RULE_FULL" ] && RULE_FULL="plugins/cc_foundry/rules/_full/task-lifecycle.md"  # timeout: 5000
> ```
