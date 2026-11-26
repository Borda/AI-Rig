# Result envelope

> Public envelope contract: `status`, `verdict`, `findings`, `files_touched`, `remaining`, and `blockers` carry decisions, blockers, and remaining work; `confidence`, `confidence_gaps`, and `confidence_null_reason` say how far the result can be trusted or why no score exists (§Confidence). Transcript-only `details` hold additional evidence and never hide required work or substitute for a public field.

> The bridge has two validation boundaries. The peer/model-to-harness result contains model-authored core defined by `schemas/envelope.schema.json` plus bounded verbose `details`. Local harness validates that core, persists `details` in bounded transcript, strips them from harness-to-caller public envelope, adds observed metadata and workspace-relative transcript and incident-path references, then validates public result with `schemas/harness-envelope.schema.json`. Open the JSON file at an `incident` path before inspecting its `fault`; public envelope does not inline that record.

## Model-authored core

> Every model response contains exactly `status`, `verdict`, `findings`, `files_touched`, `remaining`, `blockers`, and `confidence_gaps`. `status` is only `complete`, `partial`, or `blocked`; every list contains strings; no telemetry, identity, path, lifecycle, or confidence-number field is model-authored. A response that omits only `confidence_gaps` still delivers its answer and is `unscored`.

## Harness metadata

> The harness owns final timeout and refusal outcomes, model and effective effort, an optional effort substitution record, cost and token counters, `duration_seconds`, `depth`, `run_id`, incident and session references, transcript path, verb, direction, the derived `confidence`, and `confidence_null_reason`. Public envelope status may additionally be `timeout` or `refused`. Caller receives decision-critical fields and metadata; verbose detail remains opt-in through the referenced artifact.

> A timeout or a recursion refusal has no model core to merge. Harness constructs the required verdict and empty lists itself, places the concrete cause in `blockers`, and validates the full result before returning it.

> Only a Codex child stopped by `output-limit` may have optional harness-owned `partial_output`: an unverified fragment of the last complete `item.completed` agent message, limited to 4 KiB of UTF-8 without splitting a character.
>
> - It is also stored in the incident; it is absent when no message was seen and for Claude children.
> - Status remains `blocked`, incident fault remains `output-limit`, and findings remain empty.
> - Label the fragment unverified and use it only to scope a split follow-up, never as findings or a verdict.
> - Older results without this optional field remain valid; neither envelope schema has an integer version.

## Provider stops

> The harness names a provider stop; the peer never does.

- **Classes** (incident `fault`): `quota-exhausted` (credit, usage-limit, spend-limit or subscription stop), `rate-limited`, and `capacity` (model at capacity).
- **Attempts**: one child attempt; no timeout retry and no lower-effort retry.
- **Result**: status `blocked`, with a harness-authored verdict and first `blockers` line:
  - `<peer> quota exhausted: ...`
  - `<peer> rate limited[ (retry after Ns)]: ...`
  - `<peer> model at capacity: ...`
- **With a hard cutoff**: status stays `timeout`; `blockers` lists `hard cutoff reached`, then the classified line.
- **Not classified**: contradictory evidence (a 429 or 402 beside capacity wording, a 402 beside rate wording), a content-policy stop, and every unrecognized failure stay a plain `blocked` with the provider's own text.
- **Terminal failure**: only a Codex `turn.failed` with no later `turn.completed`, even when it carries no message text. A top-level reconnect or retry notice is context in `blockers`, never the verdict. A terminal failure outranks an earlier model answer.
- **Wording source**: candidate wording from the Codex and Claude Code binaries, not captured wire records. Extend the table when a real stop is captured.

## Provenance contract

- An envelope is harness-classified only when `incident` is non-null and the incident file's `fault` equals the class.
- The verdict and `blockers` text is prose. A peer model can write a `blocked` result with the same wording, but its envelope links no incident whose `fault` is a provider class: `incident` is `null`, or after a retry it names a `timeout` or `unsupported-effort` incident.
- Consumers decide on the incident `fault`, never on verdict text.
- Provider classification added no public field of its own. `confidence_null_reason` is harness-authored, a peer cannot set it, and for these classes it equals the incident `fault`, so a consumer may read either.

## Confidence

> A result's `confidence` is derived by the harness from the peer's named gaps, never authored by the peer and never a constant. When no number can be derived, `confidence` is `null` and `confidence_null_reason` names the cause from a closed set.

- **Gap lines**: each `confidence_gaps` entry reads exactly `(-0.NN) <limit>`, the deduction from 1.00 with two decimals (`(-1.00)` at most), a space, then the specific limit: a file not read, a check not run, a claim not verified. `(-0.00)` names a limit that did not lower the score.
- **Derivation**: `confidence` = 1.00 minus the sum of the deductions, exact to two decimals, only for `status` `complete`. An empty list cannot be scored: it is what a peer that skipped the accounting returns, so a peer that finds nothing limiting writes one `(-0.00) <reason>` line.
- **Exclusivity**: exactly one of `confidence` and `confidence_null_reason` is non-null. The harness rejects a stored or forwarded envelope whose number differs from its own gap lines.
- **Gaps stay public**: the peer's gap lines are kept for every status, as named limits a consumer reports; they are empty when no usable peer answer exists.
- **No ceilings**: the bridge applies no cap for what the peer could not do, such as a read-only sandbox. A consumer applies its own ceilings and names each one as a gap.

| Code | Status | Incident `fault` | Cause |
| -- | -- | -- | -- |
| `quota-exhausted` | `blocked` or `timeout` | `quota-exhausted` | Harness-classified provider stop: credit, usage, spend or subscription limit |
| `rate-limited` | `blocked` or `timeout` | `rate-limited` | Harness-classified provider rate limit |
| `capacity` | `blocked` or `timeout` | `capacity` | Harness-classified model-at-capacity stop |
| `timeout` | `timeout` | `timeout` | Hard cutoff reached without a classified provider stop |
| `output-limit` | `blocked` | `output-limit` | Child output exceeded the capture limit; any `partial_output` stays unverified |
| `refused` | `refused` | `refused` or `network-preflight` | Recursion depth refused, or a failed `github-read` preflight |
| `peer-unavailable` | `blocked` | `blocked` | The child could not be started, for example a missing peer executable |
| `peer-failed` | `blocked` | `blocked` | The child ran and failed for a cause the harness does not classify; read the incident `reason` |
| `empty-result` | `blocked` | `blocked` | The child finished without a usable model result: none, or one that fails the core contract |
| `incomplete` | `partial` or `blocked` | none of its own | The peer itself reported `partial` or `blocked`; on a split envelope, also every wave of a multi-wave partition |
| `cancelled` | `blocked` | `blocked`, or the completed child's own | The job owner cancelled the detached job |
| `invalid-request` | `blocked` | `blocked` | The request was rejected before any child ran: invalid depth or unsupported effort |
| `unscored` | `complete` | none of its own | The gap list is absent or empty, a line is not `(-0.NN) <limit>`, or the deductions exceed 1.00 |
| `not-recorded` | any | any | A stored envelope written before the bridge recorded confidence |

- **Incident column**: "none of its own" means the code records no incident; after the one timeout or effort retry, `incident` still links the earlier attempt's record.
- **Status pairing**: the harness rejects a single-call envelope whose code does not fit its status per the Status column, such as `complete` with `timeout` or `blocked` with `unscored`; `not-recorded` fits any status. A merged split envelope is not held to the column: its code is a scope's own cause lifted onto the merged status, so `partial` may pair with `quota-exhausted`, `timeout` or `unscored`; every wave of a multi-wave partition carries `incomplete` or `quota-exhausted` beside any merged status, `complete` and `timeout` included (see Partitioned reviews).
- **Consumers**: count `confidence` only on a `complete` result. A `null` confidence never enters a minimum, median or aggregate: name it once as a gap with its status and code, and never substitute a number.
- **Split reviews**: the merged envelope keeps each scope's three fields in its `splits` row and lists every gap line as a string, `(-0.NN) split N: <limit>`, so `confidence_gaps` is a list of strings on every route. Its `confidence` is the lowest scope score only when every scope has one. Keep the original deductions from the first scope with that score, and write every other scope's gaps with `(-0.00)`. The merged deductions then derive the merged score, and each row retains its original deductions. Otherwise it is `null`, and the code is `quota-exhausted` when any scope hit that stop, or else the first unscored scope's code.
- **Partitioned reviews**: one wave's rows cover only part of a partition, so when `waves` is above 1 every wave, the last included, carries `confidence: null` with the code `quota-exhausted` (a group of that wave hit that stop) or `incomplete`, whatever its rows scored; no wave's number stands for the partition. The caller derives the partition score after the last wave: the lowest `splits[].confidence` across every wave's rows, only when every group of every wave has a number. A group without one (a null `confidence`, named by its row's `status` and `confidence_null_reason`) a wave that did not run, or a file named in any wave's `skipped` list leaves the partition unscored: name each once as a gap, never take the minimum over the scored groups alone. Build the partition's gaps from `splits[].confidence_gaps` across all waves and label each line `(-0.NN) split N: <limit>` using the row's group number. For a scored partition, keep deductions only from the first group with the lowest score (smallest group number) and use `(-0.00)` for every other group. These deductions derive the partition score. Use the original row lines as input, since wave-level lines can reflect a wave's local minimum. A partition of one wave is an ordinary split.
- **Stored envelopes**: no integer version was added. A stored envelope that carries none of the three fields predates them and is read back as `null` with `not-recorded` and an empty gap list; one that carries only some of them is invalid.

## Incident records

- `schema_version` is 2; readers still accept 1.
- `workspace_delta` is `null` when no delta was computed (read-only verb, or Git unreadable and no edit reported); otherwise a list:
  - new `git status --porcelain` lines since spawn;
  - `file_change <path>` entries the Codex transcript reported (Codex only), workspace-relative when inside the workspace, bounded and free of control characters, which cover a file already modified before the call.
  - A file Git already lists as changed is not repeated from the transcript. A repository that does not ignore `.temp/` may list `?? .temp/`, created by the bridge's own artifacts.
  - An empty or null delta never proves that no edit landed.
- `returncode` is the child exit status when a child ran.

## MCP inputs

> MCP surface has three input shapes (`bridge_implement`, `bridge_advise`, and `bridge_review`); all three names are advertised by `tools/list`.

> Each bridge MCP tool requires `task` and accepts optional `model`, `effort`, `depth`, `run_id`, `timeout_seconds`, and caller-provided `supported_efforts` capability data. Server binds every request to its host-provided launch workspace; model-supplied workspace, background, and session fields are rejected. `schemas/mcp-tools.schema.json` defines all four input shapes.
