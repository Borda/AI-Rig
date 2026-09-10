---
description: 'Record format for the Claude Code Rig auto-allow audit log: the twelve mandatory fields, the extension fields, what the integrity model does and does not provide, and every deliberate deviation from the Internet-Draft it follows.'
---

# Auto-allow audit records

Every plugin in the Claude Code Rig registers one Bash `PreToolUse` hook, `allow-dispatch.js`, which decides whether a command runs without a permission prompt, and one completion hook, `audit-close.js`, which records what happened afterwards. Both append JSON lines to `~/.claude/logs/audit/`.

This page is the record format. It is written so a reader who has never seen the rig can interpret a line of that log, and so a second implementation could produce records the shipped verifier accepts.

## Where records live

One file per session, `s-<key>.jsonl`, where `<key>` is the first 32 hexadecimal characters of the SHA-256 of the session id. Records whose session id is missing or unusable go to a shared `_no-session.jsonl` instead; every rule phrased as "one file is one session" excludes that file.

The key is a hash rather than a sanitised identifier on purpose. A `[^a-zA-Z0-9_-] → _` transform maps `a/b` and `a?b` onto the same name, which would merge two unrelated sessions into one file. Hashing also removes path traversal, Windows reserved names, Unicode normalisation and case-insensitive aliasing in a single step. The unsanitised identifier is still written into every record.

The directory is created `0700` and files `0600`. Where POSIX ownership and mode bits do not exist, the check is skipped and the log proceeds — the directory is already user-scoped there.

## The twelve mandatory fields

The record follows the twelve mandatory fields of ["Agent Audit Trail: A Standard Logging Format for Autonomous AI Systems"](https://datatracker.ietf.org/doc/draft-sharif-agent-audit-trail/), revision 03. **It is an active individual-submission Internet-Draft. It is not endorsed by the IETF and has no standing in the standards process**, and it moved from revision 02 to 03 while this format was being written — check the current revision before relying on this mapping.

| #   | Field              | Our value                                                                  |
| --- | ------------------ | -------------------------------------------------------------------------- |
| 1   | `record_id`        | A UUID generated per record.                                               |
| 2   | `timestamp`        | RFC 3339 UTC with milliseconds.                                            |
| 3   | `agent_id`         | `<plugin>/<hook>`, e.g. `cc_foundry/allow-dispatch`.                       |
| 4   | `agent_version`    | The plugin's semantic version, or null when its manifest is unreadable.    |
| 5   | `session_id`       | The host's session id verbatim, or null.                                   |
| 6   | `action_type`      | `tool.bash`, `session.start` or `session.end`.                             |
| 7   | `action_detail`    | Shape depends on `record_phase` — see below.                               |
| 8   | `outcome`          | `pending` on a decision row; `success` or `failure` on an observation row. |
| 9   | `trust_level`      | `plugin` when this row's own decision was an allow, `unknown` otherwise.   |
| 10  | `parent_record_id` | Always null. Lineage is derived on read.                                   |
| 11  | `prev_hash`        | Always null. This log is not chained.                                      |
| 12  | `record_phase`     | `pre_execution` for a decision, `post_execution` for an observation.       |

### Extension fields

| Field                    | Meaning                                                                                                                                                         |
| ------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `tool_use_id`            | The host's tool-call id verbatim, or null. Absent on lifecycle rows.                                                                                            |
| `project`                | The working directory verbatim, or null.                                                                                                                        |
| `record_hash`            | SHA-256 of the record's canonical form with `record_hash` itself removed.                                                                                       |
| `action_detail.decision` | The effective decision: `allow` or `passthrough`.                                                                                                               |
| `action_detail.lane`     | Where the evidence came from: `blueprint` (provenance), `shape`, or `none`.                                                                                     |
| `action_detail.rank`     | `1` for the provenance lane, `2` for shape. Present only on an allow.                                                                                           |
| `action_detail.src`      | The matched manifest entry's source label. Present only on a provenance allow.                                                                                  |
| `action_detail.digest`   | SHA-256 of the normalized command. Present whenever the provenance lane reached a decision; **absent** when it did not, because nothing normalized the command. |
| `action_detail.verdicts` | Exactly two entries, one per lane, in rank order. Required on a decision row, forbidden elsewhere.                                                              |
| `action_detail.status`   | Observation rows only: `ok` or `error`.                                                                                                                         |
| `action_detail.event`    | Observation rows only: `PostToolUse` or `PostToolUseFailure`.                                                                                                   |
| `action_detail.reason`   | `session.end` rows only, and only when the host supplies one.                                                                                                   |

### The three-value verdict vocabulary

Each lane reports one of three things, and the difference between the last two is load-bearing:

| Verdict       | Meaning                                                                                                           |
| ------------- | ----------------------------------------------------------------------------------------------------------------- |
| `allow`       | The module produced an allow payload.                                                                             |
| `passthrough` | The module examined the command and declined. `why` names which check declined it.                                |
| `none`        | The module returned before deciding anything, or threw. This is the **absence of an opinion**, not an abstention. |

When both lanes report `none` no record is written at all: there is no opinion to record. Counting a module that never looked at a command as having abstained would overstate what the log establishes, which is why the two stay distinct everywhere downstream.

## Canonical form

`record_hash` is computed over a canonical serialization, which is normative because a JavaScript writer and a Python reader must agree byte-for-byte:

1. UTF-8, no whitespace, keys sorted, non-ASCII left unescaped. JavaScript `JSON.stringify` over a key-sorted object equals Python `json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`.
2. Every object key is a fixed ASCII identifier. JavaScript sorts UTF-16 code units and Python sorts code points; those orders differ above U+FFFF, and keeping every key far below that boundary is what makes them identical.
3. Every string value must be well-formed Unicode. A lone surrogate makes `JSON.stringify` emit an escape Python cannot UTF-8 encode, so an ill-formed free-form value is replaced with null. The null is the whole signal; no extra field records that it happened.
4. Every number is an integer within ±(2^53−1). No floats, so no float-formatting divergence is possible.
5. `null` is written explicitly; an absent optional key stays absent rather than becoming null.

## What the integrity model does and does not provide

`record_hash` **detects a record that changed after it was written** — a botched edit, a corrupt disk, a write that landed wrong but still parses. The shipped verifier exits non-zero for exactly that and nothing else. A torn line that no longer parses as JSON never reaches the hash check at all; it is classified as framing damage instead, which the next paragraph covers.

`record_hash` **is not tamper-evidence**. The same user owns the log file, the hooks that write it and the checker that reads it. Nothing here establishes that a record was not forged by whoever could also edit the file. Tamper-evidence would need a sink outside that user's control, and providing one is a deliberate non-goal. The log is not chained either, for the same reason: an unanchored local chain over a user-writable file adds cost without adding the property it appears to promise.

A line that does not parse is **torn framing**, not corruption. Several processes append one file with nothing coordinating them, and a torn line is the expected consequence. It is reported as a warning and never fails a run. It does remove its record from every count derived from that record, so a torn decision or completion row can raise `incomplete-evidence` or `incomplete-closure` beside it: read both as provisional whenever `truncated` is non-zero.

## Privacy, stated with its limit

Raw command text never enters a record. A decision row carries a digest of the normalized command and, for a provenance allow, the manifest source label it matched.

The honest limit: `task-log.js` already writes the first 200 characters of every Bash command into `timings.jsonl` under the same `tool_use_id`. Anyone holding `~/.claude/logs/` can recover command text regardless. The guarantee is about this file, not about the log directory.

## Reading the classifications

The verifier groups records by `(session_id, tool_use_id)` and reports what it observed. Almost every classification describes an observation rather than a fact:

- **`observed-abstention`** means no allow was seen. It does not mean every installed plugin abstained.
- **`incomplete-evidence`** means one group has fewer decision writers than another group in the same file. It cannot detect a plugin that never wrote at all, and a plugin may legitimately write nothing when neither of its modules reached a decision.
- **`completion-unobserved`** and **`in-flight-or-hard-kill`** mean a decision row has no completion row, split by whether the session later ended. Neither distinguishes a refusal at the prompt from a deny rule elsewhere, a tool that never returned, or a closer that died.
- **`multi-allow`** means several plugins allowed. That is normal, not an anomaly. With all four plugins installed it is also unavoidable for shape-matched commands: every plugin ships the same shape module, so every sentinel-read or date-stamp idiom produces four allows. Expect a non-zero `multi-allow` in any real log.
- **`incomplete-closure`** is the mirror of `incomplete-evidence`, on the completion side: a group reported by fewer plugins than close other groups in the same file. It exists because corroboration is only worth claiming if a missing reporter is visible, and it is a heuristic in the same way — it never proves a plugin failed, and it can only see a plugin that closes some groups and not others. What it catches is a per-call miss: a `PostToolUse` timeout on one call, or a completion row torn by a concurrent append. What it cannot catch is a plugin that never closed at all — one dark for the whole session, through `RIG_AUDIT=0` in its environment, a missing node on its cache path, or a broken build, writes no completion anywhere, never enters the comparison, and produces no finding. A clean report does not rule that out.
- **One plugin absent from a single call can raise both completeness buckets.** Where that group kept rows in both phases and the missing plugin is observed in both phases elsewhere in the file, it is short a decision writer and short a closer, so `incomplete-evidence` and `incomplete-closure` each report it. Two findings, one cause — they are derived from disjoint row sets, so this is not double-counting. Only one fires where the other side has nothing to compare: a group with completions but no decision rows raises `incomplete-closure` alone.
- **Several plugins reporting one completion is corroboration.** Only a repeat from the *same* writer is odd, and even that is a warning.
- **`unjoinable`** means a null join key. Those rows are counted and reported, never dropped.

Authority is usually unknowable. That a tool executed proves it was not blocked — not that a human approved it, since a sibling plugin's allow, a `settings.json` rule or a permission mode all execute with no prompt. The record never writes `human`, and nothing downstream may infer it.

## Deliberate deviations from the draft

| Field                            | Draft says                                             | We write                          | Why                                                                                                                                         |
| -------------------------------- | ------------------------------------------------------ | --------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| `outcome`                        | `success`, `failure`, `timeout`, `denied`, `escalated` | adds `pending`                    | A decision that has not executed yet has no outcome, and the draft has no value for that state.                                             |
| `trust_level`                    | `L0`–`L4` authentication levels                        | `plugin`, `unknown`               | What is observed is which component *proposed* an allow, not an authentication level. Mapping onto L0–L4 would assert something unknowable. |
| `agent_id`                       | a URI                                                  | `<plugin>/<hook>`                 | A URI scheme here would be invented rather than resolvable, and no consumer needs one.                                                      |
| `session_id`                     | UUID v4                                                | the host's id verbatim, or null   | The format is not ours to control, and inventing one would break the join with the host's other logs.                                       |
| `parent_record_id`               | id of the preceding record                             | always null                       | The log is not chained; lineage is derived on read instead.                                                                                 |
| `prev_hash`                      | SHA-256 of the previous record                         | always null                       | Same.                                                                                                                                       |
| `record_phase`                   | `pre_execution`, `post_execution`, `concurrent`        | `pre_execution`, `post_execution` | Conformant; `concurrent` has no use here.                                                                                                   |
| `timeout`, `denied`, `escalated` | outcome values                                         | never written                     | No host signal supports them. They stay reserved rather than being guessed at.                                                              |

## Retention and the kill switch

`verify_blueprint_audit.py prune` is the only component that deletes a log file. No hook ever deletes anything, so pruning races nothing: it is explicitly invoked, single-process, and interacts with no writer the operator did not start. It never prunes `_no-session.jsonl` by age — a growing one means the host stopped sending a session id, which is a regression to investigate rather than a file to rotate.

`prune` deletes only files named `s-<key>.jsonl`, the per-session form this writer creates, whatever path it is pointed at. That matters because `~/.claude/logs` — one path component above the default target — holds `timings.jsonl` and `invocations.jsonl`, two unrelated append-only logs. Selecting by filename shape rather than by extension is what keeps a mistyped path from destroying them, and naming any other single file deletes nothing. `--older-than` must be at least one day; a smaller value is rejected rather than read as "everything".

Symlinks are treated differently by the two paths, deliberately. `verify` follows a link you name yourself, because reading through it is harmless and refusing would answer "clean" about a file nobody looked at. A directory scan does not follow links: it reads paths you never named one by one, and a link to a sibling in the same directory would be verified twice. Skipped entries are named on stderr. `prune` refuses every link on both paths, because following one would unlink a name this writer never created.

Nothing schedules `prune`. Run it from your own scheduler if you want retention to be automatic.

`RIG_AUDIT=0` disables audit writing everywhere. It disables logging only: both decision modules are still evaluated and the same permission decision is still emitted.

## Known gaps

- **The dispatcher shares one timeout across both lanes.** A stall anywhere can push the hook past its timeout, and the host then discards its entire output — including an allow already written to stdout. Two separately registered hooks would have lost only one of the two. The frequency is unmeasured.
- **A record can be lost between the decision and the append** if the process dies in that window. Lost records surface as missing observations, never as a false allow.
- **Retention is manual** unless an operator schedules `prune`. An unpruned log grows without bound.
- **A clean exit does not mean a complete read.** `verify` exits 0 unless a record fails its own hash. A file stopped at `--max-lines` reports `limit-exceeded`, as does one whose read failed partway — a failing disk, a mount that went away — with the reason named on stderr and whatever the prefix established kept rather than discarded, corruption included. Its findings cover only the prefix that was read — treat them as provisional rather than merely partial, because a call whose completion row lies past the cut is counted as having no completion at all, inflating `completion-unobserved` and `in-flight-or-hard-kill` by the truncation itself.
- **A scan reports what it read, not whether what it read was one of our logs.** There is no "this is not an audit log" verdict, and a file yielding zero conforming records is not distinguished from one yielding many. What a foreign file reports depends on what its lines happen to be: plain text is torn framing (`truncated: N`, exit 0); a JSON array or any non-object line is `schema-invalid` (exit 0); and a **JSON object line is `corrupt-record`, which exits 1** — it parses, so it reaches the hash check, and it carries no matching `record_hash`. A JSON file from somewhere else therefore produces this tool's loudest alarm about a file that was never a log at all.
- **`--json` carries no notice of a file that could not be opened.** The text report states `files SKIPPED: N` beside `files read: N`, and stderr names each one with its reason. The JSON payload has three keys — `files`, `totals`, `exit` — pinned as a contract, and a file that was never opened appears in none of them. A machine consumer reading the payload alone sees a scan of the files that worked, with nothing saying others were missed.
- **`capped` is a different loss, on the writer's side.** It means `_no-session.jsonl` had already reached its size cutoff, so the writer refused to append. That file is read in full; the missing records never reached disk. The text report states the two separately, and a caller reading the exit code alone sees neither.
- **Derived lineage is arbitrary among equal ranks.** The `parent` a group reports is chosen by rank, and ties are broken on a random record id. Four plugins allowing on the same shape match all carry the same rank, so the reported parent is one of them rather than the one — and it need not be the same one when the events are replayed.
- **The `parent` map key joins two host-supplied ids with `|`.** A session or tool-call id containing that character makes the key ambiguous, and totals merged across files would overwrite rather than add. No host produces such an id today; the log records what the host sent, so this is stated rather than assumed away.
- **Session boundaries are positional, not timestamped.** The split between `completion-unobserved` and `in-flight-or-hard-kill` compares a row's position against the last `session.end` in the file. A session resumed under the same id appends to the same file, so a call killed in the earlier run is relabelled once the later run ends cleanly. Both are warnings and neither claims a cause.
- **`_no-session.jsonl` is a shared cross-session stream** with a soft cutoff and no compaction. Concurrent writers can overshoot the cutoff by one record each; the file is never truncated or rewritten.
