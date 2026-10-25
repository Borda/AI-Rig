# Skill Checks — Duplication (33)

> Scan-root convention for every bash block below: `checks-skills.md` §Scan-root derivation.

## Check 33 — Code block duplication (NxN similarity matrix)

<!-- policy-sibling: plugins/cc_foundry/skills/audit/modes/efficiency.md (Phase B2 Table 2 — same Gate/Score spec) -->

Full-spectrum detection of duplicate or near-duplicate fenced code blocks across all .md files (SKILL.md, agents, rules, templates, modes) — any language (bash, python, sh, perl, ruby, js, etc.). Produces NxN pairwise similarity matrix to surface extraction candidates: 33a within-file (same block 3+ times — bin/ script or helper function candidate); 33b cross-file NxN (same block in 3+ .md files — shared bin/ script candidate).

**Check 33a — Within-file repetition**: delegate to Phase A foundry:curator (has full file context). Curator prompt must include:

> "Extract every fenced code block (any language marker — ```` ```bash ````, ```` ```python ````, ```` ```sh ````, ```` ```perl ````, ```` ```ruby ````, ```` ```js ````, etc.) from this file. For each pair of blocks, compute normalized similarity: strip comments → normalize variable names to `<VAR>` → normalize string literals to `<STR>` → compare structure.
>
> Report any pair with similarity ≥ 0.8 that appears 3+ times (within this file) as a 33a finding. For each candidate: block language, purpose, occurrence count, similarity score, what differs between instances, and suggested extraction (bash function defined once in pre-flight, or `bin/<name>.py` — bin/ scripts are Python only). Context saving estimate: (block_lines − 1) × occurrence_count.
>
> Skip: blocks marked `# audit-skip: resilience-replication` (first line of block) or prose annotation matching 'intentional resilience replication'."

**33b — Cross-file NxN** — two phases: bash quick scan identifies known hotspots; curator NxN delegation runs when clusters found. Scope: all .md files in plugin tree (SKILL.md, agents, rules, templates, modes).

**Phase 1 — Bash quick scan** (known duplication hotspots):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
if [ "$LOCAL_MODE" = "true" ]; then
    _C33_DIR="plugins/"
else
    # latest foundry version dir — skip other cached versions
    _C33_DIR=$(ls -td ~/.claude/plugins/cache/borda-ai-rig/foundry/*/ 2>/dev/null | head -1)
    _C33_DIR="${_C33_DIR:-.claude/}"
fi
printf "=== Check 33b: scope=%s files=%s ===\n" "$_C33_DIR" \
    "$(find "$_C33_DIR" -name '*.md' -type f 2>/dev/null | wc -l | tr -d ' ')"

printf "=== Check 33b Phase 1: Cross-file code block quick scan ===\n"

MODE_DISPATCH=$(grep -rl 'find.*plugins/cache.*-path.*modes/' "$_C33_DIR" --include="*.md" 2>/dev/null | wc -l | tr -d ' ')
[ "${MODE_DISPATCH:-0}" -ge 3 ] && printf "⚠ 33b: bash mode-dispatch pattern in %s files — bin/ extraction candidate: resolve_skill_mode.py <mode>\n" "$MODE_DISPATCH"

SHARED_RES=$(grep -rl '=\$(find.*plugins/cache.*_shared\|=\$(ls -td.*plugins/cache' "$_C33_DIR" --include="*.md" 2>/dev/null | wc -l | tr -d ' ')
[ "${SHARED_RES:-0}" -ge 3 ] && printf "⚠ 33b: bash _shared resolution pattern in %s files (variants may be inconsistent) — bin/ extraction candidate\n" "$SHARED_RES"

PY_HEREDOC=$(grep -rl 'python -c' "$_C33_DIR" --include="*.md" 2>/dev/null | wc -l | tr -d ' ')
[ "${PY_HEREDOC:-0}" -ge 3 ] && printf "⚠ 33b: python -c one-liner in %s files — evaluate if any cluster repeats\n" "$PY_HEREDOC"

# audit-skip: resilience-replication — unsupported-flag-check is intentional per-plugin
FLAG_CHECK=$(grep -rl 'Unknown flag' "$_C33_DIR" --include="*.md" 2>/dev/null | wc -l | tr -d ' ')
[ "${FLAG_CHECK:-0}" -ge 3 ] && printf "ℹ 33b: unsupported-flag-check boilerplate in %s files — known intentional per-plugin resilience\n" "$FLAG_CHECK"

echo "--- Code block language distribution across all .md files (Phase 2 trigger signals) ---"
NEEDS_CURATOR_NXN=false
for lang in bash python sh perl ruby node js; do
  count=$(grep -rl "^\`\`\`${lang}" "$_C33_DIR" --include="*.md" 2>/dev/null | wc -l | tr -d ' ')
  [ "${count:-0}" -ge 5 ] && { echo "  ${lang}: ${count} files — TRIGGER Phase 2 curator NxN"; NEEDS_CURATOR_NXN=true; }
  [ "${count:-0}" -ge 2 ] && [ "${count:-0}" -lt 5 ] && echo "  ${lang}: ${count} files"
done

[ "$NEEDS_CURATOR_NXN" = "true" ] && printf "→ Phase 2: curator NxN delegation triggered — see 33b Phase 2 instructions below\n"
printf "✓: Check 33b Phase 1 complete\n"  # timeout: 5000
```

**Phase 2 — Curator NxN delegation**: run when Phase 1 finds any known pattern in ≥3 files OR any language marker appears in ≥5 .md files. Spawn **foundry:curator** with all flagged files:

> "Perform Check 33b cross-file code block analysis on: \<list all .md files that contain flagged language markers from Phase 1>. For each file, extract every fenced code block (any language) ≥ 5 lines. Skip: blocks marked `# audit-skip: resilience-replication` (first line) or prose annotation matching 'intentional resilience replication'.
>
> **Step 1 — Purpose statements**: for each block, write a one-sentence purpose statement describing what it does functionally (not how) — e.g. 'resolves `_shared/` path from plugin cache', 'detects codex plugin availability', 'emits boilerplate-duplication counts'. Syntactic line-intersection alone is blind to conditional-inversion and variable renaming — purpose grouping catches duplicates that normalization misses.
>
> **Step 2 — Purpose clusters**: group blocks with equivalent purpose. This is the primary grouping. Singletons omitted. Assign cluster ID `C<n>`. **Computational equivalence gate (mandatory before finalizing)**: purpose-wording similarity is necessary but not sufficient — verify cluster members target the same output namespace (e.g. `.reports/audit/*` vs `.reports/research/*` are different namespaces even when both "write a report file"), same inputs, and same side-effect semantics before merging; split into separate clusters when destinations/inputs differ structurally despite similar wording, and never propose renaming/redirecting one site's output to match another's without this equivalence evidence stated in the row.
>
> **Step 3 — Syntactic similarity (secondary)**: within each cluster, normalize: strip `#` comment lines → replace path segments / slugs / numeric literals with `<STR>` → **replace ALL argument/parameter values (flag values, option strings, concrete command arguments) with `<ARG>`** — e.g. `CODEMAP_AVAILABLE=$(find ~/.claude/plugins/cache -name "codemap*" -type d ...)` and `FOUNDRY_AVAILABLE=$(find ~/.claude/plugins/cache -name "foundry*" -type d ...)` both normalize to `<VAR>=$(find ~/.claude/plugins/cache -name "<ARG>" -type d ...)`. Compute `sim(A,B) = 2 × |lines(A_norm) ∩ lines(B_norm)| / (|A| + |B|)`. Record max-sim per cluster. Mark **DUPLICATE** when max-sim ≥ 0.90.
>
> Write Table 1 and Table 2 to `$RUN_DIR/similarity-check33.md` using the Write tool.
>
> Table 1 format: `| Cluster | Block IDs | Files | Lang | Lines each | Purpose | Max-sim | Duplicate? |`
>
> Table 2 format: `| Cluster | ParamSlots | Tokens | Gate | Score | Verdict | Differs-by | Recommended extraction |` **Never-extract list — checked before the gate.** A cluster matching any of these is not a candidate: replacement costs more tokens than the block (a one-command block becomes a longer invocation line); the block is marked `# audit-skip` or declared an intentional replication in prose beside it; its only purpose is echoing values for the model to read; it is one-off glue under ~10 lines in a single file; its body carries model-substituted placeholders (`<changed_files>`, `<TARGET>`) rather than shell variables; it is an `Agent(...)` prompt, an output template, or a prompt string assigned to a shell variable; it is a propagated copy owned by another plugin's MANIFEST entry (extract at the canonical); or its shape is load-bearing for a permission hook — `IFS= read -r VAR < "${TMPDIR:-/tmp}/…-${CSID}"` passes `sentinel-read-allow.js` by shape, and a `$(python …)` replacement reintroduces the prompt it exists to avoid. Record each in Table 1 with its reason and omit it from Table 2.
>
> Where:
>
> - **ParamSlots** = count of distinct `<ARG>` slots after normalization
> - **Tokens** = estimated token count of block
> - **Gate** = `G1:P/F · G2:P/F · G3:P/F` (all must pass or Verdict = HOLD) — G1 (Size OR execution cost): block > 100 tokens, **or** block launches an external interpreter process (subprocess/heredoc/`-c` — python/node/perl/ruby/etc) **and** occurs ≥3× in cluster (repeated process-fork cost is real even when each instance is token-small — a tiny `python -c "..."` one-liner repeated 84× must not gate out on size alone); G2 (Independence): no branch on prior LLM decision that cannot become explicit arg; G3 (Identity): has computational meaning outside orchestration prose (high CallerScopeDeps = G3 fail indicator)
> - **Score** = sum of applicable positive-dimension weights when gate passes — Testable (deterministic I/O, writable pytest/shellcheck test) +2 · Reuse (same logic in 2+ .md files) +2 · Token drain (block > 300 tokens) +2 · Process overhead (external interpreter launched ≥3× in cluster) +2 · Lintable (shellcheck/ruff applicable) +1 · Run frequency (executes >1× per skill invocation) +1 · Standalone debuggable (runnable with no SKILL.md context) +1
> - **Verdict** = HOLD (any gate fail) · LOW (0–1) · MEDIUM (2–3) · HIGH (≥4)
> - **Differs-by** = concrete `<ARG>` slot values varying across instances (become CLI parameters in extracted script)
>
> Return ONLY: `{\"status\":\"done\",\"file\":\"$RUN_DIR/similarity-check33.md\",\"clusters\":N,\"duplicates\":N,\"similar\":N,\"findings\":N,\"confidence\":0.N}`"

Severity: **medium** for actionable extraction candidates (mode-dispatch, \_shared resolution, multi-file python clusters); **low/info** for known intentional replications (unsupported-flag-check, health-monitoring constants — per-plugin resilience by design per `plugins/CLAUDE.md`).

Auto-fix guidance:

- **Bin/ script**: any self-contained block (stdout output, no function defs, no shell state mutation) → `plugins/<plugin>/bin/<name>.py`; callers: `python "${CLAUDE_PLUGIN_ROOT:-plugins/<plugin>}/bin/<name>.py"`. Python only — never `.sh`, which does not execute on native Windows (`plugins/CLAUDE.md` §Installability). Prefer the bare invocation over a `$(...)` capture: the bare form is covered by each plugin's `Bash(python:*)` allow entry, while a capture needs an exact-text entry in that plugin's `blueprint-manifest.json`.
- **Inline function**: for within-skill bash duplication where block uses caller shell state → define bash function once in pre-flight, call at each site
- **Never extract**: blocks explicitly marked as resilience replications (unsupported-flag-check, health-monitoring constants)

| Sub-check | Target | Condition | Severity | Auto-fix |
| -- | -- | -- | -- | -- |
| 33a — within-file repetition | single .md file | same code block (any language) 3+ times, constants only differ | medium | inline helper function or bin/ script |
| 33b — cross-file repetition | 3+ .md files | same block (excluding known resilience replications) | medium/low | bin/ script with fallback chain |
