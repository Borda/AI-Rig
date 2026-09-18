`CRITICAL`/`[blocking]` findings: spawn second independent agent to verify before report. High and lower go direct.

Use same agent type that raised finding:

```text
Independently review <file or scope> for the following specific issue: "<finding description>".
Do NOT read any prior output from another agent reviewing this file.
Confirm: is this a real critical/blocking issue, a false positive, or something lower severity?
Explain your reasoning. End your response with a `## Confidence` block per CLAUDE.md output standards.
```

Classify outcome:

- **Both agree critical/blocking** → include as critical/blocking in report ✓
- **Second pass disagrees or downgrades** → downgrade to `high` with note: "unconfirmed — one of two independent passes flagged this"
- **Both agree NOT critical** → remove from critical list; re-classify at lower severity both agree on

**Spawn cap: max 3 verifier agents per run.** More than 3 critical/blocking findings → group into batches of ≤2 findings per verifier (same origin agent type per batch); note grouped finding IDs in verifier's rationale; every finding still gets its own independent verdict. Unbounded one-spawn-per-finding fanout costs ~120,851 tok fixed overhead per critical. Worth capped spend — stops false-positive blockers reaching user.
