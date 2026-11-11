Severity order: **security** → **critical** → **high** → **medium** → **low** → **nit**

`Weight` is the score each finding contributes in the Adversarial Convergence Loop (`quality-gates.md`), which decides when a review → fix cycle has converged, plateaued, or gone backwards.

| Severity | Weight | Examples |
| -- | -- | -- |
| **security** | 20 | Shell injection where argument text reaches an interpreter as source — `eval "cmd $ARGUMENTS"`, `bash -c "… $ARGUMENTS …"`, `python -c "… $ARGUMENTS …"`, unquoted `$ARGUMENTS` in heredoc expansion position — hardcoded secrets (API keys, tokens, passwords) in config files, eval-unsafe bin/ output (unquoted shell assignments), `subprocess` with `shell=True` or string-concatenated command args, path traversal via unvalidated argv, `eval` with external input in shell scripts, unquoted variable expansion in command position, `pickle.load`/`yaml.load` without safe loader on external data |
| **critical** | 10 | Broken cross-reference (agent/skill not exist on disk), relative path silently fall back to wrong dir |
| **high** | 6 | Argument text handled only by a shell construct — a `[[ =~ ]]` guard, a `case`, or a substring comparison — with no `bin/` parser behind it: no interpreter runs the text, but the guard is unreliable (zsh sets `match`, not `BASH_REMATCH`) and a quote or newline in the blob breaks the block at parse time, skipping every step below it. Dead loop in follow-up chain, missing settings.json permission for tool in use, broken code example (undefined variable, wrong command syntax), agent/skill instruction directly contradicts `.claude/CLAUDE.md` directive, deprecated/invalid hook event name or type in use, tool declared in `tools:`/`allowed-tools:` needed but absent causing silent failures, `deep-reasoning` or `plan-gated` agent declared on `sonnet` (underpowered for tier) |
| **medium** | 4 | Duplication across files, stale model name, README row missing for existing skill, hardcoded `/Users/<name>/` path, undocumented modes in inputs, deprecated frontmatter field or settings key, permissions-guide.md missing row for allow entry or has orphaned row, declared tool not referenced anywhere in workflow (unnecessary permission surface), `focused-execution` agent declared on `opus` (overkill for tier), `opusplan` in agent frontmatter (runs `sonnet` — subagents never enter plan mode) |
| **low** | 2 | Verbosity, minor formatting, incomplete follow-up chain, outdated version pin with "autoupdate" note, agent/skill omits CLAUDE.md principle but no contradiction, 💡 new CC feature not yet used, inline example restates prose or superseded by `AGENTS.md`/`CONTRIBUTING.md` |
| **nit** | 1 | Wording preference, a synonym swap, a comment that reads slightly better another way — anything whose fix changes no behaviour and no reader's decision |

Check 47 (`--consolidate`) is advisory and carries no severity: weight 0, never scored, never in the fix gate.

The report-only advisory checks are a closed list: Checks 6, 8, 10, 12, 13, 19, 20, 23a, 23b, 23e, 24, 25, 26, 27, 28, 29 (including 29a/29b), 30a–30f, 31a (omitted effort and when_to_use overflow/duplication), 34, 41 (including 41a–41d), 42 (unexpanded spawn variables), 43, 46, C32, C35a–C35e, R1-WARN, R1-INFO and Check 47. Preserve their documented exclusions; do not invent additional report-only classes. Severity-bearing advisory rows set `auto_fixable:false` and enter option-(c) decision collection; severity-free advisories stay outside the scored ledger and fix gate. Systemic exclusions in fix.md remain separate from this advisory list. Missing eligibility is an unknown exclusion with a recorded reason, never a discretionary advisory classification.

## Canonical defect classes

Use one severity per class across checks; these entries take precedence over check-specific wording. A separately demonstrated runtime failure is a separate finding with its own evidence, not an assumed consequence of drift.

| Defect class | Severity | Checks and evidence boundary |
| -- | -- | -- |
| MEMORY.md inventory drift | medium | Checks 1 and 18a: documented roster differs from disk; drift alone does not demonstrate failed dispatch |
| Cross-call shell state | medium | Checks 23d and 43: a later Bash call reads an assignment that was not persisted; prove any additional runtime failure separately |

## Antipatterns (severity under-classification — common calibration failures)

- Any injection vector, secret leak, or eval-with-external-input → always **security**, not **critical**

- `subprocess(shell=True)` with any external input source → always **security**, not **high**

- Argument text guarded only by `[[ =~ ]]`, `case`, or a substring test → **high**, not **security** and not **medium** — no interpreter receives the text, so the injection tier does not apply, but the guard is unreliable and its block breaks on a quoted blob

- Hardcoded credentials (even "test" or "example" tokens with real format) → always **security**

- For inventory drift and cross-call shell state, apply §Canonical defect classes consistently; never promote severity solely from an untested runtime premise.

- `deep-reasoning` agent on `sonnet` → **high**, not "possibly underpowered" (medium) — tier table is authority

- Direct CLAUDE.md contradiction → **high**, not "best practice concern" (medium) — governance hierarchy applies
