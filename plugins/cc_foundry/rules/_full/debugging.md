---
description: Root-cause diagnosis protocol — confirm root cause before fixing; post-fix validation loop; challenger feedback; anti-patterns
paths:
  - '**'
---

## Root-Cause Discipline

Never patch symptom. Symptoms = evidence — treat as signal, not problem.

### Diagnosis loop

1. **Observe symptoms** — collect all failure signals before forming hypothesis
2. **Hypothesize root cause** — name specific mechanism; not "config wrong" but "config key X missing from production loader → Y falls back to default Z → symptom"
3. **Confirm root cause** — find direct evidence in code, logs, or tests that hypothesized mechanism exists and is active; no confirmation = no fix
4. **Fix the mechanism, not the signal** — change structural cause; never add guard that suppresses symptom without removing cause
5. **Validate** — re-run all original failure signals; confirm every symptom resolved; if any remain → root cause was incomplete or not only one → return to step 2

**Loop bound**: max 3 diagnosis-fix iterations (matches §Safety breaks default); at limit — stop, report remaining symptoms, invoke `AskUserQuestion` before continuing.

**Early-stop on repeated failure signature**: if a fix attempt draws the *same* rejection/failure signature (same error string, same failing assertion, same reviewer objection) twice in a row, stop immediately — don't wait for the iteration cap. Repeat means the last attempt added no new information toward resolution; a 3rd identical try only spends tokens. Report both attempts and the unchanged signature, then `AskUserQuestion`.

### Falsification check

Before marking fix complete: "Could this symptom have a second independent root cause not addressed by this fix?" If yes → diagnose and fix that cause too before closing.

### Post-fix challenger invocation

**Dispatch rule**: post-fix re-invoke is fresh orchestrator-initiated dispatch — not a nested call from within an active challenger run. Challenger's SKIP rule ("already inside active challenger context") still applies when executing inside challenger review; orchestrator waits for challenger to finish, then dispatches a new instance for post-fix verification.

**Never dispatch challenger as `subagent_type: "fork"`.** A fork inherits the full implementer conversation — its reasoning, self-assessment of success, framing of the symptom. Verification inside that inherited context tends toward confirming the implementer's own conclusion rather than independently re-deriving it. Challenger **must get** only the diff, the original symptom description, and the spec — no implementer reasoning trail — matching the "reviewer doesn't inherit implementer's conversation" isolation principle.

**Stakes gate — fires before every rule below.** Dispatch is not routine. Fires on: user-visible behaviour change, hard-to-reverse action, or a fix resting on an unproven premise. Ordinary multi-file work whose own tests cover the change does **not** qualify: a routine verifier spawn over work the tests already cover compounds effort without adding independent signal. When the gate doesn't fire, run the external proof (tests, logs, diff) once and close. A re-scan gated on a named, checkable trigger (e.g. a scope-boundary check against a stated rule) is targeted verification, not the routine recheck this gate excludes.

When the gate fires:

1. Invoke `foundry:challenger` with diff and original symptom description
2. Challenger confirms: (a) root cause structurally consistent with diff, (b) all original symptoms resolved, (c) no new failure modes introduced
3. Residual or new symptoms found → root cause incomplete → return to diagnosis loop

**Batched challenger dispatch**: when multiple fixes committed together (same logical release, same session), batch into **one** challenger call covering all groups — not one challenger per fix. Group fixes by logical concern (e.g. "research plugin", "hook fix", "rule change"), review together. Single-pass avoids spawning N redundant agents for overlapping context; catches cross-group regressions a per-fix reviewer can't see.

**Delegation + file-handoff**: always delegate batch to `foundry:challenger` via `Agent()` — not inline. Challenger writes full findings to a `.temp/` file, returns only a compact JSON envelope to the orchestrator. Orchestrator reads envelope verdict; reads file only on FAIL or low confidence. Never accumulate full challenger output in main context.

**Threshold**: the stakes gate above, not file count. A 12-file refactor with green tests and no user-visible change does not dispatch; a one-line change to a payment path or a migration does.

### Anti-patterns

- **Symptom suppression**: `try/except`, default value, or conditional guard hiding failure signal without removing cause — forbidden
- **First-plausible-cause stop**: accepting first root cause that sounds reasonable without confirming with evidence
- **Partial validation**: checking only primary symptom after fix, not all reported symptoms
- **Fix-before-confirm**: writing fix before confirming root cause — risk of fixing wrong thing
- **Skipping challenger on an "obvious" fix that clears the stakes gate** — obviousness isn't an exemption once stakes are in play; obvious fixes have the highest rate of incomplete root-cause identification. (Below the gate, not dispatching is the rule, not a skip.)
- **Ungrounded premise as design pillar**: using any assumption, constraint claim, recalled fact, or hypothesis as foundation for design or fix without first reading an authoritative source that proves it. Covers: technical constraints ("X can't do Y"), behavioral assumptions ("this function returns Z"), facts from memory or training ("I know this library does…"). Memory and training knowledge are never evidence. Drill move: challenge the premise's *justification* before challenging the design — "Where is this documented?" forces evidence lookup at the earliest point. Weak sources (blog posts, tweets, forum posts) need ≥2 independent corroborating sources or experimental validation before the premise is treated as fact. Layers built on a false premise make the entire design infeasible; the only catch point is before design begins.
