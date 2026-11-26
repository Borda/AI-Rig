<!-- file: gap-emission.md — consumers: skills/review/templates/consolidator-prompt.md, skills/review/modes/acceptance-gate.md, skills/resolve/modes/final-report.md; helper bin/gap_emit.py; schema owner bin/gap_ledger.py (contract foundry--gap-ledger.md) -->

# Gap emission — oss:review and oss:resolve

Every run records the gaps of its `## Confidence` block as gap-ledger records (schema `gap-ledger` v1, `foundry--gap-ledger.md`), so a gap counts once per pull request across runs. Ledger home = the run's durable directory: review `$REPORT_DIR` (`.reports/review/pr-<N>/run-<NNN>/`, beside `head-sha.txt`), resolve `$RUN_DIR` (`.reports/resolve/<ts>/`). Never `.temp/`: carry and roll-up need the ledger after the 30-day sweep.

## Prepare

The emission point's fixed block runs `gap_emit.py prepare` before the writer. It writes `gap-identity.json` (repository, PR, head, run id, installed oss version, route), carries an eligible earlier run's open gaps into this run's ledger, and prints:

- `GAP_STAGE=<path>`: the staged file to Write.
- `GAP_CARRIED_LEDGER=<path>` identifies the complete inventory in this run's ledger. Take the latest record per ID and consider every open record with `carried_from`. `GAP_CARRIED=<id> · <class> · <summary>` lines list that inventory in `gap-prepare.txt`. The console previews up to 50. If a gap still holds, leave its carried record open. If this run's evidence closed it, stage the same ID and text with `"status": "closed"`, zero or null deduction and the closing evidence path.
- `GAP_KNOWN_LIMIT=<id> · <hardware class> · <reason>`: a limit for this repository and host class. When the current evidence still shows that absence, stage the hardware gap and show it once under Review Confidence (`known limit: <reason>`), outside deducted **Gaps** lines. Append applies the registered reason and clears the deduction.
- `⚠ GAP_LEDGER=unavailable (<reason>)`: stage nothing, skip the append; name `gap ledger not written: <reason>` once under Review Confidence (resolve: under Confidence). A writer that finds no `gap-prepare.txt` (prepare never ran) does the same with reason `prepare did not run`.
- `⚠ known-limits file ignored (<path>): <error>`: the file is invalid and enforced nowhere; every hardware gap stays deducted. Name it once under Review Confidence.
- `⚠ GAP_CARRY=…`: an earlier ledger could not supply carry evidence. Use the carried inventory this run actually has and record new observations from current evidence.

## Stage

Write `GAP_STAGE` with the Write tool: one JSON object per line, one line per gap, gap fields only. `append` fills `schema`, `schema_version`, `subject` and `run` from `gap-identity.json`; never type them.

- One record per **Gaps** line of the report's `## Confidence` block, plus one per `Unreviewed dimension:`, `not reproducible on this host:` or `known limit:` observation under Review Confidence. Stage each observation once.
- `id`, `class`, and `owner`: use the Canonical gap IDs table in `foundry--gap-ledger.md`. You can query a concrete ID with `gap_ledger.py lookup <id>`. For an uncatalogued gap, reuse a stable prior ID and use the contract's general class table.
- Hardware: include the observed `hardware_class` from the closed set and exact `reason`. Append fills `host_class` and applies known limits. A hardware test that did not run is insufficient evidence of absent hardware. A missing, unprovisioned or wrong package or build (CPU-only wheel, uninstalled module) is never `hardware-absent`, even when it names CUDA; it never matches or registers a known limit.
- Implementation challenge not run (approval denied, route unavailable, user declined): `id` `implementation-challenge-not-run`, class `process`, owner `workflow`, with its deduction. Never a known limit, never a stop.
- `summary` is the gap line's text after the deduction, one line, ≤300 characters. For a physical-absence waiver, use the direct observation or a neutral coverage status such as “Accelerator tests skipped.” Keep report labels such as `known limit:` and `not reproducible on this host:` in the report. Preserve any cause or qualification that changes what the observation proves.
- `closable_by` is the action that closes the gap. `evidence` is a relative path inside the ledger's own directory to the file showing it (`review-report.md`, `review-validation.json`, `lineup.md`, `resolve-report.md`). Use durable evidence outside `.temp/`.
- `deduction`: the line's `(-0.NN)` as a number (`0.05`); `null` for a line without one.

Never record: the host operating system or a single Python version (facts of the run); a missing package without a failed provisioning step and its cause; a gap that was closed before any ledger event for its ID in the current run (cite under Review Confidence only); the documentation-only `(-0.00) full test suite not run` line (scope fact, weight 0); a push the user declined (`skipped-by-user`).

An observed hardware absence without a matching listed limit stays deducted in this review. Append tries to register it for later reviews on this host class and reports the result.

## Append

Run the emission point's append block (`gap_emit.py append --dir <run dir>`):

- `GAP_LEDGER=appended N → <path>` or `GAP_LEDGER=nothing staged` → done.
- `GAP_KNOWN_LIMIT_REGISTERED=<id>` reports a new host-scoped registration. `⚠ GAP_KNOWN_LIMIT_REGISTRATION=skipped` reports a best-effort write failure. Continue in either case.
- `GAP_LEDGER=refused` (exit 1) with errors → correct the named records in the staged file with the Write tool, run the block once more. Refused again → leave the staged file, name `gap ledger refused: <first error>` once under Review Confidence, continue. Never a stop; never edit `gaps.jsonl` itself.
- A second writer in the same run (re-spawned consolidator) finds this run's records already appended: stage only new ids, changed text for an existing id, and `status: closed` for an id the report no longer lists.
