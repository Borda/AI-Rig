## Finding Priority and Labelling

1. **Primary findings**: issues matching stated design concern (leaky abstraction, circular dep, missing ADR, compat violation) — list first, no qualification
2. **Secondary observations**: concerns outside stated scope — label "Secondary observation:" explicitly, place after primary findings. Examples: error handling gaps, missing logging, test isolation, doc gaps, perf concerns. Real issues but not primary architectural question.
3. **Never promote secondary to primary** — inflates issue count, obscures main concerns. Orthogonal issues go in "Secondary observations" section.
4. **One finding per root cause** — merge when one fix resolves both, or when B is a consequence of A (e.g. "no deprecation path", "version number understates change", "release mislabelled minor" are corollaries of one compat break). Merged finding lists each affected location/change as a sub-bullet, keeping per-location evidence. Keep findings separate when each needs its own independent fix (e.g. a removed parameter and a changed return type in the same release).
5. **Excerpt artifacts are not findings** — input shown as an excerpt (partial files, snippets, selected modules): names undefined only because their defining module is not shown (routers, loaders, helper calls, imports from unshown files) are assumed to exist. Note once as "unshown in excerpt" when it limits analysis; never file as a defect.

## Severity Rubric

Tag by consequence, not by how wrong the design looks:

| Tier | Consequence | Typical architectural cases |
| -- | -- | -- |
| `[CRITICAL]` | Data loss or corruption, security exposure, irreversible production damage | Destructive migration dropping data before readers move; trust boundary bypassed |
| `[HIGH]` | Runtime breakage or broken public contract | Import cycle blocking startup; public API break without deprecation; core layer bypassing a declared port |
| `[MEDIUM]` | Governance, process or maintainability gap with no runtime failure yet | Missing or contradicted ADR; leaky abstraction with no declared contract; undocumented boundary |
| `[LOW]` | Naming, style, local clarity | Inconsistent naming; minor doc gap |

- Startup-blocking cycles and compat breaks are `[HIGH]`, never `[CRITICAL]` — reserve CRITICAL for data/security/irreversible harm.
- Process and governance gaps (missing ADR, absent decision record) never rise above `[MEDIUM]` on their own.

## Coupling Analysis

Measure fan-in (importers) and fan-out (imports) from `<codemap-context>` below — `codemap-py query rdeps <module>` (fan-in), `deps <module>` (fan-out), `xrefs <module::symbol>` (symbol-level cross-refs, not just import-level). No index → run `/codemap-py:scan-codebase` (requires `codemap-py` plugin) first, or use Grep fallback:

- **Fan-in** fallback: Grep tool (pattern `from mypackage.target import|import mypackage.target`, glob `**/*.py`, path `src/`, output mode `files_with_matches`)
- **Fan-out** fallback: Grep tool (pattern `^from |^import `, file `src/mypackage/target.py`, output mode `content`)
- **Cross-module symbol refs** fallback: Grep on symbol name
- High fan-in = stability required; changes break many things.
- High fan-out = fragile; breaks when dependencies change.
