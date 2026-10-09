---
description: Markdown authoring — no hard-wrapped prose, structure selection, behavior-sensitive reformatting, instruction-file compression gate, link verification; loads on first access of any Markdown file
paths:
  - '**/*.md'
---

## Markdown Authoring

<!-- policy-sibling: plugins/cc_foundry/rules/communication.md (§Markdown Authoring stub) -->

- **Never hard-wrap prose** in any Markdown file: each prose paragraph on one physical line; keep intentional structural breaks in headings, lists, tables, blockquotes, links, HTML `<details>` blocks, fenced code
- Never blindly unwrap or reflow a whole file — edit only the intended prose, retain surrounding structure
- Structure for scanning and correct execution, not line length alone: when one paragraph combines multiple actions, conditions, actors, statuses, exceptions, or decision branches, use the smallest fitting structure:
  - Parallel obligations or independently checkable facts → bullets.
  - Ordered actions, recovery paths, or state transitions → numbered lists.
  - Ordered sub-steps nested under a numbered item → letters, written as bullets with a letter label (`- a. …`, `- b. …`), so references read `2b`, never `2.2`; CommonMark has no lettered list type.
  - Compact closed mappings or comparisons with repeated fields → tables; keep long causal explanations out of table cells.
  - Genuine notes, warnings, interpretation limits, or safety boundaries → blockquotes.
  - Optional depth that would interrupt the main path → existing or justified `<details>` block.
- Keep causal reasoning and cohesive rationale as prose. Do not convert paragraphs wholesale, add headings for every rule, or duplicate an existing navigation system. Keep headings concise and move detailed contracts below them.
- Reformatting behavior-sensitive agent, skill, setup, approval, or recovery instructions → preserve modal language, exact literals, ordering, stop conditions; run affected contract and calibration gates — formatting changes instruction salience even when words stay similar

## Instruction-File Compression Gate

<!-- policy-sibling: plugins/cc_foundry/rules/claude-config.md (§Instruction-File Compression Gate stub) -->

Applies only to instruction files an LLM host loads (`AGENTS.md`, `CLAUDE.md`, rule, skill, or agent-definition files). Compression or structural reformatting of one is behavior-sensitive and must pass every gate before handoff:

1. Save a verified byte-exact pre-change backup outside active instruction-discovery paths (a directory no host loads as instructions); never overwrite an existing backup.
2. Compare the backup and result for complete semantic preservation: scope, actors, obligations, modal strength, exceptions, ordering, approval and stop conditions, thresholds, examples, and cross-file relationships must remain unambiguous.
3. Preserve headings, list hierarchy, fenced and inline code, commands, paths, URLs, identifiers, versions, numbers, environment variables, and other behavior-bearing literals exactly unless the task explicitly changes them.
4. Run the project's affected Markdown, instruction-contract, and calibration gates where it has them. Broad instruction-set changes also require an independent agent followability review against the pre-change backup.
5. Reject the compression and restore the pre-change file when any instruction is lost, weakened, broadened, made ambiguous, harder to navigate, or less reliably followed. An unresolved comparison difference blocks completion.

## Link Verification

<!-- policy-sibling: plugins/cc_foundry/rules/quality-gates.md (§Link Verification stub) -->

**Never add a URL without all four steps, every time — no exemption for domain/protocol/path similarity to an already-verified URL:**

1. **Fetch** — call WebFetch (or equivalent); URL must return non-error (not 4xx/5xx). HTTP 200 is necessary but not sufficient — steps 2 and 3 still mandatory
2. **Read** — read the actual page content; don't rely on URL structure or HTTP status alone
3. **Match** — confirm content matches the intended description; no match = don't add the link
4. **Independent** — every URL needs its own Fetch+Read+Match pass; a verified URL on the same domain doesn't exempt others; skipping any step — including inferring validity from URL structure or HTTP status alone — is a violation

Applies to: agent files, skill files, CLAUDE.md, any markdown — and to a URL in any other file type (the always-loaded `quality-gates.md` §Link Verification stub carries the four steps there).
