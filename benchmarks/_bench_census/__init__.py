"""Import-edge census: score codemap-py import edges against a static, execution-free truth.

The census answers one question: on a set of pinned public repositories, how many of the in-repository file-to-file
import edges does a codemap-py index record (recall), and how many of the edges it records are real (precision)?

Modules:
    truth: the static truth edge set, built with the stdlib ``PathFinder`` and never importing repository code.
    score: recall, strict precision, parent-package-adjusted precision, and 95% Wilson intervals.
    classify: rule-based classes for every false negative and false positive.
    report: Markdown tables padded so the repository Markdown formatter leaves them unchanged.
    run: clone pinned repositories, build each index with a scanner checked out at a git revision, write results.

The census is a measurement, not a provider study: it has no A/B/C arms and runs no model.
"""
