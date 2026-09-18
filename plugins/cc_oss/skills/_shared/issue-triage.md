# Issue Triage — Decision Tree, Labels, Good First Issue Criteria

## Decision Tree

```text
Incoming issue
├── Is it a bug report?
│   ├── Has reproduction steps? → Label: bug, ask for environment info if missing
│   ├── No repro? → Label: needs-repro, ask for minimal example
│   └── Duplicate? → Close with link to canonical issue
├── Is it a feature request?
│   ├── Aligns with project scope? → Label: enhancement, discuss design
│   └── Out of scope? → Close with explanation, suggest workaround
├── Is it a question?
│   └── → Label: question, answer or redirect to docs/discussions
└── Is it a security issue?
    └── → Ask reporter to use security advisory (not public issue)
```

## Triage Labels

- `bug` / `enhancement` / `question` / `documentation`
- `needs-repro` — missing repro steps
- `good first issue` — well-scoped, self-contained, clear acceptance criteria
- `help wanted` — maintainer won't tackle soon, welcomes contribution
- `wont-fix` — out of scope or by design, always explain why
- `breaking-change` — PR/issue involves API change

## Good First Issue Criteria

Must have:

1. Clear description of change needed
2. Pointer to relevant file(s)
3. Acceptance criteria: what "done" looks like
4. No architectural decisions required
5. Estimated scope: 1 file, \<50 lines
