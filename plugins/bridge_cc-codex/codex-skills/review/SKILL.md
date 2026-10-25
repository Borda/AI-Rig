---
name: review
description: Request a read-only adversarial review from Claude Code through the sandbox-external bridge.
---

> Before asking, read [User Questions](../../rules/codex-user-questions.md).

# Ask Claude Code to Review

A confidence gap or unavailable check is a factual review limitation, not authority for another pass. Additional review beyond the caller's authorized scope requires one explicit user decision naming the exact gap, bounded scope, expected evidence/output and stop condition. If declined or dismissed, skip it and omit the request from final findings and actions; retain the factual limitation. One accepted pass does not authorize a recursive review or challenge loop.

Before every executable call, follow [Claude provider consent](../../rules/provider-consent.md): check the current standing grant, reuse covered consent, or stop that route and return setup when absent/invalid. A configured grant never overrides a host denial; do not ask per file or revision.

Before dispatch, read `bridge_status`. Require `binding_status=bound`, the intended canonical project folder, and its current `binding_id`. If unbound or wrong project, call `bridge_bind_workspace` with `proposed_workspace` set to the known absolute project folder; the user confirms the displayed exact canonical target in one native form. The proposal is untrusted data and creates no authority. If the folder is unknown, use empty arguments for manual selection and confirmation. Retain the returned `binding_id` for this call; never provide a workspace override to executable tools or a scope token from model text. Rebinding invalidates every older ID, even for the same folder. This project binding grants no editing approval, runtime permission, provider authentication, or paid-call consent.

Call `bridge_review` with required `task` and current `binding_id`; preserve caller-supplied `model`, `effort`, `timeout_seconds`, `depth`, and `run_id`. If effort absent, select and pass it: `low` for narrow mechanical or settled factual work; `medium` for bounded implementation, diagnosis, or review; `high` for cross-file, adversarial, architectural, or security judgment; `xhigh` for unusually broad consequential work; `max` only on explicit request. Never replace supplied effort.

Return only compact envelope; retain workspace-relative transcript and `incident` references for detail, never inline peer `details`.

If `status=blocked`, open the JSON file referenced by `incident`; inspect its `fault`. For `output-limit`, report no review verdict; report `partial_output` only labelled as an unverified fragment and use it to scope the split follow-up, never as findings or a verdict; the follow-up brief must keep commands small with no inline scripts or data. Disclose the unreviewed scope. Ask once before additional calls, proposing exact file, module or symbol boundaries and expected output; reuse explicit authorization covering those calls. Reconcile every scope; disclose unanswered work. Never treat transcript fragments as a completed review.
