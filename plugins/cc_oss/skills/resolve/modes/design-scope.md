<!-- file: design-scope.md — consumers: dispatch-phase3.md (cited rationale; never loaded at runtime) -->

# `/oss:resolve` Step 8 — design scope and residual limitations

Worktree isolation + the two grouping tiebreaks + centrality ordering *reduce* Phase 3 conflicts; they don't eliminate them. Phase 3's cherry-pick conflict path (Step 5a) is the catch-all for whatever slips through.

**Deliberate design choices (not limitations):**

- **Python-scoped semantic grouping** — codemap indexes `.py` (by design — the plugin's stated scope). Same-file *textual* conflict on `.yaml`/`.toml`/`.github/*.yml`/`.md` is still caught: the file-ownership tiebreak is path-based, not codemap-based, so it works for any language. Only the *semantic* layers (import-coupling, centrality) are Python-scoped; non-Python items skip them (no coupling merge, centrality 0 → ordered last). Config/CI PRs keep full textual safety.
- **Depth-1 coupling** — coupling merges only directly-importing pairs, not transitive A→B→C. Deliberate: every coupling-merge trades parallelism for conflict-safety; a direct import is a high break-risk (good trade), a transitive one is a rare break at the *same* parallelism cost (bad trade) — and a central module's transitive closure would collapse the whole batch into one group, defeating the parallelism the redesign exists for. Direct-only is the optimum, not a shortfall.
- **Import centrality, not call centrality** — ordering weight is module `rdep_count` (import graph), matching the module-granularity of the grouping. `fn-central` (call graph) is finer than the unit being ordered, so it wouldn't change whole-group order.

**Residual limitations (true gaps, all backstopped by Step 5a):**

- **Centrality ≠ semantic-break cure** — most-central-first ordering cuts conflict *cascade* and yields saner intermediate trees, but a dependent commit already contains its call to the old contract; landing order can't un-break it. The actual mitigation for semantic breakage is the per-agent blast-radius context (each specialist is told its callers); centrality is only ordering.
- **Stale index** — coupling + centrality read whatever codemap index exists. Currency is gated at skill entry (SKILL.md Gate B refreshes a stale index before Step 8), so mid-run staleness is the only exposure, and it only skews grouping slightly (all heuristic, never dangerous). `central` auto-build exceeding the 15 s budget → maps empty → plan falls back to priority order.
- **Mutex is advisory** — the branch lock stops a *second oss:resolve*, not a human `git push` or an unrelated tool writing the tree; that class is detected after the fact by the Phase 3 HEAD-fingerprint warning + Step 5a, not prevented.
