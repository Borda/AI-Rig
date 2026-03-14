For any finding classified as `CRITICAL` or `[blocking]`, spawn a second independent agent to verify before surfacing it in the report. Only apply cross-validation to `CRITICAL`/`[blocking]` findings — high and lower go directly to the report.

Use the same agent type that raised the finding (see the skill-specific note for the exact verifier):

```
Independently review <file or scope> for the following specific issue: "<finding description>".
Do NOT read any prior output from another agent reviewing this file.
Confirm: is this a real critical/blocking issue, a false positive, or something lower severity?
Explain your reasoning. End your response with a `## Confidence` block per CLAUDE.md output standards.
```

Classify the outcome:

- **Both agree it is critical/blocking** → include as critical/blocking in the report ✓
- **Second pass disagrees or downgrades** → downgrade to `high` with a note: "unconfirmed — one of two independent passes flagged this"
- **Both agree it is NOT critical** → remove from critical list; re-classify at the lower severity both agree on

This cross-validation adds one extra spawn per critical finding — it is worth it to avoid false-positive blocking issues reaching the user.
