**Re: Compress markdown to caveman format**

# Mode: Ecosystem Impact (for library maintainers)

Replace `mypackage` in commands below with actual package name (e.g. from `gh repo view --json name --jq .name`).

```bash
# Find downstream dependents on GitHub
gh api "search/code" --field "q=from mypackage import language:python" \
    --jq '[.items[].repository.full_name] | unique | .[]'

# Check PyPI reverse dependencies (who depends on us?)
# Requires johnnydep: pip install johnnydep (not installed by default — skip if unavailable)
# johnnydep mypackage --fields=name --reverse 2>/dev/null || echo "johnnydep not available — skipping PyPI reverse deps"

# Check conda-forge feedstock dependents
gh api "search/code" --field "q=mypackage repo:conda-forge/*-feedstock filename:meta.yaml" \
    --jq '[.items[].repository.full_name] | .[]'
```

Produce:

```markdown
## Ecosystem Impact: [change description]

### Downstream Consumers Found
- [repo]: uses [specific API being changed]

### Breaking Risk
- [High/Medium/Low] — [N] known consumers of changed API
- Migration path: [available / needs documentation]

### Recommended Communication
- [create migration guide / add deprecation warning / notify maintainers directly]
```

Run `mkdir -p .reports/analyse/ecosystem` then write full report to `.reports/analyse/ecosystem/output-analyse-ecosystem-$(date +%Y-%m-%d).md` using Write tool — **do not print full analysis to terminal**.

Read compact terminal summary template from `.claude/skills/_shared/terminal-summaries.md` — use **Ecosystem Impact Summary** template. Replace `[skill-specific path]` with `.reports/analyse/ecosystem/output-analyse-ecosystem-$(date +%Y-%m-%d).md`. Output opens with `---` on own line, entity line on next line, `→ saved to <path>` at end, closes with `---` on own line. After terminal print, prepend same compact block to top of report file via Edit tool — insert at line 1 so file begins with compact summary, blank line, then existing `## Ecosystem Impact:` content.
