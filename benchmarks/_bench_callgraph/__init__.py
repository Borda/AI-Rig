"""Call-graph census: score codemap-py ``fn-rdeps`` callers against an independent static reference tool.

Modules:
    truth: definitions, call sites and the call-edge truth, in the caller vocabulary ``fn-rdeps`` reports.
    jedi_backend: the jedi reference backend that resolves each call site to its definition.
    sample: caller-count strata and the seeded stratified sampler.
    score: recall, precision, miss and extra classes, and the adjudication draw.
    run: fetch, index, truth, sample, query and report for each pre-registered repository.
"""
