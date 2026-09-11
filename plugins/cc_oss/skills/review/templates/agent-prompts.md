<!-- file: agent-prompts.md — consumers: plugins/cc_oss/skills/review/SKILL.md (Step 2 agent launch) -->

**Finding evidence standard — applies to every agent, every finding:** Every finding must cite `file:line` from diff. Training knowledge never sufficient. External standard claims (OWASP, PEP, CVE) cite authoritative document. Tier 2 sources (blog, tutorial, forum) need ≥3 genuinely independent origins OR experimental validation; N posts citing same original = 1 source. Citation tracing mandatory: for each Tier 2 source, follow its citations one level; if tracing reveals Tier 1 source (official doc, CVE, spec) confirming claim, treat as Tier 1 verified (sufficient alone); if multiple Tier 2 sources share one origin, merge into one; count distinct origins only. Distinct-origin count < 3 and no experiment → downgrade to LOW or drop; never raise MEDIUM/HIGH/CRITICAL on Tier 2 alone.

**Spawn label — first line of every agent prompt below.** FleetView shows the prompt's leading chars as the agent's row, truncating the tail. Every agent here reviews the same PR, so the PR is not the label — the dimension is. Lead with the dimension, ≤10 words, no verb, no boilerplate; append `— PR <N> <owner>/<repo>` only if it fits one terminal line. Merged units name both dimensions (`perf + API design`), the issue agent names its issues (`issues 41, 44 — root cause`), the consolidator names its job (`consolidate 5 review files`). Everything below — preamble included — comes after that line.

**Run-dir resolution preamble — insert after the spawn-label line, ahead of the rest of the prompt:**

> "First run Bash `export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"; cat "${TMPDIR:-/tmp}/oss-review-run-dir-${CSID}"` (plain `cat`, no `$()` command substitution — substitution triggers Claude Code's compound-command permission prompt even when `cat` itself is allow-listed) to read the exact run-dir path as plain text. Treat that text as `$RUN_DIR` for every file you read or write below — substitute it literally, never retype by hand (the leading `.` in `.temp` is easy to drop, which scatters output into a stray `temp/` dir)."

Every agent prompt must end with:

> "Write your FULL findings (all sections, Confidence block) to `$RUN_DIR/<agent-slug>.md` using the Write tool — where `<agent-slug>` uses hyphen separator (no colon), e.g. `foundry--sw-engineer.md`, `foundry--qa-specialist.md`, `foundry--perf-optimizer.md`, `foundry--doc-scribe.md`, `foundry--linting-expert.md`, `foundry--solution-architect.md`. Colons invalid in macOS filenames. Return to caller ONLY compact JSON envelope on final line — nothing else after it: `{\"status\":\"done\",\"findings\":N,\"severity\":{\"critical\":0,\"high\":1,\"medium\":2},\"file\":\"$RUN_DIR/<agent-slug>.md\",\"confidence\":0.88}`"

**Codemap-py context preamble (substituted by orchestrator)**: when `codemap_available=true`, every dimension-agent prompt (Agents 1–6) prefixed with `## Structural Context (codemap-py, codemap_available=true)` block from `$RUN_DIR/codemap-context.md`. Agents must read that block first, skip redundant Grep/Read on symbols already covered by codemap output. Block absent → fall back to current file-read behaviour. Challenger (Agent 7) unchanged.

**Merged spawn units — each spawn costs ~120,851 tok fixed overhead, so paired dimensions share one spawn:**

- **Agents 3+6 = ONE `foundry:perf-optimizer` spawn** (opus) covering Performance + Architecture/API design — spawn when either dimension survives scope preselection; the prompt includes only the surviving dimensions' instructions.
- **Agents 4+5 = ONE `foundry:doc-scribe` spawn** (sonnet) covering Documentation + Linting — same rule.
- Agents 1 (sw-engineer), 2 (qa-specialist), 7 (challenger), 8 (cicd-steward) stay standalone spawns. Agent 2 is **pinned outside the fanout cap** on CODE PRs — the security-scan-every-PR contract below never gets ranked out.
- A merged spawn writes **one file per covered dimension** (each with its OWN full sections + Confidence block — never blended): `foundry--perf-optimizer.md` + `foundry--solution-architect.md`, or `foundry--doc-scribe.md` + `foundry--linting-expert.md`. Downstream contracts (consolidator filename list, section taxonomy ownership, Step-4 cross-validation "same type as origin", `/oss:resolve` parsing) key on those files and stay unchanged.
- Merged-spawn envelope = JSON array, one element per dimension file, same per-element schema as below. The monitor `.expected-files` list carries every file the spawn will write.

**Agent 1 — foundry:sw-engineer**: Review architecture, SOLID, type safety, error handling, code structure. Check Python anti-patterns (bare `except:`, `import *`, mutable defaults). Flag blocking vs suggestions. `codemap_available=true`: read `rdeps` first (importer list per changed module) — skip importer-walk Reads on listed modules; verify only when needed for specific finding.

**Reuse audit**: Before accepting new helper, utility, or class introduced in diff, search for existing equivalents: `Grep` with semantic function-name patterns across `src/`; if `SEMBLE_ENABLED=true`, also call `mcp__semble__search(query="<function purpose>", repo=<git_root>, top_k=10)`. Near-duplicate found → flag as MEDIUM: "existing utility at `<path>` covers this — reuse or extend instead of reimplementing."

**API-consistency audit** (any diff hunk touching public API surface — new/changed function, method, class, constant, param, flag, return shape, or module placement; NOT gated on `__init__.py` churn, so it fires for new kwargs on already-exported functions too): for each public symbol added or changed, `Read` the ACTUAL surrounding surface from source — the existing function/class it lives beside, its siblings' signatures, the module it sits in — and validate the change against the established API principles, not in isolation:

- **Params / discriminators**: new boolean/flag that overlaps an existing discriminator/enum param (`kind=`, `mode=`, `type=`, `backend=`, `format=`) → **HIGH**: "adds parallel `<flag>` while `<existing>=` already discriminates — extend the existing enum (`<existing>="<value>"`) instead". Canonical miss: `tensorrt: bool` added when `kind="onnx"` already exists → should be `kind="tensorrt"`. New param inconsistent with sibling ordering/default conventions → **MEDIUM**.
- **Naming**: new function/method/class/constant name that breaks sibling conventions (verb-noun form, casing, prefix/suffix pattern, `get_`/`is_`/`to_` idioms in the same module) → **MEDIUM**; a name that duplicates or shadows an existing public symbol's meaning → **HIGH**.
- **Organization / placement**: symbol added to the wrong module/class, or duplicating capability that already lives elsewhere in the package, or bypassing an established factory/registry/dispatch entry point → **MEDIUM–HIGH** (flag: reuse/extend the existing home instead).
- **Return / type shape**: return type or structure inconsistent with sibling functions doing the same job (one returns a dataclass, the new one a raw tuple) → **MEDIUM**.
- Any API-shape suggestion YOU emit must itself be checked against the read surface — never propose a name, param, or placement without confirming it does not duplicate or contradict an existing one.

**Error path analysis** (new/changed code): For each error-handling path introduced or modified, produce table:

| Location | Exception/Error | Caught? | Action if caught | User-visible? |
| -- | -- | -- | -- | -- |

Flag rules:

- Caught=No + User-visible=Silent → **HIGH** (unhandled error path)
- Caught=Yes + Action=`pass` or bare `except` → **MEDIUM** (swallowed error)
- Cap 15 rows. New/changed paths only.

Load `<REVIEW_SKILL_DIR>/checklist.md` via `cat` (not the Read tool — version-pinned cache path) — apply CRITICAL/HIGH patterns as severity anchors. Respect suppressions.

`ISSUE_NUMS` non-empty: read `$RUN_DIR/issue-*.md`. Evaluate whether changes address root cause, not just symptom. PR addresses symptom only → `[blocking] HIGH — root cause misalignment`. PR description diverges from issue problem → `HIGH — PR/issue scope divergence`.

**Agent 2 — foundry:qa-specialist**: Audit test coverage, run quick security/vulnerability scan. Find untested paths, missing edge cases, test quality issues. Check ML-specific issues (non-deterministic tests, missing seed pinning). List top 5 missing tests. `codemap_available=true`: read `uncovered` + `mock-rdeps` sections first — symbols listed in `uncovered` lack any test rdep; symbols listed in `mock-rdeps` tested via mock (not falsely "untested"). Skip manual grep/Read of `tests/` for symbols codemap already classifies; fall back to file reads only when codemap output empty for symbol needed or verifying specific finding.

**Security scan (runs on every PR — not conditional)**: Check OWASP Top 10 — SQL injection, XSS, insecure deserialization, hardcoded secrets/tokens, missing input validation, path traversal. Run `pip-audit` if `requirements*.txt`, `pyproject.toml`, or any `*.lock` in diff. Surface dep CVEs as HIGH; secrets as CRITICAL.

Also check explicitly: concurrent access to shared state; methods called in wrong order; resource cleanup on exception; boundary conditions for division/empty collections/zero-count inputs; type-coercion boundary inputs (`int()`, `float()`, `datetime` parsers — empty strings, None, very large values, float-string for int parser).

**Consolidation rule**: One finding per test gap with concise scenario list. Format: "Missing tests for `parse_numeric()`: empty string, None, very large integers, float-string for int parser." ≤5 items.

`ISSUE_NUMS` non-empty: read `$RUN_DIR/issue-*.md`. Check tests cover linked issue reproduction scenario. Issue has minimal repro/trace not covered by tests → `HIGH — issue reproduction not tested`.

**Agent 3 — foundry:perf-optimizer** (merged spawn with Agent 6 — see Merged spawn units): Find perf issues. Algorithmic complexity, Python loops that should be NumPy/torch ops, repeated computation, unnecessary I/O. ML code: DataLoader config, mixed precision. Prioritize by impact. Findings to `foundry--perf-optimizer.md`.

**Agent 4 — foundry:doc-scribe** (merged spawn with Agent 5 — see Merged spawn units): Check doc completeness. Public APIs without docstrings, missing Google style sections, outdated README, CHANGELOG gaps. Verify examples run. `codemap_available=true`: read `undocumented` + `xrefs --broken` sections first — `undocumented` enumerates symbols missing docstrings; `xrefs --broken` enumerates stale Sphinx refs. Skip docstring-scan Reads on listed symbols; fall back to file reads only when codemap output empty for symbol needed or verifying specific finding.

- **Algorithmic accuracy check**: Functions computing math results — verify docstring claims match implementation. Output shape/length match? Standard name (e.g. "moving average") matches behavior? Deviates from convention → MEDIUM (docstring must document deviation).
- **Deprecation check**: Check stdlib deprecated usage in public API surface only (skip private functions/classes/modules starting with `_`). E.g., `datetime.utcnow()` deprecated since Python 3.12 (use `datetime.now(datetime.UTC)` on 3.11+ or `datetime.now(tz=timezone.utc)` for all versions), `os.path` vs `pathlib`. Flag deprecated usage as MEDIUM with replacement. Route to `foundry:linting-expert` if ruff/mypy can catch automatically — avoid duplicate findings.

**Agent 5 — linting dimension (rides the Agent 4 doc-scribe spawn; standalone `foundry:linting-expert` only in DOCS_TYPING/TESTS_CI simplified modes)**: Static analysis. Check ruff/mypy pass. Type annotation gaps on public APIs, suppressed violations without explanation, missing pre-commit hooks. Flag mismatched Python version. Findings to `foundry--linting-expert.md` with its own Confidence block.

**Security scan ownership**: Agent 2 owns all security/vulnerability scanning — runs on every PR unconditionally. Agent 1 adds supplementary security scrutiny only when diff explicitly touches auth, input parsing, or serialization logic. No separate security agent spawn.

**Agent 6 — architecture dimension (rides the Agent 3 perf-optimizer spawn)**: Covers FEATURE, MIXED, and REFACTOR scope. Public-API PRs (diff touches `__init__.py` exports, Protocols/ABCs, new public classes, **or changes the signature of any already-exported public function — added/removed/renamed params, new flags**): evaluate API design, coupling, backward compat, and consistency of any added symbol (name, placement, signature, param/flag, return shape) with the existing API surface — naming conventions, module organization, sibling patterns (e.g. a new bool that duplicates an existing `kind=`/`mode=` discriminator, or a helper added where an equivalent already lives → flag, reuse/extend the existing home instead). **Backward-compat caveat for removals**: only flag removed export as requiring deprecation period if present in latest published release (`git describe --tags --abbrev=0`). Exports added after latest tag were never released — clean removal acceptable. REFACTOR-scope PRs: evaluate module boundaries, coupling/cohesion, whether restructuring introduces architectural debt. Findings to `foundry--solution-architect.md` with its own Confidence block.

**Agent 7 — foundry:challenger (skip only if `CHALLENGE_ENABLED=false` — pass `--no-challenge` to opt out)**: Adversarial review of design decisions. Attacks assumptions, missing edge cases, security risks, architectural concerns, complexity creep with mandatory refutation step. File-handoff: output to `foundry--challenger.md`. Severity mapping: Blockers → critical/high; Concerns → medium; Nitpicks → low. **Include `--no-codex` in this agent's instructions** — its own internal Codex pre-flight (`challenger.md` step 1–2) duplicates the review's own `rev-codex`/bridge co-review line (spawned separately when `CODEX_AVAILABLE=1`); skip challenger's internal Codex call here, keep the review's dedicated Codex slot as the sole Codex pass.

**Agent 8 — oss:cicd-steward (CI/CD-only mode and docs+CI/CD mode)**: Review CI/CD config changes. Check: correctness (valid YAML/syntax, correct job ordering, trigger expressions), security (pinned SHA for third-party actions, no secret exposure in logs, `permissions:` scopes minimal), best practices (cache keys, matrix strategy, workflow topology), breaking changes to existing CI behavior (removed jobs, changed required checks). Write findings to `$RUN_DIR/oss--cicd-steward.md`.
