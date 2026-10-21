---
name: manage
description: 'Create, update, or delete agents, skills, and rules; update or delete hooks (hook creation not yet implemented — edit `hooks/<name>.js` directly). Full cross-reference propagation. Trivial edits (typos, small fixes ≤10 words) applied inline without agent; `.md` content-edits delegated to foundry:curator; `.py` and hook `.js` (under `hooks/`) edits delegated to foundry:sw-engineer, other code files (`.ts`, non-hook `.js`, `.sh`) edited inline and flagged as having no owning agent; large cross-ref fan-outs (> 3 files) also delegate. The parent orchestrates MEMORY.md, README, audit, calibration, and the final report. Also manages settings.json permissions atomically with permissions-guide.md. NOT for: validation/quality audit of existing agents/skills (use /foundry:audit); implementing application source code changes outside `.claude/` (use develop:feature or develop:fix — requires `develop` plugin).'
argument-hint: create <agent|skill|rule> <name> "desc" | update <name> [new-name|"change"|spec.md] | delete <name> | add perm <rule> "desc" "use-case" | remove perm <rule>
effort: medium
disable-model-invocation: true
allowed-tools: Read, Write, Edit, Bash, Grep, Glob, Agent, TaskList, TaskCreate, TaskUpdate, AskUserQuestion
---

> **Note:** `disable-model-invocation: true` — `/manage` user-invoked only, no `Skill()` chaining from orchestrators. When suggesting `/manage` as follow-up, invoking skill must present as user-run command, not auto step.

<objective>

Manage lifecycle of agents, skills, rules, hooks in `.claude/`. Handles creation with rich domain content, atomic renames with cross-ref propagation, content editing (routed by file type — Step 4), clean deletion with broken-ref cleanup. Keeps MEMORY.md inventory in sync with disk.

</objective>

<inputs>

- **$ARGUMENTS**: required, one of:

  - `create agent <name> "description"` — create new agent with generated domain content
  - `create skill <name> "description"` — create new skill with workflow scaffold
  - `create rule <name> "description"` — create new rule file with frontmatter and sections
  - `update <name> <new-name>` — rename; type auto-detected from disk
  - `update <name> "change description"` — content-edit (routing: Step 4)
  - `update <name> <spec-file.md>` — content-edit from spec file (routing: Step 4)
  - `delete <name>` — delete; type auto-detected from disk (agents, skills, rules, hooks); asks user if ambiguous
  - `add perm <rule> "description" "use case"` — add permission to settings.json allow list and permissions-guide.md
  - `remove perm <rule>` — remove permission from settings.json allow list and permissions-guide.md

- Names must be **kebab-case** (lowercase, hyphens only)

- Descriptions must be quoted when containing spaces

- Permission rules use Claude Code format: `WebSearch`, `Bash(cmd:*)`, `WebFetch(domain:example.com)`

- `--skip-audit` — optional flag: skip Step 9 `/audit` validation (use inside `audit fix` loop to avoid recursion)

- **Spec-file paths must be quoted** — `update <name> <spec-file.md>` requires spec path quoted if it contains any whitespace (e.g. `update my-agent "docs/My Spec.md"`); unquoted paths with spaces split into multiple arguments, trigger argument-shape mismatch. Recommended: keep spec filenames free of spaces.

**Update/delete mode** — name looked up across agents, skills, rules automatically:

- One match on disk → proceed with that type
- Multiple matches → `AskUserQuestion`: (a) agent, (b) skill, (c) rule
- No match → report error and stop

**Update second-argument discrimination**:

- Two bare kebab-case args (second arg no spaces, no `.md` extension) → **rename mode**, confirmed interactively before any write (Step 1 rename confirmation gate) — an unquoted change directive such as `update my-agent add-error-handling` has this exact shape and would otherwise delete `my-agent`
- One name + quoted string → **content-edit mode** (routing: Step 4)
- One name + path ending in `.md` → **content-edit mode** (routing: Step 4)

**Examples:**

- `/foundry:manage create agent task-planner "Planning specialist for decomposing epics into actionable tasks"`
- `/foundry:manage update my-agent "add a section on error handling patterns"`
- `/foundry:manage update optimize docs/specs/YYYY-MM-DD-<spec-name>.md`
- `/foundry:manage delete old-agent-name`
- `/foundry:manage add perm "Bash(jq:*)" "Parse and filter JSON" "Extract fields from REST API responses"`

</inputs>

<constants>

- AGENTS_DIR: `.claude/agents`

- SKILLS_DIR: `.claude/skills`

- RULES_DIR: `.claude/rules`

- HOOKS_DIR: `.claude/hooks`

- AVAILABLE_COLORS: red, blue, green, yellow, purple, orange, pink, cyan

- AGENT_WATCH_DIR: `.temp/manage` · deadlines: `web-explorer` 300 s · `sw-engineer` 900 s · `curator` 900 s

Every spawn in this skill (Step 4 mode files, Step 5 cross-ref curator, Step 9 targeted curator, challenger gate) follows `_shared/agent-spawn-protocol.md` §Deadlines. Spawns here are sequential, one agent in flight at a time, so every spawn response **overwrites** `.temp/manage/agent-watch-manage.tsv` with that one agent's row (`<name>\t<deliverable or ->\t<deadline>`) via the Write tool — never a waiting tool (`ScheduleWakeup`, `ListAgents`, `Monitor`), probe, or sleep. On its completion notification run the check block once, before reading its output:

```bash
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/agent_watch.py" --state-dir ".temp/manage"  # timeout: 5000
```

`done` → continue · notification arrived but row not `done` (deliverable empty/missing, or idle without its envelope) → ⏱ `timed_out` now: surface it in the Step 10 report; a web-explorer ⏱ means default frontmatter fields (the existing missing-schema fallback). A ⏱ never answers, skips or defaults any question or gate below.

Colors in use are read from live Grep in Step 3 (authoritative) — no static used-color list to maintain. AVAILABLE_COLORS is the candidate pool for a new agent (the valid agent `color:` values); pick first entry not already in Step-3 set. All eight in use → reuse the least-used color and note the reuse in Step 10.

</constants>

<workflow>

**Task hygiene** — task tools may be deferred; load before first use: `ToolSearch(query="select:TaskList,TaskCreate,TaskUpdate,TaskGet", max_results=4)`. Call `TaskList` first and triage each task it returns: `completed` if work clearly done, `deleted` if orphaned, keep `in_progress` only if genuinely continuing. Never spend a turn on bookkeeping alone — every `TaskCreate`/`TaskUpdate` ships in the same response as the next substantive tool call; one exception, `TaskUpdate(completed)` immediately before a long output block (`rules/task-lifecycle.md`).

**Task tracking**: create tasks for each major phase; mark in_progress/completed throughout.

## Step 1: Parse and validate

Extract operation, type, name, optional arguments from `$ARGUMENTS`.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
# --keep-state: no eval, persists manage-skip-audit-${CSID} (Check 43) + clean-args; --inlined: manage keeps no compaction contract, so it clears none
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/parse-skill-flags.py" --flags skip-audit --keep-state manage --inlined "$ARGUMENTS" >/dev/null  # timeout: 5000
IFS= read -r ARGUMENTS < "${TMPDIR:-/tmp}/manage-clean-args-${CSID}" 2>/dev/null || ARGUMENTS=""
echo "${TMPDIR:-/tmp}/manage-skip-audit-${CSID}" > "${TMPDIR:-/tmp}/manage-skip-audit-path-${CSID}"
```

**Unsupported flag check** — after all supported flags extracted (`--skip-audit`), scan `$ARGUMENTS` for remaining `--<token>` tokens. Found: invoke `AskUserQuestion` with question text `` ! Unknown flag(s): `--<token>`. Supported: `--skip-audit`. `` — (a) **Abort** (stop, re-invoke with correct flags) · (b) **Continue ignoring** (skip unknown flags, proceed). On Abort: stop.

**Out-of-scope guard** — fires before validation when either holds: (a) first command word(s) in `$ARGUMENTS` are not `create`, `update`, `delete`, `add perm`, or `remove perm` (shape errors on recognized command words go to validation, not here); (b) request targets application source, a bug, or a feature outside agents/skills/rules/hooks/permissions. Deliver per Decline delivery below: `**! UNSUPPORTED — /foundry:manage manages agents, skills, rules, hooks, permissions only**` plus one redirect line (`/develop:fix` or `/develop:feature` (requires `develop` plugin)), then stop. Block + redirect = entire decline content: no diagnosis, root cause, patch, code snippet, test, or Step 2 onward. Decline delivery: when the spawn prompt names a deliverable or output file, first write the bold line, any required redirect, and `status: unsupported`, `reason` and `owner` to that file — never skip a mandated file; when it demands an envelope (`Return ONLY:`), return only `"status": "unsupported"`, `"reason"` and `"owner"` (agent/skill or `"none"`), plus `"file"` when a deliverable was written; a demanded fixed closing line (e.g. `Wrote: <id>`) takes precedence over an envelope and is the whole reply. With neither envelope nor fixed closing line required, reply only with the bold line and any required redirect. A decline is not an analysis, so it carries no Confidence block. <!-- policy-sibling: plugins/cc_foundry/agents/challenger.md, plugins/cc_foundry/agents/doc-scribe.md, plugins/cc_foundry/agents/linting-expert.md, plugins/cc_foundry/agents/creator.md, plugins/cc_foundry/skills/manage/SKILL.md — decline delivery -->

**Validation rules:**

- Name must match `^[a-z][a-z0-9-]*$` (kebab-case)
- For `create`: name must NOT already exist on disk; description required
- For `update`/`delete`: name MUST already exist on disk
- For `update` rename: new-name must NOT already exist on disk
- For `add perm`: rule must NOT already exist in settings.json allow list; description and use case required
- For `remove perm`: rule MUST already exist in settings.json allow list

**Type auto-detection** (for `update` and `delete`): first verify post-install context exists:

```bash
[ -d .claude ] || { printf "! .claude/ not found — confirm working directory is project root\n"; exit 1; }  # timeout: 3000
```

Then run all four Glob checks in parallel:

- Agent: pattern `agents/<name>.md`, path `.claude/`
- Skill: pattern `skills/<name>/SKILL.md`, path `.claude/`
- Rule: pattern `rules/<name>.md`, path `.claude/`
- Hook: pattern `hooks/<name>.js`, path `.claude/`

Results:

- One non-empty result: resolved type; proceed
- Multiple non-empty results: `AskUserQuestion`: "Multiple entities named `<name>` found. Which one? (a) agent (b) skill (c) rule (d) hook" — note: (d) hook valid for `update` and `delete` only; `create hook` not yet implemented (use Edit tool on `hooks/<name>.js` directly until create-hook mode added)
- All empty: report "No agent, skill, rule, or hook named `<name>` found", stop

For `create`, check only relevant type's path.

**Delete confirmation gate** — when `$MODE` is `delete`, immediately after type resolution invoke `AskUserQuestion`: "Delete `<name>` (`<type>`)? This cannot be undone. (a) Confirm · (b) Abort". On Abort: stop. On Confirm: proceed to Step 4.

**Rename confirmation gate** — when the argument shape resolves to `rename`, the second token is ambiguous by construction: `update my-agent add-error-handling` and `update my-agent new-agent-name` parse identically, and rename deletes the old file. Immediately after type resolution, before any write, invoke `AskUserQuestion`: "`<name>` → `<second-arg>`: rename the file, or apply `<second-arg>` as an edit directive?" — (a) **Rename** (write `<second-arg>`, delete `<name>`, propagate cross-refs) · (b) **Content-edit** (keep the name; set `MODE=content-edit` and `DIRECTIVE=<second-arg>`, continue from the edit-complexity classifier) · (c) **Abort** (stop, nothing written). Place (b) second — it is the recommended reading whenever the second token is not a plausible entity name.

```bash
jq -e --arg rule '<rule>' '.permissions.allow | index($rule) != null' .claude/settings.json >/dev/null 2>&1  # timeout: 5000
```

**Update second-argument discrimination** — apply after type resolved. Set `MODE` from parsed operation — a model-context value, not a shell variable (shell state does not persist across Bash calls); consumed by delete confirmation gate above, edit-complexity classifier below, and per-mode workflow branches in Step 4. Recognised values: `create`, `rename`, `content-edit`, `delete`, `add-perm`, `remove-perm`.

| Argument shape | `MODE` |
| -- | -- |
| `create <type> <name> "..."` | `create` |
| `update <name> <new-name>` (two bare kebab-case args; second has no spaces, no `.md`) | `rename` (validate new-name does NOT already exist; **destructive — rename confirmation gate above must pass first**) |
| `update <name> "<change>"` (one name + quoted string) | `content-edit` (validate spec non-empty; set `DIRECTIVE` = the quoted string) |
| `update <name> <spec>.md` (one name + path ending in `.md`; **must be quoted if path contains spaces**) | `content-edit` (validate spec file exists on disk and path ends in `.md`; report error if not found; set `DIRECTIVE` = contents of the spec file via Read tool) |
| `delete <name>` | `delete` |
| `add perm <rule> "..." "..."` | `add-perm` |
| `remove perm <rule>` | `remove-perm` |

Validation fails: report error, stop.

**Edit complexity classification** (content-edit mode only):

Classify `$DIRECTIVE` as **trivial** (`EDIT_TRIVIAL=true`; otherwise `false` — model-context value, like `MODE`) when ALL conditions hold:

| Condition | Required |
| -- | -- |
| Word count ≤ 10 | ✓ |
| Matches pattern: `typo`, `spelling`, `rename X to Y`, `change X to Y`, `replace X with Y`, `fix (a/the)? (typo/bug/error)`, `add missing`, `remove [word]`, `correct` | ✓ |

Both must hold — either failing: **substantive**. Trivial edits: apply inline with Edit tool — no agent spawn.

**Step skip rules**:

- **Perm operations**: skip Steps 2, 3, 5, 6, 7, 8, 9 — go Step 1 → Step 4 → Step 10
- **Hook operations**: skip Steps 2, 3, 6 (no color inventory, no MEMORY.md roster entry, no README table row); in Steps 5 and 7 skip cross-ref propagation (hook filenames not referenced from agent/skill markdown) — go Step 1 → Step 4 → Step 9 → Step 10
- **Content-edit operations**: skip Step 2 (entity already exists); skip Step 3 color inventory (no create); in Steps 5–7 only update cross-refs and README if name or description changed. Step 6 count: only update if name added or removed — content-only edits do not change agent/skill count.
- **Trivial content-edits**: additionally skip Steps 6–7 (no roster/description change possible); proceed Step 1 → Step 4 → Step 8 → Step 10

## Step 2: Overlap review (create only)

Before creating, check whether existing agents/skills already cover requested functionality:

1. Read descriptions of all existing agents (use `Read(file_path=..., limit=3)` on each `.md` in agents/) and skills (use `Read(file_path=..., limit=3)` on each `SKILL.md`)
2. Compare new description against each existing — look for domain overlap, similar workflows, redundant scope
3. Present findings:
   - **No overlap**: proceed to Step 3
   - **Partial overlap**: use `AskUserQuestion` with the findings in its question text (not reply text before the call) — the overlapping agent/skill and the coverage vs what new one adds: "Extend existing (Recommended)" / "Proceed" / "Abort"
   - **Strong overlap**: recommend against creation — suggest using or extending existing agent/skill

Skip for `update`, `delete`, perm operations.

## Step 3: Inventory current state

Snapshot current roster for later comparison. Steps 2 and 3 are independent reads — issue Glob calls for both in the same response.

Use Glob (pattern `agents/*.md`, path `.claude/`) for agents and Glob (pattern `skills/*/`, path `.claude/`) for skills. Use Grep (pattern `^color:`, glob `agents/*.md`, path `.claude/`, output mode `content`) to collect colors in use.

Extract names inline from Glob results — strip `.claude/agents/` prefix and `.md` suffix for agents; strip `.claude/skills/` prefix and trailing `/` for skills; strip `.claude/rules/` prefix and `.md` suffix for rules. Sort alphabetically when building roster string.

## Step 4: Execute operation

Load the mode file for `MODE` (`<MODE_FILE>` from the table), then execute the `### Mode:` section matching the resolved entity type — each file holds every type variant of its mode. Then continue per the Step 1 skip rules.

| `MODE` | Mode file |
| -- | -- |
| `create` | `create.md` |
| `rename` | `rename.md` |
| `content-edit` | `content-edit.md` |
| `delete` | `delete.md` |
| `add-perm`, `remove-perm` | `perm.md` |

```bash
MODE_FILE='<MODE_FILE>'
case "$MODE_FILE" in *'<'*'>'*|"") echo "! BLOCKED — <MODE_FILE> not substituted (create.md | rename.md | content-edit.md | delete.md | perm.md)"; exit 1;; esac
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/load_mode.py" manage modes "$MODE_FILE" --fallback-source  # timeout: 5000
```

## Step 5: Propagate cross-references

Search all `.claude/` markdown files for changed name and update references:

Use Grep to find all references:

- Pattern `<name>`, glob `agents/*.md`, path `.claude/`, output mode `content`
- Pattern `<name>`, glob `skills/*/SKILL.md`, path `.claude/`, output mode `content`
- Pattern `<name>`, glob `rules/*.md`, path `.claude/`, output mode `content`
- Pattern `<name>`, file `.claude/CLAUDE.md`, output mode `content`
- Pattern `<name>`, file `README.md`, output mode `content`

**For update (rename):** Count files grep returns. **≤3 files**: apply inline with Edit tool. **>3 files**: spawn **foundry:curator** subagent. For hook renames: also update hook entry in `.claude/settings.json` `hooks` array if hook filename referenced there by path.

```text
Apply these cross-reference updates (<old-name> → <new-name>):
<list each file path with the required substitution>
Use the Edit tool for each file (replace_all: true where appropriate).
Return ONLY: {"status":"done","files_updated":N}
```

**For delete:** Review each reference. Deleted name in:

- Cross-ref suggestion — remove or replace with closest alternative
- Inventory list — remove entry
- Workflow spawn directive — flag for manual review

**For create:** No cross-ref propagation needed.

**For content-edit:** Run propagation only if entity's `description:` frontmatter changed — propagate new description to any MEMORY.md or README summary lines that quote it. Skip if only internal content changed.

**Severity/priority labeling** — for every cross-reference decision above (rename fix, delete flag, content-edit propagate-or-skip), state a priority: **high** (genuine reference — invocation, routing, or discovery breaks if missed) or **low** (cosmetic/consistency-only, e.g. mention inside a code example or an unrelated similarly-named entity). Carry priority into Step 10 report (Files Changed table / Rename Occurrence Validation buckets).

### Rename occurrence validation (rename mode only)

```bash
# loads: rename-validation.md
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/load_mode.py" manage modes rename-validation.md --fallback-source  # timeout: 5000
```

Execute the mode loaded above.

## Step 6: Update MEMORY.md roster (auto-memory)

MEMORY.md is Claude Code's auto-memory file — **not** stored under `.claude/`. Injected into conversation context at session start. Absolute path appears near top of system prompt (e.g. `~/.claude/projects/.../memory/MEMORY.md`). Use that absolute path with Edit tool. System prompt parsing fails or path absent: fall back to:

```bash
MEMORY_FALLBACK="$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/resolve_memory_dir.py" "$(git rev-parse --show-toplevel 2>/dev/null || pwd)" 2>/dev/null)/MEMORY.md"
[ -f "$MEMORY_FALLBACK" ] && echo "Fallback MEMORY.md: $MEMORY_FALLBACK" || echo "MEMORY.md not found at fallback path — skip roster update"  # timeout: 5000
```

Regenerate inventory lines from disk:

Use Glob (`agents/*.md`, path `.claude/`) for agents, Glob (`skills/*/`, path `.claude/`) for skills, Glob (`rules/*.md`, path `.claude/`) for rules. Extract names inline from returned paths (strip path prefix and `.md`/trailing-`/` suffix), join as comma-separated string.

Use Edit tool with **absolute auto-memory path** to update these roster lines in MEMORY.md:

- `- Agents: doc-scribe, foundry:sw-engineer, ...`
- `- Skills: review, research, ...`
- `- Rules (N): artifact-lifecycle, ...` (update count N when rules created or deleted)

**For content-edit:** Skip if only internal content changed; update only if description changed.

## Step 7: Update README.md

**`README.md` (project root):**

- **create agent**: add row to `### Agents` table — columns: `| **name** | Short tagline | Key capabilities |`
- **create skill**: add row to `### Skills` table — columns: `` | **name** | `/name` | Description | ``
- **update (rename)**: find and replace old name in table row
- **delete**: remove row for deleted name

**`.claude/README.md` (config README) — Rules table only:**

- **create rule**: add row to Rules reference table — columns: `| rule-file | Applies to | What it governs |`
- **update rule (rename)**: replace old name in Rules table row
- **update rule (content-edit)**: update "What it governs" column if rule's description changed
- **delete rule**: remove row for deleted rule

Keep descriptions concise (one line), consistent in tone with surrounding rows. Don't add/remove table columns.

**For content-edit (agent/skill):** Update README if description OR model field changed. Model changes update the Model column only; description changes update the description column.

## Step 8: Verify integrity

Confirm no broken references remain:

Use Grep (pattern `[a-z]+:[a-z]+(-[a-z]+)*` to find cross-plugin references, or `See [a-z-]+ agent` for cross-references, glob `{agents/*.md,skills/*/SKILL.md}`, path `.claude/`, output mode `content`). Avoid broad kebab-case patterns — they match code examples, produce false positives.

Use Glob (`agents/*.md`, path `.claude/`) and Glob (`skills/*/`, path `.claude/`) for on-disk inventory; extract names inline. Use Grep to search for changed name and confirm:

- **Update (rename)**: zero unresolved genuine references to old name; documented false positives (accepted by user in Step 5 validation) may remain and must be listed in Step 10
- **Delete**: zero hits for deleted name (or flagged references noted)
- **Create**: new file exists with valid structure
- **Content-edit**: target file has valid structure (XML tag balance for agents/skills; YAML frontmatter for rules)

Add rules to on-disk inventory check: Glob (`rules/*.md`, path `.claude/`), extract names inline.

For **create** and **update (rename)**: verify tool efficiency — cross-check agent/skill's declared tools (`tools:` or `allowed-tools:`) against tool names in workflow body. Declared tool not referenced anywhere: flag as cleanup candidate in Step 10 report (report only — don't block operation).

## Step 9: Audit and calibrate

`/foundry:audit` and `/foundry:calibrate` are `disable-model-invocation: true` — this skill cannot invoke them. Validation runs through the direct `foundry:curator` spawn below; both skills are printed as user-run commands. **Skip the curator spawn and the printed audit command if invoked with `--skip-audit` or if current `manage` operation runs inside audit-initiated fix session** — outer audit covers it.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _SKIP_FILE < "${TMPDIR:-/tmp}/manage-skip-audit-path-${CSID}" 2>/dev/null || _SKIP_FILE="${TMPDIR:-/tmp}/manage-skip-audit-${CSID}"
IFS= read -r SKIP_AUDIT < "$_SKIP_FILE" 2>/dev/null || SKIP_AUDIT="false"  # reload (Check 43)
[[ "$SKIP_AUDIT" == "true" ]] && { echo "[--skip-audit] skipping Step 9 audit"; }
```

For targeted check of only affected file, spawn **foundry:curator** directly:

- For `create`: audit new file for structural completeness, cross-ref validity, content quality
- For `update`: audit renamed file, verify no stale references remain
- For `delete`: audit remaining files for broken references to deleted name

Include audit findings in final report. Don't proceed to sync if any `critical` findings remain. Then print as a REQUIRED user-run step: `/foundry:audit --skip-gate` (requires `foundry` plugin) — full-sweep validation without the interactive follow-up gate.

**Calibration** — after audit passes, print `/foundry:calibrate <name>` as a user-run command (requires `foundry` plugin), never run it here. **Mandatory only when the edit changes the routing surface** — an agent/skill `description:` field, or a `TRIGGER`/`SKIP`/`NOT-for` line. For pure body/prose edits leaving routing surface untouched (workflow steps, examples, `<notes>`, wording polish), calibration is **optional** — suggest it, don't force it. Routing accuracy is unaffected by non-routing edits, so the 10–30 min run rarely pays off; skipping keeps small polishes cheap.

Then, **only when routing surface changed**, also print `/foundry:calibrate routing --fast` as a user-run command to confirm overall routing accuracy unaffected (requires `foundry` plugin). Skip this for pure body/prose edits.

Skip calibration entirely for: trivial edits, renames, deletes, rule operations, perm operations.

## Step 10: Summary report

Report outcome fields below only — no Step 1…Step 10 execution trace; skipped steps and gates show only as the `n/a` values in their fields.

- **Operation**: what was done (create/update/delete + type + name, or add/remove perm + rule)
- **Files Changed**: table of file paths and actions (created/renamed/deleted/cross-ref updated/appended/removed)
- **Cross-References**: count of files updated, broken refs cleaned (n/a for perm operations)
- **Rename Occurrence Validation** (rename mode only): three buckets — **Fixed** (N genuine references updated, list files) · **False positives** (N skipped, list file + one-line reason each) · **User-resolved** (N ambiguous, list file + user decision); if any genuine references could not be cleanly resolved, flag explicitly as requiring manual review
- **Current Roster**: agents (N) and skills (N) with comma-separated names (n/a for perm operations)
- **Audit Result**: audit findings (pass / issues found) (n/a for perm operations)
- **Calibration Result**: suggested command(s) printed in Step 9, not run here (n/a for trivial edits, renames, deletes, perms, and pure body/prose edits that left the routing surface untouched — note calibration was suggested-but-skipped in that case)
- **Follow-up**: perm ops → confirm both `settings.json` and `permissions-guide.md` updated; project `.claude/settings.json` applies at once, the plugin's `permissions-allow.json` source edit reaches `~/.claude/` only after plugin update/reinstall followed by `/foundry:setup`

End response with `## Confidence` block per CLAUDE.md output standards.

**Challenger review gate** — after emitting summary report and Confidence block, invoke `AskUserQuestion` to offer adversarial review of just-completed changes. Skip this gate entirely when: `MODE` is `delete`, `add-perm`, or `remove-perm`; OR `EDIT_TRIVIAL=true`; OR `MODE` is `rename` (structural change only, no content to challenge). Gate is mandatory for: `create` (agent, skill, rule), non-trivial `content-edit` (agent, skill, rule).

```text
AskUserQuestion: "Run foundry:challenger to adversarially review the changes just made?"
  (a) Skip — done  [default]
  (b) Challenge — spawn foundry:challenger on modified file(s)
```

On **(b)**: spawn `foundry:challenger` — background like every spawn; in the same response Write `.temp/manage/agent-watch-manage.tsv` = `challenger\t.temp/manage-challenger-<YYYY-MM-DD>.md\t900`, end the turn, and on its notification run the `<constants>` deadline check block once (⏱ `timed_out` → report it in place of the findings):

```text
Agent(subagent_type="foundry:challenger", name="challenge-manage-edits", description="Challenge manage edits", prompt="Adversarially review the changes just made to <list modified file paths>. Challenge: correctness of design decisions, completeness, potential regressions, and whether the stated goal was achieved. Read-only. Write full findings to .temp/manage-challenger-<YYYY-MM-DD>.md using the Write tool. Return ONLY: {\"status\":\"done\",\"file\":\".temp/manage-challenger-<YYYY-MM-DD>.md\",\"findings\":N,\"confidence\":0.N}")
```

Print challenger's `findings` count and confidence; note any HIGH findings warranting a follow-up `/manage update` pass. On **(a)**: print `→ Done.`, stop.

**Cycle guard**: this challenger dispatch is from orchestrator (manage skill itself), not from a sub-agent — cycle detection in `<notes>` doesn't apply here. Do NOT spawn challenger from inside foundry:curator or foundry:sw-engineer sub-agents spawned by manage.

</workflow>

<notes>

- **Atomic updates**: write-before-delete prevents data loss on interruption; perm ops must update both `settings.json` and `permissions-guide.md`
- **settings.json format**: atomic jq edit via `bin/jq_write.py` (`perm.md`) — avoids fragile sed/awk on JSON
- **README.md tables**: agent/skill tables in project `README.md`; rules table in `.claude/README.md` — keep row format consistent with existing rows
- **No auto-edit for agent/skill/rule operations**: skill doesn't mutate settings.json for non-perm operations
- **Color pool**: AVAILABLE_COLORS is the candidate pool (in-use colors read live in Step 3); all in use → reuse the least-used color with a note
- **Inline bash / extraction gate + prose compression**: before writing any fenced bash block directly into a `.md` file (agent, skill, rule) via Edit/Write, apply two checks from `bin-authoring-guide.md`: (1) extraction gate — verdict MEDIUM or HIGH: write a `bin/` script instead; verdict LOW: inline is acceptable; (2) Prose over Code — `tokens(block) > tokens(equivalent prose/table/schema)` at identical precision: write prose/table instead. Exempt: examples, templates, exact-syntax blocks. Same rules enforced in spawn prompts to foundry:curator and foundry:sw-engineer (see `content-edit.md`). This note applies to orchestrator-level inline edits.
- **Cycle detection**: sub-tasks spawned by manage (foundry:sw-engineer, foundry:curator) must not invoke manage again. Circular dispatch — manage→sw-engineer→curator→manage — causes infinite loops. A sub-task needs manage capabilities: surface the need back to orchestrator; never chain manage from inside a manage-spawned sub-agent.
- Follow-up chains:
  - create or non-trivial update of agent/skill → print user-run `/foundry:audit --skip-gate` → `/foundry:calibrate <name>` (mandatory when routing surface changed) → `/foundry:calibrate routing --fast`
  - trivial update or rename or delete → print user-run `/foundry:audit --skip-gate` → `/foundry:calibrate routing --fast` (if description changed)
  - add/remove perm → confirm both files updated; plugin source `permissions-allow.json` edit reaches `~/.claude/` after plugin update/reinstall plus `/foundry:setup`

</notes>
