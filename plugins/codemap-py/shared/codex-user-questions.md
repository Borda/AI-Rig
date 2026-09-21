# Codex User Questions

Read once before asking; reuse while unchanged.

1. The root owns user questions. Children return context, question, concrete choices, accepted custom syntax, and answer mapping to the root; never open controls or print a user-facing fallback.
2. Reuse an existing unambiguous answer. Otherwise freeze the scope, map exact labels to canonical values, and keep one pending decision per workflow. Bind it to verified host question identity; otherwise include a decision key in the question and answers.
3. Inspect current tool restrictions. Required decisions use permitted `request_user_input`, otherwise permitted `request_user_input_async`; optional questions prefer async, then permitted sync. Use the actual tool schema. Invoke the control; printing its payload is not asking.
4. Show necessary context once before the control, without duplicating its question or choices. Offer distinct, nonempty, actionable presets and built-in free text with the complete accepted grammar. Preserve every required closed-choice action. Never add an `Other`/`Custom` button or force two presets. Put an evidence-backed `(Recommended)` choice first; when none is honest, ask free text.
5. Explain approval and denial effects; use `Approve`/`Deny` or exact action labels. A preapproval brief is context, not a question: missing workflow consent uses the native control; existing consent skips reconfirmation. Runtime permission requests use the dedicated runtime mechanism, not question tools.
6. Record the actual valid answer against the frozen scope before continuing. Tool acceptance, silence, preselection, examples, or unrelated text are not consent. Required work stays pending; only independent authorized work continues. Optional defaults require workflow/host permission, a response opportunity, and a stated assumption. Never replay a completed decision.
7. Plain text is allowed only when the host requires it or neither native control is suitable; explain the actual restriction. No tool/version probing, settings changes, or retrying a known root-only child call to obtain a widget.

Before exact-token/digest confirmation, uncertain answer binding, changed scope, delayed answers, resumption, control failure, or plain-text/headless fallback, read [detailed rules](codex-user-questions-details.md). Preserve pending state while resolving uncertainty. These details retain approval, recovery, and audit requirements; ordinary questions need only this guide.
