Output complete audit summary. List each audited file by name in `### Files Audited` — from Step 2 inventory; counts alone insufficient.

```markdown
---
Title:    Audit — .claude/ config
Date:     [YYYY-MM-DD]
Scope:    [N agents, N skills, N rules, N hooks]
Focus:    [config quality audit — agents / skills / routing / all]
Agents:   foundry:curator, foundry:challenger (adversarial mode only)
Outcome:  CLEAN | NEEDS_ATTENTION | BLOCKED
Findings: [N] security · [N] critical · [N] high · [N] medium · [N] low
Confidence: [aggregate of agent Confidence-block scores]
Next steps: /foundry:setup (sync clean config) | fix findings → re-run /foundry:audit
Path:       → .reports/audit/[report timestamp]/report.md
---

## Audit Complete — .claude/ config

### Files Audited
- **Agents** (N): name-1, name-2, ...
- **Skills** (N): name-1, name-2, ...
- **Rules** (N): name-1, name-2, ...
- **Hooks** (N): file-1.js, file-2.js, ...
- **Settings**: settings.json
- **Communication** (if in scope): communication.md, quality-gates.md, TEAM_PROTOCOL.md, file-handoff-protocol.md

### Unscanned Plugins

> Omit section entirely when the `unscanned-plugins` sentinel is empty (`--local`/`plugins` scope only — see SKILL.md Step 2 coverage reconciliation).

⚠ UNSCANNED: codemap-py — no files matched scope globs

**A non-empty section here means the sweep above is incomplete for that plugin** — its findings count of zero is not "clean", it is "not checked". Re-run after confirming the plugin's `.claude-plugin/plugin.json` `skills`/`agents` fields resolve correctly.

### Security Findings

> Omit section entirely when no security findings present.

| Severity | ID | File | Finding | Fix |
|---|---|---|---|---|
| `security/critical` | C37-1 | `hooks/foo.js` | Hardcoded API key on line 12 | Remove; use env var `$MY_KEY` |
| `security/high` | C35-1 | `skills/bar/SKILL.md` | Unquoted `$ARGUMENTS` in bash block | Route via parse_scan_args.py |

**Required action before any `/foundry:setup` or merge**: resolve all security findings — they are not deferred to "fix all" queue.

### Findings
| Severity | Found | Fixed | Remaining |
|---|---|---|---|
| security | N | N | 0 |
| critical | N | N | 0 |
| high | N | N | 0 |
| medium | N | N | 0 |
| low | N | N ("Fix all" only) | N |

**Fix convergence**: Converged in N pass(es) — 0 fixable findings remain.
```

Or if limit hit:

```markdown
**Fix convergence**: ⚠ CONVERGENCE LIMIT reached (5 passes) — N fixable findings remain (see Remaining section).
```

(Omit fix convergence line when user picked "skip" from gate — only shown when fix option chosen.)

```markdown
### Fixes Applied

| File | Change |
| --- | --- |
| agents/foo.md | Replaced broken ref `old-agent` → `correct-agent` |

### Remaining (low/nits — auto-fixed only with 'fix all'; otherwise manual review optional)

- [low findings that were not auto-fixed]
- [any infinite loops flagged for user decision]
```

### Code Block Similarity

Include when `$RUN_DIR/similarity-check33.md` exists (`--efficiency` mode only). Read file and embed both tables verbatim — do NOT summarize. Label:

```markdown
#### Purpose-based similarity clusters — Check 33 / --efficiency only
[table 1]
[table 2]
```

> Replace each placeholder with the corresponding table from `similarity-check33.md`, verbatim and in order.

Omit section if `similarity-check33.md` absent (efficiency not active or no clusters found).

```markdown
### Agent Confidence

| File | Score | Label | Gaps |
| --- | --- | --- | --- |
| agents/foo.md | 0.92 | high | — |
| skills/bar/SKILL.md | 0.64 | ⚠ low | no runtime data for bash validation |

Low-confidence files re-audited: N | Still uncertain after retry: N (see gaps above)

### Next Step

Run `/foundry:setup` to propagate clean config to ~/.claude/

```
