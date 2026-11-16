# Import-edge census — 2026-11-16

Baseline for `run-import-census.py`: codemap-py at `77d2d79b8e3b3ddc6bc0ff6c750a71047171c64b` (index generation 15) scored against the static truth on the five repositories pinned in `import-census-repos.json`. Machine-readable record with every disagreement edge and its class: [`2026-11-16-import-census.json`](2026-11-16-import-census.json).

Command: `python benchmarks/run-import-census.py --scanner-ref HEAD`, with `HEAD` resolving to `77d2d79b8e3b3ddc6bc0ff6c750a71047171c64b`. No model ran and no repository code was executed: each repository was fetched at its full pinned commit into its own empty directory, the truth came from `ast` plus the stdlib `PathFinder` over explicit search paths, and the index came from codemap-py's static scanner checked out at the named revision in a throwaway worktree.

Truth rules: universe is every `.py` file outside build, dist, virtualenv, site-packages and VCS directories; search paths are the repository root, `src/` when present, and a script's own directory for a file outside every package; an edge targets the module the statement names; imports inside functions, `try` and `TYPE_CHECKING` count; parent packages Python executes on the way are not edges. A codemap-py edge is a module's `direct_imports` entry naming another indexed module. Intervals are 95% Wilson.

| Group    | Repository             | Sources | Truth edges | codemap-py edges |  FN |  FP | Recall [95% CI]          | Precision [95% CI]       |
| -------- | ---------------------- | ------: | ----------: | ---------------: | --: | --: | ------------------------ | ------------------------ |
| held-out | yt-dlp                 |    1124 |        3831 |             3858 |   0 |  27 | 1.000 [0.999, 1.000]     | 0.993 [0.990, 0.995]     |
| held-out | stable-diffusion-webui |     213 |         866 |              867 |   0 |   1 | 1.000 [0.996, 1.000]     | 0.999 [0.993, 1.000]     |
| held-out | spec-kit               |     673 |        2431 |             2536 |   0 | 105 | 1.000 [0.998, 1.000]     | 0.959 [0.950, 0.966]     |
| held-out | **pooled**             |    2010 |        7128 |             7261 |   0 | 133 | **1.000 [0.999, 1.000]** | **0.982 [0.978, 0.985]** |
| tuned    | pytest                 |     274 |         852 |              880 |   0 |  28 | 1.000 [0.996, 1.000]     | 0.968 [0.954, 0.978]     |
| tuned    | werkzeug               |     142 |         443 |              471 |   0 |  28 | 1.000 [0.991, 1.000]     | 0.941 [0.915, 0.959]     |
| tuned    | **pooled**             |     416 |        1295 |             1351 |   0 |  56 | 1.000 [0.997, 1.000]     | 0.959 [0.947, 0.968]     |

Disagreement classes:

| Group    | Repository             | Parent-package FP | Other FP | Script-directory FN | Non-package-path FN | Other FN | Parent-package-adjusted precision [95% CI] |
| -------- | ---------------------- | ----------------: | -------: | ------------------: | ------------------: | -------: | ------------------------------------------ |
| held-out | yt-dlp                 |                27 |        0 |                   0 |                   0 |        0 | 1.000 [0.999, 1.000]                       |
| held-out | stable-diffusion-webui |                 1 |        0 |                   0 |                   0 |        0 | 1.000 [0.996, 1.000]                       |
| held-out | spec-kit               |               105 |        0 |                   0 |                   0 |        0 | 1.000 [0.998, 1.000]                       |
| held-out | **pooled**             |               133 |        0 |                   0 |                   0 |        0 | 1.000 [0.999, 1.000]                       |
| tuned    | pytest                 |                28 |        0 |                   0 |                   0 |        0 | 1.000 [0.996, 1.000]                       |
| tuned    | werkzeug               |                23 |        5 |                   0 |                   0 |        0 | 0.989 [0.975, 0.995]                       |
| tuned    | **pooled**             |                51 |        5 |                   0 |                   0 |        0 | 0.996 [0.991, 0.998]                       |

Reading:

- Held-out recall is 7128/7128 and every held-out false positive is an implicit parent-package `__init__.py` edge, which codemap-py records by design and the truth excludes by definition. The one stable-diffusion-webui false positive (`modules/sd_samplers_timesteps_impl.py` to `modules/models/diffusion/uni_pc/__init__.py`) is the parent package of a module now resolved through its project-root-qualified path.
- The five tuned "other" false positives are werkzeug `examples/` files importing siblings by bare name from inside a regular package directory; the truth does not put `examples/` on its search path. They are a truth limitation, not confirmed scanner errors.
- spec-kit: codemap-py indexed 669 of 673 universe files; the four unindexed files carry no truth edge. No codemap-py edge pointed outside the truth universe and no source failed to parse.

Reproduction of the earlier one-off measurement: `--scanner-ref 31c2c6165bb0329b5da2f67ab09e5f89c7663be8` (index generation 14, the scanner before the script-directory and project-root-qualified resolution rules) yields held-out 7128 truth edges, 7223 codemap-py edges, 37 FN (35 script-directory, 2 non-package-path) and 132 FP, recall 0.995 [0.993, 0.996] and precision 0.982 [0.978, 0.985]; tuned 3 FN and 56 FP.

Limits: one snapshot of five repositories; the truth is static and differs from a resolver that installs each repository and calls `find_spec`; classes are structural rules, not hand verdicts.
