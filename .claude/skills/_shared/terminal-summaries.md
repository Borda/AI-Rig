# Compact Terminal Summary Templates

Shared compact terminal summary templates for `/analyse` and `/review`. All templates follow the same pattern: `---` delimiters, entity identifier line, key-value fields, `→ saved to [skill-specific path]`.

## PR Summary

```
---
[entity-line]
Verdict:     [🟢 Approve / 🟡 Minor Suggestions / 🟠 Request Changes / 🔴 Block] — [one sentence]
CI:          [passing / failing / pending]
Risk:        [n]/5 [low / medium / high]
Blockers:    [N] must-fix | [N] suggestions
Suggestions: 1. [most important action]
             2. [second action]
             ...
→ saved to [skill-specific path]
---
```

Replace `[entity-line]` with the skill-specific identifier, e.g.:

- `/analyse` PR mode: `PR #[number] — [title]`
- `/review`: `Review — [target]`

## Extended Fields (review only)

When using the **PR Summary** template in `/review`, **omit the `Suggestions:` field** from the base template — `Recommendation:` below replaces it. Insert these fields after `Blockers:` and before `→ saved to`:

```
Recommendation:
  1. [most important action for the author]
  2. [second action if needed]
Summary:     [2–3 sentence overview of key findings]
Critical:    [list blocking/critical items one per line, or "none"]
Confidence:  [aggregate score] — [key gaps]
```

## Issue Summary

```
---
Issue #[number] — [title]
Priority:    [Critical / High / Medium / Low]
Top cause:   [one-sentence top hypothesis]
Action:      [most important next step — e.g. "apply fix in foo.py:42", "close as duplicate of #N", "request repro steps"]
→ saved to [skill-specific path]
---
```

## Discussion Summary

```
---
Discussion #[number] — [title]
Category:    [category name]
Status:      [open / closed]
Answered?:   [yes — @[answerer] | no]
Key points:  [2–3 bullet items summarising the main viewpoints]
Action:      [most useful next step — e.g. "mark answered", "convert to issue", "update docs"]
→ saved to [skill-specific path]
---
```

## Repo Health Summary

```
---
Repo Health — [repo]
Issues:      [N open] ([N stale], [N needs triage])
PRs:         [N open] ([N awaiting review], [N CI failing])
Top action:  [single most urgent recommendation]
→ saved to [skill-specific path]
---
```

## Duplicate Detection Summary

```
---
Duplicates — "[keyword]"
Groups:      [N groups found]
Close now:   [list of #N → duplicate of #canonical, one per line, or "none"]
Top action:  [single most impactful triage step]
→ saved to [skill-specific path]
---
```

## Contributor Activity Summary

```
---
Contributors — [repo]
Top:         @[handle] ([N] commits in 90d)
Cadence:     avg [N days] | last release [date] | [overdue? yes/no]
Bus factor:  [low / medium / high — one-liner]
Top action:  [single most urgent recommendation]
→ saved to [skill-specific path]
---
```

## Ecosystem Impact Summary

```
---
Ecosystem Impact — [change description]
Consumers:   [N known downstream users of changed API]
Risk:        [High / Medium / Low]
Top action:  [single most urgent recommendation — e.g. "create migration guide", "add deprecation warning"]
→ saved to [skill-specific path]
---
```
