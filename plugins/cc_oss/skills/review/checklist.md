<!-- policy-sibling: plugins/cc_develop/skills/review/templates/review-checklist.md — local-diff fork: PR/issue-linkage HIGH patterns intentionally oss-only; `[blocking]`-tag admission to the blocking bucket (templates/review-report.md) and the `[blocking]` clause after the test-gap ceiling (templates/agent-prompts.md) intentionally oss-only, tied to the Block-tier catalogue; Annotation-completeness taxonomy reference oss-only; sync the shared patterns and rules both ways -->

# Review Checklist

## CRITICAL Patterns (must block merge)

- `pickle.load` / `torch.load` without `weights_only=True` on external data → arbitrary code execution via insecure deserialization
- Hardcoded secret in source (password, API key, token)
- `debug=True` in production web server entry point

## HIGH Patterns

- Missing input validation on external HTTP input (not MEDIUM)
- Non-atomic registry/store update: in-memory index + filesystem op without temp-then-rename pattern. Look for: `save_index()` + `shutil.copytree()`, `delete from dict` + `os.remove()`, or any two-phase commit without temp-then-rename
- PR linked to issue but code changes don't address identified root cause — root cause misalignment
- PR description diverges from linked issue's stated problem — scope divergence (solving different thing than reported)

## MEDIUM Patterns (default anchor)

- Documented-contract or docstring-promise violation (wrong returned value, ignored parameter, ordering or state not maintained as documented) with no CRITICAL/HIGH pattern above → MEDIUM. Raise to HIGH only with cited evidence of wider impact: `imported_by` tier moderate/high (≥5 importers), an unhandled exception on the main path, or a HIGH pattern match. Importer count unknown → stays MEDIUM.

## Consolidation Rules

- Parent validates every severity against exact source/contracts/callers/tests/configuration and relevant CI before publication. Static proof is sufficient when conclusive. Only confirmed actionable defects enter findings.jsonl; open concerns remain separately durable with ID/owner/next check/closure. Bad repair does not refute the defect. New consolidated claims return through validation. Feasible checks undone mean incomplete; actual unavailable prerequisites after feasible checks mean blocked. Neither supports unconditional approval or rejection. Presentation caps never discard a confirmed actionable finding from findings.jsonl or its validation record.
- Signal-to-noise filter: classify each finding as (a) genuine defect/architectural issue or (b) style/completeness observation (unused import, print-vs-logging, missing class-level docstring on class with method-level docstrings)
- Well-scoped modules (≤5 public APIs): max 1 style item per section
- Target: GT+2 findings total per module — 10 nits obscure 2 critical fixes
- Pre-flight: before writing section, count total findings; count exceeds CRITICAL/HIGH plus 2 → group low-priority display rows; retain every confirmed actionable candidate in findings.jsonl and the validation envelope
- Annotation completeness: ≥1 HIGH/CRITICAL present → group LOW type annotation, docstring nits — handled by `foundry:linting-expert` or pre-commit hooks — presentation grouping only; confirmed nits stay in findings.jsonl, so `_shared/review-section-taxonomy.md` §LOW Grouping Rule (never omit LOW) still applies
- Cap each non-critical section at 5 items; group additional lower-priority findings with stable IDs and retain every confirmed actionable record

## Actionable Findings Format

For findings needing human decision (blocking issues, architectural trade-offs, deprecation choices):

- **[SEVERITY] Finding title** — `file:line` context
  - **Issue**: one-sentence description of what wrong or uncertain
  - **Recommendation**: what to do and why (lead with action, not analysis)
  - **Options**:
    - A) [recommended] — description, effort/risk
    - B) alternative — description, effort/risk
    - C) No action — risk accepted

Rules: lead with recommendation; one finding per block; skip options for obvious fixes; always include "no action" option

## Suppressions (DO NOT flag these)

- `print()` in CLI tools and scripts (not logging violation)
- Missing docstrings on private functions (underscore-prefixed)
- `# type: ignore[specific-code]` with specific error code (intentional)
- `# noqa: RULE` with explicit rule code (intentional)
- `Any` type in test fixtures and conftest.py
- Single-use helper functions without docstrings (self-documenting by name)
