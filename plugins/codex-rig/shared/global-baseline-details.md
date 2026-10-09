# Global Baseline Details

Activity-scoped detail moved out of the always-loaded global `assets/AGENTS.md`. Each section binds exactly as if its text were inline there; the template names the trigger that requires reading it.

## Lossless instruction compression gate

Applies only to instruction files an LLM host loads (`AGENTS.md`, `CLAUDE.md`, rule, skill, or agent-definition files). Compression or structural reformatting of one is behavior-sensitive and must pass every gate before handoff:

1. Save a verified byte-exact pre-change backup in a directory outside active instruction-discovery paths; never overwrite an existing backup.
2. Compare the backup and result for complete semantic preservation: scope, actors, obligations, modal strength, exceptions, ordering, approval and stop conditions, thresholds, examples, and cross-file relationships must remain unambiguous.
3. Preserve headings, list hierarchy, fenced and inline code, commands, paths, URLs, identifiers, versions, numbers, environment variables, and other behavior-bearing literals exactly unless the task explicitly changes them.
4. Run the project's affected Markdown, instruction-contract, and calibration gates where it has them. Broad instruction-set changes also require an independent agent followability review against the pre-change backup.
5. Reject the compression and restore the pre-change file when any instruction is lost, weakened, broadened, made ambiguous, harder to navigate, or less reliably followed. An unresolved comparison difference blocks completion.

## Reasoning-progress escalation

Continues the template's reasoning-progress bullets; the trigger bullet that names `escalation_ledger.py` stays in the template.

- Keep user's primary goal and acceptance fixed. Auxiliary setup, review dispatch, report repair, and validators must serve a named unmet primary acceptance criterion or an explicit user request; omit unrelated work. A requested review/report is itself primary work.
- Known authorized fix → implement fix + regression before another review-preparation cycle. Agent-owned reviewer/receipt failure → diagnose once, attempt one bounded repair; failed repair stops that route, not safe primary work. Never rotate reviewer names, rebuild reports repeatedly, or ask user to debug agent setup. Another supported route needs cause-avoiding evidence and permitted retry. Required coverage still gates clean completion; missing permission, prerequisite, or protected-state decision may require user.
- Schema-3 ledger retains `primary_goal`; cycle declares `work_kind=primary|auxiliary`. Auxiliary cycle names unmet criterion in `required_for` and sets `material_progress=false`. Auxiliary validator success or new metadata never resets primary stall/recurrence counts; closure remains user's acceptance, not artifact readiness.
- Work cycle records objective, operation/hypothesis, observed output, next decision.
- Material progress = new falsifiable evidence, decision-changing scope/root-cause narrowing, acceptance-check status change, or user-directed decision; repeated equivalent actions, rewording, elapsed time, token count, confidence claims don't qualify.
- Closure condition = unchanged result ending workstream: passing acceptance check, resolved decision, or user-approved scope.
- Ledger records objective; closure condition; operations/hypotheses; outputs/evidence; why each attempt lacked progress/closure; current model/effort when observable; state changes; recurrence count.

1. Pause, request exactly one permitted higher-capability advisory pass: first supported reasoning-effort increase, else next valid model tier.
2. Advisor route valid only when observed sandbox `read-only`; diagnoses, proposes one bounded recovery action + stop condition, makes no state changes or acceptance claim.
3. Read-only advisory route unavailable/unverified → ask human for missing advisory-route decision; never claim enforced isolation. Keep that route stopped while continuing unrelated authorized work or already-permitted source-inspection alternative with its limitations disclosed.
4. Parent may run that one action.
5. Action makes no evidence-backed material primary progress and closure condition remains unmet → stop that workstream and ask human with ledger, advisory evidence, current hypotheses, rejected alternatives, one recommended next step with alternatives, and evidence or decision needed to resume. Explain which unaffected work can continue.
6. Evidence-backed material primary progress permits useful unfinished recovery to continue and resets unsuccessful-attempt sequence; auxiliary success never does. Never reset/weaken separate repeated-obstacle policy; Luna never escalates bounded support to Sol, and Astra requires a separate evidenced escalation.

## Runtime effort policy

- Normal parent, implementation, verification, data, performance, research, and adversarial roles use `gpt-6.1-sol`; parent, implementation, verification, and performance start at `medium`, while data, research, and adversarial challenge use `high`.

- Delegation, documentation, CI/CD, web evidence, OSS triage, static analysis, and curation use `gpt-6-luna`; static analysis and web evidence start at `medium`, the others at `high`.

- `security-auditor` and `solution-architect` use `gpt-6.1-sol` at `high`, only after explicit user request or agent selection; both remain read-only advisory passes.

- Historical GPT-5.6 routing evidence remains archived, not proof of GPT-6 quality or cost. Astra has no standing role assignment.

- `medium`: normal coding, verification, performance analysis, linting, and web-evidence work.

- `high`: challenge, deep review, data/research method, coordination, documentation, CI, OSS triage, curation, architecture, and security work.

## Markdown authoring

Structure Markdown for scanning and correct execution, not from line length alone. When one paragraph combines multiple actions, conditions, actors, statuses, exceptions, or decision branches, use the smallest fitting structure:

- Parallel obligations or independently checkable facts → bullets.
- Ordered actions, recovery paths, or state transitions → numbered lists.
- Ordered sub-steps nested under a numbered item → letters, written as bullets with a letter label (`- a. …`, `- b. …`), so references read `2b`, never `2.2`; CommonMark has no lettered list type.
- Compact closed mappings or comparisons with repeated fields → tables; keep long causal explanations out of table cells.
- Genuine notes, warnings, interpretation limits, or safety boundaries → blockquotes.
- Optional depth that would interrupt the main path → an existing or justified `<details>` block.
- Keep causal reasoning and cohesive rationale as prose.
- Do not convert paragraphs wholesale, add headings for every rule, or duplicate an existing navigation system.
- Keep headings concise and move detailed contracts below them.
- When reformatting behavior-sensitive agent, skill, setup, approval, or recovery instructions, preserve modal language, exact literals, ordering, and stop conditions; run the affected contract and calibration gates because formatting can change instruction salience even when the words remain similar.

## Multi-OS Executables

Applies when the project supports more than one OS — declared by a CI OS matrix, packaging classifiers, or project docs; a single-OS project may skip it. There, scripts, hooks, `bin/` entry points, and CI steps run on every supported OS (typically Linux, macOS, and native Windows), and a POSIX-only assumption is a defect to fix at the source, never a reason to skip a supported platform.

- `pathlib`; `Path(p).is_absolute()` not a leading-slash check; `PurePath(p).as_posix()` before hashing, serializing, or comparing a path — native separators change the digest.
- POSIX-absolute literals are not portable fixtures: `/host/x` resolves to `D:\host\x` on Windows.
- Serialized telemetry or provenance paths are cross-host coordinates, not local paths: preserve their exact string; recognize declared POSIX and Windows absolute forms with `PurePosixPath` and `PureWindowsPath`; never convert them through host `Path` before exact comparison. Regressions must exercise both forms on every host.
- Byte-asserted or hashed writes use `newline="\n"` or bytes; text mode emits CRLF on Windows.
- Sanitized subprocess `env=` keeps `SystemRoot`, `SYSTEMROOT`, `COMSPEC`, `PATHEXT`, `TEMP`, `TMP` on win32, else the child Python aborts before running; temp dirs via `os.environ.get("TMPDIR") or tempfile.gettempdir()`, never `/tmp`.
- A workflow `run:` step invoking `.sh` needs explicit `shell: bash` — the Windows default shell dot-sources it and exits zero, a false green.
- Symlinks, file modes, and uid checks are capabilities: degrade in production code first.
- Skips are the last resort: never a blanket `skipif(sys.platform == "win32")`, always a capability probe skipping on `OSError`, with each surviving skip documented and re-audited.
- Test skips must be collection-time decorators (`pytest.mark.skipif`, `pytest.mark.skip`, or parametrized marks); never call `pytest.skip()` from a test or fixture body.
- Green macOS is absence of regression, not Windows support: prove Windows semantics with `PureWindowsPath` or `ntpath`, since monkeypatching `os.name` does not change `pathlib`.
- Recurrent defect guard: a test simulating another OS must explicitly supply every host-only API and constant it exercises instead of assuming the runner exports them. For absent surfaces such as `os.killpg` or `signal.SIGKILL`, install test doubles with `monkeypatch.setattr(..., raising=False)` and use `monkeypatch.delattr(..., raising=False)` in the regression to prove the missing-attribute case; keep the simulated branch running on every host rather than adding an OS skip.

## Codex Rig module documentation

- Describe module boundary, inputs/outputs or artifact paths, side effects (or deliberate lack), real CLI/import entrypoint, important failure/exit behavior, workflow/callers consuming it.
- Docstring must orient maintainer w/o first reading implementation; Codex Rig enforces 700-char min.
- Keep function docstrings focused on local contracts; module docstrings explain system role.
- Tests exempt from six-section format but still need concise module description.

## Testing details

- Parametrize cases when only inputs/expected outputs vary and arrange/action/assert use same behavioral oracle; keep distinct behaviors in named tests. Pytest parametrization IDs and shape:
  - Keep single, simple `str`, `bool`, `int`, and `float` cases bare with pytest's default IDs; descriptive IDs alone do not justify wrappers. Use concise semantic IDs for generated oversized strings whose default IDs would be unreadable.
  - Wrap every tuple case, including multi-argument rows, as `pytest.param(..., id="meaningful-case")`: pass row arguments separately; preserve a tuple-valued single argument as `pytest.param((...), id=...)`.
  - Use `pytest.param` with semantic IDs for functions, containers, and other opaque/unstable objects; retain case-specific `marks=`. IDs describe behavior or intent, never memory addresses or object hashes.
  - Never pass separate `ids=` to `pytest.mark.parametrize` — list, tuple, callable, or otherwise. Generated cases and mapping rows follow the same rules; attach each required ID to its case.
  - Remove optional trailing commas that force short lists or calls onto multiple lines, then run the project's pinned formatter hooks. Retain required tuple commas, comments, and formatter-restored wrapping; skip ambiguous edits. Preserve values, argument unpacking, order, duplicates, marks, and assertions.
- Pytest marker selection:
  - Use semantic markers. Apply markers directly to tests or homogeneous classes; never assign `pytestmark`, including lists of markers. Do not infer membership from paths, names, platforms, or mocked subprocesses. Ordinary tests may remain unmarked.
  - Reuse repeated collection-time `pytest.mark.skipif(...)` conditions as named decorators; preserve predicates and reasons. This does not authorize new skips or weaken the capability-probe policy in §Multi-OS Executables.
  - Register selectors in project and any shipped test configuration; validate with `--strict-markers`. Classify tests by their behavioral contract or execution requirements, never duration; use CI duration reports to investigate underperforming tests. Full CI remains unfiltered.
- Parallel runs (pytest-xdist): drop `-n` when the failure itself is what you are reading — xdist interleaves worker output, hides `-x` ordering, and breaks `--pdb`; reproduce a failure serially before diagnosing it. A test that passes serially and fails only under `-n` is a real defect, not an xdist artifact — usually shared machine-global state (a temp-dir sentinel without a session-unique suffix, a fixed port, a written file outside `tmp_path`). Fix the isolation; never pin that suite serial to hide it.
- Keep behavior-defining data + actions visible. Reuse meaningful local values arrange through assert; extract fixtures/helpers only when hiding irrelevant construction or genuinely shared infra, never scenario intent.
- Fixtures return ready-to-use concrete state or cohesive tuple of related state. Don't return callable factory unless fixture-managed lifecycle requires it; use ordinary helper function for configurable construction. Keep fixture deps minimal, unpack only values test needs, avoid aliases/forwarding helpers adding no meaning.
- Test public behavior. Mock only true external boundaries outside test's control, not system-under-test internals.
- Use smallest test surface proving behavior; don't add framework, global fixture, or config for one local case.

## Networked CLI and GitHub reads

- Keep shell network access blocked by default. Select `github-read` for a fresh session with `codex -c 'default_permissions="github-read"'` only when the consuming checkout defines that project-local profile or explicit setup or sync has installed the managed Codex-home profile. Plugin installation alone does not provide either profile in an unrelated project.
- Run audited GitHub data reads through installed `shared/github_read.py`, and PR collection through `shared/collect_pr.py`. Current runtime network access and required filesystem write grants permit them without a separate runtime read request; only unavailable required capability uses the five-field brief and external approval for the complete owning helper. The profile extends `:workspace`, enables the network proxy for `api.github.com` and `github.com`, and grants `.git` writes under workspace roots for all commands in the selected session. It controls destinations, not HTTP methods or executable identity; helper validation and the remote-mutation ban remain mandatory. A denial or stricter host restriction stops the read with a specific diagnostic; never retry by broadening permission.
- For those GitHub helpers, use the current session's effective permissions, not a stored default or profile definition. An active `github-read` profile supplies network evidence unless stricter runtime restrictions contradict it. When the profile name is hidden, explicit runtime network access enabled is sufficient to attempt the audited helper under existing destination policy; preserve explicit denied destinations. Verify required report, `.git`, and checkout paths are writable separately. With required grants, omit `sandbox_permissions` and `justification`, give no approval brief, and request no reusable rule. Missing profile identity or a failed lookup is not disabled access; never use `codex execpolicy list`, inspect credentials, or search other sessions to detect a profile. Required capability disabled, outside allowed write roots, or genuinely unknown uses the existing owning-command approval boundary only if runtime policy permits it; report uncertainty accurately. A `github-network` error alone is not an approval denial. Missing or unknown required capability with requests allowed requires the five-field brief and runtime approval for the complete owning helper before terminal reporting; continue after approval without requiring profile installation. An explicit denial or non-overridable restriction stops the attempt without a broadened retry. This changes no permission grants, profiles, or remote-mutation boundaries.
- Runnable plugin code blocks are preapproved recipes within the invoked workflow scope; required preparation and checks inherit that authorization. Forbidden-operation examples and unevaluated placeholders are not grants. Use existing grants or the configured native automatic approval reviewer for the complete populated command; no blanket shell, interpreter, or arbitrary gate-runner allowance.
- For other intentionally networked CLI, inspect the complete owning command's current network and filesystem grants, including nested subprocesses, dependency caches and hook environments. Existing grants or applicable preapproval use default execution without another request; network intent alone does not require escalation.
- Only a verified missing capability requires `sandbox_permissions="require_escalated"` with narrow justification from the first attempt; never enable persistent workspace network access, request broad interpreter prefix, or assume nested executable's approval covers its parent. Do not propose a timestamp-specific whole-command prefix for `run_gates.py`.
- This includes `kaggle`, Codex Git marketplace add/upgrade + owning sync wrapper, paid `codex exec`, and any networked CLI outside the audited GitHub-read boundary; web/browser/MCP/connector tools use their own permission path.
- Marketplace/plugin listing and `codex plugin add` from existing snapshot stay sandboxed.

## Sandboxed pytest runs

- Pytest outside the canonical `run_gates.py` gate follows packaged `shared/native-skill-contract.md` §Sandboxed Test Runs:
  - Targeted loop runs without `-n`/`--numprocesses`/`--dist` in command or pytest `addopts` add `-p no:xdist`, so plugins opening a localhost socket whenever xdist is installed stay inside sandbox.
  - Only interpreter-family reusable prefix permitted = repo's exact pinned test-runner prefix (e.g. `["<repo>/.venv/bin/python", "-m", "pytest"]`), requested once at scope selection only when selected work needs escalated pytest; never bare interpreter, `python -c`, or gate runner. Escalated pytest commands start with exactly that prefix — no `env VAR=...` or shell wrapper; pass env vars through tool's env field. Brief must state approved pytest runs unsandboxed, including repo `conftest.py`, for rest of session.
  - Canonical gate keeps configured test command; its escalation stays one-time for complete `run_gates.py` command without `prefix_rule`.
  - Loops run only changed-file targets from `shared/test_targets.py` (codemap-py test impact, else name/import heuristics); full suite runs once at canonical gate, never per finding or iteration.

## GitHub data reads

- GitHub data reads use `shared/github_read.py` only:
  - Treats `gh` as opaque local credential broker: never run `gh auth`, read token/keychain/account state, or retain GitHub CLI stdout/stderr on failure.
  - Permits only audited built-in view groups (`gist`, `issue`, `pr`, `project`, `release`, `repo`, `ruleset`, `run`, `workflow`), `gh api` GET requests, GraphQL queries, PR diffs, local `gh pr checkout`; rejects unlisted `gh * view` commands, remote mutation, browser-opening `--web`, non-GET REST calls, file-backed API fields, GraphQL mutations.
  - Use unauthenticated public `api.github.com` GET fallback only as final public-data fallback; private-only evidence fails closed.
  - GitHub Discussions use explicit read-only GraphQL query since `gh` has no `discussion view` command.
  - Codex Git marketplace add/upgrade = separate explicit lifecycle op, not GitHub data read.
- Keep `collect_pr.py` as only resource-specific GitHub collector unless another workflow demonstrably needs composite, validated evidence bundle or local-state operation.
  - Issues, releases, repositories, Discussions use `github_read.py` direct.
  - New collector needs written bundle contract, consumer workflow, regression tests; don't create parity wrappers around single read.

## Docstring style resolution

Before writing/changing docstrings:

- Inspect project for explicit style.
- Check project-local `AGENTS.md`, contributor docs, `pyproject.toml`, lint/doc settings like `pydocstyle` or `ruff`, Sphinx/MkDocs config.
- No explicit style configured → read nearby modules + tests, match dominant local style.

Fallback = 6-point Google/Napoleon style below. Use only when project doesn't define or clearly demonstrate other style.

- Public APIs need all relevant sections in selected project style.
- Types live in function signatures — never repeat in Args/Returns unless project style explicit does so.
- Internal helpers still need purpose docstring when new or material changed; keep concise unless args, return values, raised errors, examples need explicit explanation.
- Explanatory text about why function/class exists belongs in that definition docstring, not preceding inline comment.

```python
def compute_score(predictions: torch.Tensor, targets: torch.Tensor) -> torch.Tensor:
    """Compute element-wise accuracy score between predictions and targets.

    Applies softmax to predictions before comparison. Handles batch-size-1
    without broadcasting errors.

    Args:
        predictions: Raw logits, shape (B, C), in (-inf, +inf).
        targets: Class indices, shape (B,), in [0, C).

    Returns:
        Per-sample accuracy, shape (B,), in [0.0, 1.0].

    Raises:
        ValueError: If predictions and targets have incompatible batch dimensions.

    Example:
        >>> preds = torch.tensor([[2.0, 0.5], [0.1, 3.0]])
        >>> tgts = torch.tensor([0, 1])
        >>> compute_score(preds, tgts)
        tensor([1., 1.])
    """
```

## Delegation default mode

Use `delegation-lead` when task has multiple separable workstreams and routing across configured Luna and Sol roles expected to cut total cost or elapsed time after coordination overhead.

Stay in main agent when:

- Work not splittable into disjoint ownership or evidence axes
- Handoff would dup context parent already has
- Preparing, waiting, validating delegation costs more than direct execution
- Next action = single parent-owned acceptance or destructive-action decision

Use delegation lead when:

- 2+ independent domains, file sets, evidence searches, or verification commands can proceed w/o overlapping ownership
- Lower-cost registered Luna role can own bounded support work while the Sol parent retains behavior, architecture, security, and executable acceptance
- Parallel work likely cuts wall time w/o flooding every specialist w/ same context
- Task needs explicit routing ledger + consolidated handover

Parent agent responsibilities:

- Scope task, owned files, acceptance criteria before delegation
- Integrate subagent outputs into one coherent change
- Inspect delegation lead handover ledger, relevant diffs, verification evidence before accepting work
- Reject scope widening, unsupported completion claims, final acceptance transferred to support role
- Final judgment on conflicts, overlaps, release readiness

## Collaboration team patterns

- Architecture/public API changes: `solution-architect` + `sw-engineer` + `qa-specialist` + `doc-scribe`
- Security-sensitive features: `security-auditor` + `sw-engineer` + `qa-specialist`
- Data pipeline changes: `data-steward` + `sw-engineer` + `qa-specialist`
- Toolchain/CI quality changes: `cicd-steward` + `linting-expert` + `curator`
- External migration/release-note driven changes: `web-explorer` + `solution-architect` + `sw-engineer`
- Release readiness: `oss-shepherd` + `cicd-steward` + `doc-scribe` + `qa-specialist`
- Research-paper implementation: `scientist` + `solution-architect` + `sw-engineer` + `qa-specialist`
- High-risk plan validation: `challenger` + relevant domain specialist before implementation
- PR review-to-resolution: `code-review` with `scope=pr` writes report after collecting PR evidence, fetching target branch, checking out/updating PR locally. Then `code-remediate` with `mode=pr` re-collects online PR reviews, fetches latest target branch + PR branch, records clean PR/target implementation context plus merge-conflict risk before editing, triages each comment, fixes only valid selected findings in local code.

## Work handover patches

**Handing off:**

```bash
mkdir -p .codex/handover
git diff -- <owned-paths> > .codex/handover/<from>→<to>-$(date +%s).patch
```

Also include short text handoff covering:

- files touched
- intent of change
- verification performed
- open risks or questions

**Receiving:**

```bash
git apply .codex/handover/<patch-file>
```

Apply only if no discarding of local changes required.

Conflicts with existing work → resolve at parent-agent level, no cleaning tree.

**Naming convention:**

```text
<from-role>→<to-role>-<unix-timestamp>.patch
```

Examples: `sw-engineer→qa-specialist-1735000000.patch` · `linting-expert→claude-1735000001.patch`
