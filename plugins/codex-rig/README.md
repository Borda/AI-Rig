# 🤖 Codex Rig — Native Codex Workflows and Specialist Roles

Codex Rig is OpenAI Codex product in [Borda's AI-Rig](https://github.com/Borda/AI-Rig). It packages 13 reusable workflow skills, one lifecycle-manager skill, 15 canonical specialist role cards, shared quality gates, calibration, and optional health hook as one Apache-2.0-licensed plugin.

Calibration measures instruction quality against synthetic cases. It is not evidence that any individual run is correct.

The package covers capabilities Codex can currently install and verify. It contains no MCP server and no native bundled agent registrations. Parallel work uses runtime blank agent with exact role card injected when that route is available; inline role pass is serial fallback. Persistent named-agent routing remains platform-blocked until Codex exposes verifiable custom-agent selector. The split schema, approval allowlist, synchronization gates, runtime evidence, telemetry, fallback, and promotion lifecycle are defined in [`ARCHITECTURE.md`](https://github.com/Borda/AI-Rig/blob/main/plugins/codex-rig/ARCHITECTURE.md).

> Current release: `0.17.0`. Codex Rig is peer product to foundry, oss, develop, research, and codemap-py—not copy of repository's `.codex/` configuration.

<details open>
<summary><strong>Navigation</strong></summary>

- [What Codex Rig adds](#-what-codex-rig-adds)
- [Requirements and installation](#-requirements)
- [Quick start](#-quick-start)
- [Skills and specialist roles](#-skills)
- [Quality, review, and calibration](#-quality-gates-and-artifacts)
- [Update, uninstall, and safety limits](#-update-or-reinstall)
- [Package layout and verification](#-package-layout)

</details>

> Value at a glance: install one independently verifiable plugin to get evidence-backed workflows, bounded specialist role cards, portable fallbacks, and auditable artifacts without pretending Codex has native persistent-agent selection.

> Current limits at a glance: named-agent shim installation remains platform-blocked; networked workflows require runtime approval; Codex CLI, Python 3.10+, and optional `gh`/Kaggle authentication are needed for their respective paths; shim mutation is unsupported on Windows and network/distributed filesystems.

## 🎯 What Codex Rig adds

- **A complete development loop:** investigate, assess, implement, review, remediate, optimize, release, and audit with measurable gates.
- **Specialist depth without permanent agent install:** exact packaged role cards can guide independent blank agents or inline passes.
- **Bounded context:** each specialist receives narrow context pack instead of whole parent thread.
- **Evidence-backed completion:** workflows use `.reports/codex/<skill>/<canonical-safe-identity>/run-<NNN>/` only for bounded validated non-sensitive identity and otherwise use `.reports/codex/<skill>/<timestamp>/`; raw arguments are never serialized into paths. Assessed PR reviews use identity `pr-<number>`. Every workflow validates digest-bound final response after gates and discloses failed gates and confidence limits.
- **Auditable commit handoffs:** every proposed or created commit records all meaningful changes, concrete impacts, executed verification, and residual limits in commit body.
- **Cold PR review and remediation:** `$code-review #123` and `$code-remediate #123 +review` preserve current PR evidence and local merge context.
- **Scoped network access:** shell networking stays blocked by default; workflows request one runtime approval for complete command owning each intentional GitHub, Kaggle, marketplace-refresh, or paid live-calibration operation.
- **Calibration:** fixed and behavioral checks measure recall, precision, confidence accuracy, routing leaks, stale assumptions, fixture misuse, unjustified local imports, and incomplete abstractions.
- **Safe legacy cleanup:** authenticated, exact-plan removal exists for thin shims created during pre-release development.
- **Optional codemap-py structural context:** the `implement`, `investigate`, and `optimize` workflows select task-neutral route and probe public codemap-py CLI once per run for only required structural fact, or record zero-query decision for localized edit; they persist one artifact and fall back to bounded file inspection when Codemap is absent.

## ✅ Requirements

- Codex CLI with plugin support
- Python 3.10+
- GitHub CLI (`gh`) installed and authenticated for complete PR review, checkout, and private evidence; public metadata fallback remains limited
- Kaggle CLI installed and authenticated for grounded Kaggle workflows; when it is missing, Codex Rig only explains user-owned setup and never installs it
- Public GitHub access for Git marketplace install or refresh
- Windows, macOS, or Linux for workflows, package verification, sync, and read-only diagnostics
- A POSIX local filesystem only for authenticated legacy agent-shim cleanup

No official marketplace is assumed. Local, unpushed changes are not installable from GitHub.

Codex Rig never enables persistent workspace network access. In network-sandboxed runtime, invoking networked workflow authorizes plugin to request narrow runtime approval for complete owning command; user/runtime still grants or denies prompt. Approving only `gh`, `kaggle`, or another nested executable is insufficient when Python helper owns its subprocesses and HTTPS traffic.

Approval and denial behavior: brief names operation's purpose, capability and effects, target, owning command, and denial outcome. If approval is denied, current tool call stops and assistant turn may end; external command is not run, and Codex Rig does not issue equivalent reprompt or silently broaden fallback. To continue, send new message. Separate operations with materially different effects, such as GitHub read and local checkout or lifecycle mutation, remain separate approvals.

Questions show how to answer: binary plain-text confirmations include `(yes / no)`, selection prompts list supported choices, and free-text requests name the expected format with an example when useful. Native controls retain their actual options; exact-digest confirmations retain their required syntax. Existing authorization is reused, and silence or a default selection never counts as consent.

App Server review evidence requires every supplied turn identity to agree, lifecycle items to be objects, and review events to belong to a still-active reviewer. Contradictory identities, malformed items, and late events fail with a specific reason while preserving any already completed reviewer output. Final evidence validation failures also record failed status and the validation reason after cleanup, rather than leaving a completed result; failure recording still requires a writable output directory.

Internal reviewer state uses a same-module slotted dataclass so misspelled state attributes fail instead of silently creating dictionary entries. State remains mutable during review; external JSON validation and serialized evidence formats remain explicit and unchanged.

## 📦 Install from GitHub

```bash
codex plugin marketplace add Borda/AI-Rig
# Optional reproducible release pin:
# codex plugin marketplace add Borda/AI-Rig --ref codex-rig-v0.4.0
codex plugin add codex-rig@borda-ai-rig
```

The primary command follows GitHub repository's default branch. The commented form pins immutable release bytes.

Start fresh Codex session. Codex discovers plugin's `skills/` and default `hooks/hooks.json`; plugin hooks run only after their current definition is reviewed and trusted.

Verify install in fresh session:

```text
$codex-rig:agent-shims doctor
$codex-rig:audit
```

`doctor` verifies active package, manifest, helpers, role cards, and legacy shim state without writing. The audit workflow checks consuming repository and reports concrete gaps. Skill/all audits also emit prompt-efficiency evidence: matched instruction cost, loaded-reference cost, obligation preservation, behavioral/calibration guards, and adversarial review. `axis=value-per-token` accepts candidate only with matched native/tokenizer evidence, no hard-guard regression, and declared material cost reduction; length or byte count alone cannot establish quality.

## 🌍 Managed global instructions

<details open>
<summary><strong>Global-instruction behavior and sync commands</strong></summary>

`assets/AGENTS.md` is versioned template, not automatically installed plugin capability. Its policy requires following:

- **Implementation:** Use simplest solution for verified current behavior; prefer maintained standard-library/native/already-installed package functionality over duplicating custom code; reject machinery justified only by hypothetical future states, risks, scale, reuse, or edge cases; and preserve trust-boundary, data-loss, security, accessibility, and explicit-contract safeguards.
- **Abstractions and imports:** Abstractions must reduce reader-visible concepts, and Python imports stay at module scope unless verified boundary requires locality.
- **Fixtures and simplification:** Fixtures provide concrete state unless fixture-managed lifecycle requires callable; use ordinary helpers for configurable construction instead of nested fixture factories or aliases that add no meaning. A deliberately bounded simplification records its present ceiling and observable revisit trigger without creating separate debt system.

The sync paths differ as follows:

| Operation                                        | Explicit behavior                                                                                                                                                          |
| ------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Direct marketplace/plugin installation           | Leaves `${CODEX_HOME:-$HOME/.codex}/AGENTS.md` and `rules/codex-rig-github-read.rules` unchanged and does not project repository `.codex/` settings.                       |
| Direct `plugins/codex-rig/scripts/sync_codex.py` | Installs or updates managed Codex plugins, authenticated Codex Rig block, and owned GitHub reader rules; it does not project repository model defaults or personal policy. |
| Root `make sync-codex`                           | Additionally projects root `model` and `review_model` from `.codex/config.toml` and authenticated personal-policy block from `.codex/global-session-policy.md`.            |

The current repository policy keeps parent session on Terra and permits Sol only for explicitly requested advisory pass or explicitly selected Sol agent.

From AI-Rig checkout:

```bash
make sync-all                                     # full Claude + Codex restore
make sync-codex                                   # Codex scope only
make clear-all                                    # teardown: uninstall plugins + strip block; keep model/policy
make clear-codex                                  # teardown Codex scope only
```

Native Codex-only restore and teardown need no Bash or `jq`:

```text
python plugins/codex-rig/scripts/sync_codex.py
python plugins/codex-rig/scripts/sync_codex.py --no-clean
python plugins/codex-rig/scripts/sync_codex.py clear
```

For pinned setup, pass `--codex-ref` with published revision whose Codex Rig package includes `scripts/install_github_read_rules.py`. Older revisions without this lifecycle helper remain valid for direct plugin installation but cannot satisfy current managed setup contract.

`make sync-claude` changes only Claude scope, and `make sync-codex` changes only Codex scope; host selection does not otherwise alter refresh or clean-install semantics. Claude sync manages foundry, oss, develop, research, codemap-py, and `bridge`; it refreshes only retained external caveman plugin. After bridge installs successfully, sync removes any installed copy of retired external Codex rescue plugin; failed bridge install preserves it for recovery. The retired plugin and its marketplace are never installed or refreshed. Codex sync refreshes existing Git marketplace or replaces non-Git registration with canonical `Borda/AI-Rig` Git source, verifies selected source package hashes and closure plus required reader-rule helper, then removes its managed plugins by default and reinstalls them. Codex sync then runs installed Bridge static doctor: it requires the `python` launcher used by MCP to report Python 3.10 or newer and checks Claude CLI help contract without model inference, authentication changes, or provider cost. After successful plugin installation it installs or regenerates owned GitHub reader rules, regardless of `--no-codex-global-agents`; that flag skips only global `AGENTS.md` block. MCP inventory and workspace binding remain per fresh Codex project session. Direct `sync_codex.py` retains `--no-clean` and `--codex-ref`; root `make sync-codex` supplies neither and therefore uses its default clean-install and default-branch behavior.

The direct `sync_codex.py clear` action removes managed Codex plugins, strips only authenticated Codex Rig block from `${CODEX_HOME:-$HOME/.codex}/AGENTS.md`, and removes only owned `rules/codex-rig-github-read.rules`; it leaves repository-projected model defaults and personal policy untouched. Root `make clear-all` reverses selected Claude/Codex installation: it also uninstalls this marketplace's Claude plugins when Claude scope is active, strips Codex Rig block, removes owned reader rules, and leaves repository model defaults and personal-policy state in place; `make clear-codex` and `make clear-claude` scope same teardown to one side only. Both commands keep timestamped backup and preserve user-owned content byte-for-byte, honor `claude`/`codex` scoping where applicable, and leave marketplace registrations plus external plugins in place. Each helper refuses to modify its own unverifiable managed content; earlier successful sync or clear steps are not rolled back.

Codex sync uses template and helper from installed marketplace revision. A missing global file is created as one SHA-256-authenticated managed block. Existing user instructions are backed up and preserved byte-for-byte outside that block. An exact unmarked copy from older sync is adopted without duplication. The same explicit sync also creates or regenerates owned `rules/codex-rig-github-read.rules` from selected installed package; migration may rewrite `rules/default.rules` only to remove exact canonical two-token legacy reader allow entries. It validates complete package and canonical managed-rule body, not merely recomputed rule checksum, and prepares every required existing-file backup before rules mutation. Unrelated bytes remain unchanged. Later runs update only verified managed content; invalid markers, ownership, checksums, or modified bytes stop affected helper before it changes its managed files. The reader rules cover only literal `python`/`python3` launcher union and installed wrapper-path union, including native and POSIX path spellings on Windows; they do not grant broad Python or `gh` access or change network settings.

Each helper validates its own managed inputs before changing them; entire sync or clear sequence is not transactional. Marketplace/plugin refresh and reader-rule updates can finish before later global-instruction merge fails. The reader helper prepares all required existing-file backups before any rules mutation, reports each completed update immediately, and identifies later partial failure without claiming rollback. Resolve reported target state before rerunning sync. Avoid concurrent edits during restoration; portable filesystems provide no universal compare-and-swap operation.

> Reusable reader approval trusts installed cache throughout its lifetime. Setup verifies complete package hashes and closure, but this is consistency checking, not publisher authentication or protection against same-user replacement after setup. Use only trusted cache. A configured explicit pin with invalid package or missing reader-rule lifecycle helper is rejected before marketplace/plugin mutation; newly registered or refreshed sources are verified before managed-plugin removal. Default-branch upgrades can acquire helper during refresh.

Project `AGENTS.md` files remain project-owned and are never changed. Review merged instructions for semantic conflicts; byte preservation cannot resolve contradictory policies.

</details>

## ⚡ Quick start

Skills can be invoked explicitly with `$codex-rig:<skill>` or selected implicitly when request matches their description.

```text
$codex-rig:investigate find the root cause of this Windows-only CI failure
$codex-rig:implement apply the verified fix and run relevant gates
$codex-rig:code-review review the current diff with no prior assumptions
$codex-rig:code-remediate close the high-severity findings
$codex-rig:release assess release readiness for the current package
```

For PR work:

```text
$codex-rig:code-review #123
$codex-rig:code-remediate #123
$codex-rig:code-remediate #123 +review
```

A bare PR number, `#number`, PR URL, or natural-language PR target refreshes current online PR items and verified local checkout directly, so it does not require assessed review artifact. Add `+review` (or another report alias or explicit report path) when remediation should combine matching assessed review report with refreshed online PR evidence.

To remediate latest assessed review created in current session without refreshing PR evidence or online comments:

```text
$codex-rig:code-remediate review
```

When same invocations are passed from shell, quote them so `$` is not expanded:

```bash
codex '$codex-rig:code-review #123'
codex '$codex-rig:code-remediate #123'
codex '$codex-rig:code-remediate #123 +review'
codex '$codex-rig:code-remediate review'
```

## 🔧 Skills

> Skill frontmatter uses compact routing descriptions to conserve Codex skills catalog; each `SKILL.md` body remains complete workflow contract.

Codex Rig installs 14 skills: 13 workflows plus legacy shim manager.

| Skill            | Purpose                                                                                                             |
| ---------------- | ------------------------------------------------------------------------------------------------------------------- |
| `assess`         | Inspect issue, PR, module, or problem before implementation; emit ranked findings and gates.                        |
| `audit`          | Detect configuration, workflow, routing, documentation, prompt-efficiency, and quality-gate drift.                  |
| `calibrate`      | Run fixed and behavioral checks across packaged skills and roles; score recall, precision, and confidence accuracy. |
| `code-remediate` | Triage review findings, select valid work, assign owners/verifiers, apply fixes, and prove closure.                 |
| `code-review`    | Close PR at evidence-backed proposal gate or review its local diff across mandatory and risk-triggered axes.        |
| `implement`      | Run plan-build-verify implementation loop with promoted read-only fan-out and serial parent acceptance.             |
| `investigate`    | Debug code and narrow unknown failures to evidence-backed root cause before implementation.                         |
| `kaggle`         | Create or extend grounded Jupytext Kaggle notebooks, grounding schema via authenticated `kaggle` CLI.               |
| `manage`         | Safely create, update, or remove Codex skills and agent configuration with promoted read-only inventory fan-out.    |
| `optimize`       | Measure first, change one bounded variable, remeasure, and reject regressions.                                      |
| `release`        | Assess SemVer, changelog, migration, packaging, and release readiness.                                              |
| `research`       | Collect current primary evidence and map findings to concrete implementation choices.                               |
| `sync`           | Inspect active plugin-cache drift or refresh public-GitHub Codex Rig installation without cache edits.              |
| `agent-shims`    | Diagnose and remove authenticated thin shims from pre-release development; new installation stays blocked.          |

Every workflow defines input contract, fail-fast rules, required gates, artifact shape, and confidence output. `shared/quality-gates.md` owns compact outcome-coupled final-chat frame—Outcome, Results, Verification, Remaining, Recommendations / next steps, Confidence, Artifact—while `shared/final-handoff-contract.md` makes that structure executable after gates: schema-v2 results bind validated `final-handoff.json`, rendered `final.md`, and digest record before promotion, and workflow emits `final.md` verbatim. Rendered section and table labels use portable Markdown bold text instead of headings or terminal color escapes. Each skill keeps its exact outcome vocabulary, result table, and terminal exceptions; next steps reference result rows instead of repeating them, and artifacts supplement rather than replace readable result. Historical schema-v1 artifacts remain readable. `agent-shims` stays explicit exception because it has no canonical run/result artifact, and current host still has no post-send transcript hook to prove chat transport bytes. `shared/native-skill-contract.md` likewise owns generic shell-network approval and denial behavior while each networked skill retains its five concrete operation values and recovery exceptions. Workflow instructions live in `skills/<name>/SKILL.md`; shared executable contracts live in `shared/`.

## 🔗 Optional codemap-py structural context

<details>
<summary><strong>Bounded Codemap integration and fallback vocabulary</strong></summary>

`implement`, `investigate`, and `optimize` select route and probe the [codemap-py](https://github.com/Borda/AI-Rig/tree/main/plugins/codemap-py) plugin once at bounded decision point via `shared/codemap_adapter.py`, then persist result to run artifact — specialists consume that artifact, never fresh query. An exact localized edit with no unresolved structural fact uses `skip`; one unresolved fact uses matching single route; broad or unknown scope uses legacy `standard` batch; explicit structural request overrides `skip`. The other workflows retain their existing category-specific standard behavior or recorded not-applicable status. The adapter reads only public `codemap-py doctor --json`/`query` CLI surface, never codemap-py's cache internals, source paths, or cross-plugin Python import.

The adapter resolves validated launcher once per workflow and reuses it. Independent standalone read-only queries may run concurrently only against prepared stable index with self-heal disabled (`SCAN_NO_AUTOBUILD=1`); dependent queries wait, and refresh/self-heal/index writes are serial. A complete untruncated result settles its own graph fact, not sibling dimensions; there is no arbitrary total-call cap for facts required to finish, while targeted correction retries stay bounded and stop when same failure recurs. Explicit independent AST/oracle work remains allowed.

The adapter reports one named status: `available`, `absent`, `stale`, `incompatible`, `degraded`, `stale+degraded`, or `skipped`. `skipped` means workflow deliberately selected zero Codemap subprocesses; it is not structural evidence. `stale+degraded` is vocabulary's only composed value and means both caveats hold at once, so neither masks other. A standard batch run without `--target` omits queries that require one instead of failing them, so targetless probe reports honest status of queries it actually ran. Each query also records index file that answered it, and any disagreement with path health probe resolved is listed under `index_path_divergence` as evidence — both paths retained, never reconciled, and never folded into status. Absence and incompatibility are non-fatal — workflow falls back to its normal bounded file inspection. `manage`, `sync`, `agent-shims`, `calibrate`, and `kaggle` stay not-applicable with recorded behavioral reason (no Python call-graph subject); see `shared/codemap-contract.md` for full protocol, adaptive route vocabulary, category-to-query map, per-skill route selection, and not-applicable rationale. Repository sync installs Codemap alongside Codex Rig, but Codex Rig retains zero runtime dependency on it: packaging, skill discovery, and startup still work when Codemap is absent or incompatible.

The active consumer contract is separate from provider integration metadata. Audit compares active source guidance with reachable installed skill references and reports `consumer_query_guidance_missing`, `consumer_query_guidance_unreachable`, or `consumer_query_guidance_drift`; static references establish reachability only, not semantic loading or current-session activation. The provider-managed `codemap-py-integration.md` block is metadata-only, and matching installed bytes or native listings cannot substitute for session evidence.

</details>

## 🤖 Specialist role cards

Roles are canonical behavioral profiles, not claims that Codex selected custom agent configuration. Each card includes trigger/skip boundaries, evidence ownership, execution constraints, handover fields, and confidence rules.

When several independent role passes are justified, Codex Rig fixes their routes and narrow context packs first, dispatches approved work in one wave when runtime supports it, and joins every handoff before parent acceptance. The installed `shared/parallel_execution.py` validator derives `parallel` only from overlapping substantive intervals recorded in one manifest wave; completed spawned passes without overlap are `independent-spawned`, while unsafe or unavailable fan-out is `serial-fallback` with equal gates. Schema-v1 manifests remain readable for historical structural checks only; generic runtime promotion requires schema-v2 and non-sensitive portable-read-restricted plan/manifest. That runtime evidence binds frozen parent plan, parent spawn/start, child lineage, persisted restricted controls and approval `never`, terminal interval, exact output, parent result-delivery event, and context/output common-secret scans to currently observed Codex rollout shape; filesystem credential isolation remains unverified. The summary does not claim global network, command, credential, or filesystem denial or that all command behavior was inspected. Generic parallel writes and future host-isolated tier remain unavailable; code-remediate-local has separate production lifecycle described below. That shape and its timestamp units are internal rather than documented platform guarantee, so drift fails closed. It never starts second wave for same parent work item; new discoveries stay parent-serial or require user-visible re-plan. Scheduling never expands fan-out, overlaps ownership, bypasses approval, treats requested-only controls as enforced, or accepts unjoined output.

The execution contract uses `--execution=serial|parallel-read|parallel-write|auto`, which avoids ambiguity with positional task text. An explicit invocation value wins over `CODEX_RIG_EXECUTION`, which wins over shipped default. The default is `auto`: it selects only already-promoted read-only consumer route and otherwise resolves safely to `serial`. A launch-wide default can be selected with `CODEX_RIG_EXECUTION=auto codex`, while one invocation can request `--execution=parallel-read`. Neither `auto`, environment, nor `--execution=parallel-write` grants write authority: every write still needs newly frozen plan and exact digest-bound approval, and unpromoted modes fail closed or use equal-gate serial fallback. See the [canonical G0–G8 execution flow](https://github.com/Borda/AI-Rig/blob/main/plugins/codex-rig/ARCHITECTURE.md#canonical-g0g8-execution-flow) for forked dispatch and synchronization chart.

`implement` and `manage` have promoted portable read-only routes with freeze/join barriers, resource conflicts, serial parent decisions, equal-gate fallback, and stop rules. Each route is plan-bound to exact `consumer_id`, `capability=portable-read-only`, `promotion_status=promoted`, `parent_mutations=serial`, and `canonical_gates=serial`. Before dispatch, skill runs installed `parallel_execution.py preflight` for its fixed consumer; after every terminal join it runs `validate-runtime` with same consumer and repeats preflight before any parent mutation. A planned mutation requires separate approval whose exact plan digest, approval response, and human source validate. Unbound generic runtime evidence is promotion-ineligible. Every mutation and canonical quality gate remains parent-serial, and generic writes remain disabled. `auto` cannot bypass consumer promotion, plan binding, serial parent authority, or write approval. The [canonical G0–G8 execution flow](https://github.com/Borda/AI-Rig/blob/main/plugins/codex-rig/ARCHITECTURE.md#canonical-g0g8-execution-flow) is shared gate reference.

`shared/parallel_telemetry.py` provides privacy-minimized timing and token accounting for rollout analysis. It retains HMAC identifiers, counters, declared outcomes, child timing, workload-key digests, and explicit dispatch-to-final-join duration; it does not retain raw prompts or responses, reasoning, tool data, paths, environments, credentials, or raw runtime IDs. Two matched live pairs measured speedups of `1.6091x` and `1.0791x`, with token multipliers of `1.0024x` and `0.9976x`. These are workload-specific observations, not guarantee. The current host has no provider-enforced per-child usage cap, so actual context may exceed pre-dispatch reservation; compact telemetry reports overrun. Child-duration maxima are diagnostic proxies only and never support savings claim.

Each parallel wave must freeze positive token-admission ceiling and positive per-node reservations before dispatch. Completed and active reservations must form stable prefix and remain charged; admission stops before first node that would exceed ceiling, active children finish to terminal evidence, and all unstarted nodes are serially re-planned with same gates. Schema-v2 runtime acceptance binds each wave and spawned node to `token_budgets` in exact hashed plan. Earlier schema-v2 evidence without token budgets can be read only with `historical_unbudgeted=True`; that result is acceptance-blocked and promotion-ineligible, while default path remains budget-required. This bounds admitted reservations, not actual provider usage: no provider-enforced child token cap exists on current host, so actual context may exceed reservation and compact telemetry reports any overrun. Retained wave proof uses HMAC identity, proof digest, and bounded status/counters. `enforce_diagnostic_expiry` appends path-free JSONL evidence to fixed `expiry-audit.jsonl` before eligible deletion and after each outcome; it deletes only exact HMAC diagnostic and retains unresolved diagnostics until resolution. Unknown or raw fields fail closed.

Operator rollback is deliberately procedural:

1. Disable the affected skill's parallel opt-in without changing the frozen plan or its digest.
2. Preserve completed outputs, terminal child evidence, parent joins, and the original quality gates.
3. Serially execute only unfinished work; never replay completed nodes.
4. Retain failed or conflicted worktrees and stop when cleanup or repository state is ambiguous.

`shared/parallel_worktrees.py` is bounded generated-fixture scaffold, not production write route. The active parent freezes exactly two disjoint work packages at one clean `HEAD`, creates separate detached worktrees, dispatches both subagents, and joins exactly two child reports containing node ID, completed status, concise summary, exact changed paths, and canonical Git patch SHA-256. A completed child obtains that fixed-shape report through `create_completed_child_handover`, which hashes raw Git subprocess bytes through lifecycle module rather than shell- or RTK-rendered diff output. The parent verifies both reports against actual worktrees before deriving patches, integrates them in stable order, and cleans successful worktrees without force only after durable evidence. It strips inherited `GIT_*` redirection overrides, rederives managed authority before every transition, fingerprints declared retained attempts, and rejects source drift, commits, staged/untracked/undeclared/delete/rename/mode/type changes, aliases, ownership ancestry, symlinks, partial joins, report mismatches, conflicts, and cleanup uncertainty.

The retained generated-fixture record is operational audit trail under parent authority, not cryptographic host attestation. It does not claim particular child tool, child authorship, edit-time overlap, native-Windows Git lifecycle coverage, or production eligibility. It keeps `write_parallel_eligible=false` and `write_parallel_promoted=false`; every generic production route remains disabled. App Server readers, brokers, sidecars, signed receipts, and full-thread filtering are intentionally unnecessary.

Code-remediate-local has separate accepted production lifecycle, not generic resolver promotion. Its exact schema-v2 plan and approval bind one clean authoritative source repository and exact `HEAD`/tree, two to four disjoint buckets, actual context-pack paths and SHA-256 values, resource locks, detached worktrees under only external sibling root `.codex-rig-worktrees/<run-id>` outside that checkout, fixed new state basename and output names under source-local run root, fixed `code-remediate-shared-quality-gates` reference, rollback policy, and non-force cleanup policy. Plan, approval, state, patch, rollback, and lifecycle artifacts stay in authoritative repository's normal `.reports/codex/code-remediate/...` run directory. Preparation and every authority transition re-hash each actual context pack and reject drift. The thin argparse sequence is `prepare`, `create-handover`, `join`, `collect`, `integrate`, `apply-source`, and `cleanup`; it is not scheduler, registry, or global promotion mechanism. The parent prepares worktrees; children edit only their owned paths, do not commit, and return canonical terminal status, summary, changed paths, and patch SHA-256. The parent re-derives patches, joins every terminal handover, integrates them in lexical bucket order in separate integration worktree, and records only Git-structural integration as `structurally-verified`; it does not execute arbitrary plan-provided commands. After source application, existing shared quality-gate phase remains executable result authority and its validated `gates.json` is required for passing remediation result.

Source application rechecks exact raw integration postimages, captures raw source preimages, stores durable reverse patch, applies one parent-generated forward source bundle, and verifies expected Git content after authoritative worktree's clean filters. The lifecycle records raw source SHA-256 postimages for cleanup and evidence, while Git clean-filtered object identities allow LF and CRLF worktree bytes to represent same repository content across native platforms. Only known Git-content states may be restored after recomputing affected identities and confirming each path is at its recorded preimage or expected postimage; mismatch, filter failure, restore error, or failed recomputation records `rollback-ambiguous`, retains worktrees and evidence, and stops without automatic restore. Non-force cleanup occurs only after durable source application and exact recorded source postconditions, and failures retain evidence. The artifact validator independently re-hashes every child patch, forward source bundle, and rollback patch beneath exact run root, while source, worktree, evidence-root, state, output, and patch path components reject symlinks and path escapes. The schema-v2 lifecycle record and digest are bound into remediation result; schema-v1 plans are planning-only and cannot prove completed execution. Containment is `parent-authoritative operational postcondition containment` with `capability_sandbox_verified=false`, not per-child capability sandbox, hostile-child security boundary, globally atomic source transaction, or security isolation guarantee. Separately sandboxed processes remain future stronger alternative, not current prerequisite.

The code-remediate-local production route completed full lifecycle and rollback proof. Installed-package acceptance runs same lifecycle suite from manifest-declared payload, and repository's full-test matrix records that gate on Linux, macOS, and native Windows. Promotion is limited to this consumer-owned route. CI configuration, previous local proof, `auto`, environment value, or `--execution=parallel-write` never substitutes for newly frozen consumer plan and exact-digest write approval.

| Role                 | Requested model | Primary axis                                                                     |
| -------------------- | --------------- | -------------------------------------------------------------------------------- |
| `delegation-lead`    | Luna            | Cost-aware workstream routing and consolidated handover.                         |
| `sw-engineer`        | Terra           | Core implementation, APIs, types, and reproducible Python/ML code.               |
| `qa-specialist`      | Terra           | Regression proof, edge cases, test design, and executable acceptance.            |
| `squeezer`           | Terra           | Profile-first performance, throughput, memory, and synchronization analysis.     |
| `doc-scribe`         | Luna            | User documentation, docstrings, examples, changelogs, and migration guidance.    |
| `security-auditor`   | Sol             | Auth, permissions, secrets, deserialization, supply chain, and trust boundaries. |
| `data-steward`       | Terra           | Dataset integrity, leakage, splits, augmentation, and reproducibility.           |
| `cicd-steward`       | Luna            | CI/CD, matrices, caching, publishing, and flaky-run diagnosis.                   |
| `linting-expert`     | Luna            | Ruff, mypy, pre-commit, suppressions, and static-analysis policy.                |
| `oss-shepherd`       | Luna            | OSS triage, SemVer, deprecations, contributor workflow, and release readiness.   |
| `solution-architect` | Sol             | System design, public contracts, coupling, compatibility, and migrations.        |
| `web-explorer`       | Luna            | Current external documentation, changelogs, and migration evidence.              |
| `curator`            | Terra           | Configuration quality, duplication, drift, stale references, and weak gates.     |
| `challenger`         | Terra           | Adversarial stress tests for plans, risky changes, and no-finding conclusions.   |
| `scientist`          | Terra           | Papers, hypotheses, experimental methods, metrics, and ablations.                |

The model names are requested role settings. Blank-agent injection does not prove actual model, reasoning effort, sandbox, approval policy, or nesting profile. Workflows must record requested and observed controls and stop when unproved setting is mandatory for safety.

## 🔗 How portable role routing works

<details>
<summary><strong>Role-card injection, fallback, and provenance details</strong></summary>

The canonical policy is `shared/specialist-orchestration.md`.

Read-only specialists return findings or executable probe requests; parent saves responses and owns authorized scratch execution. Review and investigation do not grant source-edit authority. Implementation and remediation retain their separately authorized writer roles. Parent-run probes remain distinguishable from independent conclusions. Runtime validation rejects explicit filesystem write grants even under read-only sandbox label; absent contradictory grants still do not prove isolation. Missing child controls remain process limitation, not reason to broaden permissions or stop otherwise permitted serial investigation.

Every user-facing message starts with short plain-English explanation before technical details: progress updates, questions, approval requests, errors, blockers, handoffs, and final answers. When workflow pauses, its report names stopped action and scope, concrete cause/evidence, governing rule, safe work that continues, next step and responsible actor, and exact resume condition. Routine preparation and safe diagnosis stay agent-owned; generic "repair the environment" instruction is not diagnosis. Existing authorization is used first; denial and retry rules remain in force. Source inspection continues whenever source is accessible, while unavailable source identity, unsafe execution, credential exposure or unauthorized credential access, and unauthorized destructive or remote mutations remain hard stops; authorized credential-broker reads and local changes continue.

When native reviewer controls are unavailable, Code Review has explicitly approved [isolated App Server route](skills/code-review/app-server-review.md). The parent launches bounded independent read-only threads, disables external tool capabilities for that invocation, saves unchanged responses, and validates schema-4 evidence before normal completion gates. Its `app-server-parent-observed` evidence is not native child lineage, cryptographic attestation, or credential isolation. This experimental route requires compatible installed Codex CLI and separate paid-execution approval; it does not automatically promote other skills or enable parallel writes.

Reviewer output must be new and contained beneath resolved plan directory. Setup failures return bounded errors; when output storage is unavailable, no diagnostic artifact is promised. Completed responses remain retained if later failure occurs.

App Server accepts documented planning and warning events with bounded validation. Unknown events still stop that wave; rejection diagnostics retain static cause/recovery fields, never raw rejected payloads. A repeated protocol failure stops retries of that launcher, not accessible source review. Use available instruction-bounded reviewer or disclosed parent-serial fallback without redundant approval; ask for decision only when explicitly required independence remains unavailable. Historical rejected events cannot be reconstructed from payload-free evidence.

Review recovery: verified source → available text-only reviewers → parent inspection if needed → findings with honest coverage. Failed launchers stay stopped; accepted evidence never includes rejected outputs.

| Situation                                            | Review action                                             | Human input needed                                                     |
| ---------------------------------------------------- | --------------------------------------------------------- | ---------------------------------------------------------------------- |
| Public API changed; CI or CLA fails                  | Continue inspection; report compatibility or merge issues | None for reading                                                       |
| Child controls unproven; App Server fails repeatedly | Stop that launcher; use native text-only or parent review | None for available authorized route                                    |
| A probe might execute unsafe code                    | Pause probe; continue inspection                          | Safe execution conditions and approval if required                     |
| Required source or identity unavailable              | Pause dependent assessment                                | Supply verified source/evidence                                        |
| Explicitly required independence unavailable         | Finish available inspection; withhold full completion     | Restore independent route or revise requirement                        |
| Requested action exceeds authorization               | Pause that action; preserve unaffected work               | Specific approval or human-owned action where delegation is prohibited |

1. The parent determines whether work actually benefits from independent specialist.
2. It reads and hashes `roles/<role-id>/ROLE.md`.
3. It builds narrow context pack: objective, relevant evidence, exclusions, concrete questions, output contract, and stop rule.
4. It asks runtime-provided blank/default subagent to follow complete role card before context pack.
5. If no safe subagent route exists, it performs inline role pass and reports that independence is false.
6. The parent reconciles outputs, inspects executable evidence, and owns final acceptance.

Passing only role name or path is not role injection. A task name records provenance only; it does not select custom profile. Fallback is permitted for route absence or rejection before substantive work, never because specialist disagreed or found problem.

For every routed pass, Codex Rig records role ID, card hash, attempted and selected routes, fallback reason, observable model/effort, requested and observed controls, independence, nesting depth, and material fidelity limits.

</details>

## 💰 Orchestration and cost control

<details>
<summary><strong>Tier ownership and escalation guardrails</strong></summary>

Use delegation only when two or more disjoint workstreams can proceed without duplicating same context. Typical high-value splits are implementation versus tests, architecture versus migration docs, or CI diagnosis versus static analysis.

- Luna owns bounded coordination, documentation, CI/CD, web evidence, OSS, and linting support.
- Terra owns implementation, runtime behavior, tests, data/ML, performance, curation, challenge, and executable verification.
- Sol is reserved for solution architecture and security.

Select smallest capable tier from current task evidence: escalate only for mandatory boundary or observed lower-tier insufficiency, de-escalate only after evidenced scope split leaves bounded support, and never change tiers on cost alone. The canonical policy is [`shared/specialist-orchestration.md`](https://github.com/Borda/AI-Rig/blob/main/plugins/codex-rig/shared/specialist-orchestration.md#delegation-lead-and-model-routing).

Two consecutive work cycles without material progress, or three evidence-backed attempts that leave same closure condition unmet, trigger persisted and validated `reasoning-progress.json` stall ledger and one advisory escalation: supported higher reasoning effort first, then next permitted tier. The advisor supplies bounded recovery action and stop condition but makes no changes; its route is valid only when observed sandbox is `read-only`. If no safe route exists or that one action still fails to close condition, Codex Rig stops and asks user with consolidated evidence, hypotheses, rejected alternatives, and recommended next step. This guardrail is distinct from, and never resets, repeated-obstacle policy.

The delegation lead returns one handover. Executable acceptance, runtime/API changes, release-blocking decisions, and security/architecture conclusions remain parent- or appropriate Terra/Sol-owned. Narrow work stays in parent when handoff cost would exceed its value.

</details>

## 📊 Quality gates and artifacts

<details>
<summary><strong>Artifact shape and confidence thresholds</strong></summary>

Workflow artifacts commonly use this shape; exact files vary by workflow:

```text
.reports/codex/<skill>/<timestamp>/
├── result.json
├── gates.json
├── gates.txt
├── failed.txt
├── gates.checks.jsonl
└── skill-specific evidence
```

The exact files vary by workflow, but completion requires requested output, explainable gate results, unresolved risks, and validated `result.json`. Shared helpers provide diff collection, PR evidence collection, gate execution, artifact validation, severity mapping, and result writing. Local reviews retain timestamped shape above. A PR review starts there because current-branch input may not yet reveal PR number, then successful authoritative collection promotes whole run to `.reports/codex/code-review/pr-<number>/run-<NNN>/`; all later artifacts use that printed path. Failed pre-identity collection remains timestamped unavailable diagnostic, not assessed review. Existing flat review artifacts remain discoverable without migration.

Confidence is evidence-backed:

- `<= 0.80`: incomplete; continue recovery or report blocker.
- `0.80 < confidence < 0.85`: very questionable; stronger evidence is required.
- `0.85 <= confidence < 0.90`: cautious-low; objective recovery evidence and remaining limits must be explicit.
- `>= 0.90`: fair, not automatic; material residual limits still belong in result.

</details>

## 🗺️ PR review-to-remediation

Review completion is executable: `shared/find-review-report.py --complete-run <run-directory>` reruns both artifact validators against promoted result, checks that PR lookup selects that exact result, and emits only digest-bound final text. A notes-only run is `matching-review-incomplete`, not missing evidence; newer incomplete or malformed reviews block older assessed fallback. A later collection failure cannot clear intervening incomplete or unpromoted review. Failures lead with “Review handoff blocked,” preserve preliminary evidence, and never masquerade as completed review or silently switch to online-only remediation. Code Review first supports instruction-bounded native inspection route whose reviewers receive full role card first, then scope inventory and relevant source/diff/evidence as untrusted input, and return text only; prohibited tool use is detected and rejected, not prevented by sandbox claim. Strict portable launcher admission and post-run validation remain required only for optional portable route and other consumers.

<details>
<summary><strong>Evidence collection, review closure, and remediation boundaries</strong></summary>

- **Review intake:** `$codex-rig:code-review #123` collects contributor intent from PR title/body, comments/reviews, target-branch evidence, exact local PR head, and locally derived diff before producing structured review artifact. Collection starts in temporary timestamped run; after authoritative `pr.json` succeeds, run creator promotes it to next numeric `.reports/codex/code-review/pr-<number>/run-<NNN>/` directory and that printed path owns every later artifact. Assessed PR handoffs begin with snapshot rebuilt from those run artifacts: PR number/link, author, GitHub check status, intent-based type, and review suggestion; validation rejects missing or replaced fields and suggestion that disagrees with structured decision.
- **Terminal closure:** After successful collection it may emit evidence-backed terminal `close` decision before detailed review for one of `FALSE_GOAL`, `BREAKING_CONDUCT`, `WRONG_SCOPE`, `WRONG_PROVENANCE`, `DUPLICATE`, `UNADDRESSED_REVERT`, `SPAM`, or `ARCHITECTURE_VIOLATION`; ambiguous evidence always continues to detailed review, and decision never closes, comments on, merges, or otherwise mutates GitHub.
- **Review routing:** It prefers independent QA/challenge passes for broad or high-risk diffs and conditionally triggers architecture, security, CI, docs, data, performance, research, or web evidence when detailed review proceeds. If launcher is unavailable, documented parent-serial substitute may continue review but is not independent; expressly user-required independent pass remains unmet and withholds completion. Incomplete context or provenance is disclosed, and unsupported conclusions are withheld. Routing evidence and triggered-role reasons are always non-empty JSON string arrays, so validators can distinguish malformed output from assessed review.
- **GitHub transport:** `shared/github_read.py` is sole GitHub data transport: it prefers authenticated `gh` but never reads credentials; permits only audited built-in view groups (`gist`, `issue`, `pr`, `project`, `release`, `repo`, `ruleset`, `run`, `workflow`), REST GET, and GraphQL query operations; and retains no CLI failure output.
- **Approval boundary:** In network-sandboxed runtime, complete collector command—not standalone `gh` preflight—remains the unit for runtime external-network approval because its nested CLI, HTTPS fallback, checkout, and Git fetches inherit collector's execution context.
- **Managed host preapproval:** All four `--approve-gh` workflows reuse loaded host allow rules to execute matching helpers without another prompt, subject to stricter host restrictions. Explicit setup already covers the generic reader used by Assess and Release. For Code Review, Code Remediate, and PR-mode Assess, add repeatable `--approve-pr <canonical-pr-url>` to the installed `scripts/install_github_read_rules.py` setup command with its required `--plugin-root` and `--codex-home` options. This grants literal `python`/`python3` plus the installed collector, `--target`, and only those exact PR URLs in `rules/codex-rig-pr-collection.rules`. Future approved sync refreshes the collector path without adding targets; teardown removes the owned grants. Restart Codex after setup. Grants also apply to matching unflagged commands and permit supported output destinations and safe local checkout; they trust installed code and are not filesystem isolation. Setup still requires explicit host permission, and the flag never invokes setup or overrides a denial. See [setup details](scripts/README.md#install_github_read_rulespy).
- **One-flag authorization:** Use `$codex-rig:code-review 123 --approve-gh` or `$codex-rig:code-remediate 123 --approve-gh` (also supported by the unnamespaced skill aliases). The flag records completed user authorization for the required PR collection; the workflow does not ask for a second workflow-consent confirmation. The reusable collector boundary is the direct command prefix `actual Python executable + absolute installed collect_pr.py + --target + canonical GitHub PR URL`; `--out` and `--checkout` follow that prefix, report timestamps remain outside it, and no `rtk` wrapper is allowed. This covers GitHub evidence, fetches, collection artifacts, and safe local checkout updates for that PR. Runtime permission is separate: a matching saved host rule may be reused, but the runtime may still prompt or deny when no matching rule exists. The flag cannot bypass host prompts or denials, create or modify saved rules, change permissions, or authorize remote mutation. Numeric targets resolve only from an unambiguous local repository; otherwise initial collection uses one-shot approval until authoritative identity is known. Saved rules remain runtime-owned and need a new match after collector path/version or PR identity changes. Local/report-only scopes reject the flag; remediation still asks which findings to fix. The collector rejects duplicate targets and abbreviated options so later arguments cannot override the approved PR.
- **Analysis and release authorization:** Append `--approve-gh` to `$codex-rig:assess <question-and-scope>` or `$codex-rig:release audit`. For GitHub evidence already required by those workflows, the flag records completed user authorization for the direct reader command prefix `actual Python executable + absolute installed github_read.py`. The reader approval is deliberately reader-wide across repositories, with audited views, REST GET, GraphQL queries, supported public fallback, output-file writes, and allowlisted local PR checkout; it is not limited to the current issue, release, or repository. Dynamic `--out` and validated `gh` arguments follow the prefix, and no `rtk` wrapper is allowed. Runtime permission remains separate: a matching saved host rule may be reused, but the runtime may still prompt or deny; the flag cannot bypass host prompts or denials, create or modify saved rules, change permissions, or authorize publication or other remote mutation. The flag never adds GitHub traffic to local-only work, and PR analysis retains the narrower collector-and-PR-URL prefix. Existing evidence, scope, and release-readiness gates remain mandatory.
- **Retry and denial:** If the agent mistakenly launches that collector without runtime access and receives a sandbox-shaped `github-network` failure before any user prompt or denial, one unchanged collector retry through the runtime approval mechanism is required before failure becomes terminal. A user denial always stops the current turn and forbids that retry; `--approve-gh` does not suppress or repeat a runtime prompt.
- **Evidence tiers:** `collect_pr.py` separates core source evidence from supplemental online evidence: PR identity/body, base-repository identity, target ancestry, exact PR-head checkout, and local diff are mandatory; GraphQL review-thread resolution status and derived statistics may degrade with explicit artifacts and confidence gaps.
- **Checkout and ancestry:** GitHub CLI stays primary. The collector refreshes target and PR tips itself, including fetching fork PR's pull ref before checkout-overlap comparison; unfetched fork commit must not become false checkout blocker. Open fork PRs use `gh pr checkout <number>` unless current HEAD already exactly matches fresh PR metadata, then `git diff <base>...<head>` supplies authoritative patch. Before checkout, collector permits unrelated tracked edits and blocks only paths that checkout would overwrite, retaining `worktree-preflight.json` with exact path evidence. Both tips must be fresh before conflict resolution; use fetched target ref without switching branches. An authorized pull on verified PR branch/upstream is fast-forward-only and followed by source verification. Target advancement is integration context, not PR finding or merge blocker; genuine divergence remains collection failure.
- **Historical and assessed results:** Historical merged/closed PR evidence uses GitHub's pull ref, exact SHA verification, and detached local checkout, but merge decisions and code-remediate remain OPEN-only. Every assessed non-approval PR result includes findings/action table only for actual findings or review gates.
- **Collection failure:** If core collection fails before source review, report `PR Review Availability: unavailable`, then immediately state specific reason, source findings `not assessed`, merge decision `not made`, and plain process diagnostic/recovery/evidence prose. A dirty-worktree overlap names exact paths from `worktree-preflight.json`; never use Markdown table, list operational failure as PR issue, or emit `needs-more-work`. Current-attempt evidence is retained for diagnosis but remains explicitly unassessed. Authentication recovery remains user-owned, out-of-band `gh` operation.
- **Fallback eligibility:** Public PR metadata fallback is limited to `github-network`, `github-auth`, `github-rate-limit`, or `command-timeout` failures and requires canonical URL matching configured GitHub remote, or numeric target bound to one distinct configured GitHub repository identity. Ambiguous or unsafe targets, permission, not-found, and unclassified failures fail closed. GitHub GraphQL object-resolution failures remain not-found errors instead of activating network fallback.
- **Fallback evidence:** The HTTPS client uses Python's default CA store and recovers available system CA bundle only when that store is empty. The fallback normalizes limited metadata, verifies a `refs/pull/<number>/head` detached checkout, derives local diff, and records unavailable evidence in `online-review-summary.json`; it cannot establish private PR evidence.
- **Fallback gaps and diagnostics:** Review and remediation list sorted IDs `github_provided_file_list`, `mergeability`, `review_decision`, `reviews`, and `top_level_comments` in their online triage/action evidence, add exact gap `Public HTTPS PR metadata fallback omitted evidence: <sorted IDs>.`, and cap final confidence at `0.89`. Raw CLI stderr is never persisted; terminal diagnostics may include safe `failure_reason` enum.
- **PR refresh:** `$codex-rig:code-remediate #123` collects current online PR items and verified local checkout directly; `$codex-rig:code-remediate #123 +review` additionally finds newest matching assessed review artifact, refreshes same core PR/body/checkout/local-diff evidence, records supplemental review-thread coverage gaps, evaluates merge-conflict risk, and presents resolution table before editing.
- **Current-session reuse:** `$codex-rig:code-remediate review` instead reuses the latest assessed `code-review` result created in the current session in report mode; it does not refresh PR evidence or online comments, and fails if that artifact is unavailable or closed at the proposal gate. A newer close result blocks fallback to stale assessed findings because it contains no source-remediation contract.
- **Candidate recovery:** `$codex-rig:code-remediate #123 +review` first uses newest promoted matching `result.json`. A newer same-session `result.candidate.json` is never consumed directly: remediation reports `matching-review-candidate-unpromoted:<path>`, reruns review-specific validator and then shared validator, and promotes candidate only when both pass. A manifest-bookkeeping failure gets one evidence-preserving repair from retained specialist/rollout records before both validators rerun; unresolved failure persists exact code in `review-candidate-validation.txt` and never falls back to stale findings. `code-review` preflights manifest shape and spawned attempt cardinality before creating candidate.
- **Prompt ownership:** Scope selection renders full indexed ledger, report path, question, and choices together in one user-visible message, so collapsed terminal output cannot hide options. Parallel-plan context remains separate from its one approval control.
- **Work buckets:** Selected findings form non-overlapping specialist/domain work buckets of at most five items. Five or fewer items stay in one agent scope; larger work uses fewest coherent buckets, and parallel fan-out occurs only after user approves exact displayed plan digest.
- **Parallel child verification:** Before plan hashing, preflight exact child commands against unchanged disposable baseline, separating expected failing regression assertions from launch/configuration errors and requiring zero ignored or untracked output. No future implementation is needed to approve plan. Freeze only that command text; changes require new digest and approval. After implementation, every approved child check must pass before handover or integration, with intended postimages unchanged and no generated output. Never delete verification output to manufacture clean handover; incompatible checks keep bucket parent-owned or sequential. Authoritative coverage and full gates remain parent-owned after source application.
- **Conditional lifecycle detail:** Remediation loads its packaged `references/parallel-lifecycle.md` only when evaluating or executing parallel-specialist plan. Parent-owned and sequential routes retain common scope/approval/output checks without loading unused production lifecycle detail; parallel routes still load every containment, rollback, cleanup, and evidence obligation.
- **Plan revision:** A revise response regenerates plan and requires fresh `approve` or `parent-only` decision.
- **Validation:** The validator rejects missing/duplicate item coverage, hidden source records, invalid owners or context packs, path-alias or ancestor overlap, excessive bucket size, one-specialist-per-finding fan-out, and approval not bound to current plan.
- **Recap:** The final recap is rendered from validated per-item machine ledger and repeats every ingested item in compact outcome table, including implemented, rejected, skipped/unselected, already-closed, and unresolved dispositions.
- **Compact remediation tables:** Initial selection, durable resolution, and final outcome tables show each contributing source only as `report [<report-file>:<line>]`, `report [<report-json>#<finding-id>]`, or `online [<comment|thread|review-id>]`; online references use stable IDs, not URLs, and grouped pointers use one plain space so terminal output never exposes HTML tags. A source cell is unique pointer, never prose or evidence. Cells keep identifiers, short names, statuses, and symbols; complete summaries, resolution explanations, evidence, and next actions appear as ordered symbol definitions immediately below each table. Exact duplicates retain every compact pointer in source order, while machine metadata and expanded ledger records preserve each source location, complete body, and evidence path. Resolution, evidence, owner/status, and unresolved next actions remain mandatory.
- **Remote boundary:** The workflow never pushes, comments, merges, or publishes remotely.
- **Detailed review routing:** The shipped `skills/code-review/review_routing.py` helper derives mechanical tier and exact file/line evidence from collected diff artifacts before specialist selection; public API compatibility is assessed normally and public-API touch alone is not automatic `HIGH_RISK`. The terminal validator imports same derivation, so reviewers never calculate or copy those fields manually.
- **Remediation intake:** Assessed non-approval results retain validator-checked `Review Findings and Merge Blocks` table that becomes remediation intake contract.

Historical `.reports/codex/review/` and `.reports/codex/resolve/` artifacts remain readable fallback inputs. `code-review` and `code-remediate` are canonical names.

Use `$codex-rig:assess` (or `$assess` when the alias is available) for evidence-first analysis; it replaces `change-analysis` and retains `--approve-gh`. New artifacts use `.reports/codex/assess/`. Existing `change-analysis` reports remain readable, but the old skill name is no longer registered. Refresh the installed plugin to discover the new name.

</details>

## 🎚️ Calibration

<details>
<summary><strong>Offline and live calibration boundaries</strong></summary>

The packaged runner supports plugin layout directly:

```bash
python3 plugins/codex-rig/runtime/calibration/run.py --layout plugin --root .
```

It validates packaged skills, role cards, shared contracts, behavior fixtures, accepted routing evidence, confidence scoring, and known workflow leaks. The offline CI harness shadows network and LLM commands, uses isolated home, and writes compact failure artifacts without contacting LLM.

Paid live A/B calibration is separate, explicit, and never implied by offline result.

</details>

## 🧾 Approval prompts and commit handoffs

New final handoffs use `presentation_version=2`: plain-English explanation first, no empty Results section, one concise line for checks not run, and each recovery action shown once. Unavailable review explanations bind actual collector failure to retained safe command and checkout evidence; missing cause details stay explicitly unknown. Machine records remain complete, historical rendered bytes stay unchanged, and explicitly requested exact caller output is preserved.

Every skill keeps its normal closing gate after user intervention, recovery, compaction, or repeated invocation. Resume at first unmet checkpoint using still-valid evidence; do not replay completed work or replace required final summary/tables with informal recap. Passing tests or completed notes do not replace validated results. Non-artifact skills retain their own final verification rather than inventing report artifacts.

Review/remediation reports keep one canonical record per finding, not one source per repeated summary/action/confidence mention. New review records add title, issue, required change, evidence and closure criterion under `finding_records_version=1`, and validator requires that marker on every new schema-v2 assessed candidate — there is no bare-record fallback for new writes. Older schema-v1 ID/severity-only records and historical rendering remain readable. Real report/online references stay intact; related report mentions do not inflate source counts, and each comment has one owning item.

All-closed remediation records empty selected/deferred indexes without implying user confirmation. Grouped output keeps complete evidence in named detail blocks; only durable ledgers and historical symbol layouts require visible symbol definitions.

Selection preserves item types for report-gate count reconciliation and rejects output paths that overwrite its input or follow symlinks. Distinct report files can reuse finding IDs; verified cross-file views use explicit shared `report_id`. A shared test or closure criterion alone never merges findings. New local reviews with findings retain same canonical action table as PR reviews, including minor-change outcomes.

Before remediation selection, `shared/final_handoff.py selection --input <run-directory>/selection.json --out-scope <run-directory>/resolution-scope.md` validates inventory and derives counts; `--check` verifies existing bytes. Presentation version 3 uses `# | Severity | Finding | Resolution proposal | Sources`, with compact derived tags such as `report ×1; online ×2`. ID-only supporting groups retain context, acceptance checks and full evidence references once. Pending selection never implies user deferral. New final review/remediation tables use `layout=concise`: review rows include short resolution proposal; remediation rows include bound resolution; details omit repeated title/action/status. Exact machine cells and coverage remain validated. Finding names problem; Context explains it. Installed plugins must be deliberately refreshed to receive this behavior; historical reports are not rewritten.

Review, implementation and management apply same launcher compatibility admission before their strict portable parallel reads. `auto` falls back to serial with recorded reason when required child controls are unavailable; explicit parallel-read stops. Code Review's instruction-bounded native inspection route is exempt from that admission: it supplies full role card first, then scope inventory and relevant source/diff/evidence inline, instructs text-only output with no child tools, repository execution, edits, installation, network, credentials, or escalation, and detects rather than prevents violations. Review categories determine depth, not execution permission. BROAD/HIGH_RISK review prefers independent QA/challenger passes; documented parent-serial substitute may continue inspection without satisfying independence, and expressly required independent pass with missing coverage withholds completion. Requested role settings, inherited parent settings, supported launcher controls and observed effective controls remain distinct; no plan declaration proves sandbox isolation. A workspace-write launcher without verified child overrides remains unsupported for strict portable independent review. Diagnose this as process limitation, not source defect or failed launch; no live compatibility is claimed by offline tests.

Schema-v2 result paths must resolve to run's canonical `result.json`, including while validating `result.candidate.json` before promotion. Confidence gaps are nonblank and unique, with exactly one declared closure each. Assessed handoffs bind `Review Decision` / `Recommendation: <recommendation>.` to structured recommendation; approval requires zero findings and `minor-changes` forbids critical/high findings. Both approving recommendations require passing result status and no failed checks. Historical assessed records used `metadata.review_findings=[{"id":"CR-1","severity":"high"}]` (or `[]`); new records add canonical descriptive fields listed above, with exact per-severity totals and string severity values; malformed JSON types return stable validation error. Optional `operational_blockers=[{"id":"G-1"}]` declares non-finding actions separately. Notes and final action rows cover those stable, unique, disjoint IDs exactly; unknown, missing or duplicate identities fail. Source assessment must establish IDs before table construction. Historical schema-v1 remains readable; schema-v2 producers must supply identity lists rather than silently downgrading.

For every intentional approval request, Codex keeps detailed safety and effects pre-brief separate from runtime prompt. The prompt reason is short plain-English question about outcome or material effect; it never duplicates command syntax, arguments, flags, paths, multiline content, or full pre-brief. Reusable approval uses only justified short categorical safe prefix, while one-time or high-risk commands omit persistent prefix.

Codex-created commit handoffs identify every commit by hash and title, summarize behavior and affected surfaces, list exact verification evidence, disclose residual limits, and explain boundaries between multiple commits. After verified code remediation, Codex presents opt-in choice to commit all owned changes at once, coherent finding topics, or each safely disjoint finding; it leaves changes unstaged when choice is absent, ownership cannot be proved, or units overlap. A pre-edit worktree baseline, clean-index check, explicit-path staging, and exact staged-path comparison prevent unrelated or pre-existing user changes from entering remediation commit. Codex shows complete message in chat, passes it literally as one argument to `rtk git commit --cleanup=verbatim -m <message>`, and verifies stored message afterward. No agent-created message file, draft-file approval, or cleanup; full message can appear in runtime approval details and process arguments. Runtime permissions and hook approvals remain enforced, without persistent prefix rule. Unsupported quoting/encoding or command-size limits stop without hidden file fallback; denial, failure, or message drift stops without automatic retry or history repair.

## 🩺 Optional SessionStart diagnostic

<details>
<summary><strong>Read-only hook behavior</strong></summary>

`hooks/hooks.json` defines read-only diagnostic for `startup` and `resume`. Codex discovers this default plugin hook path after install. The hook runs same shim doctor used by manager; it does not install, update, or remove shims.

Review hook command before trusting it. Declining hook trust leaves diagnostic inactive and does not disable skills.

</details>

## ⬆️ Update or reinstall

Use bundled `$codex-rig:sync` workflow for dry-run state report and approval-gated refresh, or run supported CLI commands directly:

```bash
codex plugin marketplace upgrade borda-ai-rig
codex plugin add codex-rig@borda-ai-rig
codex plugin add bridge@borda-ai-rig
```

Then start fresh Codex session. Direct plugin reinstall does not update `CODEX_HOME` global instructions or reader rules automatically; explicit setup or repository sync is required. Use manager's authenticated `remove` action to clean prior development shims; new installation remains platform-blocked.

Repository sync never restores legacy `.codex/` tree. When Codex scope is active, root `make sync-codex` installs or updates public Codex plugins, authenticated Codex Rig block, and owned GitHub reader rules, then projects repository model defaults and personal policy as described above. The root Make target supplies no direct-script flags, so it always projects `model`, `review_model`, and personal-policy block together. The direct script's `--no-codex-global-agents` opt-out remains valid for leaving `AGENTS.md` unchanged; it does not skip reader-rule installation. Restart existing Codex sessions after sync so updated rule set is observed.

### Legacy project-to-home copies

<details>
<summary><strong>Legacy copy cleanup warning</strong></summary>

Older `sync.sh` versions — script has since been retired and its logic folded into root `Makefile` — copied AI-Rig files into `~/.codex/`. The copied files had no durable per-file ownership marker, so Codex Rig does not delete them automatically. Before manual cleanup, back up home, distinguish AI-Rig copies from user-owned modifications, and remove only files whose ownership you can establish. An old home copy can otherwise expose duplicate unnamespaced skills or stale named-agent registrations beside plugin.

</details>

## 🧪 Experimental agent shims

<details>
<summary><strong>Shim diagnostics and authenticated cleanup</strong></summary>

The manager diagnoses prior development installations and safely removes authenticated standalone TOML files. New installation is platform-blocked because current collaboration tooling does not expose verifiable custom-agent selector. Do not infer selection from matching task name, child path, or file name.

Invoke exactly one action:

```text
$codex-rig:agent-shims doctor
$codex-rig:agent-shims status
$codex-rig:agent-shims install
$codex-rig:agent-shims remove
```

- `doctor`: read-only runtime, active-package, manifest, helper, role-card, and filesystem checks.
- `status`: read-only installed-roster, lifecycle-state, target, and recovery summary.
- `install`: report platform block without creating or relinking files.
- `remove`: plan removal of intact, authenticated Codex Rig shims. No prefix-based cleanup.

`doctor` and `status` are read-only on Windows, macOS, and Linux. Windows verifies package hashes, active selection, executables, and inventory of exact `codex-rig-*.toml` names; it does not authenticate, adopt, or mutate those files. POSIX additionally validates lifecycle state and permissions. A blocked result names failed check and required invariant; it does not authorize repair. The optional SessionStart hook shows first bounded reason and confirms that no files changed. Do not apply recursive permission changes or delete/link-replace evidence from diagnostic alone. Existing POSIX `$CODEX_HOME/agents` directories are accepted when they are real current-user directories without group/world write or special permission bits; lifecycle state and recovery directories remain private mode `0700`.

Prior lifecycle files use authenticated names such as `codex-rig-linting-expert.toml`. `remove` prints exact target root, operations, and SHA-256 approval digest. Review displayed plan. Type that exact digest only after explicit approval. Wrong or missing digest causes cancellation without authorized writes.

Interrupted recognized transactions use separate recovery digest. Approved recovery rolls back partial mutation or finalizes durable committed state. Repeat original action after recovery. Use `remove` to recover prior interrupted transactions; blocked `install` never enters recovery or mutation planning.

</details>

## ⬆️ Uninstall

<details>
<summary><strong>Plugin removal and recovery procedure</strong></summary>

Remove authenticated legacy shims while plugin manager still exists:

1. Run `$codex-rig:agent-shims remove` and approve exact plan when one exists.
2. While installed package is still available, run explicit `sync_codex.py clear` action to remove managed plugins, authenticated global-instruction block, and owned GitHub reader rules; it backs up changed files and preserves unrelated bytes.
3. Run `codex plugin remove codex-rig@borda-ai-rig`.
4. Start fresh Codex session.

Removing plugin first deliberately leaves thin shim files behind. Those shims break because role cards and verifier live in removed plugin cache. They are not auto-deleted.

Recovery: reinstall `codex-rig@borda-ai-rig`, start fresh session, run `doctor`, then run approved `remove`. Compatible historical state can authenticate guarded cleanup. Verification failure remains blocked; no force cleanup is provided.

</details>

## 🧭 Lifecycle safety limits

<details open>
<summary><strong>Fail-closed mutation limits</strong></summary>

- Foreign or marker-only `codex-rig-*.toml` files are never adopted, overwritten, or removed.
- Modified managed shims, concurrent drift, unsafe links/nodes, ambiguous package selection, or changed runtime binaries block mutation.
- Executable hashing is bounded consistently at 512 MiB across manager, generator, and verifier; larger files report selected path, observed size, and limit.
- Missing, malformed, oversized, aliased, or identity-inconsistent lifecycle state blocks cleanup. Manual evidence recovery is required.
- Only one exact recognized interrupted transaction can be recovered. Unknown, conflicting, or multiple residue remains blocked.
- The manager owns only its authenticated roster and state under current user's Codex home; it never cleans unrelated agents.
- Thin shims require active compatible plugin cache. Offline cached use may work; update, reinstall, and active-package validation depend on Codex CLI state.
- Hook trust, plugin install, shim install, and shim removal are separate lifecycle decisions.
- Plugin removal does not edit `$CODEX_HOME/AGENTS.md` or `rules/codex-rig-github-read.rules`; sync-installed managed state remains until explicitly removed.
- The managed reader rules grant only literal `python`/`python3` and installed GitHub reader-wrapper path union; they never grant broad Python or `gh` access or change network settings.
- A successful shim transaction proves file ownership and link integrity, not runtime profile selection.
- Native Windows and network/distributed filesystems are unsupported for shim mutation. Windows workflows, package verification, sync, hooks, and read-only shim inventory remain supported.

</details>

## 🎯 What changed from the idealized design

<details>
<summary><strong>Architecture evidence and remaining platform dependency</strong></summary>

The initial design assumed plugin could bundle named agents with model, sandbox, approval, and nesting controls. Implementation evidence changed that architecture:

1. **Agents became role cards.** The behavioral instructions remain full-fidelity, versioned product assets, but they are no longer presented as directly installable native agents.
2. **Selection became injection.** Skills load exact role card and inject it into runtime blank agent. This preserves parallel specialist reasoning when available without inventing selector that Codex does not expose.
3. **Inline execution became mandatory fallback.** When safe blank-agent route is unavailable, parent applies card serially and reports lost independence.
4. **Thin shims became cleanup-only.** The transaction engine can authenticate and remove development shims, but `install` fails closed until runtime selection is observable and testable.
5. **Project configuration left product.** Models, MCP, and repository runtime defaults belong to user/project configuration. The plugin distributes reusable workflows, roles, hooks, helpers, evidence fixtures, and one inert global-instructions template. Repository sync installs it when Codex scope is active unless explicitly opted out; direct plugin installation alone leaves it inert.
6. **Install identity became immutable.** Released package bytes are tied to SemVer version and manifest hashes; README or code changes require new version rather than same-version cache drift.

This design delivers maintainable part of original goal today and records remaining platform dependency honestly. If Codex later exposes custom-agent selection, named shims can be reconsidered behind fresh runtime probes without changing skill or role-card semantics.

</details>

## 🏗️ Package layout

<details open>
<summary><strong>Installed package topology</strong></summary>

```text
codex-rig/
├── .codex-plugin/plugin.json
├── ARCHITECTURE.md        # parallel split, approvals, gates, evidence, telemetry
├── assets/AGENTS.md        # inert global-instructions template
├── skills/                 # 13 workflows + agent-shims lifecycle manager
├── roles/                  # 15 canonical role cards
├── shared/                 # gates, helpers, orchestration, artifact contracts
├── runtime/calibration/    # fixed, behavioral, and live calibration assets
├── hooks/                  # optional read-only SessionStart diagnostic
├── scripts/                # package, role, and shim lifecycle executables
├── tests/                  # package and cross-platform acceptance tests
└── package-manifest.json   # exact packaged file and role-card hashes
```

The installed cache is immutable input. Workflows never edit their own plugin root or manually patch Codex plugin configuration.

</details>

## 🧪 Development and verification

Pure helper docstrings include deterministic doctests for calibration, telemetry normalization, and review routing. Pytest collects these examples alongside workflow tests; regenerate package manifest after docstring edits because shipped Python bytes are hashed.

<details open>
<summary><strong>Maintainer verification commands and acceptance gate</strong></summary>

From repository root:

```bash
python3 plugins/codex-rig/scripts/build_package.py --update
python3 plugins/codex-rig/scripts/build_package.py --check
python3 plugins/codex-rig/scripts/validate_package.py
python3 -m pytest -q plugins/codex-rig
NO_MKDOCS_2_WARNING=1 python3 -m mkdocs build --strict
```

On Windows, use `python` in place of `python3`; `build_package.py --check`, package validation, calibration, and tests are native. Authoritative manifest regeneration (`--update`) remains POSIX-only because released mode bits are part of package contract.

The package is accepted only when generated manifest is current, every recorded file hash matches, plugin-only copied-tree tests pass, lifecycle safety tests pass, Windows collection and path behavior pass, offline calibration harness passes, and public documentation builds without warnings.

### Hardening checks

The denial gate is deterministic and offline: it validates local JSON Lines transcript, requires one exact `item/commandExecution/requestApproval` callback followed by `decline`, matching resolution and declined completion, rejects output or fallback execution, and requires later local recovery item. Run its focused tests and inspect supported probe interface with:

```bash
python3 -m pytest -q plugins/codex-rig/tests/test_app_server_denial_protocol.py
python3 plugins/codex-rig/tests/app_server_denial_probe.py --help
```

The installed-package-safe gate copies only manifest-declared payload into disposable cache and runs explicit package-safe test selection without checkout context (`Makefile`, `.github`, and `.git`). Run it with:

```bash
python3 -m pytest -q plugins/codex-rig/tests/test_installed_package_gate.py
```

> **CI matrix:** The repository CI test matrix runs complete plugin test suite on Linux, macOS, and Windows with Python 3.10, 3.11, 3.12, and 3.13. The synthetic denial gate and installed-package-safe gate are included in that offline matrix.
>
> **Manual-only boundary:** A live App Server candidate-binding probe is separately authorized and manual; it must not run in CI and does not establish equivalence with desktop approval UI.
>
> **Live manifest:** Its `--live-matrix` form consumes one local three-entry manifest ordered `text-control`, `skill-control`, and `denial`; all entries must use same Codex binary, model, plugin version, timeout, and independently recorded SHA-256 digest of `package-manifest.json`, with operator-prepared isolated non-overlapping Codex homes, workdirs, evidence roots, and output paths.
>
> **Isolation and launch:** The probe verifies full manifest and every declared payload before launch, rejects any candidate or cross-row digest mismatch, and mechanically enforces path isolation; whether Codex home had prior use remains operator precondition and is not inferred from directory contents. It stops at first failure, never accepts collector command, and never installs or retries.
>
> **Failure artifact:** After each process cleanup attempt, live probe atomically records either passing evidence or bounded failing diagnostic containing only allowlisted event names/statuses, run-local identifier aliases, booleans, safe failure codes, sorted schema-owned error categories, first specific category, whether any retry occurred, and final retry state. The diagnostic omits commands, paths, prompts, model output, raw identifiers, error payloads, environment values, and credentials; failing artifact explains protocol shape but never counts as acceptance.
>
> **Interpretation limit:** A passing skill control proves only that host completed turn carrying installed skill input, not semantic skill loading.

</details>

## 📄 License

Codex Rig is licensed under Apache-2.0. See `LICENSE` and `NOTICE`.
