# Result envelope

> Public envelope contract: `status`, `verdict`, `findings`, `files_touched`, `remaining`, and `blockers` carry decisions, blockers, and remaining work. Transcript-only `details` hold additional evidence and never hide required work or substitute for a public field.

> The bridge has two validation boundaries. The peer/model-to-harness result contains model-authored core defined by `schemas/envelope.schema.json` plus bounded verbose `details`. Local harness validates that core, persists `details` in bounded transcript, strips them from harness-to-caller public envelope, adds observed metadata and workspace-relative transcript and incident-path references, then validates public result with `schemas/harness-envelope.schema.json`. Open the JSON file at an `incident` path before inspecting its `fault`; public envelope does not inline that record.

## Model-authored core

> Every model response contains exactly `status`, `verdict`, `findings`, `files_touched`, `remaining`, and `blockers`. `status` is only `complete`, `partial`, or `blocked`; every list contains strings; no telemetry, identity, path, or lifecycle field is model-authored.

## Harness metadata

> The harness owns final timeout and refusal outcomes, model and effective effort, an optional effort substitution record, cost and token counters, `duration_seconds`, `depth`, `run_id`, incident and session references, transcript path, verb, and direction. Public envelope status may additionally be `timeout` or `refused`. Caller receives decision-critical fields and metadata; verbose detail remains opt-in through the referenced artifact.

> A timeout or a recursion refusal has no model core to merge. Harness constructs the required verdict and empty lists itself, places the concrete cause in `blockers`, and validates the full result before returning it.

> Only a Codex child stopped by `output-limit` may have optional harness-owned `partial_output`: an unverified fragment of the last complete `item.completed` agent message, limited to 4 KiB of UTF-8 without splitting a character. It is also stored in the incident; it is absent when no message was seen and for Claude children. Status remains `blocked`, incident fault remains `output-limit`, and findings remain empty. Label the fragment unverified and use it only to scope a split follow-up, never as findings or a verdict. Older results without this optional field remain valid; neither envelope schema has an integer version.

## MCP inputs

> MCP surface has three input shapes (`bridge_implement`, `bridge_advise`, and `bridge_review`); all three names are advertised by `tools/list`.

> Each bridge MCP tool requires `task` and accepts optional `model`, `effort`, `depth`, `run_id`, `timeout_seconds`, and caller-provided `supported_efforts` capability data. Server binds every request to its host-provided launch workspace; model-supplied workspace, background, and session fields are rejected. `schemas/mcp-tools.schema.json` defines all four input shapes.
