<!-- file: compaction-contract.md — consumers: foundry:{audit,brainstorm,calibrate,distill,investigate}, oss:{analyse,resolve,review}, develop:{debug,feature,fix,refactor,review}, research:{fortify,judge,kaggle,run,sweep,topic,verify} -->

# Compaction Contract Protocol

## What

`.temp/state/skill-contract.md` — terse block skill (re)writes at expanding-phase boundaries through `write_skill_contract.py`, which stamps `.temp/state/skill-contract.owner` with the writer session and a SHA-256 digest of the raw contract bytes when `CLAUDE_CODE_SESSION_ID` is nonempty. PreCompact (`task-log.js`) saves the contract **verbatim** under `## Skill Compaction Contract`; SessionStart compact (`rule-inject.js`) restores it as verified only when both bytes and session match. Missing, malformed or mismatched stamps produce an unverified advisory block requiring user confirmation; matching bytes stamped by another session are skipped. The owner stamp binds bytes produced by the session writer; it does not authenticate the contract against a writer with workspace access. No age bound applies; confirm the skill is still active even for a verified block.

## Block format (emit this template exactly)

```markdown
## Active Skill Contract
- skill: <plugin:skill> · phase: <name> (after <expanding phase>)
- run-dir: <path or n/a>
- preserve: <task IDs, key decisions, report/artifact paths, file list next phase needs>
- next: <what next phase does with the above>
```

## Placement rule

Refresh at boundary **after** expanding phase (parallel fan-out / iteration loop / large gather), **before** next phase begins.

- Only at phase boundaries — not every step
- `preserve:` = only what next phase consumes; drop raw expanded material
- `--keep "<items>"` at invocation → append user string to `preserve:` at Step 0

## Boundary step

Main thread only: a subagent's session ID resolves to the parent's transcript. Never run the gate inside a subagent; a phase run entirely inside one subagent returning an envelope skips it. This step never blocks auto-compaction.

1. Persist the completed phase's report, evidence and task IDs before refreshing its contract. `preserve:` names the report and next-phase inputs; append literal `-- calls_ahead=N "drop=persisted material"` metadata to the boundary's `write_skill_contract.py` call. Use that boundary's fixed estimate and drop text; `interactive=false` metadata disables the gate for unattended boundaries.

2. The boundary's Bash block runs this gate verbatim after printing this section (requires foundry plugin; absence continues). Use that decision; do not run it again:

```bash
_RS="${CLAUDE_PLUGIN_ROOT:+$CLAUDE_PLUGIN_ROOT/bin/resolve_shared_path.py}"
[ -n "$_RS" ] || _RS=$(ls plugins/cc_*/bin/resolve_shared_path.py 2>/dev/null | head -1)
[ -f "$_RS" ] && _GD=$(python "$_RS" foundry bin 2>/dev/null) && [ -f "$_GD/compact_gate.py" ] && python "$_GD/compact_gate.py" || echo "compact-gate: continue (foundry gate unavailable)"
```

3. Act on the decision. The gate never holds the run unless the user opted in:
   - `compact-gate: continue ...` → proceed silently.
   - `compact-gate: advise ...` → start the next phase in this same response. Carry the hint below to the next point where the turn ends anyway: after a spawn, or appended to the next `AskUserQuestion`'s question text. Never end a turn only for the hint; drop it when the skill completes first.
   - `compact-gate: stop ...` (user set `FOUNDRY_COMPACT_GATE=stop`) → emit the hint as the reply's final text, no tool call after it (Bash stdout is not reliably shown), then end the turn before starting the next phase.

Hint: fill `<phase>` and `<N>` (the boundary's `calls_ahead`); put the `/compact` line alone in its own fenced code block, verbatim. It stays one short fixed line: a long line wraps on terminal copy, and Claude Code turns a multi-line paste into a pasted-text block that never runs as a command. The hooks already carry the full contract, so `keep:` never repeats its fields.

```text
Boundary "<phase>" done. Next ~<N> calls; compacting now cuts their context cost.
/compact keep: skill contract .temp/state/skill-contract.md
```

Explain once per skill run, with the first hint: send the `/compact` line as typed input, then send `continue` unless an agent notification resumes the run first; a user-typed `/compact` gives the model no turn of its own, and text after `/compact` reaches the summarizer as its instructions. For a verified restored contract, the user's next message after any user-typed `/compact` (`continue`, or an answer to the pending question) confirms the skill is still active; after auto-compaction, check on disk that the run-dir and preserved report exist, and ask the user only if that check fails. An unverified restored contract remains advisory and needs user confirmation before resuming. On resume, re-read the preserved report, re-load protocol files named in `next:` and resume `next:` without repeating the completed phase or gate.

## Acceptance criteria

- `preserve:` names concrete inputs (paths, IDs, report filenames) — not vague prose
- Block ≤ ~12 lines
- `calls_ahead` literals are per-boundary estimates; calibrate them from realised calls in foundry's `cost_analyzer.py --composition` (logged-compaction "estimate vs realised" lines)

## Lifecycle

| Step | Action |
| -- | -- |
| Boundary reached | `write_skill_contract.py` → `.temp/state/skill-contract.md` and optional owner stamp |
| Compaction fires (auto or typed `/compact`) | PreCompact saves verified or unverified block verbatim to `session-context.md`; skips a matching foreign-session stamp |
| Post-compaction | SessionStart compact injects contract: only the verified block is authoritative after the Boundary step's confirmation rule; everything else, including `session-context.md`, is advisory and needs user confirmation before resuming |
| Skill completes | Delete `.temp/state/skill-contract.md` — prevents stale leak; a leftover owner file is harmless because the SHA rule makes it inert without matching contract bytes, and the next write unlinks it |
