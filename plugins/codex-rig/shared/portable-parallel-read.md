# Portable Parallel Read

Shared invariant contract for the promoted portable read-only `parallel-read` route of the `implement` and `manage` skills. The loading skill's Parallel Adoption section supplies the consumer values: promoted consumer id and `consumer_policy`, safe parallel work, freeze list and join point, bootstrap exemptions, the literal `parallel_execution.py preflight` and `validate-runtime` commands, serial-parent ownership, resource-overlap wording, and the generic-write stop sentence. Those values and this contract apply together.

<!-- policy-sibling: skills/implement/SKILL.md, skills/manage/SKILL.md (Parallel Adoption sections) — invariant sentences live here, consumer values stay per skill; also see skills/code-review/SKILL.md Workflow parallel-review section as a third divergent variant; check siblings before editing this file alone -->

## Portable Read-Only Route

Execution Route Selection must resolve to `parallel-read` before this section applies; `auto` does not select it. This consumer's runtime matrix and promotion remain mandatory. Serial parent work uses existing task authorization; exact-plan-digest approval applies when the promoted parallel-read route is selected. This route never bypasses consumer promotion, serial parent authority, or applicable write approval. Supplied denials or stale approvals remain invalid in every execution mode.

### Required barrier

Apply shared [host compatibility check](specialist-orchestration.md#host-compatibility-before-dispatch) before preparing any child work. Include `read_host` only from verified launcher-supported child controls. Missing or incompatible controls stop the selected parallel-read route before dispatch. A plan declaration is not effective-control proof, and post-run validation remains mandatory.

The frozen plan is the `<run-directory>/execution-plan.json` that the loading skill declares with its `consumer_policy` values.

It must also include `write_policy`: use `parent_writes=planned` with `approval_requirement=exact-plan-digest` when any parent mutation is planned, otherwise `parent_writes=none` with `approval_requirement=not-required`. When parallel-read is selected, a planned parent write requires `<run-directory>/write-approval.json` containing only exact plan SHA-256, `response=approve`, and `source=explicit-input|user-prompt`. Serial execution or an automatic serial fallback does not require a new digest receipt for already-authorized work.

Omit `--approval` when no parent writes are planned or effective execution is serial and no approval was supplied. Resolve host fallback before deciding whether the parallel route needs a receipt. A nonzero result stops route.

Run the same preflight again after the terminal join and before the first parent mutation. Any plan, approval, consumer, runtime, or join drift stops mutation and requires a new frozen plan plus exact approval.

### Resource conflicts

Declare only validated resource locks such as `git-index`, `cache:<path>`, `generated:<path>`, and `test-env:<name>`.

### Fallback

Unavailable or unsafe fan-out uses equal-gate `serial-fallback` from the same frozen plan with the same quality gates and retained evidence. Never label fallback as parallel or weaken checks because dispatch was unavailable.

### Acceptance

This skill's shared runtime matrix and consumer promotion must remain complete before the selected `parallel-read` route starts. Acceptance must prove freeze, complete join, truthful execution label, resource compatibility, equal gates, and unchanged serial parent authority.

### Stop rule

Stop without dispatch on missing promotion, mutable packs, ownership or resource overlap, sensitive or unproven controls, missing terminal evidence, or incomplete join.
