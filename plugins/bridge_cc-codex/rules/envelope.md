# Result envelope

> Public envelope contract: `status`, `verdict`, `findings`, `files_touched`, `remaining`, and `blockers` carry decisions, blockers, and remaining work. Transcript-only `details` hold additional evidence and never hide required work or substitute for a public field.

> The bridge has two validation boundaries. The peer/model-to-harness result contains model-authored core defined by `schemas/envelope.schema.json` plus bounded verbose `details`. Local harness validates that core, persists `details` in bounded transcript, strips them from harness-to-caller public envelope, adds observed metadata and workspace-relative transcript and incident-path references, then validates public result with `schemas/harness-envelope.schema.json`. Open the JSON file at an `incident` path before inspecting its `fault`; public envelope does not inline that record.

## Model-authored core

> Every model response contains exactly `status`, `verdict`, `findings`, `files_touched`, `remaining`, and `blockers`. `status` is only `complete`, `partial`, or `blocked`; every list contains strings; no telemetry, identity, path, or lifecycle field is model-authored.

## Harness metadata

> The harness owns final timeout and refusal outcomes, model and effective effort, an optional effort substitution record, cost and token counters, `duration_seconds`, `depth`, `run_id`, incident and session references, transcript path, verb, and direction. Public envelope status may additionally be `timeout` or `refused`. Caller receives decision-critical fields and metadata; verbose detail remains opt-in through the referenced artifact.

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
- No public envelope field was added.

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
