## Task lifecycle sequencing — worked examples

Full detail behind the `rules/task-lifecycle.md` stub — three-slot spawn labelling, FleetView examples, and the end-turn-after-spawn contract. Rules themselves (TaskUpdate-before-long-output, subagent task prohibition, slot/unique-first constraints, the no-op-filler ban) live in the stub, always loaded — this file is illustration only.

### Spawn slots — three fields, no repeats

FleetView shows `name`, `description`, and the prompt's leading chars as three columns. Filling all three with the same string wastes two of them:

```text
✗  name: review-sw-engineer   description: sw-engineer review — PR #1424 roboflow/rf-detr
                              prompt:      First run Bash export CSID=…   [label displaced by preamble]

✓  name: review-sw-engineer   description: arch + SOLID audit
                              prompt:      Review PR #1424 roboflow/rf-detr — architecture, SOLID, error paths
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

N agents, same task family → every slot leads with per-agent delta (dir/plugin/module/dimension), shared target in prompt line 1 only. Cap 1 terminal line per column — front-load differentiator, FleetView truncates tail not head.

```text
✓  B1 — cc_develop: session-scope TMPDIR sentinels
✓  B2 — cc_foundry: session-scope TMPDIR sentinels

✗  B1 — session-scope TMPDIR sentinels in plugins/cc_...
✗  B2 — session-scope TMPDIR sentinels in plugins/cc_...  [same prefix, rows indistinguishable]
```

#### When every agent shares one target

The dir-varying case above is the easy one. The hard one is a fanout over a single target — one PR, one branch, one run — where the only thing that differs is the dimension. Reading the label rule as "role + target" puts the shared half in every slot and produces rows nobody can tell apart:

```text
✓  name: review-arch       description: architecture + SOLID
✓  name: review-perf-api   description: perf + API design
✓  name: review-qa-sec     description: test coverage + security
   [prompt line 1 of each: "Review PR #596 roboflow/trackers — <its dimensions>"]

✗  description: Review PR #596 (roboflow/trackers) — architecture/...
✗  description: Review PR #596 (roboflow/trackers) — performance A...
✗  description: Review PR #596 (roboflow/trackers) — test coverage...   [23 identical chars, then truncation]
```

The target is not the label. Every agent in the batch already knows which PR it is reviewing — the prompt body says so. The row exists to answer "which of these three is this?", and only the dimension answers that. A merged spawn covering two dimensions names both, because that is its delta.

The same reading applies to a verb: "Review" is shared by all three rows and buys nothing at the front.

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

The mistake is understandable and worth naming, because skill prose still invites it: a skill that says "spawns are synchronous — the framework awaits each response natively" describes a harness that no longer exists. `Agent()` has no `run_in_background` parameter to pass, because every spawn is already background. When a skill fragment and this rule disagree, this rule is current.
