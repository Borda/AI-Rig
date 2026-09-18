## Task lifecycle sequencing — worked examples

Full detail behind the `rules/task-lifecycle.md` stub — three-slot spawn labelling, FleetView examples, end-turn-after-spawn contract. Rules themselves (TaskUpdate-before-long-output, subagent task prohibition, slot/unique-first constraints, the no-op-filler ban) live in the stub, always loaded — this file is illustration only.

### Spawn slots — three fields, no repeats

`Agent()` takes three label-bearing slots — `name`, `description`, and prompt line 1. Filling all three with the same string wastes two of them. The slot rendered and truncated is prompt line 1, so it carries the strictest delta-first obligation of the three:

```text
✗  name: review-sw-engineer   description: sw-engineer review — PR #1424 roboflow/rf-detr
                              prompt:      First run Bash export CSID=…   [label displaced by preamble]

✗  name: review-sw-engineer   description: arch + SOLID audit
                              prompt:      Review PR #1424 roboflow/rf-detr — architecture, SOLID, error paths
                                           [description fixed, but the rendered slot still leads with 34 shared chars]

✓  name: review-arch          description: arch + SOLID audit
                              prompt:      Architecture, SOLID, error paths — PR #1424 roboflow/rf-detr
```

Boilerplate-first prompt → every agent reads one useless label (e.g. "Task tracking: do NOT call TaskCreate or TaskUpdat…"):

```text
✓  Fix token-expiry off-by-one in auth/middleware.py
   Read ${HOME}/.claude/TEAM_PROTOCOL.md — AgentSpeak v2. …
   Task tracking: do NOT call TaskCreate or TaskUpdate — lead owns all task state. …

✗  Task tracking: do NOT call TaskCreate or TaskUpdate — lead owns all task state.
   You are a foundry:sw-engineer teammate fixing … [label now useless]
```

### Slot content: unique-first

N agents, same task family → every slot leads with per-agent delta (dir/plugin/module/dimension). Prompt line 1 is the only slot allowed to name the shared target at all, and names it after the delta, never before. Cap 1 terminal line per column — front-load differentiator, FleetView truncates tail not head.

```text
✓  B1 — cc_develop: session-scope TMPDIR sentinels
✓  B2 — cc_foundry: session-scope TMPDIR sentinels

✗  B1 — session-scope TMPDIR sentinels in plugins/cc_...
✗  B2 — session-scope TMPDIR sentinels in plugins/cc_...  [same prefix, rows indistinguishable]
```

#### When every agent shares one target

The dir-varying case above is the easy one. The hard one is a fanout over a single target — one PR, one branch, one run — where the only thing that differs is the dimension. Reading the label rule as "role + target" puts the shared half in every slot, produces rows nobody can tell apart:

```text
✗  description: Review PR #596 (roboflow/trackers) — architecture/...
✗  description: Review PR #596 (roboflow/trackers) — performance A...
✗  description: Review PR #596 (roboflow/trackers) — test coverage...   [23 identical chars, then truncation]
```

Fixing `description` alone doesn't fix the row, because `description` isn't what the pane prints. Observed in a real FleetView pane: rows printed `name` plus the leading chars of prompt line 1, and the correct `description` values set on those same spawns never appeared. The shared prefix simply moved one slot over, kept winning:

```text
✗  ◯ review-arch       Review PR 3 Borda/lucid-YOLO — architecture, SOLID...
✗  ◯ review-perf-api   Review PR 3 Borda/lucid-YOLO — perf claim verifica...
✗  ◯ review-challenge  Review PR 3 Borda/lucid-YOLO — adversarial challen...   [28 identical chars, then truncation]

✓  ◯ review-arch       architecture, SOLID, numerics — PR 3 Borda/lucid-YOLO
✓  ◯ review-perf-api   perf claim verification — PR 3 Borda/lucid-YOLO
✓  ◯ review-challenge  adversarial challenge — PR 3 Borda/lucid-YOLO
```

So delta-first binds hardest on prompt line 1, the slot the user actually reads:

```text
✓  name: review-arch       description: architecture + SOLID     prompt: Architecture, SOLID, numerics — PR #596 roboflow/trackers
✓  name: review-perf-api   description: perf + API design        prompt: Perf claims + API design — PR #596 roboflow/trackers
✓  name: review-qa-sec     description: test coverage + security prompt: Test coverage + security scan — PR #596 roboflow/trackers
```

The target isn't the label. Every agent in the batch already knows which PR it's reviewing — the prompt body says so. The row exists to answer "which of these three is this?", and only the dimension answers that. A merged spawn covering two dimensions names both, because that's its delta.

The same reading applies to a verb: "Review" is shared by all three rows, buys nothing at the front. Drop it from prompt line 1 too — the dimension alone is the task statement, and the target follows the dash.

### After spawning: end the turn

The stub forbids no-op filler while agents are in flight. What that looks like when the rule is missing, from a real `/oss:resolve` Step 9 run:

```text
✗  ⏺ Bash(true)          ⎿  (No output)
   ⏺ Bash(true)          ⎿  (No output)
   ⏺ Waiting for step9-linting and step9-qa.
   ⏺ Bash(true)          ⎿  (No output)
   ⏺ Still waiting.

✓  [spawn both agents in one message, then the turn simply ends]
   [completion notification arrives → read $RUN_DIR/linting-expert-step9.md, $RUN_DIR/qa-specialist-step9.md]
```

Each filler call is a full model turn: the entire live context is re-read to produce `true`. A handful of them costs more than the agents being waited on.

The mistake is understandable and worth naming, because skill prose still invites it: a skill that says "spawns are synchronous — the framework awaits each response natively" describes a harness that no longer exists. `Agent()` has no `run_in_background` parameter to pass, because every spawn is already background. Skill fragment and this rule disagree → this rule is current.
