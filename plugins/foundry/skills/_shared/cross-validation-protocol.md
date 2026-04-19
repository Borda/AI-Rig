**Re: Compress natural language in this markdown to caveman format**

For any finding classified as `CRITICAL` or `[blocking]`, spawn second independent agent to verify before surfacing in report. Cross-validation only for `CRITICAL`/`[blocking]` — high and lower go direct to report.

Use same agent type that raised finding (see skill-specific note for exact verifier):

```
Independently review <file or scope> for the following specific issue: "<finding description>".
Do NOT read any prior output from another agent reviewing this file.
Confirm: is this a real critical/blocking issue, a false positive, or something lower severity?
Explain your reasoning. End your response with a `## Confidence` block per CLAUDE.md output standards.
```

Classify outcome:

- **Both agree it is critical/blocking** → include as critical/blocking in report ✓
- **Second pass disagrees or downgrades** → downgrade to `high` with note: "unconfirmed — one of two independent passes flagged this"
- **Both agree it is NOT critical** → remove from critical list; re-classify at lower severity both agree on

Cross-validation adds one extra spawn per critical finding — worth it to avoid false-positive blocking issues reaching user.
