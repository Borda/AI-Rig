# Severity Map

Use across codex-native skills. Classify the demonstrated consequence and realistic trigger, not the file type or amount of missing evidence. Topic informs impact once; never discount an already impact-based severity again. Missing tests alone do not establish High: a localized coverage gap is normally Medium or Low unless evidence establishes a material violated contract. Keep unavailable verification separate from source-defect severity. These labels are not the five review recommendation ratings.

## Critical

- security issue with exploit path; normalize a `security` result-count label to `critical`
- data loss or corruption risk
- confirmed severe release regression

## High

- demonstrated material failure of a supported core workflow
- a material acceptance-contract violation supported by concrete evidence, not test absence alone
- unresolved migration compatibility break

## Medium

- non-core behavior mismatch
- weak assertion quality, localized missing coverage or edge-case gap without established material failure
- stale docs or partial config drift

## Low

- style/nit issues; normalize a `nit` result-count label to `low`
- wording improvements
- non-blocking cleanup

## Gate Rule

- gate timeout => `status=timeout`
- `critical > 0` => `status=fail`
- `checks_failed` non-empty => `status=fail`
- otherwise `status=pass` only when the confidence, evidence, artifact, and required-check conditions in [Quality Gates](quality-gates.md#fail-rules) are satisfied; confidence below `0.85` cannot pass, and `0.85 <= confidence < 0.9` needs recorded recovery and remaining limits
