# Adversarial Loop Evidence

Use existing Code Review routing and specialist-manifest validation for independent execution evidence. The loop adds bindings between that evidence, source contents, returned findings, current source; it does not create or promote a new execution route. Read each helper's `--help` before invocation.

## Capture source

Use `shared/collect_diff.py` snapshot mode with an explicit repository and bounded literal repository-relative scope paths. Retain one source JSON file per review round, a fresh current-source JSON before acceptance. Keep artifacts outside captured source scope, or in an ignored report directory, so writing evidence does not change the source being checked.

Snapshots record repository, normalized scope, actual `HEAD`, staged-index digest, each selected tracked or nonignored untracked file's kind, mode, bytes digest, encoding, content. Deleted files remain visible. UTF-8 source is readable in the snapshot; base64 binary content needs appropriately disclosed review limits. Scope omissions and excluded paths stay explicit in the review context/report. Never send credentials or sensitive source to a route that doesn't permit it.

Collect the source snapshot and supporting diff from the same state. Include the snapshot's complete retained JSON bytes and the complete diff in every participating reviewer's frozen context, not merely their hashes or paths. Complete source is authoritative for inspection; the diff is supplementary, may omit untracked content. The validator binds the supplied diff to the authenticated review, not to a reconstructed Git patch. Request fresh evidence if context or source changes. Current acceptance recaptures the same repository/scope, rejects drift in current contents, index, file inventory, revision; clean acceptance also requires equality with the final independently reviewed source.

For App Server, apply Code Review's [transport and capacity checks](../code-review/app-server-review.md#freeze-and-execute) before paid dispatch. Larger contexts retain this exact complete-source contract whether delivered directly or loaded byte-exact into the same thread's history before its short trigger turn. History delivery requires the adapter's validated acknowledgement/context-digest binding; short trigger text alone is not source evidence. Successful byte admission, history loading or a requested token window does not establish model capacity or review completeness. Missing, changed, partial, or substituted source remains rejected at the existing evidence gate.

## Bind existing reviewers

Keep `loop-evidence.json` alongside `loop-ledger.json`:

```json
{
  "schema_version": 1,
  "repository": "absolute path of the reviewed repository",
  "scope_paths": ["src/parser.py"],
  "current_source_path": "current-source.json",
  "rounds": [
    {
      "index": 1,
      "source_path": "round-1-source.json",
      "review_run": "path of the existing Code Review run",
      "role": "challenger"
    }
  ]
}
```

Each entry corresponds to exactly one ledger round; source and report paths stay inside the loop run. The referenced review run retains its original manifest, routing, source contexts, reports, runtime evidence. Revalidate it with Code Review's manifest-only validator; no substitute or unsupported legacy route qualifies as independent coverage. All participating required reviewers must have inspectable source and independent evidence; selecting one role does not hide another role's missing coverage.

The implementation author must match both the active host `CODEX_THREAD_ID` and the validated review parent thread; every participating reviewer must differ from that author. Read runtime identity from the executing host, never from candidate artifacts or a manually overridden environment. Missing identity or a borrowed review from another parent prevents accepting that round. Resume under the genuine owning session or obtain a fresh permitted review under the current owner; never rewrite old receipts. Parent-only source inspection may continue with its independence limit disclosed.

The selected role's observed thread identity must match the ledger reviewer. The ledger report is an exact copy of its validated returned output. The review manifest input digest, retained review diff, loop round diff must match. Preserve the runtime route's original limits: native lineage and parent-observed App Server evidence are not interchangeable claims. Native inspection schema 5 and App Server schema 4 are supported; older schemas require a fresh supported review, not relabeling. No fabricated host declarations, output hashes, or manual accepted receipts.

## Bind returned findings

For App Server reviews, require one raw JSON object with the fields below, without Markdown fences or surrounding prose; the shared runner enforces this shape through `turn/start.outputSchema` and validates returned content. Native inspection retains exactly one fenced `adversarial-loop` block, preceded only by its required provenance header. Historical fenced App Server responses remain readable. For this loop, the structured response replaces the role's free-form report sections; its review responsibilities and execution restrictions remain unchanged. No introduction, conclusion, additional fence, or finding prose may appear outside the response. Rejected responses remain intact; request a fresh conforming response only through the owning workflow's permitted recovery, never edit the original output.

```adversarial-loop
{
  "source_sha256": "SHA-256 of exact retained source JSON bytes",
  "diff_sha256": "SHA-256 of exact retained diff bytes",
  "findings": []
}
```

Supply those computed digests and the exact response contract in the frozen context; never ask the reviewer to guess or calculate digests mentally. Each finding uses the ledger's existing `signature`, `tier`, `structural`, `disposition`, `evidence` fields. Put all finding narrative, refutations, closure evidence inside those records. Any material missing-input or coverage gap preventing acceptance is a finding, not an omitted prose caveat. Retain coverage and route limits in `loop-report.md`, source snapshots and original execution evidence. Carry previous signatures into the review request, verify closure explicitly. The union of returned findings must match the ledger round; contradictory records for the same signature require reconciliation and cannot be silently discarded. An authenticated report that still finds a defect cannot become an empty clean ledger by parent editing.

When reviewers return the same signature with matching tier, structural flag and disposition, keep one ledger record, combine all exact evidence strings in manifest/reviewer order, removing exact duplicates only. Different evidence wording alone is corroboration, not a verdict conflict. Any disagreement in tier, structural classification or disposition still fails closed; never alter original outputs to resolve it.

Run this skill's `validate_evidence.py` before final promotion; shared validation repeats it. A rejected receipt remains rejected evidence. With no permitted independent review, retain the failed attempt separately, use an empty round list and `independence-unavailable`, fail the review gate, provide concrete recovery. Never fabricate an independently reviewed round to produce a complete-looking artifact.
