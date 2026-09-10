---
name: investigate
description: Investigate code debugging and root-cause narrowing; use measurable gates before fixes.
---

# Investigate

See the [fixed recurrence and root-cause policy](../../shared/native-skill-contract.md#recurrence-and-root-cause-policy) and [reasoning-progress escalation policy](../../shared/native-skill-contract.md#reasoning-progress-escalation) for repeated-obstacle handling; record and validate `reasoning-progress.json` before another cycle after escalation trigger.

Diagnosis-first loop for unclear failures: failing tests, tracebacks, regressions, surprising runtime behavior. Produce root-cause claim with evidence, falsification, rejected alternatives before any fix. Use `investigate` until root cause established; then hand off to `implement` or `code-remediate`.

## Input Schema

```json
{
  "symptom": "required failing command, traceback, runtime bug, CI failure, flaky behavior, or tool anomaly",
  "scope": "optional path/module/tool/CI run",
  "pace": "fast|full",
  "done_when": "one root cause is confirmed or the remaining uncertainty is explicit"
}
```

## Workflow

### 01: Create run directory

Run `create_run.py --skill investigate` per `../../shared/helper-cli-contract.md`.

### 02: Capture symptom and reproduction context

Write `<run-directory>/symptom.md` with:

- failing command or observed behavior
- expected behavior
- local vs CI vs external context
- first known bad time or commit, if known
- whether failure is deterministic, flaky, or unknown

### 03: Gather signals before forming hypotheses

Run `git log --oneline -10` and `python --version` as separate argv commands. Write their complete outputs to `<run-directory>/recent-commits.txt` and `<run-directory>/python-version.txt`; record either collection failure rather than treating empty file as successful evidence.

Inspect `python PLUGIN_ROOT/shared/collect_diff.py --help`, collect `working-tree` scope into `<run-directory>/baseline`; record collection failure, never treat as empty diff.

Add needed tool logs, CI excerpts, tracebacks, config, changed source. Absence of evidence ≠ evidence of absence.

**Structural context (optional)**: when `scope` names Python module/symbol, select one task-neutral route and probe codemap-py once: `python PLUGIN_ROOT/shared/codemap_adapter.py context --category implementation --query-kind <kind> [--target <qname>] --out <run-directory>/codemap-context.json`. Use `skip` when failure is localized and no structural fact is unresolved, matching single route (`central`, `callers`, `blast`, `dependencies`, `test-impact`, or `coupling`) for one unresolved fact, and `standard` for broad or unknown scope. Map direct, all, or production caller questions to `callers`; use `blast` only for explicitly transitive caller questions. An explicit user or tool request for structural evidence overrides `skip`. Per `../../shared/codemap-contract.md`, absence/incompatibility is non-fatal — continue with signals above. Persist result once here, before hypothesis ranking; step 05 specialist probes consume `<run-directory>/codemap-context.json`, never fresh query.

### 04: Rank hypotheses in `<run-directory>/hypotheses.md`

```markdown
| Rank | Hypothesis | Supporting evidence | Falsification check | Status |
| --- | --- | --- | --- | --- |
```

Include ≥3 plausible hypotheses unless failing command + code/log directly prove root cause.

### 05: Orchestrate specialist probes when hypotheses split by domain

Read and apply `../../shared/specialist-orchestration.md` only for multi-domain symptoms or useful parallel evidence; do not load it for narrow deterministic failure with one obvious hypothesis.

Write `<run-directory>/specialist-probes.md` before fan-out: role, hypothesis, context path, expected falsification signal, mode (`spawned`, `substituted`, `not_triggered`).

Recommended probe routing:

- `qa-specialist`: flaky tests, failing assertions, regression reproduction, missing edge-case evidence.
- `cicd-steward`: CI-only failure, matrix/cache/permission divergence, release workflow failures.
- `linting-expert`: ruff, mypy, pre-commit, tool version or suppression anomalies.
- `security-auditor`: only when user expressly requests Sol or selects that role for auth, secret handling, deserialization, dependency, or permission-related failures; return its bounded read-only evidence artifact to Terra parent/session for remediation and acceptance.
- `data-steward`: data split, leakage, augmentation, DataLoader, or reproducibility anomalies.
- `squeezer`: performance regressions, memory/OOM, throughput drops, GPU sync suspicion.
- `scientist`: metric instability, paper/method mismatch, experiment validity.
- `web-explorer`: volatile dependency or external API behavior.
- `challenger`: root-cause claim that would be damaging if wrong.

Each context pack: symptom slice, relevant logs/touched files/environment facts, exact falsification question. Specialists may request context; parent decides widening, consolidates outcomes, owns final root-cause claim.

### 06: Probe the top hypotheses

Use targeted probes confirming, ruling out, or narrowing one hypothesis at a time.

Diagnosis does not authorize source fixes. Parent owns probe execution and log persistence; read-only specialists return findings or concrete probe request under [read-only work and executable probes](../../shared/specialist-orchestration.md#read-only-work-and-executable-probes). A request includes hypothesis, exact command/code, working directory, inputs, expected falsifier, and anticipated side effects. Inspect executable probes before running; use isolated disposable inputs for necessary writes and retain runtime approval boundaries. No safe child route does not prevent permitted parent-serial investigation. Denied or unavailable execution remains inconclusive, never confirmed; parent-run evidence is not independent specialist conclusion. Hand source remediation off only after root-cause gate.

Each probe must have clear outcome:

- `confirmed`
- `ruled_out`
- `inconclusive`

Persist probe commands and outputs under `<run-directory>/probes/` or inline in `<run-directory>/probes.md`.

### 07: Run the anti-rationalization gate

A root-cause claim requires:

- supporting evidence from logs/code/commands
- one falsification check
- at least one rejected alternative
- explicit confidence

Low confidence: continue probing, no fix proposal.

Write `<run-directory>/root-cause.md` with:

- `Evidence`
- `Falsification`
- `Rejected Alternatives`
- `Confidence`

### 08: Run shared quality gates or targeted checks relevant to the failure

Inspect `python PLUGIN_ROOT/shared/run_gates.py --help`, run full/targeted gates needed to falsify hypotheses.

### 09: Decide gate result, write `result.candidate.json`, validate artifacts, and publish `.reports/codex/investigate/<timestamp>/result.json`

Follow `../../shared/helper-cli-contract.md` and authoritative help. Write with `INVESTIGATE_METADATA`, validate as skill `investigate`, and promote only validated candidate.

## Fail-Fast Rules

1. Missing symptom => fail.
2. No evidence collected before hypotheses => fail.
3. Root cause stated without falsification check => fail.
4. Workaround presented as root cause => fail.
5. Missing `root-cause.md` evidence, falsification, rejected alternatives, confidence => fail.
6. Broad multi-domain symptom without `specialist-probes.md` or explicit single-agent rationale => fail.
7. Result artifact validator failure => fail.
8. Result artifact missing => fail.

## Quality Gates

Required checks:

- `review`: hypothesis table, probe outcomes, rejected alternatives, and `git diff --check`.
- `artifact`: shared validator confirms investigation artifacts, gate logs, and result JSON shape.

Conditional checks:

- `tests`: failing or confirming reproduction command when available.
- `lint`, `format`, `types`: only when code/config changes are made as part of probe.

## Calibration Hooks

Update calibration when root-cause routing or workaround rejection changes:

- behavioral cases: symptom-first routing, rejected alternatives, low-confidence probe escalation, artifact validator bypass
- benchmark patterns: `investigate`

## Output Contract

Before writing result candidate, follow `../../shared/final-handoff-contract.md`: render and bind `final-handoff.json`, `final.md`, and `final-handoff.validation.json`; after both validators and promotion pass, emit `final.md` verbatim.

Use `../../shared/quality-gates.md`.

### Final chat

Final chat follows shared frame with `Next steps`. `Outcome`: root-cause status/remediation readiness. `Results`: exactly `Hypothesis | Evidence | Disposition | Next action`, one row/hypothesis. Include probes/falsifiers; unresolved hypotheses or blocked evidence name owner.

Minimum artifact payload template: `result-template.json`.
