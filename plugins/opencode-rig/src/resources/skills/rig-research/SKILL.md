---
name: Rig Research
description: Build a source-backed research brief with labeled claims, contradictions, and explicit limits.
---

# Rig Research

> Read [Evidence rules](../../references/evidence.md).

> This workflow reads and cites. It never changes product code; a change that follows from the research goes to `rig-plan`.

## Modes

| Mode | Use it for | Required output |
| -- | -- | -- |
| `docs` | API contracts, configuration, and migration behavior | Answer per question with version and retrieval date |
| `sota` | Compare approaches, libraries, or designs before choosing one | Criteria table, weighted trade-offs, recommendation |
| `paper` | Analyze a paper's method, data, metrics, and threats | Claim table, method summary, reproduction cost |
| `code-fidelity` | Check whether an implementation matches its paper or spec | Claim-to-code map with missing and divergent items |

## Workflow

1. **Frame the decision.** Write the question the research must answer and the decision it informs. A brief that does not change a decision is not worth the run.
2. **Start the run.** `rig_evidence` with `action: "start"`, `skill: "rig-research"`, and the paths the answer will affect.
3. **Rank sources.** Primary: official documentation at the exact version, source code, the paper itself, specifications. Secondary: maintainer release notes, issue threads, benchmarks with published method. Reject unattributed summaries and content farms.
4. **Record every claim as one of three states.** `sourced` with the URL or identifier and retrieval date, `inferred` with the reasoning chain, or `unknown`. A claim nobody can check is `unknown`, not a fact.
5. **Cross-check decision-changing claims.** Use at least two independent sources, or one primary source plus a reproduction. When sources disagree, report the contradiction and its likely cause; never average it away.
6. **Bind the sources.** `rig_evidence` with `action: "record"` and `inputs` for local sources, `limits` for paywalls, login walls, unreachable hosts, or version drift, and `decision` for the recommendation.
7. **Verify independently.** Dispatch `rig-scientist` with the claims and sources. It must fail a claim that the cited source does not support. Record it with `reviewer`.
8. **Finalize.** `rig_evidence` with `action: "finalize"` and the honest confidence.

## Paper and code-fidelity detail

- For a paper, state problem, method, data, baselines, metrics, ablations, compute, and threats to validity. Report what the paper does not report.
- For code fidelity, cite the file and symbol for every implemented claim; list claims that are absent, partially implemented, or divergent, and say whether the divergence is intentional.
- Reproduction cost is part of the finding: name hardware, data access, and runtime.

## Rules

- Never present an unsourced claim as sourced, and never cite a source you did not read.
- Do not fetch paywalled, login-walled, or paid-API content without explicit user approval. Report it as a limit instead.
- Do not run experiments, download datasets, or modify the repository in this workflow.
- Quote sparingly and attribute exactly; a paraphrase must not strengthen the source.
- Treat a version mismatch as a limit, not a rounding error.

## Output

The decision and recommendation, a claim table with state, source, and retrieval date, contradictions, threats to validity, limits, confidence, and the next workflow (`rig-plan`, `rig-investigate`, or `rig-optimize`).
