# Shepherd Reply Protocol

Shared invocation pattern, `oss:shepherd` reply generation — used by `oss:review` (Step 9) and `oss:analyse` (Step 7). Read file, spawn shepherd with skill-specific vars substituted.

## Spawn pattern

```bash
SPAWN_DATE="$(date -u +%Y-%m-%d)"
```

```
Agent(subagent_type="oss:shepherd", prompt="
  Read the report at <REPORT_PATH>.
  PR/issue number: <N>. Contributor handle (if known): <HANDLE>.

  Write a two-part contributor reply:

  **Part 1 — Reply summary** (always present, always complete standalone):
  (a) acknowledgement + praise naming what's genuinely good — technique, structural decisions, test quality — 1–3 concrete observations, not generic;
  (b) thematic areas needing improvement — no counts, no itemisation; name concern areas concretely enough contributor knows what to look at without Part 2;
  (c) optional closing sentence only when Part 2 follows (e.g. 'I've left inline suggestions with specifics.').

  **Part 2 — Inline suggestions** (MANDATORY whenever ≥1 finding references specific file:line; omit ONLY for true LGTM with no location-specific findings. Single unified table, never fold file:line findings into Part 1 prose):
  | Importance | Confidence | File | Line | Comment |
  — Importance and Confidence as two leftmost columns; high → medium → low → praise → learning, most confident first within tier; `praise` = explicit positive reinforcement (good technique, structural decisions, test quality); `learning` = educational note with no defect (explain pattern/context); both appear after low findings, non-blocking;
  1–2 sentences per row for high items; include all high/medium/low findings in one table, every file:line finding gets own row, not prose paragraph.
  No column-width line-wrapping in prose.

  Write your full output to <OUTPUT_PATH> using the Write tool.
  Return ONLY a one-line summary: \`part1=done | part2=N_rows | → <OUTPUT_PATH>\`
")
```

## Output path convention

| Caller | Output path |
| -- | -- |
| `oss:review` | `.temp/output-reply-<PR#>-<date>.md` |
| `oss:analyse` | `.reports/analyse/thread/output-reply-thread-<N>-<date>.md` |

## Terminal summary

After shepherd returns, print:

```text
  Part 1  — reply summary (complete standalone)
  Part 2  — N inline suggestions

  Reply:  <output-path>
```
