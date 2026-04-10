# Mode: Ecosystem Impact (for library maintainers)

Replace `mypackage` in the commands below with the actual package name (e.g., from `gh repo view --json name --jq .name`).

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

```
## Ecosystem Impact: [change description]

### Downstream Consumers Found
- [repo]: uses [specific API being changed]

### Breaking Risk
- [High/Medium/Low] — [N] known consumers of changed API
- Migration path: [available / needs documentation]

### Recommended Communication
- [create migration guide / add deprecation warning / notify maintainers directly]
```

Run `mkdir -p .reports/analyse/ecosystem` then write the full report to `.reports/analyse/ecosystem/output-analyse-ecosystem-$(date +%Y-%m-%d).md` using the Write tool — **do not print the full analysis to terminal**.

Read the compact terminal summary template from `.claude/skills/_shared/terminal-summaries.md` — use the **Ecosystem Impact Summary** template. Replace `[skill-specific path]` with `.reports/analyse/ecosystem/output-analyse-ecosystem-$(date +%Y-%m-%d).md`, ensuring the output opens with `---` on its own line, followed by the entity line on the next line, includes a `→ saved to <path>` line at the end, and closes with `---` on its own line after it. After printing to the terminal, also prepend the same compact block to the top of the report file using the Edit tool — insert it at line 1 so the file begins with the compact summary followed by a blank line, then the existing `## Ecosystem Impact:` content.
