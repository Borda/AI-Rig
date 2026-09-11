<!-- file: checks-index.md — consumers: audit/SKILL.md Step 4 -->

<!-- Quick-reference index only. Full check implementations in `audit/templates/checks-*.md`. -->

| # | Name | Severity | Scope | Notes |
| -- | -- | -- | -- | -- |
| 1 | Inventory drift (MEMORY.md vs disk) | medium | setup | Agents + skills on disk vs MEMORY.md roster |
| 2 | README vs disk | medium | setup | Agent/skill table rows in README vs disk |
| 3 | settings.json permissions | medium | setup | Bash commands in skills vs allow list |
| 4 | permissions-guide.md drift | medium | setup | Every allow entry needs a guide row, and vice versa |
| 5 | Permission safety audit | critical/high | setup | Allow entries must be non-destructive, reversible, local-only |
| 6 | Stale settings.json allow entries | low | setup | Allow entries with no usage in any .claude/ file |
| 7 | codex plugin integration | medium | setup | Plugin installed and enabled; dispatches work |
| 8 | foundry plugin correctness | critical/high/med | setup | 8a manifest, 8b symlinks, 8c hook scripts, 8d hooks.json, 8e dry-run validate, 8f perms drift, 8g init skill placement |
| 9 | Agent color drift | medium | setup | statusline COLOR_MAP vs agent frontmatter color: |
| 10 | RTK hook alignment | high/medium | setup | RTK_PREFIXES vs installed RTK subcommands - skip if rtk absent |
| 11 | Memory health | low | setup | 11a duplicate rules, 11b stale version pins, 11c absorbed feedback files |
| I1 | Plugin cache intact | high | setup | foundry in ~/.claude/plugins/installed_plugins.json; installPath exists |
| I2 | Settings merge complete | medium | setup | statusLine, permissions.allow, enabledPlugins.codex in ~/.claude/settings.json |
| I3 | Link health (conditional) | high | setup | Symlinks in ~/.claude/rules/ and ~/.claude/TEAM_PROTOCOL.md resolve; fix: /foundry:setup |
| 12 | File length | medium | all | Agents ≈300 lines, skills ≈600 lines, rules ≈200 lines; report only — fix = remove content, never collapse lines |
| 13 | Heading hierarchy continuity | medium | all | Heading level jumps >1 (e.g. ## to ####) |
| 14 | Orphaned follow-up references | medium | agents/skills | Skill-name refs in SKILL.md vs disk inventory |
| 15 | Hardcoded user paths | high | agents/skills | /Users/ and /home/ in config files + settings.json |
| 16 | Example value vs. token cost | low | agents/skills | Inline examples: high-value vs. low-value (prose restatement) |
| 17 | Cross-file code block inventory | low | agents/skills | Block count table across all .md files (skills, modes, \_shared, templates, agents, rules); flag files with ≥10 blocks for `--efficiency` run. NxN similarity analysis moved to `--efficiency` Phase B2 (expensive) |
| 18 | Rules integrity | high/medium | rules | 18a inventory, 18b frontmatter, 18c redundancy, 18d cross-ref integrity |
| 19 | Model tier + effort appropriateness | medium/high | agents | Tier policy: fable/opusplan/opus/sonnet/haiku; effort low/medium/high/xhigh/max — report only. Flag `effort:` on a haiku-tier agent (Haiku 4.5 ignores it) and any tier downgrade with no A/B evidence |
| 20 | Agent description routing | medium/low | agents | 20a overlap pairs, 20b NOT-for coverage, 20c trigger specificity, 20d keep/sharpen/prune |
| 22 | Calibration coverage gap | medium/low | agents/skills | Unregistered calibratable skills/agents; stale domain table entries |
| 23 | Bash misuse / native tool substitution | medium | agents/skills | cat/grep/find/echo>/sed replaceable by native tools; 23a python inline; 23b `# timeout: N` without `timeout S` shell prefix or Python `subprocess.*` without `timeout=`; 23c `eval "$(...)"` for multi-value bin/ output — use §Script Output Routing (TMPDIR files) instead; 23d shell variable used across `Bash()` calls — var dies at shell boundary, write to TMPDIR file |
| 24 | Skill sequence compatibility | high/medium | skills | 24a target skill not on disk; 24b argument absent from argument-hint; scans skills, agents, READMEs |
| 25 | Implicit agent references | high | agents/skills | subagent_type without plugin prefix; exempt: built-in types |
| 26 | Symbol and shortcut consistency | medium/low | agents/skills | 26a same-concept emoji conflict, 26b slash notation mixed, 26c body contradicts legend |
| 27 | Cross-plugin shared-file ref integrity | critical/high/med | skills | 27a absent from foundry/\_shared/; 27b catch-22 (fallback needs foundry); 27c plugin-local \_shared/ unmounted |
| 28 | Cross-plugin agent dispatch fallback | high/medium | skills | 28a no fallback for cross-plugin dispatch; 28b fallback present but incomplete |
| 29 | LLM context minimality | medium/low | agents/skills/rules | Within-file repetition, prose inflation, obvious-consequence restatement — report only; 29a trigger-inverse restatement; 29b hedged/non-actionable directives |
| 30 | Config token overhead | medium/low | setup | 30a CLAUDE.md + global + rules/ > 100 KB; 30b single rules file > 10 KB |
| 31 | Tool-body consistency | medium | skills | Skill `allowed-tools` must include every tool the workflow body invokes; see `checks-skills.md` for full spec |
| 32 | Dead file detection | medium/low | skills/rules | 32a mode files in `*/modes/` not referenced from parent SKILL.md; 32b template files in `*/templates/` not referenced; 32c rule files whose `paths:` globs match no project files; 32d orphaned bin/ scripts not referenced by any plugin .md file (LOCAL_MODE only) |
| 33 | Code block similarity + extraction feasibility | medium/low | skills | `--efficiency` only — Table 1: pairwise similarity per plugin; Table 2: rigidity + extraction feasibility + pos/neg impact. See `modes/efficiency.md` Phase B2 |
| R1 | Computed path resolution (local + installed duality) | high/medium/info | plugins (LOCAL_MODE only) | R1-FAIL: file exists locally but absent from installed cache; R1-WARN: installed-only file; R1-INFO: plugin not installed. Root cause guard for silent-deletion class of bugs |
| R2 | Grep-visible referencing (orphan-risk detection) | medium | plugins (LOCAL_MODE only) | Basename of indirect-load .md file (modes/, templates/, \_shared/) not literal in any consumer .md — deletion-prone; fix: add `# loads: <basename>` comment |
| R3 | bin/ script existence at local + installed | high | plugins (LOCAL_MODE only) | R3-FAIL: script referenced but missing locally; R3-WARN: script local but absent from installed cache |
| R4 | bin/ Python test coverage | medium | plugins (LOCAL_MODE only) | Every `bin/*.py` has matching non-empty `tests/test_<basename>.py` with ≥1 `def test_` function that is not a pure `pass`/`...` stub |
| 34 | Roster boundary alignment | medium/low | agents | 34a per-pair overlap >50% (>30% with --eager), 34b coverage gaps (task domain with no clear owner), 34c Sharpen Boundary section when --eager |
| 35 | $ARGUMENTS shell injection | security | agents/skills | Bash blocks interpolating env-var user input without shlex-safe quoting |
| 36 | eval-unsafe bin/ output | security | agents/skills | Python bin/ scripts producing shell assignments for eval without `shlex.quote` |
| 37 | Hardcoded secrets in config | security | all | API keys, tokens, passwords literal in any plugin `.md`, `settings.json`, or hook `.js` |
| 38 | AskUserQuestion cap violation | high | skills | Skill branch path with >4 `AskUserQuestion` calls — exceeds `communication.md:86` hard limit |
| 39 | Plugin version freeze | medium | setup | `plugin.json` version unchanged vs HEAD despite modified plugin files |
| 40 | Health monitoring gap | high/medium | agents/skills | Skill calling `Agent(subagent_type=…)` without sentinel, liveness probe, or hard cutoff (high); or still prescribing a fixed-interval poll (medium — spawns are background, nothing sleeps) |
| R5 | Consumer→template orphan | medium | plugins (LOCAL_MODE only) | `<!-- loads: X -->` or `# loads: X` comment points to non-existent template file — reverse of R2 |
| 41 | LLM-first formatting | low/medium | all .md excl. README | 41a list-marker uniformity (`-` only); 41b numbering-intent clarity (`1.`=steps, `(a)`=choices); 41c table-vs-prose preference (3+ items × 2+ attrs → table); 41d legacy phase/step numbering (`Phase A.5`/`Step 1.5` instead of canonical `1b`) |
| 42 | CLI flag drift vs. spawn-prompt vars (number collision) | medium/critical | plugins | Two unrelated checks both claim `42`: index/Step-1b use it for CLI flag drift (`check_cli_flag_drift.py`, SKILL.md flags vs argparse, medium); `checks-skills.md` separately defines `Check 42` as "Unexpanded variables in agent spawn prompts" (`check_spawn_prompt_vars.py`, critical). Pre-existing, predates Check 45 — not resolved here; needs a deliberate renumber pass (touches severity tables + dispatch lists in `steps-4-5-7.md`), not a drive-by fix |
| 43 | Shell variable persistence across Bash calls | critical | plugins/skills | `check_bash_persistence.py` — `$VAR` referenced in bash block N, assigned only in earlier block M — each Bash call is a fresh shell, silently expands to empty string |
| 44 | Sub-check naming symmetry vs. TMPDIR sentinel scoping (number collision) | low/high | agents/skills/rules | Two unrelated checks both claim `44`: `checks-shared.md` defines it as "Sub-check naming symmetry" (`Check Nb` without `Check Na`, low); `checks-skills.md` separately defines it as "TMPDIR sentinel session scoping" (bare `/tmp` sentinel names collide across sessions, critical). Pre-existing, predates Check 45 — not resolved here |
| 45 | Policy-sibling marker symmetry | high/medium | all .md | 45-BROKEN: `<!-- policy-sibling: ... -->` lists a path not on disk; 45-ASYMMETRIC: listed sibling has no marker pointing back — reference-graph completeness for policies restated (not cross-ref'd) across files |
