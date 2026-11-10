Calibration pipeline runner for `<TARGET>`. Complete all phases in sequence.

AB mode: `<AB_MODE>` — when `true`, also run `general-purpose` baseline on every problem and compute delta metrics. Local mode: `<LOCAL_MODE>` — when `true`, resolve target file from source tree (`plugins/`) first; see Pre-flight below.

Run dir: `.reports/calibrate/<TIMESTAMP>/<RUN_SLUG>/`

`<TARGET>` resolves the agent or skill file; `<TARGET_LABEL>` names this run in results and the ledger (the target, plus the mode for a mode-level domain, e.g. `/oss:analyse vitality`); `<RUN_SLUG>` is its directory name (e.g. `oss:analyse-vitality`). `<PROBLEM_SET_VERSION>` stamps every result line; ledger rows without it predate problem set 2.0 and are not comparable with it.

**Scratchpad discipline**: session scratchpad is shared across every concurrent calibration pipeline, not per-target. Write every bridge task file and any other intermediate you create inside your own run dir above — never a bare filename in the shared scratchpad. A generic name there (`codex-gen-task.txt`, etc.) gets silently overwritten by a sibling pipeline within seconds, caller then scores against wrong domain's problems with no error surfaced.

### Graceful-exit protocol

Before returning early for any reason — a failed check below, context running out, an unhandled error — write a minimal `result.jsonl` to run dir, so the orchestrator receives a signal even when phases are incomplete.

Write it on that exit path only, never at pipeline start: the orchestrator's deadline check (`agent_watch.py`) counts any non-empty `result.jsonl` written after the spawn as a finished deliverable, so a startup placeholder disables the deadline for the whole run. A pipeline that dies without writing is recorded `timed_out` by the orchestrator.

Write this line to `.reports/calibrate/<TIMESTAMP>/<RUN_SLUG>/result.jsonl` if file does not already exist:

`{"ts":"<TIMESTAMP>","target":"<TARGET_LABEL>","problem_set_version":"<PROBLEM_SET_VERSION>","verdict":"incomplete","mean_recall":null,"mean_confidence":null,"calibration_bias":null,"mean_f1":null,"severity_accuracy":null,"format_score":null,"problems":null,"scope_fp":null,"gaps":["pipeline exited before Phase 4 — re-run individually: /calibrate <TARGET> --fast"],"source_mode":null,"scoring":null,"scorer_agreement":null}`

Phase 4 overwrites this line with full results when it runs.

### Pre-flight — Codex availability

Check Codex availability once at pipeline start, set `CODEX_AVAILABLE` for all phases:

```bash
CODEX_STATUS=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/check_bridge.py" --status 2>/dev/null || echo "absent")
if [ "$CODEX_STATUS" = "available" ]; then CODEX_AVAILABLE=true; else CODEX_AVAILABLE=false; fi
echo "bridge@borda-ai-rig: $CODEX_STATUS"
```

The helper reads Claude's installed-plugin registry, marketplace cache, and enabled-plugin settings directly; doesn't require a nested `claude plugin list` call from a background agent.

Codex integration active only for `agents` and `skills` modes. Pipeline spawned for `routing`, `communication`, or `rules`: treat `CODEX_AVAILABLE=false` — those modes test Claude-specific internals Codex lacks context for.

Every Codex call in this pipeline (Phase 1a generation, Phase 3b scoring) runs in the foreground inside your turn: never a background job, never end the turn waiting on Codex — nothing re-invokes the pipeline, so a run that does idles until its deadline.

### Pre-flight — local file resolution

When `LOCAL_MODE=true`, resolve target file from source tree before Phase 2:

```bash
LOCAL_MODE=<LOCAL_MODE>
TARGET_FILE=""
if [ "$LOCAL_MODE" = "true" ]; then
  BARE=$(echo "<TARGET>" | sed 's|^/||')
  PLUGIN=$(echo "$BARE" | cut -d: -f1)
  NAME=$(echo "$BARE" | cut -d: -f2)
  # claude-skills/ holds skills of plugins that ship no agents (bridge, codemap-py)
  if [[ "<TARGET>" == /* ]]; then
    RELS="skills/$NAME/SKILL.md claude-skills/$NAME/SKILL.md"
  else
    RELS="agents/$NAME.md"
  fi
  CANDIDATE=""
  for REL in $RELS; do
    CANDIDATE="plugins/cc_$PLUGIN/$REL"
    [ -f "$CANDIDATE" ] || CANDIDATE="plugins/$PLUGIN/$REL"
    if [ ! -f "$CANDIDATE" ]; then
      MATCHES=$(find plugins -mindepth 3 -maxdepth 4 -path "*/$REL" 2>/dev/null)
      MATCH_COUNT=$(echo "$MATCHES" | grep -c .)
      if [ "$MATCH_COUNT" -eq 1 ]; then
        CANDIDATE="$MATCHES"
      elif [ "$MATCH_COUNT" -gt 1 ]; then
        CANDIDATE=$(echo "$MATCHES" | grep "/$PLUGIN[^/]*/" | head -1)
      else
        CANDIDATE=""
      fi
    fi
    [ -n "$CANDIDATE" ] && [ -f "$CANDIDATE" ] && break
  done
  if [ -n "$CANDIDATE" ] && [ -f "$CANDIDATE" ]; then
    TARGET_FILE="$CANDIDATE"
    echo "local mode: using $TARGET_FILE"
  else
    echo "! local mode: no source file resolved for '<TARGET>' (tried $RELS under plugins/cc_$PLUGIN, plugins/$PLUGIN, plugins/*) — aborting, not silently falling back to installed cache"
  fi
fi
```

Local-mode resolution failed (`$TARGET_FILE` still empty above): write graceful-exit `result.jsonl` line (see Graceful-exit protocol above) with `gaps` including `"local mode: no source file resolved for <TARGET>"`, skip all remaining phases, return compact JSON immediately (see Return value section) with `verdict:"incomplete"` — never a silent `exit 1`, never fall back to installed cache.

When `LOCAL_MODE=false` or source file not found: Phase 2 dispatches normally (agent by subagent_type, skill by cache/installed SKILL.md).

### Phase 1a — Generate problems (dual source)

**When CODEX_AVAILABLE=true**:

Split `<N>` in-scope problems between two generators. Claude always owns 1 out-of-scope problem. Both generators cover every tier, so the hard tiers stay cross-family.

| Pace | N_CLAUDE in-scope | N_CODEX in-scope | Scope (Claude) | Total |
| -- | -- | -- | -- | -- |
| fast (N=3) | 1 (medium or high) | 2 (low, medium) | 1 | 4 |
| full (N=10) | 5 (trivial, low, medium, high, extreme) | 5 (trivial, low, medium in one batch call; high and extreme one call each) | 1 | 11 |

**Problem schema v2** — both generators, one JSON object per problem:

- `problem_id`: kebab-slug; Codex ids start with `cx-`
- `difficulty`: `trivial` | `low` | `medium` | `high` | `extreme` (`scope` only for Claude's out-of-scope problem)
- `task_prompt`: instruction to the reviewer; never names or hints at an issue
- `input`: ONE string holding the code / config / content inline (no file paths); multi-part input = labelled sections inside that string, never a nested object or array
- `ground_truth`: array of atomic items `{claim, decisive, role, location, severity}`:
  - `claim`: one checkable statement, never two facts joined; a negative claim ("#812 is not the duplicate") is not a ground-truth item, it belongs in `near_misses`
  - `decisive`: the exact token a correct answer must state (`#811`, a symbol, a value), copied verbatim from `input`; `none` for a no-answer item (nothing fills `role`, e.g. no candidate is a duplicate)
  - `role`: what a correct answer says `decisive` is (`duplicate`, `root cause`, `off-by-one`, `band 🔴`, …)
  - `location`: function, section, line or item reference
  - `severity`: `critical` | `high` | `medium` | `low`, or `null` for a classification or relation item with no technical impact (excluded from severity accuracy)
- `near_misses`: `[{ref, wrong_role}]` — plausible distractors present in `input` (`ref` verbatim) that a careless answer asserts in `wrong_role`; never in `ground_truth`

A `decisive` never equals the value of a labelled input field (`Type: pull_request` → `pull_request`): copying a field is not finding an issue.

Difficulty tiers:

- **trivial**: single-line, obvious issue visible at glance; any reviewer finds immediately; full mode only
- **low**: isolated, obvious issue; single-function scope; domain expert finds immediately
- **medium**: requires reading 2-3 related functions or sections; non-obvious but unambiguous
- **high**: requires cross-function or cross-module reasoning; subtle but detectable by reading; `input` ≥ 6,000 characters
- **extreme**: intentionally adversarial or near-unsolvable from reading alone; `input` ≥ 12,000 characters — use ONE of these patterns:
  1. adversarial/misleading decoy: code looks like common anti-pattern but is correct in context (tests false-positive discipline); the decoy goes to `near_misses` and the problem also carries a separate real issue
  2. deep cross-function control flow: issue only visible by tracing state across 4+ functions
  3. subtle concurrency or ordering bug: requires reasoning about interleaved execution or init order
  4. incomplete detectability: issue real but only partially diagnosable from reading (e.g., depends on runtime config); ground_truth includes what IS detectable statically

Size floors are secondary to reasoning load — reach them with realistic material (more code paths, more comments, more candidate items), never padding. A domain entry's `scale_chars:` overrides both floors. A domain entry declaring `haystack:` requires high and extreme problems to carry that candidate pool (default: ≥ 15 candidate items, ≥ 3 of them `near_misses` sharing title, keywords or error with the target but differing in root cause).

Open shape: a domain entry declaring `open_shape: true` wants problems shaped like real reports, not closed puzzles. Render the candidate pool as at least 5 rows, one per item, as `#<N> | state: <s> | title: <t> | body: <b>` with each body a snippet of at most 300 characters, and make a duplicate answer one of those rows; put no code block inside the pool; quote no source of the project under analysis and plant no maintainer diagnosis that the input then refutes step by step. The link between the item and its answer is a shared symptom described in different words, with evidence that suggests rather than proves, and no phrase of four or more words copied between them. Phase 1b enforces the row format and count, snippet length, fence ban, pool membership of the duplicate and copied-phrase ban (`--open-shape`); real problems and the off-domain scope problem are exempt.

Extreme bar: a reviewer who reads once and reports what stands out must miss ≥1 ground-truth item. Every extreme problem needs ≥1 item whose evidence is split across two sections (visible only when both are read together) and ≥1 construct that looks defective but is correct (a decoy, listed in `near_misses`). Before returning, skim your own input: every ground-truth item visible on a first pass → harden it.

Signposting ban (medium and above): nothing in `input` or `task_prompt` names, labels or hints at an issue — no 'Risk:' headers, no comments flagging the flaw, no task_prompt wording that points at it.

Rules (both generators):

- 2-5 ground-truth items per problem; extreme problems may have 1-3 if the issue is inherently hard to detect
- Items must be unambiguous — a domain expert would confirm them
- No runtime-only-detectable issues outside the extreme `incomplete detectability` pattern

**Step 1 — Codex generates N_CODEX in-scope problems** (runs first; requires `bridge@borda-ai-rig`):

`bridge:advise`/`bridge:review` are read-only — they return their answer in the call's own envelope, never writing to the run dir. The public envelope omits `details`, so the payload must travel in `findings[]` (array of strings). Capture it from the call result, then write the target files yourself.

No independent work runs beside these calls: Step 2 starts only after they return.

Batch call — unscaled tiers (fast: low, medium; full: trivial, low, medium):

Skill(skill="bridge:advise", args="--timeout-seconds 240 --effort medium Generate \<K> synthetic calibration problems for domain: '<DOMAIN>', one per difficulty: \<TIERS>. Each problem is one JSON object following this problem schema v2, these difficulty tiers, the extreme bar, the signposting ban and these rules: \<copy the Problem schema v2, Difficulty tiers, size-floor, haystack, open-shape (when the domain declares it), Extreme bar, Signposting ban and Rules paragraphs above verbatim>. Do NOT include any out-of-scope problem. Return each problem as ONE entry of the envelope's `findings[]` array: a string holding one valid JSON object (no prose, no markdown fences, no trailing commas). `verdict` = one-line summary only. Do not attempt to write a file yourself.")

Full mode only — scaled tiers, one call per problem so a 12,000-character input never shares one call's output budget: the same args with `1` problem and difficulty `high`, then again with `extreme`, each at `--timeout-seconds 240` (worst case 576 s, under the foreground Bash cap).

Parse each `findings[]` string as one problem and write it to `.reports/calibrate/<TIMESTAMP>/<RUN_SLUG>/problems-codex-<problem_id>.json`, one Write per problem. An unparsable entry is dropped; Phase 1b's `floor(<N_CODEX> * 0.5)` validity threshold accounts for partial yield.

**Step 2 — Claude generates N_CLAUDE in-scope problems + 1 out-of-scope problem**:

Generate `<N_CLAUDE>` in-scope problems for domain `<DOMAIN>` (tiers per the table above), plus exactly 1 out-of-scope problem, in problem schema v2 under the tiers, floors, extreme bar, signposting ban and rules above.

- **Exactly 1 out-of-scope problem** (difficulty: `"scope"`): input clearly outside the target's domain (e.g., for `foundry:linting-expert`, a natural-language question; for `oss:cicd-steward`, a plain Python data script). `ground_truth: []`, `near_misses: []`. Correct response declines, redirects, or reports no findings. Any findings = false positives (scope failure).
- Write each problem with its own Write call to `.reports/calibrate/<TIMESTAMP>/<RUN_SLUG>/problems-claude-<problem_id>.json` — one JSON object per file. One array holding several scaled problems exceeds a safe single-response output.

**When CODEX_AVAILABLE=false**:

Claude generates all `<N>` in-scope problems (fast: low, medium, high; full: two per tier) + 1 out-of-scope using the rules above, one `problems-claude-<problem_id>.json` per problem. Skip Codex step.

### Phase 1b — Validate and merge problems

**Real fixtures** (`--full` only): when the domain entry names `real_fixtures: <path>` and that directory exists in the working tree, copy each of its `*.json` files into the run dir as `problems-real-<problem_id>.json`. They are frozen problems with a maintainer-recorded outcome; never in `--fast` (their count is part of the full-run deadline the orchestrator armed). Absent directory → skip silently.

Run the deterministic validator over every `problems-*.json` in the run dir (`<FLOORS_ARG>` = the validator flags the domain entry implies, else empty: `--floors high=<H>,extreme=<E>` from `scale_chars:`, `--derived-gt` from `derived_gt: true`, `--open-shape` from `open_shape: true`, `--boundary` from `boundary: true`):

```bash
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/calibrate_validate_problems.py" --run-dir ".reports/calibrate/<TIMESTAMP>/<RUN_SLUG>" <FLOORS_ARG>  # timeout: 15000
```

It checks schema v2, that every `decisive` and near-miss `ref` occurs in `input` (skipped with `--derived-gt`), the labelled-field leak, the size floors (real problems exempt), contradictory near-misses and the single scope problem; `--open-shape` adds the candidate-row checks above for synthetic problems, and `--boundary` requires each high and extreme problem to hold at least 3 answers one step from a different outcome, read from the derived-answer outputs the domain entry tells you to save in the run dir. Any failure rejects the whole problem — never one item, because a dropped item leaves its defect in `input` and turns a correct report into a false positive. It merges valid problems into `problems.json` (with `source`, colliding ids prefixed by source) and logs rejections to `problems-rejected.json`; its one-line JSON summary lists `rejected_ids`.

- The validator reads only files named `problems-<claude|codex|real>-<id>.json`; check that `valid + rejected` equals the number of problem files you wrote — a misnamed file is invisible to it, not rejected.
- Rejected Claude problem: regenerate it once, addressing the logged reasons, and rerun the validator.
- Rejected Codex problems are not retried. Fewer than `floor(<N_CODEX> * 0.5)` valid Codex problems → set `CODEX_GENERATION_FAILED=true` and Claude generates the missing tiers.
- Validator exit 1 (no valid problem): write the graceful-exit `result.jsonl` with gap `"no problem passed validation"` and return.

### Phase 1c — Difficulty probe (weak solver)

Skip this phase when the domain entry sets `weak_probe: off`: its answers come from a rubric the target ships, so a solver without the rubric cannot answer at all and one that finds it is the target. Then `weak_solver_recall_*` = null, `probe_downgraded` = 0 and the gap `"weak probe off: rubric-bound domain"` is recorded; hardness comes from that domain's own checks instead (e.g. `--boundary`).

A problem the generator calls hard is hard only if it defeats a solver without domain instructions. For every synthetic problem (`source` claude or codex) with difficulty `high` or `extreme`, spawn one `general-purpose` subagent with `model="haiku"`, all in a single response, prompt:

> `<task_prompt>`
>
> `<input>`
>
> Answer directly from the text above. Do not read, search or open any file and run no command: your only tool call is the Write below. Write your complete response to `.reports/calibrate/<TIMESTAMP>/<RUN_SLUG>/response-<problem_id>-weak.md` using the Write tool, then reply with exactly one line: `Wrote: <problem_id>`

The weak solver must see nothing but the problem: a probe that reads the target's shipped rubric, mode file or skill from the workspace measures the target's instructions, not the problem's difficulty, and its recall is void. When a weak response quotes or cites such a file, discard it and re-probe once.

Score each weak response with one Phase 3a Claude scorer (same prompt, precision gate included, response file `-weak`, output `score-<problem_id>-weak.json`), all scorers in a single response as each probe batch returns — never one at a time. Then per problem:

- weak recall < 1.0 → keep; record `probe_weak_recall` on the problem in `problems.json`
- weak recall = 1.0 → regenerate once by the same generator with the note "a solver without domain instructions found every ground-truth item; move the decisive evidence behind cross-section reasoning or a near-miss ruling; keep schema v2", rerun the validator, probe again
- still 1.0 → set `difficulty: "medium"` and `"probe": "downgraded"` on that problem

Real problems are not probed here: they were filtered against a weak solver before they were frozen, and each carries that result as `provenance.probe` (`{model, hit}`). Probe responses never enter Phase 3 scoring of the target.

### Phase 2 — Run target on each problem (parallel)

Spawn one `<TARGET>` subagent per problem using **Agent tool** — never via Bash or CLI. Issue ALL spawns in **single response** — no waiting between spawns — up to the effective tier's in-flight ceiling (haiku 20 · sonnet 8 · opus 5, `agent-spawn.md`); past it, spawn the rest as one more wave when the first returns. Same cap for Phases 2b and 3a. Never pass `name=` on nested spawns (Phases 2, 2b, 3a): a pipeline running as a named teammate gets named spawns refused, and nothing here addresses them by name.

Prompt for each subagent:

> `<task_prompt from that problem>`
>
> `<input from that problem>`
>
> End your response with a `## Confidence` block: **Score**: 0.NN (high >=0.9 / moderate 0.8-0.9 / low \<0.8) and **Gaps**: what limited thoroughness, each gap a bullet opening with its deduction `- (-0.NN) <gap>`, deductions summing to 1.0 - Score.
>
> Do not self-review or refine before answering — report your initial analysis directly.
>
> **Write your complete response** (including the Confidence block) to `.reports/calibrate/<TIMESTAMP>/<RUN_SLUG>/response-<problem_id>.md` using the Write tool. Then end your reply with exactly one line: `Wrote: <problem_id>`

**Context discipline**: subagents write to disk, return single-line acknowledgment. Pipeline agent must NOT accumulate their full analyses in context — scorers read from disk in Phase 3. `Wrote: <problem_id>` per agent = correct.

**Completion handling** — never a poll loop or a waiting tool (`ScheduleWakeup`, `ListAgents`, `Monitor`); each subagent's result arrives as its `Agent()` result or completion notification (`_FOUNDRY_SHARED/agent-spawn-protocol.md`). Each subagent returns: check for `response-<problem_id>.md`; empty or missing: mark that problem `{"timed_out": true}` in scores.json, proceed. Never block indefinitely on single response.

For **agent targets** when `LOCAL_MODE=true` and `TARGET_FILE` set: spawn `general-purpose` subagent — tests source tree definition rather than installed plugin. Write `.reports/calibrate/<TIMESTAMP>/<RUN_SLUG>/prompt-<problem_id>.md` = "You are an agent described by the following instructions:" + TARGET_FILE content + a tool-limit line (`Use only these tools: <frontmatter tools:>`, omitted when `tools:` absent) + the per-problem prompt above; spawn prompt = one line: `Read .reports/calibrate/<TIMESTAMP>/<RUN_SLUG>/prompt-<problem_id>.md and follow it exactly.` Frontmatter mapped onto the spawn: `model=` from `model:` (omit when absent or `inherit`), so tier changes are measured at the tier the agent ships with; `effort=` from `effort:` (omit when absent) — this template explicitly asks for it. Spawn schema lacks `effort`: add `"local mode: frontmatter effort not applied"` to `gaps`. `tools:` is enforced by the prompt line only, not the harness — add `"local mode: tools limited by prompt, not enforced"` to `gaps` when `tools:` present. When LOCAL_MODE=false or TARGET_FILE empty: spawn `Agent(subagent_type="<TARGET>")` normally.

For **skill targets** (target starts with `/`): spawn `general-purpose` subagent running the skill against the problem's inline fixture. Its context is the **fixture contract**, in this order, written to `.reports/calibrate/<TIMESTAMP>/<RUN_SLUG>/prompt-<problem_id>.md` (spawn prompt = one line: `Read <that file> and follow it exactly.`):

1. The complete SKILL.md — never frontmatter only: routing, out-of-scope rules and `<notes>` are part of the behaviour under test. When `LOCAL_MODE=true` and `TARGET_FILE` set, read it from `TARGET_FILE` (frontmatter `model:` handled as for agent targets; a domain entry's `solver_model:` overrides it, in both modes, as the spawn's `model=`); otherwise resolve, trying `skills/<NAME>/SKILL.md` then `claude-skills/<NAME>/SKILL.md` at each tier: `.claude/` → cache `~/.claude/plugins/cache/borda-ai-rig/<PLUGIN>/*/` (latest mtime) → `plugins/<PLUGIN>/`.
2. The mode file named by the domain entry's `mode:` key (from the same skill directory's `modes/`), when present.
3. Every file that mode file loads (its `loads:` comments and `_shared/` documents it names), so the target scores with the rubric it ships with.
4. This override line, verbatim: `Fixture contract: the input below replaces every live fetch. Do not call gh or any network command and do not spawn subagents; analyse the inline data, running only the skill's own local bin/ helpers on files you write from it.`
5. The per-problem prompt above (task_prompt, input, Confidence instruction, write-and-acknowledge).

### Phase 2b — Run general-purpose baseline (skip if AB_MODE is false)

Spawn one `general-purpose` subagent per problem using **identical prompt** as Phase 2 (same task_prompt + input + Confidence instruction) and, when the domain entry sets `solver_model:`, the same `model=` as Phase 2, so both arms run at one tier, plus same write-and-acknowledge suffix pointing to `response-<problem_id>-general.md`. Issue ALL spawns in **single response** — no waiting between spawns.

**Completion handling** — same as Phase 2 (`_FOUNDRY_SHARED/agent-spawn-protocol.md`): each subagent returns, check for `response-<problem_id>-general.md`; missing: proceed with partial baseline data.

### Phase 3a — Score responses via Claude scorers (parallel)

Spawn one `general-purpose` scorer subagent per problem using **Agent tool** — never via Bash or CLI. Issue ALL spawns in **single response** — no waiting between spawns.

Each scorer receives this prompt (substitute `<PROBLEM_ID>`, `<GROUND_TRUTH_JSON>`, `<NEAR_MISSES_JSON>` from the problem; `<TEMPLATE_SECTIONS_NOT_FP>`, `<SEV_LABEL>` and `<PRECISION_GATE>` from the domain entry, empty when absent; `<RUN_DIR>`, `<AB_MODE>`):

> Scoring agent responses against calibration ground truth.
>
> **Problem ID**: `<PROBLEM_ID>`
>
> **Ground truth** (JSON array — each item has `claim`, `decisive`, `role`, `location`, `severity`):
>
> ```text
> <GROUND_TRUTH_JSON>
> ```
>
> **Near misses** (JSON array of `{ref, wrong_role}` — distractors, never ground truth): `<NEAR_MISSES_JSON>`
>
> **Template sections whose extra entries are not false positives**: `<TEMPLATE_SECTIONS_NOT_FP>` (empty = none). **Severity label to read**: `<SEV_LABEL>` (empty = any severity or priority label on that finding).
>
> Read target response from `<RUN_DIR>/response-<PROBLEM_ID>.md`. \[If AB_MODE is true: also read `<RUN_DIR>/response-<PROBLEM_ID>-general.md`.\]
>
> For each ground-truth item: mark `true` only if the response states the item's `decisive` token in the item's `role` (the exact token or an unambiguous equivalent — same issue number, same symbol, same value). Topic overlap, or `decisive` listed under a different role, is `false`. An item with `decisive: none` is `true` when the response states that nothing fills `role` (e.g. "no duplicate among the candidates") and asserts no candidate in that role.
>
> Count false positives: (a) every near-miss `ref` the response asserts in its `wrong_role`; (b) every other reported finding with no matching ground-truth item — except entries inside the listed template sections, which count only under (a). Extract confidence from `## Confidence` block (use 0.5 if absent).
>
> **Precision gate**: `<PRECISION_GATE>` (empty = none). When `per-role`: ground-truth items sharing one `role` form a group; if the response names anything in that role that no item of the group carries, set every item of the group to `false` and count each extra name as a false positive — naming every candidate never scores.
>
> **For out-of-scope problems** (`ground_truth: []`): recall = N/A (skip from recall aggregate). Count all reported findings as false positives. If response declines or reports nothing, false_positives = 0 (correct scope discipline). Set severity_accuracy = N/A and format_score = N/A for this problem.
>
> **Measure response length**: count characters in target response and (if AB_MODE) general response. Token efficiency proxy — shorter = more focused.
>
> **Severity accuracy**: only for found items whose `severity` is not null. Compare the response's label (the `<SEV_LABEL>` field when given) with ground truth, allowing ±1 tier (tiers ordered: critical > high > medium > low — "critical" vs "high" is 1-tier miss; "critical" vs "low" is 3-tier miss). `severity_accuracy = correct_severity / found_with_severity_count` (N/A if 0). Orthogonal to recall — agent can find everything but mislabel severity. Also record `severity_assigned`: per ground-truth item in order, the label the response gave it (null if not found, unlabeled, or the item's severity is null) — Phase 3c compares these across scorers.
>
> **Format score**: for each found item, check whether response includes all three of: (a) location reference (line number, function name, or section), (b) severity or priority label (not required when the item's `severity` is null), (c) fix or action suggestion. `format_score = fully_structured_count / found_count` (N/A if found_count = 0). Measures actionability of findings, not just detection.
>
> Compute: `recall = found / total` (skip if total=0), `precision = found / (found + fp + 1e-9)`, `f1 = 2·r·p / (r+p+1e-9)`.
>
> Write following JSON (no prose, no markdown fences) to `<RUN_DIR>/score-<PROBLEM_ID>-claude.json` using Write tool: `{"problem_id":"<PROBLEM_ID>","found":[true/false,...],"false_positives":N,"confidence":0.N,"recall":0.N,"precision":0.N,"f1":0.N,"severity_accuracy":0.N,"severity_assigned":["high",null,...],"format_score":0.N,"target_chars":N,"scorer":"claude"}`
>
> \[If AB_MODE is true, also include before closing `}`: `,"recall_general":0.N,"precision_general":0.N,"f1_general":0.N,"confidence_general":0.N,"severity_accuracy_general":0.N,"severity_assigned_general":["high",null,...],"format_score_general":0.N,"general_chars":N`\]
>
> Then return ONLY one line: `Scored: <PROBLEM_ID>`

**Context discipline**: scorers write results to `score-<PROBLEM_ID>-claude.json`, return single-line acknowledgment (`Scored: <PROBLEM_ID>`). Do NOT accumulate inline JSON in pipeline context — Phase 3c reads from disk.

### Phase 3b — Score responses via Codex (skip when CODEX_AVAILABLE=false — requires `bridge@borda-ai-rig`)

For each problem, call the read-only Codex bridge skill. Run **sequentially** (not parallel — scoring writes shared result files).

Each call passes `--timeout-seconds 180` (worst case 432 s, under the foreground Bash cap), so the bridge skill itself stays foreground.

`bridge:review` is read-only — it returns its answer in the call envelope, never writing a file. The public envelope omits `details`, so the score travels as one JSON-object string in `findings[0]`. Call fails or `findings[0]` unparsable: retry once, then fall back to Claude's score alone for that problem (`scorer_mode: "single"`, per Phase 3c below) rather than looping.

Skill(skill="bridge:review", args="--timeout-seconds 180 --effort medium Score a calibration response against ground truth.

```text
Problem ID: \<PROBLEM_ID>
Ground truth (JSON array of {claim, decisive, role, location, severity}): \<GROUND_TRUTH_JSON>
Near misses (JSON array of {ref, wrong_role}): \<NEAR_MISSES_JSON>
Template sections whose extra entries are not false positives: \<TEMPLATE_SECTIONS_NOT_FP>
Severity label to read: \<SEV_LABEL>
Precision gate: \<PRECISION_GATE> (per-role = a response naming anything in a role that no ground-truth item of that role carries sets every item of that role to false; each extra name is a false positive)
```

Read response from: .reports/calibrate/<TIMESTAMP>/\<RUN_SLUG>/response-\<PROBLEM_ID>.md \[If AB_MODE is true: also read .reports/calibrate/<TIMESTAMP>/\<RUN_SLUG>/response-\<PROBLEM_ID>-general.md\]

For each ground truth item: mark true only if the response states the item's decisive token in the item's role (exact token or unambiguous equivalent); topic overlap or decisive under a different role is false. decisive none: true when the response states nothing fills role and asserts no candidate in it. False positives: each near-miss ref asserted in its wrong_role, plus each other reported finding with no ground truth match except entries inside the listed template sections. Extract confidence from ## Confidence block (use 0.5 if absent).

For out-of-scope problems (ground_truth is []): set recall=null, all reported findings are FPs, set severity_accuracy=null, format_score=null.

```text
Severity accuracy: for found items with non-null severity only, compare the response's label (the named severity label when given), allowing +-1 tier; tiers: critical>high>medium>low.
Format score: for found items, check for all three of: location reference, severity label (not required when the item's severity is null), fix suggestion.
```

Compute: recall=found/total (null if total=0), precision=found/(found+fp+1e-9), f1=2*r*p/(r+p+1e-9).

Also record `severity_assigned`: per ground-truth item in order, the severity label the response gave it (null if not found, unlabeled, or the item's severity is null).

Return ONLY this JSON as the single string in `findings[0]` (no prose, no markdown fences, no trailing commas), and do not attempt to write a file yourself: {"problem_id":"\<PROBLEM_ID>","found":[true/false,...],"false_positives":N,"confidence":0.N,"recall":0.N,"precision":0.N,"f1":0.N,"severity_accuracy":0.N,"severity_assigned":["high",null,...],"format_score":0.N,"scorer":"codex"} \[If AB_MODE is true, append before closing }: ,"recall_general":0.N,"confidence_general":0.N,"precision_general":0.N,"f1_general":0.N,"severity_accuracy_general":0.N,"severity_assigned_general":["high",null,...],"format_score_general":0.N\]")

Substitute `<PROBLEM_ID>`, `<GROUND_TRUTH_JSON>`, `<NEAR_MISSES_JSON>`, `<TEMPLATE_SECTIONS_NOT_FP>`, `<SEV_LABEL>` and `<PRECISION_GATE>` per problem, as for Phase 3a. Parse `findings[0]` from the call's return value, write it to `.reports/calibrate/<TIMESTAMP>/<RUN_SLUG>/score-<PROBLEM_ID>-codex.json` yourself. Returned payload missing or unparsable after one retry: set `scorer_mode: "single"` for that problem — Phase 3c uses Claude's score only.

### Phase 3c — Consensus merge

For each problem, read `score-<PROBLEM_ID>-claude.json` (Phase 3a output) and `score-<PROBLEM_ID>-codex.json` (Phase 3b output) from `<RUN_DIR>` and merge:

**When both scores present**:

- `found[]` — per-issue boolean: both agree → use; disagree → Claude's value (51% tiebreak)
- `false_positives` — Claude's count wins on disagreement
- `recall`, `precision`, `f1` — recomputed from consensus `found[]` and consensus `false_positives`
- `severity_accuracy` — recomputed per consensus-found issue from both scorers' `severity_assigned[i]` (scorer lacking the field: use its own `severity_accuracy`, no dispute check):
  - Both agree → use
  - Disagree within 1 tier → use harsher (more conservative) severity
  - Disagree by >1 tier → mark that issue as `severity_disputed`; exclude from `severity_accuracy` aggregate
- `format_score` — weighted average: 0.51 × Claude + 0.49 × Codex
- `confidence` — unchanged (from target agent's response, not scorers)
- `scorer_agreement` = (issues where both scorers agreed on found/not-found) / total_issues; N/A for scope problems
- `scorer_mode` = `"dual"`

**When only Claude score present** (Codex unavailable or failed for this problem):

Use Claude score directly; `scorer_agreement` = null; `scorer_mode` = `"single"`

**A/B mode**: apply same consensus logic independently to general-purpose baseline scores.

Write all merged scores to `.reports/calibrate/<TIMESTAMP>/<RUN_SLUG>/scores.json` as JSON array. Each entry includes `"source"` (from problems.json: `"claude"`/`"codex"`), `"scorer_mode"`, `"scorer_agreement"`, and `"severity_disputed_count"` (count of `severity_disputed` issues for this problem).

### Phase 4 — Aggregate, write report and result

Compute aggregates (exclude out-of-scope problem from recall/F1/severity/format averages; include in FP count):

- `mean_recall` = mean of `recall` values for in-scope, **non-extreme** problems only (trivial through high included; extreme excluded — reported separately as `extreme_recall`)
- `extreme_recall` = mean of `recall` values for extreme problems only (null if none present); partial performance (0.4–0.7) informative, not alarming; `extreme_recall` ≥ 0.95 with ≥2 extreme problems → add gap `"extreme tier saturated (extreme_recall <X>): problems did not discriminate"`; with fewer → gap `"extreme tier single-problem signal (extreme_recall <X>)"`
- `mean_confidence` = mean of `confidence` over the same problems as `mean_recall` (in-scope, non-extreme) — mixing in extreme/scope problems whose recall is excluded biases `calibration_bias` toward overconfident
- `calibration_bias` = `mean_confidence − mean_recall` (same problem set; extreme problems don't affect verdict)
- `mean_f1` = mean of `f1` values for in-scope, non-extreme problems only
- `scope_fp` = false_positives from out-of-scope problem (0 = correct discipline, >0 = scope failure)
- `mean_severity_accuracy` = mean of `severity_accuracy` for in-scope problems with found_count > 0 (exclude `severity_disputed` issues from numerator and denominator; extreme problems included if found_count > 0)
- `mean_format_score` = mean of `format_score` for in-scope problems with found_count > 0
- `token_ratio` = mean(target_chars) / mean(general_chars) across all problems — if AB_MODE, else omit
- Recall by difficulty: `recall_trivial`, `recall_low`, `recall_medium`, `recall_high`, `recall_extreme` (omit if 0 problems at that level)
- Real problems (`source: "real"`) are excluded from every aggregate above: `recall_real` = mean recall over them (null if none); `real_weak_hit_rate` = fraction of real problems whose `provenance.probe.hit` is true, over those carrying a probe record (null if none); `real_fp` = total false positives on real problems. Without `verdict_tier: real` they are reported beside the verdict, never instead of it
- Domain entry `verdict_tier: real`: the verdict reads the real tier instead — `mean_recall` and `mean_confidence` are taken over real problems, and the synthetic non-extreme values move to `synthetic_recall` / `synthetic_confidence`, still reported; fewer than 5 real problems present → keep the synthetic basis and add gap `"real tier too small for the verdict (<N> problems)"`; `recall_real ≥ 0.95` → gap `"real tier saturated (recall_real <X>)"`
- `weak_solver_recall_high`, `weak_solver_recall_extreme` = mean `probe_weak_recall` over the probed problems still at that tier (null if none); `probe_downgraded` = count of problems Phase 1c moved to medium
- Discrimination gap: `recall_high ≥ 0.95` and `weak_solver_recall_high > recall_high − 0.15` → gap `"high tier does not discriminate (target <X>, weak solver <Y>)"`; same rule for extreme

**Additional aggregates (populate when applicable; use null when not)**:

- `mean_scorer_agreement` = mean `scorer_agreement` across dual-scored problems (null if all single-scored)
- `severity_disputed_count` = total issues flagged `severity_disputed` across all problems
- `codex_problems_pct` = fraction of in-scope problems with `source: "codex"` (0.0 if claude-only)
- `recall_claude_problems` = mean recall on in-scope problems where `source: "claude"` (null if none)
- `recall_codex_problems` = mean recall on in-scope problems where `source: "codex"` (null if none)
- `generator_recall_delta` = `recall_claude_problems − recall_codex_problems` (null if either is null)
- `source_mode` = `"dual"` if CODEX_AVAILABLE and generation succeeded, else `"claude-only"`
- `scoring` = `"dual"` if any problem dual-scored, else `"single"`
- `codex_generation_failed` = true if Codex generation attempted but failed, else false

Verdict:

- `|bias| < 0.10` → `calibrated`
- `0.10 ≤ |bias| ≤ 0.15` → `borderline`
- `bias > 0.15` → `overconfident`
- `bias < −0.15` → `underconfident`

Write tool refuses any file whose exact basename is `report.md` from a subagent context ("Subagents should return findings as text, not write report files") — use `benchmark-report.md` instead. Write full report to `.reports/calibrate/<TIMESTAMP>/<RUN_SLUG>/benchmark-report.md` using this structure:

```markdown
## Benchmark Report — <TARGET> — <date>
Mode: <MODE> | Problems: <N> (in-scope) + 1 (out-of-scope) | Total known issues: M
Source: dual (claude+codex) | Scorer: dual | Scorer agreement: X.XX [consistent ≥0.85 / moderate 0.70–0.85 / divergent ⚠ <0.70]
[OR: Source: claude-only | Scorer: single — Codex unavailable or generation failed]

### Per-Problem Results
| Problem ID | Source | Difficulty | Recall | Precision | SevAcc | Fmt  | Confidence | Cal. Δ | Agreement |
| ...
| <scope-id> | claude | scope      | —      | —         | —      | —    | —          | scope_fp=N | — |

*Recall: issues found / total. Precision: found / (found + FP). Source: which model generated the problem (claude/codex). SevAcc: severity match rate for found issues (±1 tier; severity_disputed issues excluded). Fmt: fraction of found issues with location + severity + fix. Cal. Δ: confidence − recall (negative = conservative). Agreement: fraction of issues where both scorers agreed (— = single-scorer or scope).*

### Aggregate
| Metric             | Value | Status |
| ...
| Severity accuracy  | X.XX  | high ≥0.80 / moderate 0.60–0.80 / low <0.60 |
| Format score       | X.XX  | high ≥0.80 / moderate 0.60–0.80 / low <0.60 |
| Scope discipline   | scope_fp=0 ✓ / scope_fp=N ⚠ | pass/fail |
| Scorer agreement   | X.XX  | consistent ≥0.85 ✓ / moderate 0.70–0.85 ~ / divergent ⚠ <0.70 |
| Disputed severities | N    | excluded from SevAcc (scorers disagreed >1 tier) |

Recall by difficulty: trivial=X.XX | low=X.XX | medium=X.XX | high=X.XX (omit levels with 0 problems)
Extreme recall: X.XX (extreme problems excluded from mean_recall and verdict — partial performance 0.4–0.7 is expected; ≥0.95 with ≥2 extreme problems → saturated, gap recorded)
Weak solver (Phase 1c): high=X.XX | extreme=X.XX | downgraded=N (a tier discriminates when target recall exceeds weak-solver recall by ≥ 0.15)
Real problems: recall_real=X.XX over N | frozen-time weak hit rate=X.XX | real FP=N (verdict basis when the domain sets verdict_tier: real; reported only otherwise)

### Recall by Problem Source (dual source mode only)
| Source | Problems | Mean Recall |
|--------|----------|-------------|
| claude | N | X.XX |
| codex  | N | X.XX |
| delta  | — | ±X.XX (+ = harder codex problems; − = codex problems easier) |

### A/B Comparison — specialized vs. general-purpose (AB mode only)
| Metric            | Specialized | General | Delta  | Verdict   |
|-------------------|-------------|---------|--------|-----------|
| Mean Recall       | X.XX        | X.XX    | ±X.XX  | significant ✓ / marginal ~ / baseline-ahead ⚠ / inconclusive ? / no-lift ⚠ |
| Mean F1           | X.XX        | X.XX    | ±X.XX  |           |
| Severity accuracy | X.XX        | X.XX    | ±X.XX  | better ✓ / similar ~ / worse ⚠ |
| Format score      | X.XX        | X.XX    | ±X.XX  | better ✓ / similar ~ / worse ⚠ |
| Token ratio       | X.XX        | 1.00    | ±X.XX  | concise ✓ / verbose ⚠ |
| Scope FP          | N           | N       | —      | pass/fail |

*ΔRecall: specialist recall − general recall. SevAcc: severity match rate (±1 tier). Fmt: actionability score. Token ratio: specialist chars / general chars (below 1.0 = more focused). Scope FP: findings on out-of-scope input (0 = correct discipline).*
Verdict, first match wins: `significant` (delta_recall or delta_f1 > 0.10) / `marginal` (either ≥ 0.05) / `baseline-ahead` (either ≤ −0.05 — general agent beat specialist) / `inconclusive` (both arms mean recall ≥ 0.95 — ceiling, problems cannot separate arms) / `no-lift` (otherwise — measured below ceiling, no difference). For mixed-sign results, a positive delta ≥ 0.05 takes precedence: report `significant` or `marginal` as matched, and explicitly describe the negative metric delta as a trade-off; the verdict does not imply both metrics improved.

### Systematic Gaps (missed in ≥2 problems)
...

### Improvement Signals
...
```

Write single-line JSONL result to `.reports/calibrate/<TIMESTAMP>/<RUN_SLUG>/result.jsonl`: (one line per pipeline run — orchestrating skill appends these across runs to `.notes/logs/calibrations.jsonl`)

`{"ts":"<TIMESTAMP>","target":"<TARGET_LABEL>","problem_set_version":"<PROBLEM_SET_VERSION>","mode":"<MODE>","mean_recall":0.N,"extreme_recall":0.N_or_null,"mean_confidence":0.N,"calibration_bias":0.N,"mean_f1":0.N,"severity_accuracy":0.N,"format_score":0.N,"problems":<N>,"scope_fp":N,"verdict":"...","gaps":["..."],"source_mode":"dual|claude-only","scoring":"dual|single","scorer_agreement":0.N_or_null,"recall_trivial":0.N_or_null,"recall_low":0.N_or_null,"recall_medium":0.N_or_null,"recall_high":0.N_or_null,"recall_extreme":0.N_or_null,"recall_claude_problems":0.N_or_null,"recall_codex_problems":0.N_or_null,"generator_recall_delta":0.N_or_null,"severity_disputed_count":N,"codex_generation_failed":false,"recall_real":0.N_or_null,"real_weak_hit_rate":0.N_or_null,"real_fp":N,"verdict_tier":"synthetic|real","synthetic_recall":0.N_or_null,"weak_solver_recall_high":0.N_or_null,"weak_solver_recall_extreme":0.N_or_null,"probe_downgraded":N}`

**If AB_MODE is true**, append these fields to same JSON line: `"delta_recall":0.N,"delta_f1":0.N,"delta_severity_accuracy":0.N,"delta_format_score":0.N,"token_ratio":0.N,"scope_fp_general":N,"ab_verdict":"significant|marginal|baseline-ahead|inconclusive|no-lift"`

### Phase 5 — Propose instruction edits

Determine target file path for curator proposals:

When `LOCAL_MODE=true` and `TARGET_FILE` set: use `TARGET_FILE` directly.

Otherwise resolve (first match wins):

- Agent: `.claude/agents/<NAME>.md` → `~/.claude/plugins/cache/borda-ai-rig/<PLUGIN>/*/agents/<NAME>.md` (latest mtime) → `plugins/<PLUGIN>/agents/<NAME>.md`
- Skill: `.claude/skills/<NAME>/SKILL.md` → `~/.claude/plugins/cache/borda-ai-rig/<PLUGIN>/*/skills/<NAME>/SKILL.md` (latest mtime) → `plugins/<PLUGIN>/skills/<NAME>/SKILL.md`; at each tier also try `claude-skills/<NAME>/SKILL.md` in place of `skills/<NAME>/SKILL.md`

(`<PLUGIN>` and `<NAME>` parsed from `<TARGET>` as in pre-flight: strip `/`, split on `:`)

Spawn **foundry:curator** subagent using **Agent tool** — never via Bash or CLI. Pass only **file path** and **report path** — do NOT paste file contents into prompt; foundry:curator reads files itself:

> Reviewing calibration benchmark result and proposing instruction improvements.
>
> **Files to read** (use Read tool on each):
>
> 1. Target file: `<AGENT_OR_SKILL_FILE_PATH>`
> 2. Benchmark report: `.reports/calibrate/<TIMESTAMP>/<RUN_SLUG>/benchmark-report.md` — focus on **Systematic Gaps** and **Improvement Signals** sections
>
> Propose specific, minimal instruction edits that directly address each systematic gap (issues missed in ≥2/N problems) and each false-positive pattern. Conservative: one targeted change per gap. Don't refactor sections unrelated to findings.
>
> If no actionable systematic gaps (target calibrated with recall ≥ 0.70 and no repeated misses), write: `## Proposed Changes — <TARGET>\n\nNo changes needed — target is calibrated.`
>
> Otherwise format each change as:
>
> ```
> ## Proposed Changes — <TARGET>
>
> ### Change 1: <gap name>
> **File**: `<file path>`
> **Section**: `<antipatterns-to-flag>` / `<workflow>` / `<notes>` / etc.
> **Current**: [exact verbatim text to replace; or "none" if inserting new content]
> **Proposed**: [exact replacement text]
> **Rationale**: one sentence — why this closes the gap
> ```

Write foundry:curator response verbatim to `.reports/calibrate/<TIMESTAMP>/<RUN_SLUG>/proposal.md`. Ask foundry:curator to end proposed changes with `## Confidence` block per CLAUDE.md output standards.

### Return value

**CRITICAL — context discipline**: return ONLY compact JSON line below. No prose, no report summary, no table, no Confidence block, no additional output. Every extra byte accumulates in orchestrator context, can cause synthesis hang. All report content already on disk from Phase 4.

Return **only** this compact JSON (no prose before or after):

`{"target":"<TARGET_LABEL>","problem_set_version":"<PROBLEM_SET_VERSION>","mean_recall":0.N,"extreme_recall":0.N_or_null,"mean_confidence":0.N,"calibration_bias":0.N,"mean_f1":0.N,"severity_accuracy":0.N,"format_score":0.N,"scope_fp":N,"verdict":"calibrated|borderline|overconfident|underconfident","gaps":["..."],"proposed_changes":N,"source_mode":"dual|claude-only","scoring":"dual|single","scorer_agreement":0.N_or_null,"generator_recall_delta":0.N_or_null,"recall_real":0.N_or_null,"weak_solver_recall_high":0.N_or_null,"weak_solver_recall_extreme":0.N_or_null}`

If AB_MODE is true, also include: `"delta_recall":0.N,"delta_f1":0.N,"delta_severity_accuracy":0.N,"delta_format_score":0.N,"token_ratio":0.N,"scope_fp_general":N,"ab_verdict":"significant|marginal|baseline-ahead|inconclusive|no-lift"`
