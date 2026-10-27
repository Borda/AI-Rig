# Evidence rules

Every factual, causal, and completion claim needs evidence that a reader can check.

## Tiers

- **Tier 1** — source read from disk, official documentation, release notes, a spec, or output from a command run in this session. Sufficient alone.
- **Tier 2** — blog posts, tutorials, forums, or model recall. Needs at least three genuinely independent sources or a confirming experiment.

Model recall is never evidence. "Obvious" and "well-known" are not evidence.

## Applying evidence

- Cite the path and the concrete observation, not the file name alone.
- Separate `verified` from `pattern-matched` in prose. Hedge or verify the latter explicitly.
- A passing unit test proves only its exercised contract, not an untested workflow or host.
- An unavailable environment is an explicit limit: write `unable to verify: <reason>`; never simulate a pass.
- Store exact bytes and digests for reviewed source. A digest change invalidates the evidence.

## Run evidence

- `rig_evidence` binds inputs, outputs, gates, and the scope digest.
- Finalization fails on source drift, a failed gate, or missing independent review.
- Report failed gates and skipped coverage in the final answer, even when everything else passed.

## Honesty boundaries

- Prompt instructions are not a sandbox. Say what is hook-enforced and what is instruction-enforced.
- A reviewer's report is evidence of review, not proof that no defect exists.
- An unreviewed area is a coverage gap, never a clean result.
